from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, InsightEngineAttempt, ItemState, Source, SourceItemOccurrence
from app.schemas.settings import (
    InsightEngineCoverageItemRead,
    InsightEngineCoverageLatestAttemptRead,
    InsightEngineCoverageRead,
    InsightEngineCoverageReasonRead,
)
from app.services.insight_engine_artifacts import (
    FEED_ITEM_INSIGHT_ARTIFACT_KINDS,
    build_latest_occurrence_subquery,
    load_latest_feed_item_insight_artifacts,
)
from app.services.app_settings import (
    AI_PROFILE_GENERAL,
    AI_ROUTE_DISABLED,
    resolve_ai_provider_config_for_source_route,
)
from app.services.feed_content_extraction import CONTENT_EXTRACTION_STATUS_FAILED, is_probably_url_only_or_jump_prompt


@dataclass(frozen=True, slots=True)
class CoverageReason:
    code: str
    label: str
    retryable: bool
    category: str


@dataclass(frozen=True, slots=True)
class RetryableCoverageCandidate:
    feed_item_id: int
    source_id: int


REASON_ROUTE_DISABLED = CoverageReason(
    code="source_ai_route_disabled",
    label="Source AI route is disabled; Insight Engine will not generate artifacts.",
    retryable=False,
    category="blocked",
)
REASON_PROVIDER_UNCONFIGURED = CoverageReason(
    code="provider_disabled_or_unconfigured",
    label="AI provider is disabled or incomplete for this source route.",
    retryable=False,
    category="blocked",
)
REASON_LOW_CONFIDENCE_CONTENT = CoverageReason(
    code="low_confidence_content",
    label="Content is URL-only, a jump prompt, or too low-confidence for generation.",
    retryable=False,
    category="blocked",
)
REASON_PREVIOUS_FAILED = CoverageReason(
    code="previous_failed_attempt",
    label="Latest Insight Engine attempt failed.",
    retryable=True,
    category="problem",
)
REASON_PREVIOUS_SKIPPED = CoverageReason(
    code="previous_skipped_attempt",
    label="Latest Insight Engine attempt was skipped.",
    retryable=True,
    category="problem",
)
REASON_PARTIAL_ARTIFACTS = CoverageReason(
    code="partial_artifacts",
    label="Some Insight Engine artifact kinds exist, but one or more are missing.",
    retryable=True,
    category="problem",
)
REASON_NO_ATTEMPT_HISTORY = CoverageReason(
    code="no_attempt_history",
    label="No artifacts and no attempt history were found.",
    retryable=True,
    category="initial_generation",
)

COVERAGE_PROBLEM_REASON_CODES = {
    REASON_PREVIOUS_FAILED.code,
    REASON_PREVIOUS_SKIPPED.code,
    REASON_PARTIAL_ARTIFACTS.code,
}
COVERAGE_INITIAL_GENERATION_REASON_CODES = {REASON_NO_ATTEMPT_HISTORY.code}
COVERAGE_BLOCKED_REASON_CODES = {
    REASON_ROUTE_DISABLED.code,
    REASON_PROVIDER_UNCONFIGURED.code,
    REASON_LOW_CONFIDENCE_CONTENT.code,
}
COVERAGE_REASON_CODES = (
    COVERAGE_PROBLEM_REASON_CODES | COVERAGE_INITIAL_GENERATION_REASON_CODES | COVERAGE_BLOCKED_REASON_CODES
)
_COVERAGE_CATEGORY_ORDER = {"problem": 0, "blocked": 1, "initial_generation": 2}


def get_insight_engine_coverage(
    session: Session,
    *,
    source_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> InsightEngineCoverageRead:
    rows = _list_item_context_rows(session, source_id=source_id)
    feed_item_ids = [feed_item.id for feed_item, _item_state, _occurrence, _source in rows]
    artifact_map = load_latest_feed_item_insight_artifacts(session, feed_item_ids=feed_item_ids)
    latest_attempts = _load_latest_attempts(session, feed_item_ids=feed_item_ids)

    incomplete_items: list[InsightEngineCoverageItemRead] = []
    complete_item_count = 0
    no_artifact_item_count = 0
    partial_artifact_item_count = 0
    retryable_item_count = 0
    problem_item_count = 0
    problem_retryable_item_count = 0
    initial_generation_item_count = 0
    initial_generation_retryable_item_count = 0
    blocked_item_count = 0
    reason_counts: Counter[str] = Counter()
    reason_labels: dict[str, str] = {}

    for feed_item, _item_state, _occurrence, source in rows:
        existing_kinds = set(artifact_map.get(feed_item.id, {}).keys())
        missing_kinds = list(FEED_ITEM_INSIGHT_ARTIFACT_KINDS)
        if existing_kinds:
            missing_kinds = [kind for kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS if kind not in existing_kinds]
        if not missing_kinds:
            complete_item_count += 1
            continue

        if existing_kinds:
            partial_artifact_item_count += 1
        else:
            no_artifact_item_count += 1

        latest_attempt = latest_attempts.get(feed_item.id)
        reason = _classify_incomplete_item(
            feed_item=feed_item,
            source=source,
            existing_kinds=existing_kinds,
            latest_attempt=latest_attempt,
            session=session,
        )
        reason_counts[reason.code] += 1
        reason_labels[reason.code] = reason.label
        if reason.retryable:
            retryable_item_count += 1
        if reason.category == "problem":
            problem_item_count += 1
            if reason.retryable:
                problem_retryable_item_count += 1
        elif reason.category == "initial_generation":
            initial_generation_item_count += 1
            if reason.retryable:
                initial_generation_retryable_item_count += 1
        elif reason.category == "blocked":
            blocked_item_count += 1

        config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
        ai_route = (source.ai_route or AI_PROFILE_GENERAL).strip().lower()
        incomplete_items.append(
            InsightEngineCoverageItemRead(
                feed_item_id=feed_item.id,
                title=feed_item.title,
                source_id=source.id,
                source_name=source.name,
                ai_route=ai_route,
                profile=config.profile if config is not None else AI_ROUTE_DISABLED,
                provider=config.provider if config is not None else AI_ROUTE_DISABLED,
                model=config.model if config is not None else None,
                existing_artifact_kinds=[kind for kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS if kind in existing_kinds],
                missing_artifact_kinds=missing_kinds,
                content_text_source=feed_item.content_text_source,
                content_extraction_status=feed_item.content_extraction_status,
                content_extraction_error=feed_item.content_extraction_error,
                possible_reason_code=reason.code,
                possible_reason_label=reason.label,
                coverage_category=reason.category,
                retryable=reason.retryable,
                latest_attempt=_build_latest_attempt_read(latest_attempt),
            )
        )

    incomplete_items.sort(key=_coverage_item_sort_key)
    page_items = incomplete_items[offset : offset + limit]
    return InsightEngineCoverageRead(
        total_item_count=len(rows),
        complete_item_count=complete_item_count,
        incomplete_item_count=len(incomplete_items),
        no_artifact_item_count=no_artifact_item_count,
        partial_artifact_item_count=partial_artifact_item_count,
        retryable_item_count=retryable_item_count,
        problem_item_count=problem_item_count,
        problem_retryable_item_count=problem_retryable_item_count,
        initial_generation_item_count=initial_generation_item_count,
        initial_generation_retryable_item_count=initial_generation_retryable_item_count,
        blocked_item_count=blocked_item_count,
        reason_counts=[
            InsightEngineCoverageReasonRead(code=code, label=reason_labels[code], count=count)
            for code, count in sorted(reason_counts.items())
        ],
        items=page_items,
        total=len(incomplete_items),
        limit=limit,
        offset=offset,
    )


def list_retryable_insight_engine_coverage_candidates(
    session: Session,
    *,
    source_id: int | None = None,
    limit: int | None = None,
    reason_codes: list[str] | None = None,
) -> list[RetryableCoverageCandidate]:
    if limit == 0:
        return []

    rows = _list_item_context_rows(session, source_id=source_id)
    feed_item_ids = [feed_item.id for feed_item, _item_state, _occurrence, _source in rows]
    artifact_map = load_latest_feed_item_insight_artifacts(session, feed_item_ids=feed_item_ids)
    latest_attempts = _load_latest_attempts(session, feed_item_ids=feed_item_ids)
    allowed_reason_codes = set(reason_codes) if reason_codes is not None else None

    candidates: list[RetryableCoverageCandidate] = []
    for feed_item, _item_state, _occurrence, source in rows:
        existing_kinds = set(artifact_map.get(feed_item.id, {}).keys())
        missing_kinds = [kind for kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS if kind not in existing_kinds]
        if not missing_kinds:
            continue

        reason = _classify_incomplete_item(
            feed_item=feed_item,
            source=source,
            existing_kinds=existing_kinds,
            latest_attempt=latest_attempts.get(feed_item.id),
            session=session,
        )
        if allowed_reason_codes is not None and reason.code not in allowed_reason_codes:
            continue
        if not reason.retryable:
            continue
        candidates.append(RetryableCoverageCandidate(feed_item_id=feed_item.id, source_id=source.id))
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def _list_item_context_rows(session: Session, *, source_id: int | None):
    latest_occurrences = build_latest_occurrence_subquery()
    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .order_by(FeedItem.id.desc())
    )
    if source_id is not None:
        statement = statement.where(Source.id == source_id)
    return session.execute(statement).all()


def _load_latest_attempts(session: Session, *, feed_item_ids: list[int]) -> dict[int, InsightEngineAttempt]:
    if not feed_item_ids:
        return {}
    latest_attempt_ids = (
        select(
            InsightEngineAttempt.feed_item_id.label("feed_item_id"),
            func.max(InsightEngineAttempt.id).label("latest_id"),
        )
        .where(InsightEngineAttempt.feed_item_id.in_(feed_item_ids))
        .group_by(InsightEngineAttempt.feed_item_id)
        .subquery()
    )
    attempts = session.scalars(
        select(InsightEngineAttempt).join(latest_attempt_ids, InsightEngineAttempt.id == latest_attempt_ids.c.latest_id)
    ).all()
    return {attempt.feed_item_id: attempt for attempt in attempts if attempt.feed_item_id is not None}


def _classify_incomplete_item(
    *,
    feed_item: FeedItem,
    source: Source,
    existing_kinds: set[str],
    latest_attempt: InsightEngineAttempt | None,
    session: Session,
) -> CoverageReason:
    config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
    if config is None:
        return REASON_ROUTE_DISABLED
    if not config.is_configured:
        return REASON_PROVIDER_UNCONFIGURED
    if _should_skip_ai_generation_for_low_confidence_content(feed_item):
        return REASON_LOW_CONFIDENCE_CONTENT
    if latest_attempt is not None and latest_attempt.status == "failed":
        return REASON_PREVIOUS_FAILED
    if latest_attempt is not None and latest_attempt.status == "skipped":
        return REASON_PREVIOUS_SKIPPED
    if existing_kinds:
        return REASON_PARTIAL_ARTIFACTS
    return REASON_NO_ATTEMPT_HISTORY


def _coverage_item_sort_key(item: InsightEngineCoverageItemRead) -> tuple[int, int]:
    return (_COVERAGE_CATEGORY_ORDER.get(item.coverage_category, 99), -item.feed_item_id)


def _should_skip_ai_generation_for_low_confidence_content(feed_item: FeedItem) -> bool:
    if feed_item.content_extraction_status != CONTENT_EXTRACTION_STATUS_FAILED:
        return False
    candidate_text = feed_item.content_text or feed_item.feed_content_text or feed_item.summary_text
    return is_probably_url_only_or_jump_prompt(candidate_text)


def _build_latest_attempt_read(attempt: InsightEngineAttempt | None) -> InsightEngineCoverageLatestAttemptRead | None:
    if attempt is None:
        return None
    return InsightEngineCoverageLatestAttemptRead(
        id=attempt.id,
        status=attempt.status,
        stage=attempt.stage,
        error_type=attempt.error_type,
        error_message=attempt.error_message,
        created_at=attempt.created_at,
        completed_at=attempt.completed_at,
    )
