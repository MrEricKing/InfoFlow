from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import socket
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.operation_logging import log_operation_event
from app.core.config import get_settings
from app.core.security import PayloadValidationError, sanitized_operation_payload, validate_operation_payload
from app.db.models import AppOperation, FeedItem, InsightEngineArtifact, InsightEngineAttempt, ItemState, SavedItem, Source
from app.db.session import get_session_factory
from app.schemas.homepage import FeedItemInsightArtifactBuildResult
from app.schemas.operations import (
    FeedItemInsightArtifactOperationCreate,
    FeedItemInsightArtifactBatchOperationCreate,
    ManualUrlCaptureOperationCreate,
    OperationProgressRead,
    OperationRead,
    SavedItemInsightOperationCreate,
    SourceSyncOperationCreate,
)
from app.schemas.source import BulkSourceSyncResponse
from app.services.insight_engine_artifact_builder import list_missing_feed_item_insight_artifact_candidates
from app.services.insight_engine_artifacts import FEED_ITEM_INSIGHT_ARTIFACT_KINDS
from app.services.insight_engine_coverage import list_retryable_insight_engine_coverage_candidates
from app.services.homepage import invalidate_homepage_snapshots
from app.services.knowledge_constants import UPLOAD_ORIGIN_TYPES
from app.services.knowledge_errors import CaptureRetryNotAllowedError
from app.services.knowledge_capture import (
    finalize_interrupted_saved_item_capture_run,
    prepare_manual_url_capture,
)
from app.services.knowledge_uploads import prepare_saved_item_manual_import, prepare_uploaded_material_capture
from app.services.operation_jobs import (
    enqueue_channel_reroute_item_job,
    enqueue_feed_item_insight_artifact_job,
    enqueue_feed_item_extract_job,
    enqueue_feed_item_insight_artifact_build_job,
    enqueue_knowledge_capture_job,
    enqueue_local_mirror_export_job,
    enqueue_saved_item_insight_job,
    enqueue_source_sync_job,
    operation_uses_jobs,
    run_operation_jobs_for_operation,
    sync_job_backed_operation_state,
)
from app.services.channel_lifecycle import prepare_channel_retire

logger = logging.getLogger(__name__)
OPERATION_TYPE_SOURCE_SYNC = "source_sync"
OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS = "feed_item_insight_artifacts"
OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT = "feed_item_insight_artifact"
OPERATION_TYPE_KNOWLEDGE_CAPTURE = "knowledge_capture"
OPERATION_TYPE_LOCAL_MIRROR_EXPORT = "local_mirror_export"
OPERATION_TYPE_FEED_ITEM_EXTRACT = "feed_item_extract"
OPERATION_TYPE_SAVED_ITEM_INSIGHT = "saved_item_insight"
OPERATION_TYPE_CHANNEL_LIFECYCLE = "channel_lifecycle"
FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES = {OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS}
FEED_ITEM_INSIGHT_ARTIFACT_OPERATION_TYPES = {OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT}
GLOBAL_OPERATION_TYPES = {
    OPERATION_TYPE_SOURCE_SYNC,
    OPERATION_TYPE_CHANNEL_LIFECYCLE,
    *FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES,
}
SAVED_ITEM_MIRROR_MUTEX_OPERATION_TYPES = {
    OPERATION_TYPE_KNOWLEDGE_CAPTURE,
    OPERATION_TYPE_LOCAL_MIRROR_EXPORT,
    OPERATION_TYPE_SAVED_ITEM_INSIGHT,
}

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}
TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_STOPPED, STATUS_FAILED, STATUS_INTERRUPTED}

RUNNER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class _FeedItemInsightArtifactOperationCandidate:
    feed_item_id: int
    source_id: int | None


class OperationConflictError(RuntimeError):
    def __init__(self, operation: OperationRead) -> None:
        self.operation = operation
        super().__init__("Another app operation is already active.")


class OperationNotFoundError(RuntimeError):
    pass


class OperationValidationError(ValueError):
    pass


class OperationStopLockTimeoutError(RuntimeError):
    pass


def create_source_sync_operation(session: Session, payload: SourceSyncOperationCreate) -> AppOperation:
    _interrupt_stale_active_operations(session)
    _acquire_operation_mutex_lock(session, "global")
    active = _get_active_global_operation_row(session)
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))

    source = _load_source_for_operation(session, payload.source_id) if payload.source_id is not None else None
    sources = _resolve_sync_sources(
        session,
        kind=payload.kind,
        source_id=source.id if source is not None else None,
        source_ids=payload.source_ids,
    )
    source_count = len(sources)
    now = _now_utc() if source_count == 0 else None
    progress = _initial_progress(source_count)
    result = None
    message = _build_source_sync_start_message(payload.kind, source)
    if source_count == 0:
        progress["stage"] = "completed"
        progress["completed"] = 0
        result = BulkSourceSyncResponse(source_count=0, success_count=0, error_count=0, results=[]).model_dump(mode="json")
        message = "没有启用的来源可刷新。"
    operation = AppOperation(
        operation_type=OPERATION_TYPE_SOURCE_SYNC,
        operation_kind=payload.kind,
        status=STATUS_COMPLETED if source_count == 0 else STATUS_QUEUED,
        source_id=source.id if source is not None else None,
        scope_json=_operation_scope({
            "kind": payload.kind,
            "source_id": source.id if source is not None else None,
            "source_ids": payload.source_ids,
            "feed_item_insight_artifact_build_limit": payload.artifact_build_limit,
        }),
        progress_json=progress,
        result_json=result,
        message=message,
        runner_id=RUNNER_ID,
    )
    if now is not None:
        operation.completed_at = now
        operation.updated_at = now
    session.add(operation)
    session.flush()
    for index, sync_source in enumerate(sources, start=1):
        enqueue_source_sync_job(
            session,
            operation,
            source_id=sync_source.id,
            source_index=index,
            source_count=source_count,
        )
    session.refresh(operation)
    log_operation_event(logger, "operation_created", operation=operation)
    return operation


def create_feed_item_insight_artifact_batch_operation(
    session: Session,
    payload: FeedItemInsightArtifactBatchOperationCreate,
    *,
    allow_active_global_conflict: bool = False,
) -> AppOperation:
    _interrupt_stale_active_operations(session)
    if not allow_active_global_conflict:
        _acquire_operation_mutex_lock(session, "global")
        active = _get_active_global_operation_row(session)
        if active is not None:
            raise OperationConflictError(build_operation_read(session, active))

    source = _load_source_for_operation(session, payload.source_id) if payload.source_id is not None else None
    label = _feed_item_insight_batch_label(payload, source)
    if payload.scope == "failed_attempts":
        candidates = _list_failed_attempt_feed_item_insight_artifact_candidates(session, payload=payload)
        stats_scope = f"failed_attempts:source:{source.id}" if source is not None else "failed_attempts"
    elif payload.scope == "retryable_coverage":
        candidates = [
            _FeedItemInsightArtifactOperationCandidate(
                feed_item_id=candidate.feed_item_id,
                source_id=candidate.source_id,
            )
            for candidate in list_retryable_insight_engine_coverage_candidates(
                session,
                source_id=source.id if source is not None else None,
                limit=payload.limit,
                reason_codes=payload.reason_codes,
            )
        ]
        stats_scope = f"retryable_coverage:source:{source.id}" if source is not None else "retryable_coverage"
    else:
        candidates = list_missing_feed_item_insight_artifact_candidates(
            session,
            source_id=source.id if source is not None else None,
            limit=payload.limit,
        )
        stats_scope = f"source:{source.id}" if source is not None else "all"
    operation = AppOperation(
        operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
        operation_kind=payload.scope,
        status=STATUS_QUEUED if candidates else STATUS_COMPLETED,
        source_id=source.id if source is not None else None,
        scope_json=_operation_scope({
            "scope": payload.scope,
            "source_id": source.id if source is not None else None,
            "limit": payload.limit,
            "profile": payload.profile,
            "created_after": payload.created_after.isoformat() if payload.created_after is not None else None,
            "reason_codes": payload.reason_codes,
        }),
        progress_json=_initial_insight_progress(f"Feed item insight artifacts: {label}", len(candidates)),
        result_json=_feed_item_insight_artifact_result(scope=stats_scope),
        message=(
            f"Feed item insight artifact operation queued: {label}"
            if candidates
            else _feed_item_insight_batch_empty_message(payload, source)
        ),
        runner_id=RUNNER_ID,
    )
    session.add(operation)
    session.flush()
    if candidates:
        for candidate in candidates:
            enqueue_feed_item_insight_artifact_build_job(
                session,
                operation,
                feed_item_id=candidate.feed_item_id,
                source_id=candidate.source_id,
            )
    else:
        progress = _progress(operation)
        progress["stage"] = "completed"
        progress["completed"] = 1
        progress["success_count"] = 1
        operation.progress_json = progress
        operation.completed_at = _now_utc()
        operation.updated_at = operation.completed_at
        session.add(operation)
        session.flush()
    session.refresh(operation)
    log_operation_event(logger, "operation_created", operation=operation)
    return operation


def _list_failed_attempt_feed_item_insight_artifact_candidates(
    session: Session,
    *,
    payload: FeedItemInsightArtifactBatchOperationCreate,
) -> list[_FeedItemInsightArtifactOperationCandidate]:
    if payload.limit == 0:
        return []

    conditions = [
        InsightEngineAttempt.status == "failed",
        InsightEngineAttempt.feed_item_id.is_not(None),
    ]
    if payload.source_id is not None:
        conditions.append(InsightEngineAttempt.source_id == payload.source_id)
    if payload.profile:
        conditions.append(InsightEngineAttempt.profile == payload.profile)
    if payload.created_after is not None:
        conditions.append(InsightEngineAttempt.created_at >= _normalize_datetime_for_db(payload.created_after))

    ranked_attempts = (
        select(
            InsightEngineAttempt.feed_item_id.label("feed_item_id"),
            InsightEngineAttempt.source_id.label("source_id"),
            InsightEngineAttempt.created_at.label("attempt_created_at"),
            InsightEngineAttempt.id.label("attempt_id"),
            func.row_number()
            .over(
                partition_by=InsightEngineAttempt.feed_item_id,
                order_by=(InsightEngineAttempt.created_at.desc(), InsightEngineAttempt.id.desc()),
            )
            .label("attempt_rank"),
        )
        .where(*conditions)
        .subquery()
    )
    missing_artifact_conditions = [
        ~exists().where(
            and_(
                InsightEngineArtifact.feed_item_id == ranked_attempts.c.feed_item_id,
                InsightEngineArtifact.artifact_kind == artifact_kind,
            )
        )
        for artifact_kind in FEED_ITEM_INSIGHT_ARTIFACT_KINDS
    ]
    statement = (
        select(ranked_attempts.c.feed_item_id, ranked_attempts.c.source_id)
        .join(FeedItem, FeedItem.id == ranked_attempts.c.feed_item_id)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .where(ranked_attempts.c.attempt_rank == 1)
        .where(or_(*missing_artifact_conditions))
        .order_by(ranked_attempts.c.attempt_created_at.desc(), ranked_attempts.c.attempt_id.desc())
    )
    if payload.limit is not None:
        statement = statement.limit(payload.limit)
    rows = session.execute(statement).all()

    candidates: list[_FeedItemInsightArtifactOperationCandidate] = []
    for feed_item_id, source_id in rows:
        if feed_item_id is None:
            continue
        candidates.append(
            _FeedItemInsightArtifactOperationCandidate(
                feed_item_id=feed_item_id,
                source_id=source_id,
            )
        )
    return candidates


def _feed_item_insight_batch_label(payload: FeedItemInsightArtifactBatchOperationCreate, source: Source | None) -> str:
    if payload.scope == "failed_attempts":
        scope_label = source.name if source is not None else "all sources"
        profile_label = f", profile {payload.profile}" if payload.profile else ""
        return f"failed attempts for {scope_label}{profile_label}"
    if payload.scope == "retryable_coverage":
        scope_label = source.name if source is not None else "all sources"
        reason_label = f", reasons {','.join(payload.reason_codes)}" if payload.reason_codes else ""
        return f"retryable coverage for {scope_label}{reason_label}"
    return source.name if source is not None else "all sources"


def _feed_item_insight_batch_empty_message(
    payload: FeedItemInsightArtifactBatchOperationCreate,
    source: Source | None,
) -> str:
    if payload.scope == "failed_attempts":
        return (
            "No current failed Insight Engine items are missing artifacts for selected source. Historical failed attempts may still exist."
            if source is not None
            else "No current failed Insight Engine items are missing artifacts. Historical failed attempts may still exist."
        )
    if payload.scope == "retryable_coverage":
        return (
            "No retryable incomplete Insight Engine artifacts for selected source."
            if source is not None
            else "No retryable incomplete Insight Engine artifacts."
        )
    return "No missing feed item insight artifacts for selected source." if source is not None else "No missing feed item insight artifacts."


def _normalize_datetime_for_db(value: datetime) -> datetime:
    if value.tzinfo is not None and get_settings().database_url.startswith("sqlite"):
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def create_manual_url_capture_operation(session: Session, payload: ManualUrlCaptureOperationCreate) -> AppOperation:
    _interrupt_stale_active_operations(session)
    detail, capture_run_id, mirror_export_capture_run_id = prepare_manual_url_capture(
        session,
        url=payload.url,
        title=payload.title,
        user_note=payload.user_note,
        ai_route=payload.ai_route,
    )
    _acquire_saved_item_mutex_lock(session, detail.id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=detail.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
        operation_kind="manual_url",
        scope={
            "saved_item_id": detail.id,
            "capture_run_id": capture_run_id,
            "mirror_export_capture_run_id": mirror_export_capture_run_id,
        },
        message="Manual capture operation queued.",
        total=2,
    )
    enqueue_knowledge_capture_job(
        session,
        operation,
        saved_item_id=detail.id,
        capture_mode="manual_url",
        capture_run_id=capture_run_id,
    )
    return operation


def create_uploaded_material_capture_operation(
    session: Session,
    *,
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None,
    material_type: str,
    title: str | None,
    user_note: str | None,
    ai_route: str | None,
) -> AppOperation:
    _interrupt_stale_active_operations(session)
    detail = prepare_uploaded_material_capture(
        session,
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
        material_type=material_type,
        title=title,
        user_note=user_note,
        ai_route=ai_route,
    )
    _acquire_saved_item_mutex_lock(session, detail.id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=detail.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
        operation_kind="uploaded_material",
        scope={"saved_item_id": detail.id, "material_type": material_type, "filename": filename},
        message="Uploaded material capture operation queued.",
        total=2,
    )
    enqueue_knowledge_capture_job(
        session,
        operation,
        saved_item_id=detail.id,
        capture_mode="uploaded_material",
    )
    return operation


def create_saved_item_manual_import_operation(
    session: Session,
    saved_item_id: int,
    *,
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None,
    material_type: str,
    title: str | None,
    user_note: str | None,
) -> AppOperation:
    _interrupt_stale_active_operations(session)
    saved_item = _load_saved_item_for_operation(session, saved_item_id)
    _acquire_saved_item_mutex_lock(session, saved_item.id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=saved_item.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    prepared_import = prepare_saved_item_manual_import(
        session,
        saved_item.id,
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
        material_type=material_type,
        title=title,
        user_note=user_note,
    )
    if prepared_import is None:
        raise OperationValidationError("Saved item not found")
    detail, source_asset_id = prepared_import
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
        operation_kind="manual_import",
        scope={
            "saved_item_id": detail.id,
            "source_asset_id": source_asset_id,
            "material_type": material_type,
            "filename": filename,
        },
        message="Manual import operation queued.",
        total=2,
    )
    enqueue_knowledge_capture_job(
        session,
        operation,
        saved_item_id=detail.id,
        capture_mode="manual_import",
        source_asset_id=source_asset_id,
    )
    return operation


def create_feed_saved_capture_operation(session: Session, *, saved_item_id: int, capture_run_id: int) -> AppOperation:
    _interrupt_stale_active_operations(session)
    _acquire_saved_item_mutex_lock(session, saved_item_id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=saved_item_id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
        operation_kind="feed_saved_capture",
        scope={"saved_item_id": saved_item_id, "capture_run_id": capture_run_id},
        message="Feed-saved capture operation queued.",
        total=2,
    )
    enqueue_knowledge_capture_job(
        session,
        operation,
        saved_item_id=saved_item_id,
        capture_mode="feed_saved_capture",
        capture_run_id=capture_run_id,
    )
    return operation


def create_retry_capture_operation(session: Session, saved_item_id: int) -> AppOperation:
    _interrupt_stale_active_operations(session)
    saved_item = _load_saved_item_for_operation(session, saved_item_id)
    if saved_item.origin_type in UPLOAD_ORIGIN_TYPES:
        raise CaptureRetryNotAllowedError("Uploaded materials do not use the web capture pipeline")
    _acquire_saved_item_mutex_lock(session, saved_item.id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=saved_item.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
        operation_kind="retry_capture",
        scope={"saved_item_id": saved_item.id},
        message="Saved item capture operation queued.",
        total=2,
    )
    enqueue_knowledge_capture_job(
        session,
        operation,
        saved_item_id=saved_item.id,
        capture_mode="retry_capture",
    )
    return operation


def create_local_mirror_export_operation(session: Session, saved_item_id: int) -> AppOperation:
    _interrupt_stale_active_operations(session)
    saved_item = _load_saved_item_for_operation(session, saved_item_id)
    _acquire_saved_item_mutex_lock(session, saved_item.id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=saved_item.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_LOCAL_MIRROR_EXPORT,
        operation_kind="manual_reexport",
        scope={"saved_item_id": saved_item.id},
        message="Local mirror export operation queued.",
        total=1,
    )
    enqueue_local_mirror_export_job(session, operation, saved_item_id=saved_item.id)
    return operation


def create_feed_item_extract_operation(session: Session, feed_item_id: int) -> AppOperation:
    _interrupt_stale_active_operations(session)
    _acquire_operation_mutex_lock(session, f"{OPERATION_TYPE_FEED_ITEM_EXTRACT}:feed_item:{int(feed_item_id)}")
    feed_item = _load_feed_item_for_operation(session, feed_item_id)
    active = _get_active_target_operation_row(
        session,
        OPERATION_TYPE_FEED_ITEM_EXTRACT,
        target_key="feed_item_id",
        target_id=feed_item.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_FEED_ITEM_EXTRACT,
        operation_kind="manual_extract",
        scope={"feed_item_id": feed_item.id},
        message=f"Feed item operation queued: {feed_item.title}",
        total=1,
    )
    enqueue_feed_item_extract_job(session, operation, feed_item_id=feed_item.id)
    return operation


def create_feed_item_insight_artifact_operation(
    session: Session,
    feed_item_id: int,
    payload: FeedItemInsightArtifactOperationCreate,
    *,
    allow_active_target_conflict: bool = False,
) -> AppOperation:
    _interrupt_stale_active_operations(session)
    if not allow_active_target_conflict:
        _acquire_operation_mutex_lock(session, f"{OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT}:feed_item:{int(feed_item_id)}")
    feed_item = _load_feed_item_for_operation(session, feed_item_id)
    if not allow_active_target_conflict:
        active = _get_active_target_operation_row(
            session,
            FEED_ITEM_INSIGHT_ARTIFACT_OPERATION_TYPES,
            target_key="feed_item_id",
            target_id=feed_item.id,
        )
        if active is not None:
            raise OperationConflictError(build_operation_read(session, active))
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
        operation_kind=payload.mode,
        scope={"feed_item_id": feed_item.id, "mode": payload.mode},
        message=f"Feed item operation queued: {feed_item.title}",
        total=1,
    )
    enqueue_feed_item_insight_artifact_job(session, operation, feed_item_id=feed_item.id, mode=payload.mode)
    return operation


def create_saved_item_insight_operation(
    session: Session,
    saved_item_id: int,
    payload: SavedItemInsightOperationCreate | None,
) -> AppOperation:
    _interrupt_stale_active_operations(session)
    saved_item = _load_saved_item_for_operation(session, saved_item_id)
    _acquire_saved_item_mutex_lock(session, saved_item.id)
    active = _get_active_saved_item_mutex_operation_row(
        session,
        saved_item_id=saved_item.id,
    )
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))
    ai_route = payload.ai_route if payload is not None else None
    operation = _create_operation(
        session,
        operation_type=OPERATION_TYPE_SAVED_ITEM_INSIGHT,
        operation_kind="rebuild",
        scope={"saved_item_id": saved_item.id, "ai_route": ai_route},
        message="Saved item insight operation queued.",
        total=1,
    )
    enqueue_saved_item_insight_job(session, operation, saved_item_id=saved_item.id, ai_route=ai_route)
    return operation


def create_channel_retire_operation(
    session: Session,
    *,
    channel_id: str,
    expected_channel_catalog_version: int,
) -> AppOperation:
    _interrupt_stale_active_operations(session)
    _acquire_operation_mutex_lock(session, "global")
    active = _get_active_global_operation_row(session)
    if active is not None:
        raise OperationConflictError(build_operation_read(session, active))

    plan = prepare_channel_retire(
        session,
        channel_id=channel_id,
        expected_channel_catalog_version=expected_channel_catalog_version,
    )
    from app.services.homepage_read_model import mark_homepage_read_models_stale

    mark_homepage_read_models_stale(
        session,
        reason="channel_retired",
    )

    operation = AppOperation(
        operation_type=OPERATION_TYPE_CHANNEL_LIFECYCLE,
        operation_kind="retire_channel",
        status=STATUS_QUEUED,
        source_id=None,
        scope_json=_operation_scope(
            {
                "channel_id": plan.channel_id,
                "fallback_channel_id": plan.fallback_channel_id,
                "channel_catalog_version": plan.channel_catalog_version,
                "affected_feed_item_count": len(plan.affected_feed_item_ids),
            }
        ),
        progress_json=_initial_insight_progress(
            f"Channel lifecycle reroute: {plan.channel_label}",
            len(plan.affected_feed_item_ids),
        ),
        result_json={
            "channel_id": plan.channel_id,
            "fallback_channel_id": plan.fallback_channel_id,
            "channel_catalog_version": plan.channel_catalog_version,
            "affected_feed_item_count": len(plan.affected_feed_item_ids),
            "artifact_rows_created": 0,
            "skipped_feed_item_count": 0,
        },
        message=f"Channel retire operation queued: {plan.channel_label}",
        runner_id=RUNNER_ID,
    )
    session.add(operation)
    session.flush()
    for feed_item_id in plan.affected_feed_item_ids:
        enqueue_channel_reroute_item_job(
            session,
            operation,
            feed_item_id=feed_item_id,
            retired_channel_id=plan.channel_id,
            fallback_channel_id=plan.fallback_channel_id,
            channel_catalog_version=plan.channel_catalog_version,
            reason="channel_retired",
        )
    if not plan.affected_feed_item_ids:
        progress = _progress(operation)
        progress["stage"] = "completed"
        progress["completed"] = 1
        progress["success_count"] = 1
        operation.progress_json = progress
        _finish_operation(
            session,
            operation,
            STATUS_COMPLETED,
            message=f"Channel retired without affected feed items: {plan.channel_label}",
        )
    session.refresh(operation)
    log_operation_event(logger, "operation_created", operation=operation)
    return operation


def get_active_operation(session: Session) -> AppOperation | None:
    _interrupt_stale_active_operations(session)
    session.commit()
    return _get_active_global_operation_row(session)


def list_active_operations(session: Session) -> list[AppOperation]:
    _interrupt_stale_active_operations(session)
    session.commit()
    return list(
        session.scalars(
            select(AppOperation)
            .where(AppOperation.status.in_(ACTIVE_STATUSES))
            .order_by(AppOperation.created_at.asc(), AppOperation.id.asc())
        )
    )


def get_operation(session: Session, operation_id: int) -> AppOperation | None:
    return session.get(AppOperation, operation_id)


def request_stop_operation(session: Session, operation_id: int) -> AppOperation:
    try:
        _set_fast_stop_lock_timeout(session)
        operation = session.get(AppOperation, operation_id)
        if operation is None:
            raise OperationNotFoundError(f"Operation {operation_id} was not found.")
        if operation.operation_type == OPERATION_TYPE_CHANNEL_LIFECYCLE and operation.status in ACTIVE_STATUSES:
            raise OperationValidationError("Channel lifecycle operations cannot be stopped after retirement starts.")
        if operation.status in ACTIVE_STATUSES:
            operation.stop_requested = True
            if operation.status in {STATUS_QUEUED, STATUS_RUNNING}:
                operation.status = STATUS_STOPPING
            operation.message = "Stop requested; waiting for the current step to finish."
            operation.updated_at = _now_utc()
            progress = _progress(operation)
            progress["stop_requested"] = True
            if progress.get("stage") not in {"stopped", "completed"}:
                progress["stage"] = "stopping"
            operation.progress_json = progress
            session.add(operation)
            session.flush()
            log_operation_event(logger, "operation_stop_requested", operation=operation)
            session.refresh(operation)
    except OperationalError as exc:
        if _is_lock_timeout_error(exc):
            raise OperationStopLockTimeoutError(
                "Operation stop could not acquire the operation row quickly; retry after the current write finishes."
            ) from exc
        raise
    return operation


def _set_fast_stop_lock_timeout(session: Session) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(text("SET LOCAL lock_timeout = '2s'"))
    session.execute(text("SET LOCAL statement_timeout = '5s'"))


def _is_lock_timeout_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in {"55P03", "57014"}:
        return True
    message = str(exc).lower()
    return "lock timeout" in message or "statement timeout" in message


def build_operation_read(session: Session, operation: AppOperation) -> OperationRead:
    source_name = None
    if operation.source_id is not None:
        source = session.get(Source, operation.source_id)
        source_name = source.name if source is not None else None
    progress = None
    if operation.progress_json is not None:
        progress = OperationProgressRead.model_validate(operation.progress_json)
    return OperationRead(
        id=operation.id,
        operation_type=operation.operation_type,
        operation_kind=operation.operation_kind,
        status=operation.status,
        source_id=operation.source_id,
        source_name=source_name,
        scope=_operation_scope_for_read(operation.scope_json),
        progress=progress,
        result=operation.result_json,
        message=operation.message,
        error_message=operation.error_message,
        stop_requested=operation.stop_requested,
        runner_id=operation.runner_id,
        created_at=operation.created_at,
        started_at=operation.started_at,
        updated_at=operation.updated_at,
        completed_at=operation.completed_at,
    )


def run_operation_background(
    operation_id: int,
    *,
    max_drain_seconds: float | None = 5.0,
    failure_backoff_seconds: int | None = None,
) -> None:
    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        if operation is None or operation.status not in ACTIVE_STATUSES:
            return
        log_operation_event(logger, "operation_runner_started", operation=operation)
        if operation_uses_jobs(session, operation):
            session.commit()
            run_operation_jobs_for_operation(
                operation_id,
                max_drain_seconds=_operation_job_drain_timeout(operation, max_drain_seconds),
                failure_backoff_seconds=failure_backoff_seconds,
                max_concurrent_jobs=_operation_job_concurrency(operation),
            )
            return
        if operation.runner_id != RUNNER_ID:
            log_operation_event(logger, "operation_runner_interrupted_before_start", operation=operation)
            _finish_operation(
                session,
                operation,
                STATUS_INTERRUPTED,
                message=None,
                error_message="Operation runner changed before this task started.",
            )
            session.commit()
            return

        _finish_operation(
            session,
            operation,
            STATUS_FAILED,
            message=None,
            error_message=f"Operation type {operation.operation_type} is not registered as a job-backed operation.",
        )
        session.commit()


def _operation_job_concurrency(operation: AppOperation) -> int:
    if operation.operation_type == OPERATION_TYPE_CHANNEL_LIFECYCLE:
        if get_settings().database_url.startswith("sqlite"):
            return 1
        return 2
    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES:
        if get_settings().database_url.startswith("sqlite"):
            return 1
        return 32
    if operation.operation_type != OPERATION_TYPE_SOURCE_SYNC:
        return 1
    if get_settings().database_url.startswith("sqlite"):
        return 1
    return 4


def _operation_job_drain_timeout(operation: AppOperation, requested_timeout: float | None) -> float | None:
    if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
        return None
    return requested_timeout


def _create_operation(
    session: Session,
    *,
    operation_type: str | set[str] | frozenset[str],
    operation_kind: str,
    scope: dict[str, Any],
    message: str,
    total: int,
) -> AppOperation:
    operation = AppOperation(
        operation_type=operation_type,
        operation_kind=operation_kind,
        status=STATUS_QUEUED,
        source_id=None,
        scope_json=_operation_scope(scope),
        progress_json=_initial_progress(total),
        result_json=None,
        message=message,
        runner_id=RUNNER_ID,
    )
    session.add(operation)
    session.flush()
    session.refresh(operation)
    log_operation_event(logger, "operation_created", operation=operation)
    return operation


def _knowledge_capture_complete_message(operation_kind: str) -> str:
    if operation_kind == "manual_url":
        return "Manual capture operation completed."
    if operation_kind == "uploaded_material":
        return "Uploaded material capture operation completed."
    if operation_kind == "manual_import":
        return "Manual import operation completed."
    if operation_kind == "retry_capture":
        return "Saved item capture operation completed."
    if operation_kind == "feed_saved_capture":
        return "Feed-saved capture operation completed."
    return "Capture operation completed."


def _operation_scope(scope: dict[str, Any]) -> dict[str, Any]:
    scope = dict(scope)
    scope.setdefault("correlation_id", f"operation:{uuid4().hex}")
    try:
        validated = validate_operation_payload(scope)
    except PayloadValidationError as exc:
        raise OperationValidationError(str(exc)) from exc
    return validated or {}


def _operation_scope_for_read(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    scope = {key: value for key, value in scope.items() if key not in {"url", "title", "user_note"}}
    try:
        return sanitized_operation_payload(scope)
    except PayloadValidationError:
        return sanitized_operation_payload({})


def _scope_int(scope: dict[str, Any], key: str) -> int | None:
    value = scope.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finish_operation(
    session: Session,
    operation: AppOperation,
    status: str,
    *,
    message: str | None = None,
    error_message: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    operation.status = status
    if message is not None:
        operation.message = message
    operation.error_message = error_message
    if result is not None:
        operation.result_json = result
    operation.completed_at = _now_utc()
    operation.updated_at = operation.completed_at
    session.add(operation)
    session.flush()
    session.refresh(operation)
    log_operation_event(
        logger,
        "operation_terminal_write",
        operation=operation,
        status=status,
        has_error=error_message is not None,
        has_result=result is not None,
    )
    invalidate_homepage_snapshots()


def _interrupt_stale_active_operations(session: Session) -> None:
    statement = (
        select(AppOperation)
        .where(AppOperation.status.in_(ACTIVE_STATUSES))
        .where(or_(AppOperation.runner_id.is_(None), AppOperation.runner_id != RUNNER_ID))
        .order_by(AppOperation.created_at.asc(), AppOperation.id.asc())
    )
    for operation in session.scalars(statement):
        if operation_uses_jobs(session, operation):
            sync_job_backed_operation_state(session, operation)
            log_operation_event(logger, "operation_stale_runner_skipped_for_jobs", operation=operation)
            continue
        log_operation_event(logger, "operation_stale_runner_interrupted", operation=operation)
        _finalize_interrupted_capture_for_operation(session, operation)
        _finish_operation(
            session,
            operation,
            STATUS_INTERRUPTED,
            message=None,
            error_message="Operation was interrupted because the backend process restarted.",
        )


def _finalize_interrupted_capture_for_operation(session: Session, operation: AppOperation) -> None:
    if operation.operation_type != OPERATION_TYPE_KNOWLEDGE_CAPTURE:
        return

    saved_item_id = _scope_int(operation.scope_json or {}, "saved_item_id")
    if saved_item_id is None:
        return

    finalize_interrupted_saved_item_capture_run(session, saved_item_id)


def _get_active_global_operation_row(session: Session) -> AppOperation | None:
    return session.scalars(
        select(AppOperation)
        .where(AppOperation.status.in_(ACTIVE_STATUSES))
        .where(AppOperation.operation_type.in_(GLOBAL_OPERATION_TYPES))
        .order_by(AppOperation.created_at.desc(), AppOperation.id.desc())
        .limit(1)
    ).first()


def _acquire_saved_item_mutex_lock(session: Session, saved_item_id: int) -> None:
    _acquire_operation_mutex_lock(session, f"saved_item:{int(saved_item_id)}")


def _acquire_operation_mutex_lock(session: Session, mutex_key: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"infoflow_operation_mutex:{mutex_key}"},
    )


def _get_active_target_operation_row(
    session: Session,
    operation_type: str,
    *,
    target_key: str,
    target_id: int,
) -> AppOperation | None:
    operation_types = {operation_type} if isinstance(operation_type, str) else set(operation_type)
    candidates = session.scalars(
        select(AppOperation)
        .where(AppOperation.operation_type.in_(operation_types))
        .where(AppOperation.status.in_(ACTIVE_STATUSES))
        .order_by(AppOperation.created_at.asc(), AppOperation.id.asc())
    )
    for operation in candidates:
        scope = operation.scope_json or {}
        if _scope_int(scope, target_key) == target_id:
            return operation
    return None


def _get_active_saved_item_mutex_operation_row(session: Session, *, saved_item_id: int) -> AppOperation | None:
    candidates = session.scalars(
        select(AppOperation)
        .where(AppOperation.operation_type.in_(SAVED_ITEM_MIRROR_MUTEX_OPERATION_TYPES))
        .where(AppOperation.status.in_(ACTIVE_STATUSES))
        .order_by(AppOperation.created_at.asc(), AppOperation.id.asc())
    )
    for operation in candidates:
        scope = operation.scope_json or {}
        if _scope_int(scope, "saved_item_id") == saved_item_id:
            return operation
    return None


def _resolve_sync_sources(
    session: Session,
    *,
    kind: str,
    source_id: int | None,
    source_ids: list[int] | None,
) -> list[Source]:
    if kind == "all":
        return list(session.scalars(select(Source).where(Source.is_active.is_(True)).order_by(Source.id)))
    if kind == "selected":
        if not source_ids:
            raise OperationValidationError("source_ids is required for selected source refresh operations")
        sources_by_id = {
            source.id: source
            for source in session.scalars(select(Source).where(Source.id.in_(source_ids)).order_by(Source.id))
        }
        missing_source_ids = [next_source_id for next_source_id in source_ids if next_source_id not in sources_by_id]
        if missing_source_ids:
            raise OperationValidationError(f"Source not found: {missing_source_ids[0]}")
        return [sources_by_id[next_source_id] for next_source_id in source_ids]
    if source_id is None:
        raise OperationValidationError("source_id is required for single-source operations")
    source = session.get(Source, source_id)
    if source is None:
        raise OperationValidationError("Source not found")
    return [source]


def _load_source_for_operation(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise OperationValidationError("Source not found")
    return source


def _load_saved_item_for_operation(session: Session, saved_item_id: int) -> SavedItem:
    saved_item = session.get(SavedItem, saved_item_id)
    if saved_item is None or saved_item.removed_at is not None:
        raise OperationValidationError("Saved item not found")
    return saved_item


def _load_feed_item_for_operation(session: Session, feed_item_id: int) -> FeedItem:
    feed_item = session.get(FeedItem, feed_item_id)
    if feed_item is None:
        raise OperationValidationError("Feed item not found")
    return feed_item


def _initial_progress(total: int) -> dict[str, Any]:
    return {
        "stage": "idle",
        "total": total,
        "completed": 0,
        "success_count": 0,
        "error_count": 0,
        "stopped": False,
        "stop_requested": False,
        "current_source_name": None,
        "current_source_index": None,
        "current_fetch_status": None,
        "current_items_seen_count": 0,
        "current_items_created_count": 0,
        "current_items_matched_count": 0,
        "current_items_invalid_count": 0,
        "current_insight_total": 0,
        "current_insight_completed": 0,
        "current_insight_failed": 0,
        "current_insight_artifact_rows_created": 0,
        "latest_source_name": None,
        "latest_status": None,
        "items_seen_count": 0,
        "items_created_count": 0,
        "items_matched_count": 0,
        "items_invalid_count": 0,
        "insight_total_count": 0,
        "insight_completed_count": 0,
        "insight_failed_count": 0,
        "insight_artifact_rows_created": 0,
        "latest_error_message": None,
        "failed_sources": [],
    }


def _initial_insight_progress(label: str, item_count: int) -> dict[str, Any]:
    progress = _initial_progress(1)
    progress.update(
        {
            "stage": "insight",
            "current_source_name": label,
            "current_source_index": 1,
            "current_insight_total": item_count,
            "insight_total_count": item_count,
        }
    )
    return progress


def _progress(operation: AppOperation) -> dict[str, Any]:
    return dict(operation.progress_json or _initial_progress(0))


def _feed_item_insight_artifact_result(
    *,
    scope: str,
    processed_feed_item_count: int = 0,
    artifact_rows_created: int = 0,
    failed_feed_item_count: int = 0,
    stopped: bool = False,
) -> dict[str, Any]:
    payload = FeedItemInsightArtifactBuildResult(
        scope=scope,
        processed_feed_item_count=processed_feed_item_count,
        artifact_rows_created=artifact_rows_created,
        failed_feed_item_count=failed_feed_item_count,
    ).model_dump(mode="json")
    payload["stopped"] = stopped
    return payload


def _build_source_sync_start_message(kind: str, source: Source | None) -> str:
    if kind == "all":
        return "已创建全量来源刷新任务。"
    if kind == "selected":
        return "已创建所选来源刷新任务。"
    if source is not None:
        return f"已创建来源刷新任务：{source.name}"
    return "已创建来源刷新任务。"

def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
