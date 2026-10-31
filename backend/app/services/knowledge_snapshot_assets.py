from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import escape, unescape
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    SavedItem,
    SavedItemAsset,
    SavedSnapshot,
    SavedSnapshotAssetRef,
)
from app.schemas.knowledge import (
    SavedItemArchiveArtifactRead,
    SavedItemSnapshotStatusRead,
)
from app.services.capture_tools.models import CaptureAssetPayload
from app.services.insight_engine_artifacts import render_summary_html

from app.services.knowledge_constants import (
    ARCHIVE_ARTIFACT_STATUS_AVAILABLE,
    ARCHIVE_ARTIFACT_STATUS_UNAVAILABLE,
    ARCHIVE_SNAPSHOT_CONTENT_KINDS,
    ASSET_REF_PATTERN,
    ASSET_REF_SCHEME,
    CANONICAL_SNAPSHOT_KIND,
    CONTENT_BODY_PATTERN,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    CURRENT_IMAGE_REF_SNAPSHOT_KINDS,
    HTML_TITLE_PATTERN,
    INLINE_IMAGE_ASSET_ROLE,
    MARKDOWN_HEADING_PATTERN,
    MARKDOWN_IMAGE_URL_PATTERN,
    ORIGINAL_PAGE_SNAPSHOT_KIND,
    PAGE_IMAGE_ASSET_KIND,
    SINGLEFILE_HTML_SNAPSHOT_KIND,
    SNAPSHOT_STATUS_KINDS,
    SUMMARY_BODY_PATTERN,
    UPLOADED_MARKDOWN_SNAPSHOT_KIND,
    UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
    UPLOAD_ORIGIN_TYPES,
    WINDOWS_RESERVED_FILE_STEMS,
)


def _load_latest_json_snapshot(session: Session, saved_item_id: int, snapshot_kind: str) -> dict[str, Any] | None:
    snapshot = _find_latest_snapshot(session, saved_item_id, snapshot_kind)
    if snapshot is None:
        return None
    try:
        payload = json.loads(snapshot.body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _json_string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _json_datetime_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_page_title(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    normalized = re.sub(r"\s+", " ", unescape(normalized)).strip()
    return normalized or None


def _extract_html_page_title(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    match = HTML_TITLE_PATTERN.search(text)
    if match is None:
        return None
    return _normalize_page_title(re.sub(r"<[^>]+>", " ", match.group("title")))


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_compare_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _build_snapshot_status_read(session: Session, saved_item_id: int) -> SavedItemSnapshotStatusRead:
    snapshots_by_kind = _load_snapshot_map(session, [saved_item_id]).get(saved_item_id, {})
    asset_status = _load_asset_status_map(session, [saved_item_id]).get(saved_item_id, _empty_asset_status())
    return SavedItemSnapshotStatusRead(
        has_canonical_snapshot=CANONICAL_SNAPSHOT_KIND in snapshots_by_kind,
        has_original_page_snapshot=ORIGINAL_PAGE_SNAPSHOT_KIND in snapshots_by_kind,
        has_singlefile_html=bool(asset_status["has_singlefile_html"]),
        has_crawl4ai_raw_markdown=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND in snapshots_by_kind,
        has_crawl4ai_fit_markdown=CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND in snapshots_by_kind,
        has_crawl4ai_fit_image_reinserted_markdown=(
            CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND in snapshots_by_kind
        ),
        image_asset_count=int(asset_status["image_asset_count"]),
    )


def _load_snapshot_map(session: Session, saved_item_ids: list[int]) -> dict[int, dict[str, SavedSnapshot]]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    latest_ids = (
        select(
            SavedSnapshot.saved_item_id.label("saved_item_id"),
            SavedSnapshot.snapshot_kind.label("snapshot_kind"),
            func.max(SavedSnapshot.id).label("latest_id"),
        )
        .where(
            SavedSnapshot.saved_item_id.in_(unique_ids),
            SavedSnapshot.snapshot_kind.in_(SNAPSHOT_STATUS_KINDS),
        )
        .group_by(SavedSnapshot.saved_item_id, SavedSnapshot.snapshot_kind)
        .subquery()
    )

    snapshots = session.scalars(select(SavedSnapshot).join(latest_ids, SavedSnapshot.id == latest_ids.c.latest_id)).all()
    snapshot_map: dict[int, dict[str, SavedSnapshot]] = {}
    for snapshot in snapshots:
        snapshot_map.setdefault(snapshot.saved_item_id, {})[snapshot.snapshot_kind] = snapshot
    return snapshot_map


def _empty_asset_status() -> dict[str, int | bool]:
    return {
        "has_singlefile_html": False,
        "image_asset_count": 0,
    }


def _load_asset_status_map(session: Session, saved_item_ids: list[int]) -> dict[int, dict[str, int | bool]]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    rows = session.execute(
        select(
            SavedItemAsset.saved_item_id,
            SavedItemAsset.asset_kind,
            func.count(SavedItemAsset.id).label("asset_count"),
        )
        .where(
            SavedItemAsset.saved_item_id.in_(unique_ids),
            SavedItemAsset.asset_kind == SINGLEFILE_HTML_SNAPSHOT_KIND,
        )
        .group_by(SavedItemAsset.saved_item_id, SavedItemAsset.asset_kind)
    ).all()

    result = {saved_item_id: _empty_asset_status() for saved_item_id in unique_ids}
    for saved_item_id, asset_kind, asset_count in rows:
        status = result.setdefault(saved_item_id, _empty_asset_status())
        if asset_kind == SINGLEFILE_HTML_SNAPSHOT_KIND:
            status["has_singlefile_html"] = asset_count > 0

    current_image_asset_ids = _load_current_image_asset_ids_map(session, unique_ids)
    for saved_item_id, asset_ids in current_image_asset_ids.items():
        result.setdefault(saved_item_id, _empty_asset_status())["image_asset_count"] = len(asset_ids)
    return result


def _load_source_asset_map(session: Session, saved_item_ids: list[int]) -> dict[int, SavedItemAsset]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    rows = session.execute(
        select(SavedItem.id, SavedItemAsset)
        .join(SavedItemAsset, SavedItemAsset.id == SavedItem.source_asset_id)
        .where(SavedItem.id.in_(unique_ids))
    ).all()
    return {saved_item_id: asset for saved_item_id, asset in rows}


def _load_current_image_asset_ids_map(session: Session, saved_item_ids: list[int]) -> dict[int, list[int]]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    latest_snapshot_ids = (
        select(
            SavedSnapshot.saved_item_id.label("saved_item_id"),
            SavedSnapshot.snapshot_kind.label("snapshot_kind"),
            func.max(SavedSnapshot.id).label("latest_id"),
        )
        .where(
            SavedSnapshot.saved_item_id.in_(unique_ids),
            SavedSnapshot.snapshot_kind.in_(CURRENT_IMAGE_REF_SNAPSHOT_KINDS),
        )
        .group_by(SavedSnapshot.saved_item_id, SavedSnapshot.snapshot_kind)
        .subquery()
    )
    rows = session.execute(
        select(SavedSnapshot.saved_item_id, SavedSnapshotAssetRef.asset_id)
        .join(latest_snapshot_ids, SavedSnapshot.id == latest_snapshot_ids.c.latest_id)
        .join(SavedSnapshotAssetRef, SavedSnapshotAssetRef.saved_snapshot_id == SavedSnapshot.id)
        .join(SavedItemAsset, SavedItemAsset.id == SavedSnapshotAssetRef.asset_id)
        .where(SavedItemAsset.asset_kind == PAGE_IMAGE_ASSET_KIND)
        .order_by(SavedSnapshot.saved_item_id.asc(), SavedSnapshot.id.asc(), SavedSnapshotAssetRef.ordinal.asc())
    ).all()

    result: dict[int, list[int]] = {}
    seen_by_saved_item: dict[int, set[int]] = {}
    for saved_item_id, asset_id in rows:
        seen = seen_by_saved_item.setdefault(saved_item_id, set())
        if asset_id in seen:
            continue
        seen.add(asset_id)
        result.setdefault(saved_item_id, []).append(asset_id)
    return result


def _find_latest_snapshot(session: Session, saved_item_id: int, snapshot_kind: str) -> SavedSnapshot | None:
    statement = (
        select(SavedSnapshot)
        .where(
            SavedSnapshot.saved_item_id == saved_item_id,
            SavedSnapshot.snapshot_kind == snapshot_kind,
        )
        .order_by(SavedSnapshot.id.desc())
        .limit(1)
    )
    return session.scalar(statement)


def get_saved_item_asset(session: Session, asset_key: str) -> SavedItemAsset | None:
    normalized_asset_key = asset_key.strip()
    if not normalized_asset_key:
        return None
    return session.scalar(select(SavedItemAsset).where(SavedItemAsset.asset_key == normalized_asset_key))


def get_saved_item_archive_snapshot(
    session: Session,
    *,
    saved_item_id: int,
    snapshot_kind: str,
) -> SavedSnapshot | None:
    if snapshot_kind not in ARCHIVE_SNAPSHOT_CONTENT_KINDS:
        return None
    return _find_latest_snapshot(session, saved_item_id, snapshot_kind)


def _create_markdown_snapshot_with_asset_refs(
    session: Session,
    *,
    saved_item_id: int,
    capture_run_id: int | None,
    snapshot_kind: str,
    markdown: str,
    assets_by_source_url: dict[str, SavedItemAsset],
) -> SavedSnapshot:
    rewritten_markdown, refs = _rewrite_markdown_image_refs(markdown, assets_by_source_url)
    snapshot = SavedSnapshot(
        saved_item_id=saved_item_id,
        capture_run_id=capture_run_id,
        snapshot_kind=snapshot_kind,
        mime_type="text/markdown",
        body=rewritten_markdown,
    )
    session.add(snapshot)
    session.flush()

    for ordinal, ref in enumerate(refs):
        session.add(
            SavedSnapshotAssetRef(
                saved_snapshot_id=snapshot.id,
                asset_id=ref["asset"].id,
                ref_kind="markdown_image",
                ref_token=ref["ref_token"],
                alt_text=ref["alt_text"],
                ordinal=ordinal,
                source_url=ref["source_url"],
            )
        )
    session.flush()
    session.refresh(snapshot)
    return snapshot


def _rewrite_markdown_image_refs(
    markdown: str,
    assets_by_source_url: dict[str, SavedItemAsset],
) -> tuple[str, list[dict[str, Any]]]:
    refs: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        source_url = raw_url[1:-1].strip() if raw_url.startswith("<") and raw_url.endswith(">") else raw_url
        asset = assets_by_source_url.get(source_url)
        if asset is None:
            return match.group(0)

        ref_token = f"{ASSET_REF_SCHEME}{asset.asset_key}"
        if ref_token not in seen_tokens:
            seen_tokens.add(ref_token)
            refs.append(
                {
                    "asset": asset,
                    "ref_token": ref_token,
                    "alt_text": match.group("alt").strip() or None,
                    "source_url": source_url,
                }
            )

        title = match.group("title") or ""
        return f"![{match.group('alt')}]({ref_token}{title})"

    return MARKDOWN_IMAGE_URL_PATTERN.sub(replace, markdown), refs


def _create_page_image_asset(
    session: Session,
    *,
    saved_item: SavedItem,
    capture_run_id: int,
    payload: CaptureAssetPayload,
    asset_sha256: str | None = None,
) -> SavedItemAsset:
    asset = SavedItemAsset(
        saved_item_id=saved_item.id,
        capture_run_id=capture_run_id,
        asset_kind=PAGE_IMAGE_ASSET_KIND,
        asset_role=INLINE_IMAGE_ASSET_ROLE,
        logical_name=payload.logical_name,
        source_url=payload.source_url,
        mime_type=payload.mime_type,
        file_ext=payload.file_ext,
        byte_size=len(payload.content_bytes),
        sha256=asset_sha256 or sha256(payload.content_bytes).hexdigest(),
        content_bytes=payload.content_bytes,
        metadata_json=payload.metadata,
    )
    session.add(asset)
    session.flush()
    session.refresh(asset)
    return asset


def _find_existing_page_image_asset(
    session: Session,
    *,
    saved_item_id: int,
    asset_sha256: str,
) -> SavedItemAsset | None:
    return session.scalar(
        select(SavedItemAsset)
        .where(
            SavedItemAsset.saved_item_id == saved_item_id,
            SavedItemAsset.asset_kind == PAGE_IMAGE_ASSET_KIND,
            SavedItemAsset.sha256 == asset_sha256,
        )
        .order_by(SavedItemAsset.id.asc())
        .limit(1)
    )


def _delete_unreferenced_page_image_assets(session: Session, *, saved_item_id: int) -> None:
    referenced_asset_ids = (
        select(SavedSnapshotAssetRef.asset_id)
        .join(SavedItemAsset, SavedItemAsset.id == SavedSnapshotAssetRef.asset_id)
        .where(
            SavedItemAsset.saved_item_id == saved_item_id,
            SavedItemAsset.asset_kind == PAGE_IMAGE_ASSET_KIND,
        )
    )
    stale_assets = session.scalars(
        select(SavedItemAsset).where(
            SavedItemAsset.saved_item_id == saved_item_id,
            SavedItemAsset.asset_kind == PAGE_IMAGE_ASSET_KIND,
            SavedItemAsset.id.not_in(referenced_asset_ids),
        )
    ).all()
    for asset in stale_assets:
        session.delete(asset)
    if stale_assets:
        session.flush()


def _build_archive_artifact_reads(
    session: Session,
    saved_item: SavedItem,
    snapshots_by_kind: dict[str, SavedSnapshot],
) -> list[SavedItemArchiveArtifactRead]:
    artifacts: list[SavedItemArchiveArtifactRead] = []

    source_asset = session.get(SavedItemAsset, saved_item.source_asset_id) if saved_item.source_asset_id else None
    if source_asset is not None and source_asset.asset_kind != UPLOADED_SOURCE_MATERIAL_ASSET_KIND:
        source_asset = None
    uploaded_markdown_snapshot = _current_uploaded_markdown_snapshot_for_source_asset(
        source_asset,
        snapshots_by_kind.get(UPLOADED_MARKDOWN_SNAPSHOT_KIND),
    )

    if _is_upload_saved_item(saved_item):
        artifacts.append(
            _build_asset_archive_artifact_read(
                saved_item,
                artifact_kind=UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
                asset=source_asset,
                logical_name=saved_item.original_filename,
            )
        )
        artifacts.append(
            _build_snapshot_archive_artifact_read(
                saved_item,
                snapshot_kind=UPLOADED_MARKDOWN_SNAPSHOT_KIND,
                snapshot=uploaded_markdown_snapshot,
                logical_name="uploaded",
            )
        )
        return artifacts

    singlefile_asset = _find_latest_asset(session, saved_item.id, SINGLEFILE_HTML_SNAPSHOT_KIND)
    artifacts.append(
        _build_asset_archive_artifact_read(
            saved_item,
            artifact_kind=SINGLEFILE_HTML_SNAPSHOT_KIND,
            asset=singlefile_asset,
            logical_name="singlefile",
        )
    )

    for snapshot_kind, logical_name in (
        (CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND, "raw"),
        (CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND, "fit"),
        (CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND, "fit-images"),
    ):
        artifacts.append(
            _build_snapshot_archive_artifact_read(
                saved_item,
                snapshot_kind=snapshot_kind,
                snapshot=snapshots_by_kind.get(snapshot_kind),
                logical_name=logical_name,
            )
        )

    if source_asset is not None:
        artifacts.append(
            _build_asset_archive_artifact_read(
                saved_item,
                artifact_kind=UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
                asset=source_asset,
                logical_name=saved_item.original_filename,
            )
        )
    if uploaded_markdown_snapshot is not None:
        artifacts.append(
            _build_snapshot_archive_artifact_read(
                saved_item,
                snapshot_kind=UPLOADED_MARKDOWN_SNAPSHOT_KIND,
                snapshot=uploaded_markdown_snapshot,
                logical_name="uploaded",
            )
        )

    for image_asset in _list_archive_image_assets(session, saved_item.id):
        artifacts.append(
            _build_asset_archive_artifact_read(
                saved_item,
                artifact_kind=PAGE_IMAGE_ASSET_KIND,
                asset=image_asset,
                logical_name=image_asset.logical_name,
            )
        )

    return artifacts


def _build_snapshot_archive_artifact_read(
    saved_item: SavedItem,
    *,
    snapshot_kind: str,
    snapshot: SavedSnapshot | None,
    logical_name: str,
) -> SavedItemArchiveArtifactRead:
    if snapshot is None:
        return SavedItemArchiveArtifactRead(
            artifact_kind=snapshot_kind,
            status=ARCHIVE_ARTIFACT_STATUS_UNAVAILABLE,
            snapshot_id=None,
            asset_id=None,
            asset_key=None,
            logical_name=logical_name,
            mime_type=None,
            byte_size=None,
            created_at=None,
            capture_run_id=None,
            source_url=saved_item.canonical_url,
            preview_url=None,
            download_url=None,
        )

    preview_url = _build_snapshot_content_url(saved_item.id, snapshot_kind)
    return SavedItemArchiveArtifactRead(
        artifact_kind=snapshot_kind,
        status=ARCHIVE_ARTIFACT_STATUS_AVAILABLE,
        snapshot_id=snapshot.id,
        asset_id=None,
        asset_key=None,
        logical_name=logical_name,
        mime_type=snapshot.mime_type,
        byte_size=len(snapshot.body.encode("utf-8")),
        created_at=snapshot.created_at,
        capture_run_id=snapshot.capture_run_id,
        source_url=saved_item.canonical_url,
        preview_url=preview_url,
        download_url=f"{preview_url}?download=true",
    )


def _current_uploaded_markdown_snapshot_for_source_asset(
    source_asset: SavedItemAsset | None,
    snapshot: SavedSnapshot | None,
) -> SavedSnapshot | None:
    if source_asset is None or snapshot is None:
        return None
    metadata = source_asset.metadata_json or {}
    derived_snapshot_id = metadata.get("derived_markdown_snapshot_id")
    return snapshot if derived_snapshot_id == snapshot.id else None


def _build_asset_archive_artifact_read(
    saved_item: SavedItem,
    *,
    artifact_kind: str,
    asset: SavedItemAsset | None,
    logical_name: str | None,
) -> SavedItemArchiveArtifactRead:
    if asset is None:
        return SavedItemArchiveArtifactRead(
            artifact_kind=artifact_kind,
            status=ARCHIVE_ARTIFACT_STATUS_UNAVAILABLE,
            snapshot_id=None,
            asset_id=None,
            asset_key=None,
            logical_name=logical_name,
            mime_type=None,
            byte_size=None,
            created_at=None,
            capture_run_id=None,
            source_url=saved_item.canonical_url,
            preview_url=None,
            download_url=None,
        )

    preview_url = _build_asset_content_url(asset.asset_key)
    return SavedItemArchiveArtifactRead(
        artifact_kind=artifact_kind,
        status=ARCHIVE_ARTIFACT_STATUS_AVAILABLE,
        snapshot_id=None,
        asset_id=asset.id,
        asset_key=asset.asset_key,
        logical_name=asset.logical_name or logical_name,
        mime_type=asset.mime_type,
        byte_size=asset.byte_size,
        created_at=asset.created_at,
        capture_run_id=asset.capture_run_id,
        source_url=asset.source_url,
        preview_url=preview_url,
        download_url=f"{preview_url}?download=true",
    )


def _find_latest_asset(session: Session, saved_item_id: int, asset_kind: str) -> SavedItemAsset | None:
    return session.scalar(
        select(SavedItemAsset)
        .where(
            SavedItemAsset.saved_item_id == saved_item_id,
            SavedItemAsset.asset_kind == asset_kind,
        )
        .order_by(SavedItemAsset.id.desc())
        .limit(1)
    )


def _list_archive_image_assets(session: Session, saved_item_id: int) -> list[SavedItemAsset]:
    asset_ids = _load_current_image_asset_ids_map(session, [saved_item_id]).get(saved_item_id, [])
    if not asset_ids:
        return []

    assets_by_id = {
        asset.id: asset
        for asset in session.scalars(select(SavedItemAsset).where(SavedItemAsset.id.in_(asset_ids))).all()
    }
    return [asset for asset_id in asset_ids if (asset := assets_by_id.get(asset_id)) is not None]


def _build_snapshot_content_url(saved_item_id: int, snapshot_kind: str) -> str:
    return f"/api/knowledge/saved-items/{saved_item_id}/snapshots/{snapshot_kind}/content"


def _build_asset_content_url(asset_key: str) -> str:
    return f"/api/knowledge/assets/{asset_key}/content"


def build_saved_item_archive_identity(saved_item: SavedItem) -> str:
    if saved_item.canonical_url:
        return saved_item.canonical_url
    upload_parts = [
        "upload",
        str(saved_item.id),
        saved_item.original_filename or "",
        str(saved_item.source_asset_id or ""),
    ]
    return ":".join(upload_parts)


def build_saved_item_archive_slug(title: str | None) -> str:
    normalized = _normalize_compare_text(title)
    slug_chars: list[str] = []
    previous_was_separator = False
    for char in normalized:
        if char.isalnum():
            slug_chars.append(char)
            previous_was_separator = False
        elif not previous_was_separator:
            slug_chars.append("-")
            previous_was_separator = True
    slug = "".join(slug_chars).strip("-")
    return slug[:80].strip("-") or "untitled"


def build_saved_item_archive_dir_name(title: str | None, canonical_url: str | None) -> str:
    return f"{build_saved_item_archive_slug(title)}--{build_saved_item_url_hash(canonical_url)}"


def build_saved_item_url_hash(canonical_url: str | None) -> str:
    normalized = (canonical_url or "").strip()
    return sha256(normalized.encode("utf-8")).hexdigest()[:8]


def build_saved_item_title_file_stem(title: str | None) -> str:
    normalized = re.sub(r"\s+", " ", title or "").strip()
    if not normalized:
        normalized = "untitled"
    safe_chars: list[str] = []
    previous_was_separator = False
    for char in normalized:
        if char in '<>:"/\\|?*' or ord(char) < 32:
            if not previous_was_separator:
                safe_chars.append("_")
                previous_was_separator = True
            continue
        safe_chars.append(char)
        previous_was_separator = char in {" ", "_", "-", "."}
    safe = "".join(safe_chars).strip(" ._")
    safe = safe[:120].strip(" ._") or "untitled"
    if safe.upper() in WINDOWS_RESERVED_FILE_STEMS:
        return f"{safe}_"
    return safe


def build_local_mirror_markdown_filename(title: str | None, variant: str) -> str:
    return f"{build_saved_item_title_file_stem(title)}_{variant}.md"


def build_local_mirror_singlefile_filename(title: str | None) -> str:
    return f"{build_saved_item_title_file_stem(title)}.html"


def _build_source_material_filename(saved_item: SavedItem, source_asset: SavedItemAsset | None) -> str | None:
    if source_asset is None:
        return None
    return _build_source_material_filename_from_values(
        saved_item.title,
        saved_item.original_filename or source_asset.logical_name,
        source_asset.file_ext,
    )


def build_local_mirror_source_material_filename(saved_item: SavedItem, source_asset: SavedItemAsset) -> str:
    return _build_source_material_filename(saved_item, source_asset) or _build_source_material_filename_from_values(
        saved_item.title,
        source_asset.logical_name,
        source_asset.file_ext,
    )


def _build_source_material_filename_from_values(
    title: str | None,
    original_filename: str | None,
    file_ext: str | None,
) -> str:
    normalized_ext = (file_ext or Path(original_filename or "").suffix.removeprefix(".") or "bin").strip().lstrip(".")
    original_stem = Path(original_filename or "").stem.strip()
    stem = build_saved_item_asset_name_stem(original_stem, build_saved_item_title_file_stem(title))
    return f"{stem}_source.{normalized_ext}"


def build_saved_item_asset_name_stem(value: str | None, fallback: str) -> str:
    normalized = re.sub(r"\s+", "_", value or "").strip(" ._")
    if not normalized:
        normalized = fallback
    safe_chars: list[str] = []
    previous_was_separator = False
    for char in normalized:
        if char in '<>:"/\\|?*' or ord(char) < 32:
            if not previous_was_separator:
                safe_chars.append("_")
                previous_was_separator = True
            continue
        safe_chars.append(char)
        previous_was_separator = char in {" ", "_", "-", "."}
    safe = "".join(safe_chars).strip(" ._")
    safe = safe[:80].strip(" ._") or fallback
    if safe.upper() in WINDOWS_RESERVED_FILE_STEMS:
        return f"{safe}_"
    return safe


def build_local_mirror_asset_filename(
    title: str | None,
    canonical_url: str | None,
    asset: SavedItemAsset,
    *,
    index: int,
) -> str:
    normalized_ext = asset.file_ext.strip().lstrip(".") or "bin"
    asset_name = build_saved_item_asset_name_stem(asset.logical_name, f"image-{index}")
    title_stem = build_saved_item_title_file_stem(title)
    url_hash = build_saved_item_url_hash(canonical_url)
    return f"{title_stem}--{url_hash}_{asset_name}.{normalized_ext}"


def build_archive_snapshot_download_filename(saved_item_id: int, title: str | None, snapshot_kind: str) -> str:
    kind_label = {
        CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND: "raw",
        CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND: "fit",
        CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND: "fit-images",
        UPLOADED_MARKDOWN_SNAPSHOT_KIND: "uploaded",
    }.get(snapshot_kind, "snapshot")
    return build_local_mirror_markdown_filename(title, kind_label)


def build_archive_asset_download_filename(saved_item_id: int, title: str | None, asset: SavedItemAsset) -> str:
    normalized_ext = asset.file_ext.strip().lstrip(".") or "bin"
    if asset.asset_kind == SINGLEFILE_HTML_SNAPSHOT_KIND:
        return build_local_mirror_singlefile_filename(title)
    if asset.asset_kind == UPLOADED_SOURCE_MATERIAL_ASSET_KIND:
        return _build_source_material_filename_from_values(title, asset.logical_name, normalized_ext)
    return f"{build_saved_item_title_file_stem(title)}_asset-{asset.asset_key}.{normalized_ext}"


def build_content_disposition_header(filename: str) -> str:
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "download"
    return f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def _render_summary_fragment(value: str | None) -> str | None:
    summary_html = render_summary_html(value)
    if summary_html is not None:
        return summary_html
    return _render_content_fragment(value)


def _render_content_fragment(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    paragraphs = [line.strip() for line in re.split(r"(?:\r?\n){2,}", normalized) if line.strip()]
    if not paragraphs:
        return None

    return "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)


def _extract_summary_body(body: str) -> str | None:
    match = SUMMARY_BODY_PATTERN.search(body)
    if match is None:
        return None
    extracted = match.group("body").strip()
    return extracted or None


def _extract_content_body(body: str) -> str | None:
    match = CONTENT_BODY_PATTERN.search(body)
    if match is None:
        return None
    extracted = match.group("body").strip()
    return extracted or None


def _rewrite_asset_refs_for_app(value: str | None) -> str | None:
    if value is None:
        return None

    return ASSET_REF_PATTERN.sub(
        lambda match: f"/api/knowledge/assets/{match.group('asset_key')}/content",
        value,
    )


def _markdown_to_plain_preview(markdown: str) -> str:
    stripped = MARKDOWN_IMAGE_URL_PATTERN.sub(" ", markdown)
    stripped = re.sub(r"```.*?```", " ", stripped, flags=re.DOTALL)
    stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
    stripped = MARKDOWN_HEADING_PATTERN.sub("", stripped)
    stripped = re.sub(r"^[ \t]*[-*+]\s+", "", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"^[ \t]*\d+[.)]\s+", "", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"[*_~>#|]+", " ", stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def _build_markdown_summary(markdown: str) -> str:
    plain = _markdown_to_plain_preview(markdown)
    if len(plain) <= 800:
        return plain
    return plain[:800].rsplit(" ", 1)[0].strip() or plain[:800].strip()


def _is_upload_saved_item(saved_item: SavedItem) -> bool:
    return saved_item.origin_type in UPLOAD_ORIGIN_TYPES
