from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItemBehaviorEvent
from app.schemas.reader import BehaviorViewContext

EVENT_CARD_OPENED = "card_opened"
EVENT_MARKED_SEEN_FROM_LIST = "marked_seen_from_list"
EVENT_MARKED_SEEN_BULK = "marked_seen_bulk"
EVENT_SEEN_CLEARED = "seen_cleared"
EVENT_READ_FULL_IN_APP = "read_full_in_app"
EVENT_OPENED_ORIGINAL_LINK = "opened_original_link"
EVENT_READ_FULL_CLEARED = "read_full_cleared"
EVENT_SAVED = "saved"
EVENT_UNSAVED = "unsaved"
EVENT_NOT_INTERESTED = "not_interested"
EVENT_NOT_INTERESTED_CLEARED = "not_interested_cleared"
EVENT_AUTO_LOW_PRIORITY_CLEARED = "auto_low_priority_cleared"
EVENT_INSIGHT_FEEDBACK_SAVED = "insight_feedback_saved"
EVENT_INSIGHT_FEEDBACK_CLEARED = "insight_feedback_cleared"
EVENT_STRUCTURED_FEEDBACK_SAVED = "structured_feedback_saved"
EVENT_STRUCTURED_FEEDBACK_CLEARED = "structured_feedback_cleared"

SOURCE_READER_STATE_API = "reader_state_api"
SOURCE_READER_BULK_API = "reader_bulk_api"
SOURCE_READER_FEEDBACK_API = "reader_feedback_api"
SOURCE_READER_PRIORITY_API = "reader_priority_api"
SOURCE_KNOWLEDGE_SAVE = "knowledge_save"
SOURCE_KNOWLEDGE_UNSAVE = "knowledge_unsave"


def normalize_behavior_view_context(
    view_context: BehaviorViewContext | dict[str, Any] | None,
) -> BehaviorViewContext:
    if isinstance(view_context, BehaviorViewContext):
        return view_context
    if isinstance(view_context, dict):
        return BehaviorViewContext.model_validate(view_context)
    return BehaviorViewContext()


def with_behavior_event_context(
    event_payload_json: dict[str, Any] | None,
    *,
    view_context: BehaviorViewContext | dict[str, Any] | None = None,
    routing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(event_payload_json or {})
    if "view_context" in payload:
        raise ValueError("event payload must not provide view_context directly")
    if routing_context is not None and "routing_context" in payload:
        raise ValueError("event payload must not provide routing_context directly")
    payload["view_context"] = normalize_behavior_view_context(view_context).model_dump(mode="json")
    if routing_context is not None:
        payload["routing_context"] = routing_context
    return payload


def record_feed_item_behavior_event(
    session: Session,
    *,
    feed_item_id: int,
    event_type: str,
    source: str,
    event_payload_json: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> FeedItemBehaviorEvent:
    event = FeedItemBehaviorEvent(
        feed_item_id=feed_item_id,
        event_type=event_type,
        event_payload_json=event_payload_json or {},
        source=source,
        created_at=created_at or _now_utc(),
    )
    session.add(event)
    return event


def load_feed_item_behavior_events(
    session: Session,
    *,
    feed_item_ids: list[int],
) -> dict[int, list[FeedItemBehaviorEvent]]:
    if not feed_item_ids:
        return {}

    events_by_item_id: dict[int, list[FeedItemBehaviorEvent]] = {feed_item_id: [] for feed_item_id in feed_item_ids}
    statement = (
        select(FeedItemBehaviorEvent)
        .where(FeedItemBehaviorEvent.feed_item_id.in_(feed_item_ids))
        .order_by(
            FeedItemBehaviorEvent.feed_item_id.asc(),
            FeedItemBehaviorEvent.created_at.asc(),
            FeedItemBehaviorEvent.id.asc(),
        )
    )
    for event in session.scalars(statement):
        events_by_item_id.setdefault(event.feed_item_id, []).append(event)
    return events_by_item_id


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
