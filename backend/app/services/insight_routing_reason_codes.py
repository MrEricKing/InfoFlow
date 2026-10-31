from __future__ import annotations

from typing import Any

ROUTING_REASON_CODES = (
    "content_topic_match",
    "source_channel_tendency_match",
    "source_folder_hint",
    "source_flow_policy_high_frequency",
    "user_channel_correction_hint",
    "reading_depth_policy",
    "cross_channel_tooling",
    "low_signal_brief_candidate",
    "extraction_failed_needs_review",
    "ambiguous_channel_needs_review",
    "manual_override_user_corrected",
    "invalid_channel_reference",
    "channel_deleted_fallback",
)

PROMOTION_REASON_CODES = (
    "source_policy_promoted",
    "long_analysis_promoted_from_high_frequency",
    "high_confidence_primary_channel",
    "user_corrected_primary_channel",
)

ISOLATION_REASON_CODES = (
    "source_policy_high_frequency_brief",
    "low_signal_brief_candidate",
    "extraction_failed_needs_review",
    "ambiguous_channel_needs_review",
    "not_interested_deprioritized",
)

ROUTING_REASON_CODE_SET = set(ROUTING_REASON_CODES)
PROMOTION_REASON_CODE_SET = set(PROMOTION_REASON_CODES)
ISOLATION_REASON_CODE_SET = set(ISOLATION_REASON_CODES)


def build_routing_reason_codes_context() -> dict[str, Any]:
    return {
        "routing_reason_codes": list(ROUTING_REASON_CODES),
        "promotion_reason_codes": list(PROMOTION_REASON_CODES),
        "isolation_reason_codes": list(ISOLATION_REASON_CODES),
    }
