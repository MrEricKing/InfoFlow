from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.core.safety import FEED_RESPONSE_MAX_BYTES, URLSafetyError, safe_http_get
from app.schemas.source import (
    SourceKind,
    SourcePreviewDiagnostics,
    SourcePreviewEntry,
    SourcePreviewRequest,
    SourcePreviewResponse,
    SourcePreviewSummary,
)
from app.services.source_resolver import (
    SourceValidationError,
    canonicalize_source_url,
    detect_source_url,
    resolve_source_fetch_url,
)
from app.services.source_icons import choose_preview_icon
from app.services.source_sync import SourceSyncError, create_http_client, parse_feed_document


class SourcePreviewError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_stage: str,
        detected_kind: SourceKind | None = None,
        canonical_url: str | None = None,
        resolved_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure_stage = failure_stage
        self.detected_kind = detected_kind
        self.canonical_url = canonical_url
        self.resolved_url = resolved_url

    def to_detail(self) -> dict[str, str | None]:
        return {
            "message": self.message,
            "failure_stage": self.failure_stage,
            "detected_kind": self.detected_kind,
            "canonical_url": self.canonical_url,
            "resolved_url": self.resolved_url,
        }


@dataclass(slots=True)
class PreviewTarget:
    detected_kind: SourceKind
    canonical_url: str
    resolved_url: str


def preview_source(session: Session, payload: SourcePreviewRequest) -> SourcePreviewResponse:
    target = resolve_preview_target(session, payload)

    try:
        with create_http_client() as client:
            response = safe_http_get(
                client,
                target.resolved_url,
                max_bytes=FEED_RESPONSE_MAX_BYTES,
                purpose="Source preview",
                allow_private_network=target.detected_kind == "rsshub",
            )
    except URLSafetyError as exc:
        raise SourceValidationError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise SourcePreviewError(
            f"Failed to fetch preview: {exc}",
            failure_stage="fetch_failed",
            detected_kind=target.detected_kind,
            canonical_url=target.canonical_url,
            resolved_url=target.resolved_url,
        ) from exc

    if response.status_code != httpx.codes.OK:
        raise SourcePreviewError(
            f"Preview fetch returned HTTP {response.status_code}",
            failure_stage="upstream_http_error",
            detected_kind=target.detected_kind,
            canonical_url=target.canonical_url,
            resolved_url=target.resolved_url,
        )

    try:
        document = parse_feed_document(response.text, base_url=str(response.url))
    except SourceSyncError as exc:
        raise SourcePreviewError(
            str(exc),
            failure_stage="feed_parse_failed",
            detected_kind=target.detected_kind,
            canonical_url=target.canonical_url,
            resolved_url=target.resolved_url,
        ) from exc

    entries = [
        SourcePreviewEntry(
            title=entry.title,
            published_at=entry.published_at,
            author_name=entry.author_name,
            summary_text=entry.summary_text,
            original_url=entry.original_url,
        )
        for entry in document.entries
        if entry.original_url and entry.title
    ][:10]

    preview_icon = choose_preview_icon(
        document.icon_url,
        document.icon_source,
        site_url=document.site_url,
        source_url=target.canonical_url if target.detected_kind == "rss" else None,
    )

    return SourcePreviewResponse(
        source=SourcePreviewSummary(
            detected_kind=target.detected_kind,
            canonical_url=target.canonical_url,
            resolved_url=target.resolved_url,
            title=document.title,
            site_url=document.site_url,
            description=document.description,
            language=document.language,
            icon_url=preview_icon.icon_url,
            icon_source=preview_icon.icon_source,
        ),
        entries=entries,
        diagnostics=SourcePreviewDiagnostics(
            requested_url=payload.url,
            final_url=str(response.url),
            http_status=response.status_code,
            content_type=response.headers.get("Content-Type"),
        ),
    )


def resolve_preview_target(session: Session, payload: SourcePreviewRequest) -> PreviewTarget:
    raw_url = payload.url.strip()
    if payload.kind is not None:
        canonical_url = canonicalize_source_url(payload.kind, raw_url, session)
        resolved = resolve_source_fetch_url(payload.kind, canonical_url, session)
    else:
        resolved = detect_source_url(raw_url, session)

    return PreviewTarget(
        detected_kind=resolved.detected_kind,
        canonical_url=resolved.canonical_url,
        resolved_url=resolved.resolved_url,
    )
