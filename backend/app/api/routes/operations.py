from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.routes._upload_parsing import read_multipart_upload_request
from app.db.session import get_db_session
from app.schemas.operations import (
    FeedItemInsightArtifactOperationCreate,
    FeedItemInsightArtifactBatchOperationCreate,
    ManualUrlCaptureOperationCreate,
    OperationRead,
    SavedItemInsightOperationCreate,
    SourceSyncOperationCreate,
)
from app.services.knowledge_errors import CaptureRetryNotAllowedError, UploadMaterialValidationError
from app.services.operations import (
    OperationConflictError,
    OperationNotFoundError,
    OperationStopLockTimeoutError,
    OperationValidationError,
    build_operation_read,
    create_feed_item_insight_artifact_operation,
    create_feed_item_extract_operation,
    create_feed_item_insight_artifact_batch_operation,
    create_local_mirror_export_operation,
    create_manual_url_capture_operation,
    create_retry_capture_operation,
    create_saved_item_manual_import_operation,
    create_saved_item_insight_operation,
    create_source_sync_operation,
    create_uploaded_material_capture_operation,
    get_active_operation,
    get_operation,
    list_active_operations,
    request_stop_operation,
)
from app.services.operation_jobs import wake_operation_job_worker
from app.services.saved_item_insights import SavedItemInsightValidationError
from app.services.source_registry import SourceValidationError

router = APIRouter(prefix="/operations", tags=["operations"])


@router.post("/source-sync", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def start_source_sync_operation_endpoint(
    payload: SourceSyncOperationCreate,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    try:
        operation = create_source_sync_operation(session, payload)
        session.commit()
    except OperationConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Another app operation is already active.",
                "operation": exc.operation.model_dump(mode="json"),
            },
        ) from exc
    except OperationValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post("/feed-item-insight-artifacts", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def start_feed_item_insight_artifact_batch_operation_endpoint(
    payload: FeedItemInsightArtifactBatchOperationCreate,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    try:
        operation = create_feed_item_insight_artifact_batch_operation(session, payload)
        session.commit()
    except OperationConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Another app operation is already active.",
                "operation": exc.operation.model_dump(mode="json"),
            },
        ) from exc
    except OperationValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.get("/active", response_model=OperationRead | None)
def get_active_operation_endpoint(session: Session = Depends(get_db_session)) -> OperationRead | None:
    operation = get_active_operation(session)
    if operation is None:
        return None
    return build_operation_read(session, operation)


@router.get("/active-list", response_model=list[OperationRead])
def list_active_operations_endpoint(session: Session = Depends(get_db_session)) -> list[OperationRead]:
    return [build_operation_read(session, operation) for operation in list_active_operations(session)]


@router.post("/captures/manual-url", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def start_manual_url_capture_operation_endpoint(
    payload: ManualUrlCaptureOperationCreate,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    operation = _create_operation_response(
        session,
        lambda: create_manual_url_capture_operation(session, payload),
    )
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post("/captures/upload", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
async def start_uploaded_capture_operation_endpoint(
    request: Request,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    try:
        upload = await read_multipart_upload_request(request, purpose="Operation upload")
    except UploadMaterialValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    operation = _create_operation_response(
        session,
        lambda: create_uploaded_material_capture_operation(
            session,
            file_bytes=upload["file_bytes"],
            filename=upload["filename"],
            content_type=upload["content_type"],
            material_type=upload["material_type"],
            title=upload["title"],
            user_note=upload["user_note"],
            ai_route=upload["ai_route"],
        ),
    )
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post("/saved-items/{saved_item_id}/capture-runs", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def start_retry_capture_operation_endpoint(
    saved_item_id: int,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    operation = _create_operation_response(session, lambda: create_retry_capture_operation(session, saved_item_id))
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post(
    "/saved-items/{saved_item_id}/local-mirror-exports",
    response_model=OperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_local_mirror_export_operation_endpoint(
    saved_item_id: int,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    operation = _create_operation_response(session, lambda: create_local_mirror_export_operation(session, saved_item_id))
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post(
    "/saved-items/{saved_item_id}/capture-import",
    response_model=OperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_saved_item_manual_import_operation_endpoint(
    saved_item_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    try:
        upload = await read_multipart_upload_request(request, purpose="Saved item manual import")
    except UploadMaterialValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    operation = _create_operation_response(
        session,
        lambda: create_saved_item_manual_import_operation(
            session,
            saved_item_id,
            file_bytes=upload["file_bytes"],
            filename=upload["filename"],
            content_type=upload["content_type"],
            material_type=upload["material_type"],
            title=upload["title"],
            user_note=upload["user_note"],
        ),
    )
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post(
    "/saved-items/{saved_item_id}/insight-rebuild",
    response_model=OperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_saved_item_insight_operation_endpoint(
    saved_item_id: int,
    payload: SavedItemInsightOperationCreate | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> OperationRead:
    operation = _create_operation_response(
        session,
        lambda: create_saved_item_insight_operation(session, saved_item_id, payload),
    )
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post("/feed-items/{feed_item_id}/extract-content", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def start_feed_item_extract_operation_endpoint(
    feed_item_id: int,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    operation = _create_operation_response(session, lambda: create_feed_item_extract_operation(session, feed_item_id))
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.post("/feed-items/{feed_item_id}/insight-artifacts", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def start_feed_item_insight_artifact_operation_endpoint(
    feed_item_id: int,
    payload: FeedItemInsightArtifactOperationCreate,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    operation = _create_operation_response(
        session,
        lambda: create_feed_item_insight_artifact_operation(session, feed_item_id, payload),
    )
    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.get("/{operation_id}", response_model=OperationRead)
def get_operation_endpoint(operation_id: int, session: Session = Depends(get_db_session)) -> OperationRead:
    operation = get_operation(session, operation_id)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")
    return build_operation_read(session, operation)


@router.post("/{operation_id}/stop", response_model=OperationRead)
def stop_operation_endpoint(operation_id: int, session: Session = Depends(get_db_session)) -> OperationRead:
    try:
        operation = request_stop_operation(session, operation_id)
        session.commit()
    except OperationNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found") from exc
    except OperationValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OperationStopLockTimeoutError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    wake_operation_job_worker()
    return build_operation_read(session, operation)


def _create_operation_response(session: Session, factory):
    try:
        operation = factory()
        session.commit()
    except OperationConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Another app operation is already active for this target.",
                "operation": exc.operation.model_dump(mode="json"),
            },
        ) from exc
    except OperationValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        CaptureRetryNotAllowedError,
        UploadMaterialValidationError,
        SourceValidationError,
        SavedItemInsightValidationError,
    ) as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return operation
