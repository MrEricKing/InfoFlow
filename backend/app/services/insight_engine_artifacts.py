from __future__ import annotations

from html import escape, unescape
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    InsightEngineArtifact,
    FeedItem,
    InsightChannel,
    ItemState,
    Source,
    SourceChannelTendency,
    SourceFetch,
    SourceItemOccurrence,
)
from app.services.feed_content_extraction import (
    CONTENT_EXTRACTION_STATUS_FAILED,
    CONTENT_TEXT_SOURCE_EXTRACTED_PAGE,
    is_probably_url_only_or_jump_prompt,
)
from app.services.insight_engine_contracts import (
    canonicalize_content_type_id,
    canonicalize_taxonomy_id,
    get_content_type_label,
    get_taxonomy_label,
)
from app.services.insight_channels import DEFAULT_INSIGHT_CHANNELS, NEEDS_REVIEW_CHANNEL_ID, channel_is_active
from app.services.insight_routing_reason_codes import (
    ISOLATION_REASON_CODE_SET,
    PROMOTION_REASON_CODE_SET,
    ROUTING_REASON_CODE_SET,
)

HomepagePriorityBand = Literal["high", "medium", "low"]
HomepageSummarySource = Literal["insight_engine_artifact", "canonical_summary", "canonical_content", "none"]
HomepageTopicSource = Literal["insight_engine_artifact", "topic_hints", "source_folder", "none"]
HomepagePrioritySource = Literal["insight_engine_artifact", "deterministic_fallback", "backend_scoring_v1"]
TopicExtractionSource = Literal["ai_generated", "topic_hints", "source_folder", "none"]
SummaryArtifactSource = Literal["ai_generated", "canonical_summary", "canonical_content", "none"]
HomepageReadingDepth = Literal["light", "medium", "deep"]
HomepageFlowLane = Literal["main", "brief_pool", "low_priority", "needs_review"]
FeedItemInsightRoutingSource = Literal["insight_engine_artifact", "deterministic_fallback", "user_corrected", "channel_lifecycle"]
FeedItemInsightRoutingSourcePolicy = Literal["normal", "mixed_high_frequency", "high_frequency_brief"]

FEED_ITEM_SUMMARY_ARTIFACT_KIND = "feed_item_summary_v1"
FEED_ITEM_TOPICS_ARTIFACT_KIND = "feed_item_topics_v1"
FEED_ITEM_RANKING_ARTIFACT_KIND = "feed_item_ranking_v1"
FEED_ITEM_ROUTING_ARTIFACT_KIND = "feed_item_routing_v1"
FEED_ITEM_INSIGHT_ARTIFACT_KINDS = (
    FEED_ITEM_SUMMARY_ARTIFACT_KIND,
    FEED_ITEM_TOPICS_ARTIFACT_KIND,
    FEED_ITEM_RANKING_ARTIFACT_KIND,
    FEED_ITEM_ROUTING_ARTIFACT_KIND,
)
KNOWN_HOMEPAGE_CHANNEL_IDS = frozenset(str(row["id"]) for row in DEFAULT_INSIGHT_CHANNELS)
FOLDER_SPLIT_PATTERN = re.compile(r"[\\/|>,]+")
WHITESPACE_PATTERN = re.compile(r"\s+")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
URL_PATTERN = re.compile(r"https?://\S+")
BOILERPLATE_PATTERNS = (
    re.compile(r"^\u524d\u5f80\u539f\u7ad9\u67e5\u770b[:\uff1a]?\s*", re.IGNORECASE),
    re.compile(
        r"^\u8be5\u6e32\u67d3\u7531.+?\u751f\u6210[\uff0c,]?"
        r"\u53ef\u80fd\u5b58\u5728\u6392\u7248\u95ee\u9898[\uff0c,]?"
        r"\u6700\u4f73\u4f53\u9a8c\u8bf7\u524d\u5f80[:\uff1a]?\s*",
        re.IGNORECASE,
    ),
)


class RankingReasonPayload(BaseModel):
    code: str
    label: str
    weight: int


class FeedItemInsightSummaryArtifactPayload(BaseModel):
    text: str | None
    source_kind: SummaryArtifactSource

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return render_summary_html(value)
        return value


class FeedItemInsightTopicsArtifactPayload(BaseModel):
    labels: list[str]
    source_kind: TopicExtractionSource
    topic_ids: list[str] = Field(default_factory=list)
    content_type: str | None = None
    content_type_label: str | None = None

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        if not isinstance(value, list):
            return []

        labels = [normalize_text(entry) for entry in value if isinstance(entry, str)]
        return [label for label in _dedupe_keep_order(labels) if label]

    @field_validator("topic_ids", mode="before")
    @classmethod
    def normalize_topic_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            return []
        topic_ids: list[str] = []
        seen: set[str] = set()
        for entry in value:
            topic_id = canonicalize_taxonomy_id(entry)
            if topic_id is None or topic_id in seen:
                continue
            seen.add(topic_id)
            topic_ids.append(topic_id)
        return topic_ids[:3]

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return canonicalize_content_type_id(value)

    @model_validator(mode="after")
    def fill_controlled_labels(self) -> "FeedItemInsightTopicsArtifactPayload":
        if self.topic_ids:
            self.labels = [get_taxonomy_label(topic_id) for topic_id in self.topic_ids]
        if self.content_type:
            self.content_type_label = get_content_type_label(self.content_type)
        return self


class FeedItemInsightRankingArtifactPayload(BaseModel):
    priority_score: int = Field(ge=0, le=100)
    priority_band: HomepagePriorityBand
    priority_reasons: list[RankingReasonPayload]
    low_priority_bucket: bool
    feature_schema_version: str | None = None
    score_version: str | None = None
    feature_vector: dict[str, Any] | None = None
    score_breakdown: dict[str, Any] | None = None


class FeedItemInsightRoutingSecondaryChannelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    display_mode: Literal["list", "tag_only"] = "list"

    @field_validator("channel_id")
    @classmethod
    def require_non_blank_channel_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("channel_id must not be blank")
        return normalized


class FeedItemInsightRoutingLifecycleMetadataPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: int
    channel_catalog_version: int
    retired_channel_id: str
    fallback_channel_id: str
    previous_channel_ids: list[str] = Field(default_factory=list)
    reason: str


class FeedItemInsightRoutingArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["feed_item_routing_v1"] = "feed_item_routing_v1"
    primary_channel_id: str | None = None
    secondary_channels: list[FeedItemInsightRoutingSecondaryChannelPayload] = Field(default_factory=list)
    reading_depth: HomepageReadingDepth | None = None
    flow_lane: HomepageFlowLane = "main"
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    promotion_reason_codes: list[str] = Field(default_factory=list)
    isolation_reason_codes: list[str] = Field(default_factory=list)
    source_policy_applied: FeedItemInsightRoutingSourcePolicy = "normal"
    notes: str = ""
    source: Literal["insight_engine_artifact", "channel_lifecycle"] = "insight_engine_artifact"
    lifecycle_metadata: FeedItemInsightRoutingLifecycleMetadataPayload | None = None
    channel_catalog_version: int | None = None
    routing_breakdown: dict[str, Any] | None = None

    @field_validator("primary_channel_id")
    @classmethod
    def reject_blank_primary_channel_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("primary_channel_id must be null or a non-blank channel id")
        return normalized

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: list[str]) -> list[str]:
        return _validate_code_list(value, ROUTING_REASON_CODE_SET, "reason_codes")

    @field_validator("promotion_reason_codes")
    @classmethod
    def validate_promotion_reason_codes(cls, value: list[str]) -> list[str]:
        return _validate_code_list(value, PROMOTION_REASON_CODE_SET, "promotion_reason_codes")

    @field_validator("isolation_reason_codes")
    @classmethod
    def validate_isolation_reason_codes(cls, value: list[str]) -> list[str]:
        return _validate_code_list(value, ISOLATION_REASON_CODE_SET, "isolation_reason_codes")

    @model_validator(mode="after")
    def reject_duplicate_or_primary_secondary_channels(self) -> "FeedItemInsightRoutingArtifactPayload":
        secondary_ids = [secondary.channel_id for secondary in self.secondary_channels]
        duplicate_ids = {channel_id for channel_id in secondary_ids if secondary_ids.count(channel_id) > 1}
        if duplicate_ids:
            raise ValueError("secondary_channels must not contain duplicate channel ids")
        if self.primary_channel_id is not None and self.primary_channel_id in secondary_ids:
            raise ValueError("primary_channel_id must not also appear in secondary_channels")
        return self


class FeedItemInsightRoutingResolution(BaseModel):
    primary_channel_id: str | None
    secondary_channels: list[FeedItemInsightRoutingSecondaryChannelPayload]
    reading_depth: HomepageReadingDepth | None
    flow_lane: HomepageFlowLane
    confidence: float | None
    reason_codes: list[str]
    promotion_reason_codes: list[str] = Field(default_factory=list)
    isolation_reason_codes: list[str] = Field(default_factory=list)
    source_policy_applied: FeedItemInsightRoutingSourcePolicy | None = None
    notes: str | None = None
    source: FeedItemInsightRoutingSource


@dataclass(frozen=True, slots=True)
class RankingSignal:
    code: str
    label: str
    weight: int


@dataclass(frozen=True, slots=True)
class FeedItemInsightArtifactsResolution:
    summary_text: str | None
    summary_source: HomepageSummarySource
    topic_labels: list[str]
    topic_source: HomepageTopicSource
    priority_score: int
    priority_band: HomepagePriorityBand
    priority_reasons: list[RankingReasonPayload]
    priority_source: HomepagePrioritySource
    low_priority_bucket: bool
    artifact_generated_at: datetime | None


@dataclass(frozen=True, slots=True)
class InsightEngineArtifactRebuildStats:
    scope: str
    processed_feed_item_count: int
    artifact_rows_created: int
    failed_feed_item_count: int
    timing_ms: dict[str, int] | None = None


def build_latest_occurrence_subquery(feed_item_ids: Sequence[int] | None = None):
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


def load_latest_feed_item_insight_artifacts(
    session: Session,
    *,
    feed_item_ids: Sequence[int],
) -> dict[int, dict[str, InsightEngineArtifact]]:
    unique_ids = list(dict.fromkeys(feed_item_ids))
    if not unique_ids:
        return {}

    latest_ids = (
        select(
            InsightEngineArtifact.feed_item_id.label("feed_item_id"),
            InsightEngineArtifact.artifact_kind.label("artifact_kind"),
            func.max(InsightEngineArtifact.id).label("latest_id"),
        )
        .where(
            InsightEngineArtifact.feed_item_id.in_(unique_ids),
            InsightEngineArtifact.artifact_kind.in_(FEED_ITEM_INSIGHT_ARTIFACT_KINDS),
        )
        .group_by(InsightEngineArtifact.feed_item_id, InsightEngineArtifact.artifact_kind)
        .subquery()
    )

    statement = select(InsightEngineArtifact).join(latest_ids, InsightEngineArtifact.id == latest_ids.c.latest_id)
    artifacts = session.scalars(statement).all()

    results: dict[int, dict[str, InsightEngineArtifact]] = {}
    for artifact in artifacts:
        if artifact.feed_item_id is None:
            continue
        results.setdefault(artifact.feed_item_id, {})[artifact.artifact_kind] = artifact
    return results


def resolve_feed_item_insight_artifacts(
    *,
    feed_item: FeedItem,
    item_state: ItemState,
    occurrence: SourceItemOccurrence,
    source: Source,
    artifacts_by_kind: dict[str, InsightEngineArtifact] | None,
) -> FeedItemInsightArtifactsResolution:
    published_at = feed_item.published_at or occurrence.published_at or occurrence.created_at
    artifacts_by_kind = artifacts_by_kind or {}
    used_timestamps: list[datetime] = []

    summary_text, summary_source = resolve_summary(
        feed_item,
        artifacts_by_kind.get(FEED_ITEM_SUMMARY_ARTIFACT_KIND),
    )
    if summary_source == "insight_engine_artifact":
        artifact = artifacts_by_kind.get(FEED_ITEM_SUMMARY_ARTIFACT_KIND)
        if artifact is not None:
            used_timestamps.append(artifact.created_at)

    topic_labels, topic_source = resolve_topics(
        feed_item,
        source,
        artifacts_by_kind.get(FEED_ITEM_TOPICS_ARTIFACT_KIND),
    )
    if topic_source == "insight_engine_artifact":
        artifact = artifacts_by_kind.get(FEED_ITEM_TOPICS_ARTIFACT_KIND)
        if artifact is not None:
            used_timestamps.append(artifact.created_at)

    ranking_artifact = artifacts_by_kind.get(FEED_ITEM_RANKING_ARTIFACT_KIND)
    ranking_payload = _validate_payload(FeedItemInsightRankingArtifactPayload, ranking_artifact)
    if item_state.status == "not_interested" or ranking_payload is None:
        priority_score, priority_band, priority_reasons, low_priority_bucket = calculate_priority(
            status=item_state.status,
            published_at=published_at,
            has_summary=bool(summary_text),
            has_topics=bool(topic_labels),
        )
        priority_source: HomepagePrioritySource = "deterministic_fallback"
    else:
        priority_score = ranking_payload.priority_score
        priority_band = ranking_payload.priority_band
        priority_reasons = ranking_payload.priority_reasons
        low_priority_bucket = ranking_payload.low_priority_bucket
        priority_source = "insight_engine_artifact"
        used_timestamps.append(ranking_artifact.created_at)

    artifact_generated_at = max(used_timestamps) if used_timestamps else None
    return FeedItemInsightArtifactsResolution(
        summary_text=summary_text,
        summary_source=summary_source,
        topic_labels=topic_labels,
        topic_source=topic_source,
        priority_score=priority_score,
        priority_band=priority_band,
        priority_reasons=priority_reasons,
        priority_source=priority_source,
        low_priority_bucket=low_priority_bucket,
        artifact_generated_at=artifact_generated_at,
    )


def resolve_summary(
    feed_item: FeedItem,
    artifact: InsightEngineArtifact | None,
) -> tuple[str | None, HomepageSummarySource]:
    payload = _validate_payload(FeedItemInsightSummaryArtifactPayload, artifact)
    if payload is not None and payload.source_kind != "none" and payload.text:
        return payload.text, "insight_engine_artifact"

    for text_value, source_kind in iter_summary_candidates(feed_item):
        rendered = render_summary_html(text_value)
        if rendered:
            return rendered, source_kind

    return None, "none"


def resolve_topics(
    feed_item: FeedItem,
    source: Source,
    artifact: InsightEngineArtifact | None,
) -> tuple[list[str], HomepageTopicSource]:
    payload = _validate_payload(FeedItemInsightTopicsArtifactPayload, artifact)
    if payload is not None and payload.labels:
        return payload.labels, "insight_engine_artifact"

    labels, source_kind = extract_topic_labels(feed_item.topic_hints, source.folder_path)
    if source_kind == "topic_hints":
        return labels, "topic_hints"
    if source_kind == "source_folder":
        return labels, "source_folder"
    return [], "none"


def resolve_routing(
    *,
    feed_item: FeedItem,
    source: Source,
    artifacts_by_kind: dict[str, InsightEngineArtifact] | None,
    latest_topics_payload: FeedItemInsightTopicsArtifactPayload | None = None,
    channels_by_id: dict[str, InsightChannel] | None = None,
    source_tendencies: Sequence[SourceChannelTendency] | None = None,
) -> FeedItemInsightRoutingResolution:
    artifacts_by_kind = artifacts_by_kind or {}
    if latest_topics_payload is None:
        latest_topics_payload = _validate_payload(
            FeedItemInsightTopicsArtifactPayload,
            artifacts_by_kind.get(FEED_ITEM_TOPICS_ARTIFACT_KIND),
        )
    routing_payload = _validate_payload(
        FeedItemInsightRoutingArtifactPayload,
        artifacts_by_kind.get(FEED_ITEM_ROUTING_ARTIFACT_KIND),
    )
    if routing_payload is not None and _routing_payload_channels_are_enabled(routing_payload, channels_by_id):
        flow_lane = routing_payload.flow_lane
        reason_codes = list(routing_payload.reason_codes)
        isolation_reason_codes = list(routing_payload.isolation_reason_codes)
        if _feed_item_has_failed_or_empty_extraction(feed_item):
            flow_lane = "needs_review"
            _append_unique_code(reason_codes, "extraction_failed_needs_review")
            _append_unique_code(isolation_reason_codes, "extraction_failed_needs_review")
        return FeedItemInsightRoutingResolution(
            primary_channel_id=routing_payload.primary_channel_id,
            secondary_channels=routing_payload.secondary_channels,
            reading_depth=routing_payload.reading_depth,
            flow_lane=flow_lane,
            confidence=routing_payload.confidence,
            reason_codes=reason_codes,
            promotion_reason_codes=routing_payload.promotion_reason_codes,
            isolation_reason_codes=isolation_reason_codes,
            source_policy_applied=routing_payload.source_policy_applied,
            notes=routing_payload.notes,
            source=routing_payload.source,
        )

    return _resolve_routing_fallback(
        feed_item=feed_item,
        source=source,
        latest_topics_payload=latest_topics_payload,
        channels_by_id=channels_by_id,
        source_tendencies=source_tendencies or [],
    )


def build_summary_artifact_payload(feed_item: FeedItem) -> FeedItemInsightSummaryArtifactPayload:
    for text_value, source_kind in iter_summary_candidates(feed_item):
        rendered = render_summary_html(text_value)
        if rendered:
            return FeedItemInsightSummaryArtifactPayload(text=rendered, source_kind=source_kind)

    return FeedItemInsightSummaryArtifactPayload(text=None, source_kind="none")


def iter_summary_candidates(feed_item: FeedItem) -> list[tuple[str | None, SummaryArtifactSource]]:
    prefer_content = (
        feed_item.content_text_source == CONTENT_TEXT_SOURCE_EXTRACTED_PAGE
        or is_probably_url_only_or_jump_prompt(feed_item.summary_text)
    )
    if prefer_content:
        return [
            (feed_item.content_text, "canonical_content"),
            (feed_item.summary_text, "canonical_summary"),
        ]
    return [
        (feed_item.summary_text, "canonical_summary"),
        (feed_item.content_text, "canonical_content"),
    ]


def build_topics_artifact_payload(feed_item: FeedItem, source: Source) -> FeedItemInsightTopicsArtifactPayload:
    labels, source_kind = extract_topic_labels(feed_item.topic_hints, source.folder_path)
    return FeedItemInsightTopicsArtifactPayload(labels=labels, source_kind=source_kind)


def build_ranking_artifact_payload(
    *,
    status: str,
    published_at: datetime | None,
    has_summary: bool,
    has_topics: bool,
) -> FeedItemInsightRankingArtifactPayload:
    priority_score, priority_band, priority_reasons, low_priority_bucket = calculate_priority(
        status=status,
        published_at=published_at,
        has_summary=has_summary,
        has_topics=has_topics,
    )
    return FeedItemInsightRankingArtifactPayload(
        priority_score=priority_score,
        priority_band=priority_band,
        priority_reasons=priority_reasons,
        low_priority_bucket=low_priority_bucket,
    )


def extract_topic_labels(
    topic_hints: dict[str, Any] | None,
    folder_path: str | None,
) -> tuple[list[str], TopicExtractionSource]:
    labels_from_hints = []
    if isinstance(topic_hints, dict):
        raw_labels = topic_hints.get("labels")
        if isinstance(raw_labels, list):
            labels_from_hints = [normalize_text(label) for label in raw_labels if isinstance(label, str)]
            labels_from_hints = [label for label in _dedupe_keep_order(labels_from_hints) if label]
    if labels_from_hints:
        return labels_from_hints[:3], "topic_hints"

    if not folder_path:
        return [], "none"

    parts = [normalize_text(part) for part in FOLDER_SPLIT_PATTERN.split(folder_path) if normalize_text(part)]
    labels_from_folder = [label for label in _dedupe_keep_order(parts) if label]
    if not labels_from_folder:
        return [], "none"
    return labels_from_folder[:3], "source_folder"


def calculate_priority(
    *,
    status: str,
    published_at: datetime | None,
    has_summary: bool,
    has_topics: bool,
) -> tuple[int, HomepagePriorityBand, list[RankingReasonPayload], bool]:
    if status == "not_interested":
        reasons = [RankingReasonPayload(code="marked_not_interested", label="marked low priority", weight=0)]
        return 0, "low", reasons, True

    signals: list[RankingSignal] = [_status_signal(status)]
    recency_signal = _recency_signal(published_at)
    if recency_signal is not None:
        signals.append(recency_signal)
    if has_summary:
        signals.append(RankingSignal(code="has_summary", label="has summary", weight=10))
    if has_topics:
        signals.append(RankingSignal(code="has_topics", label="has topic labels", weight=5))

    score = max(0, min(100, sum(signal.weight for signal in signals)))
    return (
        score,
        _priority_band_for_score(score),
        [
            RankingReasonPayload(code=signal.code, label=signal.label, weight=signal.weight)
            for signal in signals
        ],
        False,
    )


def summarize_text(value: str, *, max_length: int = 280) -> str:
    normalized = normalize_text(value)
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 1].rstrip()}..."


def render_summary_html(value: str | None) -> str | None:
    if not value:
        return None

    summary_text = summarize_for_homepage(value)
    if not summary_text:
        return None

    return f"<p>{escape(summary_text)}</p>"


def summarize_for_homepage(value: str, *, max_length: int = 280) -> str | None:
    normalized = normalize_summary_source_text(value)
    if not normalized:
        return None

    cleaned = strip_summary_boilerplate(normalized)
    if not cleaned:
        return None
    if is_unhelpful_summary_prompt(cleaned):
        return None

    without_urls = normalize_text(URL_PATTERN.sub(" ", cleaned))
    if len(without_urls) >= 12:
        return summarize_text(without_urls, max_length=max_length)

    if len(cleaned) >= 12 and not cleaned.lower().startswith(("http://", "https://")):
        return summarize_text(cleaned, max_length=max_length)

    return None


def normalize_summary_source_text(value: str) -> str:
    without_tags = HTML_TAG_PATTERN.sub(" ", unescape(value))
    return normalize_text(without_tags)


def is_unhelpful_summary_prompt(value: str | None) -> bool:
    normalized = normalize_summary_source_text(value or "")
    if not normalized:
        return False
    without_urls = normalize_text(URL_PATTERN.sub(" ", normalized))
    return is_probably_url_only_or_jump_prompt(normalized) and len(without_urls) < 40


def strip_summary_boilerplate(value: str) -> str:
    stripped = value
    for pattern in BOILERPLATE_PATTERNS:
        stripped = pattern.sub("", stripped)
    return normalize_text(stripped)


def normalize_text(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", value).strip()


def _list_homepage_context_rows(session: Session, *, feed_item_ids: Sequence[int]):
    latest_occurrences = build_latest_occurrence_subquery(feed_item_ids=feed_item_ids)
    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .where(FeedItem.id.in_(feed_item_ids))
    )
    return session.execute(statement).all()


def _validate_payload(model: type[BaseModel], artifact: InsightEngineArtifact | None):
    if artifact is None or not isinstance(artifact.artifact_payload, dict):
        return None
    try:
        return model.model_validate(artifact.artifact_payload)
    except ValidationError:
        return None


def _routing_payload_channels_are_enabled(
    payload: FeedItemInsightRoutingArtifactPayload,
    channels_by_id: dict[str, InsightChannel] | None,
) -> bool:
    if channels_by_id is None:
        return True
    channel_ids = [payload.primary_channel_id] if payload.primary_channel_id is not None else []
    channel_ids.extend(secondary.channel_id for secondary in payload.secondary_channels)
    for channel_id in channel_ids:
        channel = channels_by_id.get(channel_id)
        if not channel_is_active(channel):
            return False
    return True


def _resolve_routing_fallback(
    *,
    feed_item: FeedItem,
    source: Source,
    latest_topics_payload: FeedItemInsightTopicsArtifactPayload | None,
    channels_by_id: dict[str, InsightChannel] | None,
    source_tendencies: Sequence[SourceChannelTendency],
) -> FeedItemInsightRoutingResolution:
    eligible_tendencies = [
        tendency for tendency in source_tendencies if _routing_tendency_channel_is_enabled(tendency, channels_by_id)
    ]
    ordered_tendencies = sorted(
        eligible_tendencies,
        key=lambda tendency: (-int(bool(tendency.is_primary)), -float(tendency.tendency), tendency.channel_id),
    )
    primary_tendency, used_topic_match = _select_fallback_primary_tendency(
        ordered_tendencies,
        latest_topics_payload=latest_topics_payload,
        channels_by_id=channels_by_id,
    )

    primary_channel_id = primary_tendency.channel_id if primary_tendency is not None else None
    if primary_channel_id is None and _fallback_needs_review_is_active(channels_by_id):
        primary_channel_id = NEEDS_REVIEW_CHANNEL_ID
    secondary_channels = [
        FeedItemInsightRoutingSecondaryChannelPayload(
            channel_id=tendency.channel_id,
            confidence=_clamp_confidence(tendency.tendency),
            display_mode="list",
        )
        for tendency in ordered_tendencies
        if tendency.channel_id != primary_channel_id and tendency.tendency >= 0.35
    ]
    reading_depth = _reading_depth_for_channel(primary_channel_id, channels_by_id)
    reading_depth_from_policy = False
    if reading_depth is None:
        reading_depth = _reading_depth_for_content_type(latest_topics_payload)
        reading_depth_from_policy = reading_depth is not None

    flow_policy = source.flow_policy or "normal"
    confidence = _clamp_confidence(primary_tendency.tendency) if primary_tendency is not None else 0.0
    reason_codes: list[str] = []
    promotion_reason_codes: list[str] = []
    isolation_reason_codes: list[str] = []
    if primary_tendency is not None:
        reason_codes.append("source_channel_tendency_match")
    if used_topic_match:
        reason_codes.append("content_topic_match")
    if reading_depth_from_policy:
        reason_codes.append("reading_depth_policy")
    if flow_policy in {"high_frequency_brief", "mixed_high_frequency"}:
        reason_codes.append("source_flow_policy_high_frequency")
    if flow_policy == "high_frequency_brief":
        isolation_reason_codes.append("source_policy_high_frequency_brief")

    has_extraction_issue = _feed_item_has_failed_or_empty_extraction(feed_item)
    if has_extraction_issue:
        reason_codes.append("extraction_failed_needs_review")
        isolation_reason_codes.append("extraction_failed_needs_review")

    flow_lane: HomepageFlowLane = "main"
    if has_extraction_issue:
        flow_lane = "needs_review"
    elif flow_policy == "high_frequency_brief":
        if _should_promote_high_frequency_item(latest_topics_payload, confidence):
            flow_lane = "main"
            promotion_reason_codes.append("long_analysis_promoted_from_high_frequency")
        else:
            flow_lane = "brief_pool"
    elif primary_tendency is None and primary_channel_id == NEEDS_REVIEW_CHANNEL_ID:
        flow_lane = "needs_review"

    if (
        primary_tendency is None
        and primary_channel_id == NEEDS_REVIEW_CHANNEL_ID
        and "ambiguous_channel_needs_review" not in reason_codes
    ):
        reason_codes.append("ambiguous_channel_needs_review")
        isolation_reason_codes.append("ambiguous_channel_needs_review")

    return FeedItemInsightRoutingResolution(
        primary_channel_id=primary_channel_id,
        secondary_channels=secondary_channels,
        reading_depth=reading_depth,
        flow_lane=flow_lane,
        confidence=confidence,
        reason_codes=reason_codes,
        promotion_reason_codes=promotion_reason_codes,
        isolation_reason_codes=isolation_reason_codes,
        source_policy_applied=_normalize_source_policy(flow_policy),
        notes=None,
        source="deterministic_fallback",
    )


def _select_fallback_primary_tendency(
    ordered_tendencies: Sequence[SourceChannelTendency],
    *,
    latest_topics_payload: FeedItemInsightTopicsArtifactPayload | None,
    channels_by_id: dict[str, InsightChannel] | None,
) -> tuple[SourceChannelTendency | None, bool]:
    primary_tendencies = [tendency for tendency in ordered_tendencies if tendency.is_primary]
    if len(primary_tendencies) == 1:
        return primary_tendencies[0], False

    if len(ordered_tendencies) == 1:
        only_tendency = ordered_tendencies[0]
        if only_tendency.tendency >= 0.35:
            return only_tendency, False
        return None, False

    matched_tendency = _match_tendency_from_topic_context(
        ordered_tendencies,
        latest_topics_payload=latest_topics_payload,
        channels_by_id=channels_by_id,
    )
    if matched_tendency is not None:
        return matched_tendency, True

    if ordered_tendencies:
        return ordered_tendencies[0], False
    return None, False


def _match_tendency_from_topic_context(
    tendencies: Sequence[SourceChannelTendency],
    *,
    latest_topics_payload: FeedItemInsightTopicsArtifactPayload | None,
    channels_by_id: dict[str, InsightChannel] | None,
) -> SourceChannelTendency | None:
    if latest_topics_payload is None:
        return None

    content_type = latest_topics_payload.content_type
    topic_ids = latest_topics_payload.topic_ids
    if not content_type and not topic_ids:
        return None

    scored: list[tuple[int, float, str, SourceChannelTendency]] = []
    for tendency in tendencies:
        score = _channel_context_match_score(
            tendency.channel_id,
            content_type=content_type,
            topic_ids=topic_ids,
            channels_by_id=channels_by_id,
        )
        if score > 0:
            scored.append((score, float(tendency.tendency), tendency.channel_id, tendency))

    if not scored:
        return None
    return max(scored, key=lambda row: (row[0], row[1], row[2]))[3]


def _channel_context_match_score(
    channel_id: str,
    *,
    content_type: str | None,
    topic_ids: Sequence[str],
    channels_by_id: dict[str, InsightChannel] | None,
) -> int:
    preferred_channel_ids: list[str] = []
    if content_type is not None:
        preferred_channel_ids.extend(_preferred_channel_ids_for_content_type(content_type))
    for topic_id in topic_ids:
        preferred_channel_ids.extend(_preferred_channel_ids_for_topic(topic_id))

    score = 0
    for index, preferred_channel_id in enumerate(preferred_channel_ids):
        if channel_id == preferred_channel_id:
            score += max(1, 100 - index)

    channel = channels_by_id.get(channel_id) if channels_by_id is not None else None
    channel_text = " ".join(
        part
        for part in [
            channel_id,
            getattr(channel, "label", None),
            getattr(channel, "description", None),
        ]
        if isinstance(part, str)
    ).lower()
    for token in [content_type, *topic_ids]:
        if token and token.replace("_", " ") in channel_text:
            score += 10
    return score


def _preferred_channel_ids_for_content_type(content_type: str) -> list[str]:
    if content_type == "research_paper":
        return ["academic_horizon", "tcp_artificial_muscle_core", "soft_actuation_horizon"]
    if content_type == "reference":
        return ["academic_horizon", "open_source_tools_practice"]
    if content_type == "analysis":
        return ["ai_agents_frontier", "academic_horizon", "securities_investing"]
    if content_type in {"release_note", "tutorial", "case_study"}:
        return ["open_source_tools_practice"]
    if content_type == "news_update":
        return ["ai_agents_frontier", "securities_investing", "cryptocurrency"]
    return []


def _preferred_channel_ids_for_topic(topic_id: str) -> list[str]:
    if topic_id == "ai_technology":
        return ["ai_agents_frontier"]
    if topic_id in {"software_systems", "tools_products", "security_privacy"}:
        return ["open_source_tools_practice"]
    if topic_id in {"science_research", "education_learning"}:
        return ["academic_horizon", "tcp_artificial_muscle_core", "soft_actuation_horizon"]
    if topic_id == "economy_finance":
        return ["securities_investing", "cryptocurrency"]
    if topic_id == "knowledge_information":
        return ["academic_horizon", "open_source_tools_practice"]
    return []


def _reading_depth_for_content_type(
    latest_topics_payload: FeedItemInsightTopicsArtifactPayload | None,
) -> HomepageReadingDepth | None:
    content_type = latest_topics_payload.content_type if latest_topics_payload is not None else None
    if content_type == "research_paper":
        return "deep"
    if content_type in {"analysis", "reference", "case_study", "tutorial"}:
        return "medium"
    if content_type in {"release_note", "news_update", "list_digest"}:
        return "light"
    return None


def _should_promote_high_frequency_item(
    latest_topics_payload: FeedItemInsightTopicsArtifactPayload | None,
    confidence: float | None,
) -> bool:
    if confidence is None or confidence < 0.75:
        return False
    content_type = latest_topics_payload.content_type if latest_topics_payload is not None else None
    return content_type in {"analysis", "reference", "research_paper"}


def _feed_item_has_failed_or_empty_extraction(feed_item: FeedItem) -> bool:
    content_text = normalize_text(feed_item.content_text or "")
    summary_text = normalize_text(feed_item.summary_text or "")
    if feed_item.content_extraction_status == CONTENT_EXTRACTION_STATUS_FAILED:
        return not content_text or is_probably_url_only_or_jump_prompt(content_text or summary_text)
    return not content_text and is_probably_url_only_or_jump_prompt(summary_text)


def _routing_tendency_channel_is_enabled(
    tendency: SourceChannelTendency,
    channels_by_id: dict[str, InsightChannel] | None,
) -> bool:
    if channels_by_id is None:
        return True
    channel = channels_by_id.get(tendency.channel_id)
    return channel_is_active(channel)


def _fallback_needs_review_is_active(channels_by_id: dict[str, InsightChannel] | None) -> bool:
    if channels_by_id is None:
        return True
    return channel_is_active(channels_by_id.get(NEEDS_REVIEW_CHANNEL_ID))


def _reading_depth_for_channel(
    channel_id: str | None,
    channels_by_id: dict[str, InsightChannel] | None,
) -> HomepageReadingDepth | None:
    if channel_id is None or channels_by_id is None:
        return None
    channel = channels_by_id.get(channel_id)
    if not channel_is_active(channel):
        return None
    if channel.reading_depth == "light":
        return "light"
    if channel.reading_depth == "deep":
        return "deep"
    if channel.reading_depth == "medium":
        return "medium"
    return None


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _normalize_source_policy(value: str | None) -> FeedItemInsightRoutingSourcePolicy:
    if value == "mixed_high_frequency":
        return "mixed_high_frequency"
    if value == "high_frequency_brief":
        return "high_frequency_brief"
    return "normal"


def _validate_code_list(values: list[str], allowed_codes: set[str], field_name: str) -> list[str]:
    normalized_values: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} must not contain blank codes")
        if normalized not in allowed_codes:
            raise ValueError(f"{field_name} contains unknown code: {normalized}")
        if normalized not in normalized_values:
            normalized_values.append(normalized)
    return normalized_values


def _append_unique_code(values: list[str], code: str) -> None:
    if code not in values:
        values.append(code)


def _status_signal(status: str) -> RankingSignal:
    mapping = {
        "unread": RankingSignal(code="status_unread", label="unread", weight=45),
        "browsed": RankingSignal(code="status_browsed", label="browsed", weight=25),
        "seen": RankingSignal(code="status_seen", label="seen", weight=12),
        "read_full": RankingSignal(code="status_read_full", label="read full", weight=10),
        "saved": RankingSignal(code="status_saved", label="saved", weight=5),
    }
    return mapping.get(status, RankingSignal(code="status_unknown", label="unknown status", weight=0))


def _recency_signal(published_at: datetime | None) -> RankingSignal | None:
    if published_at is None:
        return None

    age = _now_utc() - _normalize_utc(published_at)
    if age <= timedelta(hours=4):
        return RankingSignal(code="recent_4h", label="within 4 hours", weight=30)
    if age <= timedelta(hours=24):
        return RankingSignal(code="recent_24h", label="within 24 hours", weight=20)
    if age <= timedelta(hours=72):
        return RankingSignal(code="recent_72h", label="within 72 hours", weight=10)
    return None


def _priority_band_for_score(score: int) -> HomepagePriorityBand:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
