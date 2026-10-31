from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    InsightEngineArtifact,
    SavedItem,
    SavedItemAsset,
    SavedItemCuratedInsight,
    SavedItemCuratedInsightEvent,
    SavedSnapshot,
    Source,
    SourceItemOccurrence,
)
from app.schemas.knowledge import (
    SavedItemAIInsightRead,
    SavedItemCuratedInsightEventRead,
    SavedItemCuratedInsightRead,
    SavedItemInsightPayloadRead,
    SavedItemResolvedInsightRead,
)
from app.services.insight_engine_artifacts import build_latest_occurrence_subquery
from app.services.ai_provider import AIProviderError, parse_json_object_content, request_chat_completion_json
from app.services.app_settings import (
    AI_PROFILE_GENERAL,
    AI_PROFILE_SENSITIVE,
    AI_SOURCE_ROUTES,
    AIProviderConfig,
    resolve_ai_provider_config_for_source_route,
)
from app.services.insight_engine_contracts import (
    CONTENT_TYPE_FALLBACK_ID,
    TAXONOMY_FALLBACK_ID,
    build_content_type_options_context,
    build_taxonomy_context,
    canonicalize_content_type_id,
    canonicalize_taxonomy_id,
    get_content_type_label,
    get_taxonomy_label,
)
from app.services.insight_profiles import build_active_insight_profile_context

SAVED_ITEM_INSIGHT_ARTIFACT_KIND = "saved_item_insight_v1"
SAVED_ITEM_INSIGHT_PROMPT_VERSION = "saved-item-insight-v1"
SAVED_ITEM_INSIGHT_RESPONSE_SCHEMA_NAME = "saved_item_insight"
MAX_SAVED_ITEM_INSIGHT_INPUT_CHARS = 6000

ORIGIN_TYPE_FEED_SAVED = "feed_saved"
ORIGIN_TYPE_MANUAL_URL = "manual_url"
ORIGIN_TYPE_MARKDOWN_UPLOAD = "markdown_upload"
ORIGIN_TYPE_HTML_UPLOAD = "html_upload"
ORIGIN_TYPE_SINGLEFILE_HTML_UPLOAD = "singlefile_html_upload"
EVIDENCE_SOURCE_KIND_EXTERNAL = "external_source"
DEFAULT_SAVED_ITEM_AI_ROUTE = AI_PROFILE_SENSITIVE
EXTERNAL_SOURCE_ORIGIN_TYPES = frozenset(
    (
        ORIGIN_TYPE_MANUAL_URL,
        ORIGIN_TYPE_MARKDOWN_UPLOAD,
        ORIGIN_TYPE_HTML_UPLOAD,
        ORIGIN_TYPE_SINGLEFILE_HTML_UPLOAD,
    )
)

CANONICAL_SNAPSHOT_KIND = "canonical_content_v1"
CAPTURE_PAGE_METADATA_SNAPSHOT_KIND = "capture_page_metadata_v1"
SINGLEFILE_HTML_SNAPSHOT_KIND = "singlefile_html_v1"
CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND = "crawl4ai_raw_markdown_v1"
CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND = "crawl4ai_fit_markdown_v1"
CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND = "crawl4ai_fit_image_reinserted_markdown_v1"
UPLOADED_MARKDOWN_SNAPSHOT_KIND = "uploaded_markdown_v1"

CURATED_INSIGHT_CREATED = "curated_insight_created"
CURATED_INSIGHT_UPDATED = "curated_insight_updated"
CURATED_INSIGHT_CLEARED = "curated_insight_cleared"

_INPUT_SNAPSHOT_PRIORITY = (
    UPLOADED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
)
_WHITESPACE_PATTERN = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINE_PATTERN = re.compile(r"\n{3,}")


class SavedItemInsightError(RuntimeError):
    pass


class SavedItemInsightGenerationError(SavedItemInsightError):
    pass


class SavedItemInsightConfigurationError(SavedItemInsightGenerationError):
    pass


class SavedItemInsightValidationError(SavedItemInsightError):
    pass


class _AISavedItemInsightResponsePayload(BaseModel):
    summary: str | None = None
    long_term_value: str | None = None
    actionable_takeaways: list[str] = Field(default_factory=list)
    key_concepts: list[str] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    topic_ids: list[str] = Field(default_factory=lambda: [TAXONOMY_FALLBACK_ID])
    content_type: str = CONTENT_TYPE_FALLBACK_ID

    @field_validator("summary", "long_term_value", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = _normalize_plain_text(value)
            return normalized or None
        return value

    @field_validator("actionable_takeaways", "key_concepts", "suggested_tags", mode="before")
    @classmethod
    def normalize_text_list(cls, value: object) -> object:
        return _normalize_string_list(value, limit=8)

    @field_validator("topic_ids", mode="before")
    @classmethod
    def normalize_topic_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            return [TAXONOMY_FALLBACK_ID]
        topic_ids: list[str] = []
        seen: set[str] = set()
        for entry in value:
            topic_id = canonicalize_taxonomy_id(entry)
            if topic_id is None or topic_id in seen:
                continue
            seen.add(topic_id)
            topic_ids.append(topic_id)
        return topic_ids[:5] or [TAXONOMY_FALLBACK_ID]

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return canonicalize_content_type_id(value) or CONTENT_TYPE_FALLBACK_ID


class SavedItemInsightPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    long_term_value: str | None = None
    actionable_takeaways: list[str] = Field(default_factory=list)
    key_concepts: list[str] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    topic_ids: list[str] = Field(default_factory=lambda: [TAXONOMY_FALLBACK_ID])
    topic_labels: list[str] = Field(default_factory=list)
    content_type: str = CONTENT_TYPE_FALLBACK_ID
    content_type_label: str | None = None
    source_kind: str | None = None
    ai_route: str | None = None
    input_snapshot_kind: str | None = None
    input_text_source: str | None = None
    input_char_count: int = Field(default=0, ge=0)
    input_text_was_truncated: bool = False

    @field_validator(
        "summary",
        "long_term_value",
        "source_kind",
        "ai_route",
        "input_snapshot_kind",
        "input_text_source",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = _normalize_plain_text(value)
            return normalized or None
        return value

    @field_validator("actionable_takeaways", "key_concepts", "suggested_tags", mode="before")
    @classmethod
    def normalize_text_list(cls, value: object) -> object:
        return _normalize_string_list(value, limit=12)

    @field_validator("topic_ids", mode="before")
    @classmethod
    def normalize_topic_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            return [TAXONOMY_FALLBACK_ID]
        topic_ids: list[str] = []
        seen: set[str] = set()
        for entry in value:
            topic_id = canonicalize_taxonomy_id(entry)
            if topic_id is None or topic_id in seen:
                continue
            seen.add(topic_id)
            topic_ids.append(topic_id)
        return topic_ids[:5] or [TAXONOMY_FALLBACK_ID]

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return canonicalize_content_type_id(value) or CONTENT_TYPE_FALLBACK_ID

    @model_validator(mode="after")
    def fill_controlled_labels(self) -> "SavedItemInsightPayload":
        self.topic_labels = [get_taxonomy_label(topic_id) for topic_id in self.topic_ids]
        self.content_type_label = get_content_type_label(self.content_type)
        return self


@dataclass(frozen=True, slots=True)
class SavedItemInsightInput:
    content_text: str
    input_snapshot_kind: str | None
    input_text_source: str
    input_char_count: int
    input_text_was_truncated: bool


@dataclass(frozen=True, slots=True)
class SavedItemInsightReadBundle:
    resolved_insight: SavedItemResolvedInsightRead | None
    ai_generated_insight: SavedItemAIInsightRead | None
    curated_insight: SavedItemCuratedInsightRead | None


def rebuild_saved_item_insight(
    session: Session,
    saved_item_id: int,
    *,
    ai_route: str | None = None,
) -> InsightEngineArtifact | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None

    config, source, resolved_ai_route = _resolve_ai_config_for_saved_item(session, saved_item, ai_route=ai_route)
    if config is None:
        raise SavedItemInsightConfigurationError("AI route is disabled for this saved item")
    if not config.is_configured:
        raise SavedItemInsightConfigurationError("AI provider is not configured")

    selected_input = select_saved_item_insight_input(session, saved_item)
    request_payload = _build_saved_item_insight_request_payload(
        config=config,
        ai_route=resolved_ai_route,
        saved_item=saved_item,
        source=source,
        selected_input=selected_input,
        active_insight_profile_context=build_active_insight_profile_context(session),
    )
    try:
        result = request_chat_completion_json(
            config,
            messages=request_payload["messages"],
            max_tokens=request_payload["max_tokens"],
            temperature=request_payload["temperature"],
            json_schema_name=SAVED_ITEM_INSIGHT_RESPONSE_SCHEMA_NAME,
            json_schema=build_saved_item_insight_response_schema(
                taxonomy=request_payload["taxonomy"],
                content_type_options=request_payload["content_type_options"],
            ),
        )
        raw_payload = parse_json_object_content(result.content)
        response_payload = _AISavedItemInsightResponsePayload.model_validate(raw_payload)
    except (AIProviderError, ValidationError, json.JSONDecodeError, ValueError) as exc:
        raise SavedItemInsightGenerationError(f"Saved item insight generation failed: {exc}") from exc

    final_payload = SavedItemInsightPayload(
        **response_payload.model_dump(),
        source_kind=get_saved_item_evidence_source_kind(saved_item),
        ai_route=resolved_ai_route,
        input_snapshot_kind=selected_input.input_snapshot_kind,
        input_text_source=selected_input.input_text_source,
        input_char_count=selected_input.input_char_count,
        input_text_was_truncated=selected_input.input_text_was_truncated,
    )
    artifact = InsightEngineArtifact(
        feed_item_id=None,
        saved_item_id=saved_item.id,
        artifact_kind=SAVED_ITEM_INSIGHT_ARTIFACT_KIND,
        model_name=config.artifact_model_name,
        prompt_version=SAVED_ITEM_INSIGHT_PROMPT_VERSION,
        artifact_payload=final_payload.model_dump(),
    )
    session.add(artifact)
    session.flush()
    session.refresh(artifact)
    return artifact


def upsert_curated_saved_item_insight(
    session: Session,
    saved_item_id: int,
    *,
    payload: dict[str, Any],
    edit_note: str | None,
) -> SavedItemCuratedInsight | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None

    try:
        normalized_payload = SavedItemInsightPayload.model_validate(payload).model_dump()
    except ValidationError as exc:
        raise SavedItemInsightValidationError(str(exc)) from exc

    latest_insight_engine_artifact = load_latest_saved_item_insight_engine_artifacts(session, [saved_item_id]).get(saved_item_id)
    existing = load_curated_saved_item_insights(session, [saved_item_id]).get(saved_item_id)
    previous_payload = existing.curated_payload_json if existing is not None else (
        latest_insight_engine_artifact.artifact_payload if latest_insight_engine_artifact is not None else None
    )
    diff = _build_payload_diff(previous_payload, normalized_payload)
    now = _now_utc()

    if existing is None:
        curated = SavedItemCuratedInsight(
            saved_item_id=saved_item_id,
            base_insight_engine_artifact_id=latest_insight_engine_artifact.id if latest_insight_engine_artifact is not None else None,
            curated_payload_json=normalized_payload,
            diff_json=diff,
            edit_note=edit_note,
            created_at=now,
            updated_at=now,
        )
        event_type = CURATED_INSIGHT_CREATED
        session.add(curated)
        session.flush()
    else:
        curated = existing
        curated.base_insight_engine_artifact_id = latest_insight_engine_artifact.id if latest_insight_engine_artifact is not None else existing.base_insight_engine_artifact_id
        curated.curated_payload_json = normalized_payload
        curated.diff_json = diff
        curated.edit_note = edit_note
        curated.updated_at = now
        event_type = CURATED_INSIGHT_UPDATED
        session.add(curated)
        session.flush()

    session.add(
        SavedItemCuratedInsightEvent(
            saved_item_id=saved_item_id,
            curated_insight_id=curated.id,
            base_insight_engine_artifact_id=curated.base_insight_engine_artifact_id,
            event_type=event_type,
            event_payload_json={
                "previous_payload": previous_payload,
                "current_payload": normalized_payload,
                "diff": diff,
                "edit_note": edit_note,
            },
            created_at=now,
        )
    )
    session.flush()
    session.refresh(curated)
    return curated


def clear_curated_saved_item_insight(session: Session, saved_item_id: int) -> bool | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None

    curated = load_curated_saved_item_insights(session, [saved_item_id]).get(saved_item_id)
    if curated is None:
        return False

    now = _now_utc()
    session.add(
        SavedItemCuratedInsightEvent(
            saved_item_id=saved_item_id,
            curated_insight_id=curated.id,
            base_insight_engine_artifact_id=curated.base_insight_engine_artifact_id,
            event_type=CURATED_INSIGHT_CLEARED,
            event_payload_json={
                "previous_payload": curated.curated_payload_json,
                "edit_note": None,
            },
            created_at=now,
        )
    )
    session.flush()
    session.delete(curated)
    session.flush()
    return True


def list_curated_saved_item_insight_events(
    session: Session,
    saved_item_id: int,
) -> list[SavedItemCuratedInsightEventRead] | None:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        return None
    events = load_curated_saved_item_events(session, [saved_item_id]).get(saved_item_id, [])
    return [_build_event_read(event) for event in events]


def load_saved_item_insight_read_bundles(
    session: Session,
    saved_item_ids: Sequence[int],
) -> dict[int, SavedItemInsightReadBundle]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    latest_insight_engine_artifacts = load_latest_saved_item_insight_engine_artifacts(session, unique_ids)
    curated_insights = load_curated_saved_item_insights(session, unique_ids)
    result: dict[int, SavedItemInsightReadBundle] = {}

    for saved_item_id in unique_ids:
        ai_read = _build_ai_read(latest_insight_engine_artifacts.get(saved_item_id))
        curated_read = _build_curated_read(curated_insights.get(saved_item_id))
        resolved_read = _build_resolved_read(ai_read=ai_read, curated_read=curated_read)
        result[saved_item_id] = SavedItemInsightReadBundle(
            resolved_insight=resolved_read,
            ai_generated_insight=ai_read,
            curated_insight=curated_read,
        )
    return result


def load_latest_saved_item_insight_engine_artifacts(
    session: Session,
    saved_item_ids: Sequence[int],
) -> dict[int, InsightEngineArtifact]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}

    latest_ids = (
        select(
            InsightEngineArtifact.saved_item_id.label("saved_item_id"),
            func.max(InsightEngineArtifact.id).label("latest_id"),
        )
        .where(
            InsightEngineArtifact.saved_item_id.in_(unique_ids),
            InsightEngineArtifact.artifact_kind == SAVED_ITEM_INSIGHT_ARTIFACT_KIND,
        )
        .group_by(InsightEngineArtifact.saved_item_id)
        .subquery()
    )
    artifacts = session.scalars(select(InsightEngineArtifact).join(latest_ids, InsightEngineArtifact.id == latest_ids.c.latest_id)).all()
    return {
        artifact.saved_item_id: artifact
        for artifact in artifacts
        if artifact.saved_item_id is not None
    }


def load_curated_saved_item_insights(
    session: Session,
    saved_item_ids: Sequence[int],
) -> dict[int, SavedItemCuratedInsight]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}
    insights = session.scalars(
        select(SavedItemCuratedInsight).where(SavedItemCuratedInsight.saved_item_id.in_(unique_ids))
    ).all()
    return {insight.saved_item_id: insight for insight in insights}


def load_curated_saved_item_events(
    session: Session,
    saved_item_ids: Sequence[int],
) -> dict[int, list[SavedItemCuratedInsightEvent]]:
    unique_ids = list(dict.fromkeys(saved_item_ids))
    if not unique_ids:
        return {}
    events = session.scalars(
        select(SavedItemCuratedInsightEvent)
        .where(SavedItemCuratedInsightEvent.saved_item_id.in_(unique_ids))
        .order_by(SavedItemCuratedInsightEvent.created_at.asc(), SavedItemCuratedInsightEvent.id.asc())
    ).all()
    result: dict[int, list[SavedItemCuratedInsightEvent]] = {}
    for event in events:
        result.setdefault(event.saved_item_id, []).append(event)
    return result


def select_saved_item_insight_input(session: Session, saved_item: SavedItem) -> SavedItemInsightInput:
    for snapshot_kind in _INPUT_SNAPSHOT_PRIORITY:
        snapshot = _find_latest_snapshot(session, saved_item.id, snapshot_kind)
        if snapshot is None:
            continue
        if snapshot_kind == UPLOADED_MARKDOWN_SNAPSHOT_KIND and not _uploaded_markdown_matches_current_source_asset(
            session,
            saved_item,
            snapshot,
        ):
            continue
        text = _normalize_markdown_input(snapshot.body)
        if text:
            return _build_selected_input(text, snapshot_kind=snapshot_kind, input_text_source=snapshot_kind)

    canonical_snapshot = _find_latest_snapshot(session, saved_item.id, CANONICAL_SNAPSHOT_KIND)
    if canonical_snapshot is not None:
        text = _html_to_text(canonical_snapshot.body)
        if text:
            return _build_selected_input(
                text,
                snapshot_kind=CANONICAL_SNAPSHOT_KIND,
                input_text_source=CANONICAL_SNAPSHOT_KIND,
            )

    singlefile_asset = _find_latest_asset(session, saved_item.id, SINGLEFILE_HTML_SNAPSHOT_KIND)
    if singlefile_asset is not None:
        try:
            singlefile_html = singlefile_asset.content_bytes.decode("utf-8", errors="replace")
        except Exception:
            singlefile_html = ""
        text = _html_to_text(singlefile_html)
        if text:
            return _build_selected_input(
                text,
                snapshot_kind=SINGLEFILE_HTML_SNAPSHOT_KIND,
                input_text_source="singlefile_html_text",
            )

    fallback_lines = [
        f"Title: {saved_item.title}",
        f"Source: {saved_item.source_name}",
    ]
    if saved_item.canonical_url:
        fallback_lines.insert(1, f"URL: {saved_item.canonical_url}")
    if saved_item.original_filename:
        fallback_lines.insert(1, f"Original filename: {saved_item.original_filename}")
    if saved_item.user_note:
        fallback_lines.insert(0, f"User note: {saved_item.user_note}")
    return _build_selected_input(
        "\n".join(fallback_lines),
        snapshot_kind=None,
        input_text_source="metadata_fallback",
    )


def _uploaded_markdown_matches_current_source_asset(
    session: Session,
    saved_item: SavedItem,
    snapshot: SavedSnapshot,
) -> bool:
    if saved_item.source_asset_id is None:
        return False
    source_asset = session.get(SavedItemAsset, saved_item.source_asset_id)
    if source_asset is None:
        return False
    metadata = source_asset.metadata_json or {}
    return metadata.get("derived_markdown_snapshot_id") == snapshot.id


def normalize_saved_item_ai_route(value: str | None, *, default: str = DEFAULT_SAVED_ITEM_AI_ROUTE) -> str:
    route = (value or default).strip().lower()
    if route not in AI_SOURCE_ROUTES:
        raise SavedItemInsightValidationError("Unsupported saved item AI route")
    return route


def _resolve_ai_config_for_saved_item(
    session: Session,
    saved_item: SavedItem,
    *,
    ai_route: str | None = None,
) -> tuple[AIProviderConfig | None, Source | None, str]:
    source = _find_saved_item_source(session, saved_item)
    if ai_route is not None:
        route = normalize_saved_item_ai_route(ai_route, default=DEFAULT_SAVED_ITEM_AI_ROUTE)
    elif saved_item.ai_route:
        route = normalize_saved_item_ai_route(saved_item.ai_route, default=DEFAULT_SAVED_ITEM_AI_ROUTE)
    elif saved_item.feed_item_id is not None and source is not None:
        route = normalize_saved_item_ai_route(source.ai_route, default=AI_PROFILE_GENERAL)
    elif saved_item.feed_item_id is not None:
        route = DEFAULT_SAVED_ITEM_AI_ROUTE
    else:
        route = DEFAULT_SAVED_ITEM_AI_ROUTE
    return resolve_ai_provider_config_for_source_route(session, route), source, route


def _find_saved_item_source(session: Session, saved_item: SavedItem) -> Source | None:
    if saved_item.feed_item_id is None:
        return None
    latest_occurrences = build_latest_occurrence_subquery(feed_item_ids=[saved_item.feed_item_id])
    statement = (
        select(Source)
        .join(SourceItemOccurrence, SourceItemOccurrence.source_id == Source.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.occurrence_id == SourceItemOccurrence.id)
            & (latest_occurrences.c.occurrence_rank == 1),
        )
        .limit(1)
    )
    return session.scalar(statement)


def _build_saved_item_insight_request_payload(
    *,
    config: AIProviderConfig,
    ai_route: str,
    saved_item: SavedItem,
    source: Source | None,
    selected_input: SavedItemInsightInput,
    active_insight_profile_context: dict[str, Any],
) -> dict[str, Any]:
    taxonomy = build_taxonomy_context()
    content_types = build_content_type_options_context()
    system_message = (
        "Return only valid JSON. Analyze the saved knowledge item for long-term personal reuse. "
        "Do not invent facts that are not supported by the provided saved item content. "
        "Use taxonomy ids from taxonomy.items and one content_type id from content_type_options.options. "
        "The JSON object must contain: summary, long_term_value, actionable_takeaways, "
        "key_concepts, suggested_tags, topic_ids, content_type."
    )
    user_message = "\n\n".join(
        [
            f"Saved item title: {saved_item.title}",
            f"Saved item URL: {saved_item.canonical_url or ''}",
            f"Saved item source category: {get_saved_item_evidence_source_kind(saved_item)}",
            f"Saved item original filename: {saved_item.original_filename or ''}",
            f"Saved item AI route: {ai_route}",
            f"Source name: {saved_item.source_name}",
            f"Source kind: {source.kind if source is not None else get_saved_item_evidence_source_kind(saved_item)}",
            f"User note: {saved_item.user_note or ''}",
            f"Input source: {selected_input.input_text_source}",
            f"Input was truncated: {selected_input.input_text_was_truncated}",
            f"Taxonomy: {json.dumps(taxonomy, ensure_ascii=False)}",
            f"Content type options: {json.dumps(content_types, ensure_ascii=False)}",
            f"Active insight profile context: {json.dumps(active_insight_profile_context, ensure_ascii=False)}",
            "Saved item content:",
            selected_input.content_text,
        ]
    )
    return {
        "prompt_version": SAVED_ITEM_INSIGHT_PROMPT_VERSION,
        "ai_route": ai_route,
        "profile": config.profile,
        "provider": config.provider,
        "model": config.model,
        "temperature": 0.2,
        "max_tokens": 4096,
        "taxonomy": taxonomy,
        "content_type_options": content_types,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
    }


def build_saved_item_insight_response_schema(
    *,
    taxonomy: dict[str, Any],
    content_type_options: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": ["string", "null"]},
            "long_term_value": {"type": ["string", "null"]},
            "actionable_takeaways": {"type": "array", "items": {"type": "string"}},
            "key_concepts": {"type": "array", "items": {"type": "string"}},
            "suggested_tags": {"type": "array", "items": {"type": "string"}},
            "topic_ids": {
                "type": "array",
                "items": {"type": "string", "enum": _extract_ids(taxonomy.get("items"), "id")},
            },
            "content_type": {
                "type": "string",
                "enum": _extract_ids(content_type_options.get("options"), "id"),
            },
        },
        "required": [
            "summary",
            "long_term_value",
            "actionable_takeaways",
            "key_concepts",
            "suggested_tags",
            "topic_ids",
            "content_type",
        ],
    }


def _build_ai_read(artifact: InsightEngineArtifact | None) -> SavedItemAIInsightRead | None:
    if artifact is None or not isinstance(artifact.artifact_payload, dict):
        return None
    try:
        payload = SavedItemInsightPayload.model_validate(artifact.artifact_payload)
    except ValidationError:
        return None
    return SavedItemAIInsightRead(
        artifact_id=artifact.id,
        artifact_kind=artifact.artifact_kind,
        model_name=artifact.model_name,
        prompt_version=artifact.prompt_version,
        payload=SavedItemInsightPayloadRead.model_validate(payload.model_dump()),
        created_at=artifact.created_at,
    )


def get_saved_item_evidence_source_kind(saved_item: SavedItem) -> str:
    if saved_item.feed_item_id is not None or saved_item.origin_type == ORIGIN_TYPE_FEED_SAVED:
        return ORIGIN_TYPE_FEED_SAVED
    if saved_item.origin_type in EXTERNAL_SOURCE_ORIGIN_TYPES:
        return EVIDENCE_SOURCE_KIND_EXTERNAL
    return saved_item.origin_type or EVIDENCE_SOURCE_KIND_EXTERNAL


def _build_curated_read(curated: SavedItemCuratedInsight | None) -> SavedItemCuratedInsightRead | None:
    if curated is None:
        return None
    try:
        payload = SavedItemInsightPayload.model_validate(curated.curated_payload_json)
    except ValidationError:
        return None
    return SavedItemCuratedInsightRead(
        id=curated.id,
        saved_item_id=curated.saved_item_id,
        base_insight_engine_artifact_id=curated.base_insight_engine_artifact_id,
        payload=SavedItemInsightPayloadRead.model_validate(payload.model_dump()),
        diff_json=curated.diff_json,
        edit_note=curated.edit_note,
        created_at=curated.created_at,
        updated_at=curated.updated_at,
    )


def _build_resolved_read(
    *,
    ai_read: SavedItemAIInsightRead | None,
    curated_read: SavedItemCuratedInsightRead | None,
) -> SavedItemResolvedInsightRead | None:
    if curated_read is not None:
        return SavedItemResolvedInsightRead(
            source="curated",
            payload=curated_read.payload,
            insight_engine_artifact_id=curated_read.base_insight_engine_artifact_id,
            curated_insight_id=curated_read.id,
            curated_updated_at=curated_read.updated_at,
        )
    if ai_read is not None:
        return SavedItemResolvedInsightRead(
            source="ai_generated",
            payload=ai_read.payload,
            insight_engine_artifact_id=ai_read.artifact_id,
            model_name=ai_read.model_name,
            prompt_version=ai_read.prompt_version,
            generated_at=ai_read.created_at,
        )
    return None


def _build_event_read(event: SavedItemCuratedInsightEvent) -> SavedItemCuratedInsightEventRead:
    return SavedItemCuratedInsightEventRead(
        id=event.id,
        saved_item_id=event.saved_item_id,
        curated_insight_id=event.curated_insight_id,
        base_insight_engine_artifact_id=event.base_insight_engine_artifact_id,
        event_type=event.event_type,
        event_payload_json=event.event_payload_json or {},
        created_at=event.created_at,
    )


def _build_payload_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    changed: dict[str, Any] = {}
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            changed[key] = {"before": before, "after": after}
    return {"changed_fields": changed}


def _find_latest_snapshot(session: Session, saved_item_id: int, snapshot_kind: str) -> SavedSnapshot | None:
    return session.scalar(
        select(SavedSnapshot)
        .where(
            SavedSnapshot.saved_item_id == saved_item_id,
            SavedSnapshot.snapshot_kind == snapshot_kind,
        )
        .order_by(SavedSnapshot.id.desc())
        .limit(1)
    )


def _find_latest_asset(session: Session, saved_item_id: int, asset_kind: str) -> SavedItemAsset | None:
    return session.scalar(
        select(SavedItemAsset)
        .where(
            SavedItemAsset.saved_item_id == saved_item_id,
            SavedItemAsset.asset_kind == asset_kind,
        )
        .order_by(SavedItemAsset.id.desc())
        .limit(1)
    )


def _build_selected_input(
    text: str,
    *,
    snapshot_kind: str | None,
    input_text_source: str,
) -> SavedItemInsightInput:
    normalized = _normalize_input_text(text)
    original_length = len(normalized)
    truncated = normalized[:MAX_SAVED_ITEM_INSIGHT_INPUT_CHARS].rstrip()
    return SavedItemInsightInput(
        content_text=truncated,
        input_snapshot_kind=snapshot_kind,
        input_text_source=input_text_source,
        input_char_count=original_length,
        input_text_was_truncated=original_length > len(truncated),
    )


def _normalize_markdown_input(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _normalize_input_text(value)
    return normalized or None


def _normalize_input_text(value: str) -> str:
    normalized_lines = [_WHITESPACE_PATTERN.sub(" ", line).strip() for line in value.splitlines()]
    normalized = "\n".join(normalized_lines)
    normalized = _BLANK_LINE_PATTERN.sub("\n\n", normalized).strip()
    return normalized


def _normalize_plain_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _normalize_string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            continue
        normalized = _normalize_plain_text(entry)
        if not normalized:
            continue
        dedupe_key = normalized.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        values.append(normalized)
    return values[:limit]


def _html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _VisibleTextHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return None
    text = _normalize_input_text(parser.text)
    return text or None


class _VisibleTextHTMLParser(HTMLParser):
    _BLOCK_TAGS = {
        "article",
        "blockquote",
        "br",
        "div",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "p",
        "section",
        "tr",
    }
    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _extract_ids(values: object, field_name: str) -> list[str]:
    if not isinstance(values, list):
        return []
    ids: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = value.get(field_name)
        if isinstance(candidate, str) and candidate:
            ids.append(candidate)
    return ids
