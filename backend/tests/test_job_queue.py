from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.db.models import AppOperation, Job, JobEvent
from app.db.session import get_session_factory
from app.schemas.operations import OperationProgressRead
from app.services import operation_jobs
from app.services.job_queue import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobLeaseError,
    JobValidationError,
    aggregate_operation_progress_from_jobs,
    cancel_job,
    claim_jobs,
    complete_job,
    create_job,
    extend_job_lease,
    fail_job,
    finish_running_job_cancelled,
    manual_requeue_job,
    record_job_event,
)


def test_operation_job_registry_rejects_duplicate_job_type() -> None:
    handler = operation_jobs.OperationJobHandler(
        job_type="duplicate",
        operation_type="duplicate_operation",
        run=lambda session, job, operation: None,
        success_event_payload=lambda job: {},
    )

    with pytest.raises(ValueError, match="Duplicate operation job handler"):
        operation_jobs.build_operation_job_registry((handler, handler))


def test_operation_job_registry_covers_registered_handler_types() -> None:
    assert set(operation_jobs.JOB_HANDLER_TYPES) == set(operation_jobs.OPERATION_JOB_REGISTRY)
    assert operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS in operation_jobs.OPERATION_JOB_REGISTRY
    assert operation_jobs.OPERATION_JOB_REGISTRY[operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS].operation_type == (
        operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS
    )
    assert operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD in operation_jobs.OPERATION_JOB_REGISTRY
    assert operation_jobs.OPERATION_JOB_REGISTRY[
        operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD
    ].operation_type == (
        operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS
    )
    assert operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT in operation_jobs.OPERATION_JOB_REGISTRY
    assert operation_jobs.OPERATION_JOB_REGISTRY[operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT].operation_type == (
        operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT
    )


def test_job_queue_payload_validation_rejects_secret_raw_content_and_oversize(client) -> None:
    with get_session_factory()() as session:
        operation_id = _create_operation(session)

        with pytest.raises(JobValidationError, match="secret-like field"):
            create_job(
                session,
                operation_id=operation_id,
                job_type="test_job",
                payload={"api_key": "sk-secret"},
            )

        with pytest.raises(JobValidationError, match="large-content field"):
            create_job(
                session,
                operation_id=operation_id,
                job_type="test_job",
                payload={"raw_content": "<html>too much</html>"},
            )

        with pytest.raises(JobValidationError, match="16KB"):
            create_job(
                session,
                operation_id=operation_id,
                job_type="test_job",
                payload={"source_ids": list(range(10_000))},
            )


def test_job_queue_nullable_operation_id_is_allowlisted(client) -> None:
    with get_session_factory()() as session:
        with pytest.raises(JobValidationError, match="operation_id is required"):
            create_job(
                session,
                operation_id=None,
                job_type="test_job",
                payload={"source_id": 1},
            )

        job = create_job(
            session,
            operation_id=None,
            job_type="job_cleanup",
            payload={"limit": 10},
        )
        session.commit()

    assert job.operation_id is None
    assert job.status == JOB_STATUS_PENDING


def test_job_queue_attempt_boundary_events_only_for_service_lifecycle(client) -> None:
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
        )
        claim_jobs(session, worker_id="worker-a", limit=1)
        extend_job_lease(session, job.id, worker_id="worker-a")
        fail_job(session, job.id, worker_id="worker-a", error_type="TransientError", backoff_seconds=0)
        claim_jobs(session, worker_id="worker-a", limit=1)
        complete_job(session, job.id, worker_id="worker-a")
        session.commit()

    with get_session_factory()() as session:
        event_types = list(
            session.scalars(
                select(JobEvent.event_type)
                .where(JobEvent.job_id == job.id)
                .order_by(JobEvent.id)
            )
        )

    assert event_types == ["attempt_started", "retry_scheduled", "attempt_started", "attempt_succeeded"]


def test_job_queue_default_retry_backoff_uses_two_short_retries(client) -> None:
    started_at = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    first_retry_at = started_at + timedelta(seconds=15)
    second_retry_at = first_retry_at + timedelta(seconds=15)
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            run_after=started_at,
        )

        claim_jobs(session, worker_id="worker-a", limit=1, now=started_at)
        fail_job(session, job.id, worker_id="worker-a", error_type="TransientError", now=started_at)
        session.refresh(job)

        assert job.status == JOB_STATUS_PENDING
        assert _as_utc(job.run_after) == first_retry_at

        claim_jobs(session, worker_id="worker-a", limit=1, now=first_retry_at)
        fail_job(session, job.id, worker_id="worker-a", error_type="TransientError", now=first_retry_at)
        session.refresh(job)

        assert job.status == JOB_STATUS_PENDING
        assert _as_utc(job.run_after) == second_retry_at


def test_operation_job_retry_classification_limits_waitable_failures() -> None:
    assert operation_jobs._is_retryable_operation_exception(operation_jobs.OperationJobRetryableError("retry"))
    assert operation_jobs._is_retryable_operation_exception(httpx.ConnectError("connect failed"))
    assert operation_jobs._is_retryable_operation_exception(ConnectionError("connection failed"))
    assert operation_jobs._is_retryable_operation_exception(TimeoutError("timed out"))

    assert not operation_jobs._is_retryable_operation_exception(RuntimeError("logic failed"))
    assert not operation_jobs._is_retryable_operation_exception(ValueError("bad payload"))
    assert not operation_jobs._is_retryable_operation_exception(OSError("local filesystem failed"))


def test_feed_item_artifact_attempt_retry_classification_limits_to_provider_access_failures() -> None:
    def attempt(*, stage: str, error_type: str, error_message: str):
        return SimpleNamespace(stage=stage, error_type=error_type, error_message=error_message)

    assert operation_jobs._is_retryable_feed_item_artifact_attempt(
        attempt(
            stage="provider_request",
            error_type="AIProviderHTTPError",
            error_message="AI provider HTTP 503: temporarily unavailable",
        )
    )
    assert operation_jobs._is_retryable_feed_item_artifact_attempt(
        attempt(
            stage="provider_request",
            error_type="AIProviderError",
            error_message="AI provider request failed: connection refused",
        )
    )

    assert not operation_jobs._is_retryable_feed_item_artifact_attempt(
        attempt(
            stage="provider_request",
            error_type="AIProviderHTTPError",
            error_message="AI provider HTTP 400: context size exceeded",
        )
    )
    assert not operation_jobs._is_retryable_feed_item_artifact_attempt(
        attempt(
            stage="response_validation",
            error_type="ValidationError",
            error_message="invalid response shape",
        )
    )
    assert not operation_jobs._is_retryable_feed_item_artifact_attempt(
        attempt(
            stage="response_parse",
            error_type="InsightEngineResponseParseError",
            error_message="invalid JSON",
        )
    )


def test_job_queue_rejects_non_attempt_boundary_event_types(client) -> None:
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
        )

        with pytest.raises(JobValidationError, match="Unsupported job event type"):
            record_job_event(session, job, "heartbeat", {})


def test_job_queue_claim_retry_heartbeat_cancel_and_manual_requeue(client) -> None:
    started_at = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            idempotency_key="source:1:sync",
            max_attempts=2,
            run_after=started_at,
        )
        pending_job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 2},
            run_after=started_at,
        )

        claimed = claim_jobs(session, worker_id="worker-a", limit=1, lease_seconds=30, now=started_at)
        assert [entry.id for entry in claimed] == [job.id]
        assert claimed[0].status == JOB_STATUS_RUNNING
        assert claimed[0].attempts == 1
        assert claimed[0].locked_by == "worker-a"

        extended = extend_job_lease(
            session,
            job.id,
            worker_id="worker-a",
            lease_seconds=60,
            now=started_at + timedelta(seconds=10),
        )
        assert _as_utc(extended.lease_expires_at) == started_at + timedelta(seconds=70)
        with pytest.raises(JobLeaseError):
            extend_job_lease(session, job.id, worker_id="worker-b")

        retried = fail_job(
            session,
            job.id,
            worker_id="worker-a",
            error_type="TransientError",
            error_message="try again",
            backoff_seconds=5,
            now=started_at + timedelta(seconds=11),
        )
        assert retried.status == JOB_STATUS_PENDING
        assert _as_utc(retried.run_after) == started_at + timedelta(seconds=16)
        assert retried.locked_by is None

        cancel_job(session, pending_job.id, reason="test cleanup")
        assert pending_job.status == JOB_STATUS_CANCELLED

        claimed_again = claim_jobs(
            session,
            worker_id="worker-a",
            limit=1,
            lease_seconds=30,
            now=started_at + timedelta(seconds=20),
        )
        assert [entry.id for entry in claimed_again] == [job.id]

        failed = fail_job(
            session,
            job.id,
            worker_id="worker-a",
            error_type="PermanentError",
            error_message="done retrying",
            retry=False,
            now=started_at + timedelta(seconds=21),
        )
        assert failed.status == JOB_STATUS_FAILED

        requeued = manual_requeue_job(session, job.id, reason="manual retry")
        session.commit()

    assert requeued.id != job.id
    assert requeued.status == JOB_STATUS_PENDING
    assert requeued.attempts == 0
    assert requeued.idempotency_key == "source:1:sync"


def test_job_queue_operation_progress_aggregation_is_operation_read_compatible(client) -> None:
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        statuses = [
            JOB_STATUS_PENDING,
            JOB_STATUS_RUNNING,
            JOB_STATUS_SUCCEEDED,
            JOB_STATUS_FAILED,
            JOB_STATUS_CANCELLED,
        ]
        for index, status in enumerate(statuses, start=1):
            job = create_job(
                session,
                operation_id=operation_id,
                job_type="test_job",
                payload={"source_id": index},
            )
            job.status = status
            if status == JOB_STATUS_RUNNING:
                job.locked_by = "worker-a"
            if status in {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}:
                job.finished_at = datetime.now(timezone.utc)
            session.add(job)

        progress = aggregate_operation_progress_from_jobs(
            session,
            operation_id,
            base_progress={"latest_source_name": "Existing Source"},
        )

    progress_read = OperationProgressRead.model_validate(progress)
    assert progress["total"] == 5
    assert progress["completed"] == 3
    assert progress["success_count"] == 1
    assert progress["error_count"] == 1
    assert progress["stage"] == "running"
    assert progress["latest_source_name"] == "Existing Source"
    assert progress["job_pending_count"] == 1
    assert progress["job_running_count"] == 1
    assert progress["job_succeeded_count"] == 1
    assert progress["job_failed_count"] == 1
    assert progress["job_cancelled_count"] == 1
    assert progress_read.job_pending_count == 1
    assert progress_read.job_running_count == 1
    assert progress_read.job_succeeded_count == 1
    assert progress_read.job_failed_count == 1
    assert progress_read.job_cancelled_count == 1


def test_job_queue_operation_progress_aggregation_preserves_specific_running_stage(client) -> None:
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="knowledge_capture",
            payload={"saved_item_id": 1},
        )
        job.status = JOB_STATUS_RUNNING
        job.locked_by = "worker-a"
        session.add(job)

        progress = aggregate_operation_progress_from_jobs(
            session,
            operation_id,
            base_progress={"stage": "capture_crawl4ai", "latest_source_name": "Existing Source"},
        )

    OperationProgressRead.model_validate(progress)
    assert progress["stage"] == "capture_crawl4ai"
    assert progress["job_running_count"] == 1
    assert progress["latest_source_name"] == "Existing Source"


def test_job_queue_complete_job(client) -> None:
    completed_at = datetime(2026, 5, 15, 13, 0, tzinfo=timezone.utc)
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            run_after=completed_at,
        )
        claim_jobs(session, worker_id="worker-a", limit=1, now=completed_at)

        complete_job(session, job.id, worker_id="worker-a", result={"items_seen_count": 3}, now=completed_at)
        session.commit()

    with get_session_factory()() as session:
        persisted = session.scalar(select(Job).where(Job.id == job.id))

    assert persisted is not None
    assert persisted.status == JOB_STATUS_SUCCEEDED
    assert _as_utc(persisted.finished_at) == completed_at
    assert persisted.lease_expires_at is None


def test_job_queue_terminal_writes_require_current_worker_and_active_lease(client) -> None:
    started_at = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        pending_job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            run_after=started_at,
        )
        with pytest.raises(JobLeaseError, match="not running"):
            complete_job(session, pending_job.id, worker_id="worker-a", now=started_at)

        claim_jobs(session, worker_id="worker-a", limit=1, lease_seconds=10, now=started_at)
        with pytest.raises(JobLeaseError, match="different worker"):
            complete_job(session, pending_job.id, worker_id="worker-b", now=started_at + timedelta(seconds=1))
        with pytest.raises(JobLeaseError, match="expired"):
            fail_job(
                session,
                pending_job.id,
                worker_id="worker-a",
                error_message="too late",
                now=started_at + timedelta(seconds=11),
            )


def test_job_queue_running_cancel_can_finish_cleanly(client) -> None:
    started_at = datetime(2026, 5, 15, 15, 0, tzinfo=timezone.utc)
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            run_after=started_at,
        )
        claim_jobs(session, worker_id="worker-a", limit=1, lease_seconds=30, now=started_at)
        cancel_job(session, job.id, reason="user requested")

        cancelled = finish_running_job_cancelled(
            session,
            job.id,
            worker_id="worker-a",
            reason="user requested",
            now=started_at + timedelta(seconds=2),
        )
        session.commit()

    assert cancelled.status == JOB_STATUS_CANCELLED
    assert cancelled.locked_by is None
    assert cancelled.lease_expires_at is None

    with get_session_factory()() as session:
        event_type = session.scalar(select(JobEvent.event_type).where(JobEvent.job_id == job.id).order_by(JobEvent.id.desc()))
    assert event_type == "attempt_cancelled"


def test_job_queue_error_message_is_redacted_and_truncated(client) -> None:
    started_at = datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc)
    secret_message = "Authorization: Bearer abcdefghijklmnop api_key=sk-secret " + ("x" * 5000)
    with get_session_factory()() as session:
        operation_id = _create_operation(session)
        job = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            run_after=started_at,
        )
        claim_jobs(session, worker_id="worker-a", limit=1, lease_seconds=30, now=started_at)
        fail_job(
            session,
            job.id,
            worker_id="worker-a",
            error_type="ProviderError",
            error_message=secret_message,
            error_detail={"message": secret_message, "nested": ["Bearer qwertyuiopasdfgh"]},
            retry=False,
            now=started_at + timedelta(seconds=1),
        )
        session.commit()

    with get_session_factory()() as session:
        persisted = session.get(Job, job.id)
        event_payload = session.scalar(
            select(JobEvent.event_payload_json)
            .where(JobEvent.job_id == job.id, JobEvent.event_type == "attempt_failed")
            .order_by(JobEvent.id.desc())
        )

    assert persisted is not None
    assert persisted.error_message is not None
    assert "abcdefghijklmnop" not in persisted.error_message
    assert "sk-secret" not in persisted.error_message
    assert "[REDACTED]" in persisted.error_message
    assert persisted.error_message.endswith("...[truncated]")
    assert len(persisted.error_message.encode("utf-8")) <= 4110
    assert persisted.error_detail_json is not None
    assert "sk-secret" not in persisted.error_detail_json["message"]
    assert "abcdefghijklmnop" not in persisted.error_detail_json["message"]
    assert "qwertyuiopasdfgh" not in persisted.error_detail_json["nested"][0]
    assert event_payload is not None
    assert "sk-secret" not in event_payload["error_message"]
    assert "abcdefghijklmnop" not in event_payload["error_message"]


def test_sync_all_job_backed_operations_skips_terminal_history(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        terminal_operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_SOURCE_SYNC,
            operation_kind="all",
            status=operation_jobs.STATUS_COMPLETED,
            progress_json={"stage": "completed"},
        )
        active_operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_SOURCE_SYNC,
            operation_kind="all",
            status=operation_jobs.STATUS_QUEUED,
            progress_json={"stage": "queued"},
        )
        session.add_all([terminal_operation, active_operation])
        session.flush()
        create_job(
            session,
            operation_id=terminal_operation.id,
            job_type=operation_jobs.JOB_TYPE_SOURCE_SYNC_ONE_SOURCE,
            payload={"source_id": 1},
            queue_name=operation_jobs.SOURCE_SYNC_QUEUE_NAME,
        )
        create_job(
            session,
            operation_id=active_operation.id,
            job_type=operation_jobs.JOB_TYPE_SOURCE_SYNC_ONE_SOURCE,
            payload={"source_id": 2},
            queue_name=operation_jobs.SOURCE_SYNC_QUEUE_NAME,
        )

        synced_operation_ids: list[int] = []

        def record_sync(sync_session, operation, *, now=None):
            synced_operation_ids.append(operation.id)
            return operation

        monkeypatch.setattr(operation_jobs, "sync_job_backed_operation_state", record_sync)

        operation_jobs._sync_all_job_backed_operations(session)

    assert synced_operation_ids == [active_operation.id]


def _create_operation(session) -> int:
    operation = AppOperation(
        operation_type="source_sync",
        operation_kind="single",
        status="queued",
        progress_json={
            "stage": "idle",
            "total": 1,
            "completed": 0,
            "success_count": 0,
            "error_count": 0,
            "stopped": False,
            "stop_requested": False,
        },
    )
    session.add(operation)
    session.flush()
    return operation.id


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)
