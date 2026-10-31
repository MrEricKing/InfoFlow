from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import json
import re
from pathlib import Path
import tempfile
from typing import Any, Callable
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.safety import CAPTURE_RESPONSE_MAX_BYTES, safe_http_get, validate_public_http_url
from app.db.models import (
    FeedItem,
    SavedItem,
    SavedItemAsset,
    SavedItemCaptureRun,
    SavedItemCaptureToolCall,
    SavedSnapshot,
)
from app.schemas.knowledge import (
    SavedItemDetailRead,
)
from app.services import source_sync
from app.services.capture_tools.crawl4ai_wrapper import (
    CRAWL4AI_TOOL_NAME,
    build_crawl4ai_tool_args,
    capture_markdown,
)
from app.services.capture_tools.models import CaptureToolDiagnostic, Crawl4AICaptureResult
from app.services.capture_tools.singlefile_wrapper import (
    SINGLEFILE_TOOL_NAME,
    archive_page_with_singlefile,
    build_unavailable_singlefile_args,
)
from app.services.saved_item_insights import (
    DEFAULT_SAVED_ITEM_AI_ROUTE,
    ORIGIN_TYPE_MANUAL_URL,
    normalize_saved_item_ai_route,
)
from app.services.source_registry import SourceValidationError, normalize_url

from app.services.knowledge_constants import (
    ASSET_REF_SCHEME,
    CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
    CAPTURE_PIPELINE_VERSION,
    CAPTURE_STATUS_FAILED,
    CAPTURE_STATUS_PARTIAL_FAILED,
    CAPTURE_STATUS_PENDING,
    CAPTURE_STATUS_SUCCEEDED,
    CAPTURE_TOOL_STATUS_FAILED,
    CAPTURE_TOOL_STATUS_PENDING,
    CAPTURE_TOOL_STATUS_SUCCEEDED,
    CAPTURE_TRIGGER_FEED_SAVED_RETRY,
    CAPTURE_TRIGGER_MANUAL_RETRY,
    CAPTURE_TRIGGER_MANUAL_URL,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    DECORATIVE_IMAGE_HINTS,
    DEFAULT_CAPTURE_TARGETS,
    HTML_ATTR_PATTERN,
    HTML_CONTENT_TYPES,
    HTML_CONTEXT_BLOCK_PATTERN,
    HTML_IMAGE_PATTERN,
    MANUAL_URL_CAPTURE_INPUT_KIND,
    MARKDOWN_HEADING_PATTERN,
    MARKDOWN_IMAGE_URL_PATTERN,
    MARKDOWN_LIST_ITEM_PATTERN,
    ORIGINAL_PAGE_SNAPSHOT_KIND,
    PRIMARY_ARCHIVE_ASSET_ROLE,
    SINGLEFILE_HTML_SNAPSHOT_KIND,
)
from app.services.knowledge_content_quality import (
    KnowledgeContentQualityError,
    ensure_html_has_readable_content,
)
from app.services.knowledge_errors import CaptureRetryNotAllowedError
from app.services.knowledge_read_model import get_saved_item_detail
from app.services.knowledge_snapshot_assets import (
    _create_markdown_snapshot_with_asset_refs,
    _create_page_image_asset,
    _delete_unreferenced_page_image_assets,
    _extract_html_page_title,
    _find_existing_page_image_asset,
    _find_latest_snapshot,
    _is_upload_saved_item,
    _load_latest_json_snapshot,
    _normalize_compare_text,
    _normalize_optional_text,
    _normalize_page_title,
)


@dataclass(frozen=True, slots=True)
class _ImageCandidate:
    source_url: str
    alt_text: str | None
    context_text: str | None
    context_before: str | None
    context_after: str | None
    source_kind: str
    ordinal: int


CaptureProgressCallback = Callable[[str, int, str], None]


def execute_saved_item_capture_run(
    session: Session,
    *,
    saved_item_id: int,
    capture_run_id: int,
    progress_callback: CaptureProgressCallback | None = None,
    export_local_mirror: bool = True,
) -> SavedItemCaptureRun | None:
    saved_item = session.get(SavedItem, saved_item_id)
    capture_run = session.get(SavedItemCaptureRun, capture_run_id)
    if saved_item is None or capture_run is None or capture_run.saved_item_id != saved_item_id:
        return None
    if capture_run.status != CAPTURE_STATUS_PENDING:
        return capture_run
    if _capture_run_has_tool_calls(session, capture_run.id):
        return _finalize_interrupted_capture_run(
            session,
            saved_item=saved_item,
            capture_run=capture_run,
            progress_callback=progress_callback,
            export_local_mirror=export_local_mirror,
        )

    _ensure_original_page_snapshot(session, saved_item)
    return _execute_capture_run(
        session,
        saved_item=saved_item,
        capture_run=capture_run,
        progress_callback=progress_callback,
        export_local_mirror=export_local_mirror,
    )


def prepare_manual_url_capture(
    session: Session,
    *,
    url: str,
    title: str | None,
    user_note: str | None,
    ai_route: str | None = None,
) -> tuple[SavedItemDetailRead, int | None, int | None]:
    normalized_url = _normalize_manual_capture_url(url)

    normalized_title = _normalize_optional_text(title)
    normalized_user_note = _normalize_optional_text(user_note)
    normalized_ai_route = normalize_saved_item_ai_route(ai_route, default=DEFAULT_SAVED_ITEM_AI_ROUTE)
    now = _now_utc()
    source_name = _build_manual_source_name(normalized_url)

    restored_saved_item = _find_removed_manual_saved_item_by_url(session, normalized_url)
    if restored_saved_item is not None:
        restored_saved_item.removed_at = None
        restored_saved_item.saved_at = now
        restored_saved_item.origin_type = restored_saved_item.origin_type or ORIGIN_TYPE_MANUAL_URL
        restored_saved_item.ai_route = normalized_ai_route
        if normalized_title is not None:
            restored_saved_item.title = normalized_title
        if normalized_user_note is not None:
            restored_saved_item.user_note = normalized_user_note
        session.add(restored_saved_item)
        session.flush()
        latest_capture_run = _find_latest_capture_run(session, restored_saved_item.id)
        if latest_capture_run is None:
            capture_run = _create_capture_run(
                session,
                saved_item_id=restored_saved_item.id,
                trigger_kind=CAPTURE_TRIGGER_MANUAL_RETRY,
                retry_of_run_id=None,
                requested_targets=DEFAULT_CAPTURE_TARGETS,
            )
            capture_run_id = capture_run.id
            mirror_export_capture_run_id = None
        elif latest_capture_run.status == CAPTURE_STATUS_PENDING:
            capture_run_id = latest_capture_run.id
            mirror_export_capture_run_id = None
        else:
            capture_run_id = None
            mirror_export_capture_run_id = latest_capture_run.id
        detail = get_saved_item_detail(session, restored_saved_item.id)
        if detail is None:
            raise RuntimeError("Restored manual saved item could not be reloaded")
        return detail, capture_run_id, mirror_export_capture_run_id

    saved_item = SavedItem(
        feed_item_id=None,
        origin_type=ORIGIN_TYPE_MANUAL_URL,
        ai_route=normalized_ai_route,
        title=normalized_title or normalized_url,
        canonical_url=normalized_url,
        source_name=source_name,
        source_app=_build_origin_url(normalized_url),
        user_note=normalized_user_note,
        saved_at=now,
    )
    session.add(saved_item)
    session.flush()

    session.add(
        SavedSnapshot(
            saved_item_id=saved_item.id,
            capture_run_id=None,
            snapshot_kind=MANUAL_URL_CAPTURE_INPUT_KIND,
            mime_type="application/json",
            body=_build_manual_capture_input_body(
                requested_url=url,
                normalized_url=normalized_url,
                title=normalized_title,
                user_note=normalized_user_note,
                ai_route=normalized_ai_route,
                captured_at=now,
            ),
        )
    )
    session.flush()

    capture_run = _create_capture_run(
        session,
        saved_item_id=saved_item.id,
        trigger_kind=CAPTURE_TRIGGER_MANUAL_URL,
        retry_of_run_id=None,
        requested_targets=DEFAULT_CAPTURE_TARGETS,
    )
    detail = get_saved_item_detail(session, saved_item.id)
    if detail is None:
        raise RuntimeError("Created manual saved item could not be reloaded")
    return detail, capture_run.id, None


def retry_saved_item_capture(
    session: Session,
    saved_item_id: int,
    *,
    progress_callback: CaptureProgressCallback | None = None,
    export_local_mirror: bool = True,
) -> SavedItemDetailRead | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None:
        return None

    if _is_upload_saved_item(saved_item):
        raise CaptureRetryNotAllowedError("Uploaded materials do not use the web capture pipeline")

    latest_run = _find_latest_capture_run(session, saved_item_id)
    if latest_run is not None and latest_run.status == CAPTURE_STATUS_PENDING:
        if not _capture_run_has_tool_calls(session, latest_run.id):
            latest_run.requested_targets_json = {"targets": list(DEFAULT_CAPTURE_TARGETS)}
            session.add(latest_run)
            session.flush()
            _execute_capture_run(
                session,
                saved_item=saved_item,
                capture_run=latest_run,
                progress_callback=progress_callback,
                export_local_mirror=export_local_mirror,
            )
            return get_saved_item_detail(session, saved_item_id)

        _finalize_interrupted_capture_run(
            session,
            saved_item=saved_item,
            capture_run=latest_run,
            progress_callback=progress_callback,
            export_local_mirror=export_local_mirror,
        )
        session.refresh(latest_run)
        if latest_run.status == CAPTURE_STATUS_SUCCEEDED:
            detail = get_saved_item_detail(session, saved_item_id)
            if detail is None:
                raise RuntimeError("Pending capture run could not be reloaded")
            return detail

    trigger_kind = CAPTURE_TRIGGER_MANUAL_RETRY
    if saved_item.feed_item_id is not None:
        trigger_kind = CAPTURE_TRIGGER_FEED_SAVED_RETRY

    capture_run = _create_capture_run(
        session,
        saved_item_id=saved_item_id,
        trigger_kind=trigger_kind,
        retry_of_run_id=latest_run.id if latest_run is not None else None,
        requested_targets=DEFAULT_CAPTURE_TARGETS,
    )
    _execute_capture_run(
        session,
        saved_item=saved_item,
        capture_run=capture_run,
        progress_callback=progress_callback,
        export_local_mirror=export_local_mirror,
    )
    return get_saved_item_detail(session, saved_item_id)


def finalize_interrupted_saved_item_capture_run(
    session: Session,
    saved_item_id: int,
    *,
    progress_callback: CaptureProgressCallback | None = None,
    export_local_mirror: bool = True,
) -> SavedItemCaptureRun | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None:
        return None

    latest_run = _find_latest_capture_run(session, saved_item_id)
    if latest_run is None or latest_run.status != CAPTURE_STATUS_PENDING:
        return latest_run
    if not _capture_run_has_tool_calls(session, latest_run.id):
        return latest_run

    return _finalize_interrupted_capture_run(
        session,
        saved_item=saved_item,
        capture_run=latest_run,
        progress_callback=progress_callback,
        export_local_mirror=export_local_mirror,
    )


def create_capture_tool_call(
    session: Session,
    *,
    capture_run_id: int,
    tool_name: str,
    tool_version: str | None = None,
    tool_args_json: dict[str, object] | None = None,
    status: str = CAPTURE_TOOL_STATUS_PENDING,
    exit_code: int | None = None,
    stdout_tail: str | None = None,
    stderr_tail: str | None = None,
    error_summary: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> SavedItemCaptureToolCall:
    tool_call = SavedItemCaptureToolCall(
        capture_run_id=capture_run_id,
        tool_name=tool_name,
        tool_version=tool_version,
        tool_args_json=tool_args_json,
        status=status,
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        error_summary=error_summary,
        started_at=started_at or _now_utc(),
        finished_at=finished_at,
    )
    session.add(tool_call)
    session.flush()
    session.refresh(tool_call)
    return tool_call


def _execute_capture_run(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run: SavedItemCaptureRun,
    progress_callback: CaptureProgressCallback | None = None,
    export_local_mirror: bool = True,
) -> SavedItemCaptureRun | None:
    capture_run.requested_targets_json = {"targets": list(DEFAULT_CAPTURE_TARGETS)}
    session.add(capture_run)
    session.flush()

    _emit_capture_progress(
        progress_callback,
        "capture_singlefile",
        0,
        "Capturing SingleFile archive.",
    )
    singlefile_page_title = _execute_singlefile_capture_step(session, saved_item=saved_item, capture_run=capture_run)
    _emit_capture_progress(
        progress_callback,
        "capture_crawl4ai",
        0,
        "Capturing Crawl4AI markdown.",
    )
    crawl4ai_page_title = _execute_crawl4ai_capture_step(session, saved_item=saved_item, capture_run=capture_run)
    _emit_capture_progress(
        progress_callback,
        "capture_finalize",
        1,
        "Finalizing capture artifacts.",
    )
    _record_capture_page_metadata_and_update_title(
        session,
        saved_item=saved_item,
        capture_run_id=capture_run.id,
        singlefile_page_title=singlefile_page_title,
        crawl4ai_page_title=crawl4ai_page_title,
    )

    finalized_run = finalize_capture_run(session, capture_run.id)
    if export_local_mirror and finalized_run is not None and _should_export_after_capture(
        session,
        capture_run=finalized_run,
    ):
        _emit_capture_progress(
            progress_callback,
            "mirror_export",
            1,
            "Exporting KnowledgeBase Local Mirror.",
        )
        _export_after_capture(session, saved_item_id=saved_item.id, capture_run_id=finalized_run.id)
    return finalized_run


def _emit_capture_progress(
    progress_callback: CaptureProgressCallback | None,
    stage: str,
    completed: int,
    message: str,
) -> None:
    if progress_callback is not None:
        progress_callback(stage, completed, message)


def _execute_singlefile_capture_step(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run: SavedItemCaptureRun,
) -> str | None:
    if not saved_item.canonical_url:
        return None

    with tempfile.TemporaryDirectory(prefix="singlefile-archive-") as temp_dir:
        output_path = Path(temp_dir) / "singlefile.html"
        tool_call = create_capture_tool_call(
            session,
            capture_run_id=capture_run.id,
            tool_name=SINGLEFILE_TOOL_NAME,
            tool_version=get_settings().singlefile_version,
            tool_args_json=build_unavailable_singlefile_args(saved_item.canonical_url, output_path),
            status=CAPTURE_TOOL_STATUS_PENDING,
            started_at=_now_utc(),
        )
        try:
            result = archive_page_with_singlefile(saved_item.canonical_url, output_path=output_path)
        except (ValueError, httpx.HTTPError) as exc:
            tool_call.status = CAPTURE_TOOL_STATUS_FAILED
            tool_call.exit_code = None
            tool_call.error_summary = str(exc)
            tool_call.finished_at = _now_utc()
            session.add(tool_call)
            session.flush()
            return None

    tool_call.tool_args_json = result.tool_args_json
    tool_call.exit_code = result.exit_code
    tool_call.stdout_tail = result.stdout_tail
    tool_call.stderr_tail = result.stderr_tail
    tool_call.error_summary = result.error_summary
    tool_call.status = CAPTURE_TOOL_STATUS_SUCCEEDED if result.succeeded else CAPTURE_TOOL_STATUS_FAILED
    tool_call.finished_at = _now_utc()
    session.add(tool_call)

    if result.succeeded and result.html_bytes is not None:
        try:
            ensure_html_has_readable_content(result.html_bytes, source_label="SingleFile archive")
        except KnowledgeContentQualityError as exc:
            tool_call.status = CAPTURE_TOOL_STATUS_FAILED
            tool_call.error_summary = str(exc)
            session.add(tool_call)
            session.flush()
            return None
        page_title = _extract_html_page_title(result.html_bytes)
        _create_singlefile_asset(
            session,
            saved_item=saved_item,
            capture_run_id=capture_run.id,
            html_bytes=result.html_bytes,
            page_title=page_title,
            tool_args_json=result.tool_args_json,
        )
        return page_title
    return None


def _execute_crawl4ai_capture_step(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run: SavedItemCaptureRun,
) -> str | None:
    if not saved_item.canonical_url:
        return None

    try:
        tool_args_json = build_crawl4ai_tool_args(saved_item.canonical_url)
    except ValueError as exc:
        create_capture_tool_call(
            session,
            capture_run_id=capture_run.id,
            tool_name=CRAWL4AI_TOOL_NAME,
            tool_version=get_settings().crawl4ai_version,
            tool_args_json=_build_failed_crawl4ai_tool_args(),
            status=CAPTURE_TOOL_STATUS_FAILED,
            exit_code=None,
            error_summary=str(exc),
            started_at=_now_utc(),
            finished_at=_now_utc(),
        )
        return None

    tool_call = create_capture_tool_call(
        session,
        capture_run_id=capture_run.id,
        tool_name=CRAWL4AI_TOOL_NAME,
        tool_version=get_settings().crawl4ai_version,
        tool_args_json=tool_args_json,
        status=CAPTURE_TOOL_STATUS_PENDING,
        started_at=_now_utc(),
    )

    try:
        result = capture_markdown(saved_item.canonical_url)
    except Exception as exc:
        tool_call.status = CAPTURE_TOOL_STATUS_FAILED
        tool_call.exit_code = None
        tool_call.error_summary = str(exc)
        tool_call.finished_at = _now_utc()
        session.add(tool_call)
        session.flush()
        return None

    _persist_crawl4ai_result(
        session,
        saved_item=saved_item,
        capture_run_id=capture_run.id,
        result=result,
    )

    fatal_diagnostics = [diagnostic for diagnostic in result.diagnostics if _is_fatal_crawl4ai_diagnostic(diagnostic)]
    nonfatal_diagnostics = [diagnostic for diagnostic in result.diagnostics if diagnostic not in fatal_diagnostics]
    tool_call.status = CAPTURE_TOOL_STATUS_FAILED if fatal_diagnostics else CAPTURE_TOOL_STATUS_SUCCEEDED
    tool_call.exit_code = 1 if fatal_diagnostics else 0
    tool_call.stdout_tail = "\n".join(diagnostic.message for diagnostic in nonfatal_diagnostics)[-4000:] or None
    tool_call.stderr_tail = "\n".join(diagnostic.message for diagnostic in fatal_diagnostics)[-4000:] or None
    tool_call.error_summary = fatal_diagnostics[0].message if fatal_diagnostics else None
    tool_call.finished_at = _now_utc()
    session.add(tool_call)
    session.flush()
    return result.page_title


def _build_failed_crawl4ai_tool_args() -> dict[str, object]:
    return {
        "url": "[REDACTED]",
        "profile": "crawl4ai_dynamic_markdown_v1",
        "integration_mode": "python_api",
    }


def _is_fatal_crawl4ai_diagnostic(diagnostic: CaptureToolDiagnostic) -> bool:
    if diagnostic.level != "error":
        return False
    return not diagnostic.code.startswith("crawl4ai_image_")


def _create_singlefile_asset(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run_id: int,
    html_bytes: bytes,
    page_title: str | None,
    tool_args_json: dict[str, object] | None = None,
) -> SavedItemAsset:
    metadata: dict[str, object] = {
        "tool_name": SINGLEFILE_TOOL_NAME,
        "tool_version": get_settings().singlefile_version,
        "page_title": page_title,
    }
    if isinstance(tool_args_json, dict):
        profile = tool_args_json.get("profile")
        if isinstance(profile, str) and profile:
            metadata["capture_profile"] = profile
        fallback_reason = tool_args_json.get("fallback_reason")
        if isinstance(fallback_reason, str) and fallback_reason:
            metadata["fallback_reason"] = fallback_reason

    asset = SavedItemAsset(
        saved_item_id=saved_item.id,
        capture_run_id=capture_run_id,
        asset_kind=SINGLEFILE_HTML_SNAPSHOT_KIND,
        asset_role=PRIMARY_ARCHIVE_ASSET_ROLE,
        logical_name="singlefile",
        source_url=saved_item.canonical_url,
        mime_type="text/html",
        file_ext="html",
        byte_size=len(html_bytes),
        sha256=sha256(html_bytes).hexdigest(),
        content_bytes=html_bytes,
        metadata_json=metadata,
    )
    session.add(asset)
    session.flush()
    session.refresh(asset)
    return asset


def _persist_crawl4ai_result(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run_id: int,
    result: Crawl4AICaptureResult,
) -> None:
    assets_by_source_url: dict[str, SavedItemAsset] = {}
    assets_by_sha256: dict[str, SavedItemAsset] = {}
    for payload in result.image_assets:
        if payload.source_url is None:
            continue
        asset_sha256 = sha256(payload.content_bytes).hexdigest()
        asset = assets_by_sha256.get(asset_sha256)
        if asset is None:
            asset = _find_existing_page_image_asset(
                session,
                saved_item_id=saved_item.id,
                asset_sha256=asset_sha256,
            )
        if asset is None:
            asset = _create_page_image_asset(
                session,
                saved_item=saved_item,
                capture_run_id=capture_run_id,
                payload=payload,
                asset_sha256=asset_sha256,
            )
        assets_by_sha256[asset_sha256] = asset
        assets_by_source_url.setdefault(payload.source_url, asset)

    _create_markdown_snapshot_with_asset_refs(
        session,
        saved_item_id=saved_item.id,
        capture_run_id=capture_run_id,
        snapshot_kind=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
        markdown=result.raw_markdown,
        assets_by_source_url=assets_by_source_url,
    )
    _create_markdown_snapshot_with_asset_refs(
        session,
        saved_item_id=saved_item.id,
        capture_run_id=capture_run_id,
        snapshot_kind=CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
        markdown=result.fit_markdown,
        assets_by_source_url=assets_by_source_url,
    )
    reinserted_markdown = _build_fit_image_reinserted_markdown(
        raw_markdown=result.raw_markdown,
        raw_html=result.raw_html,
        fit_markdown=result.fit_markdown,
        assets_by_source_url=assets_by_source_url,
        base_url=saved_item.canonical_url,
    )
    _create_markdown_snapshot_with_asset_refs(
        session,
        saved_item_id=saved_item.id,
        capture_run_id=capture_run_id,
        snapshot_kind=CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
        markdown=reinserted_markdown,
        assets_by_source_url=assets_by_source_url,
    )
    _delete_unreferenced_page_image_assets(session, saved_item_id=saved_item.id)


def _record_capture_page_metadata_and_update_title(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run_id: int,
    singlefile_page_title: str | None,
    crawl4ai_page_title: str | None,
) -> None:
    singlefile_title = _normalize_page_title(singlefile_page_title)
    crawl4ai_title = _normalize_page_title(crawl4ai_page_title)
    captured_page_title = singlefile_title or crawl4ai_title
    feed_title = _load_saved_feed_title(session, saved_item)
    manual_input = _load_latest_json_snapshot(session, saved_item.id, MANUAL_URL_CAPTURE_INPUT_KIND)
    previous_metadata = _load_latest_json_snapshot(session, saved_item.id, CAPTURE_PAGE_METADATA_SNAPSHOT_KIND)
    user_title_present = _resolve_user_title_present(
        saved_item=saved_item,
        manual_input=manual_input,
        previous_metadata=previous_metadata,
    )

    title_before = saved_item.title
    preferred_title, preferred_source = _select_preferred_saved_title(
        saved_item=saved_item,
        captured_page_title=captured_page_title,
        feed_title=feed_title,
        user_title_present=user_title_present,
    )
    title_updated = False
    if _should_auto_update_saved_item_title(
        saved_item=saved_item,
        preferred_title=preferred_title,
        user_title_present=user_title_present,
        feed_title=feed_title,
        manual_input=manual_input,
        previous_metadata=previous_metadata,
    ):
        saved_item.title = preferred_title
        session.add(saved_item)
        session.flush()
        session.refresh(saved_item)
        title_updated = _normalize_compare_text(title_before) != _normalize_compare_text(saved_item.title)

    metadata = {
        "capture_kind": CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
        "capture_run_id": capture_run_id,
        "captured_at": _now_utc().astimezone(timezone.utc).isoformat(),
        "canonical_url": saved_item.canonical_url,
        "singlefile_title": singlefile_title,
        "crawl4ai_title": crawl4ai_title,
        "captured_page_title": captured_page_title,
        "feed_title": feed_title,
        "user_title_present": user_title_present,
        "title_source": preferred_source if saved_item.title == preferred_title else _derive_current_title_source(
            saved_item=saved_item,
            captured_page_title=captured_page_title,
            feed_title=feed_title,
            user_title_present=user_title_present,
        ),
        "title_updated": title_updated,
        "saved_item_title_before": title_before,
        "saved_item_title_after": saved_item.title,
    }
    session.add(
        SavedSnapshot(
            saved_item_id=saved_item.id,
            capture_run_id=capture_run_id,
            snapshot_kind=CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
            mime_type="application/json",
            body=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    )
    session.flush()


def _build_fit_image_reinserted_markdown(
    *,
    raw_markdown: str,
    raw_html: str | None,
    fit_markdown: str,
    assets_by_source_url: dict[str, SavedItemAsset],
    base_url: str,
) -> str:
    if not assets_by_source_url:
        return fit_markdown

    existing_sources = _extract_markdown_image_source_urls(fit_markdown, base_url=base_url)
    raw_candidates = _extract_markdown_image_candidates(raw_markdown, base_url=base_url, start_ordinal=0)
    html_candidates = _extract_html_image_candidates(
        raw_html,
        base_url=base_url,
        start_ordinal=len(raw_candidates),
    )
    candidates = _dedupe_image_candidates([*raw_candidates, *html_candidates])

    insertable: list[_ImageCandidate] = []
    seen_sources = set(existing_sources)
    for candidate in candidates:
        asset = assets_by_source_url.get(candidate.source_url)
        if asset is None:
            continue
        if candidate.source_url in seen_sources:
            continue
        if _is_decorative_image_candidate(candidate, asset):
            continue
        seen_sources.add(candidate.source_url)
        insertable.append(candidate)

    if not insertable:
        return fit_markdown

    return _insert_image_blocks(fit_markdown, insertable)


def _extract_markdown_image_source_urls(markdown: str, *, base_url: str) -> set[str]:
    sources: set[str] = set()
    for candidate in _extract_markdown_image_candidates(markdown, base_url=base_url, start_ordinal=0):
        sources.add(candidate.source_url)
    return sources


def _extract_markdown_image_candidates(markdown: str, *, base_url: str, start_ordinal: int) -> list[_ImageCandidate]:
    candidates: list[_ImageCandidate] = []
    for offset, match in enumerate(MARKDOWN_IMAGE_URL_PATTERN.finditer(markdown)):
        raw_url = match.group("url").strip()
        source_url = _normalize_candidate_image_url(raw_url, base_url=base_url)
        if source_url is None:
            continue
        context_text, context_before, context_after = _extract_markdown_contexts(
            markdown,
            match.start(),
            match.end(),
        )
        candidates.append(
            _ImageCandidate(
                source_url=source_url,
                alt_text=match.group("alt").strip() or None,
                context_text=context_text,
                context_before=context_before,
                context_after=context_after,
                source_kind="markdown",
                ordinal=start_ordinal + offset,
            )
        )
    return candidates


def _extract_html_image_candidates(raw_html: str | None, *, base_url: str, start_ordinal: int) -> list[_ImageCandidate]:
    if raw_html is None:
        return []

    candidates: list[_ImageCandidate] = []
    for offset, match in enumerate(HTML_IMAGE_PATTERN.finditer(raw_html)):
        attrs = _parse_html_attrs(match.group("attrs"))
        source_url = _normalize_candidate_image_url(attrs.get("src") or attrs.get("data-src") or "", base_url=base_url)
        if source_url is None:
            continue
        context_text, context_before, context_after = _extract_html_contexts(raw_html, match.start(), match.end())
        candidates.append(
            _ImageCandidate(
                source_url=source_url,
                alt_text=_normalize_optional_text(attrs.get("alt") or attrs.get("title")),
                context_text=context_text,
                context_before=context_before,
                context_after=context_after,
                source_kind="html",
                ordinal=start_ordinal + offset,
            )
        )
    return candidates


def _parse_html_attrs(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in HTML_ATTR_PATTERN.finditer(value):
        attrs[match.group("name").lower()] = unescape(match.group("value").strip())
    return attrs


def _normalize_candidate_image_url(raw_url: str, *, base_url: str) -> str | None:
    normalized = raw_url.strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1].strip()
    if not normalized or normalized.startswith("data:") or normalized.startswith(ASSET_REF_SCHEME):
        return None
    absolute_url = urljoin(base_url, normalized)
    parsed = urlsplit(absolute_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return absolute_url


def _dedupe_image_candidates(candidates: list[_ImageCandidate]) -> list[_ImageCandidate]:
    candidates_by_source: dict[str, _ImageCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: item.ordinal):
        existing = candidates_by_source.get(candidate.source_url)
        if existing is None:
            candidates_by_source[candidate.source_url] = candidate
            continue
        candidates_by_source[candidate.source_url] = _merge_image_candidate_context(existing, candidate)
    return sorted(candidates_by_source.values(), key=lambda item: item.ordinal)


def _merge_image_candidate_context(
    existing: _ImageCandidate,
    candidate: _ImageCandidate,
) -> _ImageCandidate:
    if _image_candidate_context_quality(candidate) <= _image_candidate_context_quality(existing):
        if existing.alt_text is not None or candidate.alt_text is None:
            return existing
        return _ImageCandidate(
            source_url=existing.source_url,
            alt_text=candidate.alt_text,
            context_text=existing.context_text,
            context_before=existing.context_before,
            context_after=existing.context_after,
            source_kind=existing.source_kind,
            ordinal=existing.ordinal,
        )

    return _ImageCandidate(
        source_url=existing.source_url,
        alt_text=existing.alt_text or candidate.alt_text,
        context_text=candidate.context_text,
        context_before=candidate.context_before,
        context_after=candidate.context_after,
        source_kind=candidate.source_kind,
        ordinal=min(existing.ordinal, candidate.ordinal),
    )


def _image_candidate_context_quality(candidate: _ImageCandidate) -> int:
    before_terms = _significant_terms(candidate.context_before)
    after_terms = _significant_terms(candidate.context_after)
    context_terms = _significant_terms(candidate.context_text)
    quality = min(len(before_terms), 24) * 2 + min(len(after_terms), 24) * 2 + min(len(context_terms), 48)
    if candidate.source_kind == "html" and (candidate.context_before or candidate.context_after):
        quality += 80
    if candidate.context_before:
        quality += 1
    if candidate.context_after:
        quality += 1
    return quality


def _is_decorative_image_candidate(candidate: _ImageCandidate, asset: SavedItemAsset) -> bool:
    haystack = " ".join(
        part
        for part in (
            candidate.source_url,
            candidate.alt_text or "",
            asset.logical_name or "",
        )
        if part
    ).lower()
    path_name = Path(urlsplit(candidate.source_url).path).name.lower()
    if path_name in {"1x1.gif", "pixel.gif", "spacer.gif"}:
        return True
    return any(hint in haystack for hint in DECORATIVE_IMAGE_HINTS)


def _extract_markdown_contexts(markdown: str, start: int, end: int) -> tuple[str | None, str | None, str | None]:
    before = _extract_nearest_markdown_text(markdown[:start], reverse=True)
    after = _extract_nearest_markdown_text(markdown[end:], reverse=False)
    return _combine_context_text(before, after), before, after


def _extract_nearest_markdown_text(value: str, *, reverse: bool) -> str | None:
    cleaned = MARKDOWN_IMAGE_URL_PATTERN.sub(" ", value)
    blocks = [block.strip() for block in re.split(r"\n{2,}", cleaned) if block.strip()]
    if reverse:
        blocks = list(reversed(blocks))
    for block in blocks:
        normalized = _normalize_context_text(_strip_markdown_context_markup(block))
        if normalized:
            return normalized
    return None


def _extract_html_contexts(raw_html: str, start: int, end: int) -> tuple[str | None, str | None, str | None]:
    before = _extract_nearest_html_text(raw_html[max(0, start - 2500) : start], reverse=True)
    after = _extract_nearest_html_text(raw_html[end : min(len(raw_html), end + 2500)], reverse=False)
    return _combine_context_text(before, after), before, after


def _extract_nearest_html_text(value: str, *, reverse: bool) -> str | None:
    blocks = [block.strip() for block in HTML_CONTEXT_BLOCK_PATTERN.split(value) if block.strip()]
    if reverse:
        blocks = list(reversed(blocks))
    for block in blocks:
        normalized = _normalize_context_text(unescape(re.sub(r"<[^>]+>", " ", block)))
        if normalized:
            return normalized
    return None


def _combine_context_text(before: str | None, after: str | None) -> str | None:
    return _normalize_context_text(" ".join(part for part in (before, after) if part))


def _strip_markdown_context_markup(value: str) -> str:
    stripped = MARKDOWN_HEADING_PATTERN.sub("", value)
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
    stripped = re.sub(r"`{1,3}([^`]+)`{1,3}", r"\1", stripped)
    stripped = re.sub(r"[*_~>#|]+", " ", stripped)
    return stripped


def _normalize_context_text(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized or None


def _insert_image_blocks(fit_markdown: str, candidates: list[_ImageCandidate]) -> str:
    stripped = fit_markdown.strip()
    if not stripped:
        return "\n\n".join(_format_markdown_image(candidate) for candidate in candidates) + "\n"

    blocks = _split_fit_markdown_blocks(stripped)
    insertions: dict[int, list[str]] = {}
    for candidate in candidates:
        insert_slot = _find_fit_markdown_insert_slot(blocks, candidate)
        insertions.setdefault(insert_slot, []).append(_format_markdown_image(candidate))

    result_blocks: list[str] = []
    for index, block in enumerate(blocks):
        result_blocks.extend(insertions.get(index, []))
        result_blocks.append(block)
    result_blocks.extend(insertions.get(len(blocks), []))
    return "\n\n".join(result_blocks).rstrip() + "\n"


def _split_fit_markdown_blocks(fit_markdown: str) -> list[str]:
    normalized = re.sub(r"(?<!^)(?<!\n)\s+(#{1,6}\s+)", r"\n\n\1", fit_markdown.strip())
    blocks: list[str] = []
    for chunk in re.split(r"\n{2,}", normalized):
        chunk = chunk.strip()
        if not chunk:
            continue
        current_lines: list[str] = []

        def flush_current_lines() -> None:
            if current_lines:
                blocks.append("\n".join(current_lines).strip())
                current_lines.clear()

        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.fullmatch(r"#{1,6}", line):
                flush_current_lines()
                continue
            if MARKDOWN_HEADING_PATTERN.match(line):
                flush_current_lines()
                blocks.append(line)
            elif MARKDOWN_LIST_ITEM_PATTERN.match(line):
                current_lines.append(line)
            else:
                flush_current_lines()
                blocks.append(line)
        flush_current_lines()
    return blocks


def _find_fit_markdown_insert_slot(blocks: list[str], candidate: _ImageCandidate) -> int:
    if not blocks:
        return 0
    before_index, before_score = _find_best_fit_markdown_block(blocks, candidate.context_before)
    after_index, after_score = _find_best_fit_markdown_block(blocks, candidate.context_after)
    context_index, context_score = _find_best_fit_markdown_block(blocks, candidate.context_text)

    if after_score > 0 and after_score >= before_score:
        return after_index
    if before_score > 0:
        return min(before_index + 1, len(blocks))
    if context_score > 0:
        return min(context_index + 1, len(blocks))
    return len(blocks)


def _find_best_fit_markdown_block(blocks: list[str], context_text: str | None) -> tuple[int, int]:
    context_terms = _significant_terms(context_text)
    if not context_terms:
        return len(blocks) - 1, 0

    best_index = len(blocks) - 1
    best_score = 0
    for index, block in enumerate(blocks):
        block_terms = _significant_terms(_strip_markdown_context_markup(block))
        score = len(context_terms & block_terms)
        if score > best_score:
            best_score = score
            best_index = index
    if best_score < _minimum_context_match_score(context_terms):
        return len(blocks) - 1, 0
    return best_index, best_score


def _minimum_context_match_score(context_terms: set[str]) -> int:
    return 1 if len(context_terms) <= 2 else 2


def _significant_terms(value: str | None) -> set[str]:
    if value is None:
        return set()
    normalized = value.lower()
    terms = {word for word in re.findall(r"[a-zA-Z0-9]{3,}", normalized) if len(word) >= 3}
    for sequence in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]{2,}", normalized):
        for size in (2, 3):
            if len(sequence) < size:
                continue
            for index in range(0, len(sequence) - size + 1):
                terms.add(sequence[index : index + size])
    return terms


def _format_markdown_image(candidate: _ImageCandidate) -> str:
    alt_text = (candidate.alt_text or "").replace("[", "").replace("]", "").strip()
    return f"![{alt_text}]({_wrap_markdown_url(candidate.source_url)})"


def _wrap_markdown_url(url: str) -> str:
    if any(char.isspace() for char in url) or ")" in url:
        return f"<{url}>"
    return url


def _export_after_capture(session: Session, *, saved_item_id: int, capture_run_id: int) -> None:
    from app.services.knowledge_mirror import MIRROR_TRIGGER_AUTO_AFTER_CAPTURE, export_saved_item_to_local_mirror

    export_saved_item_to_local_mirror(
        session,
        saved_item_id,
        trigger_kind=MIRROR_TRIGGER_AUTO_AFTER_CAPTURE,
        source_capture_run_id=capture_run_id,
    )


def _should_export_after_capture(
    session: Session,
    *,
    capture_run: SavedItemCaptureRun,
) -> bool:
    if capture_run.status not in {CAPTURE_STATUS_SUCCEEDED, CAPTURE_STATUS_PARTIAL_FAILED}:
        return False
    requested_targets = _normalize_requested_targets(capture_run.requested_targets_json)
    completed_targets = _load_capture_run_completed_targets(session, capture_run.id, requested_targets)
    return bool(completed_targets)


def export_saved_item_after_capture(session: Session, *, saved_item_id: int, capture_run_id: int) -> None:
    _export_after_capture(session, saved_item_id=saved_item_id, capture_run_id=capture_run_id)


def _finalize_interrupted_capture_run(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run: SavedItemCaptureRun,
    progress_callback: CaptureProgressCallback | None = None,
    export_local_mirror: bool = True,
) -> SavedItemCaptureRun | None:
    _emit_capture_progress(
        progress_callback,
        "capture_finalize",
        1,
        "Finalizing existing capture artifacts.",
    )
    finalized_run = finalize_capture_run(session, capture_run.id)
    if finalized_run is not None and finalized_run.status == CAPTURE_STATUS_PENDING:
        finalized_run.status = CAPTURE_STATUS_FAILED
        finalized_run.finished_at = _now_utc()
        finalized_run.error_summary = "Capture was interrupted before the pending run could finish."
        session.add(finalized_run)
        session.flush()
    elif finalized_run is not None and finalized_run.status != CAPTURE_STATUS_SUCCEEDED and finalized_run.error_summary is None:
        finalized_run.error_summary = "Capture was interrupted before the pending run could finish; existing tool results were finalized."
        session.add(finalized_run)
        session.flush()
    if export_local_mirror and finalized_run is not None and _should_export_after_capture(
        session,
        capture_run=finalized_run,
    ):
        _emit_capture_progress(
            progress_callback,
            "mirror_export",
            1,
            "Exporting KnowledgeBase Local Mirror.",
        )
        _export_after_capture(session, saved_item_id=saved_item.id, capture_run_id=finalized_run.id)
    return finalized_run


def finalize_capture_run(
    session: Session,
    capture_run_id: int,
    *,
    error_summary: str | None = None,
) -> SavedItemCaptureRun | None:
    capture_run = session.get(SavedItemCaptureRun, capture_run_id)
    if capture_run is None:
        return None

    session.flush()

    requested_targets = _normalize_requested_targets(capture_run.requested_targets_json)
    completed_targets = _load_capture_run_completed_targets(session, capture_run.id, requested_targets)
    tool_calls = list(
        session.scalars(
            select(SavedItemCaptureToolCall)
            .where(SavedItemCaptureToolCall.capture_run_id == capture_run.id)
            .order_by(SavedItemCaptureToolCall.id.asc())
        )
    )

    capture_run.status = _derive_capture_run_status(
        requested_targets=requested_targets,
        completed_targets=completed_targets,
        tool_calls=tool_calls,
    )
    capture_run.error_summary = error_summary or _build_capture_run_error_summary(
        status=capture_run.status,
        requested_targets=requested_targets,
        completed_targets=completed_targets,
        tool_calls=tool_calls,
    )
    capture_run.finished_at = None if capture_run.status == CAPTURE_STATUS_PENDING else _resolve_finished_at(tool_calls)
    session.add(capture_run)
    session.flush()
    session.refresh(capture_run)
    return capture_run


def _load_saved_feed_title(session: Session, saved_item: SavedItem) -> str | None:
    if saved_item.feed_item_id is None:
        return None
    feed_item = session.get(FeedItem, saved_item.feed_item_id)
    if feed_item is None:
        return None
    return _normalize_optional_text(feed_item.title)


def _resolve_user_title_present(
    *,
    saved_item: SavedItem,
    manual_input: dict[str, Any] | None,
    previous_metadata: dict[str, Any] | None,
) -> bool:
    if isinstance(previous_metadata, dict) and isinstance(previous_metadata.get("user_title_present"), bool):
        return bool(previous_metadata["user_title_present"])
    if isinstance(manual_input, dict):
        return _normalize_optional_text(manual_input.get("title") if isinstance(manual_input.get("title"), str) else None) is not None
    if saved_item.feed_item_id is not None:
        return False
    if not saved_item.canonical_url:
        return True
    return _normalize_compare_text(saved_item.title) != _normalize_compare_text(saved_item.canonical_url)


def _select_preferred_saved_title(
    *,
    saved_item: SavedItem,
    captured_page_title: str | None,
    feed_title: str | None,
    user_title_present: bool,
) -> tuple[str, str]:
    if user_title_present:
        return saved_item.title, "user_title"
    if captured_page_title:
        return _append_manual_capture_source_to_title(saved_item, captured_page_title), "captured_page_title"
    if feed_title:
        return feed_title, "feed_title"
    return saved_item.canonical_url or saved_item.title, "url"


def _should_auto_update_saved_item_title(
    *,
    saved_item: SavedItem,
    preferred_title: str,
    user_title_present: bool,
    feed_title: str | None,
    manual_input: dict[str, Any] | None,
    previous_metadata: dict[str, Any] | None,
) -> bool:
    if user_title_present:
        return False
    if not _normalize_optional_text(preferred_title):
        return False
    if _normalize_compare_text(saved_item.title) == _normalize_compare_text(preferred_title):
        return False

    current = _normalize_compare_text(saved_item.title)
    if current in {
        _normalize_compare_text(saved_item.canonical_url),
        _normalize_compare_text(feed_title),
    }:
        return True
    if isinstance(previous_metadata, dict) and previous_metadata.get("user_title_present") is False:
        return True
    if isinstance(manual_input, dict) and _normalize_optional_text(
        manual_input.get("title") if isinstance(manual_input.get("title"), str) else None
    ) is None:
        return True
    return False


def _derive_current_title_source(
    *,
    saved_item: SavedItem,
    captured_page_title: str | None,
    feed_title: str | None,
    user_title_present: bool,
) -> str:
    if user_title_present:
        return "user_title"
    current = _normalize_compare_text(saved_item.title)
    if captured_page_title and current == _normalize_compare_text(captured_page_title):
        return "captured_page_title"
    if feed_title and current == _normalize_compare_text(feed_title):
        return "feed_title"
    if current == _normalize_compare_text(saved_item.canonical_url):
        return "url"
    return "existing_title"


def _append_manual_capture_source_to_title(saved_item: SavedItem, title: str) -> str:
    if saved_item.feed_item_id is not None:
        return title
    if not saved_item.canonical_url:
        return title

    source_name = _build_manual_source_name(saved_item.canonical_url)
    if not source_name:
        return title

    normalized_title = _normalize_compare_text(title)
    normalized_source = _normalize_compare_text(source_name)
    if normalized_source and normalized_source in normalized_title:
        return title

    return f"{title} - {source_name}"


def _create_capture_run(
    session: Session,
    *,
    saved_item_id: int,
    trigger_kind: str,
    retry_of_run_id: int | None,
    requested_targets: tuple[str, ...] = DEFAULT_CAPTURE_TARGETS,
) -> SavedItemCaptureRun:
    capture_run = SavedItemCaptureRun(
        saved_item_id=saved_item_id,
        status=CAPTURE_STATUS_PENDING,
        requested_targets_json={"targets": list(requested_targets)},
        pipeline_version=CAPTURE_PIPELINE_VERSION,
        trigger_kind=trigger_kind,
        retry_of_run_id=retry_of_run_id,
        error_summary=None,
        started_at=_now_utc(),
        finished_at=None,
    )
    session.add(capture_run)
    session.flush()
    session.refresh(capture_run)
    return capture_run


def _capture_original_page_snapshot(original_url: str) -> tuple[str | None, str | None]:
    try:
        with source_sync.create_http_client() as client:
            response = safe_http_get(
                client,
                original_url,
                max_bytes=CAPTURE_RESPONSE_MAX_BYTES,
                purpose="Original page snapshot",
            )
    except httpx.HTTPError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, str(exc)

    if response.status_code != httpx.codes.OK:
        return None, f"Unexpected HTTP status {response.status_code}"

    content_type = (response.headers.get("Content-Type") or "").lower()
    if content_type and not any(kind in content_type for kind in HTML_CONTENT_TYPES):
        return None, f"Unsupported content type {content_type}"

    body = response.text.strip()
    if not body:
        return None, "Original page response was empty"

    return body, None


def _normalize_manual_capture_url(url: str) -> str:
    normalized_url = normalize_url(url)
    if normalized_url is None:
        raise SourceValidationError("Capture URL must not be empty")
    try:
        return validate_public_http_url(normalized_url, purpose="Manual capture", resolve_dns=False)
    except ValueError as exc:
        raise SourceValidationError(str(exc)) from exc


def _ensure_original_page_snapshot(session: Session, saved_item: SavedItem) -> None:
    if not saved_item.canonical_url:
        return

    if _find_latest_snapshot(session, saved_item.id, ORIGINAL_PAGE_SNAPSHOT_KIND) is not None:
        return

    snapshot_body, _capture_error = _capture_original_page_snapshot(saved_item.canonical_url)
    if snapshot_body is None:
        return

    session.add(
        SavedSnapshot(
            saved_item_id=saved_item.id,
            capture_run_id=None,
            snapshot_kind=ORIGINAL_PAGE_SNAPSHOT_KIND,
            mime_type="text/html",
            body=snapshot_body,
        )
    )
    session.flush()


def _find_latest_capture_run(session: Session, saved_item_id: int) -> SavedItemCaptureRun | None:
    return session.scalar(
        select(SavedItemCaptureRun)
        .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        .order_by(SavedItemCaptureRun.id.desc())
        .limit(1)
    )


def _find_removed_manual_saved_item_by_url(session: Session, normalized_url: str) -> SavedItem | None:
    return session.scalar(
        select(SavedItem)
        .where(
            SavedItem.feed_item_id.is_(None),
            SavedItem.canonical_url == normalized_url,
            SavedItem.removed_at.is_not(None),
        )
        .order_by(SavedItem.saved_at.desc(), SavedItem.id.desc())
        .limit(1)
    )


def _capture_run_has_tool_calls(session: Session, capture_run_id: int) -> bool:
    return (
        session.scalar(
            select(func.count(SavedItemCaptureToolCall.id)).where(
                SavedItemCaptureToolCall.capture_run_id == capture_run_id
            )
        )
        or 0
    ) > 0


def _build_manual_source_name(normalized_url: str) -> str:
    hostname = urlsplit(normalized_url).hostname or "web"
    normalized_hostname = hostname.removeprefix("www.")
    return normalized_hostname or hostname


def _build_origin_url(normalized_url: str) -> str:
    parsed = urlsplit(normalized_url)
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=parsed.netloc,
            path="/",
            query="",
            fragment="",
        )
    )


def _build_manual_capture_input_body(
    *,
    requested_url: str,
    normalized_url: str,
    title: str | None,
    user_note: str | None,
    ai_route: str,
    captured_at: datetime,
) -> str:
    return json.dumps(
        {
            "capture_kind": MANUAL_URL_CAPTURE_INPUT_KIND,
            "requested_url": requested_url,
            "normalized_url": normalized_url,
            "title": title,
            "user_note": user_note,
            "ai_route": ai_route,
            "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _normalize_requested_targets(payload: object) -> tuple[str, ...]:
    if isinstance(payload, dict):
        raw_targets = payload.get("targets")
        if isinstance(raw_targets, list):
            return tuple(target for target in raw_targets if isinstance(target, str))
    return DEFAULT_CAPTURE_TARGETS


def _load_capture_run_completed_targets(
    session: Session,
    capture_run_id: int,
    requested_targets: tuple[str, ...],
) -> set[str]:
    if not requested_targets:
        return set()

    snapshot_targets = set(
        session.scalars(
            select(SavedSnapshot.snapshot_kind).where(
                SavedSnapshot.capture_run_id == capture_run_id,
                SavedSnapshot.snapshot_kind.in_(requested_targets),
            )
        )
    )
    asset_targets = set(
        session.scalars(
            select(SavedItemAsset.asset_kind).where(
                SavedItemAsset.capture_run_id == capture_run_id,
                SavedItemAsset.asset_kind.in_(requested_targets),
            )
        )
    )
    return snapshot_targets | asset_targets


def _derive_capture_run_status(
    *,
    requested_targets: tuple[str, ...],
    completed_targets: set[str],
    tool_calls: list[SavedItemCaptureToolCall],
) -> str:
    requested_set = set(requested_targets)
    failed_calls = [tool_call for tool_call in tool_calls if tool_call.status == CAPTURE_TOOL_STATUS_FAILED]
    succeeded_targets = requested_set & completed_targets

    if requested_set and succeeded_targets == requested_set and not failed_calls:
        return CAPTURE_STATUS_SUCCEEDED
    if succeeded_targets and (failed_calls or succeeded_targets != requested_set):
        return CAPTURE_STATUS_PARTIAL_FAILED
    if failed_calls or (tool_calls and not succeeded_targets):
        return CAPTURE_STATUS_FAILED
    return CAPTURE_STATUS_PENDING


def _build_capture_run_error_summary(
    *,
    status: str,
    requested_targets: tuple[str, ...],
    completed_targets: set[str],
    tool_calls: list[SavedItemCaptureToolCall],
) -> str | None:
    if status == CAPTURE_STATUS_SUCCEEDED:
        return None

    messages = [tool_call.error_summary for tool_call in tool_calls if tool_call.error_summary]
    unique_messages: list[str] = []
    for message in messages:
        if message not in unique_messages:
            unique_messages.append(message)

    if status == CAPTURE_STATUS_PARTIAL_FAILED:
        missing_targets = [target for target in requested_targets if target not in completed_targets]
        if missing_targets and unique_messages:
            return f"Missing targets: {', '.join(missing_targets)}; {unique_messages[0]}"
        if missing_targets:
            return f"Missing targets: {', '.join(missing_targets)}"
        if unique_messages:
            return unique_messages[0]
        return "Capture run partially failed"

    if status == CAPTURE_STATUS_FAILED:
        if unique_messages:
            return unique_messages[0]
        return "Capture run failed"

    return None


def _resolve_finished_at(tool_calls: list[SavedItemCaptureToolCall]) -> datetime:
    timestamps = [tool_call.finished_at for tool_call in tool_calls if tool_call.finished_at is not None]
    return max(timestamps) if timestamps else _now_utc()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
