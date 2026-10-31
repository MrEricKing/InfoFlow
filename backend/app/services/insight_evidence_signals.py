from __future__ import annotations

from typing import Any, Literal, TypedDict


SignalStrength = Literal["strong", "medium", "weak"]
SignalPolarity = Literal["positive", "negative", "cleanup", "correction", "neutral"]


class EvidenceSignal(TypedDict):
    strength: SignalStrength
    polarity: SignalPolarity
    profile_weight: int
    learning_note: str


def classify_behavior_event(
    event_type: str,
    event_payload: dict[str, Any] | None = None,
) -> EvidenceSignal:
    payload = event_payload if isinstance(event_payload, dict) else {}
    view_context = payload.get("view_context")
    if not isinstance(view_context, dict):
        view_context = {}
    routing_context = payload.get("routing_context")
    if not isinstance(routing_context, dict):
        routing_context = {}
    if event_type in {"saved", "opened_original_link"}:
        return _signal("strong", "positive", 3, "explicit_high_value_action")
    if event_type == "read_full_in_app":
        return _signal("medium", "positive", 2, "completed_in_app_reading")
    if event_type == "not_interested":
        return _signal("strong", "negative", -3, "explicit_not_interested")
    if event_type == "marked_seen_bulk":
        if routing_context.get("flow_lane") == "brief_pool":
            return _signal("weak", "cleanup", 0, "brief_pool_bulk_seen_cleanup")
        scope_type = view_context.get("scope_type")
        if scope_type == "source":
            return _signal("weak", "cleanup", 0, "source_bulk_seen_cleanup")
        if scope_type == "source_category":
            return _signal("weak", "cleanup", 0, "source_category_bulk_seen_cleanup")
        return _signal("weak", "cleanup", 0, "bulk_seen_cleanup_not_topic_dislike")
    if event_type == "marked_seen_from_list":
        return _signal("weak", "cleanup", 0, "light_scan_processed_not_long_term_negative")
    if event_type == "structured_feedback_saved":
        structured_feedback = payload.get("structured_feedback")
        if isinstance(structured_feedback, dict) and _structured_feedback_has_channel_correction(structured_feedback):
            if structured_feedback.get("channel_remove_ids"):
                return _signal("strong", "correction", 3, "explicit_channel_correction_with_negative_channel_signal")
            return _signal("strong", "correction", 3, "explicit_channel_correction")
        return _signal("strong", "correction", 3, "explicit_structured_feedback")
    if event_type == "structured_feedback_cleared":
        return _signal("weak", "neutral", 0, "structured_feedback_removed")
    if event_type == "insight_feedback_saved":
        return _signal("strong", "correction", 3, "explicit_freeform_feedback")
    if event_type == "insight_feedback_cleared":
        return _signal("weak", "neutral", 0, "freeform_feedback_removed")
    if event_type == "auto_low_priority_cleared":
        return _signal("strong", "correction", 3, "system_ranked_item_too_low")
    if event_type == "card_opened":
        return _signal("weak", "neutral", 0, "opened_card_without_value_commitment")
    if event_type in {"seen_cleared", "read_full_cleared", "not_interested_cleared", "unsaved"}:
        return _signal("weak", "neutral", 0, "explicit_signal_removed_or_reversed")
    if payload.get("previous_status") == "read_full":
        return _signal("medium", "positive", 1, "status_transition_from_read_full")
    return _signal("weak", "neutral", 0, "unclassified_behavior_event")


def classify_current_state(status: str, saved: bool = False) -> EvidenceSignal:
    if saved:
        return _signal("strong", "positive", 3, "currently_saved")
    if status == "saved":
        return _signal("strong", "positive", 3, "currently_saved")
    if status == "read_full":
        return _signal("medium", "positive", 2, "currently_read_full")
    if status == "not_interested":
        return _signal("strong", "negative", -3, "currently_not_interested")
    if status == "seen":
        return _signal("weak", "cleanup", 0, "currently_seen_processed")
    if status == "browsed":
        return _signal("weak", "neutral", 0, "currently_browsed_without_value_commitment")
    return _signal("weak", "neutral", 0, "current_state_not_enough_for_preference")


def summarize_evidence_signals(signals: list[EvidenceSignal]) -> dict[str, Any]:
    by_learning_note: dict[str, int] = {}
    for signal in signals:
        note = signal["learning_note"]
        by_learning_note[note] = by_learning_note.get(note, 0) + 1

    return {
        "strong_signal_count": sum(1 for signal in signals if signal["strength"] == "strong"),
        "medium_signal_count": sum(1 for signal in signals if signal["strength"] == "medium"),
        "weak_cleanup_signal_count": sum(
            1 for signal in signals if signal["strength"] == "weak" and signal["polarity"] == "cleanup"
        ),
        "negative_signal_count": sum(1 for signal in signals if signal["polarity"] == "negative"),
        "signal_count": len(signals),
        "by_learning_note": by_learning_note,
    }


def _signal(
    strength: SignalStrength,
    polarity: SignalPolarity,
    profile_weight: int,
    learning_note: str,
) -> EvidenceSignal:
    return {
        "strength": strength,
        "polarity": polarity,
        "profile_weight": profile_weight,
        "learning_note": learning_note,
    }


def _structured_feedback_has_channel_correction(structured_feedback: dict[str, Any]) -> bool:
    return bool(
        structured_feedback.get("primary_channel_correction")
        or structured_feedback.get("channel_add_ids")
        or structured_feedback.get("channel_remove_ids")
        or structured_feedback.get("reading_depth_correction")
        or structured_feedback.get("flow_lane_correction")
    )
