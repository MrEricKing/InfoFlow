from __future__ import annotations

from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, InsightChannel, Source, SourceChannelTendency, SourceItemOccurrence
from app.services.insight_engine_artifacts import (
    FeedItemInsightRoutingResolution,
    build_latest_occurrence_subquery,
    load_latest_feed_item_insight_artifacts,
    resolve_routing,
)
from app.services.structured_feedback import (
    apply_structured_channel_feedback_to_routing,
    load_structured_feedback_map,
)
from app.services.insight_channels import channel_is_active, ensure_default_channels


class InsightRoutingContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed_item_id: int
    routing_source: str | None
    primary_channel_id: str | None
    secondary_channel_ids: list[str] = Field(default_factory=list)
    flow_lane: str | None
    reading_depth: str | None
    source_flow_policy: str | None
    source_id: int | None
    source_folder: str | None
    channel_context: dict[str, Any] = Field(default_factory=dict)
    prior_routing: dict[str, Any] | None
    corrected_routing: dict[str, Any] | None


def build_routing_context_snapshot(session: Session, feed_item_id: int) -> dict[str, Any]:
    return build_routing_context_snapshots(session, [feed_item_id]).get(
        feed_item_id,
        _empty_snapshot(feed_item_id).model_dump(mode="json"),
    )


def build_routing_context_snapshots(
    session: Session,
    feed_item_ids: Sequence[int],
) -> dict[int, dict[str, Any]]:
    unique_ids = list(dict.fromkeys(feed_item_ids))
    if not unique_ids:
        return {}

    latest_occurrences = build_latest_occurrence_subquery(feed_item_ids=unique_ids)
    rows = session.execute(
        select(FeedItem, Source)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .where(FeedItem.id.in_(unique_ids))
    ).all()
    feed_items_by_id: dict[int, FeedItem] = {feed_item.id: feed_item for feed_item, _source in rows}
    sources_by_item_id: dict[int, Source] = {feed_item.id: source for feed_item, source in rows}
    source_ids = [source.id for source in sources_by_item_id.values()]
    artifact_map = load_latest_feed_item_insight_artifacts(session, feed_item_ids=unique_ids)
    channels_by_id = _load_channels_by_id(session)
    tendencies_by_source_id = _load_source_tendencies_by_source_id(session, source_ids=source_ids)
    structured_feedback_map = load_structured_feedback_map(session, unique_ids)
    allowed_channel_ids = {channel_id for channel_id, channel in channels_by_id.items() if channel_is_active(channel)}

    snapshots: dict[int, dict[str, Any]] = {}
    for feed_item_id in unique_ids:
        source = sources_by_item_id.get(feed_item_id)
        if source is None:
            snapshots[feed_item_id] = _empty_snapshot(feed_item_id).model_dump(mode="json")
            continue

        base_routing = resolve_routing(
            feed_item=feed_items_by_id[feed_item_id],
            source=source,
            artifacts_by_kind=artifact_map.get(feed_item_id),
            channels_by_id=channels_by_id,
            source_tendencies=tendencies_by_source_id.get(source.id, []),
        )
        final_routing = apply_structured_channel_feedback_to_routing(
            base_routing,
            structured_feedback_map.get(feed_item_id),
            allowed_channel_ids=allowed_channel_ids,
        )
        base_payload = _routing_context_from_resolution(base_routing)
        final_payload = _routing_context_from_resolution(final_routing)
        has_correction = final_payload != base_payload
        snapshots[feed_item_id] = InsightRoutingContextSnapshot(
            feed_item_id=feed_item_id,
            routing_source=final_routing.source,
            primary_channel_id=final_routing.primary_channel_id,
            secondary_channel_ids=[secondary.channel_id for secondary in final_routing.secondary_channels],
            flow_lane=final_routing.flow_lane,
            reading_depth=final_routing.reading_depth,
            source_flow_policy=source.flow_policy or "normal",
            source_id=source.id,
            source_folder=source.folder_path,
            channel_context={
                "primary_channel_id": final_routing.primary_channel_id,
                "secondary_channel_ids": [secondary.channel_id for secondary in final_routing.secondary_channels],
            },
            prior_routing=base_payload if has_correction else None,
            corrected_routing=final_payload if has_correction else None,
        ).model_dump(mode="json")

    return snapshots


def _routing_context_from_resolution(routing: FeedItemInsightRoutingResolution) -> dict[str, Any]:
    secondary_channel_ids = [secondary.channel_id for secondary in routing.secondary_channels]
    return {
        "routing_source": routing.source,
        "primary_channel_id": routing.primary_channel_id,
        "secondary_channel_ids": secondary_channel_ids,
        "reading_depth": routing.reading_depth,
        "flow_lane": routing.flow_lane,
        "confidence": routing.confidence,
        "reason_codes": list(routing.reason_codes),
        "promotion_reason_codes": list(routing.promotion_reason_codes),
        "isolation_reason_codes": list(routing.isolation_reason_codes),
        "source_policy_applied": routing.source_policy_applied,
        "notes": routing.notes,
        "channel_context": {
            "primary_channel_id": routing.primary_channel_id,
            "secondary_channel_ids": secondary_channel_ids,
        },
    }


def _load_channels_by_id(session: Session) -> dict[str, InsightChannel]:
    ensure_default_channels(session)
    return {channel.id: channel for channel in session.scalars(select(InsightChannel)).all()}


def _load_source_tendencies_by_source_id(
    session: Session,
    *,
    source_ids: Sequence[int],
) -> dict[int, list[SourceChannelTendency]]:
    unique_source_ids = list(dict.fromkeys(source_ids))
    if not unique_source_ids:
        return {}

    tendencies_by_source_id: dict[int, list[SourceChannelTendency]] = {}
    for tendency in session.scalars(
        select(SourceChannelTendency).where(SourceChannelTendency.source_id.in_(unique_source_ids))
    ):
        tendencies_by_source_id.setdefault(tendency.source_id, []).append(tendency)
    return tendencies_by_source_id


def _empty_snapshot(feed_item_id: int) -> InsightRoutingContextSnapshot:
    return InsightRoutingContextSnapshot(
        feed_item_id=feed_item_id,
        routing_source=None,
        primary_channel_id=None,
        secondary_channel_ids=[],
        flow_lane=None,
        reading_depth=None,
        source_flow_policy=None,
        source_id=None,
        source_folder=None,
        channel_context={"primary_channel_id": None, "secondary_channel_ids": []},
        prior_routing=None,
        corrected_routing=None,
    )
