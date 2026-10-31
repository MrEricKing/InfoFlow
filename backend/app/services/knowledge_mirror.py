from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from os.path import relpath
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    MirrorExportBatch,
    SavedItem,
    SavedItemAsset,
    SavedItemMirrorExport,
    SavedItemMirrorExportFile,
    SavedSnapshot,
    SavedSnapshotAssetRef,
)
from app.services.app_settings import resolve_knowledge_base_root
from app.services.knowledge_constants import (
    ASSET_REF_SCHEME,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    PAGE_IMAGE_ASSET_KIND,
    SINGLEFILE_HTML_SNAPSHOT_KIND,
    UPLOADED_MARKDOWN_SNAPSHOT_KIND,
    UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
)
from app.services.knowledge_snapshot_assets import (
    build_saved_item_archive_identity,
    build_saved_item_archive_dir_name,
    _current_uploaded_markdown_snapshot_for_source_asset,
    build_local_mirror_asset_filename,
    build_local_mirror_markdown_filename,
    build_local_mirror_singlefile_filename,
    build_local_mirror_source_material_filename,
)

MIRROR_PROFILE_KNOWLEDGE_BASE = "knowledge_base_v1"

MIRROR_TRIGGER_MANUAL_EXPORT = "manual_export"
MIRROR_TRIGGER_AUTO_AFTER_CAPTURE = "auto_after_capture"
MIRROR_TRIGGER_FULL_REBUILD = "full_rebuild"
MIRROR_TRIGGER_SOFT_REMOVE_ARCHIVE = "soft_remove_archive"

MIRROR_STATUS_PENDING = "pending"
MIRROR_STATUS_SUCCEEDED = "succeeded"
MIRROR_STATUS_PARTIAL_FAILED = "partial_failed"
MIRROR_STATUS_FAILED = "failed"

MIRROR_TARGET_RAW_MARKDOWN = "raw_markdown"
MIRROR_TARGET_FIT_MARKDOWN = "fit_markdown"
MIRROR_TARGET_FIT_IMAGE_REINSERTED_MARKDOWN = "fit_image_reinserted_markdown"
MIRROR_TARGET_ASSETS = "assets"
MIRROR_TARGET_SINGLEFILE_HTML = "singlefile_html"
MIRROR_TARGET_SOURCE_MATERIAL = "source_material"
MIRROR_TARGET_UPLOADED_MARKDOWN = "uploaded_markdown"
MIRROR_TARGET_MANIFEST = "manifest"
MIRROR_ARCHIVE_DIR_NAME = ".archive"

DEFAULT_MIRROR_TARGETS = (
    MIRROR_TARGET_RAW_MARKDOWN,
    MIRROR_TARGET_FIT_MARKDOWN,
    MIRROR_TARGET_FIT_IMAGE_REINSERTED_MARKDOWN,
    MIRROR_TARGET_ASSETS,
    MIRROR_TARGET_SINGLEFILE_HTML,
    MIRROR_TARGET_SOURCE_MATERIAL,
    MIRROR_TARGET_UPLOADED_MARKDOWN,
    MIRROR_TARGET_MANIFEST,
)


def export_saved_item_to_local_mirror(
    session: Session,
    saved_item_id: int,
    *,
    trigger_kind: str,
    source_capture_run_id: int | None = None,
    batch_id: int | None = None,
    mirror_profile: str = MIRROR_PROFILE_KNOWLEDGE_BASE,
    requested_targets: tuple[str, ...] = DEFAULT_MIRROR_TARGETS,
    root_dir: Path | None = None,
    archived: bool = False,
) -> SavedItemMirrorExport:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None:
        raise ValueError(f"Saved item {saved_item_id} not found")

    export = SavedItemMirrorExport(
        saved_item_id=saved_item_id,
        source_capture_run_id=source_capture_run_id,
        batch_id=batch_id,
        mirror_profile=mirror_profile,
        trigger_kind=trigger_kind,
        status=MIRROR_STATUS_PENDING,
        requested_targets_json={"targets": list(requested_targets)},
        started_at=_now_utc(),
        finished_at=None,
    )
    session.add(export)
    session.flush()

    actual_root = root_dir or resolve_knowledge_base_root(session)
    item_root = _build_item_root_paths(actual_root, saved_item, archived=archived)

    raw_snapshot = _find_latest_snapshot(session, saved_item_id, CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND)
    fit_snapshot = _find_latest_snapshot(session, saved_item_id, CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND)
    reinserted_snapshot = _find_latest_snapshot(
        session,
        saved_item_id,
        CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    )
    uploaded_markdown_snapshot = _find_latest_snapshot(session, saved_item_id, UPLOADED_MARKDOWN_SNAPSHOT_KIND)
    singlefile_asset = _find_latest_asset(session, saved_item_id, SINGLEFILE_HTML_SNAPSHOT_KIND)
    source_material_asset = (
        session.get(SavedItemAsset, saved_item.source_asset_id) if saved_item.source_asset_id is not None else None
    )
    if source_material_asset is not None and source_material_asset.asset_kind != UPLOADED_SOURCE_MATERIAL_ASSET_KIND:
        source_material_asset = None
    uploaded_markdown_snapshot = _current_uploaded_markdown_snapshot_for_source_asset(
        source_material_asset,
        uploaded_markdown_snapshot,
    )
    snapshot_ids = [
        snapshot.id
        for snapshot in (raw_snapshot, fit_snapshot, reinserted_snapshot, uploaded_markdown_snapshot)
        if snapshot is not None
    ]
    image_assets = _list_referenced_page_image_assets(session, snapshot_ids)
    asset_paths = _build_asset_file_paths(item_root["assets_dir"], saved_item, image_assets)
    refs_by_snapshot_id = _load_snapshot_asset_refs_map(
        session,
        snapshot_ids,
    )

    if source_capture_run_id is None:
        export.source_capture_run_id = _resolve_source_capture_run_id(
            raw_snapshot,
            fit_snapshot,
            reinserted_snapshot,
            uploaded_markdown_snapshot,
            singlefile_asset,
            source_material_asset,
            image_assets,
        )

    exportable = any(
        [
            raw_snapshot,
            fit_snapshot,
            reinserted_snapshot,
            uploaded_markdown_snapshot,
            singlefile_asset,
            source_material_asset,
            image_assets,
        ]
    )
    if not exportable:
        export.status = MIRROR_STATUS_FAILED
        export.error_summary = "No mirror-exportable artifacts available"
        export.finished_at = _now_utc()
        session.add(export)
        session.flush()
        return export

    try:
        _cleanup_previous_export_directories(session, root_dir=actual_root, saved_item_id=saved_item_id)
        _cleanup_counterpart_item_directory(actual_root, saved_item, archived=archived)
        _clean_item_root_paths(item_root)
    except OSError as exc:
        return _fail_export(session, export=export, error_summary=str(exc))

    written_files = 0
    errors: list[str] = []
    manifest_entries: list[dict[str, object]] = []

    if MIRROR_TARGET_RAW_MARKDOWN in requested_targets and raw_snapshot is not None:
        raw_filename = build_local_mirror_markdown_filename(saved_item.title, "raw")
        raw_path = item_root["content_dir"] / raw_filename
        rewritten = rewrite_markdown_asset_refs(
            raw_snapshot.body,
            _build_snapshot_relative_asset_paths(raw_path, asset_paths, refs_by_snapshot_id.get(raw_snapshot.id, [])),
        )
        try:
            export_file = _write_export_file(
                session,
                export_id=export.id,
                source_kind="snapshot",
                source_id=raw_snapshot.id,
                file_role=MIRROR_TARGET_RAW_MARKDOWN,
                absolute_path=raw_path,
                relative_path=_relative_to_root(actual_root, raw_path),
                payload=rewritten.encode("utf-8"),
            )
            manifest_entries.append(_build_manifest_file_entry(export_file, snapshot=raw_snapshot))
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    if MIRROR_TARGET_FIT_MARKDOWN in requested_targets and fit_snapshot is not None:
        fit_filename = build_local_mirror_markdown_filename(saved_item.title, "fit")
        fit_path = item_root["content_dir"] / fit_filename
        rewritten = rewrite_markdown_asset_refs(
            fit_snapshot.body,
            _build_snapshot_relative_asset_paths(fit_path, asset_paths, refs_by_snapshot_id.get(fit_snapshot.id, [])),
        )
        try:
            export_file = _write_export_file(
                session,
                export_id=export.id,
                source_kind="snapshot",
                source_id=fit_snapshot.id,
                file_role=MIRROR_TARGET_FIT_MARKDOWN,
                absolute_path=fit_path,
                relative_path=_relative_to_root(actual_root, fit_path),
                payload=rewritten.encode("utf-8"),
            )
            manifest_entries.append(_build_manifest_file_entry(export_file, snapshot=fit_snapshot))
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    if MIRROR_TARGET_FIT_IMAGE_REINSERTED_MARKDOWN in requested_targets and reinserted_snapshot is not None:
        reinserted_filename = build_local_mirror_markdown_filename(saved_item.title, "fit-images")
        reinserted_path = item_root["content_dir"] / reinserted_filename
        rewritten = rewrite_markdown_asset_refs(
            reinserted_snapshot.body,
            _build_snapshot_relative_asset_paths(
                reinserted_path,
                asset_paths,
                refs_by_snapshot_id.get(reinserted_snapshot.id, []),
            ),
        )
        try:
            export_file = _write_export_file(
                session,
                export_id=export.id,
                source_kind="snapshot",
                source_id=reinserted_snapshot.id,
                file_role=MIRROR_TARGET_FIT_IMAGE_REINSERTED_MARKDOWN,
                absolute_path=reinserted_path,
                relative_path=_relative_to_root(actual_root, reinserted_path),
                payload=rewritten.encode("utf-8"),
            )
            manifest_entries.append(_build_manifest_file_entry(export_file, snapshot=reinserted_snapshot))
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    if MIRROR_TARGET_SINGLEFILE_HTML in requested_targets and singlefile_asset is not None:
        singlefile_filename = build_local_mirror_singlefile_filename(saved_item.title)
        try:
            export_file = _write_export_file(
                session,
                export_id=export.id,
                source_kind="asset",
                source_id=singlefile_asset.id,
                file_role=MIRROR_TARGET_SINGLEFILE_HTML,
                absolute_path=item_root["content_dir"] / singlefile_filename,
                relative_path=_relative_to_root(actual_root, item_root["content_dir"] / singlefile_filename),
                payload=singlefile_asset.content_bytes,
            )
            manifest_entries.append(_build_manifest_file_entry(export_file, asset=singlefile_asset))
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    if MIRROR_TARGET_SOURCE_MATERIAL in requested_targets and source_material_asset is not None:
        source_material_filename = build_local_mirror_source_material_filename(saved_item, source_material_asset)
        source_material_path = item_root["content_dir"] / source_material_filename
        try:
            export_file = _write_export_file(
                session,
                export_id=export.id,
                source_kind="asset",
                source_id=source_material_asset.id,
                file_role=MIRROR_TARGET_SOURCE_MATERIAL,
                absolute_path=source_material_path,
                relative_path=_relative_to_root(actual_root, source_material_path),
                payload=source_material_asset.content_bytes,
                expected_sha256=source_material_asset.sha256,
                expected_byte_size=source_material_asset.byte_size,
            )
            manifest_entries.append(_build_manifest_file_entry(export_file, asset=source_material_asset))
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    if MIRROR_TARGET_UPLOADED_MARKDOWN in requested_targets and uploaded_markdown_snapshot is not None:
        uploaded_filename = build_local_mirror_markdown_filename(saved_item.title, "uploaded")
        uploaded_path = item_root["content_dir"] / uploaded_filename
        rewritten = rewrite_markdown_asset_refs(
            uploaded_markdown_snapshot.body,
            _build_snapshot_relative_asset_paths(
                uploaded_path,
                asset_paths,
                refs_by_snapshot_id.get(uploaded_markdown_snapshot.id, []),
            ),
        )
        try:
            export_file = _write_export_file(
                session,
                export_id=export.id,
                source_kind="snapshot",
                source_id=uploaded_markdown_snapshot.id,
                file_role=MIRROR_TARGET_UPLOADED_MARKDOWN,
                absolute_path=uploaded_path,
                relative_path=_relative_to_root(actual_root, uploaded_path),
                payload=rewritten.encode("utf-8"),
            )
            manifest_entries.append(_build_manifest_file_entry(export_file, snapshot=uploaded_markdown_snapshot))
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    if MIRROR_TARGET_ASSETS in requested_targets:
        for asset in image_assets:
            absolute_path = asset_paths[asset.id]
            relative_path = _relative_to_root(actual_root, absolute_path)
            try:
                export_file = _write_export_file(
                    session,
                    export_id=export.id,
                    source_kind="asset",
                    source_id=asset.id,
                    file_role=MIRROR_TARGET_ASSETS,
                    absolute_path=absolute_path,
                    relative_path=relative_path,
                    payload=asset.content_bytes,
                    expected_sha256=asset.sha256,
                    expected_byte_size=asset.byte_size,
                )
                manifest_entries.append(_build_manifest_file_entry(export_file, asset=asset))
                written_files += 1
            except OSError as exc:
                errors.append(str(exc))

    if MIRROR_TARGET_MANIFEST in requested_targets:
        manifest_payload = _build_manifest_payload(
            saved_item=saved_item,
            mirror_profile=mirror_profile,
            export=export,
            raw_snapshot=raw_snapshot,
            fit_snapshot=fit_snapshot,
            reinserted_snapshot=reinserted_snapshot,
            uploaded_markdown_snapshot=uploaded_markdown_snapshot,
            singlefile_asset=singlefile_asset,
            source_material_asset=source_material_asset,
            image_assets=image_assets,
            files=manifest_entries,
        )
        try:
            _write_export_file(
                session,
                export_id=export.id,
                source_kind="saved_item",
                source_id=saved_item.id,
                file_role=MIRROR_TARGET_MANIFEST,
                absolute_path=item_root["metadata_dir"] / "manifest.json",
                relative_path=_relative_to_root(actual_root, item_root["metadata_dir"] / "manifest.json"),
                payload=json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            )
            written_files += 1
        except OSError as exc:
            errors.append(str(exc))

    export.status = _derive_export_status(written_files=written_files, errors=errors)
    export.error_summary = errors[0] if errors else None
    export.finished_at = _now_utc()
    session.add(export)
    session.flush()
    return export


def archive_removed_saved_item_local_mirror(
    session: Session,
    saved_item_id: int,
    *,
    root_dir: Path | None = None,
) -> SavedItemMirrorExport:
    return export_saved_item_to_local_mirror(
        session,
        saved_item_id,
        trigger_kind=MIRROR_TRIGGER_SOFT_REMOVE_ARCHIVE,
        root_dir=root_dir,
        archived=True,
    )


def rebuild_local_mirror_from_db(
    session: Session,
    *,
    mirror_profile: str = MIRROR_PROFILE_KNOWLEDGE_BASE,
    root_dir: Path | None = None,
) -> MirrorExportBatch:
    actual_root = root_dir or resolve_knowledge_base_root(session)
    saved_item_rows = list(session.execute(select(SavedItem.id, SavedItem.removed_at).order_by(SavedItem.id.asc())))
    active_saved_item_ids = [saved_item_id for saved_item_id, removed_at in saved_item_rows if removed_at is None]
    removed_saved_item_ids = [saved_item_id for saved_item_id, removed_at in saved_item_rows if removed_at is not None]
    saved_item_ids = [*active_saved_item_ids, *removed_saved_item_ids]

    batch = MirrorExportBatch(
        mirror_profile=mirror_profile,
        trigger_kind=MIRROR_TRIGGER_FULL_REBUILD,
        status=MIRROR_STATUS_PENDING,
        requested_scope_json={
            "saved_item_ids": saved_item_ids,
            "active_saved_item_ids": active_saved_item_ids,
            "removed_saved_item_ids": removed_saved_item_ids,
        },
        total_items=len(saved_item_ids),
        completed_items=0,
        failed_items=0,
        started_at=_now_utc(),
        finished_at=None,
    )
    session.add(batch)
    session.flush()

    for saved_item_id in active_saved_item_ids:
        export = export_saved_item_to_local_mirror(
            session,
            saved_item_id,
            trigger_kind=MIRROR_TRIGGER_FULL_REBUILD,
            batch_id=batch.id,
            mirror_profile=mirror_profile,
            root_dir=actual_root,
        )
        if export.status == MIRROR_STATUS_SUCCEEDED:
            batch.completed_items += 1
        else:
            batch.failed_items += 1

    for saved_item_id in removed_saved_item_ids:
        export = export_saved_item_to_local_mirror(
            session,
            saved_item_id,
            trigger_kind=MIRROR_TRIGGER_FULL_REBUILD,
            batch_id=batch.id,
            mirror_profile=mirror_profile,
            root_dir=actual_root,
            archived=True,
        )
        if export.status == MIRROR_STATUS_SUCCEEDED:
            batch.completed_items += 1
        else:
            batch.failed_items += 1

    try:
        _cleanup_orphan_item_directories(actual_root, set(active_saved_item_ids))
        _cleanup_orphan_item_directories(actual_root / MIRROR_ARCHIVE_DIR_NAME, set(removed_saved_item_ids))
    except OSError as exc:
        batch.failed_items += 1
        batch.error_summary = str(exc)

    batch.status = MIRROR_STATUS_SUCCEEDED if batch.failed_items == 0 else MIRROR_STATUS_PARTIAL_FAILED
    batch.finished_at = _now_utc()
    session.add(batch)
    session.flush()
    return batch


def rewrite_markdown_asset_refs(markdown: str, relative_asset_paths: dict[str, str]) -> str:
    rewritten = markdown
    for ref_token, relative_path in relative_asset_paths.items():
        rewritten = rewritten.replace(ref_token, relative_path)
    return rewritten


def _build_item_root_paths(root_dir: Path, saved_item: SavedItem, *, archived: bool = False) -> dict[str, Path]:
    parent_dir = root_dir / MIRROR_ARCHIVE_DIR_NAME if archived else root_dir
    article_dir = parent_dir / build_saved_item_archive_dir_name(
        saved_item.title,
        build_saved_item_archive_identity(saved_item),
    )
    return {
        "article_dir": article_dir,
        "content_dir": article_dir,
        "assets_dir": article_dir / "assets",
        "metadata_dir": article_dir / ".infoflow",
    }


def _clean_item_root_paths(paths: dict[str, Path]) -> None:
    article_dir = paths["article_dir"]
    if article_dir.exists():
        shutil.rmtree(article_dir)
    for path in (paths["content_dir"], paths["assets_dir"], paths["metadata_dir"]):
        path.mkdir(parents=True, exist_ok=True)


def _cleanup_orphan_item_directories(root_dir: Path, active_saved_item_ids: set[int]) -> None:
    if not root_dir.exists():
        return
    for child in root_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == MIRROR_ARCHIVE_DIR_NAME:
            continue
        manifest_path = child / ".infoflow" / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            saved_item_id = int(manifest["saved_item_id"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if saved_item_id not in active_saved_item_ids:
            shutil.rmtree(child)


def _cleanup_previous_export_directories(session: Session, *, root_dir: Path, saved_item_id: int) -> None:
    previous_exports = list(
        session.scalars(
            select(SavedItemMirrorExport)
            .where(SavedItemMirrorExport.saved_item_id == saved_item_id)
            .order_by(SavedItemMirrorExport.id.desc())
        )
    )
    if not previous_exports:
        return

    previous_export_ids = [export.id for export in previous_exports]
    previous_files = list(
        session.scalars(
            select(SavedItemMirrorExportFile).where(SavedItemMirrorExportFile.mirror_export_id.in_(previous_export_ids))
        )
    )
    candidate_dirs: set[Path] = set()
    for export_file in previous_files:
        relative_path = Path(export_file.relative_path)
        if (
            len(relative_path.parts) >= 3
            and relative_path.parts[0] == MIRROR_ARCHIVE_DIR_NAME
            and relative_path.parts[1] not in {"raw", "assets", "singlefile"}
        ):
            candidate_dirs.add(root_dir / relative_path.parts[0] / relative_path.parts[1])
        elif len(relative_path.parts) >= 2 and relative_path.parts[0] not in {
            MIRROR_ARCHIVE_DIR_NAME,
            "raw",
            "assets",
            "singlefile",
        }:
            candidate_dirs.add(root_dir / relative_path.parts[0])

    for candidate_dir in candidate_dirs:
        if _manifest_matches_saved_item(candidate_dir / ".infoflow" / "manifest.json", saved_item_id):
            shutil.rmtree(candidate_dir)


def _cleanup_counterpart_item_directory(root_dir: Path, saved_item: SavedItem, *, archived: bool) -> None:
    counterpart_paths = _build_item_root_paths(root_dir, saved_item, archived=not archived)
    counterpart_dir = counterpart_paths["article_dir"]
    if _manifest_matches_saved_item(counterpart_dir / ".infoflow" / "manifest.json", saved_item.id):
        shutil.rmtree(counterpart_dir)


def _manifest_matches_saved_item(manifest_path: Path, saved_item_id: int) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return manifest.get("saved_item_id") == saved_item_id


def _find_latest_snapshot(session: Session, saved_item_id: int, snapshot_kind: str) -> SavedSnapshot | None:
    return session.scalar(
        select(SavedSnapshot)
        .where(SavedSnapshot.saved_item_id == saved_item_id, SavedSnapshot.snapshot_kind == snapshot_kind)
        .order_by(SavedSnapshot.id.desc())
        .limit(1)
    )


def _find_latest_asset(session: Session, saved_item_id: int, asset_kind: str) -> SavedItemAsset | None:
    return session.scalar(
        select(SavedItemAsset)
        .where(SavedItemAsset.saved_item_id == saved_item_id, SavedItemAsset.asset_kind == asset_kind)
        .order_by(SavedItemAsset.id.desc())
        .limit(1)
    )


def _list_referenced_page_image_assets(session: Session, snapshot_ids: list[int]) -> list[SavedItemAsset]:
    if not snapshot_ids:
        return []

    refs = list(
        session.scalars(
            select(SavedSnapshotAssetRef)
            .join(SavedItemAsset, SavedItemAsset.id == SavedSnapshotAssetRef.asset_id)
            .where(
                SavedSnapshotAssetRef.saved_snapshot_id.in_(snapshot_ids),
                SavedItemAsset.asset_kind == PAGE_IMAGE_ASSET_KIND,
            )
            .order_by(SavedSnapshotAssetRef.saved_snapshot_id.asc(), SavedSnapshotAssetRef.ordinal.asc())
        )
    )
    asset_ids: list[int] = []
    seen: set[int] = set()
    for ref in refs:
        if ref.asset_id in seen:
            continue
        seen.add(ref.asset_id)
        asset_ids.append(ref.asset_id)
    if not asset_ids:
        return []

    assets_by_id = {
        asset.id: asset
        for asset in session.scalars(select(SavedItemAsset).where(SavedItemAsset.id.in_(asset_ids))).all()
    }
    return [asset for asset_id in asset_ids if (asset := assets_by_id.get(asset_id)) is not None]


def _build_asset_file_paths(
    assets_dir: Path,
    saved_item: SavedItem,
    image_assets: list[SavedItemAsset],
) -> dict[int, Path]:
    result: dict[int, Path] = {}
    used_names: set[str] = set()
    for index, asset in enumerate(image_assets, start=1):
        filename = build_local_mirror_asset_filename(
            saved_item.title,
            saved_item.canonical_url,
            asset,
            index=index,
        )
        if filename in used_names:
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            filename = f"{stem}-{index}{suffix}"
        used_names.add(filename)
        result[asset.id] = assets_dir / filename
    return result


def _load_snapshot_asset_refs_map(
    session: Session,
    snapshot_ids: list[int],
) -> dict[int, list[tuple[SavedSnapshotAssetRef, SavedItemAsset | None]]]:
    if not snapshot_ids:
        return {}

    refs = list(
        session.scalars(
            select(SavedSnapshotAssetRef)
            .where(SavedSnapshotAssetRef.saved_snapshot_id.in_(snapshot_ids))
            .order_by(SavedSnapshotAssetRef.saved_snapshot_id.asc(), SavedSnapshotAssetRef.ordinal.asc())
        )
    )
    asset_ids = [ref.asset_id for ref in refs]
    assets = (
        {
            asset.id: asset
            for asset in session.scalars(select(SavedItemAsset).where(SavedItemAsset.id.in_(asset_ids))).all()
        }
        if asset_ids
        else {}
    )

    result: dict[int, list[tuple[SavedSnapshotAssetRef, SavedItemAsset | None]]] = {}
    for ref in refs:
        result.setdefault(ref.saved_snapshot_id, []).append((ref, assets.get(ref.asset_id)))
    return result


def _build_snapshot_relative_asset_paths(
    markdown_path: Path,
    asset_paths: dict[int, Path],
    refs: list[tuple[SavedSnapshotAssetRef, SavedItemAsset | None]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for ref, asset in refs:
        if asset is None or not ref.ref_token.startswith(ASSET_REF_SCHEME):
            continue
        asset_path = asset_paths.get(asset.id)
        if asset_path is None:
            continue
        result[ref.ref_token] = _relative_path(markdown_path, asset_path)
    return result


def _resolve_source_capture_run_id(
    raw_snapshot: SavedSnapshot | None,
    fit_snapshot: SavedSnapshot | None,
    reinserted_snapshot: SavedSnapshot | None,
    uploaded_markdown_snapshot: SavedSnapshot | None,
    singlefile_asset: SavedItemAsset | None,
    source_material_asset: SavedItemAsset | None,
    image_assets: list[SavedItemAsset],
) -> int | None:
    candidates = [
        raw_snapshot.capture_run_id if raw_snapshot is not None else None,
        fit_snapshot.capture_run_id if fit_snapshot is not None else None,
        reinserted_snapshot.capture_run_id if reinserted_snapshot is not None else None,
        uploaded_markdown_snapshot.capture_run_id if uploaded_markdown_snapshot is not None else None,
        singlefile_asset.capture_run_id if singlefile_asset is not None else None,
        source_material_asset.capture_run_id if source_material_asset is not None else None,
        *[asset.capture_run_id for asset in image_assets],
    ]
    valid_candidates = [candidate for candidate in candidates if candidate is not None]
    return max(valid_candidates) if valid_candidates else None


def _build_manifest_payload(
    *,
    saved_item: SavedItem,
    mirror_profile: str,
    export: SavedItemMirrorExport,
    raw_snapshot: SavedSnapshot | None,
    fit_snapshot: SavedSnapshot | None,
    reinserted_snapshot: SavedSnapshot | None,
    uploaded_markdown_snapshot: SavedSnapshot | None,
    singlefile_asset: SavedItemAsset | None,
    source_material_asset: SavedItemAsset | None,
    image_assets: list[SavedItemAsset],
    files: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "manifest_version": "knowledge_base_manifest_v1",
        "saved_item_id": saved_item.id,
        "title": saved_item.title,
        "canonical_url": saved_item.canonical_url,
        "mirror_profile": mirror_profile,
        "mirror_export_id": export.id,
        "source_capture_run_id": export.source_capture_run_id,
        "generated_at": _now_utc().isoformat(),
        "artifacts": {
            "raw_snapshot_id": raw_snapshot.id if raw_snapshot is not None else None,
            "fit_snapshot_id": fit_snapshot.id if fit_snapshot is not None else None,
            "fit_image_reinserted_snapshot_id": reinserted_snapshot.id if reinserted_snapshot is not None else None,
            "uploaded_markdown_snapshot_id": uploaded_markdown_snapshot.id if uploaded_markdown_snapshot is not None else None,
            "singlefile_asset_id": singlefile_asset.id if singlefile_asset is not None else None,
            "source_material_asset_id": source_material_asset.id if source_material_asset is not None else None,
            "image_asset_ids": [asset.id for asset in image_assets],
        },
        "files": files,
    }


def _build_manifest_file_entry(
    export_file: SavedItemMirrorExportFile,
    *,
    snapshot: SavedSnapshot | None = None,
    asset: SavedItemAsset | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "role": export_file.file_role,
        "relative_path": export_file.relative_path,
        "source_kind": export_file.source_kind,
        "source_id": export_file.source_id,
        "byte_size": export_file.actual_byte_size,
        "sha256": export_file.actual_sha256,
        "expected_byte_size": export_file.expected_byte_size,
        "expected_sha256": export_file.expected_sha256,
        "verification_status": export_file.verification_status,
    }
    if snapshot is not None:
        entry["snapshot_id"] = snapshot.id
        entry["snapshot_kind"] = snapshot.snapshot_kind
        entry["mime_type"] = snapshot.mime_type
    if asset is not None:
        entry["asset_id"] = asset.id
        entry["asset_key"] = asset.asset_key
        entry["asset_kind"] = asset.asset_kind
        entry["mime_type"] = asset.mime_type
        entry["source_url"] = asset.source_url
    return entry


def _derive_export_status(*, written_files: int, errors: list[str]) -> str:
    if errors and written_files:
        return MIRROR_STATUS_PARTIAL_FAILED
    if errors:
        return MIRROR_STATUS_FAILED
    return MIRROR_STATUS_SUCCEEDED


def _fail_export(session: Session, *, export: SavedItemMirrorExport, error_summary: str) -> SavedItemMirrorExport:
    export.status = MIRROR_STATUS_FAILED
    export.error_summary = error_summary
    export.finished_at = _now_utc()
    session.add(export)
    session.flush()
    return export


def _write_export_file(
    session: Session,
    *,
    export_id: int,
    source_kind: str,
    source_id: int,
    file_role: str,
    absolute_path: Path,
    relative_path: Path,
    payload: bytes,
    expected_sha256: str | None = None,
    expected_byte_size: int | None = None,
) -> SavedItemMirrorExportFile:
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(payload)

    actual_bytes = absolute_path.read_bytes()
    actual_sha256 = sha256(actual_bytes).hexdigest()
    actual_byte_size = len(actual_bytes)
    normalized_relative_path = relative_path.as_posix()

    export_file = SavedItemMirrorExportFile(
        mirror_export_id=export_id,
        source_kind=source_kind,
        source_id=source_id,
        file_role=file_role,
        relative_path=normalized_relative_path,
        expected_sha256=expected_sha256 or actual_sha256,
        actual_sha256=actual_sha256,
        expected_byte_size=expected_byte_size or actual_byte_size,
        actual_byte_size=actual_byte_size,
        verification_status="matched",
        error_summary=None,
    )
    session.add(export_file)
    session.flush()
    return export_file


def _relative_path(source_file: Path, target_file: Path) -> str:
    return Path(relpath(target_file, start=source_file.parent)).as_posix()


def _relative_to_root(root_dir: Path, target_file: Path) -> Path:
    return Path(relpath(target_file, start=root_dir))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
