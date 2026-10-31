from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.homepage import (
    FeedItemInsightArtifactMissingCandidateRead,
    FeedItemInsightArtifactMissingCandidatesResponse,
)
from app.services.insight_engine_artifact_builder import (
    count_feed_item_ids_missing_feed_item_insight_artifacts,
    list_missing_feed_item_insight_artifact_candidates,
)

router = APIRouter(prefix="/feed-item-insight-artifacts", tags=["feed-item-insight-artifacts"])


@router.get("/missing-candidates", response_model=FeedItemInsightArtifactMissingCandidatesResponse)
def list_missing_feed_item_insight_artifact_candidates_endpoint(
    source_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_db_session),
) -> FeedItemInsightArtifactMissingCandidatesResponse:
    total_count = count_feed_item_ids_missing_feed_item_insight_artifacts(
        session,
        source_id=source_id,
    )
    candidates = list_missing_feed_item_insight_artifact_candidates(
        session,
        source_id=source_id,
        limit=limit,
    )
    return FeedItemInsightArtifactMissingCandidatesResponse(
        scope=f"source:{source_id}" if source_id is not None else "all",
        total_count=total_count,
        has_more=limit is not None and total_count > len(candidates),
        candidates=[
            FeedItemInsightArtifactMissingCandidateRead(
                feed_item_id=candidate.feed_item_id,
                source_id=candidate.source_id,
                ai_route=candidate.ai_route,
                profile=candidate.profile,
                provider=candidate.provider,
            )
            for candidate in candidates
        ],
    )
