from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.reader import (
    ReaderBulkMarkSeenRequest,
    ReaderBulkMarkSeenResponse,
    ReaderBehaviorEventContextRequest,
    ReaderCombinedFeedbackUpdateRequest,
    ReaderDetailRead,
    ReaderInsightFeedbackResponse,
    ReaderInsightFeedbackUpdateRequest,
    ReaderItemStatus,
    ReaderListItemRead,
    ReaderStateMutationResponse,
)
from app.services.knowledge_saved_items import save_feed_item, unmark_feed_item_saved
from app.services.operation_jobs import wake_operation_job_worker
from app.services.operations import OperationConflictError, create_feed_saved_capture_operation
from app.services.reader import (
    build_reader_item_state,
    clear_item_state,
    clear_reader_item_auto_low_priority,
    get_reader_item_detail,
    list_reader_items,
    mark_latest_source_items_seen,
    mark_item_opened_original,
    mark_item_state,
    update_reader_item_insight_feedback,
)

router = APIRouter(prefix="/reader", tags=["reader"])


@router.get("/items", response_model=list[ReaderListItemRead])
def list_reader_items_endpoint(
    status: ReaderItemStatus | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_db_session),
) -> list[ReaderListItemRead]:
    return list_reader_items(session, status=status, limit=limit)


@router.post("/items/bulk/mark_seen", response_model=ReaderBulkMarkSeenResponse)
def bulk_mark_source_items_seen_endpoint(
    payload: ReaderBulkMarkSeenRequest,
    session: Session = Depends(get_db_session),
) -> ReaderBulkMarkSeenResponse:
    response = mark_latest_source_items_seen(session, payload.source_id, view_context=payload.view_context)
    if response is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Source not found")

    session.commit()
    return response


@router.get("/items/{item_id}", response_model=ReaderDetailRead)
def get_reader_item_detail_endpoint(item_id: int, session: Session = Depends(get_db_session)) -> ReaderDetailRead:
    item = get_reader_item_detail(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Reader item not found")
    return item


@router.put("/items/{item_id}/insight-feedback", response_model=ReaderInsightFeedbackResponse)
def update_reader_item_insight_feedback_endpoint(
    item_id: int,
    payload: ReaderInsightFeedbackUpdateRequest,
    session: Session = Depends(get_db_session),
) -> ReaderInsightFeedbackResponse:
    try:
        response = update_reader_item_insight_feedback(
            session,
            item_id,
            payload.insight_feedback_text,
            insight_feedback_text_provided="insight_feedback_text" in payload.model_fields_set,
            structured_feedback=payload.structured_feedback,
            structured_feedback_provided="structured_feedback" in payload.model_fields_set,
            view_context=payload.view_context,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if response is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return response


@router.put("/items/{item_id}/feedback", response_model=ReaderDetailRead)
def update_reader_item_feedback_endpoint(
    item_id: int,
    payload: ReaderCombinedFeedbackUpdateRequest,
    session: Session = Depends(get_db_session),
) -> ReaderDetailRead:
    try:
        response = update_reader_item_insight_feedback(
            session,
            item_id,
            payload.insight_feedback_text,
            insight_feedback_text_provided="insight_feedback_text" in payload.model_fields_set,
            structured_feedback=payload.structured_feedback,
            structured_feedback_provided="structured_feedback" in payload.model_fields_set,
            view_context=payload.view_context,
        )
        if response is None:
            session.rollback()
            raise HTTPException(status_code=404, detail="Reader item not found")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session.commit()
    item = get_reader_item_detail(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Reader item not found")
    return item


@router.post("/items/{item_id}/clear_auto_low_priority", response_model=ReaderInsightFeedbackResponse)
def clear_reader_item_auto_low_priority_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderInsightFeedbackResponse:
    response = clear_reader_item_auto_low_priority(session, item_id, view_context=_view_context_from_payload(payload))
    if response is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return response


@router.post("/items/{item_id}/mark_browsed", response_model=ReaderStateMutationResponse)
def mark_item_browsed_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    return _mark_item_state_or_404(session, item_id, "browsed", view_context=_view_context_from_payload(payload))


@router.post("/items/{item_id}/mark_seen", response_model=ReaderStateMutationResponse)
def mark_item_seen_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    return _mark_item_state_or_404(session, item_id, "seen", view_context=_view_context_from_payload(payload))


@router.post("/items/{item_id}/unmark_seen", response_model=ReaderStateMutationResponse)
def unmark_item_seen_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    item_state = clear_item_state(session, item_id, "seen", view_context=_view_context_from_payload(payload))
    if item_state is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return ReaderStateMutationResponse(item_id=item_id, state=build_reader_item_state(item_state))


@router.post("/items/{item_id}/mark_read_full", response_model=ReaderStateMutationResponse)
def mark_item_read_full_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    return _mark_item_state_or_404(session, item_id, "read_full", view_context=_view_context_from_payload(payload))


@router.post("/items/{item_id}/open_original", response_model=ReaderStateMutationResponse)
def mark_item_opened_original_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    item_state = mark_item_opened_original(session, item_id, view_context=_view_context_from_payload(payload))
    if item_state is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return ReaderStateMutationResponse(item_id=item_id, state=build_reader_item_state(item_state))


@router.post("/items/{item_id}/unmark_read_full", response_model=ReaderStateMutationResponse)
def unmark_item_read_full_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    item_state = clear_item_state(session, item_id, "read_full", view_context=_view_context_from_payload(payload))
    if item_state is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return ReaderStateMutationResponse(item_id=item_id, state=build_reader_item_state(item_state))


@router.post("/items/{item_id}/mark_saved", response_model=ReaderStateMutationResponse)
def mark_item_saved_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    result = save_feed_item(session, item_id, view_context=_view_context_from_payload(payload))
    if result is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    if result.pending_capture_run_id is not None:
        try:
            create_feed_saved_capture_operation(
                session,
                saved_item_id=result.saved_item.id,
                capture_run_id=result.pending_capture_run_id,
            )
            session.commit()
            wake_operation_job_worker()
        except OperationConflictError:
            session.rollback()
    return ReaderStateMutationResponse(
        item_id=item_id,
        state=build_reader_item_state(result.item_state),
        saved_item_id=result.saved_item.id,
        has_canonical_snapshot=result.snapshots.has_canonical_snapshot,
        has_original_page_snapshot=result.snapshots.has_original_page_snapshot,
        has_singlefile_html=result.snapshots.has_singlefile_html,
        has_crawl4ai_raw_markdown=result.snapshots.has_crawl4ai_raw_markdown,
        has_crawl4ai_fit_markdown=result.snapshots.has_crawl4ai_fit_markdown,
        has_crawl4ai_fit_image_reinserted_markdown=result.snapshots.has_crawl4ai_fit_image_reinserted_markdown,
        image_asset_count=result.snapshots.image_asset_count,
    )


@router.post("/items/{item_id}/unmark_saved", response_model=ReaderStateMutationResponse)
def unmark_item_saved_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    result = unmark_feed_item_saved(session, item_id, view_context=_view_context_from_payload(payload))
    if result is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    item_state, saved_item_id = result
    session.commit()
    return ReaderStateMutationResponse(
        item_id=item_id,
        state=build_reader_item_state(item_state),
        saved_item_id=saved_item_id,
    )


@router.post("/items/{item_id}/mark_not_interested", response_model=ReaderStateMutationResponse)
def mark_item_not_interested_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    return _mark_item_state_or_404(session, item_id, "not_interested", view_context=_view_context_from_payload(payload))


@router.post("/items/{item_id}/unmark_not_interested", response_model=ReaderStateMutationResponse)
def unmark_item_not_interested_endpoint(
    item_id: int,
    payload: ReaderBehaviorEventContextRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> ReaderStateMutationResponse:
    item_state = clear_item_state(session, item_id, "not_interested", view_context=_view_context_from_payload(payload))
    if item_state is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return ReaderStateMutationResponse(item_id=item_id, state=build_reader_item_state(item_state))


def _mark_item_state_or_404(
    session: Session,
    item_id: int,
    action: str,
    *,
    view_context=None,
) -> ReaderStateMutationResponse:
    item_state = mark_item_state(session, item_id, action, view_context=view_context)
    if item_state is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Reader item not found")

    session.commit()
    return ReaderStateMutationResponse(item_id=item_id, state=build_reader_item_state(item_state))


def _view_context_from_payload(payload: ReaderBehaviorEventContextRequest | None):
    return payload.view_context if payload is not None else None
