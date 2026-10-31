from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, FeedItemStructuredFeedback, ItemState, Source, SourceFetch, SourceItemOccurrence
from app.schemas.reader import (
    BehaviorViewContext,
    ReaderBulkMarkSeenResponse,
    ReaderDetailRead,
    ReaderInsightFeedbackResponse,
    ReaderItemStateRead,
    ReaderListItemRead,
    ReaderStructuredFeedbackInput,
)
from app.services.insight_engine_artifact_builder import build_missing_feed_item_insight_artifacts_for_feed_items
from app.services.behavior_events import (
    EVENT_CARD_OPENED,
    EVENT_INSIGHT_FEEDBACK_CLEARED,
    EVENT_INSIGHT_FEEDBACK_SAVED,
    EVENT_MARKED_SEEN_BULK,
    EVENT_MARKED_SEEN_FROM_LIST,
    EVENT_AUTO_LOW_PRIORITY_CLEARED,
    EVENT_NOT_INTERESTED,
    EVENT_NOT_INTERESTED_CLEARED,
    EVENT_OPENED_ORIGINAL_LINK,
    EVENT_READ_FULL_CLEARED,
    EVENT_READ_FULL_IN_APP,
    EVENT_SEEN_CLEARED,
    EVENT_SAVED,
    EVENT_STRUCTURED_FEEDBACK_CLEARED,
    EVENT_STRUCTURED_FEEDBACK_SAVED,
    EVENT_UNSAVED,
    SOURCE_READER_BULK_API,
    SOURCE_READER_FEEDBACK_API,
    SOURCE_READER_PRIORITY_API,
    SOURCE_READER_STATE_API,
    record_feed_item_behavior_event,
    with_behavior_event_context,
)
from app.services.feed_content_extraction import extract_feed_item_content, read_content_extraction_diagnostics
from app.services.homepage_read_model import rebuild_homepage_read_models_for_feed_items
from app.services.insight_routing_context import build_routing_context_snapshot, build_routing_context_snapshots
from app.services.structured_feedback import (
    apply_structured_feedback,
    load_structured_feedback_map,
    normalize_structured_feedback_input,
    serialize_structured_feedback,
    serialize_structured_feedback_json,
    structured_feedback_row_is_empty,
)


ReaderStateAction = Literal["browsed", "seen", "read_full", "saved", "not_interested"]
ReaderStateClearAction = Literal["seen", "read_full", "saved", "not_interested"]


def list_reader_items(
    session: Session,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[ReaderListItemRead]:
    latest_occurrences = _build_latest_occurrence_subquery()
    sort_timestamp = func.coalesce(
        FeedItem.published_at,
        SourceItemOccurrence.published_at,
        SourceItemOccurrence.created_at,
    )

    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .order_by(sort_timestamp.desc(), FeedItem.id.desc())
    )

    if status is not None:
        statement = statement.where(ItemState.status == status)
    if limit is not None:
        statement = statement.limit(limit)

    rows = session.execute(statement).all()
    structured_feedback_map = load_structured_feedback_map(
        session,
        [feed_item.id for feed_item, _, _, _ in rows],
    )
    return [
        _build_reader_list_item(
            feed_item,
            item_state,
            occurrence,
            source,
            structured_feedback_map.get(feed_item.id),
        )
        for feed_item, item_state, occurrence, source in rows
    ]


def get_reader_item_detail(session: Session, item_id: int) -> ReaderDetailRead | None:
    latest_occurrences = _build_latest_occurrence_subquery(feed_item_ids=[item_id])
    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .where(FeedItem.id == item_id)
    )

    row = session.execute(statement).one_or_none()
    if row is None:
        return None

    feed_item, item_state, occurrence, source = row
    structured_feedback = session.scalar(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == feed_item.id)
    )
    return ReaderDetailRead(
        **_build_reader_list_item(
            feed_item,
            item_state,
            occurrence,
            source,
            structured_feedback,
        ).model_dump(),
        feed_content_text=feed_item.feed_content_text,
        content_text=feed_item.content_text,
    )


def extract_reader_item_content(session: Session, item_id: int) -> ReaderDetailRead | None:
    feed_item = session.get(FeedItem, item_id)
    if feed_item is None:
        return None

    result = extract_feed_item_content(feed_item, force=True, record_debug=True)
    session.add(feed_item)
    session.flush()
    if result.succeeded:
        try:
            build_missing_feed_item_insight_artifacts_for_feed_items(
                session,
                feed_item_ids=[feed_item.id],
                scope=f"manual_extract:{feed_item.id}",
            )
        except Exception:
            pass
        session.flush()

    rebuild_homepage_read_models_for_feed_items(session, [feed_item.id])
    return get_reader_item_detail(session, item_id)


def update_reader_item_insight_feedback(
    session: Session,
    item_id: int,
    insight_feedback_text: str | None,
    *,
    insight_feedback_text_provided: bool = True,
    structured_feedback: ReaderStructuredFeedbackInput | None = None,
    structured_feedback_provided: bool = False,
    view_context: BehaviorViewContext | None = None,
) -> ReaderInsightFeedbackResponse | None:
    feed_item = session.get(FeedItem, item_id)
    if feed_item is None:
        return None

    normalized_feedback = _normalize_insight_feedback(feed_item.insight_feedback_text)
    if insight_feedback_text_provided:
        previous_feedback = normalized_feedback
        normalized_feedback = _normalize_insight_feedback(insight_feedback_text)
        feed_item.insight_feedback_text = normalized_feedback
        session.add(feed_item)
        record_feed_item_behavior_event(
            session,
            feed_item_id=item_id,
            event_type=EVENT_INSIGHT_FEEDBACK_CLEARED if normalized_feedback is None else EVENT_INSIGHT_FEEDBACK_SAVED,
            source=SOURCE_READER_FEEDBACK_API,
            event_payload_json=_build_contextual_event_payload(
                session,
                item_id,
                view_context=view_context,
                event_payload_json={
                    "changed": previous_feedback != normalized_feedback,
                    "previous_feedback_present": previous_feedback is not None,
                    "feedback_text": normalized_feedback,
                },
            ),
        )

    structured_row = session.scalar(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == item_id)
    )
    if structured_feedback_provided:
        previous_routing_snapshot = build_routing_context_snapshot(session, item_id)
        previous_structured_feedback = serialize_structured_feedback_json(structured_row)
        if structured_feedback is None:
            normalized_structured_feedback = None
        else:
            normalized_structured_feedback = normalize_structured_feedback_input(
                structured_feedback,
                session=session,
                existing_row=structured_row,
            )
            if normalized_structured_feedback.is_empty:
                normalized_structured_feedback = None

        if normalized_structured_feedback is None:
            if structured_row is not None:
                session.delete(structured_row)
                structured_row = None
            current_structured_feedback = None
        else:
            if structured_row is None:
                structured_row = FeedItemStructuredFeedback(feed_item_id=item_id)
            apply_structured_feedback(structured_row, normalized_structured_feedback)
            session.add(structured_row)
            session.flush()
            if structured_feedback_row_is_empty(structured_row):
                session.delete(structured_row)
                structured_row = None
                current_structured_feedback = None
            else:
                session.refresh(structured_row)
                current_structured_feedback = serialize_structured_feedback_json(structured_row)
        session.flush()
        current_routing_snapshot = build_routing_context_snapshot(session, item_id)

        if previous_structured_feedback != current_structured_feedback:
            record_feed_item_behavior_event(
                session,
                feed_item_id=item_id,
                event_type=(
                    EVENT_STRUCTURED_FEEDBACK_CLEARED
                    if normalized_structured_feedback is None
                    else EVENT_STRUCTURED_FEEDBACK_SAVED
                ),
                source=SOURCE_READER_FEEDBACK_API,
                event_payload_json=with_behavior_event_context(
                    {
                        "changed": True,
                        "previous_structured_feedback_present": previous_structured_feedback is not None,
                        "structured_feedback": current_structured_feedback,
                        "prior_routing": previous_routing_snapshot,
                        "corrected_routing": current_routing_snapshot,
                    },
                    view_context=view_context,
                    routing_context=current_routing_snapshot,
                ),
            )

    session.flush()
    rebuild_homepage_read_models_for_feed_items(session, [item_id])
    return ReaderInsightFeedbackResponse(
        item_id=item_id,
        insight_feedback_text=normalized_feedback,
        structured_feedback=serialize_structured_feedback(structured_row),
    )


def clear_reader_item_auto_low_priority(
    session: Session,
    item_id: int,
    *,
    view_context: BehaviorViewContext | None = None,
) -> ReaderInsightFeedbackResponse | None:
    feed_item = session.get(FeedItem, item_id)
    if feed_item is None:
        return None

    structured_row = session.scalar(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == item_id)
    )
    previous_structured_feedback = serialize_structured_feedback_json(structured_row)
    if structured_row is None:
        structured_row = FeedItemStructuredFeedback(feed_item_id=item_id)

    issue_markers = list(structured_row.issue_markers_json or [])
    if "wrong_priority" not in issue_markers:
        issue_markers.append("wrong_priority")
    structured_row.priority_correction = "too_low"
    structured_row.issue_markers_json = issue_markers
    session.add(structured_row)
    session.flush()
    session.refresh(structured_row)
    current_structured_feedback = serialize_structured_feedback_json(structured_row)

    record_feed_item_behavior_event(
        session,
        feed_item_id=item_id,
        event_type=EVENT_AUTO_LOW_PRIORITY_CLEARED,
        source=SOURCE_READER_PRIORITY_API,
        event_payload_json=_build_contextual_event_payload(
            session,
            item_id,
            view_context=view_context,
            event_payload_json={
                "changed": previous_structured_feedback != current_structured_feedback,
                "previous_structured_feedback_present": previous_structured_feedback is not None,
                "structured_feedback": current_structured_feedback,
                "priority_correction": "too_low",
                "issue_marker": "wrong_priority",
            },
        ),
    )
    rebuild_homepage_read_models_for_feed_items(session, [item_id])
    return ReaderInsightFeedbackResponse(
        item_id=item_id,
        insight_feedback_text=feed_item.insight_feedback_text,
        structured_feedback=serialize_structured_feedback(structured_row),
    )


def mark_item_state(
    session: Session,
    item_id: int,
    action: ReaderStateAction,
    *,
    view_context: BehaviorViewContext | None = None,
) -> ItemState | None:
    item_state = _get_or_create_item_state(session, item_id)
    if item_state is None:
        return None

    now = _now_utc()
    previous_status = item_state.status
    timestamp_field = _timestamp_field_for_action(action)
    previous_timestamp = getattr(item_state, timestamp_field)
    changed = previous_timestamp is None
    if changed:
        setattr(item_state, timestamp_field, now)

    item_state.status = derive_item_status(item_state)
    session.add(item_state)
    record_feed_item_behavior_event(
        session,
        feed_item_id=item_id,
        event_type=_event_type_for_action(action),
        source=SOURCE_READER_STATE_API,
        event_payload_json=_build_contextual_event_payload(
            session,
            item_id,
            view_context=view_context,
            event_payload_json={
                "action": action,
                "changed": changed,
                "previous_status": previous_status,
                "status": item_state.status,
            },
        ),
        created_at=now,
    )
    session.flush()
    session.refresh(item_state)
    rebuild_homepage_read_models_for_feed_items(session, [item_id])
    return item_state


def clear_item_state(
    session: Session,
    item_id: int,
    action: ReaderStateClearAction,
    *,
    view_context: BehaviorViewContext | None = None,
) -> ItemState | None:
    item_state = _get_or_create_item_state(session, item_id)
    if item_state is None:
        return None

    now = _now_utc()
    previous_status = item_state.status
    timestamp_field = _timestamp_field_for_action(action)
    previous_timestamp = getattr(item_state, timestamp_field)
    changed = previous_timestamp is not None
    setattr(item_state, timestamp_field, None)
    item_state.status = derive_item_status(item_state)
    session.add(item_state)
    record_feed_item_behavior_event(
        session,
        feed_item_id=item_id,
        event_type=_event_type_for_clear_action(action),
        source=SOURCE_READER_STATE_API,
        event_payload_json=_build_contextual_event_payload(
            session,
            item_id,
            view_context=view_context,
            event_payload_json={
                "action": f"unmark_{action}",
                "changed": changed,
                "previous_status": previous_status,
                "status": item_state.status,
            },
        ),
        created_at=now,
    )
    session.flush()
    session.refresh(item_state)
    rebuild_homepage_read_models_for_feed_items(session, [item_id])
    return item_state


def mark_item_opened_original(
    session: Session,
    item_id: int,
    *,
    view_context: BehaviorViewContext | None = None,
) -> ItemState | None:
    item_state = _get_or_create_item_state(session, item_id)
    if item_state is None:
        return None

    feed_item = session.get(FeedItem, item_id)
    if feed_item is None:
        return None

    now = _now_utc()
    previous_status = item_state.status
    previous_timestamp = item_state.read_full_at
    changed = previous_timestamp is None
    if changed:
        item_state.read_full_at = now

    item_state.status = derive_item_status(item_state)
    session.add(item_state)
    record_feed_item_behavior_event(
        session,
        feed_item_id=item_id,
        event_type=EVENT_OPENED_ORIGINAL_LINK,
        source=SOURCE_READER_STATE_API,
        event_payload_json=_build_contextual_event_payload(
            session,
            item_id,
            view_context=view_context,
            event_payload_json={
                "action": "open_original",
                "changed": changed,
                "read_full_at_set": changed,
                "previous_status": previous_status,
                "status": item_state.status,
                "original_url": _latest_original_url_for_item(session, item_id),
                "content_text_source": feed_item.content_text_source,
                "content_extraction_status": feed_item.content_extraction_status,
                "content_extraction_error": feed_item.content_extraction_error,
                "content_extraction_visible_char_count": read_content_extraction_diagnostics(feed_item).visible_char_count,
            },
        ),
        created_at=now,
    )
    session.flush()
    session.refresh(item_state)
    rebuild_homepage_read_models_for_feed_items(session, [item_id])
    return item_state


def mark_latest_source_items_seen(
    session: Session,
    source_id: int,
    *,
    view_context: BehaviorViewContext | None = None,
) -> ReaderBulkMarkSeenResponse | None:
    if session.get(Source, source_id) is None:
        return None

    latest_occurrences = _build_latest_occurrence_subquery()
    statement = (
        select(ItemState)
        .join(FeedItem, FeedItem.id == ItemState.feed_item_id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .where(SourceItemOccurrence.source_id == source_id)
    )
    item_states = list(session.scalars(statement))
    routing_contexts = build_routing_context_snapshots(session, [item_state.feed_item_id for item_state in item_states])
    timestamp = _now_utc()
    updated_count = 0

    for item_state in item_states:
        previous_status = item_state.status
        if item_state.seen_at is None:
            item_state.seen_at = timestamp
            changed = True
            updated_count += 1
        else:
            changed = False
        item_state.status = derive_item_status(item_state)
        session.add(item_state)
        record_feed_item_behavior_event(
            session,
            feed_item_id=item_state.feed_item_id,
            event_type=EVENT_MARKED_SEEN_BULK,
            source=SOURCE_READER_BULK_API,
            event_payload_json=with_behavior_event_context(
                {
                    "source_id": source_id,
                    "changed": changed,
                    "previous_status": previous_status,
                    "status": item_state.status,
                },
                view_context=view_context,
                routing_context=routing_contexts.get(item_state.feed_item_id),
            ),
            created_at=timestamp,
        )

    session.flush()
    rebuild_homepage_read_models_for_feed_items(session, [item_state.feed_item_id for item_state in item_states])
    return ReaderBulkMarkSeenResponse(
        source_id=source_id,
        matched_item_count=len(item_states),
        updated_item_count=updated_count,
    )


def derive_item_status(item_state: ItemState) -> str:
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


def build_reader_item_state(item_state: ItemState) -> ReaderItemStateRead:
    return ReaderItemStateRead(
        status=item_state.status,
        browsed_at=item_state.browsed_at,
        seen_at=item_state.seen_at,
        read_full_at=item_state.read_full_at,
        saved_at=item_state.saved_at,
        not_interested_at=item_state.not_interested_at,
        updated_at=item_state.updated_at,
    )


def _build_reader_list_item(
    feed_item: FeedItem,
    item_state: ItemState,
    occurrence: SourceItemOccurrence,
    source: Source,
    structured_feedback: FeedItemStructuredFeedback | None,
) -> ReaderListItemRead:
    extraction_diagnostics = read_content_extraction_diagnostics(feed_item)
    return ReaderListItemRead(
        id=feed_item.id,
        title=feed_item.title,
        author_name=feed_item.author_name,
        published_at=feed_item.published_at or occurrence.published_at,
        summary_text=feed_item.summary_text,
        original_url=occurrence.original_url,
        canonical_url=feed_item.canonical_url,
        source_id=source.id,
        source_name=source.name,
        source_kind=source.kind,
        source_url=source.url,
        source_site_url=source.site_url,
        source_folder_path=source.folder_path,
        content_text_source=feed_item.content_text_source,
        content_extraction_status=feed_item.content_extraction_status,
        content_extraction_attempted_at=feed_item.content_extraction_attempted_at,
        content_extracted_at=feed_item.content_extracted_at,
        content_extraction_error=feed_item.content_extraction_error,
        content_extraction_method=extraction_diagnostics.method,
        content_extraction_final_url=extraction_diagnostics.final_url,
        content_extraction_visible_char_count=extraction_diagnostics.visible_char_count,
        insight_feedback_text=feed_item.insight_feedback_text,
        structured_feedback=serialize_structured_feedback(structured_feedback),
        state=build_reader_item_state(item_state),
    )


def _build_latest_occurrence_subquery(feed_item_ids: list[int] | None = None):
    occurrence_sort_timestamp = func.coalesce(SourceFetch.fetch_started_at, SourceItemOccurrence.created_at)
    statement = (
        select(
            SourceItemOccurrence.id.label("occurrence_id"),
            SourceItemOccurrence.feed_item_id.label("feed_item_id"),
            func.row_number()
            .over(
                partition_by=SourceItemOccurrence.feed_item_id,
                order_by=(
                    occurrence_sort_timestamp.desc(),
                    SourceItemOccurrence.created_at.desc(),
                    SourceItemOccurrence.id.desc(),
                ),
            )
            .label("occurrence_rank"),
        )
        .select_from(SourceItemOccurrence)
        .outerjoin(SourceFetch, SourceFetch.id == SourceItemOccurrence.source_fetch_id)
    )

    if feed_item_ids:
        statement = statement.where(SourceItemOccurrence.feed_item_id.in_(feed_item_ids))

    return statement.subquery()


def _timestamp_field_for_action(action: ReaderStateAction | ReaderStateClearAction) -> str:
    mapping = {
        "browsed": "browsed_at",
        "seen": "seen_at",
        "read_full": "read_full_at",
        "saved": "saved_at",
        "not_interested": "not_interested_at",
    }
    return mapping[action]


def _event_type_for_action(action: ReaderStateAction) -> str:
    mapping = {
        "browsed": EVENT_CARD_OPENED,
        "seen": EVENT_MARKED_SEEN_FROM_LIST,
        "read_full": EVENT_READ_FULL_IN_APP,
        "saved": EVENT_SAVED,
        "not_interested": EVENT_NOT_INTERESTED,
    }
    return mapping[action]


def _event_type_for_clear_action(action: ReaderStateClearAction) -> str:
    mapping = {
        "seen": EVENT_SEEN_CLEARED,
        "read_full": EVENT_READ_FULL_CLEARED,
        "saved": EVENT_UNSAVED,
        "not_interested": EVENT_NOT_INTERESTED_CLEARED,
    }
    return mapping[action]


def _latest_original_url_for_item(session: Session, item_id: int) -> str | None:
    latest_occurrences = _build_latest_occurrence_subquery(feed_item_ids=[item_id])
    statement = (
        select(SourceItemOccurrence.original_url)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == SourceItemOccurrence.feed_item_id)
            & (latest_occurrences.c.occurrence_id == SourceItemOccurrence.id)
            & (latest_occurrences.c.occurrence_rank == 1),
        )
        .where(SourceItemOccurrence.feed_item_id == item_id)
    )
    return session.scalar(statement)


def _build_contextual_event_payload(
    session: Session,
    item_id: int,
    *,
    view_context: BehaviorViewContext | None,
    event_payload_json: dict,
) -> dict:
    return with_behavior_event_context(
        event_payload_json,
        view_context=view_context,
        routing_context=build_routing_context_snapshot(session, item_id),
    )


def _normalize_insight_feedback(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip()
    return normalized or None


def _get_or_create_item_state(session: Session, item_id: int) -> ItemState | None:
    if session.get(FeedItem, item_id) is None:
        return None

    statement = select(ItemState).where(ItemState.feed_item_id == item_id)
    item_state = session.scalar(statement)
    if item_state is None:
        item_state = ItemState(feed_item_id=item_id, status="unread")
        session.add(item_state)
        session.flush()
    return item_state


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
