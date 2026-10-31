from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    FeedItem,
    FeedItemStructuredFeedback,
    InsightChannel,
    InsightEngineArtifact,
    Source,
    SourceChannelTendency,
    SourceItemOccurrence,
)
from app.schemas.settings import InsightChannelImpactRead
from app.services.insight_channels import (
    NEEDS_REVIEW_CHANNEL_ID,
    InsightChannelNotFoundError,
    InsightChannelValidationError,
    bump_channel_catalog_version,
    channel_is_active,
    ensure_default_channels,
    get_channel_catalog_version,
    validate_expected_catalog_version,
)
from app.services.insight_engine_artifacts import (
    FEED_ITEM_ROUTING_ARTIFACT_KIND,
    FeedItemInsightRoutingArtifactPayload,
    FeedItemInsightRoutingLifecycleMetadataPayload,
    FeedItemInsightRoutingSecondaryChannelPayload,
    build_latest_occurrence_subquery,
)

CHANNEL_LIFECYCLE_MODEL_NAME = "system/channel-lifecycle"
CHANNEL_LIFECYCLE_PROMPT_VERSION = "channel-lifecycle-reroute-v1"


@dataclass(frozen=True, slots=True)
class ChannelRetirePlan:
    channel_id: str
    channel_label: str
    fallback_channel_id: str
    channel_catalog_version: int
    affected_feed_item_ids: list[int]
    impact: InsightChannelImpactRead


@dataclass(frozen=True, slots=True)
class ChannelRerouteResult:
    feed_item_id: int
    artifact_created: bool
    previous_channel_ids: list[str]
    new_primary_channel_id: str | None


def get_channel_retire_impact(session: Session, channel_id: str) -> InsightChannelImpactRead:
    ensure_default_channels(session)
    channel = session.get(InsightChannel, channel_id)
    if channel is None:
        raise InsightChannelNotFoundError("Insight channel not found")
    affected_feed_item_ids = _affected_feed_item_ids(session, channel_id)
    return InsightChannelImpactRead(
        channel_id=channel.id,
        channel_label=channel.label,
        affected_feed_item_count=len(affected_feed_item_ids),
        source_tendency_count=_source_tendency_count(session, channel_id),
        human_correction_count=_human_correction_count(session, channel_id),
        channel_catalog_version=get_channel_catalog_version(session),
    )


def prepare_channel_retire(
    session: Session,
    *,
    channel_id: str,
    expected_channel_catalog_version: int,
) -> ChannelRetirePlan:
    ensure_default_channels(session)
    validate_expected_catalog_version(session, expected_channel_catalog_version)
    channel = session.get(InsightChannel, channel_id)
    if channel is None:
        raise InsightChannelNotFoundError("Insight channel not found")
    if channel.is_system or channel.id == NEEDS_REVIEW_CHANNEL_ID:
        raise InsightChannelValidationError("System insight channels cannot be retired")
    if channel.retired_at is not None:
        raise InsightChannelValidationError("Insight channel is already retired")

    fallback = session.get(InsightChannel, NEEDS_REVIEW_CHANNEL_ID)
    if not channel_is_active(fallback):
        raise InsightChannelValidationError("System fallback channel needs_review is missing or inactive")

    affected_feed_item_ids = _affected_feed_item_ids(session, channel.id)
    impact = InsightChannelImpactRead(
        channel_id=channel.id,
        channel_label=channel.label,
        affected_feed_item_count=len(affected_feed_item_ids),
        source_tendency_count=_source_tendency_count(session, channel.id),
        human_correction_count=_human_correction_count(session, channel.id),
        channel_catalog_version=get_channel_catalog_version(session),
    )

    channel.enabled = False
    channel.retired_at = _now_utc()
    session.add(channel)
    session.flush()
    next_version = bump_channel_catalog_version(session)

    return ChannelRetirePlan(
        channel_id=channel.id,
        channel_label=channel.label,
        fallback_channel_id=NEEDS_REVIEW_CHANNEL_ID,
        channel_catalog_version=next_version,
        affected_feed_item_ids=affected_feed_item_ids,
        impact=impact.model_copy(update={"channel_catalog_version": next_version}),
    )


def reroute_feed_item_for_retired_channel(
    session: Session,
    *,
    feed_item_id: int,
    retired_channel_id: str,
    fallback_channel_id: str,
    channel_catalog_version: int,
    operation_id: int,
    reason: str,
) -> ChannelRerouteResult:
    feed_item = session.get(FeedItem, feed_item_id)
    if feed_item is None:
        return ChannelRerouteResult(
            feed_item_id=feed_item_id,
            artifact_created=False,
            previous_channel_ids=[],
            new_primary_channel_id=None,
        )

    source = _latest_source_for_feed_item(session, feed_item_id)
    artifact = _latest_routing_artifact(session, feed_item_id)
    structured_feedback = session.scalar(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == feed_item_id)
    )
    routing_channel_ids = _referenced_channel_ids_from_payload(artifact.artifact_payload if artifact else None)
    feedback_channel_ids = _referenced_channel_ids_from_feedback(structured_feedback)
    previous_channel_ids = sorted(routing_channel_ids | feedback_channel_ids)
    if retired_channel_id not in routing_channel_ids:
        return ChannelRerouteResult(
            feed_item_id=feed_item_id,
            artifact_created=False,
            previous_channel_ids=previous_channel_ids,
            new_primary_channel_id=None,
        )

    channels_by_id = {channel.id: channel for channel in session.scalars(select(InsightChannel)).all()}
    fallback = channels_by_id.get(fallback_channel_id)
    if not channel_is_active(fallback):
        raise InsightChannelValidationError("System fallback channel needs_review is missing or inactive")

    active_channels_by_id = {
        channel_id: channel for channel_id, channel in channels_by_id.items() if channel_is_active(channel)
    }
    candidates = _candidate_channels(
        artifact.artifact_payload if artifact else None,
        structured_feedback,
        active_channels_by_id=active_channels_by_id,
        retired_channel_id=retired_channel_id,
    )
    selected_channel_id, selected_confidence = _select_best_channel_candidate(
        candidates,
        active_channels_by_id=active_channels_by_id,
        fallback_channel_id=fallback_channel_id,
    )
    selected_channel = active_channels_by_id[selected_channel_id]
    secondary_channels = [
        FeedItemInsightRoutingSecondaryChannelPayload(
            channel_id=channel_id,
            confidence=confidence,
            display_mode="list",
        )
        for channel_id, confidence in _secondary_channel_candidates(
            candidates,
            primary_channel_id=selected_channel_id,
            active_channels_by_id=active_channels_by_id,
        )
    ]
    flow_lane = "needs_review" if selected_channel_id == fallback_channel_id else _flow_lane_for_source(source)
    reason_codes = ["channel_deleted_fallback"]
    isolation_reason_codes = ["ambiguous_channel_needs_review"] if selected_channel_id == fallback_channel_id else []
    payload = FeedItemInsightRoutingArtifactPayload(
        schema_version=FEED_ITEM_ROUTING_ARTIFACT_KIND,
        primary_channel_id=selected_channel_id,
        secondary_channels=secondary_channels,
        reading_depth=_reading_depth_for_channel(selected_channel),
        flow_lane=flow_lane,
        confidence=selected_confidence,
        reason_codes=reason_codes,
        promotion_reason_codes=[],
        isolation_reason_codes=isolation_reason_codes,
        source_policy_applied=_source_policy_for_source(source),
        notes=(
            f"Channel lifecycle reroute after retiring {retired_channel_id}; "
            f"operation_id={operation_id}; catalog_version={channel_catalog_version}; reason={reason}."
        ),
        source="channel_lifecycle",
        lifecycle_metadata=FeedItemInsightRoutingLifecycleMetadataPayload(
            operation_id=operation_id,
            channel_catalog_version=channel_catalog_version,
            retired_channel_id=retired_channel_id,
            fallback_channel_id=fallback_channel_id,
            previous_channel_ids=previous_channel_ids,
            reason=reason,
        ),
    )
    session.add(
        InsightEngineArtifact(
            feed_item_id=feed_item.id,
            saved_item_id=None,
            artifact_kind=FEED_ITEM_ROUTING_ARTIFACT_KIND,
            model_name=CHANNEL_LIFECYCLE_MODEL_NAME,
            prompt_version=CHANNEL_LIFECYCLE_PROMPT_VERSION,
            artifact_payload=payload.model_dump(),
        )
    )
    session.flush()
    return ChannelRerouteResult(
        feed_item_id=feed_item_id,
        artifact_created=True,
        previous_channel_ids=previous_channel_ids,
        new_primary_channel_id=selected_channel_id,
    )


def _affected_feed_item_ids(session: Session, channel_id: str) -> list[int]:
    item_ids: set[int] = set()
    for artifact in _latest_routing_artifacts(session):
        if artifact.feed_item_id is not None and channel_id in _referenced_channel_ids_from_payload(artifact.artifact_payload):
            item_ids.add(artifact.feed_item_id)
    for row in session.scalars(select(FeedItemStructuredFeedback)):
        if channel_id in _referenced_channel_ids_from_feedback(row):
            item_ids.add(row.feed_item_id)
    return sorted(item_ids)


def _latest_routing_artifacts(session: Session) -> list[InsightEngineArtifact]:
    rows = list(
        session.scalars(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.artifact_kind == FEED_ITEM_ROUTING_ARTIFACT_KIND)
            .where(InsightEngineArtifact.feed_item_id.is_not(None))
            .order_by(InsightEngineArtifact.feed_item_id.asc(), InsightEngineArtifact.id.desc())
        )
    )
    latest: list[InsightEngineArtifact] = []
    seen_feed_item_ids: set[int] = set()
    for artifact in rows:
        if artifact.feed_item_id in seen_feed_item_ids:
            continue
        seen_feed_item_ids.add(artifact.feed_item_id)
        latest.append(artifact)
    return latest


def _latest_routing_artifact(session: Session, feed_item_id: int) -> InsightEngineArtifact | None:
    return session.scalar(
        select(InsightEngineArtifact)
        .where(InsightEngineArtifact.feed_item_id == feed_item_id)
        .where(InsightEngineArtifact.artifact_kind == FEED_ITEM_ROUTING_ARTIFACT_KIND)
        .order_by(InsightEngineArtifact.id.desc())
        .limit(1)
    )


def _latest_source_for_feed_item(session: Session, feed_item_id: int) -> Source | None:
    latest_occurrences = build_latest_occurrence_subquery(feed_item_ids=[feed_item_id])
    return session.scalar(
        select(Source)
        .join(SourceItemOccurrence, SourceItemOccurrence.source_id == Source.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.occurrence_id == SourceItemOccurrence.id)
            & (latest_occurrences.c.occurrence_rank == 1),
        )
        .where(SourceItemOccurrence.feed_item_id == feed_item_id)
    )


def _source_tendency_count(session: Session, channel_id: str) -> int:
    return len(
        list(
            session.scalars(
                select(SourceChannelTendency.id).where(SourceChannelTendency.channel_id == channel_id)
            )
        )
    )


def _human_correction_count(session: Session, channel_id: str) -> int:
    return sum(
        1
        for row in session.scalars(select(FeedItemStructuredFeedback))
        if channel_id in _referenced_channel_ids_from_feedback(row)
    )


def _referenced_channel_ids_from_payload(payload: dict[str, Any] | None) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    channel_ids: set[str] = set()
    primary = payload.get("primary_channel_id")
    if isinstance(primary, str) and primary:
        channel_ids.add(primary)
    secondaries = payload.get("secondary_channels")
    if isinstance(secondaries, list):
        for secondary in secondaries:
            if not isinstance(secondary, dict):
                continue
            channel_id = secondary.get("channel_id")
            if isinstance(channel_id, str) and channel_id:
                channel_ids.add(channel_id)
    return channel_ids


def _referenced_channel_ids_from_feedback(row: FeedItemStructuredFeedback | None) -> set[str]:
    if row is None:
        return set()
    channel_ids: set[str] = set()
    if row.primary_channel_correction:
        channel_ids.add(row.primary_channel_correction)
    channel_ids.update(channel_id for channel_id in (row.channel_add_ids_json or []) if isinstance(channel_id, str))
    channel_ids.update(channel_id for channel_id in (row.channel_remove_ids_json or []) if isinstance(channel_id, str))
    return channel_ids


def _candidate_channels(
    payload: dict[str, Any] | None,
    feedback: FeedItemStructuredFeedback | None,
    *,
    active_channels_by_id: dict[str, InsightChannel],
    retired_channel_id: str,
) -> list[tuple[str, float]]:
    candidates: list[tuple[str, float]] = []
    if isinstance(payload, dict):
        primary = payload.get("primary_channel_id")
        confidence = _confidence(payload.get("confidence"))
        if isinstance(primary, str) and primary != retired_channel_id and primary in active_channels_by_id:
            candidates.append((primary, confidence))
        secondaries = payload.get("secondary_channels")
        if isinstance(secondaries, list):
            for secondary in secondaries:
                if not isinstance(secondary, dict):
                    continue
                channel_id = secondary.get("channel_id")
                if isinstance(channel_id, str) and channel_id != retired_channel_id and channel_id in active_channels_by_id:
                    candidates.append((channel_id, _confidence(secondary.get("confidence"))))
    if feedback is not None:
        if (
            feedback.primary_channel_correction
            and feedback.primary_channel_correction != retired_channel_id
            and feedback.primary_channel_correction in active_channels_by_id
        ):
            candidates.append((feedback.primary_channel_correction, 1.0))
        for channel_id in feedback.channel_add_ids_json or []:
            if isinstance(channel_id, str) and channel_id != retired_channel_id and channel_id in active_channels_by_id:
                candidates.append((channel_id, 1.0))
    return candidates


def _select_best_channel_candidate(
    candidates: Sequence[tuple[str, float]],
    *,
    active_channels_by_id: dict[str, InsightChannel],
    fallback_channel_id: str,
) -> tuple[str, float]:
    best_confidence_by_channel: dict[str, float] = {}
    for channel_id, confidence in candidates:
        best_confidence_by_channel[channel_id] = max(confidence, best_confidence_by_channel.get(channel_id, 0.0))
    if not best_confidence_by_channel:
        return fallback_channel_id, 0.0
    return sorted(
        best_confidence_by_channel.items(),
        key=lambda row: (
            -row[1],
            active_channels_by_id[row[0]].sort_order,
            row[0],
        ),
    )[0]


def _secondary_channel_candidates(
    candidates: Sequence[tuple[str, float]],
    *,
    primary_channel_id: str,
    active_channels_by_id: dict[str, InsightChannel],
) -> list[tuple[str, float]]:
    best_confidence_by_channel: dict[str, float] = {}
    for channel_id, confidence in candidates:
        if channel_id == primary_channel_id or channel_id not in active_channels_by_id:
            continue
        best_confidence_by_channel[channel_id] = max(confidence, best_confidence_by_channel.get(channel_id, 0.0))
    return sorted(
        best_confidence_by_channel.items(),
        key=lambda row: (
            -row[1],
            active_channels_by_id[row[0]].sort_order,
            row[0],
        ),
    )[:3]


def _confidence(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(numeric, 0.0), 1.0)


def _reading_depth_for_channel(channel: InsightChannel) -> str:
    return channel.reading_depth if channel.reading_depth in {"light", "medium", "deep"} else "medium"


def _flow_lane_for_source(source: Source | None) -> str:
    if source is not None and source.flow_policy == "high_frequency_brief":
        return "brief_pool"
    return "main"


def _source_policy_for_source(source: Source | None) -> str:
    if source is None:
        return "normal"
    return source.flow_policy if source.flow_policy in {"normal", "mixed_high_frequency", "high_frequency_brief"} else "normal"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
