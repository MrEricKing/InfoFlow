from __future__ import annotations

import re

from app.services.saved_item_insights import (
    ORIGIN_TYPE_HTML_UPLOAD,
    ORIGIN_TYPE_MARKDOWN_UPLOAD,
    ORIGIN_TYPE_SINGLEFILE_HTML_UPLOAD,
)

CANONICAL_SNAPSHOT_KIND = "canonical_content_v1"
ORIGINAL_PAGE_SNAPSHOT_KIND = "original_page_html_v1"
MANUAL_URL_CAPTURE_INPUT_KIND = "manual_url_capture_input_v1"
CAPTURE_PAGE_METADATA_SNAPSHOT_KIND = "capture_page_metadata_v1"
SINGLEFILE_HTML_SNAPSHOT_KIND = "singlefile_html_v1"
CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND = "crawl4ai_raw_markdown_v1"
CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND = "crawl4ai_fit_markdown_v1"
CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND = "crawl4ai_fit_image_reinserted_markdown_v1"
PAGE_IMAGE_ASSET_KIND = "page_image_v1"
UPLOADED_SOURCE_MATERIAL_ASSET_KIND = "uploaded_source_material_v1"
UPLOADED_MARKDOWN_SNAPSHOT_KIND = "uploaded_markdown_v1"
PRIMARY_ARCHIVE_ASSET_ROLE = "primary_archive"
INLINE_IMAGE_ASSET_ROLE = "inline_image"
SOURCE_MATERIAL_ASSET_ROLE = "source_material"
WINDOWS_RESERVED_FILE_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

CAPTURE_STATUS_PENDING = "pending"
CAPTURE_STATUS_SUCCEEDED = "succeeded"
CAPTURE_STATUS_PARTIAL_FAILED = "partial_failed"
CAPTURE_STATUS_FAILED = "failed"

CAPTURE_TRIGGER_MANUAL_URL = "manual_url_capture"
CAPTURE_TRIGGER_MANUAL_RETRY = "manual_retry"
CAPTURE_TRIGGER_FEED_SAVED = "feed_saved"
CAPTURE_TRIGGER_FEED_SAVED_RETRY = "feed_saved_retry"
CAPTURE_PIPELINE_VERSION = "archive_pipeline_v2"

CAPTURE_TOOL_STATUS_PENDING = "pending"
CAPTURE_TOOL_STATUS_SUCCEEDED = "succeeded"
CAPTURE_TOOL_STATUS_FAILED = "failed"

PROCESSING_STATUS_NOT_APPLICABLE = "not_applicable"
PROCESSING_STATUS_PENDING = "pending"
PROCESSING_STATUS_SUCCEEDED = "succeeded"
PROCESSING_STATUS_FAILED = "failed"

UPLOAD_MATERIAL_MARKDOWN = "markdown"
UPLOAD_MATERIAL_HTML = "html"
UPLOAD_MATERIAL_SINGLEFILE_HTML = "singlefile_html"
UPLOAD_MATERIAL_TYPES = frozenset(
    (
        UPLOAD_MATERIAL_MARKDOWN,
        UPLOAD_MATERIAL_HTML,
        UPLOAD_MATERIAL_SINGLEFILE_HTML,
    )
)
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
MARKDOWN_UPLOAD_EXTENSIONS = frozenset(("md", "markdown"))
HTML_UPLOAD_EXTENSIONS = frozenset(("html", "htm"))
MARKDOWN_UPLOAD_MIME_HINTS = frozenset(
    (
        "text/markdown",
        "text/x-markdown",
        "text/plain",
        "application/octet-stream",
    )
)
HTML_UPLOAD_MIME_HINTS = frozenset(
    (
        "text/html",
        "application/xhtml+xml",
        "application/octet-stream",
    )
)
UPLOAD_ORIGIN_TYPES = frozenset(
    (
        ORIGIN_TYPE_MARKDOWN_UPLOAD,
        ORIGIN_TYPE_HTML_UPLOAD,
        ORIGIN_TYPE_SINGLEFILE_HTML_UPLOAD,
    )
)

MIRROR_STATUS_PENDING = "pending"
MIRROR_STATUS_SUCCEEDED = "succeeded"
MIRROR_STATUS_PARTIAL_FAILED = "partial_failed"
MIRROR_STATUS_FAILED = "failed"

ASSET_REF_SCHEME = "ifa-asset://"
ASSET_REF_PATTERN = re.compile(r"ifa-asset://(?P<asset_key>[0-9a-fA-F-]{36})")
MARKDOWN_IMAGE_URL_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url><[^>]+>|[^)\s]+)(?P<title>\s+['\"][^)]*['\"])?\)"
)
HTML_IMAGE_PATTERN = re.compile(r"<img\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
HTML_ATTR_PATTERN = re.compile(
    r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
HTML_CONTEXT_BLOCK_PATTERN = re.compile(
    r"</?(?:article|section|main|p|div|figure|figcaption|li|h[1-6]|blockquote|br)\b[^>]*>",
    re.IGNORECASE,
)
HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
MARKDOWN_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
MARKDOWN_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
DECORATIVE_IMAGE_HINTS = (
    "logo",
    "avatar",
    "icon",
    "share",
    "tracking",
    "pixel",
    "spacer",
    "sprite",
    "favicon",
    "social",
    "button",
    "badge",
)

DEFAULT_CAPTURE_TARGETS = (
    SINGLEFILE_HTML_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
)
SNAPSHOT_STATUS_KINDS = (
    CANONICAL_SNAPSHOT_KIND,
    ORIGINAL_PAGE_SNAPSHOT_KIND,
    CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    UPLOADED_MARKDOWN_SNAPSHOT_KIND,
)
CURRENT_IMAGE_REF_SNAPSHOT_KINDS = (
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
)
ARCHIVE_SNAPSHOT_CONTENT_KINDS = frozenset(
    (
        CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
        CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
        CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
        UPLOADED_MARKDOWN_SNAPSHOT_KIND,
    )
)
ARCHIVE_ARTIFACT_STATUS_AVAILABLE = "available"
ARCHIVE_ARTIFACT_STATUS_UNAVAILABLE = "unavailable"
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
SUMMARY_BODY_PATTERN = re.compile(
    r'<div data-role="summary-body">(?P<body>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
CONTENT_BODY_PATTERN = re.compile(
    r'<div data-role="content-body">(?P<body>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
