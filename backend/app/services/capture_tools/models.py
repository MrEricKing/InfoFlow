from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CaptureAssetPayload:
    logical_name: str | None
    source_url: str | None
    mime_type: str
    file_ext: str
    content_bytes: bytes
    alt_text: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CaptureToolDiagnostic:
    code: str
    message: str
    level: str = "info"


@dataclass(frozen=True, slots=True)
class Crawl4AICaptureResult:
    raw_markdown: str
    fit_markdown: str
    image_assets: list[CaptureAssetPayload] = field(default_factory=list)
    diagnostics: list[CaptureToolDiagnostic] = field(default_factory=list)
    page_title: str | None = None
    raw_html: str | None = None


@dataclass(frozen=True, slots=True)
class SingleFileArchiveResult:
    succeeded: bool
    html_bytes: bytes | None
    exit_code: int | None
    stdout_tail: str | None
    stderr_tail: str | None
    error_summary: str | None
    tool_args_json: dict[str, object]
    diagnostics: list[CaptureToolDiagnostic] = field(default_factory=list)


class CaptureToolUnavailableError(RuntimeError):
    pass
