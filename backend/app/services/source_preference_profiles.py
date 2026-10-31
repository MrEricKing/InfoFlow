from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import json
import re
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Source, SourcePreferenceProfile
from app.schemas.source import (
    SourcePreferenceProfileDraftUpdateRequest,
    SourcePreferenceProfileFieldError,
    SourcePreferenceProfileRead,
    SourcePreferenceProfileStateRead,
)
from app.services.insight_engine_contracts import canonicalize_content_type_id, canonicalize_taxonomy_id

SOURCE_PREFERENCE_PROFILE_SCHEMA_VERSION = "source_preference_profile_v1"
SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES = 4 * 1024
MAX_NORMALIZED_PROFILE_JSON_BYTES = 32 * 1024
MAX_TOTAL_PROMPT_TEXT_BYTES = 32 * 1024

TEXT_FIELD_LIMITS: dict[str, int] = {
    "subscription_reason": 1024,
    "quality_notes": 2048,
    "edit_note": 1024,
}

LIST_FIELD_LIMITS: dict[str, tuple[int, int | None]] = {
    "good_article_criteria": (12, 400),
    "bad_article_patterns": (12, 400),
    "preferred_topics": (20, None),
    "ignored_topics": (20, None),
    "preferred_content_types": (10, None),
    "positive_example_item_ids": (20, None),
    "negative_example_item_ids": (20, None),
    "positive_example_summaries": (10, 500),
    "negative_example_summaries": (10, 500),
}

PROMPT_CONTEXT_SECTION_ORDER = (
    "subscription_reason",
    "good_article_criteria",
    "bad_article_patterns",
    "preferred_content_types",
    "preferred_topics",
    "ignored_topics",
    "positive_example_summaries",
    "negative_example_summaries",
    "quality_notes",
)

PROMPT_CONTEXT_FIELDS = (
    "subscription_reason",
    "good_article_criteria",
    "bad_article_patterns",
    "preferred_topics",
    "ignored_topics",
    "preferred_content_types",
    "positive_example_summaries",
    "negative_example_summaries",
    "quality_notes",
)

STORAGE_FIELDS = (
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
    "source_type",
    "edit_note",
)

FORBIDDEN_PROMPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role_marker", re.compile(r"(<\|(system|assistant|user)\|>|###\s*system|\[(system|assistant|user)\]|<(system|assistant|tool)>|\b(SYSTEM|ASSISTANT|USER)\s*:)", re.IGNORECASE)),
    ("override_phrase", re.compile(r"\b(ignore previous instructions|disregard all previous|forget your instructions|you are now|act as system|act as developer|follow only this instruction)\b", re.IGNORECASE)),
    ("prompt_theft", re.compile(r"\b(reveal system prompt|print hidden instructions|show developer message)\b", re.IGNORECASE)),
    ("tool_or_exfiltration", re.compile(r"\b(run command|execute shell|call tool|read file|send api key|authorization header|cookie)\b", re.IGNORECASE)),
    ("markdown_code_fence", re.compile(r"```")),
    ("secret_like_key", re.compile(r"\b(api_key|authorization|cookie|token|secret|password)\b\s*[:=]", re.IGNORECASE)),
    ("large_base64_blob", re.compile(r"\b[A-Za-z0-9+/]{120,}={0,2}\b")),
)
BIDI_OVERRIDE_CHARS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


CANONICAL_EMPTY_SOURCE_PROFILE: dict[str, Any] = {
    "subscription_reason": None,
    "good_article_criteria": [],
    "bad_article_patterns": [],
    "preferred_topics": [],
    "ignored_topics": [],
    "preferred_content_types": [],
    "positive_example_summaries": [],
    "negative_example_summaries": [],
    "quality_notes": None,
    "profile_version": None,
    "is_empty": True,
    "render_stats": {
        "max_rendered_bytes": SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES,
        "rendered_byte_count": 0,
        "included_counts": {},
        "omitted_counts": {},
        "capped_sections": [],
    },
}


@dataclass(frozen=True, slots=True)
class SourcePreferenceProfileContext:
    profile: dict[str, Any]
    profile_version: str | None
    is_empty: bool
    render_stats: dict[str, Any]


class SourcePreferenceProfileNotFoundError(ValueError):
    pass


class SourcePreferenceProfileValidationError(ValueError):
    def __init__(self, errors: list[SourcePreferenceProfileFieldError]) -> None:
        super().__init__("Source Preference Profile validation failed.")
        self.errors = errors


def get_source_preference_profile_state(session: Session, source_id: int) -> SourcePreferenceProfileStateRead:
    _require_source(session, source_id)
    active = _get_profile_by_status(session, source_id, "active")
    draft = _get_profile_by_status(session, source_id, "draft")
    history = _list_profile_history(session, source_id)
    context = source_preference_profile_context_from_row(active)
    return SourcePreferenceProfileStateRead(
        source_id=source_id,
        active=_build_profile_read(active) if active is not None else None,
        draft=_build_profile_read(draft) if draft is not None else None,
        history=[_build_profile_read(profile) for profile in history],
        rendered_prompt_context=context.profile,
        render_stats=context.render_stats,
    )


def list_source_preference_profile_history(session: Session, source_id: int) -> list[SourcePreferenceProfileRead]:
    _require_source(session, source_id)
    return [_build_profile_read(profile) for profile in _list_profile_history(session, source_id)]


def save_source_preference_profile_draft(
    session: Session,
    source_id: int,
    payload: SourcePreferenceProfileDraftUpdateRequest,
) -> SourcePreferenceProfileStateRead:
    _require_source(session, source_id)
    draft = _get_profile_by_status(session, source_id, "draft")
    active = _get_profile_by_status(session, source_id, "active")
    patch = payload.model_dump(exclude_unset=True)
    if draft is not None:
        base = _storage_payload_from_row(draft)
    elif patch:
        base = _storage_payload_from_row(active)
    else:
        base = _empty_storage_payload()
    normalized = normalize_source_preference_profile_payload({**base, **patch})
    rendered = render_source_preference_profile_prompt_context(
        normalized,
        profile_version=draft.profile_version if draft is not None else None,
    )
    if draft is None:
        draft = SourcePreferenceProfile(source_id=source_id, status="draft")
        session.add(draft)
    _apply_normalized_payload(draft, normalized)
    draft.schema_version = SOURCE_PREFERENCE_PROFILE_SCHEMA_VERSION
    draft.profile_version = None
    draft.prompt_context_json = rendered.profile
    draft.prompt_context_stats_json = rendered.render_stats
    session.flush()
    return get_source_preference_profile_state(session, source_id)


def activate_source_preference_profile_draft(
    session: Session,
    source_id: int,
    *,
    edit_note: str | None = None,
) -> SourcePreferenceProfileStateRead:
    _require_source(session, source_id)
    draft = _get_profile_by_status(session, source_id, "draft")
    if draft is None:
        raise SourcePreferenceProfileNotFoundError("Source Preference Profile draft not found.")

    now = _now_utc()
    active = _get_profile_by_status(session, source_id, "active")
    if active is not None:
        active.status = "archived"
        active.archived_at = now
        session.add(active)

    normalized = _storage_payload_from_row(draft)
    if edit_note is not None:
        normalized["edit_note"] = edit_note
    normalized = normalize_source_preference_profile_payload(normalized)

    next_active = SourcePreferenceProfile(
        source_id=source_id,
        status="active",
        schema_version=SOURCE_PREFERENCE_PROFILE_SCHEMA_VERSION,
        activated_at=now,
    )
    _apply_normalized_payload(next_active, normalized)
    session.add(next_active)
    session.flush()
    next_active.profile_version = f"source-{source_id}-profile-{next_active.id}"
    rendered = render_source_preference_profile_prompt_context(
        normalized,
        profile_version=next_active.profile_version,
    )
    next_active.prompt_context_json = rendered.profile
    next_active.prompt_context_stats_json = rendered.render_stats
    session.delete(draft)
    session.flush()
    return get_source_preference_profile_state(session, source_id)


def delete_source_preference_profile_draft(session: Session, source_id: int) -> SourcePreferenceProfileStateRead:
    _require_source(session, source_id)
    draft = _get_profile_by_status(session, source_id, "draft")
    if draft is not None:
        session.delete(draft)
        session.flush()
    return get_source_preference_profile_state(session, source_id)


def get_active_source_preference_profile_context(
    session: Session,
    source_id: int | None,
) -> SourcePreferenceProfileContext:
    if source_id is None:
        return empty_source_preference_profile_context()
    return source_preference_profile_context_from_row(_get_profile_by_status(session, source_id, "active"))


def load_active_source_preference_profile_contexts(
    session: Session,
    source_ids: list[int] | tuple[int, ...] | set[int],
) -> dict[int, SourcePreferenceProfileContext]:
    normalized_source_ids = sorted({source_id for source_id in source_ids if source_id is not None})
    if not normalized_source_ids:
        return {}
    rows = list(
        session.scalars(
            select(SourcePreferenceProfile)
            .where(SourcePreferenceProfile.source_id.in_(normalized_source_ids))
            .where(SourcePreferenceProfile.status == "active")
        )
    )
    rows_by_source_id = {row.source_id: row for row in rows}
    return {
        source_id: source_preference_profile_context_from_row(rows_by_source_id.get(source_id))
        for source_id in normalized_source_ids
    }


def source_preference_profile_context_from_row(
    row: SourcePreferenceProfile | None,
) -> SourcePreferenceProfileContext:
    if row is None:
        return empty_source_preference_profile_context()
    profile = copy.deepcopy(row.prompt_context_json or {})
    stats = copy.deepcopy(row.prompt_context_stats_json or {})
    if not profile or "is_empty" not in profile:
        normalized = _storage_payload_from_row(row)
        rendered = render_source_preference_profile_prompt_context(
            normalized,
            profile_version=row.profile_version,
        )
        return rendered
    return SourcePreferenceProfileContext(
        profile=profile,
        profile_version=profile.get("profile_version"),
        is_empty=bool(profile.get("is_empty", True)),
        render_stats=stats or copy.deepcopy(profile.get("render_stats") or {}),
    )


def empty_source_preference_profile_context() -> SourcePreferenceProfileContext:
    profile = copy.deepcopy(CANONICAL_EMPTY_SOURCE_PROFILE)
    profile["render_stats"]["rendered_byte_count"] = _json_size(profile)
    return SourcePreferenceProfileContext(
        profile=profile,
        profile_version=None,
        is_empty=True,
        render_stats=copy.deepcopy(profile["render_stats"]),
    )


def normalize_source_preference_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[SourcePreferenceProfileFieldError] = []
    normalized: dict[str, Any] = _empty_storage_payload()

    for field_name in ("subscription_reason", "quality_notes", "edit_note"):
        if field_name in payload:
            normalized[field_name] = _clean_optional_text(payload.get(field_name))

    for field_name in (
        "good_article_criteria",
        "bad_article_patterns",
        "positive_example_summaries",
        "negative_example_summaries",
    ):
        if field_name in payload:
            normalized[field_name] = _clean_text_list(payload.get(field_name), field_name=field_name)

    for field_name, canonicalizer in (
        ("preferred_topics", canonicalize_taxonomy_id),
        ("ignored_topics", canonicalize_taxonomy_id),
        ("preferred_content_types", canonicalize_content_type_id),
    ):
        if field_name in payload:
            normalized[field_name] = _clean_controlled_id_list(
                payload.get(field_name),
                field_name=field_name,
                canonicalizer=canonicalizer,
                errors=errors,
            )

    for field_name in ("positive_example_item_ids", "negative_example_item_ids"):
        if field_name in payload:
            normalized[field_name] = _clean_item_id_list(payload.get(field_name), field_name=field_name, errors=errors)

    source_type = _clean_optional_text(payload.get("source_type")) if "source_type" in payload else normalized["source_type"]
    normalized["source_type"] = source_type if source_type in {"user_edit", "codex_import", "future_suggestion"} else "user_edit"

    _validate_size_limits(normalized, errors)
    _validate_prompt_injection(normalized, errors)
    _validate_profile_json_limit(normalized, errors)
    if errors:
        raise SourcePreferenceProfileValidationError(errors)
    return normalized


def render_source_preference_profile_prompt_context(
    normalized: dict[str, Any],
    *,
    profile_version: str | None,
) -> SourcePreferenceProfileContext:
    is_empty = _profile_payload_is_empty(normalized)
    payload = _empty_prompt_context(profile_version=profile_version, is_empty=is_empty)
    included_counts: dict[str, int] = {}
    omitted_counts: dict[str, int] = {}
    capped_sections: list[str] = []

    for section in PROMPT_CONTEXT_SECTION_ORDER:
        value = normalized.get(section)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            included: list[Any] = []
            for item in value:
                candidate = copy.deepcopy(payload)
                candidate[section] = [*included, item]
                if _json_size(candidate) <= SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES:
                    included.append(item)
                    continue
                capped_sections.append(section)
                break
            payload[section] = included
            included_counts[section] = len(included)
            omitted = max(0, len(value) - len(included))
            if omitted:
                omitted_counts[section] = omitted
            continue

        if isinstance(value, str):
            candidate = copy.deepcopy(payload)
            candidate[section] = value
            if _json_size(candidate) <= SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES:
                payload[section] = value
                included_counts[section] = 1
                continue
            capped_sections.append(section)
            truncated = _fit_text_value(payload, section, value)
            payload[section] = truncated or None
            included_counts[section] = 1 if truncated else 0
            if truncated != value:
                omitted_counts[section] = _utf8_len(value) - _utf8_len(truncated or "")

    stats = {
        "max_rendered_bytes": SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES,
        "rendered_byte_count": 0,
        "included_counts": included_counts,
        "omitted_counts": omitted_counts,
        "capped_sections": sorted(set(capped_sections), key=capped_sections.index),
    }
    _finalize_render_stats(payload, stats)
    return SourcePreferenceProfileContext(
        profile=payload,
        profile_version=profile_version,
        is_empty=is_empty,
        render_stats=stats,
    )


def _require_source(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise SourcePreferenceProfileNotFoundError("Source not found.")
    return source


def _get_profile_by_status(
    session: Session,
    source_id: int,
    status: str,
) -> SourcePreferenceProfile | None:
    return session.scalar(
        select(SourcePreferenceProfile)
        .where(SourcePreferenceProfile.source_id == source_id)
        .where(SourcePreferenceProfile.status == status)
        .order_by(desc(SourcePreferenceProfile.id))
    )


def _list_profile_history(session: Session, source_id: int) -> list[SourcePreferenceProfile]:
    return list(
        session.scalars(
            select(SourcePreferenceProfile)
            .where(SourcePreferenceProfile.source_id == source_id)
            .where(SourcePreferenceProfile.status == "archived")
            .order_by(desc(SourcePreferenceProfile.archived_at), desc(SourcePreferenceProfile.id))
        )
    )


def _build_profile_read(profile: SourcePreferenceProfile) -> SourcePreferenceProfileRead:
    return SourcePreferenceProfileRead(
        id=profile.id,
        source_id=profile.source_id,
        status=profile.status,  # type: ignore[arg-type]
        schema_version=profile.schema_version,
        profile_version=profile.profile_version,
        subscription_reason=profile.subscription_reason,
        good_article_criteria=_list_string_values(profile.good_article_criteria_json),
        bad_article_patterns=_list_string_values(profile.bad_article_patterns_json),
        preferred_topics=_list_string_values(profile.preferred_topics_json),
        ignored_topics=_list_string_values(profile.ignored_topics_json),
        preferred_content_types=_list_string_values(profile.preferred_content_types_json),
        positive_example_item_ids=_list_int_values(profile.positive_example_item_ids_json),
        negative_example_item_ids=_list_int_values(profile.negative_example_item_ids_json),
        positive_example_summaries=_list_string_values(profile.positive_example_summaries_json),
        negative_example_summaries=_list_string_values(profile.negative_example_summaries_json),
        quality_notes=profile.quality_notes,
        prompt_context=profile.prompt_context_json or {},
        prompt_context_stats=profile.prompt_context_stats_json or {},
        source_type=profile.source_type,
        edit_note=profile.edit_note,
        activated_at=profile.activated_at,
        archived_at=profile.archived_at,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _apply_normalized_payload(profile: SourcePreferenceProfile, normalized: dict[str, Any]) -> None:
    profile.subscription_reason = normalized["subscription_reason"]
    profile.good_article_criteria_json = normalized["good_article_criteria"]
    profile.bad_article_patterns_json = normalized["bad_article_patterns"]
    profile.preferred_topics_json = normalized["preferred_topics"]
    profile.ignored_topics_json = normalized["ignored_topics"]
    profile.preferred_content_types_json = normalized["preferred_content_types"]
    profile.positive_example_item_ids_json = normalized["positive_example_item_ids"]
    profile.negative_example_item_ids_json = normalized["negative_example_item_ids"]
    profile.positive_example_summaries_json = normalized["positive_example_summaries"]
    profile.negative_example_summaries_json = normalized["negative_example_summaries"]
    profile.quality_notes = normalized["quality_notes"]
    profile.source_type = normalized["source_type"]
    profile.edit_note = normalized["edit_note"]


def _storage_payload_from_row(profile: SourcePreferenceProfile | None) -> dict[str, Any]:
    if profile is None:
        return _empty_storage_payload()
    return {
        "subscription_reason": profile.subscription_reason,
        "good_article_criteria": _list_string_values(profile.good_article_criteria_json),
        "bad_article_patterns": _list_string_values(profile.bad_article_patterns_json),
        "preferred_topics": _list_string_values(profile.preferred_topics_json),
        "ignored_topics": _list_string_values(profile.ignored_topics_json),
        "preferred_content_types": _list_string_values(profile.preferred_content_types_json),
        "positive_example_item_ids": _list_int_values(profile.positive_example_item_ids_json),
        "negative_example_item_ids": _list_int_values(profile.negative_example_item_ids_json),
        "positive_example_summaries": _list_string_values(profile.positive_example_summaries_json),
        "negative_example_summaries": _list_string_values(profile.negative_example_summaries_json),
        "quality_notes": profile.quality_notes,
        "source_type": profile.source_type or "user_edit",
        "edit_note": profile.edit_note,
    }


def _empty_storage_payload() -> dict[str, Any]:
    return {
        "subscription_reason": None,
        "good_article_criteria": [],
        "bad_article_patterns": [],
        "preferred_topics": [],
        "ignored_topics": [],
        "preferred_content_types": [],
        "positive_example_item_ids": [],
        "negative_example_item_ids": [],
        "positive_example_summaries": [],
        "negative_example_summaries": [],
        "quality_notes": None,
        "source_type": "user_edit",
        "edit_note": None,
    }


def _empty_prompt_context(*, profile_version: str | None, is_empty: bool) -> dict[str, Any]:
    return {
        "subscription_reason": None,
        "good_article_criteria": [],
        "bad_article_patterns": [],
        "preferred_topics": [],
        "ignored_topics": [],
        "preferred_content_types": [],
        "positive_example_summaries": [],
        "negative_example_summaries": [],
        "quality_notes": None,
        "profile_version": profile_version,
        "is_empty": is_empty,
        "render_stats": {},
    }


def _clean_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _clean_text_list(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        cleaned.append(text)
        seen.add(text)
    return cleaned


def _clean_controlled_id_list(
    value: object,
    *,
    field_name: str,
    canonicalizer,
    errors: list[SourcePreferenceProfileFieldError],
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value if isinstance(value, list) else []):
        raw = str(item).strip()
        canonical = canonicalizer(raw)
        if canonical is None:
            errors.append(
                _field_error(
                    field=f"{field_name}[{index}]",
                    code="invalid_controlled_id",
                    message=f"{field_name}[{index}] is not a known controlled id: {raw}",
                )
            )
            continue
        if canonical not in seen:
            cleaned.append(canonical)
            seen.add(canonical)
    return cleaned


def _clean_item_id_list(
    value: object,
    *,
    field_name: str,
    errors: list[SourcePreferenceProfileFieldError],
) -> list[int]:
    cleaned: list[int] = []
    seen: set[int] = set()
    for index, item in enumerate(value if isinstance(value, list) else []):
        try:
            item_id = int(item)
        except (TypeError, ValueError):
            errors.append(
                _field_error(
                    field=f"{field_name}[{index}]",
                    code="invalid_item_id",
                    message=f"{field_name}[{index}] must be a positive integer.",
                )
            )
            continue
        if item_id <= 0:
            errors.append(
                _field_error(
                    field=f"{field_name}[{index}]",
                    code="invalid_item_id",
                    message=f"{field_name}[{index}] must be a positive integer.",
                )
            )
            continue
        if item_id not in seen:
            cleaned.append(item_id)
            seen.add(item_id)
    return cleaned


def _validate_size_limits(normalized: dict[str, Any], errors: list[SourcePreferenceProfileFieldError]) -> None:
    for field_name, limit in TEXT_FIELD_LIMITS.items():
        value = normalized.get(field_name)
        if isinstance(value, str):
            actual = _utf8_len(value)
            if actual > limit:
                errors.append(_limit_error(field_name, actual=actual, limit=limit, unit="bytes"))

    for field_name, (entry_limit, item_limit) in LIST_FIELD_LIMITS.items():
        values = normalized.get(field_name)
        if not isinstance(values, list):
            continue
        if len(values) > entry_limit:
            errors.append(_limit_error(field_name, actual=len(values), limit=entry_limit, unit="entries"))
        if item_limit is None:
            continue
        for index, item in enumerate(values):
            actual = _utf8_len(str(item))
            if actual > item_limit:
                errors.append(_limit_error(f"{field_name}[{index}]", actual=actual, limit=item_limit, unit="bytes"))

    total_prompt_text = sum(
        _utf8_len(str(value))
        for field_name in PROMPT_CONTEXT_FIELDS
        for value in _iter_field_texts(normalized.get(field_name))
    )
    if total_prompt_text > MAX_TOTAL_PROMPT_TEXT_BYTES:
        errors.append(
            _limit_error(
                "prompt_affecting_text",
                actual=total_prompt_text,
                limit=MAX_TOTAL_PROMPT_TEXT_BYTES,
                unit="bytes",
            )
        )


def _validate_prompt_injection(normalized: dict[str, Any], errors: list[SourcePreferenceProfileFieldError]) -> None:
    for field_name in PROMPT_CONTEXT_FIELDS:
        value = normalized.get(field_name)
        for field_path, text in _iter_field_texts_with_path(field_name, value):
            if _has_invalid_control_chars(text):
                errors.append(
                    _field_error(
                        field=field_path,
                        code="prompt_injection_rejected",
                        pattern_class="control_characters",
                        message=f'{field_path} contains invisible control characters. This text enters the AI prompt; rewrite it as ordinary preference guidance.',
                    )
                )
            for pattern_class, pattern in FORBIDDEN_PROMPT_PATTERNS:
                match = pattern.search(text)
                if not match:
                    continue
                errors.append(
                    _field_error(
                        field=field_path,
                        code="prompt_injection_rejected",
                        pattern_class=pattern_class,
                        message=f'{field_path} contains "{_preview_rejected_text(match.group(0))}". This text enters the AI prompt; rewrite it as ordinary preference guidance.',
                    )
                )


def _validate_profile_json_limit(normalized: dict[str, Any], errors: list[SourcePreferenceProfileFieldError]) -> None:
    actual = _json_size({field: normalized.get(field) for field in STORAGE_FIELDS})
    if actual > MAX_NORMALIZED_PROFILE_JSON_BYTES:
        errors.append(
            _limit_error(
                "normalized_profile_json",
                actual=actual,
                limit=MAX_NORMALIZED_PROFILE_JSON_BYTES,
                unit="bytes",
            )
        )


def _preview_rejected_text(text: str, *, max_length: int = 80) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_length:
        return collapsed
    return f"{collapsed[:max_length]}..."


def _profile_payload_is_empty(normalized: dict[str, Any]) -> bool:
    return not any(bool(normalized.get(field)) for field in PROMPT_CONTEXT_FIELDS)


def _enforce_render_cap(payload: dict[str, Any], stats: dict[str, Any]) -> None:
    while _json_size(payload) > SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES:
        section = _last_non_empty_prompt_section(payload)
        if section is None:
            break
        value = payload.get(section)
        capped_sections = stats.setdefault("capped_sections", [])
        if section not in capped_sections:
            capped_sections.append(section)
        omitted_counts = stats.setdefault("omitted_counts", {})
        included_counts = stats.setdefault("included_counts", {})
        if isinstance(value, list):
            if not value:
                payload[section] = []
                included_counts[section] = 0
                break
            value.pop()
            included_counts[section] = len(value)
            omitted_counts[section] = int(omitted_counts.get(section, 0)) + 1
            continue
        if isinstance(value, str):
            previous_length = _utf8_len(value)
            truncated = _fit_text_value(payload, section, value)
            if truncated == value:
                truncated = value[:-1].rstrip()
            payload[section] = truncated or None
            included_counts[section] = 1 if truncated else 0
            omitted_counts[section] = int(omitted_counts.get(section, 0)) + max(
                0,
                previous_length - _utf8_len(truncated or ""),
            )
            continue
        payload[section] = [] if isinstance(value, list) else None


def _finalize_render_stats(payload: dict[str, Any], stats: dict[str, Any]) -> None:
    payload["render_stats"] = stats
    for _ in range(50):
        measured_size = _json_size(payload)
        stats["rendered_byte_count"] = measured_size
        _enforce_render_cap(payload, stats)
        actual_size = _json_size(payload)
        if actual_size <= SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES and actual_size == measured_size:
            return

    _enforce_render_cap(payload, stats)
    stats["rendered_byte_count"] = _json_size(payload)
    if _json_size(payload) > SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES:
        _enforce_render_cap(payload, stats)
        stats["rendered_byte_count"] = _json_size(payload)


def _last_non_empty_prompt_section(payload: dict[str, Any]) -> str | None:
    for section in reversed(PROMPT_CONTEXT_SECTION_ORDER):
        value = payload.get(section)
        if value not in (None, "", []):
            return section
    return None


def _iter_field_texts(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item


def _iter_field_texts_with_path(field_name: str, value: object):
    if isinstance(value, str):
        yield field_name, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                yield f"{field_name}[{index}]", item


def _has_invalid_control_chars(value: str) -> bool:
    return any(
        (ord(char) < 32 and char not in {"\n", "\r", "\t"}) or char in BIDI_OVERRIDE_CHARS
        for char in value
    )


def _fit_text_value(payload: dict[str, Any], section: str, value: str) -> str:
    low = 0
    high = len(value)
    best = ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate_value = value[:midpoint].rstrip()
        candidate = copy.deepcopy(payload)
        candidate[section] = candidate_value
        if _json_size(candidate) <= SOURCE_PREFERENCE_PROFILE_RENDER_MAX_BYTES:
            best = candidate_value
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _list_string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _list_int_values(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    values: list[int] = []
    for item in value:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    return values


def _limit_error(field: str, *, actual: int, limit: int, unit: str) -> SourcePreferenceProfileFieldError:
    return _field_error(
        field=field,
        code="limit_exceeded",
        actual=actual,
        limit=limit,
        unit=unit,
        message=f"{field} is {actual} {unit}; limit is {limit} {unit}.",
    )


def _field_error(
    *,
    field: str,
    code: str,
    message: str,
    actual: int | None = None,
    limit: int | None = None,
    unit: str | None = None,
    pattern_class: str | None = None,
) -> SourcePreferenceProfileFieldError:
    return SourcePreferenceProfileFieldError(
        field=field,
        code=code,
        actual=actual,
        limit=limit,
        unit=unit,
        pattern_class=pattern_class,
        message=message,
    )


def _json_size(value: object) -> int:
    return len(_json_bytes(value))


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
