from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import unescape
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.safety import (
    CAPTURE_RESPONSE_MAX_BYTES,
    URLSafetyError,
    safe_http_get,
    validate_public_http_redirect_chain,
    validate_public_http_url,
)
from app.core.config import get_settings
from app.services.capture_tools.browser_locale import (
    CAPTURE_BROWSER_LOCALE,
    build_accept_language_headers,
)
from app.services.capture_tools.models import CaptureToolDiagnostic, CaptureToolUnavailableError, Crawl4AICaptureResult

Crawl4AIToolArgs = dict[str, object]

CRAWL4AI_TOOL_NAME = "crawl4ai"
CRAWL4AI_PROFILE = "crawl4ai_dynamic_markdown_v1"
CRAWL4AI_UPLOAD_PROFILE = "knowledge_upload_html_markdown_v1"
CRAWL4AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 15
CRAWL4AI_MAX_IMAGE_BYTES = 10 * 1024 * 1024
CRAWL4AI_BROWSER_HEADERS = build_accept_language_headers()
CRAWL4AI_IMAGE_DOWNLOAD_HEADERS = {"Accept-Encoding": "gzip, deflate", **CRAWL4AI_BROWSER_HEADERS}
CRAWL4AI_PAGE_TIMEOUT_MS = 120_000
CRAWL4AI_SCROLL_DELAY_SECONDS = 0.55
CRAWL4AI_MAX_SCROLL_STEPS = 24
CRAWL4AI_DELAY_BEFORE_RETURN_HTML_SECONDS = 1.5
CRAWL4AI_MARKDOWN_OPTIONS = {
    "body_width": 0,
    "ignore_images": False,
    "skip_internal_links": True,
}
CRAWL4AI_PRUNING_THRESHOLD = 0.48
CRAWL4AI_PRUNING_THRESHOLD_TYPE = "fixed"
CRAWL4AI_IGNORED_IMAGE_HOSTS = (
    "googlesyndication.com",
    "doubleclick.net",
    "googleadservices.com",
)
CRAWL4AI_PRE_CAPTURE_SCRIPT = """
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const triggerViewportEvents = () => {
  window.dispatchEvent(new Event("scroll", { bubbles: true }));
  window.dispatchEvent(new Event("resize", { bubbles: true }));
  document.dispatchEvent(new Event("scroll", { bubbles: true }));
};
const bodyText = () => (document.body && document.body.innerText) || "";
const loadingTokenCount = () => (bodyText().match(/Loading\\.\\.\\./g) || []).length;
const waitForStableBody = async (maxWaitMs) => {
  const startedAt = Date.now();
  let lastLength = -1;
  let stableSamples = 0;
  let bestLoadingCount = loadingTokenCount();
  while (Date.now() - startedAt < maxWaitMs) {
    await sleep(700);
    const textLength = bodyText().length;
    const currentLoadingCount = loadingTokenCount();
    if (textLength === lastLength && currentLoadingCount >= bestLoadingCount) {
      stableSamples += 1;
    } else {
      stableSamples = 0;
      lastLength = textLength;
      bestLoadingCount = Math.min(bestLoadingCount, currentLoadingCount);
    }
    if (stableSamples >= 3) {
      break;
    }
  }
};
window.__infoflowCrawl4AIPreCapture = true;
await sleep(1200);
const step = Math.max(360, Math.floor(window.innerHeight * 0.75));
let lastHeight = 0;
for (let pass = 0; pass < 3; pass += 1) {
  const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  for (let y = 0; y <= height; y += step) {
    window.scrollTo(0, y);
    triggerViewportEvents();
    await sleep(450);
  }
  window.scrollTo(0, Math.max(0, height - window.innerHeight));
  triggerViewportEvents();
  await waitForStableBody(4200);
  if (height === lastHeight) {
    break;
  }
  lastHeight = height;
}
await sleep(1200);
window.scrollTo(0, 0);
triggerViewportEvents();
await sleep(700);
return {
  bodyLength: bodyText().length,
  loadingTokenCount: loadingTokenCount(),
};
""".strip()

MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url><[^>]+>|[^)\s]+)(?P<title>\s+['\"][^)]*['\"])?\)"
)
HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
IMAGE_EXTENSIONS_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}
IMAGE_MIME_BY_EXTENSION = {value: key for key, value in IMAGE_EXTENSIONS_BY_MIME.items()}
IMAGE_MIME_BY_EXTENSION["jpg"] = "image/jpeg"


class Crawl4AICaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _MarkdownPayload:
    raw_markdown: str
    fit_markdown: str
    image_refs: list[tuple[str, str | None]]
    page_title: str | None
    raw_html: str | None


def get_crawl4ai_runtime_info() -> dict[str, object]:
    settings = get_settings()
    return {
        "tool_name": CRAWL4AI_TOOL_NAME,
        "tool_version": settings.crawl4ai_version,
        "integration_mode": "python_api",
        "profile": CRAWL4AI_PROFILE,
    }


def build_crawl4ai_tool_args(url: str) -> Crawl4AIToolArgs:
    normalized_url = validate_public_http_url(url, purpose="Crawl4AI capture", resolve_dns=False)
    return {
        "url": normalized_url,
        "profile": CRAWL4AI_PROFILE,
        "integration_mode": "python_api",
        "markdown_generator": "DefaultMarkdownGenerator",
        "content_filter": {
            "name": "PruningContentFilter",
            "threshold": CRAWL4AI_PRUNING_THRESHOLD,
            "threshold_type": CRAWL4AI_PRUNING_THRESHOLD_TYPE,
        },
        "markdown_options": CRAWL4AI_MARKDOWN_OPTIONS,
        "browser_capture": {
            "pre_capture_script": "generic_scroll_stability_v1",
            "page_timeout_ms": CRAWL4AI_PAGE_TIMEOUT_MS,
            "wait_for_images": True,
            "scan_full_page": True,
            "scroll_delay_seconds": CRAWL4AI_SCROLL_DELAY_SECONDS,
            "max_scroll_steps": CRAWL4AI_MAX_SCROLL_STEPS,
            "process_iframes": True,
            "flatten_shadow_dom": True,
            "delay_before_return_html_seconds": CRAWL4AI_DELAY_BEFORE_RETURN_HTML_SECONDS,
        },
        "image_download": {
            "enabled": True,
            "timeout_seconds": CRAWL4AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
            "max_image_bytes": CRAWL4AI_MAX_IMAGE_BYTES,
        },
        "fallback_fetch": {
            "enabled": True,
            "timeout_seconds": CRAWL4AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        },
    }


def build_crawl4ai_upload_tool_args(source_name: str | None = None) -> Crawl4AIToolArgs:
    return {
        "source": source_name or "uploaded_html",
        "profile": CRAWL4AI_UPLOAD_PROFILE,
        "integration_mode": "python_api",
        "input_mode": "raw_html",
        "markdown_generator": "DefaultMarkdownGenerator",
        "content_filter": {
            "name": "PruningContentFilter",
            "threshold": CRAWL4AI_PRUNING_THRESHOLD,
            "threshold_type": CRAWL4AI_PRUNING_THRESHOLD_TYPE,
        },
        "markdown_options": CRAWL4AI_MARKDOWN_OPTIONS,
        "browser_capture": {
            "page_timeout_ms": CRAWL4AI_PAGE_TIMEOUT_MS,
            "wait_for_images": False,
            "scan_full_page": False,
            "process_iframes": False,
            "flatten_shadow_dom": False,
        },
        "image_download": {
            "enabled": False,
        },
    }


def capture_markdown(url: str) -> Crawl4AICaptureResult:
    normalized_url = _preflight_capture_url(url)

    try:
        import crawl4ai  # type: ignore # noqa: F401
    except ImportError as exc:
        raise CaptureToolUnavailableError(
            "Crawl4AI is not installed in the current backend environment"
        ) from exc

    payload = asyncio.run(_capture_markdown_payload(normalized_url))
    assets, diagnostics = _download_markdown_images(payload.image_refs)
    return Crawl4AICaptureResult(
        raw_markdown=payload.raw_markdown,
        fit_markdown=payload.fit_markdown,
        image_assets=assets,
        diagnostics=diagnostics,
        page_title=payload.page_title,
        raw_html=payload.raw_html,
    )


def capture_uploaded_html_markdown(html: str, *, source_name: str | None = None) -> Crawl4AICaptureResult:
    if not html.strip():
        raise ValueError("Uploaded HTML must not be empty")

    try:
        import crawl4ai  # type: ignore # noqa: F401
    except ImportError as exc:
        raise CaptureToolUnavailableError(
            "Crawl4AI is not installed in the current backend environment"
        ) from exc

    payload = asyncio.run(
        _capture_markdown_payload(
            f"raw:{html}",
            normalize_image_refs=False,
            browser_profile=CRAWL4AI_UPLOAD_PROFILE,
        )
    )
    return Crawl4AICaptureResult(
        raw_markdown=payload.raw_markdown,
        fit_markdown=payload.fit_markdown,
        image_assets=[],
        diagnostics=[],
        page_title=payload.page_title,
        raw_html=payload.raw_html or html,
    )


def build_crawl4ai_scaffold_diagnostic(message: str) -> CaptureToolDiagnostic:
    return CaptureToolDiagnostic(
        code="crawl4ai_scaffold",
        message=message,
        level="info",
    )


async def _capture_markdown_payload(
    url: str,
    *,
    normalize_image_refs: bool = True,
    browser_profile: str = CRAWL4AI_PROFILE,
) -> _MarkdownPayload:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    markdown_generator = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(
            threshold=CRAWL4AI_PRUNING_THRESHOLD,
            threshold_type=CRAWL4AI_PRUNING_THRESHOLD_TYPE,
        ),
        options=CRAWL4AI_MARKDOWN_OPTIONS,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        markdown_generator=markdown_generator,
        fallback_fetch_function=_fallback_fetch_html,
        locale=CAPTURE_BROWSER_LOCALE,
        page_timeout=CRAWL4AI_PAGE_TIMEOUT_MS,
        wait_for_images=normalize_image_refs,
        delay_before_return_html=CRAWL4AI_DELAY_BEFORE_RETURN_HTML_SECONDS if normalize_image_refs else 0,
        js_code=CRAWL4AI_PRE_CAPTURE_SCRIPT if normalize_image_refs else None,
        scan_full_page=normalize_image_refs,
        scroll_delay=CRAWL4AI_SCROLL_DELAY_SECONDS if normalize_image_refs else 0,
        max_scroll_steps=CRAWL4AI_MAX_SCROLL_STEPS if normalize_image_refs else 0,
        process_iframes=normalize_image_refs,
        flatten_shadow_dom=normalize_image_refs,
        verbose=False,
    )
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        headers=CRAWL4AI_BROWSER_HEADERS,
        extra_args=[f"--lang={CAPTURE_BROWSER_LOCALE}"],
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    if not getattr(result, "success", False):
        error_message = getattr(result, "error_message", None) or "Crawl4AI crawl failed"
        status_code = getattr(result, "status_code", None)
        if status_code is not None:
            error_message = f"{error_message} (status_code={status_code})"
        raise Crawl4AICaptureError(error_message)

    markdown = getattr(result, "markdown", None)
    raw_markdown = _extract_markdown_text(markdown, "raw_markdown")
    fit_markdown = _extract_markdown_text(markdown, "fit_markdown") or raw_markdown
    if not raw_markdown:
        raise Crawl4AICaptureError("Crawl4AI returned empty markdown")
    _validate_capture_text_size(raw_markdown, field_name="raw_markdown")
    _validate_capture_text_size(fit_markdown, field_name="fit_markdown")

    crawled_url = getattr(result, "url", None) or url
    raw_refs: list[tuple[str, str | None]] = []
    fit_refs: list[tuple[str, str | None]] = []
    media_refs: list[tuple[str, str | None]] = []
    if normalize_image_refs:
        raw_markdown, raw_refs = _normalize_markdown_image_urls(raw_markdown, crawled_url)
        fit_markdown, fit_refs = _normalize_markdown_image_urls(fit_markdown, crawled_url)
        media_refs = _extract_media_image_refs(getattr(result, "media", None), crawled_url)
    image_refs = _dedupe_image_refs([*raw_refs, *fit_refs, *media_refs])
    raw_html = _extract_result_html(result)
    _validate_capture_text_size(raw_html, field_name="raw_html")
    return _MarkdownPayload(
        raw_markdown=raw_markdown,
        fit_markdown=fit_markdown,
        image_refs=image_refs,
        page_title=_extract_result_page_title(result, raw_html),
        raw_html=raw_html,
    )


async def _fallback_fetch_html(url: str) -> str:
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=CRAWL4AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        headers=CRAWL4AI_BROWSER_HEADERS,
    ) as client:
        response = await _async_safe_http_get(
            client,
            url,
            max_bytes=CAPTURE_RESPONSE_MAX_BYTES,
            purpose="Crawl4AI fallback fetch",
        )
        response.raise_for_status()
        return response.text


async def _async_safe_http_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    purpose: str,
    redirect_limit: int = 5,
) -> httpx.Response:
    current_url = validate_public_http_url(url, purpose=purpose, resolve_dns=True)
    for redirect_count in range(redirect_limit + 1):
        async with client.stream("GET", current_url, follow_redirects=False) as streamed_response:
            response = await _async_materialize_limited_response(
                streamed_response,
                max_bytes=max_bytes,
                purpose=purpose,
            )

        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        if redirect_count >= redirect_limit:
            raise URLSafetyError(f"{purpose} exceeded redirect limit")
        current_url = validate_public_http_url(
            urljoin(str(response.url), location),
            purpose=purpose,
            resolve_dns=True,
        )

    raise URLSafetyError(f"{purpose} exceeded redirect limit")


async def _async_materialize_limited_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    purpose: str,
) -> httpx.Response:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
        except ValueError:
            parsed_content_length = None
        if parsed_content_length is not None and parsed_content_length > max_bytes:
            raise URLSafetyError(f"{purpose} response exceeds {max_bytes} byte limit")

    chunks: list[bytes] = []
    total_size = 0
    async for chunk in response.aiter_bytes():
        total_size += len(chunk)
        if total_size > max_bytes:
            raise URLSafetyError(f"{purpose} response exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=response.request,
        extensions=response.extensions,
    )


def _extract_markdown_text(markdown: object, field_name: str) -> str:
    value = getattr(markdown, field_name, None)
    if value is None and field_name == "raw_markdown":
        value = markdown
    if value is None:
        return ""
    return str(value).strip()


def _validate_capture_text_size(value: str | None, *, field_name: str) -> None:
    if value is None:
        return
    if len(value.encode("utf-8", errors="replace")) > CAPTURE_RESPONSE_MAX_BYTES:
        raise Crawl4AICaptureError(f"Crawl4AI {field_name} exceeds {CAPTURE_RESPONSE_MAX_BYTES} byte limit")


def _extract_result_html(result: object) -> str | None:
    for field_name in ("html", "cleaned_html", "fit_html"):
        value = getattr(result, field_name, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_result_page_title(result: object, raw_html: str | None) -> str | None:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("title", "og:title", "twitter:title"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_title(value)

    if raw_html:
        match = HTML_TITLE_PATTERN.search(raw_html)
        if match is not None:
            return _normalize_title(re.sub(r"<[^>]+>", " ", match.group("title")))
    return None


def _normalize_title(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", unescape(value)).strip()
    return normalized or None


def _normalize_markdown_image_urls(markdown: str, base_url: str) -> tuple[str, list[tuple[str, str | None]]]:
    refs: list[tuple[str, str | None]] = []

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        unwrapped_url = raw_url[1:-1].strip() if raw_url.startswith("<") and raw_url.endswith(">") else raw_url
        if _is_ignored_image_url(urljoin(base_url, unwrapped_url)):
            return ""
        absolute_url = _normalize_image_url(unwrapped_url, base_url)
        if absolute_url is None:
            return match.group(0)

        alt_text = match.group("alt").strip() or None
        refs.append((absolute_url, alt_text))
        title = match.group("title") or ""
        return f"![{match.group('alt')}]({absolute_url}{title})"

    return MARKDOWN_IMAGE_PATTERN.sub(replace, markdown), refs


def _extract_media_image_refs(media: object, base_url: str) -> list[tuple[str, str | None]]:
    if not isinstance(media, dict):
        return []
    images = media.get("images")
    if not isinstance(images, list):
        return []

    refs: list[tuple[str, str | None]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        raw_url = image.get("src") or image.get("url")
        if not isinstance(raw_url, str):
            continue
        absolute_url = _normalize_image_url(raw_url, base_url)
        if absolute_url is None:
            continue
        alt_text = image.get("alt")
        refs.append((absolute_url, alt_text.strip() if isinstance(alt_text, str) and alt_text.strip() else None))
    return refs


def _normalize_image_url(raw_url: str, base_url: str) -> str | None:
    normalized = raw_url.strip()
    if not normalized or normalized.startswith("data:"):
        return None
    absolute_url = urljoin(base_url, normalized)
    try:
        absolute_url = validate_public_http_url(
            absolute_url,
            purpose="Crawl4AI image reference",
            resolve_dns=False,
        )
    except URLSafetyError:
        return None
    if _is_ignored_image_url(absolute_url):
        return None
    return absolute_url


def _is_ignored_image_url(image_url: str) -> bool:
    parsed = urlsplit(image_url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    return any(
        host == ignored_host or host.endswith(f".{ignored_host}")
        for ignored_host in CRAWL4AI_IGNORED_IMAGE_HOSTS
    )


def _dedupe_image_refs(refs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for url, alt_text in refs:
        if url in seen:
            continue
        seen.add(url)
        result.append((url, alt_text))
    return result


def _download_markdown_images(
    image_refs: list[tuple[str, str | None]],
) -> tuple[list, list[CaptureToolDiagnostic]]:
    from app.services.capture_tools.models import CaptureAssetPayload

    assets: list[CaptureAssetPayload] = []
    diagnostics: list[CaptureToolDiagnostic] = []
    if not image_refs:
        return assets, diagnostics

    with httpx.Client(follow_redirects=True, timeout=CRAWL4AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS) as client:
        for index, (image_url, alt_text) in enumerate(image_refs):
            try:
                response = _safe_image_get(client, image_url)
                response.raise_for_status()
            except (httpx.HTTPError, URLSafetyError, ValueError) as exc:
                diagnostics.append(
                    CaptureToolDiagnostic(
                        code="crawl4ai_image_download_failed",
                        message=f"Failed to download image {image_url}: {exc}",
                        level="warning",
                    )
                )
                continue

            content = response.content
            if not content:
                diagnostics.append(
                    CaptureToolDiagnostic(
                        code="crawl4ai_image_empty",
                        message=f"Image response was empty: {image_url}",
                        level="warning",
                    )
                )
                continue
            if len(content) > CRAWL4AI_MAX_IMAGE_BYTES:
                diagnostics.append(
                    CaptureToolDiagnostic(
                        code="crawl4ai_image_too_large",
                        message=f"Image exceeds max size {CRAWL4AI_MAX_IMAGE_BYTES} bytes: {image_url}",
                        level="warning",
                    )
                )
                continue

            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            file_ext = _infer_image_extension(image_url, content_type)
            mime_type = content_type if content_type.startswith("image/") else IMAGE_MIME_BY_EXTENSION.get(file_ext)
            if mime_type is None:
                diagnostics.append(
                    CaptureToolDiagnostic(
                        code="crawl4ai_image_unsupported_type",
                        message=f"Unsupported image content type for {image_url}: {content_type or 'undetected'}",
                        level="warning",
                    )
                )
                continue

            assets.append(
                CaptureAssetPayload(
                    logical_name=_build_image_logical_name(image_url, index),
                    source_url=image_url,
                    mime_type=mime_type,
                    file_ext=file_ext,
                    content_bytes=content,
                    alt_text=alt_text,
                    metadata={
                        "tool_name": CRAWL4AI_TOOL_NAME,
                        "tool_version": get_settings().crawl4ai_version,
                    },
                )
            )

    return assets, diagnostics


def _safe_image_get(client: httpx.Client, image_url: str) -> httpx.Response:
    if hasattr(client, "_transport"):
        return safe_http_get(
            client,
            image_url,
            headers=CRAWL4AI_IMAGE_DOWNLOAD_HEADERS,
            max_bytes=CRAWL4AI_MAX_IMAGE_BYTES,
            purpose="Crawl4AI image download",
        )
    validate_public_http_url(image_url, purpose="Crawl4AI image download", resolve_dns=False)
    return client.get(image_url, headers=CRAWL4AI_IMAGE_DOWNLOAD_HEADERS)


def _preflight_capture_url(url: str) -> str:
    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
        headers=CRAWL4AI_BROWSER_HEADERS,
    ) as client:
        return validate_public_http_redirect_chain(client, url, purpose="Crawl4AI capture")


def _infer_image_extension(image_url: str, content_type: str) -> str:
    if content_type in IMAGE_EXTENSIONS_BY_MIME:
        return IMAGE_EXTENSIONS_BY_MIME[content_type]

    suffix = Path(urlsplit(image_url).path).suffix.lower().removeprefix(".")
    if suffix == "jpeg":
        return "jpg"
    if suffix in IMAGE_MIME_BY_EXTENSION:
        return suffix
    return "bin"


def _build_image_logical_name(image_url: str, index: int) -> str:
    stem = Path(urlsplit(image_url).path).stem.strip()
    return stem or f"image-{index + 1}"
