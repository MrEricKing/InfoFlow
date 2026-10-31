from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItemStructuredFeedback, InsightChannel
from app.schemas.reader import HomepageFlowLane, ReaderStructuredFeedbackInput, ReaderStructuredFeedbackRead, ReadingDepth
from app.services.insight_engine_artifacts import (
    FeedItemInsightRoutingResolution,
    FeedItemInsightRoutingSecondaryChannelPayload,
)
from app.services.insight_engine_contracts import (
    REASON_CODES,
    canonicalize_content_type_id,
    canonicalize_taxonomy_id,
)


@dataclass(frozen=True, slots=True)
class NormalizedStructuredFeedback:
    overall_value: str | None
    priority_correction: str | None
    topic_add_ids: list[str]
    topic_remove_ids: list[str]
    content_type_correction: str | None
    reason_code_add_ids: list[str]
    reason_code_remove_ids: list[str]
    issue_markers: list[str]
    primary_channel_correction: str | None
    channel_add_ids: list[str]
    channel_remove_ids: list[str]
    reading_depth_correction: str | None
    flow_lane_correction: str | None
    channel_correction_notes: str
    provided_fields: frozenset[str]
    channel_fields_provided: bool

    @property
    def is_empty(self) -> bool:
        if self.provided_fields:
            return False
        return not any(
            (
                self.overall_value,
                self.priority_correction,
                self.topic_add_ids,
                self.topic_remove_ids,
                self.content_type_correction,
                self.reason_code_add_ids,
                self.reason_code_remove_ids,
                self.issue_markers,
                self.primary_channel_correction,
                self.channel_add_ids,
                self.channel_remove_ids,
                self.reading_depth_correction,
                self.flow_lane_correction,
            )
        )

    @property
    def has_regular_structured_content(self) -> bool:
        return any(
            (
                self.overall_value,
                self.priority_correction,
                self.topic_add_ids,
                self.topic_remove_ids,
                self.content_type_correction,
                self.reason_code_add_ids,
                self.reason_code_remove_ids,
                self.issue_markers,
            )
        )

    @property
    def has_channel_routing_correction(self) -> bool:
        return any(
            (
                self.primary_channel_correction,
                self.channel_add_ids,
                self.channel_remove_ids,
                self.reading_depth_correction,
                self.flow_lane_correction,
            )
        )


class StructuredChannelFeedbackRead(BaseModel):
    primary_channel_id: str | None = None
    add_channel_ids: list[str] = Field(default_factory=list)
    remove_channel_ids: list[str] = Field(default_factory=list)
    reading_depth: ReadingDepth | None = None
    flow_lane: HomepageFlowLane | None = None
    notes: str = ""
    updated_at: datetime | None = None


_REASON_CODE_SET = {reason.code for reason in REASON_CODES}
_CHANNEL_CORRECTION_FIELDS = {
    "primary_channel_correction",
    "channel_add_ids",
    "channel_remove_ids",
    "reading_depth_correction",
    "flow_lane_correction",
    "channel_correction_notes",
}


def normalize_structured_feedback_input(
    payload: ReaderStructuredFeedbackInput,
    *,
    session: Session | None = None,
    existing_row: FeedItemStructuredFeedback | None = None,
) -> NormalizedStructuredFeedback:
    channel_fields_provided = bool(_CHANNEL_CORRECTION_FIELDS & payload.model_fields_set)
    provided_fields = frozenset(payload.model_fields_set)
    normalized = NormalizedStructuredFeedback(
        overall_value=payload.overall_value,
        priority_correction=payload.priority_correction,
        topic_add_ids=_normalize_taxonomy_ids(payload.topic_add_ids, field_name="topic_add_ids"),
        topic_remove_ids=_normalize_taxonomy_ids(payload.topic_remove_ids, field_name="topic_remove_ids"),
        content_type_correction=_normalize_content_type(payload.content_type_correction),
        reason_code_add_ids=_normalize_reason_codes(payload.reason_code_add_ids, field_name="reason_code_add_ids"),
        reason_code_remove_ids=_normalize_reason_codes(payload.reason_code_remove_ids, field_name="reason_code_remove_ids"),
        issue_markers=_dedupe_strings(payload.issue_markers),
        primary_channel_correction=_normalize_primary_channel_id(
            payload.primary_channel_correction,
            provided="primary_channel_correction" in payload.model_fields_set,
        ),
        channel_add_ids=normalize_channel_ids(payload.channel_add_ids, field_name="channel_add_ids"),
        channel_remove_ids=normalize_channel_ids(payload.channel_remove_ids, field_name="channel_remove_ids"),
        reading_depth_correction=payload.reading_depth_correction,
        flow_lane_correction=payload.flow_lane_correction,
        channel_correction_notes=(payload.channel_correction_notes or "").strip(),
        provided_fields=provided_fields,
        channel_fields_provided=channel_fields_provided,
    )
    _validate_channel_feedback_input(normalized, existing_row=existing_row)
    if session is not None:
        validate_structured_feedback_channels(session, normalized)
    return normalized


def apply_structured_feedback(
    row: FeedItemStructuredFeedback,
    feedback: NormalizedStructuredFeedback,
) -> None:
    if "overall_value" in feedback.provided_fields:
        row.overall_value = feedback.overall_value
    if "priority_correction" in feedback.provided_fields:
        row.priority_correction = feedback.priority_correction
    if "topic_add_ids" in feedback.provided_fields:
        row.topic_add_ids_json = feedback.topic_add_ids
    if "topic_remove_ids" in feedback.provided_fields:
        row.topic_remove_ids_json = feedback.topic_remove_ids
    if "content_type_correction" in feedback.provided_fields:
        row.content_type_correction = feedback.content_type_correction
    if "reason_code_add_ids" in feedback.provided_fields:
        row.reason_code_add_ids_json = feedback.reason_code_add_ids
    if "reason_code_remove_ids" in feedback.provided_fields:
        row.reason_code_remove_ids_json = feedback.reason_code_remove_ids
    if "issue_markers" in feedback.provided_fields:
        row.issue_markers_json = feedback.issue_markers
    if feedback.channel_fields_provided:
        row.primary_channel_correction = feedback.primary_channel_correction
        row.channel_add_ids_json = feedback.channel_add_ids
        row.channel_remove_ids_json = feedback.channel_remove_ids
        row.reading_depth_correction = feedback.reading_depth_correction
        row.flow_lane_correction = feedback.flow_lane_correction
        row.channel_correction_notes = feedback.channel_correction_notes


def serialize_structured_feedback(
    row: FeedItemStructuredFeedback | None,
) -> ReaderStructuredFeedbackRead | None:
    if row is None:
        return None
    return ReaderStructuredFeedbackRead(
        overall_value=row.overall_value,
        priority_correction=row.priority_correction,
        topic_add_ids=list(row.topic_add_ids_json or []),
        topic_remove_ids=list(row.topic_remove_ids_json or []),
        content_type_correction=row.content_type_correction,
        reason_code_add_ids=list(row.reason_code_add_ids_json or []),
        reason_code_remove_ids=list(row.reason_code_remove_ids_json or []),
        issue_markers=list(row.issue_markers_json or []),
        primary_channel_correction=row.primary_channel_correction,
        channel_add_ids=list(row.channel_add_ids_json or []),
        channel_remove_ids=list(row.channel_remove_ids_json or []),
        reading_depth_correction=row.reading_depth_correction,
        flow_lane_correction=row.flow_lane_correction,
        channel_correction_notes=row.channel_correction_notes or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def serialize_structured_feedback_json(row: FeedItemStructuredFeedback | None) -> dict | None:
    read_model = serialize_structured_feedback(row)
    return read_model.model_dump(mode="json") if read_model is not None else None


def structured_feedback_row_is_empty(row: FeedItemStructuredFeedback | None) -> bool:
    if row is None:
        return True
    return not any(
        (
            row.overall_value,
            row.priority_correction,
            row.topic_add_ids_json,
            row.topic_remove_ids_json,
            row.content_type_correction,
            row.reason_code_add_ids_json,
            row.reason_code_remove_ids_json,
            row.issue_markers_json,
            row.primary_channel_correction,
            row.channel_add_ids_json,
            row.channel_remove_ids_json,
            row.reading_depth_correction,
            row.flow_lane_correction,
            row.channel_correction_notes,
        )
    )


def load_structured_feedback_map(
    session: Session,
    feed_item_ids: Sequence[int],
) -> dict[int, FeedItemStructuredFeedback]:
    unique_ids = list(dict.fromkeys(feed_item_ids))
    if not unique_ids:
        return {}
    rows = session.scalars(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id.in_(unique_ids))
    ).all()
    return {row.feed_item_id: row for row in rows}


def list_structured_channel_feedback_for_items(
    session: Session,
    feed_item_ids: Sequence[int],
) -> dict[int, StructuredChannelFeedbackRead]:
    rows = load_structured_feedback_map(session, feed_item_ids)
    result: dict[int, StructuredChannelFeedbackRead] = {}
    for feed_item_id, row in rows.items():
        feedback = structured_channel_feedback_from_row(row)
        if feedback is not None:
            result[feed_item_id] = feedback
    return result


def structured_channel_feedback_from_row(row: FeedItemStructuredFeedback | None) -> StructuredChannelFeedbackRead | None:
    if row is None:
        return None
    feedback = StructuredChannelFeedbackRead(
        primary_channel_id=row.primary_channel_correction,
        add_channel_ids=list(row.channel_add_ids_json or []),
        remove_channel_ids=list(row.channel_remove_ids_json or []),
        reading_depth=row.reading_depth_correction,
        flow_lane=row.flow_lane_correction,
        notes=row.channel_correction_notes or "",
        updated_at=row.updated_at,
    )
    if not _channel_feedback_has_correction(feedback):
        return None
    return feedback


def serialize_channel_corrections(row: FeedItemStructuredFeedback | None) -> dict[str, object] | None:
    feedback = structured_channel_feedback_from_row(row)
    if feedback is None:
        return None
    return {
        "primary_channel_correction": feedback.primary_channel_id,
        "channel_add_ids": feedback.add_channel_ids,
        "channel_remove_ids": feedback.remove_channel_ids,
        "reading_depth_correction": feedback.reading_depth,
        "flow_lane_correction": feedback.flow_lane,
        "channel_correction_notes": feedback.notes,
    }


def apply_structured_channel_feedback_to_routing(
    routing: FeedItemInsightRoutingResolution,
    feedback: StructuredChannelFeedbackRead | FeedItemStructuredFeedback | None,
    *,
    allowed_channel_ids: set[str] | None = None,
) -> FeedItemInsightRoutingResolution:
    if isinstance(feedback, FeedItemStructuredFeedback):
        feedback = structured_channel_feedback_from_row(feedback)
    if feedback is None:
        return routing

    remove_ids = _filter_allowed_ids(feedback.remove_channel_ids, allowed_channel_ids)
    primary_channel_id = _allowed_or_none(feedback.primary_channel_id, allowed_channel_ids)
    if primary_channel_id is None and routing.primary_channel_id not in remove_ids:
        primary_channel_id = routing.primary_channel_id
    secondary_channels = [
        (secondary.channel_id, secondary.confidence, secondary.display_mode)
        for secondary in routing.secondary_channels
        if secondary.channel_id not in remove_ids and _channel_is_allowed(secondary.channel_id, allowed_channel_ids)
    ]
    existing_ids = {channel_id for channel_id, _, _ in secondary_channels}
    for channel_id in _filter_allowed_ids(feedback.add_channel_ids, allowed_channel_ids):
        if channel_id not in existing_ids:
            secondary_channels.append((channel_id, 1.0, "list"))
            existing_ids.add(channel_id)
    secondary_channels = [
        (channel_id, confidence, display_mode)
        for channel_id, confidence, display_mode in secondary_channels
        if channel_id != primary_channel_id
    ]
    reason_codes = list(routing.reason_codes)
    if "manual_override_user_corrected" not in reason_codes:
        reason_codes.append("manual_override_user_corrected")
    if "user_channel_correction_hint" not in reason_codes:
        reason_codes.append("user_channel_correction_hint")

    return routing.model_copy(
        update={
            "primary_channel_id": primary_channel_id,
            "secondary_channels": [
                FeedItemInsightRoutingSecondaryChannelPayload(
                    channel_id=channel_id,
                    confidence=confidence,
                    display_mode=display_mode,
                )
                for channel_id, confidence, display_mode in secondary_channels
            ],
            "reading_depth": feedback.reading_depth or routing.reading_depth,
            "flow_lane": feedback.flow_lane or routing.flow_lane,
            "reason_codes": reason_codes,
            "notes": feedback.notes or routing.notes,
            "source": "user_corrected",
        }
    )


def normalize_channel_ids(values: Sequence[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        channel_id = value.strip()
        if not channel_id:
            raise ValueError(f"{field_name} must not contain blank channel ids")
        if channel_id in seen:
            raise ValueError(f"{field_name} must not contain duplicate channel ids")
        seen.add(channel_id)
        normalized.append(channel_id)
    return normalized


def _normalize_taxonomy_ids(values: Sequence[str], *, field_name: str) -> list[str]:
    result: list[str] = []
    for value in values:
        if not value.strip():
            continue
        canonical = canonicalize_taxonomy_id(value)
        if canonical is None:
            raise ValueError(f"{field_name} contains unknown taxonomy id: {value}")
        if canonical not in result:
            result.append(canonical)
    return result


def _normalize_content_type(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    canonical = canonicalize_content_type_id(value)
    if canonical is None:
        raise ValueError(f"content_type_correction contains unknown content type: {value}")
    return canonical


def _normalize_reason_codes(values: Sequence[str], *, field_name: str) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        if normalized not in _REASON_CODE_SET:
            raise ValueError(f"{field_name} contains unknown reason code: {value}")
        if normalized not in result:
            result.append(normalized)
    return result


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalize_primary_channel_id(value: str | None, *, provided: bool) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if provided and not normalized:
        raise ValueError("primary_channel_correction must not be blank")
    return normalized or None


def _validate_channel_feedback_input(
    feedback: NormalizedStructuredFeedback,
    *,
    existing_row: FeedItemStructuredFeedback | None,
) -> None:
    channel_has_correction = feedback.has_channel_routing_correction
    existing_row_has_content = not structured_feedback_row_is_empty(existing_row)
    if feedback.channel_fields_provided and feedback.channel_correction_notes and not channel_has_correction:
        raise ValueError("Channel feedback must include at least one routing correction")
    if (
        feedback.channel_fields_provided
        and not channel_has_correction
        and not feedback.has_regular_structured_content
        and not existing_row_has_content
    ):
        raise ValueError("Channel feedback must include at least one routing correction")

    overlap = sorted(set(feedback.channel_add_ids) & set(feedback.channel_remove_ids))
    if overlap:
        raise ValueError(f"Channel cannot be both added and removed: {overlap[0]}")
    if feedback.primary_channel_correction and feedback.primary_channel_correction in feedback.channel_add_ids:
        raise ValueError("Primary channel cannot also be added")
    if feedback.primary_channel_correction and feedback.primary_channel_correction in feedback.channel_remove_ids:
        raise ValueError("Primary channel cannot also be removed")


def validate_structured_feedback_channels(session: Session, feedback: NormalizedStructuredFeedback) -> None:
    requested_ids = set(feedback.channel_add_ids) | set(feedback.channel_remove_ids)
    if feedback.primary_channel_correction:
        requested_ids.add(feedback.primary_channel_correction)
    if not requested_ids:
        return
    active_ids = set(
        session.scalars(
            select(InsightChannel.id)
            .where(InsightChannel.id.in_(requested_ids))
            .where(InsightChannel.enabled.is_(True))
            .where(InsightChannel.retired_at.is_(None))
        ).all()
    )
    missing_ids = sorted(requested_ids - active_ids)
    if missing_ids:
        raise ValueError(f"Insight channel is not active or does not exist: {missing_ids[0]}")


def _channel_feedback_has_correction(feedback: StructuredChannelFeedbackRead) -> bool:
    return any(
        (
            feedback.primary_channel_id,
            feedback.add_channel_ids,
            feedback.remove_channel_ids,
            feedback.reading_depth,
            feedback.flow_lane,
        )
    )


def _channel_is_allowed(channel_id: str, allowed_channel_ids: set[str] | None) -> bool:
    return allowed_channel_ids is None or channel_id in allowed_channel_ids


def _allowed_or_none(channel_id: str | None, allowed_channel_ids: set[str] | None) -> str | None:
    if channel_id is None or not _channel_is_allowed(channel_id, allowed_channel_ids):
        return None
    return channel_id


def _filter_allowed_ids(channel_ids: list[str], allowed_channel_ids: set[str] | None) -> set[str]:
    return set(channel_id for channel_id in channel_ids if _channel_is_allowed(channel_id, allowed_channel_ids))
