from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    FeedItem,
    ItemState,
    SavedItem,
    SavedSnapshot,
    Source,
    SourceItemOccurrence,
)
from app.schemas.knowledge import (
    SavedItemDetailRead,
    SavedItemRemoveResponse,
    SavedItemSnapshotStatusRead,
)
from app.schemas.reader import BehaviorViewContext
from app.services.app_settings import AI_PROFILE_GENERAL
from app.services.behavior_events import (
    EVENT_SAVED,
    EVENT_UNSAVED,
    SOURCE_KNOWLEDGE_SAVE,
    SOURCE_KNOWLEDGE_UNSAVE,
    record_feed_item_behavior_event,
    with_behavior_event_context,
)
from app.services.insight_engine_artifacts import build_latest_occurrence_subquery
from app.services.insight_routing_context import build_routing_context_snapshot
from app.services.homepage_read_model import rebuild_homepage_read_models_for_feed_items
from app.services.saved_item_insights import (
    ORIGIN_TYPE_FEED_SAVED,
    normalize_saved_item_ai_route,
)

from app.services.knowledge_constants import (
    CANONICAL_SNAPSHOT_KIND,
    CAPTURE_STATUS_PENDING,
    CAPTURE_TRIGGER_FEED_SAVED,
    DEFAULT_CAPTURE_TARGETS,
)
from app.services.knowledge_capture import (
    _capture_run_has_tool_calls,
    _create_capture_run,
    _export_after_capture,
    _find_latest_capture_run,
    _now_utc,
)
from app.services.knowledge_read_model import _build_state_projection, _derive_item_status, get_saved_item_detail
from app.services.knowledge_snapshot_assets import (
    _build_snapshot_status_read,
    _find_latest_snapshot,
    _render_content_fragment,
    _render_summary_fragment,
)


@dataclass(frozen=True, slots=True)
class SaveFeedItemResult:
    item_state: ItemState
    saved_item: SavedItem
    snapshots: SavedItemSnapshotStatusRead
    pending_capture_run_id: int | None


@dataclass(frozen=True, slots=True)
class _SavedItemRestoreResult:
    saved_item: SavedItem
    restored_from_removed: bool


def save_feed_item(
    session: Session,
    feed_item_id: int,
    *,
    view_context: BehaviorViewContext | None = None,
) -> SaveFeedItemResult | None:
    context = _get_save_context(session, feed_item_id)
    if context is None:
        return None

    feed_item, item_state, occurrence, source = context
    now = _now_utc()
    saved_item_result = _get_or_create_saved_item(session, feed_item, occurrence, source, now=now)
    saved_item = saved_item_result.saved_item

    canonical_snapshot = _find_latest_snapshot(session, saved_item.id, CANONICAL_SNAPSHOT_KIND)
    if canonical_snapshot is None:
        canonical_snapshot = SavedSnapshot(
            saved_item_id=saved_item.id,
            capture_run_id=None,
            snapshot_kind=CANONICAL_SNAPSHOT_KIND,
            mime_type="text/html",
            body=_build_canonical_snapshot_document(feed_item, occurrence, source, saved_at=saved_item.saved_at),
        )
        session.add(canonical_snapshot)
        session.flush()

    previous_status = item_state.status
    previous_saved_at = item_state.saved_at
    if item_state.saved_at is None:
        item_state.saved_at = saved_item.saved_at
    item_state.status = _derive_item_status(item_state)
    session.add(item_state)
    record_feed_item_behavior_event(
        session,
        feed_item_id=feed_item.id,
        event_type=EVENT_SAVED,
        source=SOURCE_KNOWLEDGE_SAVE,
        event_payload_json=with_behavior_event_context(
            {
                "saved_item_id": saved_item.id,
                "changed": previous_saved_at is None or saved_item_result.restored_from_removed,
                "restored_from_removed": saved_item_result.restored_from_removed,
                "previous_status": previous_status,
                "status": item_state.status,
            },
            view_context=view_context,
            routing_context=build_routing_context_snapshot(session, feed_item.id),
        ),
        created_at=now,
    )
    session.flush()

    pending_capture_run_id: int | None = None
    latest_capture_run = _find_latest_capture_run(session, saved_item.id)
    if latest_capture_run is None:
        capture_run = _create_capture_run(
            session,
            saved_item_id=saved_item.id,
            trigger_kind=CAPTURE_TRIGGER_FEED_SAVED,
            retry_of_run_id=None,
            requested_targets=DEFAULT_CAPTURE_TARGETS,
        )
        pending_capture_run_id = capture_run.id
    elif saved_item_result.restored_from_removed:
        _export_after_capture(session, saved_item_id=saved_item.id, capture_run_id=latest_capture_run.id)
    elif latest_capture_run.status == CAPTURE_STATUS_PENDING and not _capture_run_has_tool_calls(session, latest_capture_run.id):
        pending_capture_run_id = latest_capture_run.id

    session.refresh(item_state)
    session.refresh(saved_item)
    rebuild_homepage_read_models_for_feed_items(session, [feed_item.id])

    return SaveFeedItemResult(
        item_state=item_state,
        saved_item=saved_item,
        snapshots=_build_snapshot_status_read(session, saved_item.id),
        pending_capture_run_id=pending_capture_run_id,
    )


def export_saved_item_local_mirror(session: Session, saved_item_id: int) -> SavedItemDetailRead | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None

    from app.services.knowledge_mirror import MIRROR_TRIGGER_MANUAL_EXPORT, export_saved_item_to_local_mirror

    export_saved_item_to_local_mirror(
        session,
        saved_item_id,
        trigger_kind=MIRROR_TRIGGER_MANUAL_EXPORT,
    )
    return get_saved_item_detail(session, saved_item_id)


def remove_saved_item(session: Session, saved_item_id: int) -> SavedItemRemoveResponse | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None:
        return None
    return _soft_remove_saved_item(session, saved_item)


def unmark_feed_item_saved(
    session: Session,
    feed_item_id: int,
    *,
    view_context: BehaviorViewContext | None = None,
) -> tuple[ItemState, int | None] | None:
    if session.get(FeedItem, feed_item_id) is None:
        return None

    item_state = _get_or_create_feed_item_state(session, feed_item_id)
    saved_item = session.scalar(select(SavedItem).where(SavedItem.feed_item_id == feed_item_id))
    if saved_item is None:
        previous_status = item_state.status
        changed = _clear_saved_timestamp(session, item_state)
        record_feed_item_behavior_event(
            session,
            feed_item_id=feed_item_id,
            event_type=EVENT_UNSAVED,
            source=SOURCE_KNOWLEDGE_UNSAVE,
            event_payload_json=with_behavior_event_context(
                {
                    "saved_item_id": None,
                    "changed": changed,
                    "previous_status": previous_status,
                    "status": item_state.status,
                },
                view_context=view_context,
                routing_context=build_routing_context_snapshot(session, feed_item_id),
            ),
        )
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, [feed_item_id])
        return item_state, None

    response = _soft_remove_saved_item(session, saved_item, item_state=item_state, view_context=view_context)
    session.refresh(item_state)
    return item_state, response.saved_item_id


def update_saved_item_note(session: Session, saved_item_id: int, *, user_note: str | None) -> SavedItemDetailRead | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None

    saved_item.user_note = user_note
    session.add(saved_item)
    session.flush()
    session.refresh(saved_item)
    return get_saved_item_detail(session, saved_item_id)


def _soft_remove_saved_item(
    session: Session,
    saved_item: SavedItem,
    *,
    item_state: ItemState | None = None,
    view_context: BehaviorViewContext | None = None,
) -> SavedItemRemoveResponse:
    first_removal = saved_item.removed_at is None
    if first_removal:
        saved_item.removed_at = _now_utc()
        session.add(saved_item)
        session.flush()

    if saved_item.feed_item_id is not None:
        item_state = item_state or session.scalar(
            select(ItemState).where(ItemState.feed_item_id == saved_item.feed_item_id)
        )
        if item_state is not None:
            previous_status = item_state.status
            changed = _clear_saved_timestamp(session, item_state)
            record_feed_item_behavior_event(
                session,
                feed_item_id=saved_item.feed_item_id,
                event_type=EVENT_UNSAVED,
                source=SOURCE_KNOWLEDGE_UNSAVE,
                event_payload_json=with_behavior_event_context(
                    {
                        "saved_item_id": saved_item.id,
                        "changed": first_removal or changed,
                        "first_removal": first_removal,
                        "previous_status": previous_status,
                        "status": item_state.status,
                    },
                    view_context=view_context,
                    routing_context=build_routing_context_snapshot(session, saved_item.feed_item_id),
                ),
                created_at=saved_item.removed_at if first_removal and saved_item.removed_at is not None else _now_utc(),
            )

    if first_removal:
        _archive_removed_saved_item_local_mirror(session, saved_item.id)

    session.refresh(saved_item)
    if item_state is not None:
        session.refresh(item_state)
        if saved_item.feed_item_id is not None:
            rebuild_homepage_read_models_for_feed_items(session, [saved_item.feed_item_id])

    return SavedItemRemoveResponse(
        saved_item_id=saved_item.id,
        feed_item_id=saved_item.feed_item_id,
        removed_at=saved_item.removed_at or _now_utc(),
        state=(
            _build_state_projection(saved_item, item_state)
            if saved_item.feed_item_id is not None
            else None
        ),
    )


def _clear_saved_timestamp(session: Session, item_state: ItemState) -> bool:
    changed = item_state.saved_at is not None
    item_state.saved_at = None
    item_state.status = _derive_item_status(item_state)
    session.add(item_state)
    session.flush()
    return changed


def _get_or_create_feed_item_state(session: Session, feed_item_id: int) -> ItemState:
    item_state = session.scalar(select(ItemState).where(ItemState.feed_item_id == feed_item_id))
    if item_state is not None:
        return item_state

    item_state = ItemState(feed_item_id=feed_item_id, status="unread")
    session.add(item_state)
    session.flush()
    return item_state


def _archive_removed_saved_item_local_mirror(session: Session, saved_item_id: int) -> None:
    from app.services.knowledge_mirror import archive_removed_saved_item_local_mirror

    archive_removed_saved_item_local_mirror(session, saved_item_id)


def _get_save_context(session: Session, feed_item_id: int):
    latest_occurrences = build_latest_occurrence_subquery(feed_item_ids=[feed_item_id])
    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .where(FeedItem.id == feed_item_id)
    )
    return session.execute(statement).one_or_none()


def _get_or_create_saved_item(
    session: Session,
    feed_item: FeedItem,
    occurrence: SourceItemOccurrence,
    source: Source,
    *,
    now: datetime,
) -> _SavedItemRestoreResult:
    saved_item = session.scalar(select(SavedItem).where(SavedItem.feed_item_id == feed_item.id))
    if saved_item is not None:
        restored_from_removed = saved_item.removed_at is not None
        if restored_from_removed:
            saved_item.removed_at = None
            saved_item.saved_at = now
            saved_item.origin_type = saved_item.origin_type or ORIGIN_TYPE_FEED_SAVED
            saved_item.ai_route = saved_item.ai_route or normalize_saved_item_ai_route(
                source.ai_route,
                default=AI_PROFILE_GENERAL,
            )
            session.add(saved_item)
            session.flush()
        return _SavedItemRestoreResult(saved_item=saved_item, restored_from_removed=restored_from_removed)

    saved_item = SavedItem(
        feed_item_id=feed_item.id,
        origin_type=ORIGIN_TYPE_FEED_SAVED,
        ai_route=normalize_saved_item_ai_route(source.ai_route, default=AI_PROFILE_GENERAL),
        title=feed_item.title,
        canonical_url=feed_item.canonical_url or occurrence.canonical_url or occurrence.original_url,
        source_name=source.name,
        source_app=source.site_url or source.url,
        user_note=None,
        saved_at=now,
    )
    session.add(saved_item)
    session.flush()
    return _SavedItemRestoreResult(saved_item=saved_item, restored_from_removed=False)


def _build_canonical_snapshot_document(
    feed_item: FeedItem,
    occurrence: SourceItemOccurrence,
    source: Source,
    *,
    saved_at: datetime,
) -> str:
    summary_html = _render_summary_fragment(feed_item.summary_text)
    content_html = _render_content_fragment(feed_item.content_text)
    summary_section = ""
    if summary_html is not None:
        summary_section = (
            "<section data-role=\"summary\">"
            "<h2>Summary</h2>"
            f"<div data-role=\"summary-body\">{summary_html}</div>"
            "</section>"
        )

    content_section = ""
    if content_html is not None:
        content_section = (
            "<section data-role=\"content\">"
            "<h2>Content</h2>"
            f"<div data-role=\"content-body\">{content_html}</div>"
            "</section>"
        )

    saved_at_text = saved_at.astimezone(timezone.utc).isoformat()
    published_at = feed_item.published_at or occurrence.published_at
    published_at_text = published_at.astimezone(timezone.utc).isoformat() if published_at is not None else ""

    return "".join(
        [
            "<!DOCTYPE html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\" />",
            f"<title>{escape(feed_item.title)}</title>",
            "</head>",
            "<body>",
            f"<article data-snapshot-kind=\"{CANONICAL_SNAPSHOT_KIND}\">",
            "<header>",
            f"<h1>{escape(feed_item.title)}</h1>",
            f"<p>Saved from {escape(source.name)} at {escape(saved_at_text)}</p>",
            f"<p><a href=\"{escape(occurrence.original_url)}\">Open original</a></p>",
            f"<p><a href=\"{escape(feed_item.canonical_url or occurrence.original_url)}\">Canonical URL</a></p>",
            f"<p>Published at {escape(published_at_text)}</p>" if published_at_text else "",
            "</header>",
            summary_section,
            content_section,
            "</article>",
            "</body>",
            "</html>",
        ]
    )
