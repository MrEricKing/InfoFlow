from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from html.parser import HTMLParser
import re
from typing import Callable
from urllib.parse import urljoin, urldefrag, urlparse

import httpx
from lxml import html as lxml_html
from readability import Document
import trafilatura

from app.core.safety import CAPTURE_RESPONSE_MAX_BYTES, safe_http_get
from app.db.models import FeedItem

CONTENT_TEXT_SOURCE_FEED_CONTENT = "feed_content"
CONTENT_TEXT_SOURCE_FEED_SUMMARY = "feed_summary"
CONTENT_TEXT_SOURCE_EXTRACTED_PAGE = "extracted_page"

CONTENT_EXTRACTION_STATUS_NOT_CHECKED = "not_checked"
CONTENT_EXTRACTION_STATUS_NOT_NEEDED = "not_needed"
CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION = "needs_extraction"
CONTENT_EXTRACTION_STATUS_SUCCEEDED = "succeeded"
CONTENT_EXTRACTION_STATUS_FAILED = "failed"
CONTENT_EXTRACTION_STATUS_SKIPPED = "skipped"
CONTENT_EXTRACTION_SKIP_REASON_DEAD_LINK = "dead_link"

SHORT_VISIBLE_TEXT_THRESHOLD = 40
JUMP_PROMPT_VISIBLE_TEXT_THRESHOLD = 160
MIN_EXTRACTED_VISIBLE_TEXT_CHARS = 200
MIN_SHORT_EXTRACTED_VISIBLE_TEXT_CHARS = 60
MIN_EXTRACTED_TO_FEED_RATIO = 3
SUBSTANTIAL_CJK_TITLE_MISMATCH_BYPASS_CHARS = 1_000
SUBSTANTIAL_CJK_TITLE_MISMATCH_BYPASS_CJK_CHARS = 200
MAX_HTML_BYTES = CAPTURE_RESPONSE_MAX_BYTES
MAX_HTML_REDIRECTS = 2
HTML_REDIRECT_SCAN_CHARS = 500_000
AUTO_FAILURE_COOLDOWN = timedelta(hours=24)
TOO_SHORT_QUALITY_FAILURE = "quality_failed: extracted text is too short"

HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
HORIZONTAL_WHITESPACE_PATTERN = re.compile(r"[^\S\r\n]+")
EXCESSIVE_BLANK_LINES_PATTERN = re.compile(r"\n{3,}")
URL_PATTERN = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)
LATIN_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)
LINK_MARKER_PATTERN = re.compile(r"\b(href=|阅读原文|原文链接|source|link|url)\b", re.IGNORECASE)
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
SENTENCE_MARK_PATTERN = re.compile(r"[。！？.!?]")
TRANSIENT_EXTRACTION_FAILURE_PATTERNS = (
    "timed out",
    "handshake operation timed out",
    "fetch_failed: HTTP 429",
    "fetch_failed: HTTP 500",
    "fetch_failed: HTTP 502",
    "fetch_failed: HTTP 503",
    "fetch_failed: HTTP 504",
)
DEAD_LINK_FETCH_FAILURE_PATTERNS = (
    "fetch_failed: HTTP 404",
    "fetch_failed: HTTP 410",
)
UNSUPPORTED_AUTOMATIC_ARTICLE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
)
SHORT_FORM_FEED_CONTENT_HOSTS = (
    "t.me",
    "telegram.me",
    "twitter.com",
    "x.com",
    "okjike.com",
)
JUMP_PROMPT_PATTERNS = (
    re.compile(r"往原址览之"),
    re.compile(r"前往原站查看"),
    re.compile(r"阅读原文"),
    re.compile(r"阅读全文"),
    re.compile(r"查看原文"),
    re.compile(r"继续阅读"),
    re.compile(r"Read more", re.IGNORECASE),
    re.compile(r"Continue reading", re.IGNORECASE),
    re.compile(r"View original", re.IGNORECASE),
    re.compile(r"Open original", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class FeedContentExtractionDecision:
    should_extract: bool
    status: str
    reason: str
    url: str | None
    visible_char_count: int
    visible_without_urls_char_count: int
    signals: dict[str, bool]


@dataclass(frozen=True, slots=True)
class FeedContentExtractionResult:
    attempted: bool
    succeeded: bool
    status: str
    error: str | None
    decision: FeedContentExtractionDecision


@dataclass(frozen=True, slots=True)
class HtmlRedirectTarget:
    url: str
    kind: str


@dataclass(frozen=True, slots=True)
class ContentExtractionDiagnostics:
    method: str | None
    final_url: str | None
    visible_char_count: int | None


def create_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
        },
    )


def extract_feed_item_content(
    feed_item: FeedItem,
    *,
    force: bool = False,
    record_debug: bool = False,
    client_factory: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> FeedContentExtractionResult:
    timestamp = now or _now_utc()
    resolved_client_factory = client_factory or create_http_client
    decision = decide_feed_item_content_extraction(feed_item, force=force, now=timestamp)
    debug_payload = _base_debug_payload(feed_item, decision, force=force, timestamp=timestamp)

    if not decision.should_extract:
        _apply_non_attempt_status(feed_item, decision)
        if record_debug:
            feed_item.content_extraction_debug_json = debug_payload
        return FeedContentExtractionResult(
            attempted=False,
            succeeded=False,
            status=feed_item.content_extraction_status,
            error=feed_item.content_extraction_error,
            decision=decision,
        )

    feed_item.content_extraction_status = CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION
    feed_item.content_extraction_attempted_at = timestamp
    feed_item.content_extraction_error = None

    previous_attempts: list[dict[str, object]] = []
    retry_reason: str | None = None

    for attempt_number in (1, 2):
        if attempt_number > 1:
            previous_attempts.append(debug_payload)
            debug_payload = _base_debug_payload(feed_item, decision, force=force, timestamp=timestamp)
            debug_payload["retry"] = {
                "attempt": attempt_number,
                "reason": retry_reason,
            }

        fetched_html: str | None = None
        try:
            fetched_html, fetch_debug = _fetch_html(decision.url or "", resolved_client_factory)
            debug_payload["fetch"] = fetch_debug
            extractor_url = str(fetch_debug.get("url") or decision.url or "")
            extracted_content = _extract_reader_content_with_fallback(
                fetched_html,
                extractor_url,
                feed_item=feed_item,
                debug_payload=debug_payload,
            )

            feed_item.content_text = extracted_content
            feed_item.content_text_source = CONTENT_TEXT_SOURCE_EXTRACTED_PAGE
            feed_item.content_extraction_status = CONTENT_EXTRACTION_STATUS_SUCCEEDED
            feed_item.content_extracted_at = timestamp
            feed_item.content_extraction_error = None
            if previous_attempts:
                debug_payload["previous_attempts"] = previous_attempts
            if record_debug:
                feed_item.content_extraction_debug_json = debug_payload
            return FeedContentExtractionResult(
                attempted=True,
                succeeded=True,
                status=CONTENT_EXTRACTION_STATUS_SUCCEEDED,
                error=None,
                decision=decision,
            )
        except Exception as exc:
            error_message = _summarize_error(exc)
            debug_payload.setdefault("fetch", None)
            debug_payload.setdefault("extractor", None)
            debug_payload.setdefault("quality", {"passed": False, "reason": error_message})
            debug_payload["error"] = error_message
            if fetched_html is not None:
                debug_payload["html_preview"] = fetched_html[:500]

            if attempt_number == 1 and _should_retry_extraction_failure(error_message):
                retry_reason = error_message
                continue

            if _is_dead_link_fetch_failure(error_message):
                _apply_dead_link_skip_status(feed_item, error_message=error_message, debug_payload=debug_payload)
                if previous_attempts:
                    debug_payload["previous_attempts"] = previous_attempts
                if record_debug:
                    feed_item.content_extraction_debug_json = debug_payload
                return FeedContentExtractionResult(
                    attempted=True,
                    succeeded=False,
                    status=CONTENT_EXTRACTION_STATUS_SKIPPED,
                    error=None,
                    decision=decision,
                )

            feed_item.content_extraction_status = CONTENT_EXTRACTION_STATUS_FAILED
            feed_item.content_extraction_error = error_message
            if previous_attempts:
                debug_payload["previous_attempts"] = previous_attempts
            if record_debug:
                feed_item.content_extraction_debug_json = debug_payload
            return FeedContentExtractionResult(
                attempted=True,
                succeeded=False,
                status=CONTENT_EXTRACTION_STATUS_FAILED,
                error=error_message,
                decision=decision,
            )

    raise RuntimeError("unreachable feed content extraction attempt state")


def decide_feed_item_content_extraction(
    feed_item: FeedItem,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> FeedContentExtractionDecision:
    timestamp = now or _now_utc()
    url = _extractable_url(feed_item)
    raw_text = _decision_baseline_text(feed_item)
    visible_text = normalize_visible_text(raw_text)
    visible_without_urls = normalize_visible_text(URL_PATTERN.sub(" ", visible_text))
    status = getattr(feed_item, "content_extraction_status", CONTENT_EXTRACTION_STATUS_NOT_CHECKED)

    signals = {
        "has_fetchable_url": url is not None,
        "short_visible_text": len(visible_without_urls) < SHORT_VISIBLE_TEXT_THRESHOLD,
        "url_only": is_url_only_text(visible_text),
        "jump_prompt": has_jump_prompt(visible_text),
        "jump_prompt_with_short_text": has_jump_prompt(visible_text)
        and len(visible_without_urls) < JUMP_PROMPT_VISIBLE_TEXT_THRESHOLD,
        "content_equals_summary": _same_normalized_text(feed_item.content_text, feed_item.summary_text),
        "feed_summary_source": getattr(feed_item, "content_text_source", None) == CONTENT_TEXT_SOURCE_FEED_SUMMARY,
        "short_form_feed_content_host": _is_short_form_feed_content_url(url),
        "recent_auto_failure": _has_recent_auto_failure(feed_item, timestamp),
        "already_succeeded": status == CONTENT_EXTRACTION_STATUS_SUCCEEDED,
    }

    if url is None:
        return _decision(False, CONTENT_EXTRACTION_STATUS_SKIPPED, "missing_fetchable_url", url, visible_text, visible_without_urls, signals)

    if force:
        return _decision(True, CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION, "manual_force", url, visible_text, visible_without_urls, signals)

    if signals["already_succeeded"]:
        return _decision(False, CONTENT_EXTRACTION_STATUS_SUCCEEDED, "already_succeeded", url, visible_text, visible_without_urls, signals)

    if _is_unsupported_automatic_article_url(url) and (
        signals["url_only"] or signals["jump_prompt"] or signals["short_visible_text"]
    ):
        return _decision(
            False,
            CONTENT_EXTRACTION_STATUS_SKIPPED,
            "unsupported_media_page",
            url,
            visible_text,
            visible_without_urls,
            signals,
        )

    if (
        signals["short_form_feed_content_host"]
        and visible_without_urls
        and not signals["url_only"]
        and not signals["jump_prompt"]
    ):
        return _decision(
            False,
            CONTENT_EXTRACTION_STATUS_NOT_NEEDED,
            "short_form_feed_content",
            url,
            visible_text,
            visible_without_urls,
            signals,
        )

    should_extract = (
        signals["url_only"]
        or signals["jump_prompt_with_short_text"]
        or (signals["short_visible_text"] and signals["content_equals_summary"])
        or (signals["short_visible_text"] and signals["feed_summary_source"])
    )
    if not should_extract:
        return _decision(False, CONTENT_EXTRACTION_STATUS_NOT_NEEDED, "feed_content_looks_sufficient", url, visible_text, visible_without_urls, signals)

    if signals["recent_auto_failure"]:
        return _decision(False, CONTENT_EXTRACTION_STATUS_FAILED, "recent_failure_cooldown", url, visible_text, visible_without_urls, signals)

    if should_extract:
        return _decision(True, CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION, "short_or_jump_content", url, visible_text, visible_without_urls, signals)

    raise RuntimeError("unreachable feed content extraction decision state")


def is_probably_url_only_or_jump_prompt(value: str | None) -> bool:
    visible_text = normalize_visible_text(value)
    if not visible_text:
        return False
    visible_without_urls = normalize_visible_text(URL_PATTERN.sub(" ", visible_text))
    return is_url_only_text(visible_text) or (
        has_jump_prompt(visible_text) and len(visible_without_urls) < JUMP_PROMPT_VISIBLE_TEXT_THRESHOLD
    )


def normalize_visible_text(value: str | None) -> str:
    if not value:
        return ""
    without_tags = HTML_TAG_PATTERN.sub(" ", unescape(value))
    return WHITESPACE_PATTERN.sub(" ", without_tags).strip()


def normalize_extracted_text_for_storage(value: str | None) -> str:
    if not value:
        return ""

    normalized = unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = HORIZONTAL_WHITESPACE_PATTERN.sub(" ", normalized)
    lines = [line.strip() for line in normalized.split("\n")]
    collapsed_lines: list[str] = []
    previous_blank = False

    for line in lines:
        if line:
            collapsed_lines.append(line)
            previous_blank = False
            continue
        if not previous_blank:
            collapsed_lines.append("")
        previous_blank = True

    return EXCESSIVE_BLANK_LINES_PATTERN.sub("\n\n", "\n".join(collapsed_lines)).strip()


def is_url_only_text(value: str | None) -> bool:
    visible_text = normalize_visible_text(value)
    if not visible_text:
        return False
    without_urls = normalize_visible_text(URL_PATTERN.sub(" ", visible_text))
    return bool(URL_PATTERN.search(visible_text)) and len(without_urls) < 12


def has_jump_prompt(value: str | None) -> bool:
    visible_text = normalize_visible_text(value)
    return any(pattern.search(visible_text) for pattern in JUMP_PROMPT_PATTERNS)


def validate_extracted_text_quality(
    *,
    extracted_text: str,
    original_text: str | None,
    title: str | None,
) -> tuple[bool, str]:
    normalized_extracted = normalize_visible_text(extracted_text)
    normalized_original = normalize_visible_text(original_text)
    extracted_without_urls = normalize_visible_text(URL_PATTERN.sub(" ", normalized_extracted))

    if is_probably_url_only_or_jump_prompt(normalized_extracted):
        return False, "quality_failed: extracted text still looks like a jump prompt or URL-only body"
    if _looks_like_link_collection(extracted_text):
        return False, "quality_failed: extracted text looks like a navigation or link collection"
    short_text_accepted = _is_acceptable_short_extracted_text(
        extracted_without_urls=extracted_without_urls,
        title=title,
    )
    if len(extracted_without_urls) < MIN_EXTRACTED_VISIBLE_TEXT_CHARS and not short_text_accepted:
        return False, "quality_failed: extracted text is too short"
    if normalized_original:
        original_length = max(1, len(normalize_visible_text(URL_PATTERN.sub(" ", normalized_original))))
        if (
            original_length >= MIN_EXTRACTED_VISIBLE_TEXT_CHARS
            and len(extracted_without_urls) < original_length * MIN_EXTRACTED_TO_FEED_RATIO
        ):
            return False, "quality_failed: extracted text is not substantially longer than feed text"
    if not _title_terms_match_body(title, normalized_extracted) and not _has_substantial_cjk_body_for_mixed_title(
        title,
        extracted_without_urls,
    ):
        return False, "quality_failed: extracted text does not appear related to the item title"
    return True, "passed"


class FeedContentExtractionError(RuntimeError):
    pass


def _extract_reader_content_with_fallback(
    html: str,
    url: str,
    *,
    feed_item: FeedItem,
    debug_payload: dict[str, object],
) -> str:
    original_text = _quality_baseline_text(feed_item)

    try:
        extracted_html = _extract_html_with_readability(html, url)
        normalized_text = normalize_visible_text(extracted_html)
        debug_payload["extractor"] = {
            "name": "readability-lxml",
            "output_format": "html",
            "url": url,
            "extracted_visible_char_count": len(normalized_text),
            "preview": normalized_text[:500],
            "html_preview": extracted_html[:500],
        }
        passed_quality, quality_reason = validate_extracted_text_quality(
            extracted_text=extracted_html,
            original_text=original_text,
            title=feed_item.title,
        )
        debug_payload["quality"] = {
            "extractor": "readability-lxml",
            "passed": passed_quality,
            "reason": quality_reason,
        }
        if not passed_quality:
            raise FeedContentExtractionError(quality_reason)
        return extracted_html
    except Exception as readability_exc:
        readability_error = _summarize_error(readability_exc)
        debug_payload["readability"] = {
            "name": "readability-lxml",
            "output_format": "html",
            "passed": False,
            "error": readability_error,
        }

    try:
        extracted_html = _extract_html_with_static_article_selector(html, url)
        normalized_text = normalize_visible_text(extracted_html)
        debug_payload["extractor"] = {
            "name": "static-article-selector",
            "output_format": "html",
            "url": url,
            "fallback_from": "readability-lxml",
            "extracted_visible_char_count": len(normalized_text),
            "preview": normalized_text[:500],
            "html_preview": extracted_html[:500],
        }
        passed_quality, quality_reason = validate_extracted_text_quality(
            extracted_text=extracted_html,
            original_text=original_text,
            title=feed_item.title,
        )
        debug_payload["quality"] = {
            "extractor": "static-article-selector",
            "passed": passed_quality,
            "reason": quality_reason,
        }
        if not passed_quality:
            raise FeedContentExtractionError(quality_reason)
        return extracted_html
    except Exception as static_exc:
        static_error = _summarize_error(static_exc)
        debug_payload["static_article_selector"] = {
            "name": "static-article-selector",
            "output_format": "html",
            "passed": False,
            "error": static_error,
        }

    try:
        extracted_text = _extract_text_with_trafilatura(html, url)
        readable_extracted_text = normalize_extracted_text_for_storage(extracted_text)
        normalized_extracted_text = normalize_visible_text(readable_extracted_text)
        debug_payload["extractor"] = {
            "name": "trafilatura",
            "output_format": "txt",
            "url": url,
            "fallback_from": "readability-lxml",
            "extracted_visible_char_count": len(normalized_extracted_text),
            "preview": normalized_extracted_text[:500],
            "readable_preview": readable_extracted_text[:500],
        }
        passed_quality, quality_reason = validate_extracted_text_quality(
            extracted_text=readable_extracted_text,
            original_text=original_text,
            title=feed_item.title,
        )
        debug_payload["quality"] = {
            "extractor": "trafilatura",
            "passed": passed_quality,
            "reason": quality_reason,
        }
        if not passed_quality:
            raise FeedContentExtractionError(quality_reason)
        return readable_extracted_text
    except Exception as fallback_exc:
        fallback_error = _summarize_error(fallback_exc)
        debug_payload["fallback"] = {
            "name": "trafilatura",
            "output_format": "txt",
            "passed": False,
            "error": fallback_error,
        }
        raise FeedContentExtractionError(
            f"readability_failed: {readability_error}; static_article_failed: {static_error}; "
            f"fallback_failed: {fallback_error}"
        ) from fallback_exc


def _decision_baseline_text(feed_item: FeedItem) -> str:
    if feed_item.feed_content_text:
        return feed_item.feed_content_text
    if feed_item.content_text_source != CONTENT_TEXT_SOURCE_EXTRACTED_PAGE and feed_item.content_text:
        return feed_item.content_text
    if feed_item.summary_text:
        return feed_item.summary_text
    return feed_item.content_text or ""


def _quality_baseline_text(feed_item: FeedItem) -> str | None:
    if feed_item.feed_content_text:
        return feed_item.feed_content_text
    if feed_item.summary_text:
        return feed_item.summary_text
    if feed_item.content_text_source != CONTENT_TEXT_SOURCE_EXTRACTED_PAGE:
        return feed_item.content_text
    return None


def _fetch_html(url: str, client_factory: Callable[[], httpx.Client]) -> tuple[str, dict[str, object]]:
    with client_factory() as client:
        response = safe_http_get(client, url, max_bytes=MAX_HTML_BYTES, purpose="Feed content extraction")

        request_fallbacks: list[dict[str, object]] = []
        html, debug = _response_html_and_debug_with_trailing_slash_fallback(response, client, request_fallbacks)
        redirects: list[dict[str, object]] = []
        for _ in range(MAX_HTML_REDIRECTS):
            redirect_target = _extract_html_redirect_target(html, base_url=str(response.url))
            if redirect_target is None or _same_url_without_fragment(redirect_target.url, str(response.url)):
                break

            previous_url = str(response.url)
            response = safe_http_get(
                client,
                redirect_target.url,
                max_bytes=MAX_HTML_BYTES,
                purpose="Feed content extraction HTML redirect",
            )
            html, next_debug = _response_html_and_debug(response)
            redirects.append(
                {
                    "kind": redirect_target.kind,
                    "from_url": previous_url,
                    "to_url": str(response.url),
                    "requested_url": redirect_target.url,
                    "http_status": response.status_code,
                }
            )
            debug = next_debug

    if request_fallbacks:
        debug["url_fallbacks"] = request_fallbacks
    if redirects:
        debug["html_redirects"] = redirects
    return html, debug


def _response_html_and_debug_with_trailing_slash_fallback(
    response: httpx.Response,
    client: httpx.Client,
    request_fallbacks: list[dict[str, object]],
) -> tuple[str, dict[str, object]]:
    if response.status_code != 404:
        return _response_html_and_debug(response)

    fallback_url = _without_trailing_path_slash(str(response.url))
    if fallback_url is None:
        return _response_html_and_debug(response)

    previous_url = str(response.url)
    fallback_response = safe_http_get(
        client,
        fallback_url,
        max_bytes=MAX_HTML_BYTES,
        purpose="Feed content extraction trailing-slash fallback",
    )
    html, debug = _response_html_and_debug(fallback_response)
    request_fallbacks.append(
        {
            "kind": "drop_trailing_slash_on_404",
            "from_url": previous_url,
            "requested_url": fallback_url,
            "to_url": str(fallback_response.url),
            "previous_http_status": response.status_code,
            "http_status": fallback_response.status_code,
        }
    )
    return html, debug


def _without_trailing_path_slash(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path == "/" or not parsed.path.endswith("/"):
        return None
    return parsed._replace(path=parsed.path.rstrip("/")).geturl()


def _response_html_and_debug(response: httpx.Response) -> tuple[str, dict[str, object]]:
    content_length = len(response.content)
    debug: dict[str, object] = {
        "url": str(response.url),
        "http_status": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "content_length": content_length,
    }
    if response.status_code < 200 or response.status_code >= 300:
        raise FeedContentExtractionError(f"fetch_failed: HTTP {response.status_code}")
    if content_length > MAX_HTML_BYTES:
        raise FeedContentExtractionError("fetch_failed: HTML response exceeds 50MB limit")
    return response.text, debug


def _extract_html_redirect_target(html: str, *, base_url: str) -> HtmlRedirectTarget | None:
    parser = _HtmlRedirectParser()
    try:
        parser.feed(html[:HTML_REDIRECT_SCAN_CHARS])
    except Exception:
        return None

    if parser.refresh_url:
        target = _normalize_redirect_url(parser.refresh_url, base_url=base_url)
        if target:
            return HtmlRedirectTarget(url=target, kind="meta_refresh")

    return None


def _normalize_redirect_url(value: str, *, base_url: str) -> str | None:
    target = urljoin(base_url, value.strip())
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urldefrag(target)[0]


def _same_url_without_fragment(left: str, right: str) -> bool:
    return urldefrag(left)[0] == urldefrag(right)[0]


def _parse_meta_refresh_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(";")]
    for part in parts[1:]:
        match = re.match(r"url\s*=\s*(.+)", part, flags=re.IGNORECASE)
        if match is None:
            continue
        return match.group(1).strip().strip("'\"") or None
    return None


class _HtmlRedirectParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_url: str | None = None
        self.refresh_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): value for name, value in attrs if value is not None}
        normalized_tag = tag.lower()
        if normalized_tag == "link" and self.canonical_url is None:
            rel_tokens = {token.strip().lower() for token in (attr_map.get("rel") or "").split()}
            href = attr_map.get("href")
            if "canonical" in rel_tokens and href:
                self.canonical_url = href
        elif normalized_tag == "meta" and self.refresh_url is None:
            http_equiv = (attr_map.get("http-equiv") or "").strip().lower()
            if http_equiv == "refresh":
                self.refresh_url = _parse_meta_refresh_url(attr_map.get("content"))


def _extract_html_with_readability(html: str, url: str) -> str:
    extracted = Document(html, url=url).summary(html_partial=True, keep_all_images=True)
    if not extracted:
        raise FeedContentExtractionError("extract_failed: readability-lxml returned no HTML")

    normalized = _normalize_reader_html_for_storage(extracted, base_url=url)
    if not normalize_visible_text(normalized):
        raise FeedContentExtractionError("extract_failed: readability-lxml returned no visible text")
    return normalized


def _extract_html_with_static_article_selector(html: str, url: str) -> str:
    try:
        root = lxml_html.fromstring(html)
    except Exception as exc:
        raise FeedContentExtractionError("extract_failed: static selector received invalid HTML") from exc

    selectors = (
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' vp-doc ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' theme-default-content ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' markdown-body ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' entry-content ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' cm-post-content ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' post-content ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' article-content ')]",
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' post-body ')]",
        "//*[@id='content']//article",
        "//main//article",
        "//article",
    )
    for selector in selectors:
        candidates = [node for node in root.xpath(selector) if isinstance(getattr(node, "tag", None), str)]
        candidates.sort(key=lambda node: len(normalize_visible_text(_serialize_reader_html(node))), reverse=True)
        for candidate in candidates:
            serialized = _serialize_reader_html(candidate)
            normalized = _normalize_reader_html_for_storage(serialized, base_url=url)
            if normalize_visible_text(normalized):
                return normalized

    raise FeedContentExtractionError("extract_failed: static selector found no article container")


def _normalize_reader_html_for_storage(value: str, *, base_url: str) -> str:
    try:
        root = lxml_html.fromstring(value)
    except Exception as exc:
        raise FeedContentExtractionError("extract_failed: readability-lxml returned invalid HTML") from exc

    for element in list(root.iter()):
        if not isinstance(element.tag, str):
            continue

        tag = element.tag.lower()
        if tag == "a":
            _normalize_http_url_attribute(element, "href", base_url=base_url)
        elif tag == "img":
            if not _normalize_http_url_attribute(element, "src", base_url=base_url):
                element.drop_tree()
                continue
            element.attrib.pop("srcset", None)

    serialized = _serialize_reader_html(root)
    if not serialized:
        raise FeedContentExtractionError("extract_failed: readability-lxml returned empty HTML")
    return serialized


def _normalize_http_url_attribute(element: lxml_html.HtmlElement, attr_name: str, *, base_url: str) -> bool:
    raw_value = element.get(attr_name)
    if raw_value is None:
        return False

    normalized = _normalize_http_url(raw_value, base_url=base_url)
    if normalized is None:
        element.attrib.pop(attr_name, None)
        return False

    element.set(attr_name, normalized)
    return True


def _normalize_http_url(value: str, *, base_url: str) -> str | None:
    target = urljoin(base_url, value.strip())
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urldefrag(target)[0]


def _serialize_reader_html(root: lxml_html.HtmlElement) -> str:
    body_nodes = root.xpath(".//body") if hasattr(root, "xpath") else []
    body = body_nodes[0] if body_nodes else root if isinstance(root.tag, str) and root.tag.lower() == "body" else None
    if body is not None:
        return _serialize_inner_html(body)
    return lxml_html.tostring(root, encoding="unicode", method="html").strip()


def _serialize_inner_html(element: lxml_html.HtmlElement) -> str:
    parts: list[str] = []
    if element.text and element.text.strip():
        parts.append(escape(element.text.strip()))
    parts.extend(lxml_html.tostring(child, encoding="unicode", method="html") for child in element)
    return "".join(parts).strip()


def _extract_text_with_trafilatura(html: str, url: str) -> str:
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="txt",
        include_comments=False,
        include_tables=False,
    )
    if not extracted:
        raise FeedContentExtractionError("extract_failed: trafilatura returned no text")
    return extracted


def _apply_non_attempt_status(feed_item: FeedItem, decision: FeedContentExtractionDecision) -> None:
    if decision.status == CONTENT_EXTRACTION_STATUS_SUCCEEDED:
        return
    if decision.reason == "recent_failure_cooldown":
        return
    feed_item.content_extraction_status = decision.status
    if decision.status in {CONTENT_EXTRACTION_STATUS_NOT_NEEDED, CONTENT_EXTRACTION_STATUS_SKIPPED}:
        feed_item.content_extraction_error = None


def _base_debug_payload(
    feed_item: FeedItem,
    decision: FeedContentExtractionDecision,
    *,
    force: bool,
    timestamp: datetime,
) -> dict[str, object]:
    return {
        "trigger": "manual" if force else "automatic",
        "attempted_at": timestamp.isoformat(),
        "feed_item_id": feed_item.id,
        "title": feed_item.title,
        "content_text_source": feed_item.content_text_source,
        "previous_status": feed_item.content_extraction_status,
        "decision": {
            "should_extract": decision.should_extract,
            "status": decision.status,
            "reason": decision.reason,
            "url": decision.url,
            "visible_char_count": decision.visible_char_count,
            "visible_without_urls_char_count": decision.visible_without_urls_char_count,
            "signals": decision.signals,
        },
    }


def read_content_extraction_diagnostics(feed_item: FeedItem) -> ContentExtractionDiagnostics:
    return read_content_extraction_diagnostics_from_debug(feed_item.content_extraction_debug_json)


def read_content_extraction_diagnostics_from_debug(debug_payload: object) -> ContentExtractionDiagnostics:
    if not isinstance(debug_payload, dict):
        return ContentExtractionDiagnostics(method=None, final_url=None, visible_char_count=None)

    fetch = debug_payload.get("fetch")
    extractor = debug_payload.get("extractor")
    final_url = _read_debug_string(fetch, "url") if isinstance(fetch, dict) else None
    method = _read_debug_string(extractor, "name") if isinstance(extractor, dict) else None
    extractor_url = _read_debug_string(extractor, "url") if isinstance(extractor, dict) else None
    visible_char_count = (
        _read_debug_int(extractor, "extracted_visible_char_count") if isinstance(extractor, dict) else None
    )

    return ContentExtractionDiagnostics(
        method=method,
        final_url=final_url or extractor_url,
        visible_char_count=visible_char_count,
    )


def _read_debug_string(value: dict[object, object], key: str) -> str | None:
    candidate = value.get(key)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return None


def _read_debug_int(value: dict[object, object], key: str) -> int | None:
    candidate = value.get(key)
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, int):
        return candidate
    return None


def _should_retry_extraction_failure(error_message: str) -> bool:
    normalized = error_message.casefold()
    return TOO_SHORT_QUALITY_FAILURE in error_message or any(
        pattern.casefold() in normalized for pattern in TRANSIENT_EXTRACTION_FAILURE_PATTERNS
    )


def is_dead_link_fetch_failure(error_message: str | None) -> bool:
    return _is_dead_link_fetch_failure(error_message)


def mark_feed_item_extraction_dead_link(
    feed_item: FeedItem,
    *,
    timestamp: datetime | None = None,
    error_message: str | None = None,
    record_debug: bool = True,
) -> None:
    reason_error = error_message or feed_item.content_extraction_error or "fetch_failed: HTTP 404"
    visible_text = normalize_visible_text(_decision_baseline_text(feed_item))
    visible_without_urls = normalize_visible_text(URL_PATTERN.sub(" ", visible_text))
    debug_payload = _base_debug_payload(
        feed_item,
        FeedContentExtractionDecision(
            should_extract=False,
            status=CONTENT_EXTRACTION_STATUS_SKIPPED,
            reason=CONTENT_EXTRACTION_SKIP_REASON_DEAD_LINK,
            url=_extractable_url(feed_item),
            visible_char_count=len(visible_text),
            visible_without_urls_char_count=len(visible_without_urls),
            signals={},
        ),
        force=False,
        timestamp=timestamp or _now_utc(),
    )
    _apply_dead_link_skip_status(feed_item, error_message=reason_error, debug_payload=debug_payload)
    if record_debug:
        feed_item.content_extraction_debug_json = debug_payload


def _is_dead_link_fetch_failure(error_message: str | None) -> bool:
    if not error_message:
        return False
    return any(pattern in error_message for pattern in DEAD_LINK_FETCH_FAILURE_PATTERNS)


def _apply_dead_link_skip_status(
    feed_item: FeedItem,
    *,
    error_message: str,
    debug_payload: dict[str, object],
) -> None:
    feed_item.content_extraction_status = CONTENT_EXTRACTION_STATUS_SKIPPED
    feed_item.content_extraction_error = None
    debug_payload["terminal"] = {
        "status": CONTENT_EXTRACTION_STATUS_SKIPPED,
        "reason": CONTENT_EXTRACTION_SKIP_REASON_DEAD_LINK,
        "error": error_message,
    }


def _decision(
    should_extract: bool,
    status: str,
    reason: str,
    url: str | None,
    visible_text: str,
    visible_without_urls: str,
    signals: dict[str, bool],
) -> FeedContentExtractionDecision:
    return FeedContentExtractionDecision(
        should_extract=should_extract,
        status=status,
        reason=reason,
        url=url,
        visible_char_count=len(visible_text),
        visible_without_urls_char_count=len(visible_without_urls),
        signals=signals,
    )


def _extractable_url(feed_item: FeedItem) -> str | None:
    candidate = feed_item.canonical_url or feed_item.original_url
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _is_unsupported_automatic_article_url(value: str | None) -> bool:
    return _url_host_matches(value, UNSUPPORTED_AUTOMATIC_ARTICLE_HOSTS)


def _is_short_form_feed_content_url(value: str | None) -> bool:
    return _url_host_matches(value, SHORT_FORM_FEED_CONTENT_HOSTS)


def _url_host_matches(value: str | None, hosts: tuple[str, ...]) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return any(hostname == host or hostname.endswith(f".{host}") for host in hosts)


def _has_recent_auto_failure(feed_item: FeedItem, now: datetime) -> bool:
    if feed_item.content_extraction_status != CONTENT_EXTRACTION_STATUS_FAILED:
        return False
    attempted_at = feed_item.content_extraction_attempted_at
    if attempted_at is None:
        return False
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    return now - attempted_at.astimezone(timezone.utc) < AUTO_FAILURE_COOLDOWN


def _same_normalized_text(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return normalize_visible_text(left) == normalize_visible_text(right)


def _looks_like_link_collection(value: str) -> bool:
    visible_text = normalize_visible_text(value)
    if len(visible_text) < MIN_EXTRACTED_VISIBLE_TEXT_CHARS:
        return False
    visible_without_urls = normalize_visible_text(URL_PATTERN.sub(" ", visible_text))
    raw_lines = [line for line in value.splitlines() if normalize_visible_text(line)]
    prose_line_count = sum(1 for line in raw_lines if _looks_like_prose_line(line))

    if len(visible_without_urls) >= 300 and prose_line_count >= 1:
        return False

    url_count = len(URL_PATTERN.findall(visible_text))
    line_count = max(1, len(raw_lines))
    link_marker_count = len(LINK_MARKER_PATTERN.findall(value))
    return (url_count >= 8 and prose_line_count < 2) or (
        line_count >= 8 and link_marker_count >= line_count // 2 and prose_line_count < max(2, line_count // 4)
    )


def _looks_like_prose_line(value: str) -> bool:
    visible_without_urls = normalize_visible_text(URL_PATTERN.sub(" ", value))
    if len(visible_without_urls) < 40:
        return False
    if CJK_PATTERN.search(visible_without_urls):
        return True
    if SENTENCE_MARK_PATTERN.search(visible_without_urls):
        return True
    return len(visible_without_urls.split()) >= 12


def _is_acceptable_short_extracted_text(*, extracted_without_urls: str, title: str | None) -> bool:
    if len(extracted_without_urls) < MIN_SHORT_EXTRACTED_VISIBLE_TEXT_CHARS:
        return False
    if not _title_terms_match_body(title, extracted_without_urls):
        return False
    if CJK_PATTERN.search(extracted_without_urls):
        return True
    if SENTENCE_MARK_PATTERN.search(extracted_without_urls):
        return True
    return len(extracted_without_urls.split()) >= 8


def _title_terms_match_body(title: str | None, body: str) -> bool:
    title_terms = [term.casefold() for term in LATIN_TOKEN_PATTERN.findall(title or "")]
    unique_terms = list(dict.fromkeys(title_terms))
    if len(unique_terms) < 2:
        return True
    body_casefold = body.casefold()
    return any(term in body_casefold for term in unique_terms[:6])


def _has_substantial_cjk_body_for_mixed_title(title: str | None, body: str) -> bool:
    if not title or not CJK_PATTERN.search(title):
        return False
    if len(body) < SUBSTANTIAL_CJK_TITLE_MISMATCH_BYPASS_CHARS:
        return False
    return len(CJK_PATTERN.findall(body)) >= SUBSTANTIAL_CJK_TITLE_MISMATCH_BYPASS_CJK_CHARS


def _summarize_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if len(message) > 500:
        return f"{message[:497]}..."
    return message


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
