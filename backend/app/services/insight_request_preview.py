from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, ItemState, Source, SourceItemOccurrence
from app.schemas.settings import AIInsightRequestPreviewRead, AIInsightRequestPreviewSampleRead
from app.services.insight_engine_artifacts import (
    FEED_ITEM_INSIGHT_ARTIFACT_KINDS,
    build_latest_occurrence_subquery,
    load_latest_feed_item_insight_artifacts,
)
from app.services.app_settings import (
    AI_ROUTE_DISABLED,
    AIProviderConfig,
    resolve_ai_provider_config_for_source_route,
)
from app.services.insight_engine_request_builder import build_insight_engine_request_payload
from app.services.insight_engine_request_builder import load_insight_engine_request_context
from app.services.insight_profiles import build_active_insight_profile_context

PREVIEW_CANDIDATE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class _PreviewCandidate:
    feed_item: FeedItem
    source: Source
    config: AIProviderConfig
    missing_artifact_kinds: list[str]

    @property
    def has_content_text(self) -> bool:
        return bool((self.feed_item.content_text or "").strip())

    @property
    def has_missing_artifacts(self) -> bool:
        return bool(self.missing_artifact_kinds)

    @property
    def sort_timestamp(self) -> float:
        value = self.feed_item.published_at or self.feed_item.created_at
        if value is None:
            return 0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()


def get_insight_request_preview(
    session: Session,
    *,
    item_id: int | None = None,
) -> AIInsightRequestPreviewRead:
    candidates = _list_preview_candidates(session)
    candidate_samples = [_build_sample(candidate) for candidate in candidates]
    if not candidates:
        return AIInsightRequestPreviewRead(
            available=False,
            can_send=False,
            message="No eligible feed item with a non-disabled AI route is available for preview.",
            candidates=[],
            sample=None,
            payload=None,
        )

    candidate = _select_preview_candidate(candidates, item_id=item_id)
    if candidate is None:
        return AIInsightRequestPreviewRead(
            available=False,
            can_send=False,
            message="Selected item is not available for Insight Engine request preview.",
            candidates=candidate_samples,
            sample=None,
            payload=None,
        )

    sample = _build_sample(candidate)

    try:
        context = load_insight_engine_request_context(
            session=session,
            config=candidate.config,
            feed_item=candidate.feed_item,
            source=candidate.source,
            active_insight_profile_context=build_active_insight_profile_context(session),
        )
        payload = build_insight_engine_request_payload(context).model_dump(mode="json")
    except ValueError as exc:
        return AIInsightRequestPreviewRead(
            available=True,
            can_send=False,
            message=f"Cannot build Insight Engine request preview: {exc}",
            candidates=candidate_samples,
            sample=sample,
            payload=None,
        )

    if item_id is not None:
        prefix = "Preview generated for the selected sample"
    else:
        prefix = "Preview generated for the automatically selected sample"

    if candidate.config.is_configured:
        message = f"{prefix} from current saved AI settings; real generation can send this payload."
    else:
        message = (
            f"{prefix} from current saved AI settings, but real generation would skip this sample because "
            f"the {candidate.config.profile} profile is disabled or incomplete."
        )

    return AIInsightRequestPreviewRead(
        available=True,
        can_send=candidate.config.is_configured,
        message=message,
        candidates=candidate_samples,
        sample=sample,
        payload=payload,
    )


def _list_preview_candidates(session: Session) -> list[_PreviewCandidate]:
    rows = _list_preview_candidate_rows(session)
    if not rows:
        return []

    feed_item_ids = [feed_item.id for feed_item, _item_state, _occurrence, _source in rows]
    latest_artifacts = load_latest_feed_item_insight_artifacts(session, feed_item_ids=feed_item_ids)
    candidates: list[_PreviewCandidate] = []
    for feed_item, _item_state, _occurrence, source in rows:
        config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
        if config is None:
            continue
        existing_kinds = set(latest_artifacts.get(feed_item.id, {}).keys())
        missing_kinds = [kind for kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS if kind not in existing_kinds]
        candidates.append(
            _PreviewCandidate(
                feed_item=feed_item,
                source=source,
                config=config,
                missing_artifact_kinds=missing_kinds,
            )
        )

    return sorted(candidates, key=_candidate_sort_key, reverse=True)


def _select_preview_candidate(
    candidates: list[_PreviewCandidate],
    *,
    item_id: int | None,
) -> _PreviewCandidate | None:
    if item_id is not None:
        return next((candidate for candidate in candidates if candidate.feed_item.id == item_id), None)
    return candidates[0] if candidates else None


def _candidate_sort_key(candidate: _PreviewCandidate) -> tuple[bool, bool, bool, float, int]:
    return (
        candidate.config.is_configured,
        candidate.has_content_text,
        candidate.has_missing_artifacts,
        candidate.sort_timestamp,
        candidate.feed_item.id,
    )


def _build_sample(candidate: _PreviewCandidate) -> AIInsightRequestPreviewSampleRead:
    return AIInsightRequestPreviewSampleRead(
        item_id=candidate.feed_item.id,
        title=candidate.feed_item.title,
        source_id=candidate.source.id,
        source_name=candidate.source.name,
        source_folder=candidate.source.folder_path,
        ai_route=candidate.source.ai_route,
        profile=candidate.config.profile,
        content_text_source=candidate.feed_item.content_text_source,
        content_extraction_status=candidate.feed_item.content_extraction_status,
        missing_artifact_kinds=candidate.missing_artifact_kinds,
    )


def _list_preview_candidate_rows(session: Session):
    latest_occurrences = build_latest_occurrence_subquery()
    latest_item_timestamp = func.coalesce(FeedItem.published_at, FeedItem.created_at)
    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .where(Source.ai_route != AI_ROUTE_DISABLED)
        .order_by(latest_item_timestamp.desc(), FeedItem.id.desc())
        .limit(PREVIEW_CANDIDATE_LIMIT)
    )
    return session.execute(statement).all()
