from __future__ import annotations

from datetime import datetime, timezone

from app.services.insight_engine_artifacts import RankingReasonPayload
from app.services.insight_scoring import (
    FEATURE_SCHEMA_VERSION,
    SCORE_VERSION,
    ArticleScoringSignals,
    HistoryCounts,
    InsightScoringContext,
    flatten_insight_feature_vector,
    score_feed_item_priority,
    score_insight_feature_vector,
)


def test_insight_feature_vector_uses_grouped_contract_and_flat_numeric_view() -> None:
    context = InsightScoringContext(
        source_history_by_source_id={10: HistoryCounts(positive=2)},
        low_priority_score_threshold=40,
    )
    signals = ArticleScoringSignals(
        topic_ids=("software_systems",),
        content_type="analysis",
        ai_reasons=(RankingReasonPayload(code="implementation_detail", label="Implementation detail", weight=50),),
        llm_candidate_count=1,
        top_llm_candidate_confidence=0.8,
    )

    priority = score_feed_item_priority(
        feed_item_id=123,
        item_status="unread",
        source_id=10,
        signals=signals,
        context=context,
        generated_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        channel_catalog_version=3,
        provider_profile="general",
    )

    vector = priority.feature_vector
    assert vector["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert vector["source"]["source_profile_is_empty"] == 1
    assert vector["semantic"]["quality_score"] == 0.5
    assert vector["routing"]["top_llm_candidate_confidence"] == 0.8
    assert vector["context"]["channel_catalog_version"] == 3

    flat = flatten_insight_feature_vector(vector)
    assert flat["semantic.quality_score"] == 0.5
    assert flat["routing.top_llm_candidate_confidence"] == 0.8
    assert flat["behavior.source_saved_recently"] == 1.0
    assert "source.source_id" not in flat


def test_score_v1_hand_weighted_formula_and_band_are_stable() -> None:
    vector = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feed_item_id": 1,
        "generated_at": "2026-05-20T12:00:00+00:00",
        "item": {"age_hours": 24, "text_length_chars": 1000, "has_canonical_url": 1, "has_extracted_content": 1},
        "source": {
            "source_id": 10,
            "flow_policy": "normal",
            "source_profile_is_empty": 1,
            "source_profile_has_good_criteria": 0,
            "source_prefers_deep_content": 0,
            "source_penalizes_marketing": 0,
        },
        "semantic": {
            "content_type": "analysis",
            "quality_score": 0.8,
            "relevance_score": 0.6,
            "depth_score": 0.5,
            "marketing_score": 0.25,
            "model_confidence": 0.8,
        },
        "routing": {
            "llm_candidate_count": 1,
            "top_llm_candidate_confidence": 0.75,
            "source_tendency_confidence": 0,
            "has_human_channel_correction": 0,
            "uses_needs_review_fallback": 0,
        },
        "feedback": {
            "explicit_positive_signal": 1,
            "explicit_negative_signal": 0,
            "has_priority_correction": 1,
            "has_channel_correction": 0,
        },
        "behavior": {
            "source_saved_recently": 1,
            "source_not_interested_recently": 0,
            "topic_saved_recently": 1,
            "topic_not_interested_recently": 0,
        },
        "current_state": {
            "is_saved": 0,
            "is_not_interested": 0,
            "is_read_full": 0,
            "is_seen": 0,
        },
        "context": {
            "profile_version": None,
            "channel_catalog_version": 3,
            "source_profile_version": None,
            "provider_profile": "general",
        },
    }

    score, band, breakdown, reasons = score_insight_feature_vector(vector)

    assert score == 100
    assert band == "high"
    assert breakdown["score_version"] == SCORE_VERSION
    assert breakdown["final_priority_score"] == 100
    assert any(entry["feature"] == "semantic.quality_score" for entry in breakdown["contributions"])
    assert [reason.code for reason in reasons]


def test_needs_review_is_routing_feature_not_priority_band() -> None:
    context = InsightScoringContext()
    signals = ArticleScoringSignals(uses_needs_review_fallback=True, top_llm_candidate_confidence=0.0)

    priority = score_feed_item_priority(
        feed_item_id=1,
        item_status="unread",
        source_id=None,
        signals=signals,
        context=context,
    )

    assert priority.band in {"high", "medium", "low"}
    assert priority.band != "needs_review"
    assert priority.feature_vector["routing"]["uses_needs_review_fallback"] == 1


def test_not_interested_is_strong_reversible_negative_contribution() -> None:
    priority = score_feed_item_priority(
        feed_item_id=1,
        item_status="not_interested",
        source_id=None,
        signals=ArticleScoringSignals(),
        context=InsightScoringContext(),
    )

    assert priority.score < 40
    assert priority.band == "low"
    assert priority.low_priority_bucket is True
    assert priority.low_priority_bucket_reason == "not_interested"
    assert ("current_state.is_not_interested", -60.0) in [
        (entry["feature"], entry["weight"]) for entry in priority.score_breakdown["contributions"]
    ]
