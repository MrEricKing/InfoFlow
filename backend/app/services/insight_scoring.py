from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from typing import Any, Literal, Sequence

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    InsightEngineArtifact,
    FeedItem,
    FeedItemBehaviorEvent,
    FeedItemStructuredFeedback,
    ItemState,
    Source,
    SourceItemOccurrence,
)
from app.services.app_settings import get_insight_engine_settings
from app.services.insight_engine_artifacts import (
    FEED_ITEM_RANKING_ARTIFACT_KIND,
    FEED_ITEM_ROUTING_ARTIFACT_KIND,
    FEED_ITEM_TOPICS_ARTIFACT_KIND,
    HomepagePriorityBand,
    FeedItemInsightRankingArtifactPayload,
    FeedItemInsightRoutingArtifactPayload,
    FeedItemInsightTopicsArtifactPayload,
    RankingReasonPayload,
    build_latest_occurrence_subquery,
    load_latest_feed_item_insight_artifacts,
)
from app.services.behavior_events import (
    EVENT_NOT_INTERESTED,
    EVENT_OPENED_ORIGINAL_LINK,
    EVENT_READ_FULL_IN_APP,
    EVENT_SAVED,
    EVENT_UNSAVED,
)
from app.services.insight_engine_contracts import AI_ALLOWED_REASON_CODES, get_reason_code_label
from app.services.insight_profiles import (
    DEFAULT_INSIGHT_PROFILE,
    build_active_insight_profile_context,
    get_active_insight_profile,
)
from app.services.source_preference_profiles import (
    CANONICAL_EMPTY_SOURCE_PROFILE,
    SourcePreferenceProfileContext,
    load_active_source_preference_profile_contexts,
)

BACKEND_SCORING_SOURCE = "backend_scoring_v1"
FEATURE_SCHEMA_VERSION = "insight_feature_v1"
SCORE_VERSION = "score_v1_hand_weighted"
LOW_PRIORITY_REASON_NOT_INTERESTED = "not_interested"
LOW_PRIORITY_REASON_AUTO_SCORE_THRESHOLD = "auto_score_threshold"
LowPriorityBucketReason = Literal["not_interested", "auto_score_threshold"]

BASE_SCORE = 50
LOW_PRIORITY_SCORE_THRESHOLD = 50
MAX_SCORING_AGE_HOURS = 168.0
MAX_REASON_DELTA = 10
HISTORY_NET_THRESHOLD = 2
HISTORY_REASON_WEIGHT = 8
NOT_INTERESTED_OVERRIDE_WEIGHT = -60

SCORE_WEIGHTS: dict[str, float] = {
    "semantic.quality_score": 18.0,
    "semantic.relevance_score": 16.0,
    "semantic.depth_score": 8.0,
    "semantic.marketing_score": -14.0,
    "routing.top_llm_candidate_confidence": 6.0,
    "routing.uses_needs_review_fallback": -10.0,
    "feedback.explicit_positive_signal": 18.0,
    "feedback.explicit_negative_signal": -28.0,
    "behavior.source_saved_recently": 8.0,
    "behavior.source_not_interested_recently": -14.0,
    "behavior.topic_saved_recently": 6.0,
    "behavior.topic_not_interested_recently": -10.0,
    "current_state.is_saved": 20.0,
    "current_state.is_read_full": 6.0,
    "current_state.is_seen": -3.0,
    "current_state.is_not_interested": -60.0,
    "item.has_extracted_content": 5.0,
    "item.age_hours": -0.10,
}

SCORING_SECTIONS = (
    "item",
    "source",
    "semantic",
    "routing",
    "feedback",
    "behavior",
    "current_state",
)

NON_SCORING_FLAT_FEATURES = {
    "source.source_id",
}

NOT_INTERESTED_REASON_CODE = "marked_not_interested"
PROFILE_REASON_CODE = "profile_focus_match"
POSITIVE_SOURCE_HISTORY_REASON_CODE = "positive_source_history"
NEGATIVE_SOURCE_HISTORY_REASON_CODE = "negative_source_history"
POSITIVE_CATEGORY_HISTORY_REASON_CODE = "positive_category_history"
NEGATIVE_CATEGORY_HISTORY_REASON_CODE = "negative_category_history"

POSITIVE_AI_REASON_CODES = {
    "high_actionability",
    "deep_reference_value",
    "specific_insight",
    "implementation_detail",
}
NEGATIVE_AI_REASON_CODES = {
    "thin_content",
    "marketing_or_pr",
    "repetitive_update",
    "low_content_confidence",
}

POSITIVE_HISTORY_EVENTS = {EVENT_SAVED, EVENT_READ_FULL_IN_APP, EVENT_OPENED_ORIGINAL_LINK}
NEGATIVE_HISTORY_EVENTS = {EVENT_NOT_INTERESTED, EVENT_UNSAVED}
SCORING_HISTORY_EVENTS = POSITIVE_HISTORY_EVENTS | NEGATIVE_HISTORY_EVENTS


@dataclass(frozen=True, slots=True)
class ArticleScoringSignals:
    topic_ids: tuple[str, ...] = ()
    content_type: str | None = None
    ai_reasons: tuple[RankingReasonPayload, ...] = ()
    quality_score: float | None = None
    relevance_score: float | None = None
    depth_score: float | None = None
    marketing_score: float | None = None
    model_confidence: float | None = None
    llm_candidate_count: int = 0
    top_llm_candidate_confidence: float = 0.0
    uses_needs_review_fallback: bool = False


@dataclass(slots=True)
class HistoryCounts:
    positive: int = 0
    negative: int = 0

    @property
    def net(self) -> int:
        return self.positive - self.negative

    def add_delta(self, delta: int) -> None:
        if delta > 0:
            self.positive += delta
        elif delta < 0:
            self.negative += abs(delta)

    def without(self, other: "HistoryCounts | None") -> "HistoryCounts":
        if other is None:
            return HistoryCounts(positive=self.positive, negative=self.negative)
        return HistoryCounts(
            positive=max(0, self.positive - other.positive),
            negative=max(0, self.negative - other.negative),
        )


@dataclass(frozen=True, slots=True)
class InsightScoringContext:
    profile_version: str | None = None
    active_profile_context: dict[str, Any] = field(default_factory=dict)
    source_profile_by_source_id: dict[int, SourcePreferenceProfileContext] = field(default_factory=dict)
    structured_feedback_by_item_id: dict[int, FeedItemStructuredFeedback] = field(default_factory=dict)
    source_history_by_source_id: dict[int, HistoryCounts] = field(default_factory=dict)
    source_history_by_item_id: dict[int, dict[int, HistoryCounts]] = field(default_factory=dict)
    category_history_by_topic_id: dict[str, HistoryCounts] = field(default_factory=dict)
    category_history_by_item_id: dict[int, dict[str, HistoryCounts]] = field(default_factory=dict)
    auto_low_priority_archiving_enabled: bool = False
    low_priority_score_threshold: int = LOW_PRIORITY_SCORE_THRESHOLD
    auto_low_priority_cleared_item_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class InsightPriorityScore:
    score: int
    band: HomepagePriorityBand
    reasons: list[RankingReasonPayload]
    low_priority_bucket: bool
    low_priority_bucket_reason: LowPriorityBucketReason | None = None
    auto_low_priority_cleared: bool = False
    source: str = BACKEND_SCORING_SOURCE
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    score_version: str = SCORE_VERSION
    feature_vector: dict[str, Any] = field(default_factory=dict)
    score_breakdown: dict[str, Any] = field(default_factory=dict)


def extract_article_scoring_signals(artifacts_by_kind: dict[str, InsightEngineArtifact] | None) -> ArticleScoringSignals:
    artifacts_by_kind = artifacts_by_kind or {}
    topic_ids: tuple[str, ...] = ()
    content_type: str | None = None
    ai_reasons: tuple[RankingReasonPayload, ...] = ()

    topics_artifact = artifacts_by_kind.get(FEED_ITEM_TOPICS_ARTIFACT_KIND)
    topics_payload = _validate_payload(FeedItemInsightTopicsArtifactPayload, topics_artifact)
    if topics_payload is not None:
        topic_ids = tuple(topics_payload.topic_ids)
        content_type = topics_payload.content_type

    ranking_artifact = artifacts_by_kind.get(FEED_ITEM_RANKING_ARTIFACT_KIND)
    ranking_payload = _validate_payload(FeedItemInsightRankingArtifactPayload, ranking_artifact)
    if ranking_payload is not None:
        ai_reasons = tuple(reason for reason in ranking_payload.priority_reasons if reason.code in AI_ALLOWED_REASON_CODES)

    feature_vector = ranking_payload.feature_vector if ranking_payload is not None else None
    semantic_features = feature_vector.get("semantic") if isinstance(feature_vector, dict) else None
    routing_features = feature_vector.get("routing") if isinstance(feature_vector, dict) else None

    routing_artifact = artifacts_by_kind.get(FEED_ITEM_ROUTING_ARTIFACT_KIND)
    routing_payload = _validate_payload(FeedItemInsightRoutingArtifactPayload, routing_artifact)
    if routing_payload is not None:
        routing_breakdown = routing_payload.routing_breakdown
        if isinstance(routing_breakdown, dict):
            candidates = routing_breakdown.get("candidates")
            if isinstance(candidates, list):
                llm_candidates = [
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, dict) and candidate.get("source") == "llm_candidate"
                ]
                top_confidence = max(
                    (
                        _normalize_unit_interval(candidate.get("confidence"))
                        for candidate in llm_candidates
                    ),
                    default=0.0,
                )
                if routing_features is None:
                    routing_features = {}
                routing_features = {
                    **routing_features,
                    "llm_candidate_count": len(llm_candidates),
                    "top_llm_candidate_confidence": top_confidence,
                }

    return ArticleScoringSignals(
        topic_ids=topic_ids,
        content_type=content_type,
        ai_reasons=ai_reasons,
        quality_score=_optional_feature_score(semantic_features, "quality_score"),
        # Relevance remains a read-time projection so active profile changes can rescore
        # existing artifacts without rebuilding semantic summaries.
        relevance_score=None,
        depth_score=_optional_feature_score(semantic_features, "depth_score"),
        marketing_score=_optional_feature_score(semantic_features, "marketing_score"),
        model_confidence=_optional_feature_score(semantic_features, "model_confidence"),
        llm_candidate_count=_optional_feature_int(routing_features, "llm_candidate_count"),
        top_llm_candidate_confidence=_optional_feature_score(routing_features, "top_llm_candidate_confidence") or 0.0,
        uses_needs_review_fallback=bool(
            _optional_feature_int(routing_features, "uses_needs_review_fallback")
        ),
    )


def build_insight_scoring_context(
    session: Session,
    *,
    feed_item_ids: Sequence[int],
    source_ids: Sequence[int],
    target_topic_ids: Sequence[str],
) -> InsightScoringContext:
    unique_source_ids = set(source_ids)
    unique_target_topic_ids = set(target_topic_ids)
    category_history_by_topic_id, category_history_by_item_id = _load_category_history_maps(
        session,
        target_topic_ids=unique_target_topic_ids,
    )
    insight_engine_settings = get_insight_engine_settings(session)
    active_profile = get_active_insight_profile(session)
    return InsightScoringContext(
        profile_version=(
            active_profile.profile_version
            if active_profile is not None
            else DEFAULT_INSIGHT_PROFILE["profile_version"]
        ),
        active_profile_context=build_active_insight_profile_context(session),
        source_profile_by_source_id=load_active_source_preference_profile_contexts(session, unique_source_ids),
        structured_feedback_by_item_id=_load_structured_feedback_by_item_id(session, feed_item_ids=feed_item_ids),
        source_history_by_source_id=_load_source_history(session, source_ids=unique_source_ids),
        source_history_by_item_id=_load_source_history_by_item(session, source_ids=unique_source_ids),
        category_history_by_topic_id=category_history_by_topic_id,
        category_history_by_item_id=category_history_by_item_id,
        auto_low_priority_archiving_enabled=insight_engine_settings.auto_low_priority_archiving_enabled,
        low_priority_score_threshold=insight_engine_settings.low_priority_score_threshold,
        auto_low_priority_cleared_item_ids=_load_auto_low_priority_cleared_item_ids(session, feed_item_ids=feed_item_ids),
    )


def score_feed_item_priority(
    *,
    feed_item_id: int,
    item_status: str,
    source_id: int | None,
    signals: ArticleScoringSignals,
    context: InsightScoringContext,
    feed_item: FeedItem | None = None,
    item_state: ItemState | None = None,
    occurrence: SourceItemOccurrence | None = None,
    source: Source | None = None,
    generated_at: datetime | None = None,
    channel_catalog_version: int | None = None,
    provider_profile: str | None = None,
    source_profile: dict[str, Any] | None = None,
    structured_feedback: FeedItemStructuredFeedback | None = None,
) -> InsightPriorityScore:
    feature_vector = build_insight_feature_vector(
        feed_item_id=feed_item_id,
        item_status=item_status,
        source_id=source_id,
        signals=signals,
        context=context,
        feed_item=feed_item,
        item_state=item_state,
        occurrence=occurrence,
        source=source,
        generated_at=generated_at,
        channel_catalog_version=channel_catalog_version,
        provider_profile=provider_profile,
        source_profile=source_profile,
        structured_feedback=structured_feedback,
    )
    score, band, score_breakdown, reasons = score_insight_feature_vector(feature_vector, signals=signals)
    auto_low_priority_cleared = feed_item_id in context.auto_low_priority_cleared_item_ids
    low_priority_bucket = (
        bool(feature_vector["current_state"]["is_not_interested"])
        or (
            context.auto_low_priority_archiving_enabled
            and not auto_low_priority_cleared
            and score < context.low_priority_score_threshold
        )
    )
    low_priority_bucket_reason = None
    if low_priority_bucket:
        low_priority_bucket_reason = (
            LOW_PRIORITY_REASON_NOT_INTERESTED
            if bool(feature_vector["current_state"]["is_not_interested"])
            else LOW_PRIORITY_REASON_AUTO_SCORE_THRESHOLD
        )
    return InsightPriorityScore(
        score=score,
        band=band,
        reasons=reasons,
        low_priority_bucket=low_priority_bucket,
        low_priority_bucket_reason=low_priority_bucket_reason,
        auto_low_priority_cleared=auto_low_priority_cleared,
        feature_vector=feature_vector,
        score_breakdown=score_breakdown,
    )


def build_insight_feature_vector(
    *,
    feed_item_id: int,
    item_status: str,
    source_id: int | None,
    signals: ArticleScoringSignals,
    context: InsightScoringContext,
    feed_item: FeedItem | None = None,
    item_state: ItemState | None = None,
    occurrence: SourceItemOccurrence | None = None,
    source: Source | None = None,
    generated_at: datetime | None = None,
    channel_catalog_version: int | None = None,
    provider_profile: str | None = None,
    source_profile: dict[str, Any] | None = None,
    structured_feedback: FeedItemStructuredFeedback | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or _now_utc()
    source_profile = source_profile or (
        context.source_profile_by_source_id[source_id].profile
        if source_id is not None and source_id in context.source_profile_by_source_id
        else CANONICAL_EMPTY_SOURCE_PROFILE
    )
    structured_feedback = structured_feedback or context.structured_feedback_by_item_id.get(feed_item_id)
    published_at = _published_at(feed_item, occurrence)
    text_value = _text_for_feature(feed_item)
    source_history = _history_without_current(
        context.source_history_by_source_id.get(source_id, HistoryCounts()) if source_id is not None else HistoryCounts(),
        context.source_history_by_item_id.get(feed_item_id, {}).get(source_id) if source_id is not None else None,
    )
    topic_history = _aggregate_topic_history_without_current(feed_item_id, signals, context)
    semantic_scores = _semantic_scores(signals, context.active_profile_context)
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feed_item_id": feed_item_id,
        "generated_at": generated_at.isoformat(),
        "item": {
            "age_hours": _age_hours(published_at, generated_at),
            "text_length_chars": len(text_value),
            "has_canonical_url": _bool_to_int(bool(feed_item and feed_item.canonical_url)),
            "has_extracted_content": _bool_to_int(bool(text_value)),
        },
        "source": {
            "source_id": source_id,
            "flow_policy": source.flow_policy if source is not None else None,
            "source_profile_is_empty": _bool_to_int(bool(source_profile.get("is_empty", True))),
            "source_profile_has_good_criteria": _bool_to_int(bool(source_profile.get("good_article_criteria"))),
            "source_prefers_deep_content": _bool_to_int(_source_profile_prefers_deep_content(source_profile)),
            "source_penalizes_marketing": _bool_to_int(_source_profile_penalizes_marketing(source_profile)),
        },
        "semantic": {
            "content_type": signals.content_type,
            "quality_score": semantic_scores["quality_score"],
            "relevance_score": semantic_scores["relevance_score"],
            "depth_score": semantic_scores["depth_score"],
            "marketing_score": semantic_scores["marketing_score"],
            "model_confidence": semantic_scores["model_confidence"],
        },
        "routing": {
            "llm_candidate_count": min(5, max(0, int(signals.llm_candidate_count))),
            "top_llm_candidate_confidence": _normalize_unit_interval(signals.top_llm_candidate_confidence),
            "source_tendency_confidence": 0.0,
            "has_human_channel_correction": _bool_to_int(_has_channel_correction(structured_feedback)),
            "uses_needs_review_fallback": _bool_to_int(signals.uses_needs_review_fallback),
        },
        "feedback": {
            "explicit_positive_signal": _bool_to_int(_has_explicit_positive_feedback(structured_feedback)),
            "explicit_negative_signal": _bool_to_int(_has_explicit_negative_feedback(structured_feedback)),
            "has_priority_correction": _bool_to_int(bool(structured_feedback and structured_feedback.priority_correction)),
            "has_channel_correction": _bool_to_int(_has_channel_correction(structured_feedback)),
        },
        "behavior": {
            "source_saved_recently": _bool_to_int(source_history.positive >= HISTORY_NET_THRESHOLD),
            "source_not_interested_recently": _bool_to_int(source_history.negative >= HISTORY_NET_THRESHOLD),
            "topic_saved_recently": _bool_to_int(topic_history.positive >= HISTORY_NET_THRESHOLD),
            "topic_not_interested_recently": _bool_to_int(topic_history.negative >= HISTORY_NET_THRESHOLD),
        },
        "current_state": {
            "is_saved": _bool_to_int(item_status == "saved" or bool(item_state and item_state.saved_at)),
            "is_not_interested": _bool_to_int(item_status == "not_interested" or bool(item_state and item_state.not_interested_at)),
            "is_read_full": _bool_to_int(item_status == "read_full" or bool(item_state and item_state.read_full_at)),
            "is_seen": _bool_to_int(item_status in {"browsed", "seen", "read_full", "saved", "not_interested"} or bool(item_state and (item_state.seen_at or item_state.browsed_at))),
        },
        "context": {
            "profile_version": context.profile_version,
            "channel_catalog_version": channel_catalog_version,
            "source_profile_version": source_profile.get("profile_version"),
            "provider_profile": provider_profile,
        },
    }


def flatten_insight_feature_vector(feature_vector: dict[str, Any]) -> dict[str, float]:
    flat: dict[str, float] = {}
    for section in SCORING_SECTIONS:
        section_value = feature_vector.get(section)
        if not isinstance(section_value, dict):
            continue
        for field_name, value in section_value.items():
            path = f"{section}.{field_name}"
            if path in NON_SCORING_FLAT_FEATURES:
                continue
            if isinstance(value, bool):
                flat[path] = 1.0 if value else 0.0
            elif isinstance(value, (int, float)):
                flat[path] = float(value)
    return dict(sorted(flat.items()))


def score_insight_feature_vector(
    feature_vector: dict[str, Any],
    *,
    signals: ArticleScoringSignals | None = None,
) -> tuple[int, HomepagePriorityBand, dict[str, Any], list[RankingReasonPayload]]:
    flat_features = flatten_insight_feature_vector(feature_vector)
    contributions: list[dict[str, Any]] = []
    raw_score = float(BASE_SCORE)
    for feature_name, weight in SCORE_WEIGHTS.items():
        value = float(flat_features.get(feature_name, 0.0))
        contribution = value * weight
        raw_score += contribution
        section = feature_name.split(".", 1)[0]
        contributions.append(
            {
                "feature": feature_name,
                "section": section,
                "value": _round_float(value),
                "weight": _round_float(weight),
                "contribution": _round_float(contribution),
                "reason_codes": _reason_codes_for_feature(feature_name, signals),
            }
        )

    score = _clamp_score(round(raw_score))
    band = _priority_band_for_score(score)
    rollups: dict[str, float] = {}
    for contribution in contributions:
        section = str(contribution["section"])
        rollups[section] = rollups.get(section, 0.0) + float(contribution["contribution"])
    reason_codes = _dedupe_keep_order(
        code
        for contribution in contributions
        if abs(float(contribution["contribution"])) > 0.0001
        for code in contribution["reason_codes"]
    )
    breakdown = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "score_version": SCORE_VERSION,
        "intercept": BASE_SCORE,
        "contributions": contributions,
        "rollups": {section: _round_float(value) for section, value in sorted(rollups.items())},
        "raw_score": _round_float(raw_score),
        "final_priority_score": score,
        "priority_band": band,
        "reason_codes": reason_codes,
    }
    return score, band, breakdown, _ranking_reasons_from_contributions(contributions)


def _load_structured_feedback_by_item_id(
    session: Session,
    *,
    feed_item_ids: Sequence[int],
) -> dict[int, FeedItemStructuredFeedback]:
    unique_ids = list(dict.fromkeys(feed_item_ids))
    if not unique_ids:
        return {}
    rows = session.scalars(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id.in_(unique_ids))
    ).all()
    return {row.feed_item_id: row for row in rows}


def _optional_feature_score(section: object, field_name: str) -> float | None:
    if not isinstance(section, dict):
        return None
    value = section.get(field_name)
    if isinstance(value, (int, float)):
        return _normalize_unit_interval(value)
    return None


def _optional_feature_int(section: object, field_name: str) -> int:
    if not isinstance(section, dict):
        return 0
    value = section.get(field_name)
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _normalize_unit_interval(value: object) -> float:
    if not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _published_at(feed_item: FeedItem | None, occurrence: SourceItemOccurrence | None) -> datetime | None:
    if feed_item is not None and feed_item.published_at is not None:
        return feed_item.published_at
    if occurrence is not None:
        return occurrence.published_at or occurrence.created_at
    return None


def _text_for_feature(feed_item: FeedItem | None) -> str:
    if feed_item is None:
        return ""
    return feed_item.content_text or feed_item.feed_content_text or feed_item.summary_text or ""


def _age_hours(published_at: datetime | None, generated_at: datetime) -> float:
    if published_at is None:
        return 0.0
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    delta = generated_at - published_at
    return _round_float(min(MAX_SCORING_AGE_HOURS, max(0.0, delta.total_seconds() / 3600)))


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


def _source_profile_prefers_deep_content(source_profile: dict[str, Any]) -> bool:
    preferred_content_types = source_profile.get("preferred_content_types")
    if isinstance(preferred_content_types, list):
        return any(value in {"analysis", "tutorial", "case_study", "reference", "research_paper"} for value in preferred_content_types)
    return False


def _source_profile_penalizes_marketing(source_profile: dict[str, Any]) -> bool:
    patterns = source_profile.get("bad_article_patterns")
    if not isinstance(patterns, list):
        return False
    return any("market" in str(pattern).lower() or "营销" in str(pattern) for pattern in patterns)


def _history_without_current(total: HistoryCounts, current: HistoryCounts | None) -> HistoryCounts:
    return total.without(current)


def _aggregate_topic_history_without_current(
    feed_item_id: int,
    signals: ArticleScoringSignals,
    context: InsightScoringContext,
) -> HistoryCounts:
    aggregate = HistoryCounts()
    current_by_topic = context.category_history_by_item_id.get(feed_item_id, {})
    for topic_id in signals.topic_ids:
        total = context.category_history_by_topic_id.get(topic_id)
        if total is None:
            continue
        counts = total.without(current_by_topic.get(topic_id))
        aggregate.positive += counts.positive
        aggregate.negative += counts.negative
    return aggregate


def _semantic_scores(
    signals: ArticleScoringSignals,
    profile_context: dict[str, Any],
) -> dict[str, float]:
    positive_reason_score = max(
        (_normalize_unit_interval(reason.weight / 100) for reason in signals.ai_reasons if reason.code in POSITIVE_AI_REASON_CODES),
        default=0.0,
    )
    depth_reason_score = max(
        (
            _normalize_unit_interval(reason.weight / 100)
            for reason in signals.ai_reasons
            if reason.code in {"deep_reference_value", "implementation_detail"}
        ),
        default=0.0,
    )
    marketing_reason_score = max(
        (_normalize_unit_interval(reason.weight / 100) for reason in signals.ai_reasons if reason.code in NEGATIVE_AI_REASON_CODES),
        default=0.0,
    )
    profile_match_score = _profile_match_score(signals, profile_context)
    content_type_depth = _content_type_depth_score(signals.content_type)
    quality_score = signals.quality_score if signals.quality_score is not None else positive_reason_score
    relevance_score = (
        signals.relevance_score
        if signals.relevance_score is not None
        else max(positive_reason_score, profile_match_score)
    )
    depth_score = (
        signals.depth_score
        if signals.depth_score is not None
        else max(depth_reason_score, content_type_depth)
    )
    marketing_score = signals.marketing_score if signals.marketing_score is not None else marketing_reason_score
    model_confidence = signals.model_confidence
    if model_confidence is None:
        model_confidence = max(quality_score, relevance_score, depth_score, 1.0 - marketing_score if marketing_score else 0.0)
    return {
        "quality_score": _normalize_unit_interval(quality_score),
        "relevance_score": _normalize_unit_interval(relevance_score),
        "depth_score": _normalize_unit_interval(depth_score),
        "marketing_score": _normalize_unit_interval(marketing_score),
        "model_confidence": _normalize_unit_interval(model_confidence),
    }


def _content_type_depth_score(content_type: str | None) -> float:
    if content_type in {"research_paper", "reference"}:
        return 1.0
    if content_type in {"analysis", "tutorial", "case_study"}:
        return 0.65
    if content_type in {"opinion", "list_digest"}:
        return 0.35
    return 0.0


def _profile_match_score(signals: ArticleScoringSignals, profile_context: dict[str, Any]) -> float:
    topic_ids = set(signals.topic_ids)
    best_weight = 0
    if topic_ids:
        for field_name in ("active_focus", "long_term_interests"):
            for entry in _profile_entries(profile_context, field_name):
                if entry.get("topic_id") in topic_ids:
                    best_weight = max(best_weight, int(entry.get("weight") or 0))
        for entry in _profile_entries(profile_context, "topic_preferences"):
            if entry.get("topic_id") in topic_ids and entry.get("preference") == "boost":
                best_weight = max(best_weight, int(entry.get("weight") or 0))
    if signals.content_type:
        for entry in _profile_entries(profile_context, "content_type_preferences"):
            if entry.get("content_type") == signals.content_type and entry.get("preference") == "boost":
                best_weight = max(best_weight, int(entry.get("weight") or 0))
    return _normalize_unit_interval(best_weight / 100)


def _has_channel_correction(row: FeedItemStructuredFeedback | None) -> bool:
    if row is None:
        return False
    return bool(
        row.primary_channel_correction
        or row.channel_add_ids_json
        or row.channel_remove_ids_json
        or row.reading_depth_correction
        or row.flow_lane_correction
    )


def _has_explicit_positive_feedback(row: FeedItemStructuredFeedback | None) -> bool:
    if row is None:
        return False
    return row.overall_value in {"useful", "very_useful", "must_read", "high"} or row.priority_correction == "too_low"


def _has_explicit_negative_feedback(row: FeedItemStructuredFeedback | None) -> bool:
    if row is None:
        return False
    return row.overall_value == "no_value" or row.priority_correction == "too_high"


def _round_float(value: float) -> float:
    return round(float(value), 4)


def _reason_codes_for_feature(feature_name: str, signals: ArticleScoringSignals | None) -> list[str]:
    positive_codes = [
        reason.code for reason in (signals.ai_reasons if signals is not None else ()) if reason.code in POSITIVE_AI_REASON_CODES
    ]
    negative_codes = [
        reason.code for reason in (signals.ai_reasons if signals is not None else ()) if reason.code in NEGATIVE_AI_REASON_CODES
    ]
    mapping = {
        "semantic.quality_score": positive_codes or ["specific_insight"],
        "semantic.relevance_score": positive_codes or [PROFILE_REASON_CODE],
        "semantic.depth_score": [
            code
            for code in positive_codes
            if code in {"deep_reference_value", "implementation_detail"}
        ]
        or ["deep_reference_value"],
        "semantic.marketing_score": negative_codes or ["marketing_or_pr"],
        "routing.top_llm_candidate_confidence": ["routing_candidate_confidence"],
        "routing.uses_needs_review_fallback": ["needs_review_fallback"],
        "feedback.explicit_positive_signal": ["explicit_positive_feedback"],
        "feedback.explicit_negative_signal": ["explicit_negative_feedback"],
        "behavior.source_saved_recently": [POSITIVE_SOURCE_HISTORY_REASON_CODE],
        "behavior.source_not_interested_recently": [NEGATIVE_SOURCE_HISTORY_REASON_CODE],
        "behavior.topic_saved_recently": [POSITIVE_CATEGORY_HISTORY_REASON_CODE],
        "behavior.topic_not_interested_recently": [NEGATIVE_CATEGORY_HISTORY_REASON_CODE],
        "current_state.is_saved": ["currently_saved"],
        "current_state.is_read_full": ["currently_read_full"],
        "current_state.is_seen": ["currently_seen"],
        "current_state.is_not_interested": [NOT_INTERESTED_REASON_CODE],
        "item.has_extracted_content": ["has_extracted_content"],
        "item.age_hours": ["item_age_hours"],
    }
    return mapping.get(feature_name, [feature_name])


def _ranking_reasons_from_contributions(contributions: list[dict[str, Any]]) -> list[RankingReasonPayload]:
    weights_by_code: dict[str, float] = {}
    order: list[str] = []
    for contribution in contributions:
        value = float(contribution["contribution"])
        if abs(value) < 0.5:
            continue
        reason_codes = contribution.get("reason_codes")
        code = reason_codes[0] if isinstance(reason_codes, list) and reason_codes else str(contribution["feature"])
        if code not in weights_by_code:
            order.append(code)
            weights_by_code[code] = 0.0
        weights_by_code[code] += value
    return [
        RankingReasonPayload(code=code, label=_reason_label(code), weight=int(round(weights_by_code[code])))
        for code in order
        if int(round(weights_by_code[code])) != 0
    ][:12]


def _dedupe_keep_order(values) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result


def _load_source_history(session: Session, *, source_ids: set[int]) -> dict[int, HistoryCounts]:
    totals: dict[int, HistoryCounts] = {}
    for _item_id, event_type, source_id in _iter_source_history_rows(session, source_ids=source_ids):
        _add_history_delta(totals, source_id, _event_delta(event_type))
    return totals


def _load_auto_low_priority_cleared_item_ids(
    session: Session,
    *,
    feed_item_ids: Sequence[int],
) -> frozenset[int]:
    unique_ids = list(dict.fromkeys(feed_item_ids))
    if not unique_ids:
        return frozenset()
    statement = (
        select(FeedItemStructuredFeedback.feed_item_id)
        .where(
            FeedItemStructuredFeedback.feed_item_id.in_(unique_ids),
            FeedItemStructuredFeedback.priority_correction == "too_low",
        )
    )
    return frozenset(session.scalars(statement).all())


def _load_source_history_by_item(session: Session, *, source_ids: set[int]) -> dict[int, dict[int, HistoryCounts]]:
    by_item: dict[int, dict[int, HistoryCounts]] = {}
    for item_id, event_type, source_id in _iter_source_history_rows(session, source_ids=source_ids):
        item_counts = by_item.setdefault(item_id, {})
        _add_history_delta(item_counts, source_id, _event_delta(event_type))
    return by_item


def _iter_source_history_rows(session: Session, *, source_ids: set[int]):
    if not source_ids:
        return []

    latest_occurrences = build_latest_occurrence_subquery()
    statement = (
        select(
            FeedItemBehaviorEvent.feed_item_id,
            FeedItemBehaviorEvent.event_type,
            SourceItemOccurrence.source_id,
        )
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItemBehaviorEvent.feed_item_id)
            & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(ItemState, ItemState.feed_item_id == FeedItemBehaviorEvent.feed_item_id)
        .where(
            SourceItemOccurrence.source_id.in_(list(source_ids)),
            FeedItemBehaviorEvent.event_type.in_(list(SCORING_HISTORY_EVENTS)),
            or_(
                FeedItemBehaviorEvent.event_type != EVENT_NOT_INTERESTED,
                ItemState.status == "not_interested",
            ),
        )
    )
    return session.execute(statement).all()


def _load_category_history_maps(
    session: Session,
    *,
    target_topic_ids: set[str],
) -> tuple[dict[str, HistoryCounts], dict[int, dict[str, HistoryCounts]]]:
    totals: dict[str, HistoryCounts] = {}
    by_item: dict[int, dict[str, HistoryCounts]] = {}
    event_rows = _iter_category_history_event_rows(session, target_topic_ids=target_topic_ids)
    if not event_rows:
        return totals, by_item

    event_item_ids = [item_id for item_id, _event_type in event_rows]
    artifact_map = load_latest_feed_item_insight_artifacts(session, feed_item_ids=event_item_ids)
    topic_ids_by_item_id = {
        item_id: tuple(
            topic_id
            for topic_id in extract_article_scoring_signals(artifact_map.get(item_id)).topic_ids
            if topic_id in target_topic_ids
        )
        for item_id in set(event_item_ids)
    }

    for item_id, event_type in event_rows:
        delta = _event_delta(event_type)
        for topic_id in topic_ids_by_item_id.get(item_id, ()):
            _add_history_delta(totals, topic_id, delta)
            item_counts = by_item.setdefault(item_id, {})
            _add_history_delta(item_counts, topic_id, delta)
    return totals, by_item


def _iter_category_history_event_rows(session: Session, *, target_topic_ids: set[str]):
    if not target_topic_ids:
        return []
    statement = (
        select(FeedItemBehaviorEvent.feed_item_id, FeedItemBehaviorEvent.event_type)
        .join(ItemState, ItemState.feed_item_id == FeedItemBehaviorEvent.feed_item_id)
        .where(FeedItemBehaviorEvent.event_type.in_(list(SCORING_HISTORY_EVENTS)))
        .where(
            or_(
                FeedItemBehaviorEvent.event_type != EVENT_NOT_INTERESTED,
                ItemState.status == "not_interested",
            )
        )
        .order_by(FeedItemBehaviorEvent.feed_item_id.asc())
    )
    return session.execute(statement).all()


def _add_profile_reason_deltas(
    reason_weights: dict[str, int],
    reason_order: list[str],
    signals: ArticleScoringSignals,
    profile_context: dict[str, Any],
) -> None:
    topic_ids = set(signals.topic_ids)
    if topic_ids:
        for field_name in ("active_focus", "long_term_interests"):
            for entry in _profile_entries(profile_context, field_name):
                if entry.get("topic_id") in topic_ids:
                    _add_reason_delta(
                        reason_weights,
                        reason_order,
                        PROFILE_REASON_CODE,
                        _weight_to_delta(entry.get("weight")),
                    )

        for entry in _profile_entries(profile_context, "topic_preferences"):
            if entry.get("topic_id") not in topic_ids:
                continue
            delta = _preference_delta(entry)
            if delta:
                _add_reason_delta(reason_weights, reason_order, PROFILE_REASON_CODE, delta)

    if signals.content_type:
        for entry in _profile_entries(profile_context, "content_type_preferences"):
            if entry.get("content_type") != signals.content_type:
                continue
            delta = _preference_delta(entry)
            if delta:
                _add_reason_delta(reason_weights, reason_order, PROFILE_REASON_CODE, delta)


def _add_source_history_reason_delta(
    reason_weights: dict[str, int],
    reason_order: list[str],
    feed_item_id: int,
    source_id: int | None,
    context: InsightScoringContext,
) -> None:
    if source_id is None:
        return
    total = context.source_history_by_source_id.get(source_id, HistoryCounts())
    current_item_counts = context.source_history_by_item_id.get(feed_item_id, {}).get(source_id)
    counts = total.without(current_item_counts)
    if counts.net >= HISTORY_NET_THRESHOLD:
        _add_reason_delta(reason_weights, reason_order, POSITIVE_SOURCE_HISTORY_REASON_CODE, HISTORY_REASON_WEIGHT)
    elif counts.net <= -HISTORY_NET_THRESHOLD:
        _add_reason_delta(reason_weights, reason_order, NEGATIVE_SOURCE_HISTORY_REASON_CODE, -HISTORY_REASON_WEIGHT)


def _add_category_history_reason_delta(
    reason_weights: dict[str, int],
    reason_order: list[str],
    feed_item_id: int,
    signals: ArticleScoringSignals,
    context: InsightScoringContext,
) -> None:
    if not signals.topic_ids:
        return
    aggregate = HistoryCounts()
    current_by_topic = context.category_history_by_item_id.get(feed_item_id, {})
    for topic_id in signals.topic_ids:
        total = context.category_history_by_topic_id.get(topic_id)
        if total is None:
            continue
        counts = total.without(current_by_topic.get(topic_id))
        aggregate.positive += counts.positive
        aggregate.negative += counts.negative
    if aggregate.net >= HISTORY_NET_THRESHOLD:
        _add_reason_delta(reason_weights, reason_order, POSITIVE_CATEGORY_HISTORY_REASON_CODE, HISTORY_REASON_WEIGHT)
    elif aggregate.net <= -HISTORY_NET_THRESHOLD:
        _add_reason_delta(reason_weights, reason_order, NEGATIVE_CATEGORY_HISTORY_REASON_CODE, -HISTORY_REASON_WEIGHT)


def _ai_reason_delta(reason: RankingReasonPayload) -> int:
    if reason.code in POSITIVE_AI_REASON_CODES:
        return _weight_to_delta(reason.weight)
    if reason.code in NEGATIVE_AI_REASON_CODES:
        return -_weight_to_delta(reason.weight)
    return 0


def _preference_delta(entry: dict[str, Any]) -> int:
    preference = entry.get("preference")
    if preference == "boost":
        return _weight_to_delta(entry.get("weight"))
    if preference == "deprioritize":
        return -_weight_to_delta(entry.get("weight"))
    return 0


def _weight_to_delta(value: object) -> int:
    if not isinstance(value, int):
        return 0
    return min(MAX_REASON_DELTA, ceil(abs(value) / 10))


def _profile_entries(profile_context: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    value = profile_context.get(field_name)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _add_reason_delta(
    reason_weights: dict[str, int],
    reason_order: list[str],
    code: str,
    delta: int,
) -> None:
    if code not in reason_weights:
        reason_order.append(code)
        reason_weights[code] = 0
    reason_weights[code] += delta


def _add_history_delta(counts_by_key: dict[Any, HistoryCounts], key: Any, delta: int) -> None:
    if delta == 0:
        return
    counts_by_key.setdefault(key, HistoryCounts()).add_delta(delta)


def _event_delta(event_type: str) -> int:
    if event_type in POSITIVE_HISTORY_EVENTS:
        return 1
    if event_type in NEGATIVE_HISTORY_EVENTS:
        return -1
    return 0


def _reason_label(code: str) -> str:
    return get_reason_code_label(code) or code.replace("_", " ")


def _validate_payload(model, artifact: InsightEngineArtifact | None):
    if artifact is None or not isinstance(artifact.artifact_payload, dict):
        return None
    try:
        return model.model_validate(artifact.artifact_payload)
    except ValidationError:
        return None


def _clamp_score(value: int) -> int:
    return max(0, min(100, value))


def _priority_band_for_score(score: int) -> HomepagePriorityBand:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
