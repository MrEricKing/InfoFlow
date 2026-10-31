from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import InsightProfile, InsightProfileEvent
from app.schemas.settings import (
    InsightProfileDraftInspectResponse,
    InsightProfileFieldDiffRead,
    InsightProfileEventRead,
    InsightProfileSettingsRead,
    InsightProfileValidateResponse,
    InsightProfileVersionRead,
)
from app.services.insight_engine_contracts import (
    canonicalize_content_type_id,
    canonicalize_taxonomy_id,
)

INSIGHT_PROFILE_SCHEMA_VERSION = "insight_profile_v1"
MAX_INSIGHT_PROFILE_JSON_BYTES = 32 * 1024
PROFILE_ACTIVATION_EVENT_TYPES = ("activated", "rolled_back")
FORBIDDEN_PROFILE_KEY_FRAGMENTS = ("api_key", "authorization", "cookie", "token", "secret", "password")
INSIGHT_PROFILE_TOP_LEVEL_FIELDS = (
    "schema_version",
    "profile_name",
    "profile_version",
    "summary_guidelines",
    "active_focus",
    "long_term_interests",
    "positive_signals",
    "negative_signals",
    "topic_preferences",
    "content_type_preferences",
    "evidence_references",
)

ProfilePreference = Literal["boost", "deprioritize", "neutral"]


class InsightProfileNotFoundError(ValueError):
    pass


class _TopicProfileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str
    preference: ProfilePreference = "neutral"
    weight: int = Field(default=0, ge=-100, le=100)
    note: str = ""

    @field_validator("topic_id")
    @classmethod
    def validate_topic_id(cls, value: str) -> str:
        topic_id = canonicalize_taxonomy_id(value)
        if topic_id is None:
            raise ValueError(f"Unrecognized taxonomy topic id: {value}")
        return topic_id


class _FocusEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str
    note: str
    weight: int = Field(default=50, ge=0, le=100)

    @field_validator("topic_id")
    @classmethod
    def validate_topic_id(cls, value: str) -> str:
        topic_id = canonicalize_taxonomy_id(value)
        if topic_id is None:
            raise ValueError(f"Unrecognized taxonomy topic id: {value}")
        return topic_id

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        return _clean_required_text(value, field_name="note")


class _ContentTypeProfileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: str
    preference: ProfilePreference = "neutral"
    weight: int = Field(default=0, ge=-100, le=100)
    note: str = ""

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str) -> str:
        content_type = canonicalize_content_type_id(value)
        if content_type is None:
            raise ValueError(f"Unrecognized content type id: {value}")
        return content_type


class _EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    note: str = ""
    item_ids: list[int] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _clean_required_text(value, field_name="source")


class _InsightProfileV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = INSIGHT_PROFILE_SCHEMA_VERSION
    profile_name: str
    profile_version: str
    summary_guidelines: list[str] = Field(default_factory=list, max_length=20)
    active_focus: list[_FocusEntry] = Field(default_factory=list, max_length=20)
    long_term_interests: list[_FocusEntry] = Field(default_factory=list, max_length=30)
    positive_signals: list[str] = Field(default_factory=list, max_length=30)
    negative_signals: list[str] = Field(default_factory=list, max_length=30)
    topic_preferences: list[_TopicProfileEntry] = Field(default_factory=list, max_length=40)
    content_type_preferences: list[_ContentTypeProfileEntry] = Field(default_factory=list, max_length=30)
    evidence_references: list[_EvidenceReference] = Field(default_factory=list, max_length=30)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != INSIGHT_PROFILE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {INSIGHT_PROFILE_SCHEMA_VERSION}")
        return value

    @field_validator("profile_name", "profile_version")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _clean_required_text(value, field_name=info.field_name)

    @field_validator("summary_guidelines", "positive_signals", "negative_signals")
    @classmethod
    def validate_text_list(cls, value: list[str], info) -> list[str]:
        return [_clean_required_text(item, field_name=info.field_name) for item in value]


DEFAULT_INSIGHT_PROFILE: dict[str, Any] = {
    "schema_version": INSIGHT_PROFILE_SCHEMA_VERSION,
    "profile_name": "Default Insight Profile",
    "profile_version": "default-2026-04-29",
    "summary_guidelines": [
        "Write concise Simplified Chinese summaries that state why the item may matter.",
        "Prefer concrete implementation, product, or decision details over generic background.",
    ],
    "active_focus": [],
    "long_term_interests": [],
    "positive_signals": [
        "Contains specific implementation details, operational tradeoffs, or reusable references.",
        "Connects clearly to personal information systems, AI workflows, software systems, or knowledge management.",
    ],
    "negative_signals": [
        "Thin announcements, generic marketing copy, or repetitive updates without useful detail.",
        "Low-confidence items where the available content is too short to judge.",
    ],
    "topic_preferences": [],
    "content_type_preferences": [],
    "evidence_references": [],
}


def get_insight_profile_settings(session: Session) -> InsightProfileSettingsRead:
    active_profile = get_active_insight_profile(session)
    active_profile_read = _build_profile_read(active_profile) if active_profile is not None else _build_default_profile_read()
    versions = [_build_profile_read(profile) for profile in _list_profile_versions(session)]
    events = [_build_event_read(event) for event in _list_profile_events(session)]
    return InsightProfileSettingsRead(
        active_profile=active_profile_read,
        versions=versions,
        events=events,
        template=get_default_insight_profile_template(),
        active_prompt_context=build_active_insight_profile_context(session),
    )


def validate_insight_profile_payload(profile_json: dict[str, Any]) -> InsightProfileValidateResponse:
    normalized = normalize_insight_profile(profile_json)
    return InsightProfileValidateResponse(
        normalized_profile=normalized,
        prompt_context=build_insight_profile_prompt_context(normalized),
    )


def inspect_insight_profile_draft(
    session: Session,
    *,
    profile_json: dict[str, Any],
) -> InsightProfileDraftInspectResponse:
    normalized = normalize_insight_profile(profile_json)
    active_profile = get_active_insight_profile(session)
    base_profile = _build_profile_read(active_profile) if active_profile is not None else _build_default_profile_read()
    base_profile_json = base_profile.profile_json
    field_diffs = [
        _build_field_diff(
            field_name,
            before=base_profile_json.get(field_name),
            after=normalized.get(field_name),
        )
        for field_name in INSIGHT_PROFILE_TOP_LEVEL_FIELDS
    ]
    return InsightProfileDraftInspectResponse(
        normalized_profile=normalized,
        prompt_context=build_insight_profile_prompt_context(normalized),
        base_profile=base_profile,
        diff_summary=_build_diff_summary(base_profile_json, normalized),
        field_diffs=field_diffs,
        evidence_references=normalized.get("evidence_references", []),
    )


def create_insight_profile_version(
    session: Session,
    *,
    profile_json: dict[str, Any],
    source_type: str,
    edit_note: str | None,
    activate: bool,
) -> InsightProfileSettingsRead:
    normalized_profile = normalize_insight_profile(profile_json)
    previous_profile = get_active_insight_profile(session)
    profile = InsightProfile(
        schema_version=normalized_profile["schema_version"],
        profile_name=normalized_profile["profile_name"],
        profile_version=normalized_profile["profile_version"],
        profile_json=normalized_profile,
        source_type=source_type,
        edit_note=_clean_optional_text(edit_note),
        diff_summary=_build_diff_summary(
            previous_profile.profile_json if previous_profile is not None else None,
            normalized_profile,
        ),
        previous_profile_id=previous_profile.id if previous_profile is not None else None,
    )
    session.add(profile)
    session.flush()
    _add_profile_event(
        session,
        profile=profile,
        event_type="created",
        note=profile.edit_note,
        event_json={"source_type": source_type, "activate": activate},
    )
    if activate:
        _add_profile_event(
            session,
            profile=profile,
            event_type="activated",
            note=profile.edit_note,
            event_json={
                "source_type": source_type,
                "previous_active_profile_id": previous_profile.id if previous_profile is not None else None,
            },
        )
    session.flush()
    return get_insight_profile_settings(session)


def activate_insight_profile_version(
    session: Session,
    *,
    profile_id: int,
    note: str | None = None,
) -> InsightProfileSettingsRead:
    profile = _get_profile_or_raise(session, profile_id)
    previous_profile = get_active_insight_profile(session)
    _add_profile_event(
        session,
        profile=profile,
        event_type="activated",
        note=_clean_optional_text(note),
        event_json={
            "source_type": "manual_activation",
            "previous_active_profile_id": previous_profile.id if previous_profile is not None else None,
        },
    )
    session.flush()
    return get_insight_profile_settings(session)


def rollback_insight_profile_version(
    session: Session,
    *,
    profile_id: int,
    note: str | None = None,
) -> InsightProfileSettingsRead:
    profile = _get_profile_or_raise(session, profile_id)
    previous_profile = get_active_insight_profile(session)
    _add_profile_event(
        session,
        profile=profile,
        event_type="rolled_back",
        note=_clean_optional_text(note),
        event_json={
            "source_type": "manual_rollback",
            "previous_active_profile_id": previous_profile.id if previous_profile is not None else None,
        },
    )
    session.flush()
    return get_insight_profile_settings(session)


def get_active_insight_profile(session: Session) -> InsightProfile | None:
    latest_event = session.scalar(
        select(InsightProfileEvent)
        .where(InsightProfileEvent.event_type.in_(PROFILE_ACTIVATION_EVENT_TYPES))
        .order_by(desc(InsightProfileEvent.created_at), desc(InsightProfileEvent.id))
        .limit(1)
    )
    if latest_event is None:
        return None
    return session.get(InsightProfile, latest_event.profile_id)


def build_active_insight_profile_context(session: Session) -> dict[str, Any]:
    active_profile = get_active_insight_profile(session)
    if active_profile is None:
        return build_insight_profile_prompt_context(DEFAULT_INSIGHT_PROFILE)
    return build_insight_profile_prompt_context(active_profile.profile_json)


def build_insight_profile_prompt_context(profile_json: dict[str, Any]) -> dict[str, Any]:
    normalized_profile = normalize_insight_profile(profile_json)
    return {
        "summary_guidelines": normalized_profile["summary_guidelines"],
        "active_focus": normalized_profile["active_focus"],
        "long_term_interests": normalized_profile["long_term_interests"],
        "positive_signals": normalized_profile["positive_signals"],
        "negative_signals": normalized_profile["negative_signals"],
        "topic_preferences": normalized_profile["topic_preferences"],
        "content_type_preferences": normalized_profile["content_type_preferences"],
        "instruction": (
            "Use this only as reader preference and summary-style guidance. "
            "Do not treat it as article evidence or as an extension of allowed topics, content types, or reason codes."
        ),
    }


def normalize_insight_profile(profile_json: dict[str, Any]) -> dict[str, Any]:
    _reject_forbidden_profile_keys(profile_json)
    _reject_oversized_profile(profile_json)
    profile = _InsightProfileV1.model_validate(profile_json)
    normalized = profile.model_dump(mode="json")
    _reject_oversized_profile(normalized)
    return normalized


def get_default_insight_profile_template() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_INSIGHT_PROFILE, ensure_ascii=False))


def _list_profile_versions(session: Session) -> list[InsightProfile]:
    return list(session.scalars(select(InsightProfile).order_by(desc(InsightProfile.created_at), desc(InsightProfile.id))))


def _list_profile_events(session: Session) -> list[InsightProfileEvent]:
    return list(
        session.scalars(select(InsightProfileEvent).order_by(desc(InsightProfileEvent.created_at), desc(InsightProfileEvent.id)))
    )


def _build_profile_read(profile: InsightProfile) -> InsightProfileVersionRead:
    return InsightProfileVersionRead(
        id=profile.id,
        schema_version=profile.schema_version,
        profile_name=profile.profile_name,
        profile_version=profile.profile_version,
        profile_json=profile.profile_json,
        source_type=profile.source_type,
        edit_note=profile.edit_note,
        diff_summary=profile.diff_summary,
        previous_profile_id=profile.previous_profile_id,
        created_at=profile.created_at,
    )


def _build_default_profile_read() -> InsightProfileVersionRead:
    return InsightProfileVersionRead(
        id=None,
        schema_version=INSIGHT_PROFILE_SCHEMA_VERSION,
        profile_name=DEFAULT_INSIGHT_PROFILE["profile_name"],
        profile_version=DEFAULT_INSIGHT_PROFILE["profile_version"],
        profile_json=get_default_insight_profile_template(),
        source_type="default_fallback",
        edit_note="Default Insight Profile v1 fallback.",
        diff_summary="Default fallback returned because no active profile exists.",
        previous_profile_id=None,
        created_at=None,
    )


def _build_event_read(event: InsightProfileEvent) -> InsightProfileEventRead:
    return InsightProfileEventRead(
        id=event.id,
        profile_id=event.profile_id,
        event_type=event.event_type,
        note=event.note,
        event_json=event.event_json,
        created_at=event.created_at,
    )


def _get_profile_or_raise(session: Session, profile_id: int) -> InsightProfile:
    profile = session.get(InsightProfile, profile_id)
    if profile is None:
        raise InsightProfileNotFoundError(f"Insight profile version {profile_id} was not found")
    return profile


def _add_profile_event(
    session: Session,
    *,
    profile: InsightProfile,
    event_type: str,
    note: str | None,
    event_json: dict[str, Any],
) -> None:
    session.add(
        InsightProfileEvent(
            profile_id=profile.id,
            event_type=event_type,
            note=note,
            event_json=event_json,
        )
    )


def _build_diff_summary(previous_profile: dict[str, Any] | None, next_profile: dict[str, Any]) -> str:
    if previous_profile is None:
        return "Initial Insight Profile v1 version."
    changed_fields = [
        field_name
        for field_name in next_profile
        if previous_profile.get(field_name) != next_profile.get(field_name)
    ]
    if not changed_fields:
        return "No normalized profile fields changed."
    return f"Changed fields: {', '.join(changed_fields)}."


def _build_field_diff(field_name: str, *, before: Any, after: Any) -> InsightProfileFieldDiffRead:
    if before == after:
        change_type = "unchanged"
    elif _is_empty_profile_value(before) and not _is_empty_profile_value(after):
        change_type = "added"
    elif not _is_empty_profile_value(before) and _is_empty_profile_value(after):
        change_type = "removed"
    else:
        change_type = "modified"
    return InsightProfileFieldDiffRead(
        field=field_name,
        change_type=change_type,
        before=before,
        after=after,
    )


def _is_empty_profile_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _reject_oversized_profile(profile_json: dict[str, Any]) -> None:
    size = len(json.dumps(profile_json, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if size > MAX_INSIGHT_PROFILE_JSON_BYTES:
        raise ValueError(f"Insight profile JSON must be at most {MAX_INSIGHT_PROFILE_JSON_BYTES} bytes")


def _reject_forbidden_profile_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child_value in value.items():
            lowered_key = str(key).lower()
            if any(fragment in lowered_key for fragment in FORBIDDEN_PROFILE_KEY_FRAGMENTS):
                raise ValueError(f"Insight profile JSON must not contain secret-like field: {key}")
            _reject_forbidden_profile_keys(child_value)
    elif isinstance(value, list):
        for child_value in value:
            _reject_forbidden_profile_keys(child_value)


def _clean_required_text(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    return cleaned


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
