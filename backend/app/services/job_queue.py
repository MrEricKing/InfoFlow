from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.operation_logging import log_job_event
from app.core.security import PayloadValidationError, redact_text, sanitized_operation_payload, validate_operation_payload
from app.db.models import AppOperation, Job, JobEvent

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
ACTIVE_JOB_STATUSES = {JOB_STATUS_PENDING, JOB_STATUS_RUNNING}
TERMINAL_JOB_STATUSES = {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}

JOB_EVENT_ATTEMPT_STARTED = "attempt_started"
JOB_EVENT_ATTEMPT_SUCCEEDED = "attempt_succeeded"
JOB_EVENT_ATTEMPT_FAILED = "attempt_failed"
JOB_EVENT_ATTEMPT_CANCELLED = "attempt_cancelled"
JOB_EVENT_RETRY_SCHEDULED = "retry_scheduled"
JOB_EVENT_TYPES = {
    JOB_EVENT_ATTEMPT_STARTED,
    JOB_EVENT_ATTEMPT_SUCCEEDED,
    JOB_EVENT_ATTEMPT_FAILED,
    JOB_EVENT_ATTEMPT_CANCELLED,
    JOB_EVENT_RETRY_SCHEDULED,
}

DEFAULT_QUEUE_NAME = "default"
DEFAULT_LEASE_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = (15, 15)
MAX_ERROR_MESSAGE_BYTES = 4096
NULL_OPERATION_ID_JOB_TYPES = {"job_cleanup"}
logger = logging.getLogger(__name__)


class JobQueueError(RuntimeError):
    pass


class JobNotFoundError(JobQueueError):
    pass


class JobValidationError(ValueError):
    pass


class JobLeaseError(JobQueueError):
    pass


def create_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any] | None = None,
    operation_id: int | None,
    queue_name: str = DEFAULT_QUEUE_NAME,
    priority: int = 0,
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    run_after: datetime | None = None,
) -> Job:
    if operation_id is None and job_type not in NULL_OPERATION_ID_JOB_TYPES:
        raise JobValidationError(f"operation_id is required for job_type={job_type!r}")
    if not job_type.strip():
        raise JobValidationError("job_type is required")
    if not queue_name.strip():
        raise JobValidationError("queue_name is required")
    if max_attempts < 1:
        raise JobValidationError("max_attempts must be at least 1")

    normalized_payload = _validate_payload(payload)
    now = _now_utc()
    job = Job(
        operation_id=operation_id,
        job_type=job_type.strip(),
        status=JOB_STATUS_PENDING,
        priority=priority,
        queue_name=queue_name.strip(),
        payload_json=normalized_payload,
        redacted_payload_json=sanitized_operation_payload(normalized_payload) or {},
        idempotency_key=idempotency_key,
        attempts=0,
        max_attempts=max_attempts,
        run_after=run_after or now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    session.refresh(job)
    log_job_event(logger, "job_created", session, job)
    return job


def claim_jobs(
    session: Session,
    *,
    worker_id: str,
    limit: int = 1,
    queue_names: Sequence[str] | None = None,
    job_types: Sequence[str] | None = None,
    job_ids: Sequence[int] | None = None,
    operation_id: int | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[Job]:
    if not worker_id.strip():
        raise JobValidationError("worker_id is required")
    if limit <= 0:
        return []
    if lease_seconds <= 0:
        raise JobValidationError("lease_seconds must be positive")

    claim_time = now or _now_utc()
    normalized_queues = _normalize_queue_names(queue_names)
    normalized_job_types = _normalize_job_types(job_types)
    normalized_job_ids = _normalize_job_ids(job_ids)
    _requeue_expired_running_jobs(
        session,
        queue_names=normalized_queues,
        job_types=normalized_job_types,
        operation_id=operation_id,
        now=claim_time,
    )

    stmt = (
        select(Job)
        .where(Job.status == JOB_STATUS_PENDING)
        .where(Job.run_after <= claim_time)
        .order_by(Job.priority.desc(), Job.run_after.asc(), Job.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if normalized_queues is not None:
        stmt = stmt.where(Job.queue_name.in_(normalized_queues))
    if normalized_job_types is not None:
        stmt = stmt.where(Job.job_type.in_(normalized_job_types))
    if normalized_job_ids is not None:
        stmt = stmt.where(Job.id.in_(normalized_job_ids))
    if operation_id is not None:
        stmt = stmt.where(Job.operation_id == operation_id)

    jobs = list(session.scalars(stmt))
    lease_expires_at = claim_time + timedelta(seconds=lease_seconds)
    for job in jobs:
        job.status = JOB_STATUS_RUNNING
        job.locked_at = claim_time
        job.locked_by = worker_id
        job.lease_expires_at = lease_expires_at
        job.started_at = job.started_at or claim_time
        job.updated_at = claim_time
        job.attempts += 1
        session.add(job)
        record_job_event(
            session,
            job,
            JOB_EVENT_ATTEMPT_STARTED,
            {
                "attempt": job.attempts,
                "locked_by": worker_id,
                "lease_expires_at": _isoformat(lease_expires_at),
            },
        )
        log_job_event(logger, "job_claimed", session, job, worker_id=worker_id)
    session.flush()
    return jobs


def requeue_expired_running_jobs(
    session: Session,
    *,
    queue_names: Sequence[str] | None = None,
    job_types: Sequence[str] | None = None,
    operation_id: int | None = None,
    now: datetime | None = None,
) -> None:
    _requeue_expired_running_jobs(
        session,
        queue_names=_normalize_queue_names(queue_names),
        job_types=_normalize_job_types(job_types),
        operation_id=operation_id,
        now=now or _now_utc(),
    )


def extend_job_lease(
    session: Session,
    job_id: int,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> Job:
    if lease_seconds <= 0:
        raise JobValidationError("lease_seconds must be positive")
    job = _get_job_for_update_or_raise(session, job_id)
    if job.status != JOB_STATUS_RUNNING:
        raise JobLeaseError(f"Job {job_id} is not running")
    if job.locked_by != worker_id:
        raise JobLeaseError(f"Job {job_id} is leased by a different worker")

    heartbeat_time = now or _now_utc()
    lease_expires_at = heartbeat_time + timedelta(seconds=lease_seconds)
    job.lease_expires_at = lease_expires_at
    job.updated_at = heartbeat_time
    session.add(job)
    session.flush()
    session.refresh(job)
    return job


def complete_job(
    session: Session,
    job_id: int,
    *,
    worker_id: str,
    result: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    job = _get_job_for_update_or_raise(session, job_id)
    completed_at = now or _now_utc()
    _validate_worker_owns_active_lease(job, worker_id=worker_id, now=completed_at)

    job.status = JOB_STATUS_SUCCEEDED
    job.finished_at = completed_at
    job.updated_at = completed_at
    job.lease_expires_at = None
    job.error_type = None
    job.error_message = None
    job.error_detail_json = None
    session.add(job)
    record_job_event(session, job, JOB_EVENT_ATTEMPT_SUCCEEDED, result or {})
    log_job_event(logger, "job_completed", session, job, worker_id=worker_id)
    session.flush()
    session.refresh(job)
    return job


def fail_job(
    session: Session,
    job_id: int,
    *,
    worker_id: str,
    error_type: str = "JobError",
    error_message: str | None = None,
    error_detail: dict[str, Any] | None = None,
    retry: bool = True,
    backoff_seconds: int | None = None,
    now: datetime | None = None,
) -> Job:
    job = _get_job_for_update_or_raise(session, job_id)
    failed_at = now or _now_utc()
    _validate_worker_owns_active_lease(job, worker_id=worker_id, now=failed_at)
    safe_error_message = _sanitize_error_message(error_message)

    job.error_type = error_type
    job.error_message = safe_error_message
    job.error_detail_json = _sanitize_event_payload(error_detail)
    job.updated_at = failed_at

    should_retry = retry and not job.cancel_requested and job.attempts < job.max_attempts
    if should_retry:
        delay_seconds = backoff_seconds if backoff_seconds is not None else _retry_backoff_seconds(job.attempts)
        run_after = failed_at + timedelta(seconds=delay_seconds)
        job.status = JOB_STATUS_PENDING
        job.run_after = run_after
        job.locked_at = None
        job.locked_by = None
        job.lease_expires_at = None
        session.add(job)
        record_job_event(
            session,
            job,
            JOB_EVENT_RETRY_SCHEDULED,
            {
                "attempt": job.attempts,
                "error_type": error_type,
                "error_message": safe_error_message,
                "run_after": _isoformat(run_after),
            },
        )
        log_job_event(
            logger,
            "job_retry_scheduled",
            session,
            job,
            worker_id=worker_id,
            error_type=error_type,
            run_after=_isoformat(run_after),
        )
    else:
        job.status = JOB_STATUS_FAILED
        job.finished_at = failed_at
        job.lease_expires_at = None
        session.add(job)
        record_job_event(
            session,
            job,
            JOB_EVENT_ATTEMPT_FAILED,
            {
                "attempt": job.attempts,
                "error_type": error_type,
                "error_message": safe_error_message,
            },
        )
        log_job_event(logger, "job_failed", session, job, worker_id=worker_id, error_type=error_type)
    session.flush()
    session.refresh(job)
    return job


def finish_running_job_cancelled(
    session: Session,
    job_id: int,
    *,
    worker_id: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> Job:
    job = _get_job_for_update_or_raise(session, job_id)
    cancelled_at = now or _now_utc()
    _validate_worker_owns_active_lease(job, worker_id=worker_id, now=cancelled_at)
    if not job.cancel_requested:
        raise JobValidationError(f"Job {job_id} has no cancellation request")

    job.status = JOB_STATUS_CANCELLED
    job.finished_at = cancelled_at
    job.updated_at = cancelled_at
    job.locked_at = None
    job.locked_by = None
    job.lease_expires_at = None
    session.add(job)
    record_job_event(
        session,
        job,
        JOB_EVENT_ATTEMPT_CANCELLED,
        {"attempt": job.attempts, "reason": _sanitize_error_message(reason)},
    )
    log_job_event(logger, "job_cancelled", session, job, worker_id=worker_id)
    session.flush()
    session.refresh(job)
    return job


def cancel_job(
    session: Session,
    job_id: int,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> Job:
    job = _get_job_for_update_or_raise(session, job_id)
    cancelled_at = now or _now_utc()
    if job.status == JOB_STATUS_PENDING:
        job.status = JOB_STATUS_CANCELLED
        job.finished_at = cancelled_at
        job.locked_at = None
        job.locked_by = None
        job.lease_expires_at = None
        job.updated_at = cancelled_at
        session.add(job)
        log_job_event(logger, "job_cancelled", session, job, reason=_sanitize_error_message(reason))
    elif job.status == JOB_STATUS_RUNNING:
        job.cancel_requested = True
        job.updated_at = cancelled_at
        session.add(job)
        log_job_event(logger, "job_cancel_requested", session, job, reason=_sanitize_error_message(reason))
    session.flush()
    session.refresh(job)
    return job


def manual_requeue_job(
    session: Session,
    job_id: int,
    *,
    reason: str | None = None,
    run_after: datetime | None = None,
) -> Job:
    original = _get_job_for_update_or_raise(session, job_id)
    if original.status not in TERMINAL_JOB_STATUSES:
        raise JobValidationError(f"Job {job_id} must be terminal before manual requeue")

    requeued = create_job(
        session,
        operation_id=original.operation_id,
        job_type=original.job_type,
        payload=original.payload_json,
        queue_name=original.queue_name,
        priority=original.priority,
        idempotency_key=original.idempotency_key,
        max_attempts=original.max_attempts,
        run_after=run_after,
    )
    session.flush()
    session.refresh(requeued)
    log_job_event(logger, "job_manual_requeued", session, requeued, original_job_id=original.id)
    return requeued


def record_job_event(
    session: Session,
    job: Job | int,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> JobEvent:
    normalized_event_type = event_type.strip()
    if not normalized_event_type:
        raise JobValidationError("event_type is required")
    if normalized_event_type not in JOB_EVENT_TYPES:
        raise JobValidationError(f"Unsupported job event type: {normalized_event_type}")
    job_id = job.id if isinstance(job, Job) else job
    event = JobEvent(
        job_id=job_id,
        event_type=normalized_event_type,
        event_payload_json=_sanitize_event_payload(payload),
    )
    session.add(event)
    session.flush()
    return event


def aggregate_operation_progress_from_jobs(
    session: Session,
    operation_id: int,
    *,
    base_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session.flush()
    rows = session.execute(
        select(Job.status, func.count(Job.id))
        .where(Job.operation_id == operation_id)
        .group_by(Job.status)
    ).all()
    counts = {status: int(count) for status, count in rows}
    pending = counts.get(JOB_STATUS_PENDING, 0)
    running = counts.get(JOB_STATUS_RUNNING, 0)
    succeeded = counts.get(JOB_STATUS_SUCCEEDED, 0)
    failed = counts.get(JOB_STATUS_FAILED, 0)
    cancelled = counts.get(JOB_STATUS_CANCELLED, 0)
    total = pending + running + succeeded + failed + cancelled
    completed = succeeded + failed + cancelled

    progress = _operation_progress_defaults()
    if base_progress:
        progress.update(base_progress)
    progress.update(
        {
            "total": total,
            "completed": completed,
            "success_count": succeeded,
            "error_count": failed,
            "stopped": cancelled > 0 and completed == total,
            "job_total_count": total,
            "job_pending_count": pending,
            "job_running_count": running,
            "job_succeeded_count": succeeded,
            "job_failed_count": failed,
            "job_cancelled_count": cancelled,
        }
    )

    operation = session.get(AppOperation, operation_id)
    if operation is not None:
        progress["stop_requested"] = bool(operation.stop_requested)

    specific_stage = _specific_running_progress_stage(progress.get("stage"))
    if total == 0:
        progress["stage"] = progress.get("stage") or "idle"
    elif running:
        progress["stage"] = specific_stage or "running"
    elif pending:
        progress["stage"] = "queued"
    elif cancelled and completed == total:
        progress["stage"] = "stopped"
    else:
        progress["stage"] = "completed"
    return progress


def _specific_running_progress_stage(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stage = value.strip()
    if not stage:
        return None
    if stage in {
        "idle",
        "queued",
        "running",
        "completed",
        "failed",
        "stopped",
        "stopping",
        "waiting_source_sync",
    }:
        return None
    return stage


def _requeue_expired_running_jobs(
    session: Session,
    *,
    queue_names: Sequence[str] | None,
    job_types: Sequence[str] | None,
    operation_id: int | None,
    now: datetime,
) -> None:
    stmt = (
        select(Job)
        .where(Job.status == JOB_STATUS_RUNNING)
        .where(Job.lease_expires_at.is_not(None))
        .where(Job.lease_expires_at <= now)
        .order_by(Job.lease_expires_at.asc(), Job.id.asc())
        .with_for_update(skip_locked=True)
    )
    if queue_names is not None:
        stmt = stmt.where(Job.queue_name.in_(queue_names))
    if job_types is not None:
        stmt = stmt.where(Job.job_type.in_(job_types))
    if operation_id is not None:
        stmt = stmt.where(Job.operation_id == operation_id)

    for job in session.scalars(stmt):
        job.updated_at = now
        job.locked_at = None
        job.locked_by = None
        job.lease_expires_at = None
        if job.cancel_requested:
            job.status = JOB_STATUS_CANCELLED
            job.finished_at = now
            event_type = JOB_EVENT_ATTEMPT_CANCELLED
            event_payload = {"reason": "lease_expired_after_cancel_request", "attempt": job.attempts}
        elif job.attempts >= job.max_attempts:
            job.status = JOB_STATUS_FAILED
            job.finished_at = now
            job.error_type = job.error_type or "LeaseExpired"
            job.error_message = job.error_message or "Job lease expired and no retry attempts remain."
            event_type = JOB_EVENT_ATTEMPT_FAILED
            event_payload = {"reason": "lease_expired", "attempt": job.attempts}
        else:
            job.status = JOB_STATUS_PENDING
            job.run_after = now
            event_type = JOB_EVENT_RETRY_SCHEDULED
            event_payload = {"reason": "lease_expired", "attempt": job.attempts}
        session.add(job)
        record_job_event(session, job, event_type, event_payload)
        log_job_event(logger, "job_lease_reclaimed", session, job, event_type=event_type)
    session.flush()


def _get_job_or_raise(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} was not found")
    return job


def _get_job_for_update_or_raise(session: Session, job_id: int) -> Job:
    job = session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise JobNotFoundError(f"Job {job_id} was not found")
    return job


def _validate_worker_owns_active_lease(job: Job, *, worker_id: str, now: datetime) -> None:
    if not worker_id.strip():
        raise JobValidationError("worker_id is required")
    if job.status != JOB_STATUS_RUNNING:
        raise JobLeaseError(f"Job {job.id} is not running")
    if job.locked_by != worker_id:
        raise JobLeaseError(f"Job {job.id} is leased by a different worker")
    if job.lease_expires_at is None:
        raise JobLeaseError(f"Job {job.id} has no active lease")
    if _as_utc(job.lease_expires_at) <= _as_utc(now):
        raise JobLeaseError(f"Job {job.id} lease has expired")


def _validate_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    try:
        validated = validate_operation_payload(payload or {})
    except PayloadValidationError as exc:
        raise JobValidationError(str(exc)) from exc
    return validated or {}


def _sanitize_event_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return _validate_payload(_redact_text_values(payload))


def _redact_text_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_text_values(entry) for key, entry in value.items()}
    if isinstance(value, list):
        return [_redact_text_values(entry) for entry in value]
    if isinstance(value, str):
        return _sanitize_error_message(value)
    return value


def _sanitize_error_message(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = redact_text(value)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= MAX_ERROR_MESSAGE_BYTES:
        return redacted
    return encoded[:MAX_ERROR_MESSAGE_BYTES].decode("utf-8", errors="ignore") + "...[truncated]"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _retry_backoff_seconds(attempts: int) -> int:
    index = max(0, attempts - 1)
    if index >= len(DEFAULT_RETRY_BACKOFF_SECONDS):
        return DEFAULT_RETRY_BACKOFF_SECONDS[-1]
    return DEFAULT_RETRY_BACKOFF_SECONDS[index]


def _normalize_queue_names(queue_names: Sequence[str] | None) -> list[str] | None:
    if queue_names is None:
        return None
    normalized = [queue_name.strip() for queue_name in queue_names if queue_name.strip()]
    if not normalized:
        raise JobValidationError("queue_names cannot be empty")
    return normalized


def _normalize_job_types(job_types: Sequence[str] | None) -> list[str] | None:
    if job_types is None:
        return None
    normalized = [job_type.strip() for job_type in job_types if job_type.strip()]
    if not normalized:
        raise JobValidationError("job_types cannot be empty")
    return normalized


def _normalize_job_ids(job_ids: Sequence[int] | None) -> list[int] | None:
    if job_ids is None:
        return None
    normalized: list[int] = []
    for job_id in job_ids:
        try:
            parsed = int(job_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            normalized.append(parsed)
    if not normalized:
        raise JobValidationError("job_ids cannot be empty")
    return list(dict.fromkeys(normalized))


def _operation_progress_defaults() -> dict[str, Any]:
    return {
        "stage": "idle",
        "total": 0,
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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
