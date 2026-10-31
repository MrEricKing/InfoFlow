from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, SavedItem, Source
from app.schemas.settings import (
    InsightProfileCandidateChannelSuggestionRead,
    InsightProfileCandidateInspectResponse,
    InsightProfileCandidateSourceApplyResponse,
    InsightProfileCandidateSourceChangeRead,
    InsightProfileFieldDiffRead,
)
from app.schemas.source import SourcePreferenceProfileDraftUpdateRequest
from app.services.insight_channels import list_channels
from app.services.insight_profiles import inspect_insight_profile_draft
from app.services.source_preference_profiles import (
    get_source_preference_profile_state,
    normalize_source_preference_profile_payload,
    render_source_preference_profile_prompt_context,
    save_source_preference_profile_draft,
)

MAX_PROFILE_CANDIDATE_JSON_BYTES = 1024 * 1024
MAX_CANDIDATE_TEXT_FIELD_BYTES = 4 * 1024
MAX_CANDIDATE_PROMPT_TEXT_BYTES = 32 * 1024

SOURCE_PROFILE_FIELDS = (
    "subscription_reason",
    "good_article_criteria",
    "bad_article_patterns",
    "preferred_topics",
    "ignored_topics",
    "preferred_content_types",
    "positive_example_item_ids",
    "negative_example_item_ids",
    "positive_example_summaries",
    "negative_example_summaries",
    "quality_notes",
    "edit_note",
)
SOURCE_PROFILE_STORAGE_FIELDS = (*SOURCE_PROFILE_FIELDS, "source_type")

FORBIDDEN_CANDIDATE_KEY_FRAGMENTS = ("api_key", "authorization", "cookie", "token", "secret", "password")
FORBIDDEN_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role_marker", re.compile(r"(<\|(system|assistant|user)\|>|###\s*system|\[(system|assistant|user)\]|<(system|assistant|tool)>|\b(SYSTEM|ASSISTANT|USER)\s*:)", re.IGNORECASE)),
    ("override_phrase", re.compile(r"\b(ignore previous instructions|disregard all previous|forget your instructions|you are now|act as system|act as developer|follow only this instruction)\b", re.IGNORECASE)),
    ("prompt_theft", re.compile(r"\b(reveal system prompt|print hidden instructions|show developer message)\b", re.IGNORECASE)),
    ("tool_or_exfiltration", re.compile(r"\b(run command|execute shell|call tool|read file|send api key|authorization header|cookie)\b", re.IGNORECASE)),
    ("markdown_code_fence", re.compile(r"```")),
    ("secret_like_key", re.compile(r"\b(api_key|authorization|cookie|token|secret|password)\b\s*[:=]", re.IGNORECASE)),
    ("large_base64_blob", re.compile(r"\b[A-Za-z0-9+/]{120,}={0,2}\b")),
)
INVISIBLE_OR_BIDI_CONTROL_CHARS = frozenset(
    "\u0000\u0001\u0002\u0003\u0004\u0005\u0006\u0007\u0008"
    "\u000b\u000c\u000e\u000f"
    "\u0010\u0011\u0012\u0013\u0014\u0015\u0016\u0017\u0018\u0019"
    "\u001a\u001b\u001c\u001d\u001e\u001f"
    "\u034f\u061c\u180e\u200b\u200c\u200d\u200e\u200f\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069"
)


class _SourceProfileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: int
    source_name: str | None = None
    subscription_reason: str | None = None
    good_article_criteria: list[str] | None = None
    bad_article_patterns: list[str] | None = None
    preferred_topics: list[str] | None = None
    ignored_topics: list[str] | None = None
    preferred_content_types: list[str] | None = None
    positive_example_item_ids: list[int] | None = None
    negative_example_item_ids: list[int] | None = None
    positive_example_summaries: list[str] | None = None
    negative_example_summaries: list[str] | None = None
    quality_notes: str | None = None
    edit_note: str | None = None
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)


class _ChannelPromptBoundarySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    channel_id: str
    suggested_focus_prompt: str | None = None
    suggested_boundary_prompt: str | None = None
    rationale: str | None = None
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("suggested_focus_prompt", "suggested_boundary_prompt", "rationale")
    @classmethod
    def empty_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class _ProfileCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_version: str
    based_on_package: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    global_profile_changes: dict[str, Any] | None = None
    source_profile_changes: list[_SourceProfileChange] = Field(default_factory=list)
    channel_prompt_boundary_suggestions: list[_ChannelPromptBoundarySuggestion] = Field(default_factory=list)
    scoring_policy_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def inspect_profile_candidate(session: Session, *, candidate_json: dict[str, Any]) -> InsightProfileCandidateInspectResponse:
    candidate = _parse_candidate(candidate_json)
    channels_by_id = {channel.id: channel for channel in list_channels(session, include_disabled=True)}
    sources_by_id = _load_sources_by_id(session, [change.source_id for change in candidate.source_profile_changes])
    _validate_candidate_references(session, candidate=candidate, channels_by_id=channels_by_id)

    global_inspection = None
    if candidate.global_profile_changes is not None:
        profile_json = candidate.global_profile_changes.get("profile_json", candidate.global_profile_changes)
        if not isinstance(profile_json, dict):
            raise ValueError("global_profile_changes must be an Insight Profile object or {profile_json: object}.")
        global_inspection = inspect_insight_profile_draft(session, profile_json=profile_json)

    source_changes = [
        _inspect_source_change(
            session,
            change=change,
            source=sources_by_id[change.source_id],
            candidate_version=candidate.candidate_version,
        )
        for change in candidate.source_profile_changes
    ]
    channel_suggestions = [
        _inspect_channel_suggestion(suggestion, channels_by_id=channels_by_id)
        for suggestion in candidate.channel_prompt_boundary_suggestions
    ]

    return InsightProfileCandidateInspectResponse(
        candidate_version=candidate.candidate_version,
        based_on_package=candidate.based_on_package,
        summary=candidate.summary,
        normalized_candidate=candidate.model_dump(mode="json"),
        global_profile_inspection=global_inspection,
        source_profile_changes=source_changes,
        channel_prompt_boundary_suggestions=channel_suggestions,
        scoring_policy_suggestions=candidate.scoring_policy_suggestions,
        evidence_references=candidate.evidence_references,
        warnings=candidate.warnings,
        dry_run={
            "writes_database": False,
            "calls_ai_provider": False,
            "source_drafts_available": [change.source_id for change in candidate.source_profile_changes],
            "channel_suggestions_apply_automatically": False,
            "global_profile_uses_existing_editor": global_inspection is not None,
        },
    )


def apply_source_profile_candidate_drafts(
    session: Session,
    *,
    candidate_json: dict[str, Any],
    source_ids: list[int],
) -> InsightProfileCandidateSourceApplyResponse:
    selected_ids = list(dict.fromkeys(source_ids))
    if not selected_ids:
        raise ValueError("source_ids must contain at least one source id.")
    inspect_profile_candidate(session, candidate_json=candidate_json)
    candidate = _parse_candidate(candidate_json)
    changes_by_id = {change.source_id: change for change in candidate.source_profile_changes}
    missing_ids = [source_id for source_id in selected_ids if source_id not in changes_by_id]
    if missing_ids:
        raise ValueError(f"Candidate does not include source_profile_changes for source ids: {missing_ids}")
    _load_sources_by_id(session, selected_ids)

    for source_id in selected_ids:
        change = changes_by_id[source_id]
        payload_data = _source_apply_patch_from_change(change, candidate_version=candidate.candidate_version)
        payload = SourcePreferenceProfileDraftUpdateRequest(**payload_data)
        save_source_preference_profile_draft(session, source_id, payload)

    return InsightProfileCandidateSourceApplyResponse(
        applied_source_ids=selected_ids,
        applied_count=len(selected_ids),
        skipped_source_ids=[change.source_id for change in candidate.source_profile_changes if change.source_id not in selected_ids],
        message=f"Saved {len(selected_ids)} Source Profile candidate draft(s).",
    )


def _parse_candidate(candidate_json: dict[str, Any]) -> _ProfileCandidate:
    _reject_unsafe_candidate(candidate_json)
    payload = candidate_json.get("profile_candidate", candidate_json)
    if not isinstance(payload, dict):
        raise ValueError("profile_candidate must be a JSON object.")
    try:
        candidate = _ProfileCandidate.model_validate(payload)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    source_ids = [change.source_id for change in candidate.source_profile_changes]
    duplicate_source_ids = sorted({source_id for source_id in source_ids if source_ids.count(source_id) > 1})
    if duplicate_source_ids:
        raise ValueError(f"Duplicate source_profile_changes source ids: {duplicate_source_ids}")
    return candidate


def _reject_unsafe_candidate(candidate_json: dict[str, Any]) -> None:
    size = len(json.dumps(candidate_json, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if size > MAX_PROFILE_CANDIDATE_JSON_BYTES:
        raise ValueError(f"profile candidate JSON must be at most {MAX_PROFILE_CANDIDATE_JSON_BYTES} bytes")
    text_bytes_total = 0
    for key_path, key in _walk_dict_keys(candidate_json):
        lowered_key = key.lower()
        if any(fragment in lowered_key for fragment in FORBIDDEN_CANDIDATE_KEY_FRAGMENTS):
            raise ValueError(f"profile candidate must not contain secret-like field: {key_path}")
        if any(char in INVISIBLE_OR_BIDI_CONTROL_CHARS for char in key):
            raise ValueError(f"profile candidate key {key_path} contains invisible/bidi control characters")
    for key_path, text in _walk_strings(candidate_json):
        field_size = len(text.encode("utf-8"))
        if field_size > MAX_CANDIDATE_TEXT_FIELD_BYTES:
            raise ValueError(f"profile candidate text field {key_path} must be at most {MAX_CANDIDATE_TEXT_FIELD_BYTES} bytes")
        text_bytes_total += field_size
        if any(char in INVISIBLE_OR_BIDI_CONTROL_CHARS for char in text):
            raise ValueError(f"profile candidate text field {key_path} contains invisible/bidi control characters")
        for pattern_name, pattern in FORBIDDEN_TEXT_PATTERNS:
            if pattern.search(text):
                raise ValueError(f"profile candidate text field {key_path} contains forbidden pattern: {pattern_name}")
    if text_bytes_total > MAX_CANDIDATE_PROMPT_TEXT_BYTES:
        raise ValueError(f"profile candidate prompt-affecting text must be at most {MAX_CANDIDATE_PROMPT_TEXT_BYTES} bytes")


def _walk_dict_keys(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for key, child in value.items():
            key_text = str(key)
            key_path = f"{prefix}.{key_text}"
            result.append((key_path, key_text))
            result.extend(_walk_dict_keys(child, key_path))
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            result.extend(_walk_dict_keys(child, f"{prefix}[{index}]"))
        return result
    return []


def _walk_strings(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for key, child in value.items():
            result.extend(_walk_strings(child, f"{prefix}.{key}"))
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            result.extend(_walk_strings(child, f"{prefix}[{index}]"))
        return result
    return []


def _validate_candidate_references(
    session: Session,
    *,
    candidate: _ProfileCandidate,
    channels_by_id: dict[str, Any],
) -> None:
    reference_ids = _collect_reference_ids(candidate.model_dump(mode="json"))
    _load_sources_by_id(session, sorted(reference_ids["source_ids"]))
    _validate_existing_ids(session, FeedItem, "feed item", sorted(reference_ids["feed_item_ids"]))
    _validate_existing_ids(session, SavedItem, "saved item", sorted(reference_ids["saved_item_ids"]))
    unknown_channel_ids = sorted(channel_id for channel_id in reference_ids["channel_ids"] if channel_id not in channels_by_id)
    if unknown_channel_ids:
        raise ValueError(f"Unknown channel ids in evidence references: {unknown_channel_ids}")


def _collect_reference_ids(value: Any, prefix: str = "$") -> dict[str, set[Any]]:
    result: dict[str, set[Any]] = {
        "source_ids": set(),
        "feed_item_ids": set(),
        "saved_item_ids": set(),
        "channel_ids": set(),
    }

    def walk(child: Any, path: str) -> None:
        if isinstance(child, dict):
            for key, nested in child.items():
                key_text = str(key)
                nested_path = f"{path}.{key_text}"
                if nested is None:
                    continue
                if key_text in {"source_id"}:
                    result["source_ids"].add(_read_positive_int_reference(nested, nested_path))
                elif key_text in {"source_ids"}:
                    result["source_ids"].update(_read_positive_int_reference_list(nested, nested_path))
                elif key_text in {"feed_item_id", "item_id"}:
                    result["feed_item_ids"].add(_read_positive_int_reference(nested, nested_path))
                elif key_text in {"feed_item_ids", "item_ids", "positive_example_item_ids", "negative_example_item_ids"}:
                    result["feed_item_ids"].update(_read_positive_int_reference_list(nested, nested_path))
                elif key_text == "saved_item_id":
                    result["saved_item_ids"].add(_read_positive_int_reference(nested, nested_path))
                elif key_text == "saved_item_ids":
                    result["saved_item_ids"].update(_read_positive_int_reference_list(nested, nested_path))
                elif key_text == "channel_id":
                    result["channel_ids"].add(_read_channel_reference(nested, nested_path))
                elif key_text in {"channel_ids", "channel_add_ids", "channel_remove_ids"}:
                    result["channel_ids"].update(_read_channel_reference_list(nested, nested_path))
                walk(nested, nested_path)
        elif isinstance(child, list):
            for index, nested in enumerate(child):
                walk(nested, f"{path}[{index}]")

    walk(value, prefix)
    return result


def _read_positive_int_reference(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"candidate evidence reference field {path} must be a positive integer")
    return value


def _read_positive_int_reference_list(value: Any, path: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"candidate evidence reference field {path} must be a list of positive integers")
    return [_read_positive_int_reference(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _read_channel_reference(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"candidate evidence reference field {path} must be a non-empty string")
    return value.strip()


def _read_channel_reference_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"candidate evidence reference field {path} must be a list of channel ids")
    return [_read_channel_reference(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _validate_existing_ids(session: Session, model: Any, label: str, ids: list[int]) -> None:
    if not ids:
        return
    existing_ids = set(session.scalars(select(model.id).where(model.id.in_(ids))).all())
    missing_ids = [item_id for item_id in ids if item_id not in existing_ids]
    if missing_ids:
        raise ValueError(f"Unknown {label} ids in evidence references: {missing_ids}")


def _load_sources_by_id(session: Session, source_ids: list[int]) -> dict[int, Source]:
    unique_ids = list(dict.fromkeys(source_ids))
    if not unique_ids:
        return {}
    sources = {source.id: source for source in session.scalars(select(Source).where(Source.id.in_(unique_ids))).all()}
    missing_ids = [source_id for source_id in unique_ids if source_id not in sources]
    if missing_ids:
        raise ValueError(f"Unknown source ids: {missing_ids}")
    return sources


def _inspect_source_change(
    session: Session,
    *,
    change: _SourceProfileChange,
    source: Source,
    candidate_version: str,
) -> InsightProfileCandidateSourceChangeRead:
    state = get_source_preference_profile_state(session, source.id)
    base_profile = state.draft or state.active
    base_json = _source_profile_read_to_json(base_profile) if base_profile is not None else {}
    base_storage = _source_profile_read_to_storage_payload(base_profile) if base_profile is not None else {}
    patch = _source_apply_patch_from_change(change, candidate_version=candidate_version)
    normalized = normalize_source_preference_profile_payload({**base_storage, **patch})
    prompt_context = render_source_preference_profile_prompt_context(
        normalized,
        profile_version=state.draft.profile_version if state.draft is not None else None,
    )
    return InsightProfileCandidateSourceChangeRead(
        source_id=source.id,
        source_name=source.name,
        normalized_profile=normalized,
        prompt_context=prompt_context.profile,
        diff_summary=_build_source_diff_summary(base_json, normalized),
        field_diffs=[
            _build_field_diff(field, before=base_json.get(field), after=normalized.get(field))
            for field in SOURCE_PROFILE_FIELDS
        ],
        evidence_references=change.evidence_references,
    )


def _inspect_channel_suggestion(
    suggestion: _ChannelPromptBoundarySuggestion,
    *,
    channels_by_id: dict[str, Any],
) -> InsightProfileCandidateChannelSuggestionRead:
    channel = channels_by_id.get(suggestion.channel_id)
    if channel is None:
        raise ValueError(f"Unknown channel id: {suggestion.channel_id}")
    return InsightProfileCandidateChannelSuggestionRead(
        channel_id=channel.id,
        channel_label=channel.label,
        current_focus_prompt=channel.focus_prompt,
        suggested_focus_prompt=suggestion.suggested_focus_prompt,
        current_boundary_prompt=channel.boundary_prompt,
        suggested_boundary_prompt=suggestion.suggested_boundary_prompt,
        rationale=suggestion.rationale,
        evidence_references=suggestion.evidence_references,
    )


def _source_payload_from_change(change: _SourceProfileChange) -> dict[str, Any]:
    payload = change.model_dump(exclude_unset=True, mode="json")
    return {field: payload[field] for field in SOURCE_PROFILE_FIELDS if field in payload}


def _source_apply_patch_from_change(change: _SourceProfileChange, *, candidate_version: str) -> dict[str, Any]:
    payload = _source_payload_from_change(change)
    payload["source_type"] = "codex_import"
    payload["edit_note"] = payload.get("edit_note") or f"Imported from profile candidate {candidate_version}."
    return payload


def _source_profile_read_to_json(profile: Any) -> dict[str, Any]:
    return {field: getattr(profile, field, None) for field in SOURCE_PROFILE_FIELDS}


def _source_profile_read_to_storage_payload(profile: Any) -> dict[str, Any]:
    return {field: getattr(profile, field, None) for field in SOURCE_PROFILE_STORAGE_FIELDS}


def _build_source_diff_summary(before: dict[str, Any], after: dict[str, Any]) -> str:
    changed = [field for field in SOURCE_PROFILE_FIELDS if before.get(field) != after.get(field)]
    if not changed:
        return "No Source Profile fields changed."
    return "Changed Source Profile fields: " + ", ".join(changed)


def _build_field_diff(field: str, *, before: Any, after: Any) -> InsightProfileFieldDiffRead:
    if _is_empty(before) and not _is_empty(after):
        change_type = "added"
    elif not _is_empty(before) and _is_empty(after):
        change_type = "removed"
    elif before != after:
        change_type = "modified"
    else:
        change_type = "unchanged"
    return InsightProfileFieldDiffRead(field=field, change_type=change_type, before=before, after=after)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
