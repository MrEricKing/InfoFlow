from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.db.models import (
    FeedItem,
    ItemState,
    SavedItem,
    SavedItemAsset,
    SavedItemCaptureRun,
    SavedItemCaptureToolCall,
    SavedItemMirrorExport,
    SavedItemMirrorExportFile,
    SavedSnapshot,
    Source,
    SourceItemOccurrence,
)
from app.schemas.knowledge import (
    SavedItemCaptureRead,
    SavedItemCaptureTitleMetadataRead,
    SavedItemCaptureToolCallRead,
    SavedItemDetailRead,
    SavedItemListRead,
    SavedItemLocalMirrorRead,
    SavedItemLocalMirrorPathsRead,
    SavedItemSnapshotStatusRead,
)
from app.schemas.reader import ReaderItemStateRead
from app.services.app_settings import resolve_knowledge_base_root
from app.services.insight_engine_artifacts import build_latest_occurrence_subquery, render_summary_html
from app.services.saved_item_insights import (
    ORIGIN_TYPE_FEED_SAVED,
    ORIGIN_TYPE_MANUAL_URL,
    SavedItemInsightReadBundle,
    load_saved_item_insight_read_bundles,
)

from app.services.knowledge_constants import (
    CANONICAL_SNAPSHOT_KIND,
    CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
    CAPTURE_STATUS_FAILED,
    CAPTURE_STATUS_PARTIAL_FAILED,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    ORIGINAL_PAGE_SNAPSHOT_KIND,
    PROCESSING_STATUS_NOT_APPLICABLE,
    UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
)
from app.services.knowledge_snapshot_assets import (
    _build_archive_artifact_reads,
    _build_asset_archive_artifact_read,
    _build_source_material_filename,
    _empty_asset_status,
    _extract_content_body,
    _extract_summary_body,
    _json_datetime_or_none,
    _json_string_or_none,
    _load_asset_status_map,
    _load_snapshot_map,
    _load_source_asset_map,
    _render_content_fragment,
    _rewrite_asset_refs_for_app,
    build_local_mirror_markdown_filename,
    build_local_mirror_singlefile_filename,
    build_saved_item_archive_dir_name,
    build_saved_item_archive_identity,
)


def list_saved_items(session: Session, *, limit: int | None = None, query: str | None = None) -> list[SavedItemListRead]:
    rows = _list_saved_item_rows(session, limit=limit, query=_normalize_query(query))
    saved_item_ids = [saved_item.id for saved_item, *_ in rows]
    snapshot_map = _load_snapshot_map(session, saved_item_ids)
    asset_status_map = _load_asset_status_map(session, saved_item_ids)
    source_asset_map = _load_source_asset_map(session, saved_item_ids)
    capture_run_map = _load_latest_capture_run_map(session, saved_item_ids)
    local_mirror_map = _load_latest_local_mirror_map(session, saved_item_ids)
    insight_bundle_map = load_saved_item_insight_read_bundles(session, saved_item_ids)
    knowledge_base_root = resolve_knowledge_base_root(session)
    local_mirror_file_count_map = _load_local_mirror_file_count_map(
        session,
        [export.id for export in local_mirror_map.values()],
    )
    return [
        _build_saved_item_list_read(
            saved_item,
            feed_item,
            item_state,
            occurrence,
            source,
            snapshot_map.get(saved_item.id, {}),
            asset_status_map.get(saved_item.id, _empty_asset_status()),
            source_asset_map.get(saved_item.id),
            capture_run_map.get(saved_item.id),
            local_mirror_map.get(saved_item.id),
            insight_bundle_map.get(saved_item.id),
            local_mirror_file_count_map,
            knowledge_base_root,
        )
        for saved_item, feed_item, item_state, occurrence, source in rows
    ]


def get_saved_item_detail(session: Session, saved_item_id: int) -> SavedItemDetailRead | None:
    rows = _list_saved_item_rows(session, saved_item_ids=[saved_item_id], limit=1)
    if not rows:
        return None

    saved_item, feed_item, item_state, occurrence, source = rows[0]
    snapshot_map = _load_snapshot_map(session, [saved_item.id]).get(saved_item.id, {})
    asset_status = _load_asset_status_map(session, [saved_item.id]).get(saved_item.id, _empty_asset_status())
    source_asset = _load_source_asset_map(session, [saved_item.id]).get(saved_item.id)
    capture_run = _load_latest_capture_run_map(session, [saved_item.id]).get(saved_item.id)
    local_mirror = _load_latest_local_mirror_map(session, [saved_item.id]).get(saved_item.id)
    insight_bundle = load_saved_item_insight_read_bundles(session, [saved_item.id]).get(saved_item.id)
    knowledge_base_root = resolve_knowledge_base_root(session)
    local_mirror_file_count_map = _load_local_mirror_file_count_map(
        session,
        [local_mirror.id] if local_mirror is not None else [],
    )
    list_read = _build_saved_item_list_read(
        saved_item,
        feed_item,
        item_state,
        occurrence,
        source,
        snapshot_map,
        asset_status,
        source_asset,
        capture_run,
        local_mirror,
        insight_bundle,
        local_mirror_file_count_map,
        knowledge_base_root,
    )
    canonical_snapshot = snapshot_map.get(CANONICAL_SNAPSHOT_KIND)
    content_html = _extract_content_body(canonical_snapshot.body) if canonical_snapshot is not None else None

    if content_html is None and feed_item is not None:
        content_html = _render_content_fragment(feed_item.content_text)
    content_html = _rewrite_asset_refs_for_app(content_html)

    capture_tool_calls = []
    if capture_run is not None:
        capture_tool_calls = _build_capture_tool_call_reads(
            _load_capture_tool_calls_map(session, [capture_run.id]).get(capture_run.id, [])
        )

    return SavedItemDetailRead(
        **list_read.model_dump(),
        content_html=content_html,
        capture_tool_calls=capture_tool_calls,
        archive_artifacts=_build_archive_artifact_reads(session, saved_item, snapshot_map),
        ai_generated_insight=insight_bundle.ai_generated_insight if insight_bundle is not None else None,
        curated_insight=insight_bundle.curated_insight if insight_bundle is not None else None,
    )


def _list_saved_item_rows(
    session: Session,
    *,
    saved_item_ids: list[int] | None = None,
    query: str | None = None,
    limit: int | None,
    include_removed: bool = False,
):
    active_filter = True if include_removed else SavedItem.removed_at.is_(None)
    feed_item_ids = list(
        session.scalars(
            select(SavedItem.feed_item_id).where(
                SavedItem.feed_item_id.is_not(None),
                SavedItem.id.in_(saved_item_ids) if saved_item_ids else True,
                active_filter,
            )
        )
    )
    latest_occurrences = build_latest_occurrence_subquery(feed_item_ids=feed_item_ids)

    statement = (
        select(SavedItem, FeedItem, ItemState, SourceItemOccurrence, Source)
        .outerjoin(FeedItem, FeedItem.id == SavedItem.feed_item_id)
        .outerjoin(ItemState, ItemState.feed_item_id == FeedItem.id)
        .outerjoin(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .outerjoin(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .outerjoin(Source, Source.id == SourceItemOccurrence.source_id)
        .where(active_filter)
        .order_by(SavedItem.saved_at.desc(), SavedItem.id.desc())
    )

    if saved_item_ids:
        statement = statement.where(SavedItem.id.in_(saved_item_ids))

    if query:
        canonical_snapshot = aliased(SavedSnapshot)
        latest_canonical_snapshot_ids = (
            select(
                SavedSnapshot.saved_item_id.label("saved_item_id"),
                func.max(SavedSnapshot.id).label("latest_id"),
            )
            .where(SavedSnapshot.snapshot_kind == CANONICAL_SNAPSHOT_KIND)
            .group_by(SavedSnapshot.saved_item_id)
            .subquery()
        )
        pattern = f"%{query}%"
        statement = (
            statement.outerjoin(
                latest_canonical_snapshot_ids,
                latest_canonical_snapshot_ids.c.saved_item_id == SavedItem.id,
            )
            .outerjoin(canonical_snapshot, canonical_snapshot.id == latest_canonical_snapshot_ids.c.latest_id)
            .where(
                or_(
                    SavedItem.title.ilike(pattern),
                    SavedItem.canonical_url.ilike(pattern),
                    SavedItem.user_note.ilike(pattern),
                    canonical_snapshot.body.ilike(pattern),
                )
            )
        )
    if limit is not None:
        statement = statement.limit(limit)

    return session.execute(statement).all()


def _normalize_query(query: str | None) -> str | None:
    if query is None:
        return None
    normalized = query.strip()
    return normalized or None


def _load_latest_capture_run_map(session: Session, saved_item_ids: list[int]) -> dict[int, SavedItemCaptureRun]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    latest_ids = (
        select(
            SavedItemCaptureRun.saved_item_id.label("saved_item_id"),
            func.max(SavedItemCaptureRun.id).label("latest_id"),
        )
        .where(SavedItemCaptureRun.saved_item_id.in_(unique_ids))
        .group_by(SavedItemCaptureRun.saved_item_id)
        .subquery()
    )
    runs = session.scalars(
        select(SavedItemCaptureRun).join(latest_ids, SavedItemCaptureRun.id == latest_ids.c.latest_id)
    ).all()
    return {run.saved_item_id: run for run in runs}


def _load_latest_local_mirror_map(session: Session, saved_item_ids: list[int]) -> dict[int, SavedItemMirrorExport]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    latest_ids = (
        select(
            SavedItemMirrorExport.saved_item_id.label("saved_item_id"),
            func.max(SavedItemMirrorExport.id).label("latest_id"),
        )
        .where(SavedItemMirrorExport.saved_item_id.in_(unique_ids))
        .group_by(SavedItemMirrorExport.saved_item_id)
        .subquery()
    )
    exports = session.scalars(
        select(SavedItemMirrorExport).join(latest_ids, SavedItemMirrorExport.id == latest_ids.c.latest_id)
    ).all()
    return {export.saved_item_id: export for export in exports}


def _load_local_mirror_file_count_map(session: Session, mirror_export_ids: list[int]) -> dict[int, int]:
    unique_ids = list(dict.fromkeys(mirror_export_ids))
    if not unique_ids:
        return {}

    rows = session.execute(
        select(
            SavedItemMirrorExportFile.mirror_export_id,
            func.count(SavedItemMirrorExportFile.id),
        )
        .where(SavedItemMirrorExportFile.mirror_export_id.in_(unique_ids))
        .group_by(SavedItemMirrorExportFile.mirror_export_id)
    ).all()
    return {mirror_export_id: int(file_count) for mirror_export_id, file_count in rows}


def _load_capture_tool_calls_map(
    session: Session,
    capture_run_ids: list[int],
) -> dict[int, list[SavedItemCaptureToolCall]]:
    unique_ids = list(dict.fromkeys(capture_run_ids))
    if not unique_ids:
        return {}

    tool_calls = list(
        session.scalars(
            select(SavedItemCaptureToolCall)
            .where(SavedItemCaptureToolCall.capture_run_id.in_(unique_ids))
            .order_by(SavedItemCaptureToolCall.capture_run_id.asc(), SavedItemCaptureToolCall.id.asc())
        )
    )
    result: dict[int, list[SavedItemCaptureToolCall]] = {}
    for tool_call in tool_calls:
        result.setdefault(tool_call.capture_run_id, []).append(tool_call)
    return result


def _build_saved_item_list_read(
    saved_item: SavedItem,
    feed_item: FeedItem | None,
    item_state: ItemState | None,
    occurrence: SourceItemOccurrence | None,
    source: Source | None,
    snapshots_by_kind: dict[str, SavedSnapshot],
    asset_status: dict[str, int | bool],
    source_asset: SavedItemAsset | None,
    capture_run: SavedItemCaptureRun | None,
    local_mirror: SavedItemMirrorExport | None,
    insight_bundle: SavedItemInsightReadBundle | None,
    local_mirror_file_count_map: dict[int, int],
    knowledge_base_root: Path,
) -> SavedItemListRead:
    canonical_snapshot = snapshots_by_kind.get(CANONICAL_SNAPSHOT_KIND)
    summary_text = _extract_summary_body(canonical_snapshot.body) if canonical_snapshot is not None else None

    if summary_text is None and feed_item is not None:
        summary_text = render_summary_html(feed_item.summary_text) or render_summary_html(feed_item.content_text)

    state = _build_state_projection(saved_item, item_state)
    original_url = (
        occurrence.original_url
        if occurrence is not None
        else (feed_item.original_url if feed_item is not None else saved_item.canonical_url)
    )
    source_url = source.url if source is not None else saved_item.source_app
    source_site_url = source.site_url if source is not None else saved_item.source_app
    source_kind = source.kind if source is not None else None
    source_folder_path = source.folder_path if source is not None else None
    published_at = (
        feed_item.published_at
        if feed_item is not None and feed_item.published_at is not None
        else (occurrence.published_at if occurrence is not None else None)
    )
    source_material_artifact = _build_asset_archive_artifact_read(
        saved_item,
        artifact_kind=UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
        asset=source_asset,
        logical_name=saved_item.original_filename,
    ) if source_asset is not None else None

    return SavedItemListRead(
        id=saved_item.id,
        feed_item_id=saved_item.feed_item_id,
        origin_type=saved_item.origin_type
        or (ORIGIN_TYPE_FEED_SAVED if saved_item.feed_item_id is not None else ORIGIN_TYPE_MANUAL_URL),
        ai_route=saved_item.ai_route,
        title=saved_item.title,
        author_name=feed_item.author_name if feed_item is not None else None,
        published_at=published_at,
        summary_text=summary_text,
        canonical_url=saved_item.canonical_url,
        original_url=original_url,
        source_id=source.id if source is not None else None,
        source_name=source.name if source is not None else saved_item.source_name,
        source_kind=source_kind,
        source_url=source_url,
        source_site_url=source_site_url,
        source_folder_path=source_folder_path,
        original_filename=saved_item.original_filename,
        mime_type=saved_item.mime_type,
        processing_status=saved_item.processing_status or PROCESSING_STATUS_NOT_APPLICABLE,
        processing_error=saved_item.processing_error,
        source_material_artifact=source_material_artifact,
        saved_at=saved_item.saved_at,
        user_note=saved_item.user_note,
        state=state,
        snapshots=SavedItemSnapshotStatusRead(
            has_canonical_snapshot=canonical_snapshot is not None,
            has_original_page_snapshot=ORIGINAL_PAGE_SNAPSHOT_KIND in snapshots_by_kind,
            has_singlefile_html=bool(asset_status["has_singlefile_html"]),
            has_crawl4ai_raw_markdown=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND in snapshots_by_kind,
            has_crawl4ai_fit_markdown=CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND in snapshots_by_kind,
            has_crawl4ai_fit_image_reinserted_markdown=(
                CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND in snapshots_by_kind
            ),
            image_asset_count=int(asset_status["image_asset_count"]),
        ),
        capture=_build_capture_read(capture_run, snapshots_by_kind.get(CAPTURE_PAGE_METADATA_SNAPSHOT_KIND)),
        local_mirror=_build_local_mirror_read(
            local_mirror,
            saved_item_title=saved_item.title,
            saved_item_archive_identity=build_saved_item_archive_identity(saved_item),
            source_material_filename=_build_source_material_filename(saved_item, source_asset),
            knowledge_base_root=knowledge_base_root,
            written_file_count=(
                local_mirror_file_count_map.get(local_mirror.id, 0) if local_mirror is not None else 0
            ),
        ),
        resolved_insight=insight_bundle.resolved_insight if insight_bundle is not None else None,
    )


def _build_state_projection(saved_item: SavedItem, item_state: ItemState | None) -> ReaderItemStateRead:
    active_saved_at = saved_item.saved_at if saved_item.removed_at is None else None
    if item_state is None:
        return ReaderItemStateRead(
            status="saved" if active_saved_at is not None else "unread",
            browsed_at=None,
            seen_at=None,
            read_full_at=None,
            saved_at=active_saved_at,
            not_interested_at=None,
            updated_at=saved_item.removed_at or saved_item.created_at,
        )

    return ReaderItemStateRead(
        status=_derive_item_status(item_state),
        browsed_at=item_state.browsed_at,
        seen_at=item_state.seen_at,
        read_full_at=item_state.read_full_at,
        saved_at=item_state.saved_at or active_saved_at,
        not_interested_at=item_state.not_interested_at,
        updated_at=item_state.updated_at,
    )


def _build_capture_read(
    capture_run: SavedItemCaptureRun | None,
    title_metadata_snapshot: SavedSnapshot | None,
) -> SavedItemCaptureRead | None:
    if capture_run is None:
        return None

    return SavedItemCaptureRead(
        status=capture_run.status,
        latest_run_id=capture_run.id,
        latest_error_summary=capture_run.error_summary,
        latest_started_at=capture_run.started_at,
        latest_finished_at=capture_run.finished_at,
        retryable=capture_run.status in {CAPTURE_STATUS_FAILED, CAPTURE_STATUS_PARTIAL_FAILED},
        title_metadata=_build_capture_title_metadata_read(title_metadata_snapshot),
    )


def _build_capture_title_metadata_read(
    title_metadata_snapshot: SavedSnapshot | None,
) -> SavedItemCaptureTitleMetadataRead | None:
    if title_metadata_snapshot is None:
        return None

    try:
        payload = json.loads(title_metadata_snapshot.body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    return SavedItemCaptureTitleMetadataRead(
        captured_at=_json_datetime_or_none(payload.get("captured_at")),
        captured_page_title=_json_string_or_none(payload.get("captured_page_title")),
        singlefile_title=_json_string_or_none(payload.get("singlefile_title")),
        crawl4ai_title=_json_string_or_none(payload.get("crawl4ai_title")),
        feed_title=_json_string_or_none(payload.get("feed_title")),
        title_source=_json_string_or_none(payload.get("title_source")),
        user_title_present=(
            payload.get("user_title_present") if isinstance(payload.get("user_title_present"), bool) else None
        ),
        title_updated=payload.get("title_updated") if isinstance(payload.get("title_updated"), bool) else None,
        saved_item_title_before=_json_string_or_none(payload.get("saved_item_title_before")),
        saved_item_title_after=_json_string_or_none(payload.get("saved_item_title_after")),
    )


def _build_capture_tool_call_reads(
    tool_calls: list[SavedItemCaptureToolCall],
) -> list[SavedItemCaptureToolCallRead]:
    return [
        SavedItemCaptureToolCallRead(
            id=tool_call.id,
            tool_name=tool_call.tool_name,
            tool_version=tool_call.tool_version,
            status=tool_call.status,
            exit_code=tool_call.exit_code,
            error_summary=tool_call.error_summary,
            started_at=tool_call.started_at,
            finished_at=tool_call.finished_at,
        )
        for tool_call in tool_calls
    ]


def _build_local_mirror_read(
    local_mirror: SavedItemMirrorExport | None,
    *,
    saved_item_title: str,
    saved_item_archive_identity: str,
    source_material_filename: str | None,
    knowledge_base_root: Path,
    written_file_count: int,
) -> SavedItemLocalMirrorRead | None:
    if local_mirror is None:
        return None

    return SavedItemLocalMirrorRead(
        status=local_mirror.status,
        latest_export_id=local_mirror.id,
        latest_error_summary=local_mirror.error_summary,
        latest_started_at=local_mirror.started_at,
        latest_finished_at=local_mirror.finished_at,
        source_capture_run_id=local_mirror.source_capture_run_id,
        written_file_count=written_file_count,
        paths=_build_local_mirror_paths_read(
            knowledge_base_root,
            saved_item_title=saved_item_title,
            saved_item_archive_identity=saved_item_archive_identity,
            source_material_filename=source_material_filename,
        ),
    )


def _build_local_mirror_paths_read(
    knowledge_base_root: Path,
    *,
    saved_item_title: str,
    saved_item_archive_identity: str,
    source_material_filename: str | None,
) -> SavedItemLocalMirrorPathsRead:
    article_dir = knowledge_base_root / build_saved_item_archive_dir_name(saved_item_title, saved_item_archive_identity)
    assets_dir = article_dir / "assets"
    metadata_dir = article_dir / ".infoflow"
    return SavedItemLocalMirrorPathsRead(
        knowledge_base_root=str(knowledge_base_root),
        article_dir=str(article_dir),
        raw_markdown=str(article_dir / build_local_mirror_markdown_filename(saved_item_title, "raw")),
        fit_markdown=str(article_dir / build_local_mirror_markdown_filename(saved_item_title, "fit")),
        fit_image_reinserted_markdown=str(
            article_dir / build_local_mirror_markdown_filename(saved_item_title, "fit-images")
        ),
        uploaded_markdown=str(article_dir / build_local_mirror_markdown_filename(saved_item_title, "uploaded")),
        manifest=str(metadata_dir / "manifest.json"),
        markdown_assets_dir=str(assets_dir),
        singlefile_html=str(article_dir / build_local_mirror_singlefile_filename(saved_item_title)),
        source_material=str(article_dir / source_material_filename) if source_material_filename else None,
    )


def _derive_item_status(item_state: ItemState) -> str:
    if item_state.not_interested_at is not None:
        return "not_interested"
    if item_state.saved_at is not None:
        return "saved"
    if item_state.read_full_at is not None:
        return "read_full"
    if item_state.seen_at is not None:
        return "seen"
    if item_state.browsed_at is not None:
        return "browsed"
    return "unread"
