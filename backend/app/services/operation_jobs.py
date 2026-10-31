from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import socket
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.operation_logging import log_operation_event
from app.core.security import redact_text, sanitized_operation_payload
from app.db.models import AppOperation, InsightEngineAttempt, Job, SavedItem, SavedItemCaptureRun, Source, SourceItemOccurrence
from app.schemas.source import BulkSourceSyncResponse, SingleSourceSyncResponse
from app.db.session import get_session_factory
from app.services.job_queue import (
    ACTIVE_JOB_STATUSES,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_QUEUE_NAME,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    TERMINAL_JOB_STATUSES,
    JobLeaseError,
    cancel_job,
    claim_jobs,
    complete_job,
    create_job,
    extend_job_lease,
    fail_job,
    finish_running_job_cancelled,
    aggregate_operation_progress_from_jobs,
    requeue_expired_running_jobs,
)
from app.services.source_sync import sync_one_source
from app.services.insight_engine_artifact_builder import (
    build_feed_item_insight_artifacts_for_item,
    list_missing_feed_item_insight_artifact_candidates,
)
from app.services.app_settings import (
    AI_PROFILE_GENERAL,
    AI_ROUTE_DISABLED,
    AI_PROFILES,
    effective_ai_artifact_concurrency,
    resolve_ai_provider_config_for_source_route,
)
from app.services.homepage import invalidate_homepage_snapshots
from app.services.homepage_read_model import (
    rebuild_homepage_read_models_for_feed_items,
    rebuild_homepage_read_models_for_source,
)
from app.services.insight_channels import ensure_default_channels, get_channel_catalog_version
from app.services.insight_engine_attempts import ATTEMPT_STATUS_FAILED
from app.services.insight_engine_artifacts import InsightEngineArtifactRebuildStats, build_latest_occurrence_subquery
from app.services.insight_profiles import DEFAULT_INSIGHT_PROFILE, get_active_insight_profile
from app.services.insight_scoring import FEATURE_SCHEMA_VERSION, SCORE_VERSION
from app.services.source_preference_profiles import (
    get_active_source_preference_profile_context,
    load_active_source_preference_profile_contexts,
)
from app.services.channel_lifecycle import reroute_feed_item_for_retired_channel
from app.services.knowledge_constants import (
    CAPTURE_STATUS_FAILED,
    CAPTURE_STATUS_PARTIAL_FAILED,
    CAPTURE_STATUS_PENDING,
    CAPTURE_STATUS_SUCCEEDED,
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_SUCCEEDED,
)
from app.services.knowledge_capture import execute_saved_item_capture_run, export_saved_item_after_capture, retry_saved_item_capture
from app.services.knowledge_read_model import get_saved_item_detail
from app.services.knowledge_saved_items import export_saved_item_local_mirror
from app.services.knowledge_uploads import execute_uploaded_material_capture
from app.services.reader import extract_reader_item_content
from app.services.saved_item_insights import (
    DEFAULT_SAVED_ITEM_AI_ROUTE,
    SavedItemInsightConfigurationError,
    normalize_saved_item_ai_route,
    rebuild_saved_item_insight,
)

logger = logging.getLogger(__name__)

OPERATION_TYPE_FEED_ITEM_EXTRACT = "feed_item_extract"
OPERATION_TYPE_SOURCE_SYNC = "source_sync"
OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS = "feed_item_insight_artifacts"
OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT = "feed_item_insight_artifact"
OPERATION_TYPE_KNOWLEDGE_CAPTURE = "knowledge_capture"
OPERATION_TYPE_LOCAL_MIRROR_EXPORT = "local_mirror_export"
OPERATION_TYPE_SAVED_ITEM_INSIGHT = "saved_item_insight"
OPERATION_TYPE_CHANNEL_LIFECYCLE = "channel_lifecycle"
FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES = {OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS}
FEED_ITEM_INSIGHT_ARTIFACT_OPERATION_TYPES = {OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT}
OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_GLOBAL = "global"
OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_CURRENT_SOURCE = "current_source"
OPERATION_KIND_SOURCE_SYNC_CREATED_FEED_ITEMS = "source_sync_created_feed_items"
OPERATION_KIND_FEED_ITEM_MANUAL_EXTRACT = "manual_extract"
OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_URL = "manual_url"
OPERATION_KIND_KNOWLEDGE_CAPTURE_UPLOADED_MATERIAL = "uploaded_material"
OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_IMPORT = "manual_import"
OPERATION_KIND_KNOWLEDGE_CAPTURE_RETRY = "retry_capture"
OPERATION_KIND_KNOWLEDGE_CAPTURE_FEED_SAVED = "feed_saved_capture"
OPERATION_KIND_LOCAL_MIRROR_MANUAL_REEXPORT = "manual_reexport"
OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING = "build_missing"
OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_REBUILD = "rebuild"
OPERATION_KIND_SAVED_ITEM_INSIGHT_REBUILD = "rebuild"
JOB_TYPE_SOURCE_SYNC_ONE_SOURCE = "source_sync_one_source"
JOB_TYPE_FEED_ITEM_EXTRACT = "feed_item_extract"
JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS = "feed_item_insight_artifacts"
JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD = "feed_item_insight_artifact_build"
JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT = "feed_item_insight_artifact"
JOB_TYPE_KNOWLEDGE_CAPTURE = "knowledge_capture"
JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT = "knowledge_capture_local_mirror_export"
JOB_TYPE_LOCAL_MIRROR_EXPORT = "local_mirror_export"
JOB_TYPE_SAVED_ITEM_INSIGHT = "saved_item_insight"
JOB_TYPE_CHANNEL_REROUTE_ITEM = "channel_reroute_item"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"
ACTIVE_OPERATION_STATUSES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}
SOURCE_SYNC_QUEUE_NAME = "source_sync"
SOURCE_SYNC_MAX_CONCURRENT_JOBS = 4
FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME = "feed_item_insight_artifacts"
FEED_ITEM_INSIGHT_ARTIFACT_MAX_CONCURRENT_JOBS = 32
CHANNEL_LIFECYCLE_QUEUE_NAME = "channel_lifecycle"
CHANNEL_LIFECYCLE_MAX_CONCURRENT_JOBS = 2
AI_THROTTLED_JOB_TYPES = frozenset(
    {
        JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
        JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
        JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
        JOB_TYPE_SAVED_ITEM_INSIGHT,
    }
)
AI_THROTTLE_DEFAULT_KEY = ("unknown", "unknown")

PROCESS_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}:operation-jobs"
HEARTBEAT_INTERVAL_SECONDS = 15.0
WORKER_POLL_INTERVAL_SECONDS = 1.0
RETRYABLE_FEED_ITEM_ARTIFACT_ATTEMPT_STAGES = frozenset(
    {
        "provider_request",
    }
)
RETRYABLE_PROVIDER_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
RETRYABLE_SOURCE_SYNC_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_worker_lock = threading.Lock()
_worker_stop_event = threading.Event()
_worker_wake_event = threading.Event()
_worker_thread: threading.Thread | None = None
_ai_lane_wake_event = threading.Event()
_ai_lane_worker_thread: threading.Thread | None = None
_ai_throttle_claim_lock = threading.Lock()
_source_sync_artifact_result_lock = threading.RLock()


@dataclass(frozen=True, slots=True)
class OperationJobHandler:
    job_type: str
    operation_type: str
    run: Callable[[Session, Job, AppOperation], None]
    success_event_payload: Callable[[Job], dict[str, Any]]


class OperationJobPermanentError(RuntimeError):
    pass


class OperationJobRetryableError(RuntimeError):
    pass


def _build_feed_item_insight_enqueue_context(
    session: Session,
    *,
    operation_id: int | None,
    feed_item_id: int | None = None,
    source_id: int | None = None,
) -> dict[str, Any]:
    ensure_default_channels(session)
    active_profile = get_active_insight_profile(session)
    effective_source_id = source_id if source_id is not None else _latest_source_id_for_feed_item(session, feed_item_id)
    source_profile_context = get_active_source_preference_profile_context(session, effective_source_id)
    context = {
        "feed_item_id": feed_item_id,
        "source_id": effective_source_id,
        "profile_version": active_profile.profile_version if active_profile is not None else DEFAULT_INSIGHT_PROFILE["profile_version"],
        "channel_catalog_version": get_channel_catalog_version(session),
        "source_profile_version": source_profile_context.profile_version,
        "source_profile_is_empty": source_profile_context.is_empty,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "score_version": SCORE_VERSION,
        "operation_id": operation_id,
    }
    ai_context = _build_feed_item_ai_enqueue_context(session, source_id=effective_source_id)
    if ai_context:
        context.update(ai_context)
    if feed_item_id is None and source_id is None:
        context["source_profile_snapshot_by_source_id"] = _build_source_profile_snapshot_by_source_id(session)
    return context


def _build_feed_item_ai_enqueue_context(session: Session, *, source_id: int | None) -> dict[str, Any]:
    if source_id is None:
        return {}
    source = session.get(Source, source_id)
    if source is None:
        return {}
    ai_route = (source.ai_route or AI_PROFILE_GENERAL).strip().lower()
    config = resolve_ai_provider_config_for_source_route(session, ai_route)
    if config is None:
        return {
            "ai_route": AI_ROUTE_DISABLED,
            "ai_profile": AI_ROUTE_DISABLED,
            "ai_provider": AI_ROUTE_DISABLED,
            "ai_model": None,
        }
    return {
        "ai_route": ai_route,
        "ai_profile": config.profile,
        "ai_provider": config.provider,
        "ai_model": config.model,
    }


def _build_saved_item_insight_enqueue_context(
    session: Session,
    *,
    saved_item_id: int,
    ai_route: str | None,
) -> dict[str, Any]:
    saved_item = session.get(SavedItem, saved_item_id)
    route = normalize_saved_item_ai_route(
        ai_route if ai_route not in {None, "saved_item"} else (saved_item.ai_route if saved_item is not None else None),
        default=DEFAULT_SAVED_ITEM_AI_ROUTE,
    )
    config = resolve_ai_provider_config_for_source_route(session, route)
    if config is None:
        return {
            "saved_item_id": saved_item_id,
            "ai_route": AI_ROUTE_DISABLED,
            "ai_profile": AI_ROUTE_DISABLED,
            "ai_provider": AI_ROUTE_DISABLED,
            "ai_model": None,
        }
    return {
        "saved_item_id": saved_item_id,
        "ai_route": route,
        "ai_profile": config.profile,
        "ai_provider": config.provider,
        "ai_model": config.model,
    }


def _validate_feed_item_insight_enqueue_context(
    session: Session,
    payload: dict[str, Any],
    *,
    operation_id: int | None,
    feed_item_id: int | None,
) -> None:
    context = payload.get("enqueue_context")
    if not isinstance(context, dict):
        return

    payload_source_id = _payload_int(payload, "source_id")
    current_source_id = payload_source_id
    if feed_item_id is not None:
        latest_source_id = _latest_source_id_for_feed_item(session, feed_item_id)
        current_source_id = latest_source_id if latest_source_id is not None else payload_source_id
    current = _build_feed_item_insight_enqueue_context(
        session,
        operation_id=operation_id,
        feed_item_id=feed_item_id,
        source_id=current_source_id,
    )
    expected = context
    actual = current

    global_snapshot = expected.get("source_profile_snapshot_by_source_id")
    use_global_source_snapshot = (
        feed_item_id is not None
        and expected.get("feed_item_id") is None
        and expected.get("source_id") is None
        and isinstance(global_snapshot, dict)
    )
    keys = (
        ("operation_id", "profile_version", "channel_catalog_version", "feature_schema_version", "score_version")
        if use_global_source_snapshot
        else (
            "operation_id",
            "feed_item_id",
            "source_id",
            "ai_route",
            "ai_profile",
            "ai_provider",
            "ai_model",
            "profile_version",
            "channel_catalog_version",
            "source_profile_version",
            "source_profile_is_empty",
            "feature_schema_version",
            "score_version",
        )
    )

    mismatches: list[str] = []
    for key in keys:
        if key.startswith("ai_") and key not in expected:
            continue
        if expected.get(key) != actual.get(key):
            mismatches.append(f"{key} enqueue={expected.get(key)!r} current={actual.get(key)!r}")
    if use_global_source_snapshot:
        if current_source_id is None:
            mismatches.append("source_id enqueue=<global snapshot> current=None")
        else:
            source_snapshot = global_snapshot.get(str(current_source_id))
            if not isinstance(source_snapshot, dict):
                mismatches.append(f"source_profile_snapshot source_id={current_source_id} missing")
            else:
                for key in ("source_profile_version", "source_profile_is_empty"):
                    if source_snapshot.get(key) != actual.get(key):
                        mismatches.append(
                            f"{key} source_id={current_source_id} "
                            f"enqueue={source_snapshot.get(key)!r} current={actual.get(key)!r}"
                        )
    if mismatches:
        raise OperationJobPermanentError(
            "Feed item insight enqueue context is stale or unavailable; "
            + "; ".join(mismatches)
        )


def _validate_saved_item_insight_enqueue_context(
    session: Session,
    payload: dict[str, Any],
    *,
    saved_item_id: int,
) -> None:
    context = payload.get("enqueue_context")
    if not isinstance(context, dict):
        return
    current = _build_saved_item_insight_enqueue_context(
        session,
        saved_item_id=saved_item_id,
        ai_route=payload.get("ai_route") if isinstance(payload.get("ai_route"), str) else None,
    )
    mismatches = []
    for key in ("saved_item_id", "ai_route", "ai_profile", "ai_provider", "ai_model"):
        if key.startswith("ai_") and key not in context:
            continue
        if context.get(key) != current.get(key):
            mismatches.append(f"{key} enqueue={context.get(key)!r} current={current.get(key)!r}")
    if mismatches:
        raise OperationJobPermanentError(
            "Saved item insight enqueue context is stale or unavailable; "
            + "; ".join(mismatches)
        )


def _latest_source_id_for_feed_item(session: Session, feed_item_id: int | None) -> int | None:
    if feed_item_id is None:
        return None
    latest_occurrences = build_latest_occurrence_subquery()
    return session.scalar(
        select(SourceItemOccurrence.source_id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.occurrence_id == SourceItemOccurrence.id)
            & (latest_occurrences.c.occurrence_rank == 1),
        )
        .where(latest_occurrences.c.feed_item_id == feed_item_id)
    )


def _build_source_profile_snapshot_by_source_id(session: Session) -> dict[str, dict[str, Any]]:
    source_ids = list(session.scalars(select(Source.id).order_by(Source.id)))
    contexts = load_active_source_preference_profile_contexts(session, source_ids)
    return {
        str(source_id): {
            "source_profile_version": context.profile_version,
            "source_profile_is_empty": context.is_empty,
        }
        for source_id, context in contexts.items()
    }


def _has_global_source_profile_snapshot(payload: dict[str, Any]) -> bool:
    context = payload.get("enqueue_context")
    return isinstance(context, dict) and isinstance(context.get("source_profile_snapshot_by_source_id"), dict)


def build_operation_job_registry(handlers: Sequence[OperationJobHandler]) -> dict[str, OperationJobHandler]:
    registry: dict[str, OperationJobHandler] = {}
    for handler in handlers:
        if handler.job_type in registry:
            raise ValueError(f"Duplicate operation job handler: {handler.job_type}")
        registry[handler.job_type] = handler
    return registry


def _feed_item_insight_artifacts_payload(
    *,
    scope: str,
    source_id: int | None,
    limit: int | None,
    source_sync_operation_id: int | None,
    enqueue_context: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "scope": scope,
        "source_id": source_id,
        "limit": limit,
        "enqueue_context": enqueue_context,
    }
    if source_sync_operation_id is not None:
        payload["source_sync_operation_id"] = source_sync_operation_id
    return payload


def enqueue_source_sync_job(
    session: Session,
    operation: AppOperation,
    *,
    source_id: int,
    source_index: int,
    source_count: int,
) -> Job:
    if operation.id is None:
        session.flush()
    return create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_SOURCE_SYNC_ONE_SOURCE,
        queue_name=SOURCE_SYNC_QUEUE_NAME,
        payload={
            "source_id": source_id,
            "source_index": source_index,
            "source_count": source_count,
        },
        idempotency_key=f"source_sync_one_source:{source_id}",
    )


def enqueue_feed_item_extract_job(session: Session, operation: AppOperation, *, feed_item_id: int) -> Job:
    if operation.id is None:
        session.flush()
    return create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_FEED_ITEM_EXTRACT,
        payload={"feed_item_id": feed_item_id},
        idempotency_key=f"operation:feed_item_extract:{feed_item_id}",
    )


def enqueue_feed_item_insight_artifacts_job(
    session: Session,
    operation: AppOperation,
    *,
    scope: str,
    source_id: int | None,
    limit: int | None,
    source_sync_operation_id: int | None = None,
) -> Job:
    if operation.id is None:
        session.flush()
    enqueue_context = _build_feed_item_insight_enqueue_context(
        session,
        operation_id=operation.id,
        feed_item_id=None,
        source_id=source_id,
    )
    job_type = JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS
    idempotency_prefix = "feed_item_insight_artifacts"
    if scope == "source_sync_created_feed_items" and source_sync_operation_id is not None:
        idempotency_key = f"operation:{idempotency_prefix}:source-sync:{source_sync_operation_id}"
    elif source_id is not None:
        idempotency_key = f"operation:{idempotency_prefix}:source:{source_id}"
    else:
        idempotency_key = f"operation:{idempotency_prefix}:global"
    job = create_job(
        session,
        operation_id=operation.id,
        job_type=job_type,
        queue_name=FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
        payload=_feed_item_insight_artifacts_payload(
            scope=scope,
            source_id=source_id,
            limit=limit,
            source_sync_operation_id=source_sync_operation_id,
            enqueue_context=enqueue_context,
        ),
        idempotency_key=idempotency_key,
    )
    wake_ai_lane_worker()
    return job


def enqueue_source_sync_artifact_build_job(
    session: Session,
    operation: AppOperation,
    *,
    source_sync_operation_id: int,
    feed_item_id: int,
    source_id: int | None,
) -> tuple[Job, bool]:
    return enqueue_feed_item_insight_artifact_build_job(
        session,
        operation,
        feed_item_id=feed_item_id,
        source_id=source_id,
        source_sync_operation_id=source_sync_operation_id,
    )


def enqueue_feed_item_insight_artifact_build_job(
    session: Session,
    operation: AppOperation,
    *,
    feed_item_id: int,
    source_id: int | None,
    source_sync_operation_id: int | None = None,
) -> tuple[Job, bool]:
    if operation.id is None:
        session.flush()
    enqueue_context = _build_feed_item_insight_enqueue_context(
        session,
        operation_id=operation.id,
        feed_item_id=feed_item_id,
        source_id=source_id,
    )
    idempotency_context = (
        f"source-sync:{source_sync_operation_id}"
        if source_sync_operation_id is not None
        else f"operation:{operation.id}"
    )
    idempotency_key = f"operation:feed_item_insight_artifact_build:{idempotency_context}:item:{feed_item_id}"
    existing = _existing_job_by_idempotency_key(session, idempotency_key)
    if existing is not None:
        return existing, False
    payload: dict[str, Any] = {
        "feed_item_id": feed_item_id,
        "source_id": source_id,
        "mode": OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING,
        "enqueue_context": enqueue_context,
    }
    if source_sync_operation_id is not None:
        payload["source_sync_operation_id"] = source_sync_operation_id
    job = create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
        queue_name=FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    wake_ai_lane_worker()
    return job, True


def enqueue_knowledge_capture_job(
    session: Session,
    operation: AppOperation,
    *,
    saved_item_id: int,
    capture_mode: str,
    capture_run_id: int | None = None,
    source_asset_id: int | None = None,
) -> Job:
    if operation.id is None:
        session.flush()
    payload: dict[str, Any] = {
        "saved_item_id": saved_item_id,
        "capture_mode": capture_mode,
    }
    if capture_run_id is not None:
        payload["capture_run_id"] = capture_run_id
    if source_asset_id is not None:
        payload["source_asset_id"] = source_asset_id
    return create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_KNOWLEDGE_CAPTURE,
        payload=payload,
        idempotency_key=f"operation:knowledge_capture:{operation.id}:capture",
    )


def enqueue_knowledge_capture_local_mirror_job(
    session: Session,
    operation: AppOperation,
    *,
    saved_item_id: int,
    capture_mode: str,
    source_capture_run_id: int | None = None,
) -> Job:
    if operation.id is None:
        session.flush()
    payload: dict[str, Any] = {
        "saved_item_id": saved_item_id,
        "capture_mode": capture_mode,
    }
    if source_capture_run_id is not None:
        payload["source_capture_run_id"] = source_capture_run_id
    return create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
        payload=payload,
        idempotency_key=f"operation:knowledge_capture:{operation.id}:mirror",
    )


def enqueue_local_mirror_export_job(session: Session, operation: AppOperation, *, saved_item_id: int) -> Job:
    if operation.id is None:
        session.flush()
    return create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_LOCAL_MIRROR_EXPORT,
        payload={"saved_item_id": saved_item_id},
        idempotency_key=f"operation:local_mirror_export:{saved_item_id}",
    )


def enqueue_feed_item_insight_artifact_job(
    session: Session,
    operation: AppOperation,
    *,
    feed_item_id: int,
    mode: str,
) -> Job:
    if operation.id is None:
        session.flush()
    job_type = JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT
    idempotency_prefix = "feed_item_insight_artifact"
    enqueue_context = _build_feed_item_insight_enqueue_context(
        session,
        operation_id=operation.id,
        feed_item_id=feed_item_id,
    )
    job = create_job(
        session,
        operation_id=operation.id,
        job_type=job_type,
        queue_name=FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
        payload={
            "feed_item_id": feed_item_id,
            "mode": mode,
            "enqueue_context": enqueue_context,
        },
        idempotency_key=f"operation:{idempotency_prefix}:{operation.id}:item:{feed_item_id}:mode:{mode}",
    )
    wake_ai_lane_worker()
    return job


def enqueue_saved_item_insight_job(
    session: Session,
    operation: AppOperation,
    *,
    saved_item_id: int,
    ai_route: str | None,
) -> Job:
    if operation.id is None:
        session.flush()
    enqueue_context = _build_saved_item_insight_enqueue_context(session, saved_item_id=saved_item_id, ai_route=ai_route)
    job = create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_SAVED_ITEM_INSIGHT,
        queue_name=FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
        payload={"saved_item_id": saved_item_id, "ai_route": ai_route, "enqueue_context": enqueue_context},
        idempotency_key=f"operation:saved_item_insight:{operation.id}:item:{saved_item_id}",
    )
    wake_ai_lane_worker()
    return job


def enqueue_channel_reroute_item_job(
    session: Session,
    operation: AppOperation,
    *,
    feed_item_id: int,
    retired_channel_id: str,
    fallback_channel_id: str,
    channel_catalog_version: int,
    reason: str,
) -> Job:
    if operation.id is None:
        session.flush()
    return create_job(
        session,
        operation_id=operation.id,
        job_type=JOB_TYPE_CHANNEL_REROUTE_ITEM,
        queue_name=CHANNEL_LIFECYCLE_QUEUE_NAME,
        payload={
            "feed_item_id": feed_item_id,
            "retired_channel_id": retired_channel_id,
            "fallback_channel_id": fallback_channel_id,
            "channel_catalog_version": channel_catalog_version,
            "reason": reason,
            "operation_id": operation.id,
        },
        idempotency_key=f"channel_reroute_item:{feed_item_id}:{channel_catalog_version}",
    )


def operation_uses_jobs(session: Session, operation: AppOperation) -> bool:
    del session
    return operation.id is not None and operation.operation_type in JOB_BACKED_OPERATION_TYPES


def wake_ai_lane_worker() -> None:
    _ai_lane_wake_event.set()


def wake_operation_job_worker() -> None:
    _worker_wake_event.set()
    _ai_lane_wake_event.set()


def cancel_active_jobs_for_operation(session: Session, operation: AppOperation) -> AppOperation:
    if operation.id is None:
        return operation
    jobs = list(
        session.scalars(
            select(Job)
            .where(Job.operation_id == operation.id)
            .where(Job.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(Job.id.asc())
        )
    )
    for job in jobs:
        cancel_job(session, job.id, reason="operation_stop_requested")
    return sync_job_backed_operation_state(session, operation)


def run_operation_jobs_for_operation(
    operation_id: int,
    *,
    worker_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    poll_interval_seconds: float = 1.0,
    max_drain_seconds: float | None = 900.0,
    failure_backoff_seconds: int | None = None,
    max_concurrent_jobs: int = 1,
    now: datetime | None = None,
) -> bool:
    resolved_worker_id = worker_id or _new_drain_worker_id()
    claim_limit = max(1, max_concurrent_jobs)
    ran_job = False
    drain_started = time.monotonic()

    while True:
        with get_session_factory()() as session:
            operation = _lock_operation_for_update(session, operation_id)
            if operation is None or not operation_uses_jobs(session, operation):
                return ran_job
            if operation.status not in {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}:
                return ran_job
            if operation.stop_requested:
                cancel_active_jobs_for_operation(session, operation)
                if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC and operation.id is not None:
                    request_stop_source_sync_artifact_build(session, operation.id)
            if _operation_uses_ai_throttled_jobs(operation):
                sync_job_backed_operation_state(session, operation, now=now)
                session.commit()
                wake_ai_lane_worker()
                return ran_job

            effective_claim_limit = _operation_job_claim_limit(
                session,
                operation,
                max_concurrent_jobs=claim_limit,
                now=now,
            )
            claimed = []
            if effective_claim_limit > 0:
                claimed = claim_jobs(
                    session,
                    worker_id=resolved_worker_id,
                    limit=effective_claim_limit,
                    job_types=tuple(JOB_HANDLER_TYPES),
                    operation_id=operation_id,
                    lease_seconds=lease_seconds,
                    now=now,
                )
            sync_job_backed_operation_state(session, operation, now=now)
            wait_seconds = None
            if not claimed and now is None:
                wait_seconds = _next_active_job_wait_seconds(
                    session,
                    operation_id,
                    max_poll_interval_seconds=poll_interval_seconds,
                )
            session.commit()

        if not claimed:
            if wait_seconds is None:
                return ran_job
            if max_drain_seconds is not None:
                remaining = max_drain_seconds - (time.monotonic() - drain_started)
                if remaining <= 0:
                    return ran_job
                wait_seconds = min(wait_seconds, remaining)
            time.sleep(max(0.0, wait_seconds))
            continue

        ran_job = True
        if len(claimed) == 1:
            _run_claimed_job(
                claimed[0].id,
                worker_id=resolved_worker_id,
                lease_seconds=lease_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                failure_backoff_seconds=failure_backoff_seconds,
                now=now,
            )
        else:
            with ThreadPoolExecutor(max_workers=len(claimed)) as executor:
                futures = [
                    executor.submit(
                        _run_claimed_job,
                        job.id,
                        worker_id=resolved_worker_id,
                        lease_seconds=lease_seconds,
                        heartbeat_interval_seconds=heartbeat_interval_seconds,
                        failure_backoff_seconds=failure_backoff_seconds,
                        now=now,
                    )
                    for job in claimed
                ]
                for future in futures:
                    future.result()


def run_next_available_operation_job(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> bool:
    with get_session_factory()() as session:
        claimed = claim_jobs(
            session,
            worker_id=worker_id,
            limit=_channel_lifecycle_queue_concurrency(),
            queue_names=(CHANNEL_LIFECYCLE_QUEUE_NAME,),
            job_types=tuple(GLOBAL_WORKER_JOB_TYPES),
            lease_seconds=lease_seconds,
        )
        if not claimed:
            claimed = claim_jobs(
                session,
                worker_id=worker_id,
                limit=1,
                queue_names=(DEFAULT_QUEUE_NAME,),
                job_types=tuple(GLOBAL_WORKER_JOB_TYPES),
                lease_seconds=lease_seconds,
            )
        _sync_all_job_backed_operations(session)
        session.commit()

    if not claimed:
        return False

    if len(claimed) == 1:
        _run_claimed_job(
            claimed[0].id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            failure_backoff_seconds=None,
            now=None,
        )
    else:
        with ThreadPoolExecutor(max_workers=len(claimed)) as executor:
            futures = [
                executor.submit(
                    _run_claimed_job,
                    job.id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                    failure_backoff_seconds=None,
                    now=None,
                )
                for job in claimed
            ]
            for future in futures:
                future.result()
    return True


def run_ai_lane_jobs_until_idle(
    *,
    worker_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    poll_interval_seconds: float = 0.1,
    failure_backoff_seconds: int | None = None,
    max_drain_seconds: float | None = 30.0,
    max_concurrent_jobs: int | None = None,
) -> bool:
    return _run_ai_lane_engine(
        worker_id=worker_id or _new_ai_lane_worker_id(),
        stop_event=threading.Event(),
        wake_event=None,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        poll_interval_seconds=poll_interval_seconds,
        failure_backoff_seconds=failure_backoff_seconds,
        max_drain_seconds=max_drain_seconds,
        max_concurrent_jobs=max_concurrent_jobs,
        stop_when_idle=True,
    )


def run_next_recoverable_source_sync_operation(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> bool:
    operation_id = _next_recoverable_source_sync_operation_id()
    if operation_id is None:
        return False
    return run_operation_jobs_for_operation(
        operation_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        max_drain_seconds=None,
        max_concurrent_jobs=SOURCE_SYNC_MAX_CONCURRENT_JOBS,
    )


def start_operation_job_worker(*, poll_interval_seconds: float = WORKER_POLL_INTERVAL_SECONDS) -> bool:
    global _worker_thread, _ai_lane_worker_thread
    started = False
    with _worker_lock:
        generic_alive = _worker_thread is not None and _worker_thread.is_alive()
        ai_lane_alive = _ai_lane_worker_thread is not None and _ai_lane_worker_thread.is_alive()
        if generic_alive and ai_lane_alive:
            return False
        _worker_stop_event.clear()
        _worker_wake_event.clear()
        if not generic_alive:
            worker_id = _new_slot_worker_id(0)
            _worker_thread = threading.Thread(
                target=_operation_job_worker_loop,
                kwargs={"worker_id": worker_id, "poll_interval_seconds": poll_interval_seconds},
                name="operation-job-worker",
                daemon=True,
            )
            _worker_thread.start()
            logger.info("operation_job_worker_started worker_id=%s", worker_id)
            started = True
        if not ai_lane_alive:
            ai_worker_id = _new_ai_lane_worker_id()
            _ai_lane_wake_event.clear()
            _ai_lane_worker_thread = threading.Thread(
                target=_ai_lane_worker_loop,
                kwargs={"worker_id": ai_worker_id, "poll_interval_seconds": poll_interval_seconds},
                name="ai-lane-worker",
                daemon=True,
            )
            _ai_lane_worker_thread.start()
            logger.info("ai_lane_worker_started worker_id=%s", ai_worker_id)
            started = True
    return started


def stop_operation_job_worker(*, timeout_seconds: float = 5.0) -> None:
    global _worker_thread, _ai_lane_worker_thread
    with _worker_lock:
        thread = _worker_thread
        ai_thread = _ai_lane_worker_thread
        _worker_stop_event.set()
        _worker_wake_event.set()
        _ai_lane_wake_event.set()
    if thread is not None:
        thread.join(timeout=timeout_seconds)
    if ai_thread is not None:
        ai_thread.join(timeout=timeout_seconds)
    with _worker_lock:
        if thread is not None and _worker_thread is thread and not thread.is_alive():
            _worker_thread = None
        if ai_thread is not None and _ai_lane_worker_thread is ai_thread and not ai_thread.is_alive():
            _ai_lane_worker_thread = None
    logger.info("operation_job_workers_stopped")


def sync_job_backed_operation_state(
    session: Session,
    operation: AppOperation,
    *,
    now: datetime | None = None,
) -> AppOperation:
    if operation.id is None:
        return operation
    if operation.status not in ACTIVE_OPERATION_STATUSES:
        return operation
    locked_operation = _lock_operation_for_update(session, operation.id)
    if locked_operation is None:
        return operation
    operation = locked_operation
    if operation.status not in ACTIVE_OPERATION_STATUSES:
        return operation
    jobs = list(session.scalars(select(Job).where(Job.operation_id == operation.id).order_by(Job.id.asc())))
    if not jobs:
        if operation.status in {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}:
            previous_status = operation.status
            updated_at = now or _now_utc()
            progress = dict(operation.progress_json) if isinstance(operation.progress_json, dict) else {}
            progress["stage"] = "failed"
            progress["stop_requested"] = bool(operation.stop_requested)
            if operation.operation_type == OPERATION_TYPE_KNOWLEDGE_CAPTURE:
                _compensate_knowledge_capture_terminal_state(
                    session,
                    operation,
                    reason="Job-backed operation has no jobs.",
                )
            operation.status = STATUS_FAILED
            operation.error_message = "Job-backed operation has no jobs."
            operation.message = _operation_failed_message(operation)
            operation.completed_at = operation.completed_at or updated_at
            operation.updated_at = updated_at
            operation.progress_json = progress
            session.add(operation)
            session.flush()
            session.refresh(operation)
            if previous_status not in {STATUS_COMPLETED, STATUS_STOPPED, STATUS_FAILED}:
                log_operation_event(
                    logger,
                    "operation_terminal_write",
                    operation=operation,
                    status=operation.status,
                    has_error=True,
                    has_result=operation.result_json is not None,
                )
                invalidate_homepage_snapshots()
        return operation

    previous_status = operation.status
    base_progress = operation.progress_json if isinstance(operation.progress_json, dict) else {}
    updated_at = now or _now_utc()
    progress = aggregate_operation_progress_from_jobs(session, operation.id, base_progress=base_progress)
    progress["stop_requested"] = bool(operation.stop_requested)
    started_values = [_as_utc(job.started_at) for job in jobs if job.started_at is not None]
    if started_values and operation.started_at is None:
        operation.started_at = min(started_values)

    statuses = {job.status for job in jobs}
    terminal = statuses.issubset(TERMINAL_JOB_STATUSES)
    if terminal:
        operation.completed_at = operation.completed_at or updated_at
        if JOB_STATUS_FAILED in statuses:
            failed_job = next((job for job in jobs if job.status == JOB_STATUS_FAILED), None)
            if operation.operation_type == OPERATION_TYPE_KNOWLEDGE_CAPTURE:
                _compensate_knowledge_capture_terminal_state(
                    session,
                    operation,
                    reason=_safe_error_message(failed_job.error_message if failed_job else "Capture job failed.")
                    or "Capture job failed.",
                )
            operation.status = STATUS_FAILED
            operation.error_message = _safe_error_message(failed_job.error_message if failed_job else "Job failed.")
            operation.message = _operation_failed_message(operation)
            progress["stage"] = "failed"
        elif JOB_STATUS_CANCELLED in statuses:
            if operation.operation_type == OPERATION_TYPE_KNOWLEDGE_CAPTURE:
                _compensate_knowledge_capture_terminal_state(
                    session,
                    operation,
                    reason="Capture operation was stopped before processing completed.",
                )
            operation.status = STATUS_STOPPED
            operation.error_message = None
            if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
                operation.result_json = _build_source_sync_operation_result(operation, progress, stopped=True)
            else:
                if isinstance(operation.result_json, dict):
                    operation.result_json = {**operation.result_json, "stopped": True}
                else:
                    operation.result_json = {"stopped": True}
            operation.message = _operation_stopped_message(operation)
            progress["stage"] = "stopped"
            progress["stopped"] = True
        else:
            operation.status = STATUS_COMPLETED
            operation.error_message = None
            progress["stage"] = "completed"
            if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
                operation.result_json = _build_source_sync_operation_result(operation, progress, stopped=False)
                operation.message = _build_source_sync_complete_message(operation.result_json)
            elif operation.operation_type == OPERATION_TYPE_CHANNEL_LIFECYCLE:
                result = operation.result_json if isinstance(operation.result_json, dict) else {}
                operation.message = (
                    f"Channel reroute completed: created {result.get('artifact_rows_created', 0)} routing artifacts, "
                    f"skipped {result.get('skipped_feed_item_count', 0)} items."
                )
            elif operation.operation_type == OPERATION_TYPE_KNOWLEDGE_CAPTURE:
                operation.message = operation.message or _knowledge_capture_complete_message(operation)
    elif JOB_STATUS_RUNNING in statuses:
        operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
        operation.completed_at = None
    elif JOB_STATUS_PENDING in statuses:
        operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_QUEUED
        operation.completed_at = None

    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES and statuses == {JOB_STATUS_SUCCEEDED}:
        _restore_feed_item_insight_artifact_operation_counters(base_progress, progress)
    if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
        _restore_source_sync_counters(base_progress, progress)
    if (
        terminal
        and operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES
        and operation.operation_kind == OPERATION_KIND_SOURCE_SYNC_CREATED_FEED_ITEMS
        and _linked_source_sync_operation_is_active(session, operation)
    ):
        operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
        operation.completed_at = None
        progress["stage"] = "stopping" if operation.stop_requested else "waiting_source_sync"
        progress["stop_requested"] = bool(operation.stop_requested)
        operation.message = (
            "Feed item insight artifact operation is waiting for source refresh to finish."
            if not operation.stop_requested
            else "Stop requested; waiting for source refresh to finish."
        )
    source_sync_operation_id_to_refresh = None
    if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
        source_sync_operation_id_to_refresh = operation.id
    elif (
        operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES
        and operation.operation_kind == OPERATION_KIND_SOURCE_SYNC_CREATED_FEED_ITEMS
    ):
        scope = operation.scope_json if isinstance(operation.scope_json, dict) else {}
        source_sync_operation_id_to_refresh = _scope_int(scope, "source_sync_operation_id")
    operation.progress_json = progress
    operation.updated_at = updated_at
    session.add(operation)
    session.flush()
    if source_sync_operation_id_to_refresh is not None:
        _refresh_source_sync_artifact_build_results(session, source_sync_operation_id_to_refresh)
    session.refresh(operation)
    operation_terminal_statuses = {STATUS_COMPLETED, STATUS_STOPPED, STATUS_FAILED}
    if previous_status not in operation_terminal_statuses and operation.status in operation_terminal_statuses:
        log_operation_event(
            logger,
            "operation_terminal_write",
            operation=operation,
            status=operation.status,
            has_error=operation.error_message is not None,
            has_result=operation.result_json is not None,
        )
        invalidate_homepage_snapshots()
    return operation


def _restore_feed_item_insight_artifact_operation_counters(base_progress: dict[str, Any], progress: dict[str, Any]) -> None:
    for key in (
        "success_count",
        "error_count",
        "current_insight_total",
        "current_insight_completed",
        "current_insight_failed",
        "current_insight_artifact_rows_created",
        "insight_total_count",
        "insight_completed_count",
        "insight_failed_count",
        "insight_artifact_rows_created",
    ):
        value = base_progress.get(key)
        if isinstance(value, int):
            progress[key] = value


def _restore_source_sync_counters(base_progress: dict[str, Any], progress: dict[str, Any]) -> None:
    for key in (
        "items_seen_count",
        "items_created_count",
        "items_matched_count",
        "items_invalid_count",
        "failed_sources",
        "_source_sync_result_entries",
        "current_fetch_status",
        "current_items_seen_count",
        "current_items_created_count",
        "current_items_matched_count",
        "current_items_invalid_count",
        "latest_source_name",
        "latest_status",
        "latest_error_message",
    ):
        if key in base_progress:
            progress[key] = base_progress[key]
    entries = _source_sync_result_entries(progress)
    if entries:
        fetch_error_count = sum(
            1
            for entry in entries
            if isinstance(entry.get("result"), dict)
            and isinstance(entry["result"].get("fetch"), dict)
            and entry["result"]["fetch"].get("status") == "error"
        )
        progress["success_count"] = len(entries) - fetch_error_count
        progress["error_count"] = fetch_error_count


def _run_claimed_job(
    job_id: int,
    *,
    worker_id: str,
    lease_seconds: int,
    heartbeat_interval_seconds: float,
    failure_backoff_seconds: int | None,
    now: datetime | None,
) -> None:
    heartbeat = _Heartbeat(job_id, worker_id=worker_id, lease_seconds=lease_seconds, interval_seconds=heartbeat_interval_seconds)
    heartbeat.start()
    try:
        with get_session_factory()() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            operation = session.get(AppOperation, job.operation_id) if job.operation_id is not None else None
            if operation is None:
                raise OperationJobPermanentError("Operation not found for job.")

            _finish_cancelled_if_requested(session, job, operation, worker_id=worker_id, now=now)
            if job.status == JOB_STATUS_CANCELLED:
                session.commit()
                return

            handler = OPERATION_JOB_REGISTRY.get(job.job_type)
            if handler is None:
                raise OperationJobPermanentError(f"Unsupported job type: {job.job_type}")
            if operation.operation_type != handler.operation_type:
                raise OperationJobPermanentError(
                    f"Job type {job.job_type} does not match operation type {operation.operation_type}"
                )
            log_operation_event(logger, "operation_job_handler_started", operation=operation, job=job, worker_id=worker_id)
            handler_started_at = time.perf_counter()
            handler.run(session, job, operation)
            handler_run_ms = _elapsed_ms(handler_started_at)

            session.refresh(job)
            session.refresh(operation)
            if _job_success_overrides_late_cancel(job, operation):
                job.cancel_requested = False
                session.add(job)
                session.flush()
            else:
                _finish_cancelled_if_requested(session, job, operation, worker_id=worker_id, now=now)
            complete_started_at = time.perf_counter()
            if job.status != JOB_STATUS_CANCELLED:
                complete_job(
                    session,
                    job.id,
                    worker_id=worker_id,
                    result=handler.success_event_payload(job),
                    now=now,
                )
            complete_job_ms = _elapsed_ms(complete_started_at)
            sync_started_at = time.perf_counter()
            sync_job_backed_operation_state(session, operation, now=now)
            sync_operation_ms = _elapsed_ms(sync_started_at)
            total_job_ms = _job_running_total_ms(job)
            _log_operation_job_handler_timing(
                operation,
                job,
                worker_id=worker_id,
                handler_run_ms=handler_run_ms,
                complete_job_ms=complete_job_ms,
                sync_operation_ms=sync_operation_ms,
                total_job_ms=total_job_ms,
            )
            log_operation_event(logger, "operation_job_handler_completed", operation=operation, job=job, worker_id=worker_id)
            session.commit()
            wake_ai_lane_worker()
    except OperationJobPermanentError as exc:
        _fail_claimed_job(job_id, worker_id=worker_id, exc=exc, retry=False, backoff_seconds=failure_backoff_seconds, now=now)
    except Exception as exc:
        _fail_claimed_job(
            job_id,
            worker_id=worker_id,
            exc=exc,
            retry=_is_retryable_operation_exception(exc),
            backoff_seconds=failure_backoff_seconds,
            now=now,
        )
    finally:
        heartbeat.stop()


def _is_retryable_operation_exception(exc: Exception) -> bool:
    if isinstance(exc, OperationJobRetryableError):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, socket.herror, socket.timeout))


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _log_operation_job_handler_timing(
    operation: AppOperation,
    job: Job,
    *,
    worker_id: str,
    handler_run_ms: int,
    complete_job_ms: int,
    sync_operation_ms: int,
    total_job_ms: int | None,
) -> None:
    if job.job_type not in {
        JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
        JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
        JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
    }:
        return
    log_operation_event(
        logger,
        "operation_job_handler_timing",
        operation=operation,
        job=job,
        worker_id=worker_id,
        feed_item_id=_payload_int(job.payload_json or {}, "feed_item_id"),
        source_id=_payload_int(job.payload_json or {}, "source_id"),
        source_sync_operation_id=_payload_int(job.payload_json or {}, "source_sync_operation_id"),
        ai_context=_feed_item_ai_log_context(job.payload_json or {}),
        timing_ms={
            "handler_run_ms": handler_run_ms,
            "complete_job_ms": complete_job_ms,
            "sync_operation_ms": sync_operation_ms,
            "job_complete_sync_ms": complete_job_ms + sync_operation_ms,
            **({"total_job_ms": total_job_ms} if total_job_ms is not None else {}),
        },
    )


def _job_running_total_ms(job: Job) -> int | None:
    if job.started_at is None:
        return None
    return max(0, int((_now_utc() - _as_utc(job.started_at)).total_seconds() * 1000))


def _is_retryable_source_sync_response(response: SingleSourceSyncResponse) -> bool:
    fetch = response.fetch
    if fetch.status != "error":
        return False
    error_message = fetch.error_message or ""
    if error_message.startswith("fetch_failed:"):
        return True
    if error_message.startswith("upstream_http_error:"):
        return fetch.http_status in RETRYABLE_SOURCE_SYNC_HTTP_STATUS_CODES
    return False


def _source_sync_retry_error_message(response: SingleSourceSyncResponse) -> str:
    fetch = response.fetch
    if fetch.http_status is not None:
        return (
            f"来源刷新访问失败 source_id={response.source_id}: "
            f"HTTP {fetch.http_status}: {fetch.error_message}"
        )
    return f"来源刷新访问失败 source_id={response.source_id}: {fetch.error_message}"


def _run_source_sync_one_source_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    source_id = _payload_int(payload, "source_id")
    if source_id is None:
        raise OperationJobPermanentError("source_id is required for source refresh jobs")
    source_index = _payload_int(payload, "source_index") or 1
    source = session.get(Source, source_id)
    if source is None:
        raise OperationJobPermanentError("Source not found")

    _write_source_sync_running_progress(
        session,
        operation,
        source_name=source.name,
        source_index=source_index,
    )
    session.commit()

    response = sync_one_source(session, source_id)
    if _is_retryable_source_sync_response(response) and job.attempts < job.max_attempts:
        session.commit()
        raise OperationJobRetryableError(_source_sync_retry_error_message(response))
    if response.fetch.status == "success":
        rebuild_homepage_read_models_for_source(session, source_id)
    _append_source_sync_result(session, operation.id, source_index=source_index, response=response)
    _enqueue_source_sync_created_item_artifact_jobs(
        session,
        source_operation_id=operation.id,
        response=response,
    )
    session.flush()


def _run_feed_item_extract_job(session: Session, job: Job, operation: AppOperation) -> None:
    feed_item_id = _payload_int(job.payload_json or {}, "feed_item_id")
    if feed_item_id is None:
        raise OperationJobPermanentError("feed_item_id is required for feed item extract jobs")

    _write_running_progress(session, operation, stage="extracting", completed=0, message="Extracting article content.")
    session.commit()

    item = extract_reader_item_content(session, feed_item_id)
    session.flush()
    if item is None:
        raise OperationJobPermanentError("Feed item not found")

    operation.result_json = {"feed_item_id": feed_item_id, "feed_item": item.model_dump(mode="json")}
    operation.message = f"Article content extracted: {item.title}"
    session.add(operation)
    session.flush()


def _run_feed_item_insight_artifacts_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    _validate_feed_item_insight_enqueue_context(session, payload, operation_id=operation.id, feed_item_id=None)
    scope = payload.get("scope") if isinstance(payload.get("scope"), str) else None
    source_id = _payload_int(payload, "source_id")
    limit = _payload_int(payload, "limit")
    source_sync_operation_id = None
    source_sync_item_source_ids: dict[int, int] = {}
    if scope == "source_sync_created_feed_items":
        source_sync_operation_id = _payload_int(payload, "source_sync_operation_id")
        source_sync_item_source_ids = _source_sync_created_feed_item_source_ids(session, source_sync_operation_id, limit)
        feed_item_ids = list(source_sync_item_source_ids)
        label = "Feed item insight artifacts: source refresh created feed items"
        stats_scope = f"source-sync:{source_sync_operation_id}"
    else:
        candidates = list_missing_feed_item_insight_artifact_candidates(session, source_id=source_id, limit=limit)
        feed_item_ids = [candidate.feed_item_id for candidate in candidates]
        label = f"Feed item insight artifacts: {_source_label(session, source_id)}"
        stats_scope = f"source:{source_id}" if source_id is not None else "all"
    progress = _initial_insight_progress(label, len(feed_item_ids))
    _write_feed_item_insight_artifact_progress(session, operation, progress)
    session.commit()

    if not feed_item_ids:
        operation.result_json = _feed_item_insight_artifact_result(scope=stats_scope)
        operation.message = (
            "No source refresh created feed items need feed item insight artifacts."
            if scope == "source_sync_created_feed_items"
            else (
                "No missing feed item insight artifacts for selected source."
                if source_id is not None
                else "No missing feed item insight artifacts."
            )
        )
        session.add(operation)
        session.flush()
        return

    processed_feed_item_count = 0
    artifact_rows_created = 0
    failed_feed_item_count = 0
    stopped = False
    for feed_item_id in feed_item_ids:
        session.refresh(operation)
        if operation.stop_requested:
            stopped = True
            progress["stopped"] = True
            progress["stop_requested"] = True
            progress["stage"] = "stopped"
            _write_feed_item_insight_artifact_progress(session, operation, progress)
            session.commit()
            break
        if _has_global_source_profile_snapshot(payload):
            _validate_feed_item_insight_enqueue_context(
                session,
                payload,
                operation_id=operation.id,
                feed_item_id=feed_item_id,
            )
        item_stats = _build_one_feed_item_insight_artifact(session, feed_item_id, stats_scope)
        _record_batch_artifact_build_job_result(
            session,
            job,
            source_id=source_sync_item_source_ids.get(feed_item_id),
            stats=item_stats,
        )
        _record_source_sync_artifact_build_result(
            session,
            source_sync_operation_id=source_sync_operation_id,
            source_id=source_sync_item_source_ids.get(feed_item_id),
            stats=item_stats,
        )
        processed_feed_item_count += item_stats.processed_feed_item_count
        artifact_rows_created += item_stats.artifact_rows_created
        failed_feed_item_count += item_stats.failed_feed_item_count
        progress["current_insight_completed"] += 1
        progress["insight_completed_count"] += 1
        progress["current_insight_failed"] += item_stats.failed_feed_item_count
        progress["insight_failed_count"] += item_stats.failed_feed_item_count
        progress["current_insight_artifact_rows_created"] += item_stats.artifact_rows_created
        progress["insight_artifact_rows_created"] += item_stats.artifact_rows_created
        _write_feed_item_insight_artifact_progress(session, operation, progress)
        session.commit()

    result = _feed_item_insight_artifact_result(
        scope=stats_scope,
        processed_feed_item_count=processed_feed_item_count,
        artifact_rows_created=artifact_rows_created,
        failed_feed_item_count=failed_feed_item_count,
        stopped=stopped,
    )
    if stopped:
        operation.result_json = result
        operation.message = "Insight artifact fill stopped."
    else:
        progress["completed"] = 1
        progress["success_count"] = 0 if failed_feed_item_count else 1
        progress["error_count"] = 1 if failed_feed_item_count else 0
        progress["current_source_name"] = None
        progress["current_source_index"] = None
        progress["stage"] = "completed"
        _write_feed_item_insight_artifact_progress(session, operation, progress)
        operation.result_json = result
        operation.message = (
            f"Processed {processed_feed_item_count} feed items, "
            f"created {artifact_rows_created} insight artifact rows, "
            f"failed {failed_feed_item_count} feed items."
        )
    session.add(operation)
    session.flush()


def _run_source_sync_artifact_build_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    feed_item_id = _payload_int(payload, "feed_item_id")
    source_id = _payload_int(payload, "source_id")
    source_sync_operation_id = _payload_int(payload, "source_sync_operation_id")
    if feed_item_id is None:
        raise OperationJobPermanentError("feed_item_id is required for source refresh artifact build jobs")
    _validate_feed_item_insight_enqueue_context(
        session,
        payload,
        operation_id=operation.id,
        feed_item_id=feed_item_id,
    )

    stats_scope = (
        f"source-sync:{source_sync_operation_id}"
        if source_sync_operation_id is not None
        else (
            f"source:{_payload_int(payload, 'source_id')}"
            if _payload_int(payload, "source_id") is not None
            else "all"
        )
    )
    last_attempt_id = _latest_insight_attempt_id(session, feed_item_id)
    stats = build_feed_item_insight_artifacts_for_item(
        session,
        feed_item_id=feed_item_id,
        mode=OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING,
        scope=stats_scope,
    )
    session.flush()
    if stats.failed_feed_item_count and _has_retryable_feed_item_artifact_attempt(
        session,
        feed_item_id=feed_item_id,
        after_id=last_attempt_id,
    ):
        if job.attempts < job.max_attempts:
            _log_feed_item_insight_artifact_build_timing(
                operation,
                job,
                feed_item_id=feed_item_id,
                source_id=source_id,
                source_sync_operation_id=source_sync_operation_id,
                stats=stats,
            )
            session.commit()
            raise OperationJobRetryableError("Feed item insight artifact build job failed.")

    result_update_started_at = time.perf_counter()
    _record_artifact_build_job_payload_result(session, job, stats)
    _record_artifact_build_job_result(
        session,
        operation.id,
        stats,
        source_sync_operation_id=source_sync_operation_id,
        source_id=source_id,
    )
    timing_ms = _artifact_timing_ms(stats, operation_result_update_ms=_elapsed_ms(result_update_started_at))
    if stats.processed_feed_item_count and not stats.failed_feed_item_count:
        read_model_started_at = time.perf_counter()
        rebuild_homepage_read_models_for_feed_items(session, [feed_item_id])
        timing_ms["homepage_read_model_rebuild_ms"] = _elapsed_ms(read_model_started_at)
    _log_feed_item_insight_artifact_build_timing(
        operation,
        job,
        feed_item_id=feed_item_id,
        source_id=source_id,
        source_sync_operation_id=source_sync_operation_id,
        stats=stats,
        timing_ms=timing_ms,
    )


def _run_knowledge_capture_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    saved_item_id = _payload_int(payload, "saved_item_id")
    if saved_item_id is None:
        raise OperationJobPermanentError("saved_item_id is required for knowledge capture jobs")
    capture_mode = payload.get("capture_mode")
    if capture_mode not in {
        OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_URL,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_UPLOADED_MATERIAL,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_IMPORT,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_RETRY,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_FEED_SAVED,
    }:
        raise OperationJobPermanentError("capture_mode is invalid for knowledge capture jobs")

    def write_capture_progress(stage: str, completed: int, message: str) -> None:
        _write_running_progress(session, operation, stage=stage, completed=completed, message=message)
        session.commit()

    source_capture_run_id: int | None = None
    if capture_mode in {
        OPERATION_KIND_KNOWLEDGE_CAPTURE_UPLOADED_MATERIAL,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_IMPORT,
    }:
        _write_running_progress(
            session,
            operation,
            stage="processing_upload",
            completed=0,
            message="Processing uploaded material.",
        )
        session.commit()
        detail = execute_uploaded_material_capture(
            session,
            saved_item_id,
            export_local_mirror=False,
            source_asset_id=_payload_int(payload, "source_asset_id"),
        )
        if detail is None:
            raise OperationJobPermanentError("Saved item not found")
    elif capture_mode == OPERATION_KIND_KNOWLEDGE_CAPTURE_RETRY:
        detail = retry_saved_item_capture(
            session,
            saved_item_id,
            progress_callback=write_capture_progress,
            export_local_mirror=False,
        )
        if detail is None:
            raise OperationJobPermanentError("Saved item not found")
        latest_capture_run = _latest_capture_run_for_saved_item(session, saved_item_id)
        source_capture_run_id = latest_capture_run.id if latest_capture_run is not None else None
    else:
        capture_run_id = _payload_int(payload, "capture_run_id")
        if capture_run_id is None:
            _write_running_progress(
                session,
                operation,
                stage="capture_skipped",
                completed=1,
                message="No new web capture is required.",
            )
            session.flush()
            source_capture_run_id = _operation_scope_int(operation, "mirror_export_capture_run_id")
        else:
            capture_run = execute_saved_item_capture_run(
                session,
                saved_item_id=saved_item_id,
                capture_run_id=capture_run_id,
                progress_callback=write_capture_progress,
                export_local_mirror=False,
            )
            if capture_run is None:
                raise OperationJobPermanentError("Capture run not found")
            source_capture_run_id = capture_run.id
        detail = get_saved_item_detail(session, saved_item_id)
        if detail is None:
            raise OperationJobPermanentError("Saved item not found after capture")

    enqueue_knowledge_capture_local_mirror_job(
        session,
        operation,
        saved_item_id=saved_item_id,
        capture_mode=capture_mode,
        source_capture_run_id=source_capture_run_id,
    )
    operation.result_json = {"saved_item_id": saved_item_id, "saved_item": detail.model_dump(mode="json")}
    operation.message = "Knowledge capture step completed; local mirror export is queued."
    session.add(operation)
    session.flush()


def _run_knowledge_capture_local_mirror_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    saved_item_id = _payload_int(payload, "saved_item_id")
    if saved_item_id is None:
        raise OperationJobPermanentError("saved_item_id is required for knowledge capture mirror jobs")
    capture_mode = payload.get("capture_mode")

    _write_running_progress(
        session,
        operation,
        stage="mirror_export",
        completed=1,
        message="Exporting KnowledgeBase Local Mirror.",
    )
    session.commit()

    detail = None
    if capture_mode in {
        OPERATION_KIND_KNOWLEDGE_CAPTURE_UPLOADED_MATERIAL,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_IMPORT,
    }:
        saved_item = session.get(SavedItem, saved_item_id)
        if saved_item is None:
            raise OperationJobPermanentError("Saved item not found")
        if saved_item.processing_status == PROCESSING_STATUS_SUCCEEDED:
            detail = export_saved_item_local_mirror(session, saved_item_id)
        else:
            detail = get_saved_item_detail(session, saved_item_id)
    else:
        source_capture_run_id = _payload_int(payload, "source_capture_run_id")
        capture_run = (
            session.get(SavedItemCaptureRun, source_capture_run_id)
            if source_capture_run_id is not None
            else _latest_capture_run_for_saved_item(session, saved_item_id)
        )
        if capture_run is not None and capture_run.saved_item_id != saved_item_id:
            raise OperationJobPermanentError("Capture run does not belong to saved item")
        if capture_run is not None and capture_run.status in {CAPTURE_STATUS_SUCCEEDED, CAPTURE_STATUS_PARTIAL_FAILED}:
            export_saved_item_after_capture(session, saved_item_id=saved_item_id, capture_run_id=capture_run.id)
        detail = get_saved_item_detail(session, saved_item_id)

    if detail is None:
        raise OperationJobPermanentError("Saved item not found after local mirror export")

    operation.result_json = {"saved_item_id": saved_item_id, "saved_item": detail.model_dump(mode="json")}
    operation.message = _knowledge_capture_complete_message(operation)
    session.add(operation)
    session.flush()


def _run_local_mirror_export_job(session: Session, job: Job, operation: AppOperation) -> None:
    saved_item_id = _payload_int(job.payload_json or {}, "saved_item_id")
    if saved_item_id is None:
        raise OperationJobPermanentError("saved_item_id is required for local mirror export jobs")

    _write_running_progress(session, operation, stage="mirror_export", completed=0, message="Exporting local mirror.")
    session.commit()

    detail = export_saved_item_local_mirror(session, saved_item_id)
    session.flush()
    if detail is None:
        raise OperationJobPermanentError("Saved item not found")

    operation.result_json = {"saved_item_id": saved_item_id, "saved_item": detail.model_dump(mode="json")}
    operation.message = "Local mirror export operation completed."
    session.add(operation)
    session.flush()


def _run_feed_item_insight_artifact_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    feed_item_id = _payload_int(payload, "feed_item_id")
    if feed_item_id is None:
        raise OperationJobPermanentError("feed_item_id is required for feed item artifact jobs")
    mode = payload.get("mode")
    if mode not in {OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING, OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_REBUILD}:
        raise OperationJobPermanentError("mode must be build_missing or rebuild for feed item artifact jobs")
    _validate_feed_item_insight_enqueue_context(
        session,
        payload,
        operation_id=operation.id,
        feed_item_id=feed_item_id,
    )

    _write_running_progress(session, operation, stage="insight", completed=0, message="Processing feed item insight artifacts.")
    session.commit()

    last_attempt_id = _latest_insight_attempt_id(session, feed_item_id)
    if mode == OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING:
        stats = build_feed_item_insight_artifacts_for_item(
            session,
            feed_item_id=feed_item_id,
            mode=OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING,
            scope=f"feed_item:{feed_item_id}:missing",
        )
    else:
        stats = build_feed_item_insight_artifacts_for_item(
            session,
            feed_item_id=feed_item_id,
            mode=OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_REBUILD,
            scope=f"feed_item:{feed_item_id}",
        )
    session.flush()
    if stats.failed_feed_item_count and _has_retryable_feed_item_artifact_attempt(
        session,
        feed_item_id=feed_item_id,
        after_id=last_attempt_id,
    ):
        _log_feed_item_insight_artifact_build_timing(operation, job, feed_item_id=feed_item_id, stats=stats)
        session.commit()
        raise OperationJobRetryableError("Feed item insight artifact operation failed.")

    result_update_started_at = time.perf_counter()
    operation.result_json = {"feed_item_id": feed_item_id, **_feed_item_insight_artifact_result_from_stats(stats)}
    operation.message = "Feed item insight artifact operation completed."
    session.add(operation)
    timing_ms = _artifact_timing_ms(stats, operation_result_update_ms=_elapsed_ms(result_update_started_at))
    if stats.processed_feed_item_count and not stats.failed_feed_item_count:
        read_model_started_at = time.perf_counter()
        rebuild_homepage_read_models_for_feed_items(session, [feed_item_id])
        timing_ms["homepage_read_model_rebuild_ms"] = _elapsed_ms(read_model_started_at)
    _log_feed_item_insight_artifact_build_timing(
        operation,
        job,
        feed_item_id=feed_item_id,
        stats=stats,
        timing_ms=timing_ms,
    )
    session.flush()


def _run_saved_item_insight_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    saved_item_id = _payload_int(payload, "saved_item_id")
    if saved_item_id is None:
        raise OperationJobPermanentError("saved_item_id is required for saved item insight jobs")
    _validate_saved_item_insight_enqueue_context(session, payload, saved_item_id=saved_item_id)
    ai_route = payload.get("ai_route")
    if ai_route == "saved_item":
        ai_route = None

    _write_running_progress(session, operation, stage="insight", completed=0, message="Rebuilding saved item insight.")
    session.commit()

    try:
        artifact = rebuild_saved_item_insight(session, saved_item_id, ai_route=ai_route if isinstance(ai_route, str) else None)
    except SavedItemInsightConfigurationError as exc:
        raise OperationJobPermanentError(str(exc)) from exc
    if artifact is None:
        raise OperationJobPermanentError("Saved item not found")
    session.flush()
    detail = get_saved_item_detail(session, saved_item_id)
    if detail is None:
        raise OperationJobPermanentError("Saved item not found after insight rebuild")

    operation.result_json = {"saved_item_id": saved_item_id, "saved_item": detail.model_dump(mode="json")}
    operation.message = "Saved item insight operation completed."
    session.add(operation)
    session.flush()


def _run_channel_reroute_item_job(session: Session, job: Job, operation: AppOperation) -> None:
    payload = job.payload_json or {}
    feed_item_id = _payload_int(payload, "feed_item_id")
    channel_catalog_version = _payload_int(payload, "channel_catalog_version")
    operation_id = _payload_int(payload, "operation_id") or operation.id
    retired_channel_id = payload.get("retired_channel_id")
    fallback_channel_id = payload.get("fallback_channel_id")
    reason = payload.get("reason")
    if feed_item_id is None:
        raise OperationJobPermanentError("feed_item_id is required for channel reroute jobs")
    if channel_catalog_version is None:
        raise OperationJobPermanentError("channel_catalog_version is required for channel reroute jobs")
    if not isinstance(retired_channel_id, str) or not retired_channel_id:
        raise OperationJobPermanentError("retired_channel_id is required for channel reroute jobs")
    if not isinstance(fallback_channel_id, str) or not fallback_channel_id:
        raise OperationJobPermanentError("fallback_channel_id is required for channel reroute jobs")

    _write_running_progress(session, operation, stage="channel_reroute", completed=0, message="Rerouting retired channel items.")
    session.commit()

    result = reroute_feed_item_for_retired_channel(
        session,
        feed_item_id=feed_item_id,
        retired_channel_id=retired_channel_id,
        fallback_channel_id=fallback_channel_id,
        channel_catalog_version=channel_catalog_version,
        operation_id=operation_id or operation.id,
        reason=reason if isinstance(reason, str) and reason else "channel_retired",
    )
    _record_channel_reroute_result(session, operation, result)
    rebuild_homepage_read_models_for_feed_items(session, [feed_item_id])


def _record_channel_reroute_result(session: Session, operation: AppOperation, result) -> None:
    progress = dict(operation.progress_json or {})
    progress["stage"] = "channel_reroute"
    progress["current_insight_completed"] = _safe_int(progress.get("current_insight_completed")) + 1
    progress["insight_completed_count"] = _safe_int(progress.get("insight_completed_count")) + 1
    progress["current_insight_artifact_rows_created"] = _safe_int(
        progress.get("current_insight_artifact_rows_created")
    ) + (1 if result.artifact_created else 0)
    progress["insight_artifact_rows_created"] = _safe_int(progress.get("insight_artifact_rows_created")) + (
        1 if result.artifact_created else 0
    )
    previous_result = operation.result_json if isinstance(operation.result_json, dict) else {}
    artifact_rows_created = _safe_int(previous_result.get("artifact_rows_created")) + (
        1 if result.artifact_created else 0
    )
    skipped_feed_item_count = _safe_int(previous_result.get("skipped_feed_item_count")) + (
        0 if result.artifact_created else 1
    )
    operation.result_json = {
        **previous_result,
        "artifact_rows_created": artifact_rows_created,
        "skipped_feed_item_count": skipped_feed_item_count,
        "latest_feed_item_id": result.feed_item_id,
        "latest_primary_channel_id": result.new_primary_channel_id,
    }
    operation.message = (
        f"Channel reroute progress: created {artifact_rows_created} routing artifacts, "
        f"skipped {skipped_feed_item_count} items."
    )
    operation.progress_json = progress
    operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
    operation.updated_at = _now_utc()
    session.add(operation)
    session.flush()


def _fail_claimed_job(
    job_id: int,
    *,
    worker_id: str,
    exc: Exception,
    retry: bool,
    backoff_seconds: int | None,
    now: datetime | None,
) -> None:
    with get_session_factory()() as session:
        try:
            job = fail_job(
                session,
                job_id,
                worker_id=worker_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc) or exc.__class__.__name__,
                error_detail={"job_id": job_id},
                retry=retry,
                backoff_seconds=backoff_seconds,
                now=now,
            )
            operation = session.get(AppOperation, job.operation_id) if job.operation_id is not None else None
            if operation is not None:
                log_operation_event(
                    logger,
                    "operation_job_failure_synced",
                    operation=operation,
                    job=job,
                    worker_id=worker_id,
                )
                sync_job_backed_operation_state(session, operation, now=now)
            session.commit()
        except JobLeaseError:
            session.rollback()
            raise


def _finish_cancelled_if_requested(
    session: Session,
    job: Job,
    operation: AppOperation,
    *,
    worker_id: str,
    now: datetime | None,
) -> None:
    if operation.stop_requested and not job.cancel_requested and job.status == JOB_STATUS_RUNNING:
        cancel_job(session, job.id, reason="operation_stop_requested")
        session.flush()
        session.refresh(job)
    if job.cancel_requested and job.status == JOB_STATUS_RUNNING:
        finish_running_job_cancelled(
            session,
            job.id,
            worker_id=worker_id,
            reason="operation_stop_requested",
            now=now,
        )
        sync_job_backed_operation_state(session, operation, now=now)


def _job_success_overrides_late_cancel(job: Job, operation: AppOperation) -> bool:
    if operation.operation_type != OPERATION_TYPE_KNOWLEDGE_CAPTURE:
        return False
    return job.cancel_requested and job.status == JOB_STATUS_RUNNING


def request_stop_source_sync_artifact_build(session: Session, source_sync_operation_id: int) -> AppOperation | None:
    operation = _existing_source_sync_artifact_followup(session, source_sync_operation_id)
    if operation is None or operation.status not in {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}:
        return operation
    operation.stop_requested = True
    if operation.status in {STATUS_QUEUED, STATUS_RUNNING}:
        operation.status = STATUS_STOPPING
    progress = dict(operation.progress_json or {})
    progress["stop_requested"] = True
    if progress.get("stage") not in {"stopped", "completed"}:
        progress["stage"] = "stopping"
    operation.progress_json = progress
    operation.message = "Stop requested; waiting for feed item insight artifact jobs to finish."
    operation.updated_at = _now_utc()
    session.add(operation)
    session.flush()
    cancel_active_jobs_for_operation(session, operation)
    session.refresh(operation)
    return operation


def _enqueue_source_sync_created_item_artifact_jobs(
    session: Session,
    *,
    source_operation_id: int | None,
    response: SingleSourceSyncResponse,
) -> None:
    if source_operation_id is None or not response.created_feed_item_ids:
        return
    source = session.get(Source, response.source_id)
    if source is None or source.ai_route == "disabled":
        return
    source_operation = _lock_operation_for_update(session, source_operation_id)
    if source_operation is None or source_operation.stop_requested:
        return
    scope = source_operation.scope_json or {}
    build_limit = scope.get("feed_item_insight_artifact_build_limit")
    if _limit_item_ids([1], build_limit) == []:
        return

    artifact_operation = _ensure_source_sync_artifact_build_operation(session, source_operation)
    if artifact_operation.status not in {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}:
        return
    if artifact_operation.stop_requested:
        return

    remaining_limit = _remaining_source_sync_artifact_limit(session, artifact_operation, build_limit)
    if remaining_limit is not None and remaining_limit <= 0:
        return
    selected_item_ids = _limit_item_ids(response.created_feed_item_ids, remaining_limit)
    if not selected_item_ids:
        return

    created_count = 0
    for feed_item_id in selected_item_ids:
        _, created = enqueue_source_sync_artifact_build_job(
            session,
            artifact_operation,
            source_sync_operation_id=source_operation.id,
            feed_item_id=feed_item_id,
            source_id=response.source_id,
        )
        if created:
            created_count += 1
    if created_count:
        _extend_artifact_build_job_total(session, artifact_operation.id, created_count)


def _ensure_source_sync_artifact_build_operation(
    session: Session,
    source_operation: AppOperation,
) -> AppOperation:
    if source_operation.id is None:
        session.flush()
    existing = _existing_source_sync_artifact_followup(session, source_operation.id)
    if existing is not None:
        return existing
    scope = source_operation.scope_json or {}
    build_limit = scope.get("feed_item_insight_artifact_build_limit")
    operation = AppOperation(
        operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
        operation_kind=OPERATION_KIND_SOURCE_SYNC_CREATED_FEED_ITEMS,
        status=STATUS_QUEUED,
        source_id=source_operation.source_id,
        scope_json={
            "scope": OPERATION_KIND_SOURCE_SYNC_CREATED_FEED_ITEMS,
            "source_sync_operation_id": source_operation.id,
            "source_id": source_operation.source_id,
            "limit": build_limit,
        },
        progress_json=_initial_insight_progress("Feed item insight artifacts: source refresh created feed items", 0),
        result_json=_feed_item_insight_artifact_result(scope=f"source-sync:{source_operation.id}"),
        message="Feed item insight artifact operation queued for source refresh created feed items.",
        runner_id=PROCESS_WORKER_ID,
    )
    session.add(operation)
    session.flush()
    return operation


def _remaining_source_sync_artifact_limit(
    session: Session,
    operation: AppOperation,
    build_limit: Any,
) -> int | None:
    normalized_limit = _limit_int(build_limit)
    if normalized_limit is None:
        return None
    existing_total = _artifact_build_job_total(session, operation)
    return max(0, normalized_limit - existing_total)


def _artifact_build_job_total(session: Session, operation: AppOperation) -> int:
    progress = operation.progress_json if isinstance(operation.progress_json, dict) else {}
    progress_total = _safe_int(progress.get("insight_total_count"))
    if operation.id is None:
        return progress_total
    job_total = int(
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.operation_id == operation.id)
            .where(Job.job_type == JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD)
        )
        or 0
    )
    return max(progress_total, job_total)


def _extend_artifact_build_job_total(session: Session, operation_id: int | None, count: int) -> None:
    if operation_id is None or count <= 0:
        return
    operation = _lock_operation_for_update(session, operation_id)
    if operation is None:
        return
    progress = _insight_progress(operation)
    progress["current_insight_total"] += count
    progress["insight_total_count"] += count
    operation.progress_json = progress
    operation.updated_at = _now_utc()
    session.add(operation)
    session.flush()


def _record_artifact_build_job_result(
    session: Session,
    operation_id: int | None,
    stats: InsightEngineArtifactRebuildStats,
    *,
    source_sync_operation_id: int | None = None,
    source_id: int | None = None,
) -> None:
    if operation_id is None:
        return
    operation = _lock_operation_for_update(session, operation_id)
    if operation is None:
        return
    progress = _insight_progress(operation)
    progress["current_insight_completed"] += 1
    progress["insight_completed_count"] += 1
    progress["current_insight_failed"] += stats.failed_feed_item_count
    progress["insight_failed_count"] += stats.failed_feed_item_count
    progress["current_insight_artifact_rows_created"] += stats.artifact_rows_created
    progress["insight_artifact_rows_created"] += stats.artifact_rows_created
    progress["success_count"] = 0 if progress["insight_failed_count"] else 1
    progress["error_count"] = 1 if progress["insight_failed_count"] else 0
    progress["stage"] = "insight"
    progress["stop_requested"] = bool(operation.stop_requested)
    previous_result = operation.result_json if isinstance(operation.result_json, dict) else {}
    operation.result_json = _feed_item_insight_artifact_result(
        scope=str(previous_result.get("scope") or stats.scope),
        processed_feed_item_count=_safe_int(previous_result.get("processed_feed_item_count"))
        + stats.processed_feed_item_count,
        artifact_rows_created=_safe_int(previous_result.get("artifact_rows_created")) + stats.artifact_rows_created,
        failed_feed_item_count=_safe_int(previous_result.get("failed_feed_item_count")) + stats.failed_feed_item_count,
        stopped=bool(previous_result.get("stopped")),
    )
    operation.progress_json = progress
    operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
    operation.started_at = operation.started_at or _now_utc()
    operation.updated_at = _now_utc()
    operation.message = (
        f"Processed {operation.result_json['processed_feed_item_count']} feed items, "
        f"created {operation.result_json['artifact_rows_created']} insight artifact rows, "
        f"failed {operation.result_json['failed_feed_item_count']} feed items."
    )
    session.add(operation)
    session.flush()
    _record_source_sync_artifact_build_result(
        session,
        source_sync_operation_id=source_sync_operation_id,
        source_id=source_id,
        stats=stats,
    )


def _record_artifact_build_job_payload_result(
    session: Session,
    job: Job,
    stats: InsightEngineArtifactRebuildStats,
) -> None:
    payload = dict(job.payload_json or {})
    payload["artifact_build_stats"] = _artifact_build_stats_payload(stats)
    job.payload_json = payload
    job.redacted_payload_json = sanitized_operation_payload(payload) or {}
    session.add(job)
    session.flush()


def _record_batch_artifact_build_job_result(
    session: Session,
    job: Job,
    *,
    source_id: int | None,
    stats: InsightEngineArtifactRebuildStats,
) -> None:
    if source_id is None:
        return
    payload = dict(job.payload_json or {})
    source_stats = payload.get("source_sync_artifact_source_stats")
    source_stats = dict(source_stats) if isinstance(source_stats, dict) else {}
    key = str(source_id)
    current = _artifact_build_stats_from_payload(source_stats.get(key))
    source_stats[key] = {
        "processed_feed_item_count": current["processed_feed_item_count"] + stats.processed_feed_item_count,
        "artifact_rows_created": current["artifact_rows_created"] + stats.artifact_rows_created,
        "failed_feed_item_count": current["failed_feed_item_count"] + stats.failed_feed_item_count,
    }
    payload["source_sync_artifact_source_stats"] = source_stats
    job.payload_json = payload
    job.redacted_payload_json = sanitized_operation_payload(payload) or {}
    session.add(job)
    session.flush()


def _artifact_build_stats_payload(stats: InsightEngineArtifactRebuildStats) -> dict[str, int]:
    return {
        "processed_feed_item_count": stats.processed_feed_item_count,
        "artifact_rows_created": stats.artifact_rows_created,
        "failed_feed_item_count": stats.failed_feed_item_count,
    }


def _artifact_build_stats_from_payload(raw_stats: Any) -> dict[str, int]:
    stats = raw_stats if isinstance(raw_stats, dict) else {}
    return {
        "processed_feed_item_count": _safe_int(stats.get("processed_feed_item_count")),
        "artifact_rows_created": _safe_int(stats.get("artifact_rows_created")),
        "failed_feed_item_count": _safe_int(stats.get("failed_feed_item_count")),
    }


def _record_source_sync_artifact_build_result(
    session: Session,
    *,
    source_sync_operation_id: int | None,
    source_id: int | None,
    stats: InsightEngineArtifactRebuildStats,
) -> None:
    del source_id, stats
    if source_sync_operation_id is None:
        return
    _refresh_source_sync_artifact_build_results(session, source_sync_operation_id)


def _refresh_source_sync_artifact_build_results(session: Session, source_sync_operation_id: int) -> None:
    with _source_sync_artifact_result_lock:
        _refresh_source_sync_artifact_build_results_unlocked(session, source_sync_operation_id)


def _refresh_source_sync_artifact_build_results_unlocked(session: Session, source_sync_operation_id: int) -> None:
    source_operation = _lock_operation_for_update(session, source_sync_operation_id)
    if source_operation is None or source_operation.operation_type != OPERATION_TYPE_SOURCE_SYNC:
        return
    progress = dict(source_operation.progress_json or {})
    entries = _source_sync_result_entries(progress)
    if not entries:
        return
    source_stats = _source_sync_artifact_stats_by_source(session, source_sync_operation_id)
    for entry in entries:
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        source_id = _entry_source_id(entry)
        result["feed_item_insight_artifacts"] = source_stats.get(
            source_id,
            {
                "processed_feed_item_count": 0,
                "artifact_rows_created": 0,
                "failed_feed_item_count": 0,
            },
        )
        entry["result"] = result
    progress["_source_sync_result_entries"] = entries
    previous_result = source_operation.result_json if isinstance(source_operation.result_json, dict) else {}
    source_operation.progress_json = progress
    source_operation.result_json = _build_source_sync_operation_result(
        source_operation,
        progress,
        stopped=bool(previous_result.get("stopped")),
    )
    source_operation.updated_at = _now_utc()
    session.add(source_operation)
    session.flush()


def _source_sync_artifact_stats_by_source(
    session: Session,
    source_sync_operation_id: int,
) -> dict[int, dict[str, int]]:
    operation = _existing_source_sync_artifact_followup(session, source_sync_operation_id)
    if operation is None or operation.id is None:
        return {}
    aggregate: dict[int, dict[str, int]] = {}
    jobs = session.scalars(select(Job).where(Job.operation_id == operation.id).order_by(Job.id.asc()))
    for job in jobs:
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        if _payload_int(payload, "source_sync_operation_id") != source_sync_operation_id:
            continue
        if job.job_type == JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD:
            source_id = _payload_int(payload, "source_id")
            if source_id is None:
                continue
            _add_artifact_stats(aggregate, source_id, _artifact_build_stats_from_payload(payload.get("artifact_build_stats")))
        elif job.job_type == JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS:
            source_stats = payload.get("source_sync_artifact_source_stats")
            if not isinstance(source_stats, dict):
                continue
            for raw_source_id, raw_stats in source_stats.items():
                try:
                    source_id = int(raw_source_id)
                except (TypeError, ValueError):
                    continue
                _add_artifact_stats(aggregate, source_id, _artifact_build_stats_from_payload(raw_stats))
    return aggregate


def _add_artifact_stats(
    aggregate: dict[int, dict[str, int]],
    source_id: int,
    stats: dict[str, int],
) -> None:
    current = aggregate.setdefault(
        source_id,
        {
            "processed_feed_item_count": 0,
            "artifact_rows_created": 0,
            "failed_feed_item_count": 0,
        },
    )
    current["processed_feed_item_count"] += stats["processed_feed_item_count"]
    current["artifact_rows_created"] += stats["artifact_rows_created"]
    current["failed_feed_item_count"] += stats["failed_feed_item_count"]


def _insight_progress(operation: AppOperation) -> dict[str, Any]:
    base = operation.progress_json if isinstance(operation.progress_json, dict) else {}
    progress = _initial_insight_progress(
        str(base.get("current_source_name") or "Feed item insight artifacts: source refresh created feed items"),
        _safe_int(base.get("insight_total_count")),
    )
    progress.update(base)
    return progress


def _existing_job_by_idempotency_key(session: Session, idempotency_key: str) -> Job | None:
    return session.scalar(
        select(Job)
        .where(Job.idempotency_key == idempotency_key)
        .order_by(Job.id.desc())
        .limit(1)
    )


def _source_sync_created_feed_item_ids(
    session: Session,
    source_sync_operation_id: int | None,
    limit: int | None,
) -> list[int]:
    return list(_source_sync_created_feed_item_source_ids(session, source_sync_operation_id, limit))


def _source_sync_created_feed_item_source_ids(
    session: Session,
    source_sync_operation_id: int | None,
    limit: int | None,
) -> dict[int, int]:
    if source_sync_operation_id is None:
        return {}
    source_operation = session.get(AppOperation, source_sync_operation_id)
    if source_operation is None:
        return {}
    progress = source_operation.progress_json if isinstance(source_operation.progress_json, dict) else {}
    progress_entries = _source_sync_result_entries(progress)
    if progress_entries:
        entries = [entry["result"] for entry in progress_entries]
    else:
        result = source_operation.result_json if isinstance(source_operation.result_json, dict) else {}
        entries = result.get("results") if isinstance(result.get("results"), list) else [result]
    item_source_ids: dict[int, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_id = _result_source_id(entry)
        if source_id is not None:
            source = session.get(Source, source_id)
            if source is not None and source.ai_route == "disabled":
                continue
        for item_id in entry.get("created_feed_item_ids") or []:
            try:
                normalized_item_id = int(item_id)
            except (TypeError, ValueError):
                continue
            if source_id is not None and normalized_item_id not in item_source_ids:
                item_source_ids[normalized_item_id] = source_id
    limited_item_ids = _limit_item_ids(list(item_source_ids), limit)
    return {item_id: item_source_ids[item_id] for item_id in limited_item_ids}


def _result_source_id(entry: dict[str, Any]) -> int | None:
    try:
        return int(entry.get("source_id"))
    except (TypeError, ValueError):
        return None


def _create_source_sync_artifact_followup_if_needed(session: Session, source_operation: AppOperation) -> AppOperation | None:
    if source_operation.id is None:
        return None
    if source_operation.operation_type != OPERATION_TYPE_SOURCE_SYNC or source_operation.status != STATUS_COMPLETED:
        return None
    if _existing_source_sync_artifact_followup(session, source_operation.id) is not None:
        return None
    scope = source_operation.scope_json or {}
    build_limit = scope.get("feed_item_insight_artifact_build_limit")
    if _limit_item_ids([1], build_limit) == []:
        return None
    normalized_limit = _limit_int(build_limit)
    feed_item_source_ids = _source_sync_created_feed_item_source_ids(session, source_operation.id, normalized_limit)
    if not feed_item_source_ids:
        return None
    if _active_global_operation(session) is not None:
        return None

    operation = AppOperation(
        operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
        operation_kind="source_sync_created_feed_items",
        status=STATUS_QUEUED,
        source_id=source_operation.source_id,
        scope_json={
            "scope": "source_sync_created_feed_items",
            "source_sync_operation_id": source_operation.id,
            "source_id": source_operation.source_id,
            "limit": build_limit,
        },
        progress_json=_initial_insight_progress(
            "Feed item insight artifacts: source refresh created feed items",
            len(feed_item_source_ids),
        ),
        result_json=None,
        message="Feed item insight artifact operation queued for source refresh created feed items.",
        runner_id=PROCESS_WORKER_ID,
    )
    session.add(operation)
    session.flush()
    for feed_item_id, source_id in feed_item_source_ids.items():
        enqueue_source_sync_artifact_build_job(
            session,
            operation,
            source_sync_operation_id=source_operation.id,
            feed_item_id=feed_item_id,
            source_id=source_id,
        )
    session.flush()
    return operation


def _limit_item_ids(item_ids: Sequence[int], limit: Any) -> list[int]:
    unique_ids = list(dict.fromkeys(item_ids))
    normalized_limit = _limit_int(limit)
    if normalized_limit is None:
        return unique_ids
    return unique_ids[: max(0, normalized_limit)]


def _limit_int(limit: Any) -> int | None:
    if limit is None:
        return None
    try:
        return int(limit)
    except (TypeError, ValueError):
        return None


def _existing_source_sync_artifact_followup(session: Session, source_sync_operation_id: int) -> AppOperation | None:
    candidates = session.scalars(
        select(AppOperation)
        .where(AppOperation.operation_type.in_(FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES))
        .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        .order_by(AppOperation.id.desc())
    )
    for operation in candidates:
        scope = operation.scope_json or {}
        if _scope_int(scope, "source_sync_operation_id") == source_sync_operation_id:
            return operation
    return None


def _linked_source_sync_operation_is_active(session: Session, operation: AppOperation) -> bool:
    scope = operation.scope_json if isinstance(operation.scope_json, dict) else {}
    source_sync_operation_id = _scope_int(scope, "source_sync_operation_id")
    if source_sync_operation_id is None:
        return False
    source_operation = session.get(AppOperation, source_sync_operation_id)
    return bool(source_operation is not None and source_operation.status in {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING})


def _active_global_operation(session: Session) -> AppOperation | None:
    return session.scalars(
        select(AppOperation)
        .where(AppOperation.status.in_({STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}))
        .where(AppOperation.operation_type.in_({OPERATION_TYPE_SOURCE_SYNC, *FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES}))
        .order_by(AppOperation.created_at.desc(), AppOperation.id.desc())
        .limit(1)
    ).first()


def _scope_int(scope: dict[str, Any], key: str) -> int | None:
    value = scope.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _latest_capture_run_for_saved_item(session: Session, saved_item_id: int) -> SavedItemCaptureRun | None:
    return session.scalar(
        select(SavedItemCaptureRun)
        .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        .order_by(SavedItemCaptureRun.id.desc())
        .limit(1)
    )


def _compensate_knowledge_capture_terminal_state(
    session: Session,
    operation: AppOperation,
    *,
    reason: str,
) -> None:
    scope = operation.scope_json if isinstance(operation.scope_json, dict) else {}
    saved_item_id = _scope_int(scope, "saved_item_id")
    if saved_item_id is None:
        return
    safe_reason = (reason or "Capture operation did not complete.")[:1000]
    if operation.operation_kind in {
        OPERATION_KIND_KNOWLEDGE_CAPTURE_UPLOADED_MATERIAL,
        OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_IMPORT,
    }:
        saved_item = session.get(SavedItem, saved_item_id)
        if saved_item is not None and saved_item.processing_status == PROCESSING_STATUS_PENDING:
            saved_item.processing_status = PROCESSING_STATUS_FAILED
            saved_item.processing_error = safe_reason
            session.add(saved_item)
            session.flush()
        _refresh_knowledge_capture_operation_result(session, operation, saved_item_id)
        return

    capture_run_id = _scope_int(scope, "capture_run_id")
    capture_run = session.get(SavedItemCaptureRun, capture_run_id) if capture_run_id is not None else None
    if capture_run is None:
        capture_run = _latest_capture_run_for_saved_item(session, saved_item_id)
    if capture_run is not None and capture_run.status == CAPTURE_STATUS_PENDING:
        capture_run.status = CAPTURE_STATUS_FAILED
        capture_run.error_summary = safe_reason
        capture_run.finished_at = _now_utc()
        session.add(capture_run)
        session.flush()
    _refresh_knowledge_capture_operation_result(session, operation, saved_item_id)


def _refresh_knowledge_capture_operation_result(
    session: Session,
    operation: AppOperation,
    saved_item_id: int,
) -> None:
    detail = get_saved_item_detail(session, saved_item_id)
    if detail is None:
        return
    operation.result_json = {"saved_item_id": saved_item_id, "saved_item": detail.model_dump(mode="json")}
    session.add(operation)
    session.flush()


def _write_source_sync_running_progress(
    session: Session,
    operation: AppOperation,
    *,
    source_name: str,
    source_index: int,
) -> None:
    if operation.id is not None:
        locked_operation = _lock_operation_for_update(session, operation.id)
        if locked_operation is not None:
            operation = locked_operation
    progress = dict(operation.progress_json or {})
    progress.update(
        {
            "stage": "fetching",
            "current_source_name": source_name,
            "current_source_index": source_index,
            "current_fetch_status": None,
            "current_items_seen_count": 0,
            "current_items_created_count": 0,
            "current_items_matched_count": 0,
            "current_items_invalid_count": 0,
            "current_insight_total": 0,
            "current_insight_completed": 0,
            "current_insight_failed": 0,
            "current_insight_artifact_rows_created": 0,
            "stop_requested": bool(operation.stop_requested),
        }
    )
    operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
    operation.started_at = operation.started_at or _now_utc()
    operation.updated_at = _now_utc()
    operation.progress_json = progress
    operation.message = f"Syncing source: {source_name}"
    session.add(operation)
    session.flush()


def _lock_operation_for_update(session: Session, operation_id: int) -> AppOperation | None:
    return session.scalar(
        select(AppOperation)
        .where(AppOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _append_source_sync_result(
    session: Session,
    operation_id: int | None,
    *,
    source_index: int,
    response: SingleSourceSyncResponse,
) -> None:
    if operation_id is None:
        return
    operation = session.scalar(
        select(AppOperation)
        .where(AppOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if operation is None:
        return
    progress = dict(operation.progress_json or {})
    entries = [
        entry
        for entry in _source_sync_result_entries(progress)
        if _entry_source_id(entry) != response.source_id
    ]
    entries.append({"source_index": source_index, "result": response.model_dump(mode="json")})
    entries.sort(key=lambda entry: (_entry_source_index(entry), _entry_source_id(entry)))
    progress["_source_sync_result_entries"] = entries
    _apply_source_sync_response_to_progress(progress, response)
    operation.progress_json = progress
    operation.result_json = _build_source_sync_operation_result(operation, progress, stopped=False)
    operation.message = _build_source_sync_complete_message(operation.result_json)
    operation.updated_at = _now_utc()
    session.add(operation)
    session.flush()


def _source_sync_result_entries(progress: dict[str, Any]) -> list[dict[str, Any]]:
    raw_entries = progress.get("_source_sync_result_entries")
    if not isinstance(raw_entries, list):
        return []
    return [entry for entry in raw_entries if isinstance(entry, dict) and isinstance(entry.get("result"), dict)]


def _entry_source_id(entry: dict[str, Any]) -> int:
    result = entry.get("result")
    if isinstance(result, dict):
        try:
            return int(result.get("source_id") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _entry_source_index(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("source_index") or 0)
    except (TypeError, ValueError):
        return 0


def _apply_source_sync_response_to_progress(progress: dict[str, Any], response: SingleSourceSyncResponse) -> None:
    fetch = response.fetch
    progress["current_fetch_status"] = fetch.status
    progress["current_items_seen_count"] = fetch.items_seen_count
    progress["current_items_created_count"] = fetch.items_created_count
    progress["current_items_matched_count"] = fetch.items_matched_count
    progress["current_items_invalid_count"] = fetch.items_invalid_count
    progress["latest_source_name"] = response.source_name
    progress["latest_status"] = fetch.status
    progress["latest_error_message"] = fetch.error_message
    entries = _source_sync_result_entries(progress)
    progress["completed"] = len(entries)
    progress["items_seen_count"] = sum(_entry_fetch_int(entry, "items_seen_count") for entry in entries)
    progress["items_created_count"] = sum(_entry_fetch_int(entry, "items_created_count") for entry in entries)
    progress["items_matched_count"] = sum(_entry_fetch_int(entry, "items_matched_count") for entry in entries)
    progress["items_invalid_count"] = sum(_entry_fetch_int(entry, "items_invalid_count") for entry in entries)
    failed_sources = []
    for entry in entries:
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        fetch_result = result.get("fetch") if isinstance(result.get("fetch"), dict) else {}
        if fetch_result.get("status") == "error":
            failed_sources.append(
                {
                    "source_id": result.get("source_id"),
                    "source_name": result.get("source_name"),
                    "status": fetch_result.get("status"),
                    "http_status": fetch_result.get("http_status"),
                    "error_message": fetch_result.get("error_message"),
                }
            )
    progress["failed_sources"] = failed_sources


def _entry_fetch_int(entry: dict[str, Any], key: str) -> int:
    result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
    fetch = result.get("fetch") if isinstance(result.get("fetch"), dict) else {}
    try:
        return int(fetch.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _build_source_sync_operation_result(
    operation: AppOperation,
    progress: dict[str, Any],
    *,
    stopped: bool,
) -> dict[str, Any]:
    entries = _source_sync_result_entries(progress)
    results = [entry["result"] for entry in entries]
    kind = str((operation.scope_json or {}).get("kind") or operation.operation_kind)
    if kind in {"single", "auto_create"} and results:
        result = dict(results[-1])
        if stopped:
            result["stopped"] = True
        return result
    error_count = sum(
        1
        for result in results
        if isinstance(result.get("fetch"), dict) and result["fetch"].get("status") == "error"
    )
    source_count = int(progress.get("total") or len(results))
    payload = BulkSourceSyncResponse(
        source_count=source_count,
        success_count=len(results) - error_count,
        error_count=error_count,
        results=[SingleSourceSyncResponse.model_validate(result) for result in results],
    ).model_dump(mode="json")
    payload["stopped"] = stopped
    return payload


def _build_source_sync_complete_message(result: dict[str, Any]) -> str:
    if "results" not in result:
        fetch = result.get("fetch") or {}
        source_name = result.get("source_name") or "source"
        if fetch.get("status") == "error":
            return f"来源刷新失败：{source_name}"
        return (
            f"来源刷新完成：{source_name}；"
            f"新增 {fetch.get('items_created_count', 0)} 条。"
        )
    if result.get("source_count", 0) == 0:
        return "没有启用的来源可刷新。"
    item_totals = {"seen": 0, "created": 0, "matched": 0, "invalid": 0}
    for item in result.get("results", []):
        fetch = item.get("fetch") or {}
        item_totals["seen"] += fetch.get("items_seen_count", 0)
        item_totals["created"] += fetch.get("items_created_count", 0)
        item_totals["matched"] += fetch.get("items_matched_count", 0)
        item_totals["invalid"] += fetch.get("items_invalid_count", 0)
    status = "已停止" if result.get("stopped") else "已完成"
    return (
        f"来源刷新{status}：{result.get('success_count', 0)}/{result.get('source_count', 0)} 个来源成功，"
        f"{result.get('error_count', 0)} 个失败；"
        f"已见 {item_totals['seen']}，新增 {item_totals['created']}，匹配 {item_totals['matched']}，"
        f"无效 {item_totals['invalid']}。"
    )


def _write_running_progress(
    session: Session,
    operation: AppOperation,
    *,
    stage: str,
    completed: int,
    message: str,
) -> None:
    progress = dict(operation.progress_json or {})
    progress.update(
        {
            "stage": stage,
            "total": progress.get("total", 1),
            "completed": completed,
            "success_count": progress.get("success_count", 0),
            "error_count": progress.get("error_count", 0),
            "stopped": False,
            "stop_requested": bool(operation.stop_requested),
        }
    )
    operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
    operation.started_at = operation.started_at or _now_utc()
    operation.updated_at = _now_utc()
    operation.progress_json = progress
    operation.message = message
    session.add(operation)
    session.flush()


def _write_feed_item_insight_artifact_progress(session: Session, operation: AppOperation, progress: dict[str, Any]) -> None:
    session.refresh(operation)
    progress["stop_requested"] = bool(progress.get("stop_requested")) or operation.stop_requested
    if operation.stop_requested and progress.get("stage") not in {"stopped", "completed"}:
        progress["stage"] = "stopping" if progress.get("stage") == "idle" else progress.get("stage")
    operation.progress_json = progress
    operation.status = STATUS_STOPPING if operation.stop_requested else STATUS_RUNNING
    operation.started_at = operation.started_at or _now_utc()
    operation.updated_at = _now_utc()
    session.add(operation)
    session.flush()


def _initial_insight_progress(label: str, total: int) -> dict[str, Any]:
    return {
        "stage": "insight",
        "total": 1,
        "completed": 0,
        "success_count": 0,
        "error_count": 0,
        "stopped": False,
        "stop_requested": False,
        "current_source_name": label,
        "current_source_index": 1,
        "current_fetch_status": None,
        "current_items_seen_count": 0,
        "current_items_created_count": 0,
        "current_items_matched_count": 0,
        "current_items_invalid_count": 0,
        "current_insight_total": total,
        "current_insight_completed": 0,
        "current_insight_failed": 0,
        "current_insight_artifact_rows_created": 0,
        "items_seen_count": 0,
        "items_created_count": 0,
        "items_matched_count": 0,
        "items_invalid_count": 0,
        "insight_total_count": total,
        "insight_completed_count": 0,
        "insight_failed_count": 0,
        "insight_artifact_rows_created": 0,
        "latest_source_name": label,
        "latest_error_message": None,
    }


def _build_one_feed_item_insight_artifact(session: Session, feed_item_id: int, scope: str) -> InsightEngineArtifactRebuildStats:
    try:
        stats = build_feed_item_insight_artifacts_for_item(
            session,
            feed_item_id=feed_item_id,
            mode=OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING,
            scope=scope,
        )
        if stats.processed_feed_item_count and not stats.failed_feed_item_count:
            rebuild_homepage_read_models_for_feed_items(session, [feed_item_id])
        session.commit()
        return stats
    except Exception:
        session.rollback()
        return InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=0,
            artifact_rows_created=0,
            failed_feed_item_count=1,
        )


def _artifact_timing_ms(stats: InsightEngineArtifactRebuildStats, **extra_timing_ms: int) -> dict[str, int]:
    timing_ms = dict(stats.timing_ms or {})
    timing_ms.update(extra_timing_ms)
    return timing_ms


def _feed_item_ai_log_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    context = payload.get("enqueue_context")
    if not isinstance(context, dict):
        return None
    return {
        key: context.get(key)
        for key in (
            "ai_route",
            "ai_profile",
            "ai_provider",
            "ai_model",
            "profile_version",
            "channel_catalog_version",
            "source_profile_version",
            "source_profile_is_empty",
            "feature_schema_version",
            "score_version",
        )
        if key in context
    }


def _log_feed_item_insight_artifact_build_timing(
    operation: AppOperation,
    job: Job,
    *,
    feed_item_id: int,
    stats: InsightEngineArtifactRebuildStats,
    source_id: int | None = None,
    source_sync_operation_id: int | None = None,
    timing_ms: dict[str, int] | None = None,
) -> None:
    log_operation_event(
        logger,
        "feed_item_insight_artifact_build_timing",
        operation=operation,
        job=job,
        feed_item_id=feed_item_id,
        source_id=source_id if source_id is not None else _payload_int(job.payload_json or {}, "source_id"),
        source_sync_operation_id=(
            source_sync_operation_id
            if source_sync_operation_id is not None
            else _payload_int(job.payload_json or {}, "source_sync_operation_id")
        ),
        ai_context=_feed_item_ai_log_context(job.payload_json or {}),
        processed_feed_item_count=stats.processed_feed_item_count,
        artifact_rows_created=stats.artifact_rows_created,
        failed_feed_item_count=stats.failed_feed_item_count,
        timing_ms=timing_ms or stats.timing_ms,
    )


def _feed_item_insight_artifact_result(
    *,
    scope: str,
    processed_feed_item_count: int = 0,
    artifact_rows_created: int = 0,
    failed_feed_item_count: int = 0,
    stopped: bool = False,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "processed_feed_item_count": processed_feed_item_count,
        "artifact_rows_created": artifact_rows_created,
        "failed_feed_item_count": failed_feed_item_count,
        "stopped": stopped,
    }


def _source_label(session: Session, source_id: int | None) -> str:
    if source_id is None:
        return "all sources"
    source = session.get(Source, source_id)
    return source.name if source is not None else f"source:{source_id}"


def _feed_item_extract_success_payload(job: Job) -> dict[str, Any]:
    return {"feed_item_id": _payload_int(job.payload_json or {}, "feed_item_id")}


def _feed_item_insight_artifacts_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "source_id": _payload_int(payload, "source_id"),
        "scope": payload.get("scope") if isinstance(payload.get("scope"), str) else None,
        "limit": _payload_int(payload, "limit"),
    }


def _artifact_build_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "feed_item_id": _payload_int(payload, "feed_item_id"),
        "source_id": _payload_int(payload, "source_id"),
        "source_sync_operation_id": _payload_int(payload, "source_sync_operation_id"),
    }


def _local_mirror_export_success_payload(job: Job) -> dict[str, Any]:
    return {"saved_item_id": _payload_int(job.payload_json or {}, "saved_item_id")}


def _knowledge_capture_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "saved_item_id": _payload_int(payload, "saved_item_id"),
        "capture_run_id": _payload_int(payload, "capture_run_id"),
        "capture_mode": payload.get("capture_mode") if isinstance(payload.get("capture_mode"), str) else None,
    }


def _knowledge_capture_mirror_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "saved_item_id": _payload_int(payload, "saved_item_id"),
        "source_capture_run_id": _payload_int(payload, "source_capture_run_id"),
        "capture_mode": payload.get("capture_mode") if isinstance(payload.get("capture_mode"), str) else None,
    }


def _feed_item_insight_artifact_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "feed_item_id": _payload_int(payload, "feed_item_id"),
        "mode": payload.get("mode") if isinstance(payload.get("mode"), str) else None,
    }


def _saved_item_insight_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "saved_item_id": _payload_int(payload, "saved_item_id"),
        "ai_route": payload.get("ai_route") if isinstance(payload.get("ai_route"), str) else None,
    }


def _channel_reroute_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "feed_item_id": _payload_int(payload, "feed_item_id"),
        "retired_channel_id": payload.get("retired_channel_id")
        if isinstance(payload.get("retired_channel_id"), str)
        else None,
        "fallback_channel_id": payload.get("fallback_channel_id")
        if isinstance(payload.get("fallback_channel_id"), str)
        else None,
        "channel_catalog_version": _payload_int(payload, "channel_catalog_version"),
    }


def _source_sync_success_payload(job: Job) -> dict[str, Any]:
    payload = job.payload_json or {}
    return {
        "source_id": _payload_int(payload, "source_id"),
        "source_index": _payload_int(payload, "source_index"),
    }


OPERATION_JOB_REGISTRY = build_operation_job_registry(
    (
        OperationJobHandler(
            job_type=JOB_TYPE_SOURCE_SYNC_ONE_SOURCE,
            operation_type=OPERATION_TYPE_SOURCE_SYNC,
            run=_run_source_sync_one_source_job,
            success_event_payload=_source_sync_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_FEED_ITEM_EXTRACT,
            operation_type=OPERATION_TYPE_FEED_ITEM_EXTRACT,
            run=_run_feed_item_extract_job,
            success_event_payload=_feed_item_extract_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
            operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
            run=_run_feed_item_insight_artifacts_job,
            success_event_payload=_feed_item_insight_artifacts_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
            run=_run_source_sync_artifact_build_job,
            success_event_payload=_artifact_build_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_KNOWLEDGE_CAPTURE,
            operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
            run=_run_knowledge_capture_job,
            success_event_payload=_knowledge_capture_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
            operation_type=OPERATION_TYPE_KNOWLEDGE_CAPTURE,
            run=_run_knowledge_capture_local_mirror_job,
            success_event_payload=_knowledge_capture_mirror_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_LOCAL_MIRROR_EXPORT,
            operation_type=OPERATION_TYPE_LOCAL_MIRROR_EXPORT,
            run=_run_local_mirror_export_job,
            success_event_payload=_local_mirror_export_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
            operation_type=OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
            run=_run_feed_item_insight_artifact_job,
            success_event_payload=_feed_item_insight_artifact_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_SAVED_ITEM_INSIGHT,
            operation_type=OPERATION_TYPE_SAVED_ITEM_INSIGHT,
            run=_run_saved_item_insight_job,
            success_event_payload=_saved_item_insight_success_payload,
        ),
        OperationJobHandler(
            job_type=JOB_TYPE_CHANNEL_REROUTE_ITEM,
            operation_type=OPERATION_TYPE_CHANNEL_LIFECYCLE,
            run=_run_channel_reroute_item_job,
            success_event_payload=_channel_reroute_success_payload,
        ),
    )
)
JOB_HANDLER_TYPES = frozenset(OPERATION_JOB_REGISTRY)
AI_LANE_JOB_TYPES = frozenset(job_type for job_type in JOB_HANDLER_TYPES if job_type in AI_THROTTLED_JOB_TYPES)
GLOBAL_WORKER_JOB_TYPES = frozenset(
    job_type for job_type in JOB_HANDLER_TYPES if job_type not in {JOB_TYPE_SOURCE_SYNC_ONE_SOURCE, *AI_LANE_JOB_TYPES}
)
JOB_BACKED_OPERATION_TYPES = frozenset(handler.operation_type for handler in OPERATION_JOB_REGISTRY.values())


def _latest_insight_attempt_id(session: Session, feed_item_id: int) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.max(InsightEngineAttempt.id), 0)).where(
                InsightEngineAttempt.feed_item_id == feed_item_id
            )
        )
        or 0
    )


def _has_retryable_feed_item_artifact_attempt(
    session: Session,
    *,
    feed_item_id: int,
    after_id: int,
) -> bool:
    attempts = session.scalars(
        select(InsightEngineAttempt)
        .where(InsightEngineAttempt.feed_item_id == feed_item_id)
        .where(InsightEngineAttempt.id > after_id)
        .where(InsightEngineAttempt.status == ATTEMPT_STATUS_FAILED)
        .where(InsightEngineAttempt.stage.in_(tuple(RETRYABLE_FEED_ITEM_ARTIFACT_ATTEMPT_STAGES)))
        .order_by(InsightEngineAttempt.id.desc())
    ).all()
    return any(_is_retryable_feed_item_artifact_attempt(attempt) for attempt in attempts)


def _is_retryable_feed_item_artifact_attempt(attempt: InsightEngineAttempt) -> bool:
    if attempt.stage != "provider_request":
        return False
    error_type = attempt.error_type or ""
    error_message = attempt.error_message or ""
    if error_type == "AIProviderHTTPError":
        return any(
            f"AI provider HTTP {status_code}" in error_message
            for status_code in RETRYABLE_PROVIDER_HTTP_STATUS_CODES
        )
    if error_type == "AIProviderError":
        return "AI provider request failed" in error_message
    return False


def _operation_job_worker_loop(*, worker_id: str, poll_interval_seconds: float) -> None:
    while not _worker_stop_event.is_set():
        try:
            ran_job = run_next_recoverable_source_sync_operation(worker_id=worker_id)
            if not ran_job:
                ran_job = run_next_available_operation_job(worker_id=worker_id)
        except Exception:
            logger.exception("Operation job worker failed while draining jobs.")
            ran_job = False
        if not ran_job:
            _worker_wake_event.wait(max(0.1, poll_interval_seconds))
            _worker_wake_event.clear()


def _ai_lane_worker_loop(*, worker_id: str, poll_interval_seconds: float) -> None:
    while not _worker_stop_event.is_set():
        try:
            _run_ai_lane_engine(
                worker_id=worker_id,
                stop_event=_worker_stop_event,
                wake_event=_ai_lane_wake_event,
                lease_seconds=DEFAULT_LEASE_SECONDS,
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                poll_interval_seconds=poll_interval_seconds,
                failure_backoff_seconds=None,
                max_drain_seconds=None,
                max_concurrent_jobs=None,
                stop_when_idle=False,
            )
        except Exception:
            logger.exception("AI lane worker failed while draining jobs.")
            _worker_stop_event.wait(max(0.1, poll_interval_seconds))
        else:
            if not _worker_stop_event.is_set():
                _worker_stop_event.wait(max(0.1, poll_interval_seconds))


def _run_ai_lane_engine(
    *,
    worker_id: str,
    stop_event: threading.Event,
    wake_event: threading.Event | None,
    lease_seconds: int,
    heartbeat_interval_seconds: float,
    poll_interval_seconds: float,
    failure_backoff_seconds: int | None,
    max_drain_seconds: float | None,
    max_concurrent_jobs: int | None,
    stop_when_idle: bool,
) -> bool:
    max_workers = max(1, max_concurrent_jobs or _artifact_queue_concurrency())
    in_flight: dict[Future[None], int] = {}
    ran_job = False
    drain_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while not stop_event.is_set():
            completed_futures = [future for future in in_flight if future.done()]
            for future in completed_futures:
                job_id = in_flight.pop(future, None)
                try:
                    future.result()
                except Exception:
                    logger.exception("AI lane worker failed while running job_id=%s.", job_id)
                ran_job = True

            if completed_futures:
                _sync_job_backed_operations_after_worker_activity()

            capacity = max_workers - len(in_flight)
            claimed: list[Job] = []
            if capacity > 0:
                try:
                    claimed = _claim_ai_lane_jobs(
                        worker_id=worker_id,
                        limit=capacity,
                        lease_seconds=lease_seconds,
                    )
                except Exception:
                    logger.exception("AI lane worker failed while claiming jobs.")
                    claimed = []
                for job in claimed:
                    future = executor.submit(
                        _run_claimed_job,
                        job.id,
                        worker_id=worker_id,
                        lease_seconds=lease_seconds,
                        heartbeat_interval_seconds=heartbeat_interval_seconds,
                        failure_backoff_seconds=failure_backoff_seconds,
                        now=None,
                    )
                    in_flight[future] = job.id
                if claimed:
                    ran_job = True

            if stop_when_idle and not in_flight and not claimed:
                wait_seconds = _next_ai_lane_job_wait_seconds(max_poll_interval_seconds=poll_interval_seconds)
                if wait_seconds is None:
                    return ran_job
                if max_drain_seconds is None:
                    return ran_job
                remaining = max_drain_seconds - (time.monotonic() - drain_started)
                if remaining <= 0:
                    return ran_job
                time.sleep(max(0.0, min(wait_seconds, remaining)))
                continue

            if not in_flight:
                if wake_event is None:
                    time.sleep(max(0.0, poll_interval_seconds))
                else:
                    wake_event.wait(max(0.1, poll_interval_seconds))
                    wake_event.clear()
                continue

            done, _ = wait(
                list(in_flight),
                timeout=max(0.0, poll_interval_seconds),
                return_when=FIRST_COMPLETED,
            )
            if not done and wake_event is not None and wake_event.is_set():
                wake_event.clear()
    return ran_job


def _claim_ai_lane_jobs(
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
) -> list[Job]:
    with get_session_factory()() as session:
        claimed = _claim_ai_limited_jobs(
            session,
            worker_id=worker_id,
            limit=limit,
            queue_names=(FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
            job_types=tuple(AI_LANE_JOB_TYPES),
            lease_seconds=lease_seconds,
        )
        _sync_all_job_backed_operations(session)
        session.commit()
        return claimed


def _sync_job_backed_operations_after_worker_activity() -> None:
    with get_session_factory()() as session:
        _sync_all_job_backed_operations(session)
        session.commit()


def _next_ai_lane_job_wait_seconds(*, max_poll_interval_seconds: float) -> float | None:
    now = _now_utc()
    with get_session_factory()() as session:
        running_count = int(
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status == JOB_STATUS_RUNNING)
                .where(Job.queue_name == FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME)
                .where(Job.job_type.in_(tuple(AI_LANE_JOB_TYPES)))
                .where(or_(Job.lease_expires_at.is_(None), Job.lease_expires_at > now))
            )
            or 0
        )
        if running_count:
            return max_poll_interval_seconds
        pending_run_after = session.scalar(
            select(func.min(Job.run_after))
            .where(Job.status == JOB_STATUS_PENDING)
            .where(Job.queue_name == FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME)
            .where(Job.job_type.in_(tuple(AI_LANE_JOB_TYPES)))
        )
    if pending_run_after is None:
        return None
    wait_seconds = (_as_utc(pending_run_after) - now).total_seconds()
    if wait_seconds <= 0:
        return max_poll_interval_seconds
    return min(max_poll_interval_seconds, wait_seconds)


def _sync_all_job_backed_operations(session: Session) -> None:
    operation_ids = list(
        session.scalars(
            select(Job.operation_id)
            .join(AppOperation, AppOperation.id == Job.operation_id)
            .where(Job.operation_id.is_not(None))
            .where(Job.job_type.in_(tuple(JOB_HANDLER_TYPES)))
            .where(AppOperation.status.in_(ACTIVE_OPERATION_STATUSES))
            .distinct()
        )
    )
    for operation_id in operation_ids:
        if operation_id is None:
            continue
        operation = session.get(AppOperation, operation_id)
        if operation is not None and operation.operation_type in JOB_BACKED_OPERATION_TYPES:
            sync_job_backed_operation_state(session, operation)


def _next_active_job_wait_seconds(
    session: Session,
    operation_id: int,
    *,
    max_poll_interval_seconds: float,
) -> float | None:
    active_jobs = list(
        session.scalars(
            select(Job)
            .where(Job.operation_id == operation_id)
            .where(Job.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(Job.run_after.asc(), Job.lease_expires_at.asc(), Job.id.asc())
        )
    )
    if not active_jobs:
        return None
    now = _now_utc()
    candidates: list[datetime] = []
    for job in active_jobs:
        if job.status == JOB_STATUS_PENDING:
            candidates.append(_as_utc(job.run_after))
        elif job.status == JOB_STATUS_RUNNING and job.lease_expires_at is not None:
            candidates.append(_as_utc(job.lease_expires_at))
    if not candidates:
        return max_poll_interval_seconds
    next_time = min(candidates)
    return min(max_poll_interval_seconds, max(0.0, (next_time - now).total_seconds()))


def _next_recoverable_source_sync_operation_id() -> int | None:
    now = _now_utc()
    with get_session_factory()() as session:
        return session.scalar(
            select(Job.operation_id)
            .join(AppOperation, AppOperation.id == Job.operation_id)
            .where(AppOperation.operation_type == OPERATION_TYPE_SOURCE_SYNC)
            .where(AppOperation.status.in_({STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}))
            .where(Job.job_type == JOB_TYPE_SOURCE_SYNC_ONE_SOURCE)
            .where(
                or_(
                    and_(Job.status == JOB_STATUS_PENDING, Job.run_after <= now),
                    and_(
                        Job.status == JOB_STATUS_RUNNING,
                        Job.lease_expires_at.is_not(None),
                        Job.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(Job.run_after.asc(), Job.id.asc())
            .limit(1)
        )


def _operation_job_claim_limit(
    session: Session,
    operation: AppOperation,
    *,
    max_concurrent_jobs: int,
    now: datetime | None,
) -> int:
    limit = max(1, max_concurrent_jobs)
    if operation.id is None:
        return limit
    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES:
        running_count = _non_expired_running_queue_job_count(
            session,
            operation.id,
            queue_name=FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            now=now,
        )
        return max(0, limit - running_count)
    if operation.operation_type != OPERATION_TYPE_SOURCE_SYNC:
        return limit
    running_count = _non_expired_running_job_count(
        session,
        operation.id,
        job_type=JOB_TYPE_SOURCE_SYNC_ONE_SOURCE,
        now=now,
    )
    return max(0, limit - running_count)


def _operation_uses_ai_throttled_jobs(operation: AppOperation) -> bool:
    return operation.operation_type in {
        *FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES,
        *FEED_ITEM_INSIGHT_ARTIFACT_OPERATION_TYPES,
        OPERATION_TYPE_SAVED_ITEM_INSIGHT,
    }


def _claim_ai_limited_jobs(
    session: Session,
    *,
    worker_id: str,
    limit: int,
    queue_names: Sequence[str] | None = None,
    job_types: Sequence[str] | None = None,
    operation_id: int | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[Job]:
    with _ai_throttle_claim_lock:
        _acquire_ai_throttle_claim_database_lock(session)
        return _claim_ai_limited_jobs_locked(
            session,
            worker_id=worker_id,
            limit=limit,
            queue_names=queue_names,
            job_types=job_types,
            operation_id=operation_id,
            lease_seconds=lease_seconds,
            now=now,
        )


def _claim_ai_limited_jobs_locked(
    session: Session,
    *,
    worker_id: str,
    limit: int,
    queue_names: Sequence[str] | None = None,
    job_types: Sequence[str] | None = None,
    operation_id: int | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[Job]:
    if limit <= 0:
        return []
    claim_time = now or _now_utc()
    throttled_job_types = tuple(_ai_throttled_job_types(job_types))
    if get_settings().database_url.startswith("sqlite"):
        requeue_expired_running_jobs(
            session,
            queue_names=queue_names,
            job_types=throttled_job_types,
            operation_id=operation_id,
            now=claim_time,
        )
        if _running_ai_throttle_counts(session, now=claim_time):
            return []
        return claim_jobs(
            session,
            worker_id=worker_id,
            limit=1,
            queue_names=queue_names,
            job_types=throttled_job_types,
            operation_id=operation_id,
            lease_seconds=lease_seconds,
            now=claim_time,
        )

    requeue_expired_running_jobs(
        session,
        queue_names=queue_names,
        job_types=throttled_job_types,
        operation_id=operation_id,
        now=claim_time,
    )
    base_stmt = (
        select(Job)
        .where(Job.status == JOB_STATUS_PENDING)
        .where(Job.run_after <= claim_time)
        .where(Job.job_type.in_(throttled_job_types))
        .order_by(Job.priority.desc(), Job.run_after.asc(), Job.id.asc())
    )
    if queue_names is not None:
        base_stmt = base_stmt.where(Job.queue_name.in_(tuple(queue_names)))
    if operation_id is not None:
        base_stmt = base_stmt.where(Job.operation_id == operation_id)

    running_counts = _running_ai_throttle_counts(session, now=claim_time)
    selected_ids: list[int] = []
    selected_counts: dict[tuple[str, str], int] = {}
    scan_window = max(limit * 4, 64)
    offset = 0
    while len(selected_ids) < limit:
        candidates = list(session.scalars(base_stmt.limit(scan_window).offset(offset)))
        if not candidates:
            break
        for job in candidates:
            key = _ai_throttle_key_for_job(job)
            effective_limit = _ai_throttle_limit(session, key)
            used = running_counts.get(key, 0) + selected_counts.get(key, 0)
            if used >= effective_limit:
                continue
            selected_ids.append(job.id)
            selected_counts[key] = selected_counts.get(key, 0) + 1
            if len(selected_ids) >= limit:
                break
        if len(candidates) < scan_window:
            break
        offset += scan_window
    if not selected_ids:
        return []
    return claim_jobs(
        session,
        worker_id=worker_id,
        limit=len(selected_ids),
        queue_names=queue_names,
        job_types=throttled_job_types,
        job_ids=selected_ids,
        operation_id=operation_id,
        lease_seconds=lease_seconds,
        now=claim_time,
    )


def _acquire_ai_throttle_claim_database_lock(session: Session) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": "infoflow_ai_artifact_claim"},
    )


def _ai_throttled_job_types(job_types: Sequence[str] | None) -> set[str]:
    if job_types is None:
        return set(AI_THROTTLED_JOB_TYPES)
    return {job_type for job_type in job_types if job_type in AI_THROTTLED_JOB_TYPES}


def _running_ai_throttle_counts(session: Session, *, now: datetime) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    jobs = session.scalars(
        select(Job)
        .where(Job.job_type.in_(tuple(AI_THROTTLED_JOB_TYPES)))
        .where(Job.status == JOB_STATUS_RUNNING)
        .where(or_(Job.lease_expires_at.is_(None), Job.lease_expires_at > now))
    )
    for job in jobs:
        key = _ai_throttle_key_for_job(job)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _ai_throttle_key_for_job(job: Job) -> tuple[str, str]:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    context = payload.get("enqueue_context")
    if not isinstance(context, dict):
        return AI_THROTTLE_DEFAULT_KEY
    profile = context.get("ai_profile")
    provider = context.get("ai_provider")
    if not isinstance(profile, str) or not profile or not isinstance(provider, str) or not provider:
        return AI_THROTTLE_DEFAULT_KEY
    return (profile, provider)


def _ai_throttle_limit(session: Session, key: tuple[str, str]) -> int:
    profile, provider = key
    if profile not in AI_PROFILES or provider == AI_ROUTE_DISABLED:
        return 1
    return max(1, effective_ai_artifact_concurrency(session, profile))


def _non_expired_running_queue_job_count(
    session: Session,
    operation_id: int,
    *,
    queue_name: str,
    now: datetime | None,
) -> int:
    reference_time = now or _now_utc()
    return int(
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.operation_id == operation_id)
            .where(Job.queue_name == queue_name)
            .where(Job.status == JOB_STATUS_RUNNING)
            .where(or_(Job.lease_expires_at.is_(None), Job.lease_expires_at > reference_time))
        )
        or 0
    )


def _artifact_queue_concurrency() -> int:
    if get_settings().database_url.startswith("sqlite"):
        return 1
    return FEED_ITEM_INSIGHT_ARTIFACT_MAX_CONCURRENT_JOBS


def _channel_lifecycle_queue_concurrency() -> int:
    if get_settings().database_url.startswith("sqlite"):
        return 1
    return CHANNEL_LIFECYCLE_MAX_CONCURRENT_JOBS


def _non_expired_running_job_count(
    session: Session,
    operation_id: int,
    *,
    job_type: str,
    now: datetime | None,
) -> int:
    reference_time = now or _now_utc()
    return int(
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.operation_id == operation_id)
            .where(Job.job_type == job_type)
            .where(Job.status == JOB_STATUS_RUNNING)
            .where(or_(Job.lease_expires_at.is_(None), Job.lease_expires_at > reference_time))
        )
        or 0
    )


def _payload_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _operation_scope_int(operation: AppOperation, key: str) -> int | None:
    scope = operation.scope_json
    if not isinstance(scope, dict):
        return None
    return _payload_int(scope, key)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_error_message(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = redact_text(value)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= 4096:
        return redacted
    return encoded[:4096].decode("utf-8", errors="ignore") + "...[truncated]"


def _operation_failed_message(operation: AppOperation) -> str:
    if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
        return "来源刷新任务失败。"
    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES:
        return "Feed item insight artifact fill operation failed."
    if operation.operation_type == OPERATION_TYPE_KNOWLEDGE_CAPTURE:
        return "Capture operation failed."
    if operation.operation_type == OPERATION_TYPE_LOCAL_MIRROR_EXPORT:
        return "Local mirror export operation failed."
    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_OPERATION_TYPES:
        return "Feed item insight artifact operation failed."
    if operation.operation_type == OPERATION_TYPE_SAVED_ITEM_INSIGHT:
        return "Saved item insight operation failed."
    return "Feed item extraction operation failed."


def _operation_stopped_message(operation: AppOperation) -> str:
    if operation.operation_type == OPERATION_TYPE_SOURCE_SYNC:
        return "来源刷新任务已停止。"
    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_BATCH_OPERATION_TYPES:
        return "Feed item insight artifact fill operation stopped."
    if operation.operation_type == OPERATION_TYPE_KNOWLEDGE_CAPTURE:
        return "Capture operation stopped."
    if operation.operation_type == OPERATION_TYPE_LOCAL_MIRROR_EXPORT:
        return "Local mirror export operation stopped."
    if operation.operation_type in FEED_ITEM_INSIGHT_ARTIFACT_OPERATION_TYPES:
        return "Feed item insight artifact operation stopped."
    if operation.operation_type == OPERATION_TYPE_SAVED_ITEM_INSIGHT:
        return "Saved item insight operation stopped."
    return "Feed item extraction operation stopped."


def _knowledge_capture_complete_message(operation: AppOperation) -> str:
    if operation.operation_kind == OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_URL:
        return "Manual capture operation completed."
    if operation.operation_kind == OPERATION_KIND_KNOWLEDGE_CAPTURE_UPLOADED_MATERIAL:
        return "Uploaded material capture operation completed."
    if operation.operation_kind == OPERATION_KIND_KNOWLEDGE_CAPTURE_MANUAL_IMPORT:
        return "Manual import operation completed."
    if operation.operation_kind == OPERATION_KIND_KNOWLEDGE_CAPTURE_RETRY:
        return "Saved item capture operation completed."
    if operation.operation_kind == OPERATION_KIND_KNOWLEDGE_CAPTURE_FEED_SAVED:
        return "Feed-saved capture operation completed."
    return "Capture operation completed."


def _feed_item_insight_artifact_result_from_stats(stats: InsightEngineArtifactRebuildStats) -> dict[str, Any]:
    return {
        "scope": stats.scope,
        "processed_feed_item_count": stats.processed_feed_item_count,
        "artifact_rows_created": stats.artifact_rows_created,
        "failed_feed_item_count": stats.failed_feed_item_count,
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_drain_worker_id() -> str:
    return f"{PROCESS_WORKER_ID}:drain:{uuid4().hex[:12]}"


def _new_slot_worker_id(slot_index: int) -> str:
    return f"{PROCESS_WORKER_ID}:slot-{slot_index}:{uuid4().hex[:12]}"


def _new_ai_lane_worker_id() -> str:
    return f"{PROCESS_WORKER_ID}:ai-lane:{uuid4().hex[:12]}"


class _Heartbeat:
    def __init__(self, job_id: int, *, worker_id: str, lease_seconds: int, interval_seconds: float) -> None:
        self._job_id = job_id
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._interval_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._run, name=f"operation-job-heartbeat-{self._job_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            with get_session_factory()() as session:
                try:
                    extend_job_lease(
                        session,
                        self._job_id,
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    return
