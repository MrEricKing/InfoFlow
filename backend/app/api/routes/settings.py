from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from datetime import datetime

from app.db.session import get_db_session
from app.schemas.settings import (
    AIConnectionTestRequest,
    AIConnectionTestResponse,
    AIInsightRequestPreviewRead,
    AISettingsRead,
    AISettingsUpdateRequest,
    InsightChannelCreateRequest,
    InsightChannelImpactRead,
    InsightChannelRead,
    InsightChannelRetireRequest,
    InsightChannelUpdateRequest,
    InsightEngineSettingsRead,
    InsightEngineSettingsUpdateRequest,
    InsightEngineCoverageRead,
    InsightEngineAttemptListRead,
    InsightEngineAttemptRead,
    InsightEvidencePackagePreviewRead,
    InsightProfileCandidateInspectRequest,
    InsightProfileCandidateInspectResponse,
    InsightProfileCandidateSourceApplyRequest,
    InsightProfileCandidateSourceApplyResponse,
    InsightProfileDraftInspectRequest,
    InsightProfileDraftInspectResponse,
    InsightProfileActivateRequest,
    InsightProfileSettingsRead,
    InsightProfileValidateRequest,
    InsightProfileValidateResponse,
    InsightProfileVersionCreateRequest,
    RsshubSettingsRead,
    RsshubSettingsUpdateRequest,
    StoragePathsRead,
    StoragePathsUpdateRequest,
    StoragePathsValidateRequest,
)
from app.services.app_settings import (
    get_ai_settings,
    get_insight_engine_settings,
    get_rsshub_settings,
    get_storage_paths,
    resolve_ai_provider_config_for_test,
    update_ai_settings,
    update_insight_engine_settings,
    update_rsshub_settings,
    update_storage_paths,
    validate_storage_paths,
)
from app.services.ai_provider import AIProviderError, test_chat_completion_connection
from app.services.insight_request_preview import get_insight_request_preview
from app.services.insight_channels import (
    InsightChannelConflictError,
    InsightChannelNotFoundError,
    InsightChannelValidationError,
    build_channel_read,
    create_channel,
    ensure_default_channels,
    get_channel_catalog_version,
    update_channel,
)
from app.services.channel_lifecycle import get_channel_retire_impact
from app.services.operations import (
    OperationConflictError,
    build_operation_read,
    create_channel_retire_operation,
)
from app.services.operation_jobs import wake_operation_job_worker
from app.schemas.operations import OperationRead
from app.services.insight_engine_coverage import get_insight_engine_coverage
from app.services.insight_engine_attempts import get_insight_engine_attempt, list_insight_engine_attempts
from app.services.insight_evidence_package import (
    DEFAULT_EVIDENCE_PACKAGE_ARTIFACT_MODE,
    DEFAULT_EVIDENCE_PACKAGE_RANGE,
    build_insight_evidence_package_archive,
    preview_insight_evidence_package,
)
from app.services.insight_profile_candidates import (
    apply_source_profile_candidate_drafts,
    inspect_profile_candidate,
)
from app.services.insight_profiles import (
    InsightProfileNotFoundError,
    activate_insight_profile_version,
    create_insight_profile_version,
    get_insight_profile_settings,
    inspect_insight_profile_draft,
    rollback_insight_profile_version,
    validate_insight_profile_payload,
)
from app.services.homepage_read_model import (
    ensure_homepage_read_model_backfill_started,
    mark_homepage_read_models_stale,
)
from app.services.source_resolver import SourceValidationError

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/storage-paths", response_model=StoragePathsRead)
def get_storage_paths_endpoint(session: Session = Depends(get_db_session)) -> StoragePathsRead:
    return get_storage_paths(session)


@router.patch("/storage-paths", response_model=StoragePathsRead)
def update_storage_paths_endpoint(
    payload: StoragePathsUpdateRequest,
    session: Session = Depends(get_db_session),
) -> StoragePathsRead:
    result = update_storage_paths(session, payload)
    session.commit()
    return result


@router.post("/storage-paths/validate", response_model=StoragePathsRead)
def validate_storage_paths_endpoint(
    payload: StoragePathsValidateRequest,
    session: Session = Depends(get_db_session),
) -> StoragePathsRead:
    return validate_storage_paths(
        session,
        knowledge_base_root=payload.knowledge_base_root,
    )


@router.get("/rsshub", response_model=RsshubSettingsRead)
def get_rsshub_settings_endpoint(session: Session = Depends(get_db_session)) -> RsshubSettingsRead:
    return get_rsshub_settings(session)


@router.patch("/rsshub", response_model=RsshubSettingsRead)
def update_rsshub_settings_endpoint(
    payload: RsshubSettingsUpdateRequest,
    session: Session = Depends(get_db_session),
) -> RsshubSettingsRead:
    try:
        result = update_rsshub_settings(session, payload)
        session.commit()
    except SourceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@router.get("/ai", response_model=AISettingsRead)
def get_ai_settings_endpoint(session: Session = Depends(get_db_session)) -> AISettingsRead:
    try:
        return get_ai_settings(session)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/insight-engine", response_model=InsightEngineSettingsRead)
def get_insight_engine_settings_endpoint(session: Session = Depends(get_db_session)) -> InsightEngineSettingsRead:
    return get_insight_engine_settings(session)


@router.get("/insight-channels", response_model=list[InsightChannelRead])
def list_insight_channels_endpoint(session: Session = Depends(get_db_session)) -> list[InsightChannelRead]:
    channels = ensure_default_channels(session)
    session.commit()
    version = get_channel_catalog_version(session)
    return [build_channel_read(channel, channel_catalog_version=version) for channel in channels]


@router.post("/insight-channels", response_model=InsightChannelRead, status_code=status.HTTP_201_CREATED)
def create_insight_channel_endpoint(
    payload: InsightChannelCreateRequest,
    session: Session = Depends(get_db_session),
) -> InsightChannelRead:
    try:
        channel = create_channel(session, payload)
        mark_homepage_read_models_stale(session, reason="insight_channel_created")
        session.commit()
        ensure_homepage_read_model_backfill_started("insight_channel_created")
    except InsightChannelConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InsightChannelValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    version = get_channel_catalog_version(session)
    return build_channel_read(channel, channel_catalog_version=version)


@router.patch("/insight-channels/{channel_id}", response_model=InsightChannelRead)
def update_insight_channel_endpoint(
    channel_id: str,
    payload: InsightChannelUpdateRequest,
    session: Session = Depends(get_db_session),
) -> InsightChannelRead:
    try:
        channel = update_channel(session, channel_id, payload)
        mark_homepage_read_models_stale(session, reason="insight_channel_updated")
        session.commit()
        ensure_homepage_read_model_backfill_started("insight_channel_updated")
    except InsightChannelNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InsightChannelConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InsightChannelValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    version = get_channel_catalog_version(session)
    return build_channel_read(channel, channel_catalog_version=version)


@router.get("/insight-channels/{channel_id}/impact", response_model=InsightChannelImpactRead)
def get_insight_channel_impact_endpoint(
    channel_id: str,
    session: Session = Depends(get_db_session),
) -> InsightChannelImpactRead:
    try:
        impact = get_channel_retire_impact(session, channel_id)
        session.commit()
    except InsightChannelNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return impact


@router.post("/insight-channels/{channel_id}/retire", response_model=OperationRead, status_code=status.HTTP_202_ACCEPTED)
def retire_insight_channel_endpoint(
    channel_id: str,
    payload: InsightChannelRetireRequest,
    session: Session = Depends(get_db_session),
) -> OperationRead:
    if not payload.confirm_retire:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="confirm_retire must be true")
    try:
        operation = create_channel_retire_operation(
            session,
            channel_id=channel_id,
            expected_channel_catalog_version=payload.expected_channel_catalog_version,
        )
        session.commit()
    except InsightChannelNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except OperationConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Another app operation is already active.",
                "operation": exc.operation.model_dump(mode="json"),
            },
        ) from exc
    except InsightChannelConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InsightChannelValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    wake_operation_job_worker()
    return build_operation_read(session, operation)


@router.patch("/insight-engine", response_model=InsightEngineSettingsRead)
def update_insight_engine_settings_endpoint(
    payload: InsightEngineSettingsUpdateRequest,
    session: Session = Depends(get_db_session),
) -> InsightEngineSettingsRead:
    result = update_insight_engine_settings(session, payload)
    mark_homepage_read_models_stale(session, reason="insight_engine_settings_updated")
    session.commit()
    ensure_homepage_read_model_backfill_started("insight_engine_settings_updated")
    return result


@router.get("/insight-engine/attempts", response_model=InsightEngineAttemptListRead)
def list_insight_engine_attempts_endpoint(
    status_filter: str | None = Query(default=None, alias="status"),
    source_id: int | None = Query(default=None, ge=1),
    profile: str | None = None,
    item_id: int | None = Query(default=None, ge=1),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> InsightEngineAttemptListRead:
    return list_insight_engine_attempts(
        session,
        status=status_filter,
        source_id=source_id,
        profile=profile,
        item_id=item_id,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )


@router.get("/insight-engine/coverage", response_model=InsightEngineCoverageRead)
def get_insight_engine_coverage_endpoint(
    source_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> InsightEngineCoverageRead:
    return get_insight_engine_coverage(session, source_id=source_id, limit=limit, offset=offset)


@router.get("/insight-engine/attempts/{attempt_id}", response_model=InsightEngineAttemptRead)
def get_insight_engine_attempt_endpoint(
    attempt_id: int,
    session: Session = Depends(get_db_session),
) -> InsightEngineAttemptRead:
    attempt = get_insight_engine_attempt(session, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight engine attempt not found")
    return attempt


@router.get("/ai/insight-request-preview", response_model=AIInsightRequestPreviewRead)
def get_ai_insight_request_preview_endpoint(
    item_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> AIInsightRequestPreviewRead:
    return get_insight_request_preview(session, item_id=item_id)


@router.get("/insight-profile", response_model=InsightProfileSettingsRead)
def get_insight_profile_endpoint(session: Session = Depends(get_db_session)) -> InsightProfileSettingsRead:
    return get_insight_profile_settings(session)


@router.get("/insight-evidence-package/preview", response_model=InsightEvidencePackagePreviewRead)
def preview_insight_evidence_package_endpoint(
    range_value: str = Query(default=DEFAULT_EVIDENCE_PACKAGE_RANGE, alias="range"),
    artifact_mode: str = Query(default=DEFAULT_EVIDENCE_PACKAGE_ARTIFACT_MODE),
    start_date: str | None = None,
    end_date: str | None = None,
    session: Session = Depends(get_db_session),
) -> InsightEvidencePackagePreviewRead:
    try:
        return preview_insight_evidence_package(
            session,
            range_value=range_value,
            artifact_mode=artifact_mode,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/insight-evidence-package/export")
def export_insight_evidence_package_endpoint(
    background_tasks: BackgroundTasks,
    range_value: str = Query(default=DEFAULT_EVIDENCE_PACKAGE_RANGE, alias="range"),
    artifact_mode: str = Query(default=DEFAULT_EVIDENCE_PACKAGE_ARTIFACT_MODE),
    start_date: str | None = None,
    end_date: str | None = None,
    session: Session = Depends(get_db_session),
) -> FileResponse:
    try:
        archive = build_insight_evidence_package_archive(
            session,
            range_value=range_value,
            artifact_mode=artifact_mode,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    background_tasks.add_task(archive.path.unlink, missing_ok=True)
    return FileResponse(
        path=archive.path,
        media_type="application/zip",
        filename=archive.filename,
        background=background_tasks,
        headers={
            "Content-Disposition": f'attachment; filename="{archive.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0",
        },
    )


@router.post("/insight-profile/validate", response_model=InsightProfileValidateResponse)
def validate_insight_profile_endpoint(
    payload: InsightProfileValidateRequest,
) -> InsightProfileValidateResponse:
    try:
        return validate_insight_profile_payload(payload.profile_json)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/insight-profile/draft/inspect", response_model=InsightProfileDraftInspectResponse)
def inspect_insight_profile_draft_endpoint(
    payload: InsightProfileDraftInspectRequest,
    session: Session = Depends(get_db_session),
) -> InsightProfileDraftInspectResponse:
    try:
        return inspect_insight_profile_draft(session, profile_json=payload.profile_json)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/insight-profile/candidate/inspect", response_model=InsightProfileCandidateInspectResponse)
def inspect_insight_profile_candidate_endpoint(
    payload: InsightProfileCandidateInspectRequest,
    session: Session = Depends(get_db_session),
) -> InsightProfileCandidateInspectResponse:
    try:
        return inspect_profile_candidate(session, candidate_json=payload.candidate_json)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/insight-profile/candidate/source-drafts", response_model=InsightProfileCandidateSourceApplyResponse)
def apply_insight_profile_candidate_source_drafts_endpoint(
    payload: InsightProfileCandidateSourceApplyRequest,
    session: Session = Depends(get_db_session),
) -> InsightProfileCandidateSourceApplyResponse:
    try:
        result = apply_source_profile_candidate_drafts(
            session,
            candidate_json=payload.candidate_json,
            source_ids=payload.source_ids,
        )
        mark_homepage_read_models_stale(
            session,
            source_ids=payload.source_ids,
            reason="source_profile_candidate_applied",
        )
        session.commit()
        ensure_homepage_read_model_backfill_started("source_profile_candidate_applied")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@router.post("/insight-profile/versions", response_model=InsightProfileSettingsRead)
def create_insight_profile_version_endpoint(
    payload: InsightProfileVersionCreateRequest,
    session: Session = Depends(get_db_session),
) -> InsightProfileSettingsRead:
    try:
        result = create_insight_profile_version(
            session,
            profile_json=payload.profile_json,
            source_type=payload.source_type,
            edit_note=payload.edit_note,
            activate=payload.activate,
        )
        if payload.activate:
            mark_homepage_read_models_stale(session, reason="insight_profile_activated")
        session.commit()
        if payload.activate:
            ensure_homepage_read_model_backfill_started("insight_profile_activated")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@router.post("/insight-profile/versions/{profile_id}/activate", response_model=InsightProfileSettingsRead)
def activate_insight_profile_version_endpoint(
    profile_id: int,
    payload: InsightProfileActivateRequest | None = None,
    session: Session = Depends(get_db_session),
) -> InsightProfileSettingsRead:
    try:
        result = activate_insight_profile_version(
            session,
            profile_id=profile_id,
            note=payload.note if payload is not None else None,
        )
        mark_homepage_read_models_stale(session, reason="insight_profile_activated")
        session.commit()
        ensure_homepage_read_model_backfill_started("insight_profile_activated")
    except InsightProfileNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return result


@router.post("/insight-profile/versions/{profile_id}/rollback", response_model=InsightProfileSettingsRead)
def rollback_insight_profile_version_endpoint(
    profile_id: int,
    payload: InsightProfileActivateRequest | None = None,
    session: Session = Depends(get_db_session),
) -> InsightProfileSettingsRead:
    try:
        result = rollback_insight_profile_version(
            session,
            profile_id=profile_id,
            note=payload.note if payload is not None else None,
        )
        mark_homepage_read_models_stale(session, reason="insight_profile_rollback")
        session.commit()
        ensure_homepage_read_model_backfill_started("insight_profile_rollback")
    except InsightProfileNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return result


@router.patch("/ai", response_model=AISettingsRead)
def update_ai_settings_endpoint(
    payload: AISettingsUpdateRequest,
    session: Session = Depends(get_db_session),
) -> AISettingsRead:
    try:
        result = update_ai_settings(session, payload)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@router.post("/ai/test", response_model=AIConnectionTestResponse)
def test_ai_settings_endpoint(
    payload: AIConnectionTestRequest | None = None,
    session: Session = Depends(get_db_session),
) -> AIConnectionTestResponse:
    profile = payload.profile if payload is not None else "general"
    try:
        config = resolve_ai_provider_config_for_test(
            session,
            profile=profile,
            payload=payload.settings if payload is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        result = test_chat_completion_connection(config)
    except (AIProviderError, ValueError) as exc:
        return AIConnectionTestResponse(
            ok=False,
            message=str(exc),
            profile=config.profile,
            provider=config.provider,
            model=config.model,
            latency_ms=None,
        )
    return AIConnectionTestResponse(
        ok=True,
        message="AI provider connection succeeded",
        profile=config.profile,
        provider=config.provider,
        model=config.model,
        latency_ms=result.latency_ms,
    )
