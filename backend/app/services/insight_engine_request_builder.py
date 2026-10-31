from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, FeedItemStructuredFeedback, InsightChannel, Source, SourceChannelTendency
from app.services.insight_engine_artifacts import normalize_summary_source_text
from app.services.ai_provider import build_chat_completion_body, build_structured_response_format
from app.services.app_settings import AIProviderConfig, build_ai_chat_completions_url
from app.services.insight_channels import NEEDS_REVIEW_CHANNEL_ID, channel_is_active, get_channel_catalog_version
from app.services.insight_engine_contracts import (
    build_content_type_options_context,
    build_reason_codes_context,
    build_taxonomy_context,
)
from app.services.insight_routing_reason_codes import (
    ISOLATION_REASON_CODES,
    PROMOTION_REASON_CODES,
    ROUTING_REASON_CODES,
    build_routing_reason_codes_context,
)
from app.services.insight_profiles import (
    DEFAULT_INSIGHT_PROFILE,
    build_insight_profile_prompt_context,
    get_active_insight_profile,
)
from app.services.insight_scoring import FEATURE_SCHEMA_VERSION, SCORE_VERSION
from app.services.source_preference_profiles import get_active_source_preference_profile_context
from app.services.structured_feedback import serialize_channel_corrections

INSIGHT_ENGINE_PROMPT_VERSION = "insight-engine-v2-routing-v1"
INSIGHT_ENGINE_TEMPERATURE = 0.2
INSIGHT_ENGINE_MAX_TOKENS = 4096
MAX_INSIGHT_ENGINE_INPUT_CHARS = 20000
INSIGHT_ENGINE_INPUT_HEAD_CHARS = 10000
INSIGHT_ENGINE_INPUT_TAIL_CHARS = 10000
INSIGHT_ENGINE_RESPONSE_SCHEMA_NAME = "feed_item_insight"

ContentTextPayloadSource = Literal["content_text", "summary_text_fallback", "empty"]


class InsightEngineExcludedField(BaseModel):
    field: str
    reason: str


class InsightEngineRequestBudgetError(ValueError):
    def __init__(self, *, source: str, source_char_count: int, max_chars: int) -> None:
        super().__init__(
            f"content_too_large_for_provider_budget: {source} has {source_char_count} chars; max is {max_chars}"
        )
        self.error_type = "content_too_large_for_provider_budget"
        self.source = source
        self.source_char_count = source_char_count
        self.max_chars = max_chars


class InsightEngineRequestStats(BaseModel):
    source_content_char_count: int
    content_text_char_count: int
    content_text_was_truncated: bool
    content_text_estimated_token_count: int
    system_message_char_count: int
    system_message_estimated_token_count: int
    user_message_char_count: int
    user_message_estimated_token_count: int


class InsightEngineChannelContext(BaseModel):
    id: str
    label: str
    description: str
    focus_prompt: str = ""
    boundary_prompt: str = ""
    reading_depth: Literal["light", "medium", "deep"]
    default_flow_policy: Literal["normal", "mixed_high_frequency", "high_frequency_brief"] = "normal"
    enabled: bool


class InsightEngineSourceChannelTendencyContext(BaseModel):
    channel_id: str
    tendency: float
    is_primary: bool
    notes: str


@dataclass(frozen=True, slots=True)
class InsightEngineRequestContext:
    config: AIProviderConfig
    item_title: str
    source_name: str
    source_folder: str
    source_flow_policy: str
    content_extraction_status: str | None
    content_text: str
    content_text_source: ContentTextPayloadSource
    source_content_char_count: int
    content_text_was_truncated: bool
    taxonomy: dict[str, Any]
    content_type_options: dict[str, Any]
    reason_codes: dict[str, Any]
    routing_reason_codes: dict[str, Any]
    active_insight_profile_context: dict[str, Any]
    profile_version: str
    source_preference_profile: dict[str, Any]
    source_profile_version: str | None
    source_profile_is_empty: bool
    feature_schema_version: str
    score_version: str
    channels: list[InsightEngineChannelContext]
    channel_catalog_version: int
    source_channel_tendencies: list[InsightEngineSourceChannelTendencyContext]
    human_evidence: dict[str, Any]


class InsightEngineRequestPayload(BaseModel):
    prompt_version: str = INSIGHT_ENGINE_PROMPT_VERSION
    profile: str
    provider: str
    model: str
    endpoint: str
    temperature: float
    max_tokens: int
    messages: list[dict[str, str]]
    system_message: str
    user_message: str
    request_body: dict[str, Any]
    fallback_request_body: dict[str, Any]
    taxonomy: dict[str, Any] = Field(default_factory=dict)
    content_type_options: dict[str, Any] = Field(default_factory=dict)
    reason_codes: dict[str, Any] = Field(default_factory=dict)
    active_insight_profile_context: dict[str, Any] = Field(default_factory=dict)
    profile_version: str
    source_preference_profile: dict[str, Any] = Field(default_factory=dict)
    source_profile_version: str | None = None
    source_profile_is_empty: bool = True
    feature_schema_version: str
    score_version: str
    channels: list[InsightEngineChannelContext] = Field(default_factory=list)
    channel_catalog_version: int
    source_channel_tendencies: list[InsightEngineSourceChannelTendencyContext] = Field(default_factory=list)
    source_flow_policy: str
    human_evidence: dict[str, Any] = Field(default_factory=dict)
    content_extraction_status: str | None = None
    source_folder: str = ""
    content_text: str
    content_text_source: ContentTextPayloadSource
    stats: InsightEngineRequestStats
    excluded_fields: list[InsightEngineExcludedField]


def load_insight_engine_request_context(
    *,
    session: Session,
    config: AIProviderConfig,
    feed_item: FeedItem,
    source: Source,
    active_insight_profile_context: dict[str, Any] | None = None,
) -> InsightEngineRequestContext:
    taxonomy = _build_taxonomy_context()
    content_type_options = _build_content_type_options_context()
    reason_codes = _build_reason_codes_context()
    routing_reason_codes = build_routing_reason_codes_context()
    active_profile = get_active_insight_profile(session)
    active_profile_json = active_profile.profile_json if active_profile is not None else DEFAULT_INSIGHT_PROFILE
    active_profile_context = active_insight_profile_context or _build_active_insight_profile_context(active_profile_json)
    profile_version = active_profile.profile_version if active_profile is not None else DEFAULT_INSIGHT_PROFILE["profile_version"]
    channels = _build_channel_context(session)
    channel_catalog_version = get_channel_catalog_version(session)
    source_channel_tendencies = _build_source_channel_tendency_context(session, source.id)
    source_flow_policy = source.flow_policy or "normal"
    source_profile_context = get_active_source_preference_profile_context(session, source.id)
    human_evidence = _build_human_evidence_context(session, feed_item.id)
    content_text, content_text_source, source_content_char_count, content_was_truncated = _build_content_text(feed_item)

    return InsightEngineRequestContext(
        config=config,
        item_title=feed_item.title,
        source_name=source.name,
        source_folder=source.folder_path or "",
        source_flow_policy=source_flow_policy,
        content_extraction_status=feed_item.content_extraction_status,
        content_text=content_text,
        content_text_source=content_text_source,
        source_content_char_count=source_content_char_count,
        content_text_was_truncated=content_was_truncated,
        taxonomy=taxonomy,
        content_type_options=content_type_options,
        reason_codes=reason_codes,
        routing_reason_codes=routing_reason_codes,
        active_insight_profile_context=active_profile_context,
        profile_version=profile_version,
        source_preference_profile=source_profile_context.profile,
        source_profile_version=source_profile_context.profile_version,
        source_profile_is_empty=source_profile_context.is_empty,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        score_version=SCORE_VERSION,
        channels=channels,
        channel_catalog_version=channel_catalog_version,
        source_channel_tendencies=source_channel_tendencies,
        human_evidence=human_evidence,
    )


def build_insight_engine_request_payload(
    context: InsightEngineRequestContext,
    *,
    max_tokens: int = INSIGHT_ENGINE_MAX_TOKENS,
    temperature: float = INSIGHT_ENGINE_TEMPERATURE,
) -> InsightEngineRequestPayload:
    system_message = _build_system_message()
    user_message = _build_user_message(
        item_title=context.item_title,
        source_name=context.source_name,
        source_folder=context.source_folder,
        taxonomy=context.taxonomy,
        content_type_options=context.content_type_options,
        reason_codes=context.reason_codes,
        routing_reason_codes=context.routing_reason_codes,
        active_profile_context=context.active_insight_profile_context,
        source_preference_profile=context.source_preference_profile,
        channels=context.channels,
        source_channel_tendencies=context.source_channel_tendencies,
        source_flow_policy=context.source_flow_policy,
        human_evidence=context.human_evidence,
        content_text=context.content_text,
    )
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]
    fallback_request_body = build_chat_completion_body(
        context.config,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    response_schema = build_insight_engine_response_schema(
        taxonomy=context.taxonomy,
        content_type_options=context.content_type_options,
        reason_codes=context.reason_codes,
        channel_ids=[channel.id for channel in context.channels],
    )
    request_body = {
        **fallback_request_body,
        "response_format": build_structured_response_format(
            context.config,
            json_schema_name=INSIGHT_ENGINE_RESPONSE_SCHEMA_NAME,
            json_schema=response_schema,
        ),
    }

    return InsightEngineRequestPayload(
        profile=context.config.profile,
        provider=context.config.provider,
        model=context.config.model,
        endpoint=build_ai_chat_completions_url(context.config),
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
        system_message=system_message,
        user_message=user_message,
        request_body=request_body,
        fallback_request_body=fallback_request_body,
        taxonomy=context.taxonomy,
        content_type_options=context.content_type_options,
        reason_codes=context.reason_codes,
        active_insight_profile_context=context.active_insight_profile_context,
        profile_version=context.profile_version,
        source_preference_profile=context.source_preference_profile,
        source_profile_version=context.source_profile_version,
        source_profile_is_empty=context.source_profile_is_empty,
        feature_schema_version=context.feature_schema_version,
        score_version=context.score_version,
        channels=context.channels,
        channel_catalog_version=context.channel_catalog_version,
        source_channel_tendencies=context.source_channel_tendencies,
        source_flow_policy=context.source_flow_policy,
        human_evidence=context.human_evidence,
        content_extraction_status=context.content_extraction_status,
        source_folder=context.source_folder,
        content_text=context.content_text,
        content_text_source=context.content_text_source,
        stats=InsightEngineRequestStats(
            source_content_char_count=context.source_content_char_count,
            content_text_char_count=len(context.content_text),
            content_text_was_truncated=context.content_text_was_truncated,
            content_text_estimated_token_count=_estimate_token_count(context.content_text),
            system_message_char_count=len(system_message),
            system_message_estimated_token_count=_estimate_token_count(system_message),
            user_message_char_count=len(user_message),
            user_message_estimated_token_count=_estimate_token_count(user_message),
        ),
        excluded_fields=_build_excluded_fields(),
    )


def _build_system_message() -> str:
    return (
        "You are the local Insight Engine for a personal information system. "
        "Read the article title, source, folder, normalized content, taxonomy, content types, "
        "reason-code contracts, channel context, source routing hints, human evidence, and active profile context. "
        "Return one strict JSON object that matches the requested schema. "
        "Use only allowed taxonomy ids, content type ids, reason codes, channel ids, reading depths, and flow lanes. "
        "Do not invent unavailable facts, URLs, markdown, or extra fields. "
        "Keep summary and reason labels concise. "
        "If the content is insufficient, still return the best valid object with conservative confidence. "
    )


def _build_user_message(
    *,
    item_title: str,
    source_name: str,
    source_folder: str,
    taxonomy: dict[str, Any],
    content_type_options: dict[str, Any],
    reason_codes: dict[str, Any],
    routing_reason_codes: dict[str, Any],
    active_profile_context: dict[str, Any],
    source_preference_profile: dict[str, Any],
    channels: list[InsightEngineChannelContext],
    source_channel_tendencies: list[InsightEngineSourceChannelTendencyContext],
    source_flow_policy: str,
    human_evidence: dict[str, Any],
    content_text: str,
) -> str:
    return "\n".join(
        [
            "Return a JSON object with exactly these fields:",
            "- summary: string or null, concise and useful for the reader.",
            "- topics: array of 1-3 taxonomy ids from taxonomy.items[].id.",
            "- content_type: exactly one id from content_type_options.options[].id.",
            "- priority_reasons: semantic signal array of objects with code, label, and weight integer from 0 to 100.",
            "- routing: object with schema_version, primary_channel_id, secondary_channels[].display_mode, reading_depth, flow_lane, confidence, reason_codes, promotion_reason_codes, isolation_reason_codes, source_policy_applied, notes.",
            "",
            "Example JSON output:",
            "{",
            '  "summary": "The article explains a reusable implementation detail with practical tradeoffs.",',
            '  "topics": ["software_systems"],',
            '  "content_type": "analysis",',
            '  "priority_reasons": [{"code": "implementation_detail", "label": "包含可复用的实现细节", "weight": 72}],',
            '  "routing": {',
            '    "schema_version": "feed_item_routing_v1",',
            '    "primary_channel_id": "ai_agents_frontier",',
            '    "secondary_channels": [{"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "list"}],',
            '    "reading_depth": "medium",',
            '    "flow_lane": "main",',
            '    "confidence": 0.76,',
            '    "reason_codes": ["source_channel_tendency_match", "cross_channel_tooling"],',
            '    "promotion_reason_codes": [],',
            '    "isolation_reason_codes": [],',
            '    "source_policy_applied": "normal",',
            '    "notes": "Technical implementation update with reusable engineering value."',
            "  }",
            "}",
            "",
            "Output language:",
            "- summary and priority_reasons[].label must be written in Simplified Chinese.",
            "- topics, content_type, and priority_reasons[].code must stay as controlled English ids/codes.",
            "- Do not translate source names or article titles unless they are part of a summary sentence.",
            "- Do not invent free-form topics or reason codes.",
            "",
            "Controlled contract rules:",
            "- Taxonomy is the article domain only; do not use taxonomy to express quality, priority, or low signal.",
            "- Content type is the article format only.",
            "- Low-quality, marketing, repetitive, or low-confidence judgments must be represented with reason codes.",
            "- Use only reason_codes.ai_allowed_codes in priority_reasons[].code.",
            "- Use only routing_reason_codes.routing_reason_codes in routing.reason_codes.",
            "- Use only routing_reason_codes.promotion_reason_codes in routing.promotion_reason_codes.",
            "- Use only routing_reason_codes.isolation_reason_codes in routing.isolation_reason_codes.",
            "",
            "Routing rules:",
            f'- Choose one primary_channel_id only when the article naturally belongs to a channel; otherwise use "{NEEDS_REVIEW_CHANNEL_ID}".',
            "- Multiple secondary_channels are allowed when overlap is real.",
            "- TCP/artificial muscle core and soft actuation overlap but are distinct.",
            "- AI & Agents and open-source tools overlap but are distinct.",
            "- Securities and cryptocurrency are separate unless the article explicitly bridges both.",
            '- Use flow_lane "brief_pool" for short, high-frequency, low-context updates.',
            '- Use flow_lane "main" for ordinary or high-value long-form items.',
            '- Use flow_lane "needs_review" for ambiguous or conflicting source-level tendencies.',
            "",
            "Insight Engine context:",
            "Taxonomy context:",
            _json_context(taxonomy),
            "Content type options context:",
            _json_context(content_type_options),
            "Reason codes context:",
            _json_context(reason_codes),
            "Routing reason codes context:",
            _json_context(routing_reason_codes),
            "Active insight profile context:",
            _json_context(active_profile_context),
            "Source preference profile context:",
            _json_context(source_preference_profile),
            "Enabled channel context:",
            _json_context({"channels": [channel.model_dump(mode="json") for channel in channels]}),
            "Source channel tendencies context:",
            _json_context(
                {
                    "source_flow_policy": source_flow_policy,
                    "channel_tendencies": [
                        tendency.model_dump(mode="json") for tendency in source_channel_tendencies
                    ],
                }
            ),
            "Human evidence channel corrections:",
            _json_context(human_evidence),
            "Treat human_evidence as user-authored correction signals. Do not overwrite them unless the article clearly contradicts the correction.",
            "",
            "Article context:",
            f"Title: {item_title}",
            f"Source name: {source_name}",
            f"Source folder: {source_folder}",
            f"Content: {content_text}",
        ]
    )


def _build_human_evidence_context(session: Session, feed_item_id: int | None) -> dict[str, Any]:
    if feed_item_id is None:
        return {"channel_corrections": None}
    row = session.scalar(
        select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == feed_item_id)
    )
    return {"channel_corrections": serialize_channel_corrections(row)}


def _build_taxonomy_context() -> dict[str, Any]:
    return build_taxonomy_context()


def _build_channel_context(session: Session) -> list[InsightEngineChannelContext]:
    channels = list(
        session.scalars(
            select(InsightChannel).order_by(
                InsightChannel.sort_order.asc(),
                InsightChannel.id.asc(),
            )
        )
    )
    if not channels:
        raise ValueError("Insight channel catalog is empty; seed or configure active DB channels before building insight requests.")
    active_channels = [_channel_context_from_row(channel) for channel in channels if channel_is_active(channel)]
    if not active_channels:
        raise ValueError("Insight channel catalog has no active channels.")
    if NEEDS_REVIEW_CHANNEL_ID not in {channel.id for channel in active_channels}:
        raise ValueError("Insight channel catalog is missing active system channel needs_review.")
    return active_channels


def _build_source_channel_tendency_context(
    session: Session,
    source_id: int | None,
) -> list[InsightEngineSourceChannelTendencyContext]:
    if source_id is None:
        return []

    enabled_channel_ids = set(
        session.scalars(
            select(InsightChannel.id)
            .where(InsightChannel.enabled.is_(True))
            .where(InsightChannel.retired_at.is_(None))
        )
    )
    if not enabled_channel_ids:
        return []

    tendencies = list(
        session.scalars(
            select(SourceChannelTendency)
            .where(SourceChannelTendency.source_id == source_id)
            .where(SourceChannelTendency.channel_id.in_(enabled_channel_ids))
            .order_by(
                SourceChannelTendency.is_primary.desc(),
                SourceChannelTendency.tendency.desc(),
                SourceChannelTendency.channel_id.asc(),
            )
        )
    )
    return [
        InsightEngineSourceChannelTendencyContext(
            channel_id=tendency.channel_id,
            tendency=tendency.tendency,
            is_primary=tendency.is_primary,
            notes=tendency.notes,
        )
        for tendency in tendencies
    ]


def _channel_context_from_row(channel: InsightChannel) -> InsightEngineChannelContext:
    return InsightEngineChannelContext(
        id=channel.id,
        label=channel.label,
        description=channel.description,
        focus_prompt=channel.focus_prompt,
        boundary_prompt=channel.boundary_prompt,
        reading_depth=_normalize_reading_depth(channel.reading_depth),
        default_flow_policy=_normalize_flow_policy(channel.default_flow_policy),
        enabled=channel.enabled,
    )


def _normalize_reading_depth(value: object) -> Literal["light", "medium", "deep"]:
    if value == "light":
        return "light"
    if value == "deep":
        return "deep"
    return "medium"


def _normalize_flow_policy(value: object) -> Literal["normal", "mixed_high_frequency", "high_frequency_brief"]:
    if value == "mixed_high_frequency":
        return "mixed_high_frequency"
    if value == "high_frequency_brief":
        return "high_frequency_brief"
    return "normal"


def build_insight_engine_response_schema(
    *,
    taxonomy: dict[str, Any],
    content_type_options: dict[str, Any],
    reason_codes: dict[str, Any],
    channel_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    allowed_channel_ids = _channel_ids_for_schema(channel_ids)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": ["string", "null"]},
            "topics": {
                "type": "array",
                "items": {"type": "string", "enum": _extract_ids(taxonomy.get("items"), "id")},
            },
            "content_type": {
                "type": "string",
                "enum": _extract_ids(content_type_options.get("options"), "id"),
            },
            "priority_reasons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "code": {"type": "string", "enum": _extract_string_values(reason_codes.get("ai_allowed_codes"))},
                        "label": {"type": "string"},
                        "weight": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                    "required": ["code", "label", "weight"],
                },
            },
            "routing": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "schema_version",
                    "primary_channel_id",
                    "secondary_channels",
                    "reading_depth",
                    "flow_lane",
                    "confidence",
                    "reason_codes",
                    "promotion_reason_codes",
                    "isolation_reason_codes",
                    "source_policy_applied",
                    "notes",
                ],
                "properties": {
                    "schema_version": {"type": "string", "enum": ["feed_item_routing_v1"]},
                    "primary_channel_id": {
                        "type": "string",
                        "enum": allowed_channel_ids,
                    },
                    "secondary_channels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["channel_id", "confidence", "display_mode"],
                            "properties": {
                                "channel_id": {"type": "string", "enum": allowed_channel_ids},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "display_mode": {"type": "string", "enum": ["list", "tag_only"]},
                            },
                        },
                    },
                    "reading_depth": {"type": ["string", "null"], "enum": ["light", "medium", "deep", None]},
                    "flow_lane": {
                        "type": "string",
                        "enum": ["main", "brief_pool", "low_priority", "needs_review"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason_codes": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(ROUTING_REASON_CODES)},
                    },
                    "promotion_reason_codes": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(PROMOTION_REASON_CODES)},
                    },
                    "isolation_reason_codes": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(ISOLATION_REASON_CODES)},
                    },
                    "source_policy_applied": {
                        "type": "string",
                        "enum": ["normal", "mixed_high_frequency", "high_frequency_brief"],
                    },
                    "notes": {"type": "string"},
                },
            },
        },
        "required": [
            "summary",
            "topics",
            "content_type",
            "priority_reasons",
            "routing",
        ],
    }


def _build_content_type_options_context() -> dict[str, Any]:
    return build_content_type_options_context()


def _build_reason_codes_context() -> dict[str, Any]:
    return build_reason_codes_context()


def _build_active_insight_profile_context(profile_json: dict[str, Any]) -> dict[str, Any]:
    return build_insight_profile_prompt_context(profile_json)


def _build_excluded_fields() -> list[InsightEngineExcludedField]:
    return [
        InsightEngineExcludedField(
            field="URL",
            reason="Excluded from runtime AI prompt to keep external context minimal.",
        ),
        InsightEngineExcludedField(
            field="Published at",
            reason="Excluded from runtime AI prompt; local sorting and audit can still use it.",
        ),
        InsightEngineExcludedField(
            field="Reader status",
            reason="Excluded from runtime AI prompt; local state should not steer generation directly.",
        ),
    ]


def _build_content_text(feed_item: FeedItem) -> tuple[str, ContentTextPayloadSource, int, bool]:
    normalized_content = normalize_summary_source_text(feed_item.content_text or "")
    if normalized_content:
        return _truncate_normalized_content(normalized_content, "content_text")

    normalized_summary = normalize_summary_source_text(feed_item.summary_text or "")
    if normalized_summary:
        return _truncate_normalized_content(normalized_summary, "summary_text_fallback")

    return "", "empty", 0, False


def _truncate_normalized_content(
    normalized: str,
    source: ContentTextPayloadSource,
) -> tuple[str, ContentTextPayloadSource, int, bool]:
    source_char_count = len(normalized)
    if source_char_count <= MAX_INSIGHT_ENGINE_INPUT_CHARS:
        return normalized, source, source_char_count, False
    return (
        normalized[:INSIGHT_ENGINE_INPUT_HEAD_CHARS] + normalized[-INSIGHT_ENGINE_INPUT_TAIL_CHARS:],
        source,
        source_char_count,
        True,
    )


def _estimate_token_count(value: str) -> int:
    if not value:
        return 0
    return ceil(len(value) / 4)


def _json_context(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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


def _extract_string_values(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def _default_channel_ids() -> list[str]:
    return []


def _channel_ids_for_schema(channel_ids: Sequence[str] | None) -> list[str]:
    ids: list[str] = []
    for channel_id in channel_ids or []:
        if isinstance(channel_id, str) and channel_id and channel_id not in ids:
            ids.append(channel_id)
    return ids or _default_channel_ids()
