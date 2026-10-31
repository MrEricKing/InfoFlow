from __future__ import annotations

from datetime import timezone
from hashlib import sha256
from html import escape
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import (
    SavedItem,
    SavedItemAsset,
    SavedSnapshot,
)
from app.schemas.knowledge import (
    SavedItemDetailRead,
)
from app.services.capture_tools.crawl4ai_wrapper import (
    build_crawl4ai_upload_tool_args,
    capture_uploaded_html_markdown,
)
from app.services.saved_item_insights import (
    DEFAULT_SAVED_ITEM_AI_ROUTE,
    ORIGIN_TYPE_HTML_UPLOAD,
    ORIGIN_TYPE_MARKDOWN_UPLOAD,
    ORIGIN_TYPE_SINGLEFILE_HTML_UPLOAD,
    normalize_saved_item_ai_route,
)

from app.services.knowledge_constants import (
    CANONICAL_SNAPSHOT_KIND,
    HTML_UPLOAD_EXTENSIONS,
    HTML_UPLOAD_MIME_HINTS,
    MARKDOWN_UPLOAD_EXTENSIONS,
    MARKDOWN_UPLOAD_MIME_HINTS,
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_SUCCEEDED,
    SOURCE_MATERIAL_ASSET_ROLE,
    UPLOADED_MARKDOWN_SNAPSHOT_KIND,
    UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
    UPLOAD_MATERIAL_HTML,
    UPLOAD_MATERIAL_MARKDOWN,
    UPLOAD_MATERIAL_TYPES,
    UPLOAD_MAX_BYTES,
)
from app.services.knowledge_content_quality import (
    ensure_html_has_no_known_failure,
    ensure_markdown_has_readable_content,
)
from app.services.knowledge_errors import UploadMaterialValidationError
from app.services.knowledge_read_model import get_saved_item_detail
from app.services.knowledge_snapshot_assets import (
    _build_asset_content_url,
    _build_markdown_summary,
    _create_markdown_snapshot_with_asset_refs,
    _extract_html_page_title,
    _markdown_to_plain_preview,
    _normalize_optional_text,
    _render_content_fragment,
)
from app.services.knowledge_capture import _now_utc


def prepare_uploaded_material_capture(
    session: Session,
    *,
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None,
    material_type: str,
    title: str | None,
    user_note: str | None,
    ai_route: str | None = None,
) -> SavedItemDetailRead:
    normalized_material_type = _normalize_upload_material_type(material_type)
    normalized_filename = _normalize_uploaded_filename(filename)
    normalized_mime_type = _normalize_upload_mime_type(content_type)
    _validate_upload_bytes(file_bytes)
    _validate_upload_kind(
        filename=normalized_filename,
        mime_type=normalized_mime_type,
        material_type=normalized_material_type,
    )
    _decode_uploaded_text(file_bytes)

    normalized_title = _normalize_optional_text(title)
    normalized_user_note = _normalize_optional_text(user_note)
    normalized_ai_route = normalize_saved_item_ai_route(ai_route, default=DEFAULT_SAVED_ITEM_AI_ROUTE)
    origin_type = _origin_type_for_upload_material(normalized_material_type)
    source_name = _source_name_for_upload_material(normalized_material_type)
    now = _now_utc()
    saved_item = SavedItem(
        feed_item_id=None,
        origin_type=origin_type,
        ai_route=normalized_ai_route,
        title=normalized_title or normalized_filename,
        canonical_url=None,
        source_name=source_name,
        source_app=None,
        original_filename=normalized_filename,
        mime_type=normalized_mime_type,
        source_asset_id=None,
        processing_status=PROCESSING_STATUS_PENDING,
        processing_error=None,
        user_note=normalized_user_note,
        saved_at=now,
    )
    session.add(saved_item)
    session.flush()

    source_asset = _create_uploaded_source_material_asset(
        session,
        saved_item=saved_item,
        file_bytes=file_bytes,
        filename=normalized_filename,
        mime_type=normalized_mime_type,
        material_type=normalized_material_type,
    )
    source_asset.metadata_json = {
        **(source_asset.metadata_json or {}),
        "requested_title": normalized_title,
    }
    saved_item.source_asset_id = source_asset.id
    session.add(source_asset)
    session.add(saved_item)
    session.flush()

    detail = get_saved_item_detail(session, saved_item.id)
    if detail is None:
        raise RuntimeError("Created uploaded saved item could not be reloaded")
    return detail


def prepare_saved_item_manual_import(
    session: Session,
    saved_item_id: int,
    *,
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None,
    material_type: str,
    title: str | None,
    user_note: str | None,
) -> tuple[SavedItemDetailRead, int] | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None

    normalized_material_type = _normalize_upload_material_type(material_type)
    normalized_filename = _normalize_uploaded_filename(filename)
    normalized_mime_type = _normalize_upload_mime_type(content_type)
    _validate_upload_bytes(file_bytes)
    _validate_upload_kind(
        filename=normalized_filename,
        mime_type=normalized_mime_type,
        material_type=normalized_material_type,
    )
    _decode_uploaded_text(file_bytes)

    normalized_title = _normalize_optional_text(title)
    normalized_user_note = _normalize_optional_text(user_note)
    saved_item.processing_status = PROCESSING_STATUS_PENDING
    saved_item.processing_error = None
    session.add(saved_item)
    session.flush()

    source_asset = _create_uploaded_source_material_asset(
        session,
        saved_item=saved_item,
        file_bytes=file_bytes,
        filename=normalized_filename,
        mime_type=normalized_mime_type,
        material_type=normalized_material_type,
    )
    source_asset.metadata_json = {
        **(source_asset.metadata_json or {}),
        "requested_title": normalized_title,
        "requested_user_note": normalized_user_note,
        "preserve_existing_title": normalized_title is None,
        "manual_import_for_saved_item_id": saved_item.id,
        "previous_source_asset_id": saved_item.source_asset_id,
        "pending_manual_import": True,
    }
    session.add(source_asset)
    session.add(saved_item)
    session.flush()

    detail = get_saved_item_detail(session, saved_item.id)
    if detail is None:
        raise RuntimeError("Imported saved item could not be reloaded")
    return detail, source_asset.id


def execute_uploaded_material_capture(
    session: Session,
    saved_item_id: int,
    *,
    export_local_mirror: bool = True,
    source_asset_id: int | None = None,
) -> SavedItemDetailRead | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None:
        return None
    current_source_asset_id = source_asset_id if source_asset_id is not None else saved_item.source_asset_id
    if current_source_asset_id is None:
        return None
    source_asset = session.get(SavedItemAsset, current_source_asset_id)
    if source_asset is None:
        return None
    if source_asset.saved_item_id != saved_item.id:
        raise RuntimeError("Uploaded source asset does not belong to the saved item")
    metadata = source_asset.metadata_json or {}
    material_type = str(metadata.get("material_type") or "")
    normalized_material_type = _normalize_upload_material_type(material_type)
    text = _decode_uploaded_text(source_asset.content_bytes)
    try:
        if normalized_material_type == UPLOAD_MATERIAL_MARKDOWN:
            uploaded_markdown = text
            ensure_markdown_has_readable_content(uploaded_markdown, source_label="Uploaded Markdown")
            page_title = _extract_markdown_heading_title(text)
        else:
            source_asset.metadata_json = {
                **(source_asset.metadata_json or {}),
                "crawl4ai_tool_args": build_crawl4ai_upload_tool_args(source_asset.logical_name or "uploaded.html"),
            }
            session.add(source_asset)
            session.flush()
            ensure_html_has_no_known_failure(text, source_label="Uploaded HTML")
            result = capture_uploaded_html_markdown(text, source_name=source_asset.logical_name)
            uploaded_markdown = (result.fit_markdown or result.raw_markdown or "").strip()
            page_title = result.page_title or _extract_html_page_title(text)
            if not uploaded_markdown:
                raise RuntimeError("Crawl4AI returned empty markdown")
            ensure_markdown_has_readable_content(uploaded_markdown, source_label="Uploaded HTML markdown")

        requested_title = metadata.get("requested_title") if isinstance(metadata.get("requested_title"), str) else None
        if not metadata.get("preserve_existing_title") or requested_title:
            saved_item.title = _select_uploaded_material_title(
                requested_title=requested_title,
                material_type=normalized_material_type,
                filename=source_asset.logical_name or saved_item.original_filename or "uploaded",
                markdown=uploaded_markdown if normalized_material_type == UPLOAD_MATERIAL_MARKDOWN else None,
                html=text if normalized_material_type != UPLOAD_MATERIAL_MARKDOWN else None,
                crawl4ai_title=page_title,
            )
        requested_user_note = metadata.get("requested_user_note")
        if isinstance(requested_user_note, str):
            saved_item.user_note = requested_user_note
        saved_item.original_filename = source_asset.logical_name or saved_item.original_filename
        saved_item.mime_type = source_asset.mime_type or saved_item.mime_type
        saved_item.source_asset_id = source_asset.id
        if metadata.get("pending_manual_import") or metadata.get("manual_import_for_saved_item_id") == saved_item.id:
            source_asset.metadata_json = {
                **(source_asset.metadata_json or {}),
                "pending_manual_import": False,
                "manual_import_committed_at": _now_utc().isoformat(),
            }
            session.add(source_asset)
        _create_uploaded_markdown_snapshot(
            session,
            saved_item=saved_item,
            markdown=uploaded_markdown,
            source_asset=source_asset,
            material_type=normalized_material_type,
        )
        _create_uploaded_canonical_snapshot(
            session,
            saved_item=saved_item,
            markdown=uploaded_markdown,
            material_type=normalized_material_type,
            source_asset=source_asset,
        )
        saved_item.processing_status = PROCESSING_STATUS_SUCCEEDED
        saved_item.processing_error = None
    except Exception as exc:
        saved_item.processing_status = PROCESSING_STATUS_FAILED
        saved_item.processing_error = _summarize_processing_error(exc)

    session.add(saved_item)
    session.flush()
    session.refresh(saved_item)
    if export_local_mirror and saved_item.processing_status == PROCESSING_STATUS_SUCCEEDED:
        _export_uploaded_material(session, saved_item_id=saved_item.id)
    return get_saved_item_detail(session, saved_item.id)


def _normalize_upload_material_type(value: str) -> str:
    material_type = (value or "").strip().lower()
    if material_type not in UPLOAD_MATERIAL_TYPES:
        raise UploadMaterialValidationError("Unsupported upload material type")
    return material_type


def _normalize_uploaded_filename(value: str | None) -> str:
    filename = Path(value or "uploaded").name.strip()
    return filename or "uploaded"


def _normalize_upload_mime_type(value: str | None) -> str:
    mime_type = (value or "application/octet-stream").split(";")[0].strip().lower()
    return mime_type or "application/octet-stream"


def _validate_upload_bytes(file_bytes: bytes) -> None:
    if not file_bytes:
        raise UploadMaterialValidationError("Uploaded file must not be empty")
    if len(file_bytes) > UPLOAD_MAX_BYTES:
        raise UploadMaterialValidationError("Uploaded file exceeds the 25 MB limit")


def _validate_upload_kind(*, filename: str, mime_type: str, material_type: str) -> None:
    extension = Path(filename).suffix.lower().removeprefix(".")
    if material_type == UPLOAD_MATERIAL_MARKDOWN:
        if extension in MARKDOWN_UPLOAD_EXTENSIONS or mime_type in MARKDOWN_UPLOAD_MIME_HINTS:
            return
        raise UploadMaterialValidationError("Markdown files require .md/.markdown or a Markdown text MIME type")

    if extension in HTML_UPLOAD_EXTENSIONS or mime_type in HTML_UPLOAD_MIME_HINTS:
        return
    raise UploadMaterialValidationError("HTML files require .html/.htm or an HTML MIME type")


def _decode_uploaded_text(file_bytes: bytes) -> str:
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadMaterialValidationError("Uploaded text must be UTF-8 encoded") from exc
    if not text.strip():
        raise UploadMaterialValidationError("Uploaded file must contain text")
    return text


def _origin_type_for_upload_material(material_type: str) -> str:
    if material_type == UPLOAD_MATERIAL_MARKDOWN:
        return ORIGIN_TYPE_MARKDOWN_UPLOAD
    if material_type == UPLOAD_MATERIAL_HTML:
        return ORIGIN_TYPE_HTML_UPLOAD
    return ORIGIN_TYPE_SINGLEFILE_HTML_UPLOAD


def _source_name_for_upload_material(material_type: str) -> str:
    if material_type == UPLOAD_MATERIAL_MARKDOWN:
        return "External Source · Markdown"
    if material_type == UPLOAD_MATERIAL_HTML:
        return "External Source · HTML"
    return "External Source · SingleFile HTML"


def _create_uploaded_source_material_asset(
    session: Session,
    *,
    saved_item: SavedItem,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    material_type: str,
) -> SavedItemAsset:
    extension = Path(filename).suffix.lower().removeprefix(".")
    if not extension:
        extension = "md" if material_type == UPLOAD_MATERIAL_MARKDOWN else "html"
    asset = SavedItemAsset(
        saved_item_id=saved_item.id,
        capture_run_id=None,
        asset_kind=UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
        asset_role=SOURCE_MATERIAL_ASSET_ROLE,
        logical_name=filename,
        source_url=None,
        mime_type=mime_type,
        file_ext=extension,
        byte_size=len(file_bytes),
        sha256=sha256(file_bytes).hexdigest(),
        content_bytes=file_bytes,
        metadata_json={
            "material_type": material_type,
            "original_filename": filename,
        },
    )
    session.add(asset)
    session.flush()
    session.refresh(asset)
    return asset


def _create_uploaded_markdown_snapshot(
    session: Session,
    *,
    saved_item: SavedItem,
    markdown: str,
    source_asset: SavedItemAsset,
    material_type: str,
) -> SavedSnapshot:
    snapshot = _create_markdown_snapshot_with_asset_refs(
        session,
        saved_item_id=saved_item.id,
        capture_run_id=None,
        snapshot_kind=UPLOADED_MARKDOWN_SNAPSHOT_KIND,
        markdown=markdown,
        assets_by_source_url={},
    )
    source_asset.metadata_json = {
        **(source_asset.metadata_json or {}),
        "derived_markdown_snapshot_kind": UPLOADED_MARKDOWN_SNAPSHOT_KIND,
        "derived_markdown_snapshot_id": snapshot.id,
        "derived_material_type": material_type,
    }
    session.add(source_asset)
    session.flush()
    return snapshot


def _create_uploaded_canonical_snapshot(
    session: Session,
    *,
    saved_item: SavedItem,
    markdown: str,
    material_type: str,
    source_asset: SavedItemAsset,
) -> SavedSnapshot:
    snapshot = SavedSnapshot(
        saved_item_id=saved_item.id,
        capture_run_id=None,
        snapshot_kind=CANONICAL_SNAPSHOT_KIND,
        mime_type="text/html",
        body=_build_uploaded_canonical_snapshot_document(
            saved_item=saved_item,
            markdown=markdown,
            material_type=material_type,
            source_asset=source_asset,
        ),
    )
    session.add(snapshot)
    session.flush()
    session.refresh(snapshot)
    return snapshot


def _build_uploaded_canonical_snapshot_document(
    *,
    saved_item: SavedItem,
    markdown: str,
    material_type: str,
    source_asset: SavedItemAsset,
) -> str:
    content_html = _render_content_fragment(_markdown_to_plain_preview(markdown)) or ""
    summary_html = _render_content_fragment(_build_markdown_summary(markdown)) or ""
    saved_at_text = saved_item.saved_at.astimezone(timezone.utc).isoformat()
    source_link = _build_asset_content_url(source_asset.asset_key)
    return "".join(
        [
            "<!DOCTYPE html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\" />",
            f"<title>{escape(saved_item.title)}</title>",
            "</head>",
            "<body>",
            f"<article data-snapshot-kind=\"{CANONICAL_SNAPSHOT_KIND}\" data-origin-type=\"{escape(saved_item.origin_type)}\">",
            "<header>",
            f"<h1>{escape(saved_item.title)}</h1>",
            f"<p>Saved from uploaded {escape(material_type)} at {escape(saved_at_text)}</p>",
            f"<p>Original file: <a href=\"{escape(source_link)}\">{escape(source_asset.logical_name or 'uploaded')}</a></p>",
            "</header>",
            "<section data-role=\"summary\">",
            "<h2>Summary</h2>",
            f"<div data-role=\"summary-body\">{summary_html}</div>",
            "</section>",
            "<section data-role=\"content\">",
            "<h2>Content</h2>",
            f"<div data-role=\"content-body\">{content_html}</div>",
            "</section>",
            "</article>",
            "</body>",
            "</html>",
        ]
    )


def _select_uploaded_material_title(
    *,
    requested_title: str | None,
    material_type: str,
    filename: str,
    markdown: str | None,
    html: str | None,
    crawl4ai_title: str | None,
) -> str:
    if requested_title:
        return requested_title
    if markdown:
        heading = _extract_markdown_heading_title(markdown)
        if heading:
            return heading
    if crawl4ai_title:
        return crawl4ai_title
    if html:
        html_title = _extract_html_page_title(html)
        if html_title:
            return html_title
    title_from_filename = Path(filename).stem.strip()
    if title_from_filename:
        return title_from_filename
    return _source_name_for_upload_material(material_type)


def _extract_markdown_heading_title(markdown: str | None) -> str | None:
    if not markdown:
        return None
    for line in markdown.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$", line)
        if match is not None:
            return _normalize_optional_text(match.group("title"))
    return None


def _summarize_processing_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:1000]


def _export_uploaded_material(session: Session, *, saved_item_id: int) -> None:
    from app.services.knowledge_mirror import MIRROR_TRIGGER_AUTO_AFTER_CAPTURE, export_saved_item_to_local_mirror

    export_saved_item_to_local_mirror(
        session,
        saved_item_id,
        trigger_kind=MIRROR_TRIGGER_AUTO_AFTER_CAPTURE,
    )
