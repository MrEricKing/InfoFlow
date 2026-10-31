from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re


MIN_HTML_VISIBLE_CHARS = 30
MIN_MARKDOWN_VISIBLE_CHARS = 20

_WHITESPACE_PATTERN = re.compile(r"\s+")
_MARKDOWN_TOKEN_PATTERN = re.compile(r"!\[[^\]]*]\([^)]*\)|\[[^\]]+]\([^)]*\)|[`*_>#~-]+")
_JUMP_PROMPT_PATTERN = re.compile(
    r"阅读原文|阅读全文|继续阅读|前往原站查看|read more|continue reading",
    re.IGNORECASE,
)
_KNOWN_ERROR_PATTERNS = (
    "您当前请求存在异常",
    "暂时限制本次访问",
    "知乎小管家",
    "40362",
    "verify you are human",
    "checking your browser",
    "access denied",
    "request blocked",
)


class KnowledgeContentQualityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HtmlQualityResult:
    text: str
    visible_char_count: int


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self.parts.append(data)


def ensure_html_has_readable_content(html: bytes | str, *, source_label: str) -> HtmlQualityResult:
    html_text = _decode_html(html)
    result = analyze_html_content(html_text)
    _raise_for_known_failure(result.text, html_text, source_label=source_label)
    if result.visible_char_count < MIN_HTML_VISIBLE_CHARS:
        raise KnowledgeContentQualityError(
            f"{source_label} does not contain enough readable text ({result.visible_char_count} chars)."
        )
    if result.visible_char_count <= 160 and _JUMP_PROMPT_PATTERN.search(result.text):
        raise KnowledgeContentQualityError(f"{source_label} only contains a read-original jump prompt.")
    return result


def ensure_html_has_no_known_failure(html: bytes | str, *, source_label: str) -> HtmlQualityResult:
    html_text = _decode_html(html)
    result = analyze_html_content(html_text)
    _raise_for_known_failure(result.text, html_text, source_label=source_label)
    return result


def ensure_markdown_has_readable_content(markdown: str, *, source_label: str) -> str:
    text = _markdown_to_visible_text(markdown)
    _raise_for_known_failure(text, markdown, source_label=source_label)
    if len(text) < MIN_MARKDOWN_VISIBLE_CHARS:
        raise KnowledgeContentQualityError(f"{source_label} does not contain enough readable text ({len(text)} chars).")
    if len(text) <= 160 and _JUMP_PROMPT_PATTERN.search(text):
        raise KnowledgeContentQualityError(f"{source_label} only contains a read-original jump prompt.")
    return text


def analyze_html_content(html_text: str) -> HtmlQualityResult:
    parser = _VisibleTextParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    text = _normalize_visible_text(" ".join(parser.parts))
    return HtmlQualityResult(text=text, visible_char_count=len(text))


def _decode_html(html: bytes | str) -> str:
    if isinstance(html, bytes):
        return html.decode("utf-8-sig", errors="replace")
    return html


def _markdown_to_visible_text(markdown: str) -> str:
    without_tokens = _MARKDOWN_TOKEN_PATTERN.sub(" ", markdown)
    return _normalize_visible_text(without_tokens)


def _normalize_visible_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value).strip()


def _raise_for_known_failure(text: str, raw_text: str, *, source_label: str) -> None:
    probe = f"{text}\n{raw_text[:4000]}".lower()
    if any(pattern.lower() in probe for pattern in _KNOWN_ERROR_PATTERNS):
        raise KnowledgeContentQualityError(f"{source_label} appears to be an anti-bot or access-denied page.")
    if _looks_like_json_error(text):
        raise KnowledgeContentQualityError(f"{source_label} appears to be an error response, not article content.")


def _looks_like_json_error(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if isinstance(payload, dict):
        return "error" in payload or "errors" in payload or "code" in payload and "message" in payload
    if isinstance(payload, list):
        return any(isinstance(item, dict) and ("error" in item or "message" in item) for item in payload[:3])
    return False
