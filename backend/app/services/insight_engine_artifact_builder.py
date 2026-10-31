from __future__ import annotations

from dataclasses import dataclass
import threading
from time import perf_counter
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.orm import Session

from app.db.models import InsightEngineArtifact, FeedItem, InsightChannel, ItemState, Source, SourceItemOccurrence
from app.services.insight_engine_artifacts import (
    FEED_ITEM_INSIGHT_ARTIFACT_KINDS,
    FEED_ITEM_RANKING_ARTIFACT_KIND,
    FEED_ITEM_ROUTING_ARTIFACT_KIND,
    FEED_ITEM_SUMMARY_ARTIFACT_KIND,
    FEED_ITEM_TOPICS_ARTIFACT_KIND,
    InsightEngineArtifactRebuildStats,
    HomepagePriorityBand,
    FeedItemInsightRankingArtifactPayload,
    FeedItemInsightRoutingArtifactPayload,
    FeedItemInsightSummaryArtifactPayload,
    FeedItemInsightTopicsArtifactPayload,
    RankingReasonPayload,
    build_latest_occurrence_subquery,
    load_latest_feed_item_insight_artifacts,
)
from app.services.ai_provider import (
    AIProviderError,
    AIProviderInvalidJSONError,
    parse_json_object_content,
    request_chat_completion_json,
)
from app.services.app_settings import (
    AI_PROFILE_GENERAL,
    AI_ROUTE_DISABLED,
    AIProviderConfig,
    resolve_ai_provider_config_for_source_route,
)
from app.services.insight_channels import NEEDS_REVIEW_CHANNEL_ID, channel_is_active, get_channel_catalog_version
from app.services.insight_engine_request_builder import (
    INSIGHT_ENGINE_PROMPT_VERSION,
    INSIGHT_ENGINE_RESPONSE_SCHEMA_NAME,
    InsightEngineRequestBudgetError,
    InsightEngineRequestPayload,
    MAX_INSIGHT_ENGINE_INPUT_CHARS,
    build_insight_engine_response_schema,
    build_insight_engine_request_payload,
    load_insight_engine_request_context,
)
from app.services.insight_engine_contracts import (
    CONTENT_TYPE_FALLBACK_ID,
    TAXONOMY_FALLBACK_ID,
    canonicalize_content_type_id,
    canonicalize_taxonomy_id,
    get_ai_allowed_reason_label,
    get_content_type_label,
    get_taxonomy_label,
)
from app.services.insight_profiles import build_active_insight_profile_context
from app.services.insight_scoring import (
    ArticleScoringSignals,
    build_insight_scoring_context,
    score_feed_item_priority,
)
from app.services.insight_engine_attempts import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_SKIPPED,
    record_insight_engine_attempt,
)
from app.services.feed_content_extraction import (
    CONTENT_EXTRACTION_STATUS_FAILED,
    is_probably_url_only_or_jump_prompt,
)

FEED_ITEM_INSIGHT_PROMPT_VERSION = INSIGHT_ENGINE_PROMPT_VERSION
MAX_AI_INPUT_CHARS = MAX_INSIGHT_ENGINE_INPUT_CHARS
_feed_item_build_locks_guard = threading.Lock()
_feed_item_build_locks: dict[int, threading.Lock] = {}


@dataclass(frozen=True, slots=True)
class FeedItemInsightArtifactMissingCandidate:
    feed_item_id: int
    source_id: int
    ai_route: str
    profile: str
    provider: str


RoutingArtifactBuildStatus = Literal[
    "valid",
    "needs_review",
    "omitted",
    "invalid_shape",
    "invalid_channel_id",
    "disabled_channel_id",
    "invalid_payload",
]


@dataclass(frozen=True, slots=True)
class RoutingArtifactBuildResult:
    status: RoutingArtifactBuildStatus
    payload: FeedItemInsightRoutingArtifactPayload | None = None
    message: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelCatalogLookup:
    enabled_by_id: dict[str, bool]
    channel_catalog_version: int


class InsightEngineResponseParseError(ValueError):
    def __init__(self, message: str, *, repair_retry_attempted: bool, parse_attempt_count: int) -> None:
        super().__init__(message)
        self.repair_retry_attempted = repair_retry_attempted
        self.parse_attempt_count = parse_attempt_count


def _feed_item_artifact_build_lock(feed_item_id: int) -> threading.Lock:
    with _feed_item_build_locks_guard:
        lock = _feed_item_build_locks.get(feed_item_id)
        if lock is None:
            lock = threading.Lock()
            _feed_item_build_locks[feed_item_id] = lock
        return lock


def _acquire_feed_item_artifact_database_lock(session: Session, feed_item_id: int) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_namespace), CAST(:lock_id AS integer))"),
        {
            "lock_namespace": "infoflow_feed_item_artifact_build",
            "lock_id": int(feed_item_id),
        },
    )


class FeedItemInsightAIResponsePayload(BaseModel):
    summary: str | None = None
    topics: list[str] = Field(default_factory=lambda: [TAXONOMY_FALLBACK_ID])
    content_type: str = CONTENT_TYPE_FALLBACK_ID
    priority_score: int | None = Field(default=None, ge=0, le=100)
    priority_band: HomepagePriorityBand | None = None
    priority_reasons: list[RankingReasonPayload] = Field(default_factory=list)
    low_priority_bucket: bool | None = None
    routing: Any = None

    @field_validator("summary", mode="before")
    @classmethod
    def normalize_summary(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = " ".join(value.split()).strip()
            return normalized or None
        return value

    @field_validator("topics", mode="before")
    @classmethod
    def normalize_topics(cls, value: object) -> object:
        if not isinstance(value, list):
            return [TAXONOMY_FALLBACK_ID]
        topic_ids: list[str] = []
        seen: set[str] = set()
        for entry in value:
            taxonomy_id = canonicalize_taxonomy_id(entry)
            if taxonomy_id is None or taxonomy_id in seen:
                continue
            seen.add(taxonomy_id)
            topic_ids.append(taxonomy_id)
        return topic_ids[:3] or [TAXONOMY_FALLBACK_ID]

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return canonicalize_content_type_id(value) or CONTENT_TYPE_FALLBACK_ID

    @field_validator("priority_reasons", mode="before")
    @classmethod
    def normalize_priority_reasons(cls, value: object) -> object:
        if not isinstance(value, list):
            return []
        reasons: list[dict[str, object]] = []
        seen: set[str] = set()
        for entry in value:
            if not isinstance(entry, dict):
                continue
            code = entry.get("code")
            if not isinstance(code, str):
                continue
            code = code.strip()
            label = get_ai_allowed_reason_label(code)
            if label is None or code in seen:
                continue
            seen.add(code)
            reasons.append(
                {
                    "code": code,
                    "label": label,
                    "weight": _clamp_reason_weight(entry.get("weight")),
                }
            )
        return reasons[:5]

    @field_validator("priority_reasons")
    @classmethod
    def require_valid_priority_reasons(cls, value: list[RankingReasonPayload]) -> list[RankingReasonPayload]:
        if not value:
            raise ValueError("AI response must include at least one allowed reason code")
        return value


def build_missing_feed_item_insight_artifacts(
    session: Session,
    *,
    source_id: int | None = None,
    limit: int | None = None,
) -> InsightEngineArtifactRebuildStats:
    feed_item_ids = _list_feed_item_ids_missing_feed_item_insight_artifacts(session, source_id=source_id, limit=limit)
    scope = f"source:{source_id}" if source_id is not None else "all"
    return build_missing_feed_item_insight_artifacts_for_feed_items(session, feed_item_ids=feed_item_ids, scope=scope)


def list_feed_item_ids_missing_feed_item_insight_artifacts(
    session: Session,
    *,
    source_id: int | None = None,
    limit: int | None = None,
) -> list[int]:
    return _list_feed_item_ids_missing_feed_item_insight_artifacts(session, source_id=source_id, limit=limit)


def count_feed_item_ids_missing_feed_item_insight_artifacts(
    session: Session,
    *,
    source_id: int | None = None,
) -> int:
    statement = _feed_item_ids_missing_feed_item_insight_artifacts_statement(source_id=source_id).subquery()
    return int(session.scalar(select(func.count()).select_from(statement)) or 0)


def list_missing_feed_item_insight_artifact_candidates(
    session: Session,
    *,
    source_id: int | None = None,
    limit: int | None = None,
) -> list[FeedItemInsightArtifactMissingCandidate]:
    feed_item_ids = _list_feed_item_ids_missing_feed_item_insight_artifacts(session, source_id=source_id, limit=limit)
    if not feed_item_ids:
        return []

    rows = _list_homepage_context_rows(session, feed_item_ids=feed_item_ids)
    candidates: list[FeedItemInsightArtifactMissingCandidate] = []
    for feed_item, item_state, occurrence, source in rows:
        ai_route = (source.ai_route or AI_PROFILE_GENERAL).strip().lower()
        config = resolve_ai_provider_config_for_source_route(session, ai_route)
        if config is None:
            profile = AI_ROUTE_DISABLED
            provider = AI_ROUTE_DISABLED
        else:
            profile = config.profile
            provider = config.provider
        candidates.append(
            FeedItemInsightArtifactMissingCandidate(
                feed_item_id=feed_item.id,
                source_id=source.id,
                ai_route=ai_route,
                profile=profile,
                provider=provider,
            )
        )
    return candidates


def build_missing_feed_item_insight_artifacts_for_feed_item(
    session: Session,
    *,
    feed_item_id: int,
) -> InsightEngineArtifactRebuildStats:
    return build_feed_item_insight_artifacts_for_item(
        session,
        feed_item_id=feed_item_id,
        mode="build_missing",
        scope=f"feed_item:{feed_item_id}:missing",
    )


def rebuild_feed_item_insight_artifacts_for_feed_item(
    session: Session,
    *,
    feed_item_id: int,
) -> InsightEngineArtifactRebuildStats:
    return build_feed_item_insight_artifacts_for_item(
        session,
        feed_item_id=feed_item_id,
        mode="rebuild",
        scope=f"feed_item:{feed_item_id}",
    )


def build_feed_item_insight_artifacts_for_item(
    session: Session,
    *,
    feed_item_id: int,
    mode: Literal["build_missing", "rebuild"],
    scope: str,
) -> InsightEngineArtifactRebuildStats:
    with _feed_item_artifact_build_lock(feed_item_id):
        _acquire_feed_item_artifact_database_lock(session, feed_item_id)
        return _build_feed_item_insight_artifacts_for_item_locked(
            session,
            feed_item_id=feed_item_id,
            mode=mode,
            scope=scope,
        )


def _build_feed_item_insight_artifacts_for_item_locked(
    session: Session,
    *,
    feed_item_id: int,
    mode: Literal["build_missing", "rebuild"],
    scope: str,
) -> InsightEngineArtifactRebuildStats:
    total_started_at = perf_counter()
    rows = _list_homepage_context_rows(session, feed_item_ids=[feed_item_id])
    if not rows:
        return _empty_stats(scope)

    missing_kinds = (
        set(FEED_ITEM_INSIGHT_ARTIFACT_KINDS)
        if mode == "rebuild"
        else _missing_feed_item_insight_artifact_kinds(session, [feed_item_id]).get(feed_item_id, set())
    )
    if not missing_kinds:
        return _empty_stats(scope)

    for feed_item, item_state, occurrence, source in rows:
        config = _resolve_config_for_source(session, source)
        if config is None:
            _record_skipped_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=None,
                stage="provider_resolution",
                error_type="provider_disabled_or_unconfigured",
                error_message="AI provider is disabled or not configured for this source route.",
            )
            continue
        if _should_skip_ai_generation_for_low_confidence_content(feed_item):
            _record_skipped_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                stage="content_filter",
                error_type="low_confidence_content",
                error_message=feed_item.content_extraction_error
                or "Content extraction failed with URL-only or jump-prompt-like content.",
            )
            return InsightEngineArtifactRebuildStats(
                scope=scope,
                processed_feed_item_count=1,
                artifact_rows_created=0,
                failed_feed_item_count=1,
            )
        request_payload: InsightEngineRequestPayload | None = None
        started_at = perf_counter()
        timing_ms: dict[str, int] = {}
        try:
            request_build_started_at = perf_counter()
            request_payload = _build_insight_engine_request_payload(session, config, feed_item, source)
            timing_ms["request_build_ms"] = _elapsed_ms(request_build_started_at)

            provider_request_started_at = perf_counter()
            response_payload = _request_ai_homepage_payload(config, request_payload)
            timing_ms["provider_request_ms"] = _elapsed_ms(provider_request_started_at)

            artifact_build_started_at = perf_counter()
            channel_catalog = _channel_catalog_lookup(session)
            artifacts, routing_result = _build_artifacts_from_ai_response(
                session=session,
                feed_item=feed_item,
                item_state=item_state,
                occurrence=occurrence,
                source=source,
                config=config,
                model_name=config.artifact_model_name,
                payload=response_payload,
                missing_kinds=missing_kinds,
                channel_catalog=channel_catalog,
            )
            timing_ms["artifact_build_ms"] = _elapsed_ms(artifact_build_started_at)

            artifact_persist_started_at = perf_counter()
            with session.begin_nested():
                session.add_all(artifacts)
                session.flush()
            timing_ms["artifact_persist_ms"] = _elapsed_ms(artifact_persist_started_at)

            routing_diagnostic_started_at = perf_counter()
            _record_routing_diagnostic_if_needed(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                routing_result=routing_result,
                missing_kinds=missing_kinds,
                artifact_count=len(artifacts),
                request_payload=request_payload,
            )
            timing_ms["routing_diagnostic_ms"] = _elapsed_ms(routing_diagnostic_started_at)
            timing_ms["total_build_ms"] = _elapsed_ms(total_started_at)
            return InsightEngineArtifactRebuildStats(
                scope=scope,
                processed_feed_item_count=1,
                artifact_rows_created=len(artifacts),
                failed_feed_item_count=0,
                timing_ms=timing_ms,
            )
        except InsightEngineResponseParseError as exc:
            timing_ms["total_build_ms"] = _elapsed_ms(total_started_at)
            _record_failed_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                stage="response_parse",
                exc=exc,
                duration_ms=_elapsed_ms(started_at),
                request_payload=request_payload,
            )
        except AIProviderError as exc:
            timing_ms["total_build_ms"] = _elapsed_ms(total_started_at)
            _record_failed_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                stage="provider_request",
                exc=exc,
                duration_ms=_elapsed_ms(started_at),
                request_payload=request_payload,
            )
        except InsightEngineRequestBudgetError as exc:
            timing_ms["total_build_ms"] = _elapsed_ms(total_started_at)
            _record_failed_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                stage="request_build",
                exc=exc,
                duration_ms=_elapsed_ms(started_at),
                request_payload=request_payload,
            )
        except ValidationError as exc:
            timing_ms["total_build_ms"] = _elapsed_ms(total_started_at)
            _record_failed_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                stage="response_validation",
                exc=exc,
                duration_ms=_elapsed_ms(started_at),
                request_payload=request_payload,
            )
        except ValueError as exc:
            timing_ms["total_build_ms"] = _elapsed_ms(total_started_at)
            _record_failed_attempt(
                session,
                feed_item=feed_item,
                source=source,
                config=config,
                stage="response_parse" if request_payload is not None else "request_build",
                exc=exc,
                duration_ms=_elapsed_ms(started_at),
                request_payload=request_payload,
            )
        return InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=0,
            failed_feed_item_count=1,
            timing_ms=timing_ms or {"total_build_ms": _elapsed_ms(total_started_at)},
        )

    return _empty_stats(scope)


def build_missing_feed_item_insight_artifacts_for_feed_items(
    session: Session,
    *,
    feed_item_ids: Sequence[int],
    scope: str,
    limit: int | None = None,
) -> InsightEngineArtifactRebuildStats:
    unique_ids = list(dict.fromkeys(feed_item_ids))
    if limit is not None:
        unique_ids = unique_ids[:limit]
    if not unique_ids:
        return _empty_stats(scope)

    missing_by_item = _missing_feed_item_insight_artifact_kinds(session, unique_ids)
    candidate_ids = [feed_item_id for feed_item_id in unique_ids if missing_by_item.get(feed_item_id)]
    if limit is not None:
        candidate_ids = candidate_ids[:limit]
    if not candidate_ids:
        return _empty_stats(scope)

    artifact_rows_created = 0
    failed_feed_item_count = 0
    processed_feed_item_count = 0

    for feed_item_id in candidate_ids:
        stats = build_feed_item_insight_artifacts_for_item(
            session,
            feed_item_id=feed_item_id,
            mode="build_missing",
            scope=scope,
        )
        processed_feed_item_count += stats.processed_feed_item_count
        artifact_rows_created += stats.artifact_rows_created
        failed_feed_item_count += stats.failed_feed_item_count

    return InsightEngineArtifactRebuildStats(
        scope=scope,
        processed_feed_item_count=processed_feed_item_count,
        artifact_rows_created=artifact_rows_created,
        failed_feed_item_count=failed_feed_item_count,
    )


def _should_skip_ai_generation_for_low_confidence_content(feed_item: FeedItem) -> bool:
    if feed_item.content_extraction_status != CONTENT_EXTRACTION_STATUS_FAILED:
        return False
    candidate_text = feed_item.content_text or feed_item.feed_content_text or feed_item.summary_text
    return is_probably_url_only_or_jump_prompt(candidate_text)


def _resolve_config_for_source(session: Session, source: Source) -> AIProviderConfig | None:
    config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
    if config is None or not config.is_configured:
        return None
    return config


def _build_insight_engine_request_payload(
    session: Session,
    config: AIProviderConfig,
    feed_item: FeedItem,
    source: Source,
) -> InsightEngineRequestPayload:
    context = load_insight_engine_request_context(
        session=session,
        config=config,
        feed_item=feed_item,
        source=source,
        active_insight_profile_context=build_active_insight_profile_context(session),
    )
    return build_insight_engine_request_payload(context)


def _request_ai_homepage_payload(
    config: AIProviderConfig,
    request_payload: InsightEngineRequestPayload,
) -> FeedItemInsightAIResponsePayload:
    result = _request_ai_completion(config, request_payload.messages, request_payload)
    try:
        raw_payload = parse_json_object_content(result.content)
    except AIProviderInvalidJSONError as first_exc:
        repair_messages = [
            *request_payload.messages,
            {
                "role": "user",
                "content": (
                    "Your previous response was malformed JSON. Return one strict JSON object only, "
                    "with no markdown fences, comments, or explanatory text."
                ),
            },
        ]
        repair_result = _request_ai_completion(config, repair_messages, request_payload)
        try:
            raw_payload = parse_json_object_content(repair_result.content)
        except AIProviderInvalidJSONError as second_exc:
            raise InsightEngineResponseParseError(
                f"AI response remained invalid JSON after one repair retry: {second_exc}",
                repair_retry_attempted=True,
                parse_attempt_count=2,
            ) from first_exc
    return FeedItemInsightAIResponsePayload.model_validate(raw_payload)


def _request_ai_completion(
    config: AIProviderConfig,
    messages: list[dict[str, str]],
    request_payload: InsightEngineRequestPayload,
):
    result = request_chat_completion_json(
        config,
        messages=messages,
        max_tokens=request_payload.max_tokens,
        temperature=request_payload.temperature,
        json_schema_name=INSIGHT_ENGINE_RESPONSE_SCHEMA_NAME,
        json_schema=build_insight_engine_response_schema(
            taxonomy=request_payload.taxonomy,
            content_type_options=request_payload.content_type_options,
            reason_codes=request_payload.reason_codes,
            channel_ids=[channel.id for channel in request_payload.channels],
        ),
    )
    return result


def _build_artifacts_from_ai_response(
    *,
    session: Session,
    feed_item: FeedItem,
    item_state: ItemState,
    occurrence: SourceItemOccurrence,
    source: Source,
    config: AIProviderConfig,
    model_name: str,
    payload: FeedItemInsightAIResponsePayload,
    missing_kinds: set[str],
    channel_catalog: ChannelCatalogLookup,
) -> tuple[list[InsightEngineArtifact], RoutingArtifactBuildResult]:
    summary_payload = FeedItemInsightSummaryArtifactPayload(
        text=payload.summary,
        source_kind="ai_generated" if payload.summary else "none",
    )
    topics_payload = FeedItemInsightTopicsArtifactPayload(
        topic_ids=payload.topics,
        labels=[get_taxonomy_label(topic_id) for topic_id in payload.topics],
        content_type=payload.content_type,
        content_type_label=get_content_type_label(payload.content_type),
        source_kind="ai_generated" if payload.topics else "none",
    )
    routing_result = _build_routing_result_for_scoring(
        session=session,
        feed_item_id=feed_item.id,
        value=payload.routing,
        missing_kinds=missing_kinds,
        channel_catalog=channel_catalog,
    )
    signals = _article_signals_from_ai_response(payload, routing_result)
    scoring_context = build_insight_scoring_context(
        session,
        feed_item_ids=[feed_item.id],
        source_ids=[source.id],
        target_topic_ids=payload.topics,
    )
    priority = score_feed_item_priority(
        feed_item_id=feed_item.id,
        item_status=item_state.status,
        source_id=source.id,
        signals=signals,
        context=scoring_context,
        feed_item=feed_item,
        item_state=item_state,
        occurrence=occurrence,
        source=source,
        channel_catalog_version=channel_catalog.channel_catalog_version,
        provider_profile=config.profile,
    )
    ranking_payload = FeedItemInsightRankingArtifactPayload(
        priority_score=priority.score,
        priority_band=priority.band,
        priority_reasons=priority.reasons,
        low_priority_bucket=priority.low_priority_bucket,
        feature_schema_version=priority.feature_schema_version,
        score_version=priority.score_version,
        feature_vector=priority.feature_vector,
        score_breakdown=priority.score_breakdown,
    )

    payloads_by_kind: dict[str, dict[str, Any]] = {
        FEED_ITEM_SUMMARY_ARTIFACT_KIND: summary_payload.model_dump(),
        FEED_ITEM_TOPICS_ARTIFACT_KIND: topics_payload.model_dump(),
        FEED_ITEM_RANKING_ARTIFACT_KIND: ranking_payload.model_dump(),
    }
    if routing_result.payload is not None:
        payloads_by_kind[FEED_ITEM_ROUTING_ARTIFACT_KIND] = routing_result.payload.model_dump()
    artifacts = [
        InsightEngineArtifact(
            feed_item_id=feed_item.id,
            saved_item_id=None,
            artifact_kind=artifact_kind,
            model_name=model_name,
            prompt_version=FEED_ITEM_INSIGHT_PROMPT_VERSION,
            artifact_payload=payloads_by_kind[artifact_kind],
        )
        for artifact_kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS
        if artifact_kind in missing_kinds and artifact_kind in payloads_by_kind
    ]
    return artifacts, routing_result


def _build_routing_result_for_scoring(
    *,
    session: Session,
    feed_item_id: int,
    value: Any,
    missing_kinds: set[str],
    channel_catalog: ChannelCatalogLookup,
) -> RoutingArtifactBuildResult:
    if FEED_ITEM_ROUTING_ARTIFACT_KIND in missing_kinds:
        return _build_routing_artifact_payload(value, channel_catalog)

    latest_artifacts = load_latest_feed_item_insight_artifacts(session, feed_item_ids=[feed_item_id]).get(feed_item_id, {})
    existing_artifact = latest_artifacts.get(FEED_ITEM_ROUTING_ARTIFACT_KIND)
    if existing_artifact is not None:
        try:
            payload = FeedItemInsightRoutingArtifactPayload.model_validate(existing_artifact.artifact_payload)
        except ValidationError:
            pass
        else:
            return _existing_routing_result_for_scoring(payload, channel_catalog)

    return _build_routing_artifact_payload(value, channel_catalog)


def _article_signals_from_ai_response(
    payload: FeedItemInsightAIResponsePayload,
    routing_result: RoutingArtifactBuildResult,
) -> ArticleScoringSignals:
    routing_breakdown = (
        routing_result.payload.routing_breakdown
        if routing_result.payload is not None and isinstance(routing_result.payload.routing_breakdown, dict)
        else {}
    )
    candidates = routing_breakdown.get("candidates") if isinstance(routing_breakdown, dict) else []
    llm_candidates = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("source") == "llm_candidate"
    ]
    top_confidence = max(
        (
            float(candidate.get("confidence"))
            for candidate in llm_candidates
            if isinstance(candidate.get("confidence"), (int, float))
        ),
        default=0.0,
    )
    return ArticleScoringSignals(
        topic_ids=tuple(payload.topics),
        content_type=payload.content_type,
        ai_reasons=tuple(payload.priority_reasons),
        llm_candidate_count=len(llm_candidates),
        top_llm_candidate_confidence=top_confidence,
        uses_needs_review_fallback=(
            routing_result.payload is not None
            and routing_result.payload.primary_channel_id == NEEDS_REVIEW_CHANNEL_ID
            and routing_result.status != "valid"
        ),
    )


def _record_skipped_attempt(
    session: Session,
    *,
    feed_item: FeedItem,
    source: Source,
    config: AIProviderConfig | None,
    stage: str,
    error_type: str,
    error_message: str,
) -> None:
    record_insight_engine_attempt(
        session,
        feed_item=feed_item,
        source=source,
        config=config,
        status=ATTEMPT_STATUS_SKIPPED,
        stage=stage,
        error_type=error_type,
        error_message=error_message,
        artifact_count=0,
        request_snapshot={
            "feed_item_id": feed_item.id,
            "source_id": source.id,
            "source_name": source.name,
            "ai_route": source.ai_route,
            "content_extraction_status": feed_item.content_extraction_status,
            "content_extraction_error": feed_item.content_extraction_error,
        },
    )


def _record_failed_attempt(
    session: Session,
    *,
    feed_item: FeedItem,
    source: Source,
    config: AIProviderConfig,
    stage: str,
    exc: Exception,
    duration_ms: int,
    request_payload: InsightEngineRequestPayload | None,
) -> None:
    request_snapshot = _request_snapshot(request_payload)
    request_snapshot = _add_validation_metadata_for_failure(request_snapshot, stage=stage, exc=exc)
    if isinstance(exc, InsightEngineResponseParseError):
        request_snapshot = request_snapshot or {}
        request_snapshot["response_parse"] = {
            "repair_retry_attempted": exc.repair_retry_attempted,
            "parse_attempt_count": exc.parse_attempt_count,
        }
    record_insight_engine_attempt(
        session,
        feed_item=feed_item,
        source=source,
        config=config,
        status=ATTEMPT_STATUS_FAILED,
        stage=stage,
        error_type=_attempt_error_type(exc),
        error_message=str(exc),
        duration_ms=duration_ms,
        artifact_count=0,
        request_snapshot=request_snapshot,
    )


def _record_routing_diagnostic_if_needed(
    session: Session,
    *,
    feed_item: FeedItem,
    source: Source,
    config: AIProviderConfig,
    routing_result: RoutingArtifactBuildResult,
    missing_kinds: set[str],
    artifact_count: int,
    request_payload: InsightEngineRequestPayload | None,
) -> None:
    if FEED_ITEM_ROUTING_ARTIFACT_KIND not in missing_kinds or routing_result.status == "valid":
        return
    if routing_result.status == "needs_review":
        return

    snapshot = _request_snapshot(request_payload) or {}
    snapshot = _add_validation_metadata_for_routing(snapshot, routing_result)
    snapshot["routing_diagnostic"] = {
        "status": routing_result.status,
        "message": routing_result.message,
        "channel_id": routing_result.channel_id,
        "missing_artifact_kind": FEED_ITEM_ROUTING_ARTIFACT_KIND,
    }
    record_insight_engine_attempt(
        session,
        feed_item=feed_item,
        source=source,
        config=config,
        status=ATTEMPT_STATUS_SKIPPED,
        stage="routing_validation",
        error_type=routing_result.status,
        error_message=routing_result.message,
        artifact_count=artifact_count,
        request_snapshot=snapshot,
    )


def _request_snapshot(request_payload: InsightEngineRequestPayload | None) -> dict[str, Any] | None:
    if request_payload is None:
        return None
    return {
        "prompt_version": request_payload.prompt_version,
        "profile": request_payload.profile,
        "provider": request_payload.provider,
        "model": request_payload.model,
        "endpoint": request_payload.endpoint,
        "temperature": request_payload.temperature,
        "max_tokens": request_payload.max_tokens,
        "profile_version": request_payload.profile_version,
        "source_profile_version": request_payload.source_profile_version,
        "source_profile_is_empty": request_payload.source_profile_is_empty,
        "feature_schema_version": request_payload.feature_schema_version,
        "score_version": request_payload.score_version,
        "channel_catalog_version": request_payload.channel_catalog_version,
        "message_count": len(request_payload.messages),
        "system_message_char_count": len(request_payload.system_message),
        "user_message_char_count": len(request_payload.user_message),
        "content_text_source": request_payload.content_text_source,
        "stats": request_payload.stats.model_dump(mode="json"),
        "request_body_keys": sorted(request_payload.request_body.keys()),
        "fallback_request_body_keys": sorted(request_payload.fallback_request_body.keys()),
        "taxonomy_count": len(request_payload.taxonomy),
        "content_type_count": len(request_payload.content_type_options),
        "reason_code_count": len(request_payload.reason_codes),
        "channel_count": len(request_payload.channels),
        "source_channel_tendency_count": len(request_payload.source_channel_tendencies),
    }


def _attempt_error_type(exc: Exception) -> str:
    if isinstance(exc, InsightEngineRequestBudgetError):
        return exc.error_type
    return exc.__class__.__name__


def _add_validation_metadata_for_failure(
    snapshot: dict[str, Any] | None,
    *,
    stage: str,
    exc: Exception,
) -> dict[str, Any] | None:
    if snapshot is None and not isinstance(exc, InsightEngineRequestBudgetError):
        return snapshot
    next_snapshot = snapshot or {}
    validation = dict(next_snapshot.get("validation") if isinstance(next_snapshot.get("validation"), dict) else {})
    if isinstance(exc, InsightEngineResponseParseError):
        validation["parse"] = {
            "status": "failed",
            "repair_retry_attempted": exc.repair_retry_attempted,
            "parse_attempt_count": exc.parse_attempt_count,
        }
        validation.setdefault("schema", {"status": "not_run"})
        validation.setdefault("channel", {"status": "not_run"})
    elif isinstance(exc, ValidationError):
        validation["parse"] = {"status": "succeeded", "parse_attempt_count": 1}
        validation["schema"] = {"status": "failed", "error_type": exc.__class__.__name__}
        validation.setdefault("channel", {"status": "not_run"})
    elif isinstance(exc, InsightEngineRequestBudgetError):
        budget = {
            "error_type": exc.error_type,
            "source": exc.source,
            "source_char_count": exc.source_char_count,
            "max_chars": exc.max_chars,
        }
        validation["request_budget"] = {"status": "failed", **budget}
        validation.setdefault("parse", {"status": "not_run"})
        validation.setdefault("schema", {"status": "not_run"})
        validation.setdefault("channel", {"status": "not_run"})
        next_snapshot.setdefault("budget", budget)
    elif stage == "provider_request":
        validation.setdefault("parse", {"status": "not_run"})
        validation.setdefault("schema", {"status": "not_run"})
        validation.setdefault("channel", {"status": "not_run"})
    next_snapshot["validation"] = validation
    return next_snapshot


def _add_validation_metadata_for_routing(
    snapshot: dict[str, Any],
    routing_result: RoutingArtifactBuildResult,
) -> dict[str, Any]:
    validation = dict(snapshot.get("validation") if isinstance(snapshot.get("validation"), dict) else {})
    validation.setdefault("parse", {"status": "succeeded", "parse_attempt_count": 1})
    validation.setdefault("schema", {"status": "succeeded"})
    validation["channel"] = {
        "status": "succeeded" if routing_result.status in {"valid", "needs_review"} else "failed",
        "result": routing_result.status,
        "channel_id": routing_result.channel_id,
        "message": routing_result.message,
    }
    snapshot["validation"] = validation
    return snapshot


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _list_feed_item_ids_missing_feed_item_insight_artifacts(
    session: Session,
    *,
    source_id: int | None,
    limit: int | None,
) -> list[int]:
    statement = _feed_item_ids_missing_feed_item_insight_artifacts_statement(source_id=source_id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def _feed_item_ids_missing_feed_item_insight_artifacts_statement(*, source_id: int | None):
    missing_conditions = [
        ~exists().where(
            and_(
                InsightEngineArtifact.feed_item_id == FeedItem.id,
                InsightEngineArtifact.artifact_kind == artifact_kind,
            )
        )
        for artifact_kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS
    ]

    statement = select(FeedItem.id).join(ItemState, ItemState.feed_item_id == FeedItem.id).where(or_(*missing_conditions))
    if source_id is not None:
        latest_occurrences = build_latest_occurrence_subquery()
        statement = (
            statement.join(
                latest_occurrences,
                (latest_occurrences.c.feed_item_id == FeedItem.id)
                & (latest_occurrences.c.occurrence_rank == 1),
            )
            .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
            .where(SourceItemOccurrence.source_id == source_id)
        )
    return statement.order_by(FeedItem.id.desc())


def _missing_feed_item_insight_artifact_kinds(session: Session, feed_item_ids: Sequence[int]) -> dict[int, set[str]]:
    existing = load_latest_feed_item_insight_artifacts(session, feed_item_ids=feed_item_ids)
    expected = set(FEED_ITEM_INSIGHT_ARTIFACT_KINDS)
    return {
        feed_item_id: expected - set(existing.get(feed_item_id, {}).keys())
        for feed_item_id in feed_item_ids
    }


def _build_routing_artifact_payload(
    value: Any,
    channel_catalog: ChannelCatalogLookup,
) -> RoutingArtifactBuildResult:
    if value is None:
        return _fallback_routing_build_result(
            status="omitted",
            message="AI response omitted feed item routing.",
            channel_catalog=channel_catalog,
            rejected_candidates=[],
        )
    if not isinstance(value, dict):
        return _fallback_routing_build_result(
            status="invalid_shape",
            message="AI response routing must be a JSON object.",
            channel_catalog=channel_catalog,
            rejected_candidates=[],
        )
    try:
        payload = FeedItemInsightRoutingArtifactPayload.model_validate(value)
    except ValidationError as exc:
        return _fallback_routing_build_result(
            status="invalid_payload",
            message=str(exc),
            channel_catalog=channel_catalog,
            rejected_candidates=[],
        )

    if payload.primary_channel_id is None:
        return _fallback_routing_build_result(
            status="needs_review",
            message="AI response routing selected needs_review because primary_channel_id was not specified.",
            channel_catalog=channel_catalog,
            rejected_candidates=_routing_candidates_from_payload(payload),
            reason_codes=["ambiguous_channel_needs_review"],
        )

    channel_validation_result = _routing_channel_validation_result(payload, channel_catalog)
    if channel_validation_result is not None:
        return channel_validation_result
    routing_breakdown = _routing_breakdown_for_valid_payload(payload, channel_catalog.channel_catalog_version)
    payload = payload.model_copy(
        update={
            "channel_catalog_version": channel_catalog.channel_catalog_version,
            "routing_breakdown": routing_breakdown,
        }
    )
    return RoutingArtifactBuildResult(status="valid", payload=payload)


def _existing_routing_result_for_scoring(
    payload: FeedItemInsightRoutingArtifactPayload,
    channel_catalog: ChannelCatalogLookup,
) -> RoutingArtifactBuildResult:
    channel_validation_result = _routing_channel_validation_result(payload, channel_catalog)
    if channel_validation_result is not None:
        return channel_validation_result
    if _routing_payload_is_needs_review_fallback(payload):
        return RoutingArtifactBuildResult(
            status="omitted",
            message=_routing_fallback_message(payload),
            channel_id=NEEDS_REVIEW_CHANNEL_ID,
            payload=payload,
        )
    return RoutingArtifactBuildResult(status="valid", payload=payload)


def _routing_channel_validation_result(
    payload: FeedItemInsightRoutingArtifactPayload,
    channel_catalog: ChannelCatalogLookup,
) -> RoutingArtifactBuildResult | None:
    for channel_id in _referenced_routing_channel_ids(payload):
        enabled = channel_catalog.enabled_by_id.get(channel_id)
        if enabled is None:
            return _fallback_routing_build_result(
                status="invalid_channel_id",
                message=f"Routing references unknown channel id: {channel_id}",
                channel_id=channel_id,
                channel_catalog=channel_catalog,
                rejected_candidates=_routing_candidates_from_payload(
                    payload,
                    rejected_channel_id=channel_id,
                    rejected_reason="unknown_channel_id",
                ),
            )
        if not enabled:
            return _fallback_routing_build_result(
                status="disabled_channel_id",
                message=f"Routing references disabled channel id: {channel_id}",
                channel_id=channel_id,
                channel_catalog=channel_catalog,
                rejected_candidates=_routing_candidates_from_payload(
                    payload,
                    rejected_channel_id=channel_id,
                    rejected_reason="disabled_or_retired_channel",
                ),
            )
    return None


def _referenced_routing_channel_ids(payload: FeedItemInsightRoutingArtifactPayload) -> list[str]:
    channel_ids = [payload.primary_channel_id] if payload.primary_channel_id is not None else []
    channel_ids.extend(secondary.channel_id for secondary in payload.secondary_channels)
    return channel_ids


def _routing_payload_is_needs_review_fallback(payload: FeedItemInsightRoutingArtifactPayload) -> bool:
    if payload.primary_channel_id != NEEDS_REVIEW_CHANNEL_ID:
        return False
    routing_breakdown = payload.routing_breakdown if isinstance(payload.routing_breakdown, dict) else {}
    decision = routing_breakdown.get("decision") if isinstance(routing_breakdown, dict) else None
    if isinstance(decision, dict) and decision.get("fallback_reason"):
        return True
    candidates = routing_breakdown.get("candidates") if isinstance(routing_breakdown, dict) else []
    if isinstance(candidates, list) and any(
        isinstance(candidate, dict)
        and candidate.get("channel_id") == NEEDS_REVIEW_CHANNEL_ID
        and candidate.get("source") == "fallback"
        and candidate.get("accepted") is True
        for candidate in candidates
    ):
        return True
    return (
        "invalid_channel_reference" in payload.reason_codes
        or "ambiguous_channel_needs_review" in payload.isolation_reason_codes
    )


def _routing_fallback_message(payload: FeedItemInsightRoutingArtifactPayload) -> str | None:
    routing_breakdown = payload.routing_breakdown if isinstance(payload.routing_breakdown, dict) else {}
    decision = routing_breakdown.get("decision") if isinstance(routing_breakdown, dict) else None
    fallback_reason = decision.get("fallback_reason") if isinstance(decision, dict) else None
    if isinstance(fallback_reason, str) and fallback_reason.strip():
        return fallback_reason.strip()
    return payload.notes or "Existing routing artifact uses needs_review fallback."


def _channel_catalog_lookup(session: Session) -> ChannelCatalogLookup:
    channels = list(session.scalars(select(InsightChannel)))
    if not channels:
        raise ValueError("Insight channel catalog is empty; seed or configure channels before building insight artifacts.")
    enabled_by_id = {channel.id: channel_is_active(channel) for channel in channels}
    if not enabled_by_id.get(NEEDS_REVIEW_CHANNEL_ID):
        raise ValueError("Insight channel catalog is missing active system channel needs_review.")
    return ChannelCatalogLookup(
        enabled_by_id=enabled_by_id,
        channel_catalog_version=get_channel_catalog_version(session),
    )


def _fallback_routing_build_result(
    *,
    status: Literal["needs_review", "omitted", "invalid_shape", "invalid_payload", "invalid_channel_id", "disabled_channel_id"],
    message: str,
    channel_catalog: ChannelCatalogLookup,
    rejected_candidates: list[dict[str, Any]],
    channel_id: str | None = None,
    reason_codes: list[str] | None = None,
) -> RoutingArtifactBuildResult:
    routing_breakdown = _routing_breakdown_for_fallback(
        channel_catalog_version=channel_catalog.channel_catalog_version,
        message=message,
        rejected_candidates=rejected_candidates,
    )
    payload = FeedItemInsightRoutingArtifactPayload(
        schema_version=FEED_ITEM_ROUTING_ARTIFACT_KIND,
        primary_channel_id=NEEDS_REVIEW_CHANNEL_ID,
        secondary_channels=[],
        reading_depth="medium",
        flow_lane="needs_review",
        confidence=0.0,
        reason_codes=reason_codes or ["invalid_channel_reference"],
        promotion_reason_codes=[],
        isolation_reason_codes=["ambiguous_channel_needs_review"],
        source_policy_applied="normal",
        notes=message[:500],
        source="insight_engine_artifact",
        channel_catalog_version=channel_catalog.channel_catalog_version,
        routing_breakdown=routing_breakdown,
    )
    return RoutingArtifactBuildResult(status=status, message=message, channel_id=channel_id, payload=payload)


def _routing_candidates_from_payload(
    payload: FeedItemInsightRoutingArtifactPayload,
    *,
    rejected_channel_id: str | None = None,
    rejected_reason: str | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    rank = 1
    if payload.primary_channel_id is not None:
        candidates.append(
            _routing_candidate(
                channel_id=payload.primary_channel_id,
                source="llm_candidate",
                confidence=payload.confidence,
                reason_codes=payload.reason_codes,
                accepted=payload.primary_channel_id != rejected_channel_id,
                rejected_reason=rejected_reason if payload.primary_channel_id == rejected_channel_id else None,
                rank=rank,
            )
        )
        rank += 1
    for secondary in payload.secondary_channels:
        candidates.append(
            _routing_candidate(
                channel_id=secondary.channel_id,
                source="llm_candidate",
                confidence=secondary.confidence,
                reason_codes=payload.reason_codes,
                accepted=secondary.channel_id != rejected_channel_id,
                rejected_reason=rejected_reason if secondary.channel_id == rejected_channel_id else None,
                rank=rank,
            )
        )
        rank += 1
    return candidates


def _routing_breakdown_for_valid_payload(
    payload: FeedItemInsightRoutingArtifactPayload,
    channel_catalog_version: int,
) -> dict[str, Any]:
    candidates = _routing_candidates_from_payload(payload)
    secondary_ids = [secondary.channel_id for secondary in payload.secondary_channels]
    return {
        "channel_catalog_version": channel_catalog_version,
        "candidates": candidates,
        "decision": {
            "primary_channel_id": payload.primary_channel_id,
            "secondary_channel_ids": secondary_ids,
            "routing_source": payload.source,
            "routing_reason_codes": payload.reason_codes,
            "fallback_reason": None,
        },
    }


def _routing_breakdown_for_fallback(
    *,
    channel_catalog_version: int,
    message: str,
    rejected_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback_candidate = _routing_candidate(
        channel_id=NEEDS_REVIEW_CHANNEL_ID,
        source="fallback",
        confidence=0.0,
        reason_codes=["invalid_channel_reference"],
        accepted=True,
        rejected_reason=None,
        rank=len(rejected_candidates) + 1,
    )
    return {
        "channel_catalog_version": channel_catalog_version,
        "candidates": [*rejected_candidates, fallback_candidate],
        "decision": {
            "primary_channel_id": NEEDS_REVIEW_CHANNEL_ID,
            "secondary_channel_ids": [],
            "routing_source": "insight_engine_artifact",
            "routing_reason_codes": ["invalid_channel_reference"],
            "fallback_reason": message[:500],
        },
    }


def _routing_candidate(
    *,
    channel_id: str,
    source: str,
    confidence: float,
    reason_codes: list[str],
    accepted: bool,
    rejected_reason: str | None,
    rank: int,
) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "source": source,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "reason_codes": list(reason_codes),
        "accepted": accepted,
        "rejected_reason": rejected_reason,
        "rank": rank,
    }


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
    rows = session.execute(statement).all()
    order = {feed_item_id: index for index, feed_item_id in enumerate(feed_item_ids)}
    return sorted(rows, key=lambda row: order.get(row[0].id, len(order)))


def _clamp_reason_weight(value: object) -> int:
    try:
        weight = int(float(value))
    except (TypeError, ValueError):
        weight = 0
    return max(0, min(100, weight))


def _empty_stats(scope: str) -> InsightEngineArtifactRebuildStats:
    return InsightEngineArtifactRebuildStats(
        scope=scope,
        processed_feed_item_count=0,
        artifact_rows_created=0,
        failed_feed_item_count=0,
    )
