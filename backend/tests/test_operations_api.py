from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, inspect, select

from app.core.config import reset_settings_cache
from app.db.models import (
    AppOperation,
    FeedItem,
    FeedItemHomepageReadModel,
    FeedItemStructuredFeedback,
    InsightEngineArtifact,
    InsightEngineAttempt,
    ItemState,
    Job,
    SavedItem,
    SavedItemCaptureRun,
    SavedItemCaptureToolCall,
    SavedItemMirrorExport,
    Source,
    SourceChannelTendency,
    SourceItemOccurrence,
)
from app.db.session import get_engine, get_session_factory
from app.schemas.operations import (
    FeedItemInsightArtifactOperationCreate,
    FeedItemInsightArtifactBatchOperationCreate,
    ManualUrlCaptureOperationCreate,
    SavedItemInsightOperationCreate,
    SourceSyncOperationCreate,
)
from app.schemas.source import SingleSourceSyncResponse, SourceFetchRead, FeedItemInsightArtifactBuildStats
from app.services import ai_provider, channel_lifecycle, feed_content_extraction, operations as operations_service, source_sync
from app.services import operation_jobs
from app.services.job_queue import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    claim_jobs,
    complete_job,
)
from app.services.knowledge_constants import (
    CAPTURE_TOOL_STATUS_FAILED,
    DEFAULT_CAPTURE_TARGETS,
    PROCESSING_STATUS_FAILED,
)
from app.services.homepage_read_model import rebuild_homepage_read_models_for_feed_items
from app.services.knowledge_snapshot_assets import build_saved_item_archive_dir_name
from app.services.saved_item_insights import SavedItemInsightConfigurationError


RSS_SAMPLE_ALPHA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Alpha Feed</title>
    <item>
      <guid>alpha-1</guid>
      <title>Alpha Story One</title>
      <link>https://alpha.example.com/posts/1</link>
      <description>Alpha summary one</description>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>alpha-2</guid>
      <title>Alpha Story Two</title>
      <link>https://alpha.example.com/posts/2</link>
      <description>Alpha summary two</description>
      <pubDate>Sat, 28 Mar 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def _wait_for_job_status(job_id: int, expected_status: str, *, timeout_seconds: float = 2.0) -> Job:
    deadline = time.monotonic() + timeout_seconds
    last_job: Job | None = None
    while time.monotonic() < deadline:
        with get_session_factory()() as session:
            job = session.get(Job, job_id)
            assert job is not None
            last_job = job
            if job.status == expected_status:
                return job
        time.sleep(0.01)
    assert last_job is not None
    assert last_job.status == expected_status
    return last_job


def _drain_operation(operation_id: int) -> None:
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)


def _request_stop_and_drain(operation_id: int) -> None:
    with get_session_factory()() as session:
        operations_service.request_stop_operation(session, operation_id)
        session.commit()
    _drain_operation(operation_id)


RSS_SAMPLE_BETA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Beta Feed</title>
    <item>
      <guid>beta-1</guid>
      <title>Beta Story One</title>
      <link>https://beta.example.com/posts/1</link>
      <description>Beta summary one</description>
      <pubDate>Sat, 28 Mar 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_app_operations_table_has_expected_columns(client) -> None:
    inspector = inspect(get_engine())

    columns = {column["name"] for column in inspector.get_columns("app_operations")}

    assert {
        "id",
        "operation_type",
        "operation_kind",
        "status",
        "source_id",
        "scope_json",
        "progress_json",
        "result_json",
        "message",
        "error_message",
        "stop_requested",
        "runner_id",
        "created_at",
        "started_at",
        "updated_at",
        "completed_at",
    }.issubset(columns)


def test_active_operation_can_be_read_stopped_and_blocks_other_operation(client) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id),
        )
        operation_id = operation.id
        session.commit()

    active_response = client.get("/api/operations/active")
    assert active_response.status_code == 200
    active = active_response.json()
    assert active["id"] == operation_id
    assert active["status"] == "queued"
    assert active["operation_type"] == "source_sync"

    conflict_response = client.post("/api/operations/feed-item-insight-artifacts", json={"scope": "global", "limit": 1})
    assert conflict_response.status_code == 409
    assert conflict_response.json()["detail"]["operation"]["id"] == operation_id

    stop_response = client.post(f"/api/operations/{operation_id}/stop")
    assert stop_response.status_code == 200
    stopped = stop_response.json()
    assert stopped["status"] == "stopping"
    assert stopped["stop_requested"] is True
    assert stopped["progress"]["stop_requested"] is True
    assert stopped["progress"]["stage"] == "stopping"

    _drain_operation(operation_id)
    stopped_response = client.get(f"/api/operations/{operation_id}")
    assert stopped_response.status_code == 200
    stopped = stopped_response.json()
    assert stopped["status"] == "stopped"
    assert stopped["progress"]["stage"] == "stopped"


def test_source_sync_endpoint_wakes_worker_without_draining_jobs(client, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("app.api.routes.operations.wake_operation_job_worker", lambda: calls.append("wake"))
    source_id = create_source_for_sync(client, "Wake Source Sync", "https://wake-source-sync.example.com/feed.xml")

    response = client.post(
        "/api/operations/source-sync",
        json={"kind": "single", "source_id": source_id, "feed_item_insight_artifact_build_limit": 0},
    )

    assert response.status_code == 202
    assert calls == ["wake"]
    operation_id = response.json()["id"]
    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id)))

    assert operation is not None
    assert operation.status == "queued"
    assert len(jobs) == 1
    assert jobs[0].status == JOB_STATUS_PENDING


def test_stop_endpoint_wakes_worker_without_draining_jobs(client, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("app.api.routes.operations.wake_operation_job_worker", lambda: calls.append("wake"))
    source_id = create_source_for_sync(client, "Wake Stop", "https://wake-stop.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id),
        )
        session.commit()
        operation_id = operation.id

    response = client.post(f"/api/operations/{operation_id}/stop")

    assert response.status_code == 200
    assert calls == ["wake"]
    payload = response.json()
    assert payload["status"] == "stopping"
    assert payload["stop_requested"] is True
    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id)))

    assert operation is not None
    assert operation.status == "stopping"
    assert len(jobs) == 1
    assert jobs[0].status == JOB_STATUS_PENDING


def test_reader_mark_saved_wakes_worker_without_draining_capture_job(client, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("app.api.routes.reader.wake_operation_job_worker", lambda: calls.append("wake"))
    with get_session_factory()() as session:
        source = Source(
            name="Reader Wake Source",
            kind="rss",
            url="https://reader-wake.example.com/feed.xml",
            site_url="https://reader-wake.example.com",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        feed_item = FeedItem(
            original_url="https://reader-wake.example.com/posts/1",
            canonical_url="https://reader-wake.example.com/posts/1",
            title="Reader Wake Save",
            summary_text="Reader wake summary.",
            content_text="Reader wake content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        session.add(ItemState(feed_item_id=feed_item.id, status="unread"))
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=feed_item.id,
                external_id=feed_item.canonical_url,
                original_url=feed_item.original_url,
                canonical_url=feed_item.canonical_url,
                match_rule="external_id",
                match_value=feed_item.canonical_url,
            )
        )
        session.commit()
        feed_item_id = feed_item.id

    response = client.post(f"/api/reader/items/{feed_item_id}/mark_saved")

    assert response.status_code == 200
    saved_item_id = response.json()["saved_item_id"]
    assert saved_item_id is not None
    assert calls == ["wake"]
    with get_session_factory()() as session:
        operation = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "knowledge_capture")
            .where(AppOperation.operation_kind == "feed_saved_capture")
        )
        assert operation is not None
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation.id)))

    assert operation.status == "queued"
    assert len(jobs) == 1
    assert jobs[0].status == JOB_STATUS_PENDING


def test_active_endpoint_marks_old_runner_operation_interrupted(client) -> None:
    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="source_sync",
            operation_kind="all",
            status="running",
            scope_json={"kind": "all"},
            progress_json=_initial_progress(),
            runner_id="old-process",
        )
        session.add(operation)
        session.commit()
        operation_id = operation.id

    active_response = client.get("/api/operations/active")
    assert active_response.status_code == 200
    assert active_response.json() is None

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "failed"
        assert operation.completed_at is not None
        assert operation.error_message == "Job-backed operation has no jobs."


def test_active_endpoint_finalizes_old_runner_capture_run(client) -> None:
    create_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/stale-capture", "title": "Stale Capture"},
    )
    assert create_response.status_code == 202
    create_operation_id = create_response.json()["id"]
    _drain_operation(create_operation_id)
    saved_item_id = client.get(f"/api/operations/{create_operation_id}").json()["scope"]["saved_item_id"]
    now = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        capture_run = SavedItemCaptureRun(
            saved_item_id=saved_item_id,
            status="pending",
            requested_targets_json={"targets": list(DEFAULT_CAPTURE_TARGETS)},
            pipeline_version="archive_pipeline_v2",
            trigger_kind="manual_retry",
            retry_of_run_id=None,
            started_at=now,
        )
        session.add(capture_run)
        session.flush()
        session.add(
            SavedItemCaptureToolCall(
                capture_run_id=capture_run.id,
                tool_name="singlefile",
                tool_version="2.0.83",
                status="failed",
                exit_code=143,
                error_summary="SingleFile extension capture exited with code 143: no output",
                started_at=now,
                finished_at=now,
            )
        )
        operation = AppOperation(
            operation_type="knowledge_capture",
            operation_kind="retry_capture",
            status="running",
            scope_json={"saved_item_id": saved_item_id},
            progress_json=_initial_progress(),
            runner_id="old-process",
        )
        session.add(operation)
        session.commit()
        capture_run_id = capture_run.id
        operation_id = operation.id

    active_response = client.get("/api/operations/active")
    assert active_response.status_code == 200
    assert active_response.json() is None

    with get_session_factory()() as session:
        capture_run = session.get(SavedItemCaptureRun, capture_run_id)
        assert capture_run is not None
        assert capture_run.status == "failed"
        assert capture_run.finished_at is not None
        assert capture_run.error_summary == "Job-backed operation has no jobs."
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "failed"
        assert operation.error_message == "Job-backed operation has no jobs."


def test_channel_retire_operation_enqueues_jobs_and_appends_audit_routing(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    version = seed_response.json()[0]["channel_catalog_version"]
    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "phase4_operation_retire",
            "label": "Phase 4 Operation Retire",
            "sort_order": 77,
            "expected_channel_catalog_version": version,
        },
    )
    assert create_response.status_code == 201
    version = create_response.json()["channel_catalog_version"]

    with get_session_factory()() as session:
        source = Source(
            name="Channel Operation Source",
            kind="rss",
            url="https://channel-operation.example.com/feed.xml",
            site_url="https://channel-operation.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://channel-operation.example.com/posts/1",
            canonical_url="https://channel-operation.example.com/posts/1",
            title="Channel Operation Item",
            published_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
            summary_text="Channel operation summary.",
            content_text="Channel operation content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(item)
        session.flush()
        session.add(ItemState(feed_item_id=item.id, status="unread"))
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=item.id,
                external_id=item.canonical_url,
                original_url=item.original_url,
                canonical_url=item.canonical_url,
                published_at=item.published_at,
                match_rule="external_id",
                match_value=item.canonical_url,
            )
        )
        session.add(
            InsightEngineArtifact(
                feed_item_id=item.id,
                artifact_kind="feed_item_routing_v1",
                model_name="test",
                prompt_version="test",
                artifact_payload={
                    "schema_version": "feed_item_routing_v1",
                    "primary_channel_id": "phase4_operation_retire",
                    "secondary_channels": [
                        {"channel_id": "ai_agents_frontier", "confidence": 0.6, "display_mode": "tag_only"}
                    ],
                    "reading_depth": "medium",
                    "flow_lane": "main",
                    "confidence": 0.7,
                    "reason_codes": ["content_topic_match"],
                    "promotion_reason_codes": [],
                    "isolation_reason_codes": [],
                    "source_policy_applied": "normal",
                    "notes": "Retire operation fixture.",
                    "source": "ai",
                },
            )
        )
        session.add(
            SourceChannelTendency(
                source_id=source.id,
                channel_id="phase4_operation_retire",
                tendency=0.8,
                is_primary=True,
                notes="historical tendency",
            )
        )
        session.add(
            FeedItemStructuredFeedback(
                feed_item_id=item.id,
                primary_channel_correction="phase4_operation_retire",
                channel_add_ids_json=[],
                channel_remove_ids_json=[],
            )
        )
        session.commit()
        item_id = item.id

    retire_response = client.post(
        "/api/settings/insight-channels/phase4_operation_retire/retire",
        json={"confirm_retire": True, "expected_channel_catalog_version": version},
    )
    assert retire_response.status_code == 202
    operation_id = retire_response.json()["id"]
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.operation_type == "channel_lifecycle"
        assert operation.operation_kind == "retire_channel"
        assert operation.scope_json["channel_id"] == "phase4_operation_retire"
        assert operation.status == "completed"
        assert operation.result_json["affected_feed_item_count"] == 1
        assert operation.result_json["artifact_rows_created"] == 1
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.job_type == "channel_reroute_item"
        assert job.queue_name == "channel_lifecycle"
        assert job.idempotency_key == f"channel_reroute_item:{item_id}:{version + 1}"
        assert job.status == JOB_STATUS_SUCCEEDED
        artifacts = list(
            session.scalars(
                select(InsightEngineArtifact)
                .where(InsightEngineArtifact.feed_item_id == item_id)
                .where(InsightEngineArtifact.artifact_kind == "feed_item_routing_v1")
                .order_by(InsightEngineArtifact.id.asc())
            )
        )
        assert len(artifacts) == 2
        original_payload = artifacts[0].artifact_payload
        reroute_payload = artifacts[1].artifact_payload
        assert original_payload["primary_channel_id"] == "phase4_operation_retire"
        assert reroute_payload["primary_channel_id"] == "ai_agents_frontier"
        assert reroute_payload["source"] == "channel_lifecycle"
        assert reroute_payload["secondary_channels"] == []
        assert reroute_payload["reason_codes"] == ["channel_deleted_fallback"]
        assert f"operation_id={operation_id}" in reroute_payload["notes"]
        assert f"catalog_version={version + 1}" in reroute_payload["notes"]
        assert reroute_payload["lifecycle_metadata"] == {
            "operation_id": operation_id,
            "channel_catalog_version": version + 1,
            "retired_channel_id": "phase4_operation_retire",
            "fallback_channel_id": "needs_review",
            "previous_channel_ids": ["ai_agents_frontier", "phase4_operation_retire"],
            "reason": "channel_retired",
        }
        feedback = session.scalar(
            select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == item_id)
        )
        assert feedback is not None
        assert feedback.primary_channel_correction == "phase4_operation_retire"
        tendency = session.scalar(
            select(SourceChannelTendency).where(SourceChannelTendency.channel_id == "phase4_operation_retire")
        )
        assert tendency is not None


def test_channel_retire_rejects_conflicting_global_operation(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    version = seed_response.json()[0]["channel_catalog_version"]
    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "phase4_conflict",
            "label": "Phase 4 Conflict",
            "sort_order": 78,
            "expected_channel_catalog_version": version,
        },
    )
    assert create_response.status_code == 201
    version = create_response.json()["channel_catalog_version"]

    with get_session_factory()() as session:
        active_operation = AppOperation(
            operation_type="source_sync",
            operation_kind="all",
            status="queued",
            scope_json={"kind": "all"},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        session.add(active_operation)
        session.commit()
        active_operation_id = active_operation.id

    retire_response = client.post(
        "/api/settings/insight-channels/phase4_conflict/retire",
        json={"confirm_retire": True, "expected_channel_catalog_version": version},
    )

    assert retire_response.status_code == 409
    assert retire_response.json()["detail"]["operation"]["id"] == active_operation_id


def test_channel_reroute_job_noops_when_latest_route_no_longer_references_retired_channel(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        item = _create_channel_lifecycle_item(
            session,
            channel_id="ai_agents_frontier",
            secondary_channels=[],
        )
        result = channel_lifecycle.reroute_feed_item_for_retired_channel(
            session,
            feed_item_id=item.id,
            retired_channel_id="phase4_already_moved",
            fallback_channel_id="needs_review",
            channel_catalog_version=2,
            operation_id=999,
            reason="channel_retired",
        )
        routing_count = session.scalar(
            select(func.count())
            .select_from(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item.id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_routing_v1")
        )

    assert result.artifact_created is False
    assert routing_count == 1


def test_channel_reroute_job_noops_when_only_feedback_references_retired_channel(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        item = _create_channel_lifecycle_item(
            session,
            channel_id="ai_agents_frontier",
            secondary_channels=[],
        )
        session.add(
            FeedItemStructuredFeedback(
                feed_item_id=item.id,
                primary_channel_correction="phase4_feedback_only",
                channel_add_ids_json=[],
                channel_remove_ids_json=["phase4_feedback_only"],
            )
        )
        session.flush()

        result = channel_lifecycle.reroute_feed_item_for_retired_channel(
            session,
            feed_item_id=item.id,
            retired_channel_id="phase4_feedback_only",
            fallback_channel_id="needs_review",
            channel_catalog_version=2,
            operation_id=1001,
            reason="channel_retired",
        )
        routing_count = session.scalar(
            select(func.count())
            .select_from(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item.id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_routing_v1")
        )

    assert result.artifact_created is False
    assert result.previous_channel_ids == ["ai_agents_frontier", "phase4_feedback_only"]
    assert routing_count == 1


def test_channel_lifecycle_operation_stop_is_rejected_after_retirement_starts(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    version = seed_response.json()[0]["channel_catalog_version"]
    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "phase4_stop_guard",
            "label": "Phase 4 Stop Guard",
            "sort_order": 79,
            "expected_channel_catalog_version": version,
        },
    )
    assert create_response.status_code == 201
    version = create_response.json()["channel_catalog_version"]

    with get_session_factory()() as session:
        item = _create_channel_lifecycle_item(
            session,
            channel_id="phase4_stop_guard",
            secondary_channels=[],
        )
        rebuild_homepage_read_models_for_feed_items(session, [item.id])
        row = session.get(FeedItemHomepageReadModel, item.id)
        assert row is not None
        assert row.is_stale is False
        operation = operations_service.create_channel_retire_operation(
            session,
            channel_id="phase4_stop_guard",
            expected_channel_catalog_version=version,
        )
        operation_id = operation.id
        session.flush()
        row = session.get(FeedItemHomepageReadModel, item.id)
        assert row is not None
        assert row.is_stale is True
        assert row.stale_reason == "channel_retired"
        session.commit()

    stop_response = client.post(f"/api/operations/{operation_id}/stop")

    assert stop_response.status_code == 400
    assert "Channel lifecycle operations cannot be stopped" in stop_response.json()["detail"]
    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "queued"
        assert operation.stop_requested is False
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.status == JOB_STATUS_PENDING


def test_channel_retire_marks_source_tendency_fallback_homepage_rows_stale(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    version = seed_response.json()[0]["channel_catalog_version"]
    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "phase4_tendency_retire",
            "label": "Phase 4 Tendency Retire",
            "sort_order": 81,
            "expected_channel_catalog_version": version,
        },
    )
    assert create_response.status_code == 201
    version = create_response.json()["channel_catalog_version"]

    with get_session_factory()() as session:
        source = Source(
            name="Lifecycle Tendency Source",
            kind="rss",
            url="https://phase4-tendency-retire.example.com/feed.xml",
            site_url="https://phase4-tendency-retire.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://phase4-tendency-retire.example.com/posts/1",
            canonical_url="https://phase4-tendency-retire.example.com/posts/1",
            title="Lifecycle Tendency Item",
            published_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
            summary_text="Lifecycle tendency summary.",
            content_text="Lifecycle tendency content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(item)
        session.flush()
        session.add(ItemState(feed_item_id=item.id, status="unread"))
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=item.id,
                external_id=item.canonical_url,
                original_url=item.original_url,
                canonical_url=item.canonical_url,
                published_at=item.published_at,
                match_rule="external_id",
                match_value=item.canonical_url,
            )
        )
        session.add(
            SourceChannelTendency(
                source_id=source.id,
                channel_id="phase4_tendency_retire",
                tendency=0.9,
                is_primary=True,
                notes="source tendency fallback fixture",
            )
        )
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, [item.id])
        row = session.get(FeedItemHomepageReadModel, item.id)
        assert row is not None
        assert row.is_stale is False
        assert row.card_json["primary_channel_id"] == "phase4_tendency_retire"

        operation = operations_service.create_channel_retire_operation(
            session,
            channel_id="phase4_tendency_retire",
            expected_channel_catalog_version=version,
        )
        session.flush()
        row = session.get(FeedItemHomepageReadModel, item.id)
        assert row is not None
        assert row.is_stale is True
        assert row.stale_reason == "channel_retired"
        assert operation.result_json["affected_feed_item_count"] == 0
        session.commit()


def test_channel_lifecycle_pending_job_recovers_and_completes(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    version = seed_response.json()[0]["channel_catalog_version"]
    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "phase4_recovery",
            "label": "Phase 4 Recovery",
            "sort_order": 79,
            "expected_channel_catalog_version": version,
        },
    )
    assert create_response.status_code == 201
    version = create_response.json()["channel_catalog_version"]

    with get_session_factory()() as session:
        item = _create_channel_lifecycle_item(
            session,
            channel_id="phase4_recovery",
            secondary_channels=[],
        )
        item_id = item.id
        operation = operations_service.create_channel_retire_operation(
            session,
            channel_id="phase4_recovery",
            expected_channel_catalog_version=version,
        )
        operation_id = operation.id
        session.commit()

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "queued"
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.status == JOB_STATUS_PENDING

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="phase4-recovery-worker",
        max_drain_seconds=None,
        max_concurrent_jobs=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "completed"
        assert operation.result_json["artifact_rows_created"] == 1
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.status == JOB_STATUS_SUCCEEDED
        latest_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_routing_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert latest_artifact is not None
        assert latest_artifact.artifact_payload["source"] == "channel_lifecycle"
        assert latest_artifact.artifact_payload["primary_channel_id"] == "needs_review"


def test_channel_reroute_candidate_tie_break_uses_confidence_sort_order_and_id(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    version = seed_response.json()[0]["channel_catalog_version"]
    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "phase4_tie_channel",
            "label": "Phase 4 Tie Channel",
            "sort_order": 35,
            "expected_channel_catalog_version": version,
        },
    )
    assert create_response.status_code == 201

    with get_session_factory()() as session:
        item = _create_channel_lifecycle_item(
            session,
            channel_id="phase4_retired_tie",
            secondary_channels=[
                {"channel_id": "academic_horizon", "display_mode": "tag_only"},
                {"channel_id": "phase4_tie_channel", "confidence": 0.8, "display_mode": "list"},
                {"channel_id": "ai_agents_frontier", "confidence": 0.8, "display_mode": "list"},
            ],
        )
        result = channel_lifecycle.reroute_feed_item_for_retired_channel(
            session,
            feed_item_id=item.id,
            retired_channel_id="phase4_retired_tie",
            fallback_channel_id="needs_review",
            channel_catalog_version=3,
            operation_id=1000,
            reason="channel_retired",
        )
        latest_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item.id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_routing_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )

    assert result.artifact_created is True
    assert result.new_primary_channel_id == "phase4_tie_channel"
    assert latest_artifact is not None
    assert latest_artifact.artifact_payload["primary_channel_id"] == "phase4_tie_channel"
    assert latest_artifact.artifact_payload["secondary_channels"] == [
        {"channel_id": "ai_agents_frontier", "confidence": 0.8, "display_mode": "list"},
        {"channel_id": "academic_horizon", "confidence": 0.0, "display_mode": "list"},
    ]


def test_source_sync_operation_runner_persists_progress_and_result(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(monkeypatch, {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA)})

    create_response = client.post(
        "/api/operations/source-sync",
        json={"kind": "single", "source_id": source_id, "feed_item_insight_artifact_build_limit": 0},
    )
    assert create_response.status_code == 202
    operation_id = create_response.json()["id"]
    _drain_operation(operation_id)

    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["operation_type"] == "source_sync"
    assert operation["status"] == "completed", operation
    assert operation["progress"]["completed"] == 1
    assert operation["progress"]["success_count"] == 1
    assert operation["result"]["source_id"] == source_id
    assert operation["result"]["fetch"]["items_seen_count"] == 2
    assert operation["result"]["fetch"]["items_created_count"] == 2

    assert client.get("/api/operations/active").json() is None


def test_source_sync_operation_selected_sources_queues_only_requested_sources(client) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    create_source_for_sync(client, "Gamma", "https://gamma.example.com/feed.xml")

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(
                kind="selected",
                source_ids=[beta_id, alpha_id, beta_id],
                feed_item_insight_artifact_build_limit=0,
            ),
        )
        operation_id = operation.id
        session.commit()

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.operation_kind == "selected"
        assert operation.source_id is None
        assert operation.scope_json["source_ids"] == [beta_id, alpha_id]
        assert operation.progress_json["total"] == 2

        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id)))
        assert [job.payload_json["source_id"] for job in jobs] == [beta_id, alpha_id]


def test_source_sync_operation_rejects_removed_artifact_limit_alias(client) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")

    response = client.post(
        "/api/operations/source-sync",
        json={
            "kind": "single",
            "source_id": source_id,
            "feed_item_insight_artifact_build_limit": 0,
            "homepage_artifact_build_limit": 1,
        },
    )

    assert response.status_code == 422
    assert "homepage_artifact_build_limit" in str(response.json())
    assert "Extra inputs are not permitted" in str(response.json())


def test_source_sync_running_progress_preserves_existing_fan_in_entries(client) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=0),
        )
        operation_id = operation.id
        session.commit()

    stale_session = get_session_factory()()
    try:
        stale_operation = stale_session.get(AppOperation, operation_id)
        assert stale_operation is not None

        with get_session_factory()() as session:
            operation = session.get(AppOperation, operation_id)
            assert operation is not None
            progress = dict(operation.progress_json or {})
            progress["_source_sync_result_entries"] = [
                {
                    "source_index": 1,
                    "result": {
                        "source_id": source_id,
                        "source_name": "Alpha",
                        "fetch": {"status": "success", "items_created_count": 1},
                        "created_feed_item_ids": [101],
                        "feed_item_insight_artifacts": {
                            "processed_feed_item_count": 0,
                            "artifact_rows_created": 0,
                            "failed_feed_item_count": 0,
                        },
                    },
                }
            ]
            operation.progress_json = progress
            session.add(operation)
            session.commit()

        operation_jobs._write_source_sync_running_progress(
            stale_session,
            stale_operation,
            source_name="Alpha",
            source_index=1,
        )
        stale_session.commit()
    finally:
        stale_session.close()

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        entries = operation.progress_json["_source_sync_result_entries"]
        assert len(entries) == 1
        assert entries[0]["result"]["created_feed_item_ids"] == [101]


def test_global_worker_does_not_claim_source_sync_one_source_jobs(client) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=0),
        )
        session.flush()
        job = session.scalar(select(Job).where(Job.operation_id == operation.id))
        assert job is not None
        job_id = job.id
        session.commit()

    assert operation_jobs.run_next_available_operation_job(worker_id="test-global-worker") is False

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JOB_STATUS_PENDING
        assert job.locked_by is None


@pytest.mark.file_sqlite
def test_source_sync_recovery_worker_drains_pending_source_sync_jobs(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")

    def fake_sync_one_source(session, next_source_id: int, **_kwargs):
        source = session.get(Source, next_source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=1,
                items_created_count=1,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[101],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=0),
        )
        operation_id = operation.id
        session.commit()

    assert (
        operation_jobs.run_next_recoverable_source_sync_operation(
            worker_id="test-source-sync-recovery-worker",
        )
        is True
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "completed"
        assert operation.result_json["source_id"] == source_id
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        job_id = job.id
    _wait_for_job_status(job_id, JOB_STATUS_SUCCEEDED)


def test_operation_worker_prioritizes_source_sync_recovery_over_artifact_backlog(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")

    def fake_sync_one_source(session, next_source_id: int, **_kwargs):
        source = session.get(Source, next_source_id)
        assert source is not None
        operation_jobs._worker_stop_event.set()
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=0,
                items_created_count=0,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)

    with get_session_factory()() as session:
        source_operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=0),
        )
        source_operation_id = source_operation.id
        source_job_id = session.scalar(select(Job.id).where(Job.operation_id == source_operation_id))
        artifact_operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="queued",
            scope_json={"scope": "global", "source_id": None, "limit": 0},
            progress_json=_initial_progress(),
            message="Feed item insight artifact operation queued.",
            runner_id=operation_jobs.PROCESS_WORKER_ID,
        )
        session.add(artifact_operation)
        session.flush()
        artifact_job = operation_jobs.enqueue_feed_item_insight_artifacts_job(
            session,
            artifact_operation,
            scope="global",
            source_id=None,
            limit=0,
        )
        artifact_job_id = artifact_job.id
        session.commit()

    try:
        operation_jobs._worker_stop_event.clear()
        operation_jobs._operation_job_worker_loop(
            worker_id="test-source-sync-priority-worker",
            poll_interval_seconds=0.01,
        )
    finally:
        operation_jobs._worker_stop_event.clear()

    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, source_operation_id)
        source_job = session.get(Job, source_job_id)
        artifact_job = session.get(Job, artifact_job_id)
        assert source_operation is not None
        assert source_operation.status == "completed"
        assert source_job is not None
        assert source_job.status == JOB_STATUS_SUCCEEDED
        assert artifact_job is not None
        assert artifact_job.status == JOB_STATUS_PENDING


def test_source_sync_pipeline_enqueues_after_each_source_job_before_all_sources_complete(client, monkeypatch) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")

    def fake_sync_one_source(session, next_source_id: int, **_kwargs):
        source = session.get(Source, next_source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=1,
                items_created_count=1,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[source.id * 100],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(session, SourceSyncOperationCreate(kind="all"))
        operation_id = operation.id
        claimed = claim_jobs(
            session,
            worker_id="source-sync-pipeline-test",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_SOURCE_SYNC_ONE_SOURCE,),
            operation_id=operation_id,
        )
        assert len(claimed) == 1
        claimed_job_id = claimed[0].id
        session.commit()

    operation_jobs._run_claimed_job(
        claimed_job_id,
        worker_id="source-sync-pipeline-test",
        lease_seconds=60,
        heartbeat_interval_seconds=0,
        failure_backoff_seconds=None,
        now=None,
    )

    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, operation_id)
        assert source_operation is not None
        assert source_operation.status in {"queued", "running"}
        assert len(source_operation.progress_json["_source_sync_result_entries"]) == 1
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        assert followup.status == "queued"
        assert followup.progress_json["insight_total_count"] == 1
        artifact_job = session.scalar(select(Job).where(Job.operation_id == followup.id))
        assert artifact_job is not None
        assert artifact_job.status == JOB_STATUS_PENDING
        assert artifact_job.job_type == operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD
        assert artifact_job.queue_name == operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME
        assert artifact_job.payload_json["feed_item_id"] == alpha_id * 100


def test_source_sync_operation_stop_takes_effect_between_sources(client, monkeypatch) -> None:
    create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(session, SourceSyncOperationCreate(kind="all"))
        operation_id = operation.id
        session.commit()

    synced_source_ids: list[int] = []

    def fake_sync_one_source(session, source_id: int, **_kwargs):
        source = session.get(Source, source_id)
        assert source is not None
        synced_source_ids.append(source.id)
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        operation.stop_requested = True
        session.add(operation)
        session.commit()
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=1,
                items_created_count=1,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)

    operations_service.run_operation_background(operation_id, max_drain_seconds=None)

    assert len(synced_source_ids) == 1
    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "stopped"
        assert operation.stop_requested is True
        assert operation.progress_json["stopped"] is True
        assert operation.result_json["stopped"] is True
        assert len(operation.result_json["results"]) == 1


def test_source_sync_followup_operation_stop_takes_effect_between_item_insights(client, monkeypatch) -> None:
    create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(session, SourceSyncOperationCreate(kind="all"))
        operation_id = operation.id
        session.commit()

    processed_item_ids: list[int] = []

    def fake_sync_one_source(session, source_id: int, **_kwargs):
        source = session.get(Source, source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=2,
                items_created_count=2,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[101, 102],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    def fake_build_feed_item_insight_artifacts_for_item(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        processed_item_ids.append(feed_item_id)
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        followup.stop_requested = True
        session.add(followup)
        session.commit()
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=1,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)
    monkeypatch.setattr(
        operation_jobs,
        "build_feed_item_insight_artifacts_for_item",
        fake_build_feed_item_insight_artifacts_for_item,
    )

    operations_service.run_operation_background(operation_id, max_drain_seconds=None)

    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, operation_id)
        assert source_operation is not None
        assert source_operation.status == "completed"
        assert source_operation.result_json["results"][0]["feed_item_insight_artifacts"]["processed_feed_item_count"] == 0
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        followup_id = followup.id

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="source-sync-stop-followup-ai-lane",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )

    assert processed_item_ids == [101]
    with get_session_factory()() as session:
        followup = session.get(AppOperation, followup_id)
        assert followup is not None
        assert followup.status == "stopped"
        assert followup.progress_json["current_insight_total"] == 2
        assert followup.progress_json["current_insight_completed"] == 1
        assert followup.result_json["stopped"] is True
        assert followup.result_json["processed_feed_item_count"] == 1
        source_operation = session.get(AppOperation, operation_id)
        assert source_operation is not None
        assert source_operation.result_json["results"][0]["feed_item_insight_artifacts"]["processed_feed_item_count"] == 1
        assert source_operation.result_json["results"][0]["feed_item_insight_artifacts"]["artifact_rows_created"] == 1


def test_source_sync_background_drains_linked_artifact_pipeline_without_worker(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    processed_item_ids: list[int] = []

    def fake_sync_one_source(session, next_source_id: int, **_kwargs):
        source = session.get(Source, next_source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=2,
                items_created_count=2,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[101, 102],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    def fake_build_feed_item_insight_artifacts_for_item(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        assert scope.startswith("source-sync:")
        processed_item_ids.append(feed_item_id)
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)
    monkeypatch.setattr(
        operation_jobs,
        "build_feed_item_insight_artifacts_for_item",
        fake_build_feed_item_insight_artifacts_for_item,
    )

    response = client.post(
        "/api/operations/source-sync",
        json={"kind": "single", "source_id": source_id, "feed_item_insight_artifact_build_limit": 2},
    )

    assert response.status_code == 202
    assert processed_item_ids == []
    _drain_operation(response.json()["id"])
    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="source-sync-linked-artifacts-ai-lane",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )
    assert processed_item_ids == [101, 102]
    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, response.json()["id"])
        assert source_operation is not None
        assert source_operation.status == "completed"
        assert source_operation.result_json["feed_item_insight_artifacts"]["processed_feed_item_count"] == 2
        assert source_operation.result_json["feed_item_insight_artifacts"]["artifact_rows_created"] == 8
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        assert followup.status == "completed"
        assert followup.progress_json["insight_total_count"] == 2
        assert followup.progress_json["insight_completed_count"] == 2
        assert followup.result_json["processed_feed_item_count"] == 2
        assert followup.result_json["artifact_rows_created"] == 8
        jobs = list(session.scalars(select(Job).where(Job.operation_id == followup.id).order_by(Job.id.asc())))
        assert len(jobs) == 2
        assert {job.status for job in jobs} == {JOB_STATUS_SUCCEEDED}


def test_source_sync_followup_updates_bulk_result_artifact_stats_per_source(client, monkeypatch) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    item_ids_by_source = {alpha_id: alpha_id * 100, beta_id: beta_id * 100}

    def fake_sync_one_source(session, source_id: int, **_kwargs):
        source = session.get(Source, source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=1,
                items_created_count=1,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[item_ids_by_source[source.id]],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    def fake_build_feed_item_insight_artifacts_for_item(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        assert scope.startswith("source-sync:")
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=3 if feed_item_id == item_ids_by_source[alpha_id] else 5,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)
    monkeypatch.setattr(
        operation_jobs,
        "build_feed_item_insight_artifacts_for_item",
        fake_build_feed_item_insight_artifacts_for_item,
    )

    response = client.post("/api/operations/source-sync", json={"kind": "all"})

    assert response.status_code == 202
    _drain_operation(response.json()["id"])
    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, response.json()["id"])
        assert source_operation is not None
        assert source_operation.status == "completed"
        results_by_source = {result["source_id"]: result for result in source_operation.result_json["results"]}
        assert results_by_source[alpha_id]["feed_item_insight_artifacts"]["processed_feed_item_count"] == 0
        assert results_by_source[beta_id]["feed_item_insight_artifacts"]["processed_feed_item_count"] == 0

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="source-sync-bulk-artifacts-ai-lane",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, response.json()["id"])
        assert source_operation is not None
        assert source_operation.status == "completed"
        results_by_source = {result["source_id"]: result for result in source_operation.result_json["results"]}
        assert results_by_source[alpha_id]["feed_item_insight_artifacts"] == {
            "processed_feed_item_count": 1,
            "artifact_rows_created": 3,
            "failed_feed_item_count": 0,
        }
        assert results_by_source[beta_id]["feed_item_insight_artifacts"] == {
            "processed_feed_item_count": 1,
            "artifact_rows_created": 5,
            "failed_feed_item_count": 0,
        }


def test_source_sync_operation_default_followup_has_no_item_cap(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    created_feed_item_ids = list(range(1, 26))

    def fake_sync_one_source(session, source_id: int, **_kwargs):
        source = session.get(Source, source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=len(created_feed_item_ids),
                items_created_count=len(created_feed_item_ids),
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=created_feed_item_ids,
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)

    response = client.post("/api/operations/source-sync", json={"kind": "single", "source_id": source_id})

    assert response.status_code == 202
    _drain_operation(response.json()["id"])
    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, response.json()["id"])
        assert source_operation is not None
        assert source_operation.scope_json["feed_item_insight_artifact_build_limit"] is None
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        assert followup.scope_json["limit"] is None
        assert followup.progress_json["current_insight_total"] == len(created_feed_item_ids)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == followup.id).order_by(Job.id.asc())))
        assert len(jobs) == len(created_feed_item_ids)
        assert {job.job_type for job in jobs} == {operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD}
        assert {job.queue_name for job in jobs} == {operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME}
        assert jobs[0].payload_json["feed_item_id"] == created_feed_item_ids[0]
        assert jobs[0].payload_json["source_id"] == source_id
        assert jobs[0].payload_json["source_sync_operation_id"] == source_operation.id
        _assert_feed_item_insight_enqueue_context(jobs[0].payload_json, feed_item_id=created_feed_item_ids[0])


def test_source_sync_pipeline_build_limit_and_idempotency_use_source_completion_result(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    created_feed_item_ids = [201, 202, 203]

    def fake_sync_one_source(session, next_source_id: int, **_kwargs):
        source = session.get(Source, next_source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=len(created_feed_item_ids),
                items_created_count=len(created_feed_item_ids),
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=created_feed_item_ids,
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=2),
        )
        operation_id = operation.id
        session.commit()

    operations_service.run_operation_background(operation_id, max_drain_seconds=None)

    with get_session_factory()() as session:
        source_operation = session.get(AppOperation, operation_id)
        assert source_operation is not None
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        jobs = list(session.scalars(select(Job).where(Job.operation_id == followup.id).order_by(Job.id.asc())))
        assert [job.payload_json["feed_item_id"] for job in jobs] == created_feed_item_ids[:2]
        assert followup.progress_json["insight_total_count"] == 2

        response = SingleSourceSyncResponse.model_validate(source_operation.result_json)
        operation_jobs._enqueue_source_sync_created_item_artifact_jobs(
            session,
            source_operation_id=source_operation.id,
            response=response,
        )
        session.flush()
        jobs_after_duplicate_enqueue = list(session.scalars(select(Job).where(Job.operation_id == followup.id)))
        assert len(jobs_after_duplicate_enqueue) == 2


def test_source_sync_artifact_build_item_retries_retryable_attempt_then_succeeds(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/pipeline-retry",
            canonical_url="https://alpha.example.com/posts/pipeline-retry",
            title="Pipeline Retry",
            summary_text="Pipeline retry summary.",
            content_text="Pipeline retry content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.commit()
        feed_item_id = feed_item.id

    def fake_sync_one_source(session, next_source_id: int, **_kwargs):
        source = session.get(Source, next_source_id)
        assert source is not None
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=SourceFetchRead(
                id=-source.id,
                source_id=source.id,
                requested_url=source.url,
                status="success",
                http_status=200,
                etag=None,
                last_modified=None,
                error_message=None,
                items_seen_count=1,
                items_created_count=1,
                items_matched_count=0,
                items_invalid_count=0,
                fetch_started_at=datetime.now(timezone.utc),
                fetch_finished_at=datetime.now(timezone.utc),
            ),
            created_feed_item_ids=[feed_item_id],
            feed_item_insight_artifacts=FeedItemInsightArtifactBuildStats(
                processed_feed_item_count=0,
                artifact_rows_created=0,
                failed_feed_item_count=0,
            ),
        )

    calls: list[int] = []

    def build_retry_then_success(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        assert scope.startswith("source-sync:")
        calls.append(feed_item_id)
        if len(calls) == 1:
            session.add(
                InsightEngineAttempt(
                    feed_item_id=feed_item_id,
                    source_id=source_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: temporarily unavailable",
                    artifact_count=0,
                )
            )
            session.flush()
            return operation_jobs.InsightEngineArtifactRebuildStats(
                scope=scope,
                processed_feed_item_count=1,
                artifact_rows_created=0,
                failed_feed_item_count=1,
            )
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "sync_one_source", fake_sync_one_source)
    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", build_retry_then_success)

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=1),
        )
        operation_id = operation.id
        session.commit()

    operations_service.run_operation_background(operation_id, max_drain_seconds=None)

    with get_session_factory()() as session:
        followup = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
        )
        assert followup is not None
        followup_id = followup.id

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="source-sync-pipeline-retry-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
        failure_backoff_seconds=1,
        max_drain_seconds=3,
    )

    assert calls == [feed_item_id, feed_item_id]
    with get_session_factory()() as session:
        followup = session.get(AppOperation, followup_id)
        job = session.scalar(select(Job).where(Job.operation_id == followup_id))
        assert followup is not None
        assert followup.status == "completed"
        assert followup.progress_json["insight_completed_count"] == 1
        assert followup.progress_json["insight_failed_count"] == 0
        assert followup.result_json["processed_feed_item_count"] == 1
        assert followup.result_json["artifact_rows_created"] == 4
        assert job is not None
        assert job.status == JOB_STATUS_SUCCEEDED
        assert job.attempts == 2


def test_active_list_returns_global_and_item_operations(client) -> None:
    with get_session_factory()() as session:
        global_operation = AppOperation(
            operation_type="source_sync",
            operation_kind="all",
            status="running",
            scope_json={"kind": "all"},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        item_operation = AppOperation(
            operation_type="knowledge_capture",
            operation_kind="manual_url",
            status="running",
            scope_json={"saved_item_id": 42},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        session.add_all([global_operation, item_operation])
        session.commit()
        global_operation_id = global_operation.id
        item_operation_id = item_operation.id

    active_list_response = client.get("/api/operations/active-list")
    assert active_list_response.status_code == 200
    active_ids = {operation["id"] for operation in active_list_response.json()}
    assert {global_operation_id, item_operation_id}.issubset(active_ids)

    active_response = client.get("/api/operations/active")
    assert active_response.status_code == 200
    assert active_response.json()["id"] == global_operation_id


def test_manual_url_capture_operation_completes_and_writes_result(client) -> None:
    create_response = client.post(
        "/api/operations/captures/manual-url",
        json={
            "url": "https://manual.example.com/operation-capture",
            "title": "Operation Capture",
            "user_note": "Background capture",
            "ai_route": "general",
        },
    )
    assert create_response.status_code == 202
    operation_id = create_response.json()["id"]
    _drain_operation(operation_id)

    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["status"] == "completed", operation
    assert operation["operation_type"] == "knowledge_capture"
    assert operation["operation_kind"] == "manual_url"
    assert operation["scope"]["saved_item_id"] >= 1
    assert "url" not in operation["scope"]
    assert "title" not in operation["scope"]
    assert "user_note" not in operation["scope"]
    assert "operation-capture" not in operation["message"]
    assert "Operation Capture" not in operation["message"]
    assert "Background capture" not in operation["message"]
    assert operation["result"]["saved_item"]["capture"]["status"] == "succeeded"
    assert operation["result"]["saved_item"]["local_mirror"]["status"] == "succeeded"

    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))
    assert [job.job_type for job in jobs] == [
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE,
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
    ]
    assert jobs[0].payload_json == {
        "saved_item_id": operation["scope"]["saved_item_id"],
        "capture_mode": "manual_url",
        "capture_run_id": operation["scope"]["capture_run_id"],
    }
    assert jobs[1].payload_json == {
        "saved_item_id": operation["scope"]["saved_item_id"],
        "capture_mode": "manual_url",
        "source_capture_run_id": operation["scope"]["capture_run_id"],
    }
    assert {job.status for job in jobs} == {JOB_STATUS_SUCCEEDED}

    assert client.get("/api/operations/active-list").json() == []


def test_uploaded_material_capture_operation_completes_from_multipart(client) -> None:
    create_response = client.post(
        "/api/operations/captures/upload",
        data={
            "material_type": "markdown",
            "title": "Operation Upload",
            "user_note": "Upload through operation route",
            "ai_route": "general",
        },
        files={"file": ("operation-note.md", b"# Operation Upload\n\nUploaded body.", "text/markdown")},
    )
    assert create_response.status_code == 202
    operation_id = create_response.json()["id"]
    _drain_operation(operation_id)

    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["status"] == "completed", operation
    assert operation["operation_type"] == "knowledge_capture"
    assert operation["operation_kind"] == "uploaded_material"
    assert operation["scope"]["saved_item_id"] >= 1
    assert operation["scope"]["material_type"] == "markdown"
    assert operation["scope"]["filename"] == "operation-note.md"
    assert "Uploaded body" not in operation["scope"]
    saved_item = operation["result"]["saved_item"]
    assert saved_item["origin_type"] == "markdown_upload"
    assert saved_item["processing_status"] == "succeeded"
    assert saved_item["source_material_artifact"]["status"] == "available"
    assert any(
        artifact["artifact_kind"] == "uploaded_markdown_v1" and artifact["status"] == "available"
        for artifact in saved_item["archive_artifacts"]
    )
    assert saved_item["local_mirror"]["status"] == "succeeded"

    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))
    assert [job.job_type for job in jobs] == [
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE,
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
    ]
    assert jobs[0].payload_json == {
        "saved_item_id": operation["scope"]["saved_item_id"],
        "capture_mode": "uploaded_material",
    }
    assert "Uploaded body" not in json.dumps(jobs[0].payload_json)
    assert jobs[1].payload_json == {
        "saved_item_id": operation["scope"]["saved_item_id"],
        "capture_mode": "uploaded_material",
    }
    assert {job.status for job in jobs} == {JOB_STATUS_SUCCEEDED}


def test_uploaded_material_capture_operation_html_processing_failure_completes_with_failed_domain_state(
    client,
    monkeypatch,
) -> None:
    def fail_upload_capture(html: str, *, source_name: str | None = None):
        raise RuntimeError("crawl4ai failed for operation upload")

    monkeypatch.setattr("app.services.knowledge_uploads.capture_uploaded_html_markdown", fail_upload_capture)

    create_response = client.post(
        "/api/operations/captures/upload",
        data={
            "material_type": "html",
            "title": "Operation Broken HTML Upload",
            "user_note": "Operation upload failure path",
            "ai_route": "general",
        },
        files={"file": ("broken-operation.html", b"<!doctype html><html><body>Broken</body></html>", "text/html")},
    )
    assert create_response.status_code == 202
    operation_id = create_response.json()["id"]
    _drain_operation(operation_id)

    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["status"] == "completed", operation
    assert operation["operation_type"] == "knowledge_capture"
    assert operation["operation_kind"] == "uploaded_material"
    saved_item = operation["result"]["saved_item"]
    assert saved_item["origin_type"] == "html_upload"
    assert saved_item["processing_status"] == "failed"
    assert "crawl4ai failed" in saved_item["processing_error"]
    assert saved_item["source_material_artifact"]["status"] == "available"
    assert saved_item["local_mirror"] is None

    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))
        mirror_export_count = session.scalar(
            select(func.count())
            .select_from(SavedItemMirrorExport)
            .where(SavedItemMirrorExport.saved_item_id == operation["scope"]["saved_item_id"])
        )
    assert [job.job_type for job in jobs] == [
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE,
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
    ]
    assert {job.status for job in jobs} == {JOB_STATUS_SUCCEEDED}
    assert jobs[1].payload_json == {
        "saved_item_id": operation["scope"]["saved_item_id"],
        "capture_mode": "uploaded_material",
    }
    assert mirror_export_count == 0


def test_feed_saved_capture_operation_enqueues_and_runs_capture_jobs(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Feed Saved Capture", "https://feed-saved.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://feed-saved.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://alpha.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article>Feed saved original page.</article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )
    sync_source_with_operation(client, source_id, artifact_limit=0)
    item_id = client.get("/api/reader/items").json()[0]["id"]

    save_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert save_response.status_code == 200
    saved_item_id = save_response.json()["saved_item_id"]
    assert saved_item_id is not None

    with get_session_factory()() as session:
        operations = list(
            session.scalars(
                select(AppOperation)
                .where(AppOperation.operation_type == "knowledge_capture")
                .where(AppOperation.operation_kind == "feed_saved_capture")
                .order_by(AppOperation.id.desc())
            )
        )
        operation = next(
            (
                candidate
                for candidate in operations
                if int((candidate.scope_json or {}).get("saved_item_id") or 0) == saved_item_id
            ),
            None,
        )
        assert operation is not None
        operation_id = operation.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation.id).order_by(Job.id.asc())))

    assert operation.status == "completed"
    assert operation.result_json["saved_item"]["capture"]["status"] == "succeeded"
    assert [job.job_type for job in jobs] == [
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE,
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
    ]
    assert jobs[0].payload_json["capture_mode"] == "feed_saved_capture"
    assert jobs[1].payload_json["capture_mode"] == "feed_saved_capture"
    assert {job.status for job in jobs} == {JOB_STATUS_SUCCEEDED}


def test_uploaded_material_capture_operation_validation_errors_return_400(client) -> None:
    response = client.post("/api/operations/captures/upload", json={"material_type": "markdown"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Upload request must be multipart/form-data"


def test_capture_operation_validation_errors_return_400(client) -> None:
    blank_url_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "   ", "title": "Blank URL"},
    )
    assert blank_url_response.status_code == 400
    assert blank_url_response.json()["detail"] == "Capture URL must not be empty"

    invalid_route_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/invalid-route", "ai_route": "unsupported"},
    )
    assert invalid_route_response.status_code == 400
    assert invalid_route_response.json()["detail"] == "Unsupported saved item AI route"


def test_retry_capture_operation_finalizes_pending_run_with_existing_tool_calls(client) -> None:
    capture_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/interrupted-capture", "title": "Interrupted Capture"},
    )
    assert capture_response.status_code == 202
    capture_operation_id = capture_response.json()["id"]
    _drain_operation(capture_operation_id)
    saved_item_id = client.get(f"/api/operations/{capture_operation_id}").json()["scope"]["saved_item_id"]

    with get_session_factory()() as session:
        capture_run = session.scalar(
            select(SavedItemCaptureRun)
            .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
            .order_by(SavedItemCaptureRun.id.desc())
            .limit(1)
        )
        assert capture_run is not None
        capture_run.status = "pending"
        capture_run.error_summary = None
        capture_run.finished_at = None
        session.add(capture_run)
        session.commit()
        capture_run_id = capture_run.id

    retry_response = client.post(f"/api/operations/saved-items/{saved_item_id}/capture-runs")
    assert retry_response.status_code == 202
    retry_operation_id = retry_response.json()["id"]
    _drain_operation(retry_operation_id)
    operation = client.get(f"/api/operations/{retry_operation_id}").json()
    assert operation["status"] == "completed", operation
    assert operation["result"]["saved_item"]["capture"]["status"] == "succeeded"

    with get_session_factory()() as session:
        capture_run = session.get(SavedItemCaptureRun, capture_run_id)
        assert capture_run is not None
        assert capture_run.status == "succeeded"
        assert capture_run.finished_at is not None


def test_retry_capture_operation_restarts_failed_interrupted_pending_run(client) -> None:
    capture_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/interrupted-failed-capture", "title": "Interrupted Failed Capture"},
    )
    assert capture_response.status_code == 202
    capture_operation_id = capture_response.json()["id"]
    _drain_operation(capture_operation_id)
    saved_item_id = client.get(f"/api/operations/{capture_operation_id}").json()["scope"]["saved_item_id"]
    now = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        previous_run = session.scalar(
            select(SavedItemCaptureRun)
            .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
            .order_by(SavedItemCaptureRun.id.desc())
            .limit(1)
        )
        assert previous_run is not None
        interrupted_run = SavedItemCaptureRun(
            saved_item_id=saved_item_id,
            status="pending",
            requested_targets_json={"targets": list(DEFAULT_CAPTURE_TARGETS)},
            pipeline_version="archive_pipeline_v2",
            trigger_kind="manual_retry",
            retry_of_run_id=previous_run.id,
            started_at=now,
        )
        session.add(interrupted_run)
        session.flush()
        session.add(
            SavedItemCaptureToolCall(
                capture_run_id=interrupted_run.id,
                tool_name="singlefile",
                tool_version="2.0.83",
                status="failed",
                exit_code=143,
                error_summary="SingleFile extension capture exited with code 143: no output",
                started_at=now,
                finished_at=now,
            )
        )
        session.commit()
        interrupted_run_id = interrupted_run.id

    retry_response = client.post(f"/api/operations/saved-items/{saved_item_id}/capture-runs")
    assert retry_response.status_code == 202
    retry_operation_id = retry_response.json()["id"]
    _drain_operation(retry_operation_id)
    operation = client.get(f"/api/operations/{retry_operation_id}").json()
    assert operation["status"] == "completed", operation
    assert operation["result"]["saved_item"]["capture"]["status"] == "succeeded"
    assert operation["result"]["saved_item"]["capture"]["latest_run_id"] != interrupted_run_id

    with get_session_factory()() as session:
        interrupted_run = session.get(SavedItemCaptureRun, interrupted_run_id)
        assert interrupted_run is not None
        assert interrupted_run.status == "failed"
        assert interrupted_run.finished_at is not None
        assert "SingleFile extension capture exited" in (interrupted_run.error_summary or "")

        latest_run = session.scalar(
            select(SavedItemCaptureRun)
            .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
            .order_by(SavedItemCaptureRun.id.desc())
            .limit(1)
        )
        assert latest_run is not None
        assert latest_run.id != interrupted_run_id
        assert latest_run.retry_of_run_id == interrupted_run_id
        assert latest_run.status == "succeeded"

        latest_call = session.scalar(
            select(SavedItemCaptureToolCall)
            .where(SavedItemCaptureToolCall.capture_run_id == latest_run.id)
            .order_by(SavedItemCaptureToolCall.id.asc())
            .limit(1)
        )
        assert latest_call is not None
        assert latest_call.tool_name == "singlefile_extension"


def test_capture_and_mirror_operations_block_each_other_for_same_saved_item(client) -> None:
    capture_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/mirror-mutex", "title": "Mirror Mutex"},
    )
    assert capture_response.status_code == 202
    capture_operation_id = capture_response.json()["id"]
    _drain_operation(capture_operation_id)
    saved_item_id = client.get(f"/api/operations/{capture_operation_id}").json()["scope"]["saved_item_id"]

    with get_session_factory()() as session:
        capture_operation = AppOperation(
            operation_type="knowledge_capture",
            operation_kind="retry_capture",
            status="running",
            scope_json={"saved_item_id": saved_item_id},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        session.add(capture_operation)
        session.commit()
        capture_operation_id = capture_operation.id

    mirror_response = client.post(f"/api/operations/saved-items/{saved_item_id}/local-mirror-exports")
    assert mirror_response.status_code == 409
    assert mirror_response.json()["detail"]["operation"]["id"] == capture_operation_id

    with get_session_factory()() as session:
        capture_operation = session.get(AppOperation, capture_operation_id)
        assert capture_operation is not None
        capture_operation.status = "completed"
        capture_operation.completed_at = datetime.now(timezone.utc)
        mirror_operation = AppOperation(
            operation_type="local_mirror_export",
            operation_kind="manual_reexport",
            status="running",
            scope_json={"saved_item_id": saved_item_id},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        session.add_all([capture_operation, mirror_operation])
        session.commit()
        mirror_operation_id = mirror_operation.id

    retry_response = client.post(f"/api/operations/saved-items/{saved_item_id}/capture-runs")
    assert retry_response.status_code == 409
    assert retry_response.json()["detail"]["operation"]["id"] == mirror_operation_id


def test_saved_item_insight_and_mirror_operations_block_each_other_for_same_saved_item(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        mirror_operation = AppOperation(
            operation_type="local_mirror_export",
            operation_kind="manual_reexport",
            status="running",
            scope_json={"saved_item_id": saved_item_id},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        session.add(mirror_operation)
        session.commit()
        mirror_operation_id = mirror_operation.id

    insight_response = client.post(
        f"/api/operations/saved-items/{saved_item_id}/insight-rebuild",
        json={"ai_route": "general"},
    )
    assert insight_response.status_code == 409
    assert insight_response.json()["detail"]["operation"]["id"] == mirror_operation_id

    with get_session_factory()() as session:
        mirror_operation = session.get(AppOperation, mirror_operation_id)
        assert mirror_operation is not None
        mirror_operation.status = "completed"
        mirror_operation.completed_at = datetime.now(timezone.utc)
        insight_operation = AppOperation(
            operation_type="saved_item_insight",
            operation_kind="rebuild",
            status="running",
            scope_json={"saved_item_id": saved_item_id},
            progress_json=_initial_progress(),
            runner_id=operations_service.RUNNER_ID,
        )
        session.add_all([mirror_operation, insight_operation])
        session.commit()
        insight_operation_id = insight_operation.id

    mirror_response = client.post(f"/api/operations/saved-items/{saved_item_id}/local-mirror-exports")
    assert mirror_response.status_code == 409
    assert mirror_response.json()["detail"]["operation"]["id"] == insight_operation_id


def test_manual_url_capture_operation_restore_reexports_local_mirror(client, tmp_path, monkeypatch) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()
    try:
        create_response = client.post(
            "/api/operations/captures/manual-url",
            json={
                "url": "https://manual.example.com/operation-archive-restore",
                "title": "Operation Archive Restore",
            },
        )
        assert create_response.status_code == 202
        create_operation_id = create_response.json()["id"]
        _drain_operation(create_operation_id)
        create_operation = client.get(f"/api/operations/{create_operation_id}").json()
        saved_item_id = create_operation["scope"]["saved_item_id"]
        article_dir_name = build_saved_item_archive_dir_name(
            "Operation Archive Restore",
            "https://manual.example.com/operation-archive-restore",
        )
        active_dir = mirror_root / article_dir_name
        archive_dir = mirror_root / ".archive" / article_dir_name
        assert active_dir.exists()

        remove_response = client.post(f"/api/knowledge/saved-items/{saved_item_id}/remove")
        assert remove_response.status_code == 200
        assert not active_dir.exists()
        assert archive_dir.exists()

        restore_response = client.post(
            "/api/operations/captures/manual-url",
            json={
                "url": "https://manual.example.com/operation-archive-restore",
                "title": "Operation Archive Restore",
            },
        )
        assert restore_response.status_code == 202
        restore_operation_id = restore_response.json()["id"]
        _drain_operation(restore_operation_id)
        restore_operation = client.get(f"/api/operations/{restore_operation_id}").json()
        assert restore_operation["status"] == "completed", restore_operation
        assert restore_operation["scope"]["saved_item_id"] == saved_item_id
        assert restore_operation["scope"]["mirror_export_capture_run_id"] is not None
        assert restore_operation["result"]["saved_item"]["local_mirror"]["status"] == "succeeded"
        assert active_dir.exists()
        assert not archive_dir.exists()
    finally:
        reset_settings_cache()


def test_target_operation_creation_interrupts_stale_active_rows(client, monkeypatch) -> None:
    capture_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/stale-target", "title": "Stale Target"},
    )
    assert capture_response.status_code == 202
    capture_operation_id = capture_response.json()["id"]
    _drain_operation(capture_operation_id)
    saved_item_id = client.get(f"/api/operations/{capture_operation_id}").json()["scope"]["saved_item_id"]
    with get_session_factory()() as session:
        stale_operation = AppOperation(
            operation_type="saved_item_insight",
            operation_kind="rebuild",
            status="running",
            scope_json={"saved_item_id": saved_item_id},
            progress_json=_initial_progress(),
            runner_id="old-runner",
        )
        session.add(stale_operation)
        session.commit()
        stale_operation_id = stale_operation.id

    monkeypatch.setattr(operation_jobs, "rebuild_saved_item_insight", lambda session, saved_item_id, ai_route=None: object())

    response = client.post(
        f"/api/operations/saved-items/{saved_item_id}/insight-rebuild",
        json={"ai_route": "general"},
    )
    assert response.status_code == 202, response.text

    with get_session_factory()() as session:
        stale_operation = session.get(AppOperation, stale_operation_id)
        assert stale_operation is not None
        assert stale_operation.status == "failed"
        assert stale_operation.error_message == "Job-backed operation has no jobs."


def test_job_backed_feed_item_extract_operation_survives_stale_runner_interrupt(client) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/stale-job",
            canonical_url="https://alpha.example.com/posts/stale-job",
            title="Stale Job Extract",
            summary_text="Stale job summary.",
            content_text="Stale job content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_extract_operation(session, feed_item.id)
        operation.runner_id = "old-runner"
        session.add(operation)
        session.commit()
        operation_id = operation.id

    response = client.get("/api/operations/active-list")
    assert response.status_code == 200
    active_ids = {entry["id"] for entry in response.json()}
    assert operation_id in active_ids

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "queued"
        assert operation.error_message is None


def test_feed_item_extract_and_artifact_operations_complete(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://alpha.example.com/posts/1": httpx.Response(
                200,
                text="<html><head><title>Alpha Story One</title></head><body><article>Extracted article body.</article></body></html>",
            ),
        },
    )
    sync_source_with_operation(client, source_id, artifact_limit=0)
    item_id = client.get("/api/reader/items").json()[0]["id"]

    extract_response = client.post(f"/api/operations/feed-items/{item_id}/extract-content")
    assert extract_response.status_code == 202
    extract_operation_id = extract_response.json()["id"]
    _drain_operation(extract_operation_id)
    extract_operation = client.get(f"/api/operations/{extract_operation_id}").json()
    assert extract_operation["status"] == "completed", extract_operation
    assert extract_operation["result"]["feed_item_id"] == item_id
    with get_session_factory()() as session:
        job = session.scalar(select(Job).where(Job.operation_id == extract_operation["id"]))
        assert job is not None
        assert job.job_type == "feed_item_extract"
        assert job.payload_json == {"feed_item_id": item_id}
        assert job.status == JOB_STATUS_SUCCEEDED

    configure_ai(client)
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    artifact_response = client.post(
        f"/api/operations/feed-items/{item_id}/insight-artifacts",
        json={"mode": "build_missing"},
    )
    assert artifact_response.status_code == 202
    artifact_operation_id = artifact_response.json()["id"]
    artifact_operation = client.get(f"/api/operations/{artifact_operation_id}").json()
    assert artifact_operation["status"] == "queued", artifact_operation
    assert len(ai_requests_seen) == 0

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="test-ai-lane-feed-item-artifact",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )

    artifact_operation = client.get(f"/api/operations/{artifact_operation_id}").json()
    assert artifact_operation["status"] == "completed", artifact_operation
    assert artifact_operation["result"]["feed_item_id"] == item_id
    assert artifact_operation["result"]["artifact_rows_created"] == 4
    assert len(ai_requests_seen) == 1
    with get_session_factory()() as session:
        job = session.scalar(select(Job).where(Job.operation_id == artifact_operation["id"]))
        assert job is not None
        assert job.job_type == operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT
        assert job.payload_json["feed_item_id"] == item_id
        assert job.payload_json["mode"] == "build_missing"
        _assert_feed_item_insight_enqueue_context(job.payload_json, feed_item_id=item_id)
        assert job.status == JOB_STATUS_SUCCEEDED


def test_saved_item_insight_and_mirror_operations_complete(client, monkeypatch) -> None:
    capture_response = client.post(
        "/api/operations/captures/manual-url",
        json={"url": "https://manual.example.com/saved-insight", "title": "Saved Insight"},
    )
    assert capture_response.status_code == 202
    capture_operation_id = capture_response.json()["id"]
    _drain_operation(capture_operation_id)
    saved_item_id = client.get(f"/api/operations/{capture_operation_id}").json()["scope"]["saved_item_id"]

    mirror_response = client.post(f"/api/operations/saved-items/{saved_item_id}/local-mirror-exports")
    assert mirror_response.status_code == 202
    mirror_operation_id = mirror_response.json()["id"]
    _drain_operation(mirror_operation_id)
    mirror_operation = client.get(f"/api/operations/{mirror_operation_id}").json()
    assert mirror_operation["status"] == "completed", mirror_operation
    assert mirror_operation["result"]["saved_item"]["local_mirror"]["status"] == "succeeded"
    with get_session_factory()() as session:
        job = session.scalar(select(Job).where(Job.operation_id == mirror_operation["id"]))
        assert job is not None
        assert job.job_type == operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT
        assert job.payload_json == {"saved_item_id": saved_item_id}
        assert job.status == JOB_STATUS_SUCCEEDED

    monkeypatch.setattr(operation_jobs, "rebuild_saved_item_insight", lambda session, saved_item_id, ai_route=None: object())
    insight_response = client.post(
        f"/api/operations/saved-items/{saved_item_id}/insight-rebuild",
        json={"ai_route": "general"},
    )
    assert insight_response.status_code == 202
    insight_operation_id = insight_response.json()["id"]
    insight_operation = client.get(f"/api/operations/{insight_operation_id}").json()
    assert insight_operation["status"] == "queued", insight_operation

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="test-ai-lane-saved-item-insight",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )

    insight_operation = client.get(f"/api/operations/{insight_operation_id}").json()
    assert insight_operation["status"] == "completed", insight_operation
    assert insight_operation["operation_type"] == "saved_item_insight"
    assert insight_operation["result"]["saved_item_id"] == saved_item_id
    with get_session_factory()() as session:
        job = session.scalar(select(Job).where(Job.operation_id == insight_operation["id"]))
        assert job is not None
        assert job.job_type == operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT
        assert job.payload_json["saved_item_id"] == saved_item_id
        assert job.payload_json["ai_route"] == "general"
        assert job.payload_json["enqueue_context"]["ai_profile"] == "general"
        assert job.status == JOB_STATUS_SUCCEEDED


def test_local_mirror_export_operation_duplicate_active_conflict_keeps_single_job(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_local_mirror_export_operation(session, saved_item_id)
        session.commit()
        operation_id = operation.id

    response = client.post(f"/api/operations/saved-items/{saved_item_id}/local-mirror-exports")

    assert response.status_code == 409
    assert response.json()["detail"]["operation"]["id"] == operation_id
    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id)))
    assert len(jobs) == 1
    assert jobs[0].job_type == operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT
    assert jobs[0].status == JOB_STATUS_PENDING


def test_feed_item_insight_artifacts_operation_duplicate_active_conflict_keeps_single_job(client) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/duplicate-artifact-job",
            canonical_url="https://alpha.example.com/posts/duplicate-artifact-job",
            title="Duplicate Artifact Job",
            summary_text="Duplicate artifact summary.",
            content_text="Duplicate artifact content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item.id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )
        session.commit()
        feed_item_id = feed_item.id
        operation_id = operation.id

    response = client.post(f"/api/operations/feed-items/{feed_item_id}/insight-artifacts", json={"mode": "build_missing"})

    assert response.status_code == 409
    assert response.json()["detail"]["operation"]["id"] == operation_id
    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id)))
    assert len(jobs) == 1
    assert jobs[0].job_type == operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT
    assert jobs[0].status == JOB_STATUS_PENDING


def test_feed_item_extract_operation_locks_before_loading_feed_item(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/extract-lock-order",
            canonical_url="https://alpha.example.com/posts/extract-lock-order",
            title="Extract Lock Order",
            summary_text="Extract lock order summary.",
            content_text="Extract lock order content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        feed_item_id = feed_item.id

        calls: list[str] = []
        original_load = operations_service._load_feed_item_for_operation

        def record_lock(_session, key):
            calls.append(f"lock:{key}")

        def record_load(_session, item_id):
            calls.append(f"load:{item_id}")
            return original_load(_session, item_id)

        monkeypatch.setattr(operations_service, "_acquire_operation_mutex_lock", record_lock)
        monkeypatch.setattr(operations_service, "_load_feed_item_for_operation", record_load)

        operations_service.create_feed_item_extract_operation(session, feed_item_id)

    assert calls[:2] == [
        f"lock:{operations_service.OPERATION_TYPE_FEED_ITEM_EXTRACT}:feed_item:{feed_item_id}",
        f"load:{feed_item_id}",
    ]


def test_feed_item_insight_artifact_operation_locks_before_loading_feed_item(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/artifact-lock-order",
            canonical_url="https://alpha.example.com/posts/artifact-lock-order",
            title="Artifact Lock Order",
            summary_text="Artifact lock order summary.",
            content_text="Artifact lock order content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        feed_item_id = feed_item.id

        calls: list[str] = []
        original_load = operations_service._load_feed_item_for_operation

        def record_lock(_session, key):
            calls.append(f"lock:{key}")

        def record_load(_session, item_id):
            calls.append(f"load:{item_id}")
            return original_load(_session, item_id)

        monkeypatch.setattr(operations_service, "_acquire_operation_mutex_lock", record_lock)
        monkeypatch.setattr(operations_service, "_load_feed_item_for_operation", record_load)

        operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item_id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )

    assert calls[:2] == [
        f"lock:{operations_service.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT}:feed_item:{feed_item_id}",
        f"load:{feed_item_id}",
    ]


def test_saved_item_insight_operation_duplicate_active_conflict_keeps_single_job(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_saved_item_insight_operation(
            session,
            saved_item_id,
            SavedItemInsightOperationCreate(ai_route="general"),
        )
        session.commit()
        operation_id = operation.id

    response = client.post(
        f"/api/operations/saved-items/{saved_item_id}/insight-rebuild",
        json={"ai_route": "general"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["operation"]["id"] == operation_id
    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id)))
    assert len(jobs) == 1
    assert jobs[0].job_type == operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT
    assert jobs[0].status == JOB_STATUS_PENDING


def test_local_mirror_export_operation_pending_stop_cancels_job(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_local_mirror_export_operation(session, saved_item_id)
        session.commit()
        operation_id = operation.id

    with get_session_factory()() as session:
        operation = operations_service.request_stop_operation(session, operation_id)
        session.commit()
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert job is not None
    assert job.status == JOB_STATUS_CANCELLED


def test_knowledge_capture_operation_pending_stop_cancels_jobs_and_fails_pending_capture_run(client) -> None:
    with get_session_factory()() as session:
        operation = operations_service.create_manual_url_capture_operation(
            session,
            ManualUrlCaptureOperationCreate(
                url="https://manual.example.com/pending-stop-capture",
                title="Pending Stop Capture",
            ),
        )
        capture_run_id = operation.scope_json["capture_run_id"]
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        capture_run = session.get(SavedItemCaptureRun, capture_run_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert capture_run is not None
    assert capture_run.status == "failed"
    assert "stopped" in (capture_run.error_summary or "")
    assert [job.status for job in jobs] == [JOB_STATUS_CANCELLED]


def test_uploaded_material_capture_operation_pending_stop_marks_processing_failed(client) -> None:
    with get_session_factory()() as session:
        operation = operations_service.create_uploaded_material_capture_operation(
            session,
            file_bytes=b"# Stop Upload\n\nBody",
            filename="stop-upload.md",
            content_type="text/markdown",
            material_type="markdown",
            title=None,
            user_note=None,
            ai_route=None,
        )
        saved_item_id = operation.scope_json["saved_item_id"]
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        saved_item = session.get(SavedItem, saved_item_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert operation is not None
    assert operation.status == "stopped"
    assert saved_item is not None
    assert saved_item.processing_status == PROCESSING_STATUS_FAILED
    assert "stopped" in (saved_item.processing_error or "")
    assert [job.status for job in jobs] == [JOB_STATUS_CANCELLED]


def test_manual_import_operation_pending_stop_marks_processing_failed(client) -> None:
    with get_session_factory()() as session:
        saved_item = SavedItem(
            origin_type="manual_url",
            ai_route="sensitive",
            title="Manual Import Pending Stop",
            canonical_url="https://manual-import.example.com/pending-stop",
            source_name="Manual Import",
            source_app="https://manual-import.example.com",
        )
        session.add(saved_item)
        session.flush()
        operation = operations_service.create_saved_item_manual_import_operation(
            session,
            saved_item.id,
            file_bytes=b"# Manual Import Pending Stop\n\nBody",
            filename="manual-import.md",
            content_type="text/markdown",
            material_type="markdown",
            title=None,
            user_note=None,
        )
        saved_item_id = operation.scope_json["saved_item_id"]
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        saved_item = session.get(SavedItem, saved_item_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert operation is not None
    assert operation.status == "stopped"
    assert saved_item is not None
    assert saved_item.processing_status == PROCESSING_STATUS_FAILED
    assert "stopped" in (saved_item.processing_error or "")
    assert [job.status for job in jobs] == [JOB_STATUS_CANCELLED]


def test_feed_item_insight_artifacts_operation_pending_stop_cancels_job(client) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/stop-artifact-job",
            canonical_url="https://alpha.example.com/posts/stop-artifact-job",
            title="Stop Artifact Job",
            summary_text="Stop artifact summary.",
            content_text="Stop artifact content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item.id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert job is not None
    assert job.status == JOB_STATUS_CANCELLED


def test_saved_item_insight_operation_pending_stop_cancels_job(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_saved_item_insight_operation(
            session,
            saved_item_id,
            SavedItemInsightOperationCreate(ai_route="general"),
        )
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert job is not None
    assert job.status == JOB_STATUS_CANCELLED


def test_local_mirror_export_operation_worker_runtime_failure_fails_without_retry_redacted(client, monkeypatch) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_local_mirror_export_operation(session, saved_item_id)
        session.commit()
        operation_id = operation.id

    def fail_mirror(session, item_id: int):
        assert item_id == saved_item_id
        raise RuntimeError("mirror failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "export_saved_item_local_mirror", fail_mirror)

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="mirror-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=5,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert operation.message == "Local mirror export operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == 1


def test_local_mirror_export_operation_worker_access_failure_retries_then_fails_redacted(client, monkeypatch) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_local_mirror_export_operation(session, saved_item_id)
        session.commit()
        operation_id = operation.id

    def fail_mirror(session, item_id: int):
        assert item_id == saved_item_id
        raise httpx.ConnectError("mirror connect failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "export_saved_item_local_mirror", fail_mirror)

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="mirror-access-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=5,
        failure_backoff_seconds=0,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert operation.message == "Local mirror export operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == job.max_attempts


def test_knowledge_capture_operation_worker_runtime_failure_fails_without_retry_and_compensates(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        operation = operations_service.create_manual_url_capture_operation(
            session,
            ManualUrlCaptureOperationCreate(
                url="https://manual.example.com/failing-capture-job",
                title="Failing Capture Job",
            ),
        )
        capture_run_id = operation.scope_json["capture_run_id"]
        session.commit()
        operation_id = operation.id

    def fail_capture(session, *, saved_item_id: int, capture_run_id: int, progress_callback=None, export_local_mirror=True):
        raise RuntimeError("capture failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "execute_saved_item_capture_run", fail_capture)

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="knowledge-capture-failure-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=5,
        failure_backoff_seconds=0,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        capture_run = session.get(SavedItemCaptureRun, capture_run_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.message == "Capture operation failed."
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert capture_run is not None
    assert capture_run.status == "failed"
    assert "REDACTED" in (capture_run.error_summary or "")
    assert [job.job_type for job in jobs] == [operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE]
    assert jobs[0].status == JOB_STATUS_FAILED
    assert jobs[0].attempts == 1


def test_knowledge_capture_job_finalizes_existing_tool_calls_without_rerun(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        operation = operations_service.create_manual_url_capture_operation(
            session,
            ManualUrlCaptureOperationCreate(
                url="https://manual.example.com/precommitted-tool-call",
                title="Precommitted Tool Call",
            ),
        )
        saved_item_id = operation.scope_json["saved_item_id"]
        capture_run_id = operation.scope_json["capture_run_id"]
        now = datetime.now(timezone.utc)
        session.add(
            SavedItemCaptureToolCall(
                capture_run_id=capture_run_id,
                tool_name="singlefile_extension",
                tool_version="2.0.83",
                status=CAPTURE_TOOL_STATUS_FAILED,
                exit_code=143,
                error_summary="precommitted singlefile failure",
                started_at=now,
                finished_at=now,
            )
        )
        session.commit()
        operation_id = operation.id

    def fail_if_rerun(*args, **kwargs):
        raise AssertionError("capture tool should not rerun when pending run already has tool calls")

    monkeypatch.setattr("app.services.knowledge_capture.archive_page_with_singlefile", fail_if_rerun)
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", fail_if_rerun)

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="knowledge-capture-precommitted-tool-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=5,
        failure_backoff_seconds=0,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        capture_run = session.get(SavedItemCaptureRun, capture_run_id)
        tool_calls = list(
            session.scalars(
                select(SavedItemCaptureToolCall)
                .where(SavedItemCaptureToolCall.capture_run_id == capture_run_id)
                .order_by(SavedItemCaptureToolCall.id.asc())
            )
        )
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))
        mirror_exports = list(
            session.scalars(select(SavedItemMirrorExport).where(SavedItemMirrorExport.saved_item_id == saved_item_id))
        )

    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["saved_item"]["capture"]["status"] == "failed"
    assert capture_run is not None
    assert capture_run.status == "failed"
    assert "precommitted singlefile failure" in (capture_run.error_summary or "")
    assert [tool_call.tool_name for tool_call in tool_calls] == ["singlefile_extension"]
    assert [job.job_type for job in jobs] == [
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE,
        operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
    ]
    assert {job.status for job in jobs} == {JOB_STATUS_SUCCEEDED}
    assert mirror_exports == []


def test_knowledge_capture_running_stop_preserves_successful_capture_side_effect(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        operation = operations_service.create_manual_url_capture_operation(
            session,
            ManualUrlCaptureOperationCreate(
                url="https://manual.example.com/running-stop-capture",
                title="Running Stop Capture",
            ),
        )
        capture_run_id = operation.scope_json["capture_run_id"]
        session.commit()
        operation_id = operation.id

    real_execute = operation_jobs.execute_saved_item_capture_run

    def stop_after_success(session, *, saved_item_id: int, capture_run_id: int, progress_callback=None, export_local_mirror=True):
        result = real_execute(
            session,
            saved_item_id=saved_item_id,
            capture_run_id=capture_run_id,
            progress_callback=progress_callback,
            export_local_mirror=export_local_mirror,
        )
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(
            select(Job)
            .where(Job.operation_id == operation_id)
            .where(Job.job_type == operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE)
        )
        assert operation is not None
        assert job is not None
        operation.stop_requested = True
        job.cancel_requested = True
        session.add_all([operation, job])
        session.flush()
        return result

    monkeypatch.setattr(operation_jobs, "execute_saved_item_capture_run", stop_after_success)

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="knowledge-capture-running-stop-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=5,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        capture_run = session.get(SavedItemCaptureRun, capture_run_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert operation is not None
    assert operation.status == "stopped"
    assert capture_run is not None
    assert capture_run.status == "succeeded"
    assert [job.status for job in jobs] == [JOB_STATUS_SUCCEEDED, JOB_STATUS_CANCELLED]


def test_feed_item_insight_artifacts_operation_worker_runtime_failure_fails_without_retry_redacted(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/failing-artifact-job",
            canonical_url="https://alpha.example.com/posts/failing-artifact-job",
            title="Failing Artifact Job",
            summary_text="Failing artifact summary.",
            content_text="Failing artifact content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item.id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )
        session.commit()
        operation_id = operation.id

    def fail_artifacts(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        raise RuntimeError("provider failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fail_artifacts)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="artifact-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert operation.message == "Feed item insight artifact operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == 1


def test_feed_item_insight_artifacts_operation_failed_stats_retry_then_fail(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/failed-artifact-stats-job",
            canonical_url="https://alpha.example.com/posts/failed-artifact-stats-job",
            title="Failed Artifact Stats Job",
            summary_text="Failed artifact stats summary.",
            content_text="Failed artifact stats content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item.id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )
        session.commit()
        operation_id = operation.id

    def failed_stats(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        session.add(
            InsightEngineAttempt(
                feed_item_id=feed_item_id,
                source_id=None,
                ai_route="general",
                profile="general",
                provider="openai-compatible",
                model="test-model",
                status="failed",
                stage="provider_request",
                error_type="AIProviderHTTPError",
                error_message="AI provider HTTP 503: temporarily unavailable",
                artifact_count=0,
            )
        )
        session.flush()
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=f"feed_item:{feed_item_id}:missing",
            processed_feed_item_count=1,
            artifact_rows_created=0,
            failed_feed_item_count=1,
        )

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", failed_stats)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="artifact-failed-stats-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=0,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        attempt_count = session.scalar(
            select(func.count()).select_from(InsightEngineAttempt).where(InsightEngineAttempt.feed_item_id == job.payload_json["feed_item_id"])
        )

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message == "Feed item insight artifact operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == job.max_attempts
    assert attempt_count == job.max_attempts


def test_feed_item_insight_artifacts_operation_non_retryable_failed_stats_complete_with_attempt(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/skipped-artifact-stats-job",
            canonical_url="https://alpha.example.com/posts/skipped-artifact-stats-job",
            title="Skipped Artifact Stats Job",
            summary_text="Skipped artifact stats summary.",
            content_text="Skipped artifact stats content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item.id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )
        session.commit()
        operation_id = operation.id
        feed_item_id = feed_item.id

    def skipped_stats(session, *, feed_item_id: int, mode: str, scope: str):
        assert mode == "build_missing"
        session.add(
            InsightEngineAttempt(
                feed_item_id=feed_item_id,
                source_id=None,
                ai_route="general",
                profile="general",
                provider="openai-compatible",
                model="test-model",
                status="skipped",
                stage="content_filter",
                error_type="low_confidence_content",
                error_message="Content extraction failed with URL-only content.",
                artifact_count=0,
            )
        )
        session.flush()
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=f"feed_item:{feed_item_id}:missing",
            processed_feed_item_count=1,
            artifact_rows_created=0,
            failed_feed_item_count=1,
        )

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", skipped_stats)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="artifact-skipped-stats-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        attempt = session.scalar(select(InsightEngineAttempt).where(InsightEngineAttempt.feed_item_id == feed_item_id))

    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["failed_feed_item_count"] == 1
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert job.attempts == 1
    assert attempt is not None
    assert attempt.status == "skipped"
    assert attempt.stage == "content_filter"


def test_saved_item_insight_operation_worker_runtime_failure_fails_without_retry_redacted(client, monkeypatch) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_saved_item_insight_operation(
            session,
            saved_item_id,
            SavedItemInsightOperationCreate(ai_route="general"),
        )
        session.commit()
        operation_id = operation.id

    def fail_insight(session, item_id: int, ai_route=None):
        assert item_id == saved_item_id
        raise RuntimeError("provider failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "rebuild_saved_item_insight", fail_insight)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="saved-insight-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert operation.message == "Saved item insight operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == 1


def test_saved_item_insight_operation_config_failure_fails_without_retry(client, monkeypatch) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_saved_item_insight_operation(
            session,
            saved_item_id,
            SavedItemInsightOperationCreate(ai_route="general"),
        )
        session.commit()
        operation_id = operation.id

    def fail_config(session, item_id: int, ai_route=None):
        assert item_id == saved_item_id
        raise SavedItemInsightConfigurationError("AI provider is not configured")

    monkeypatch.setattr(operation_jobs, "rebuild_saved_item_insight", fail_config)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="saved-insight-config-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message == "AI provider is not configured"
    assert operation.message == "Saved item insight operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == 1


def test_job_backed_local_mirror_operation_survives_stale_runner_interrupt(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_local_mirror_export_operation(session, saved_item_id)
        operation.runner_id = "old-runner"
        session.add(operation)
        session.commit()
        operation_id = operation.id

    with get_session_factory()() as session:
        active_operations = operations_service.list_active_operations(session)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert any(operation.id == operation_id for operation in active_operations)
    assert operation is not None
    assert operation.status == "queued"
    assert operation.error_message is None
    assert job is not None
    assert job.status == JOB_STATUS_PENDING


def test_job_backed_knowledge_capture_operation_survives_stale_runner_interrupt(client) -> None:
    with get_session_factory()() as session:
        operation = operations_service.create_manual_url_capture_operation(
            session,
            ManualUrlCaptureOperationCreate(
                url="https://manual.example.com/stale-job-backed-capture",
                title="Stale Job Backed Capture",
            ),
        )
        operation.runner_id = "old-runner"
        session.add(operation)
        session.commit()
        operation_id = operation.id

    with get_session_factory()() as session:
        active_operations = operations_service.list_active_operations(session)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert any(operation.id == operation_id for operation in active_operations)
    assert operation is not None
    assert operation.status == "queued"
    assert operation.error_message is None
    assert [job.status for job in jobs] == [JOB_STATUS_PENDING]


def test_job_backed_feed_item_insight_artifacts_operation_survives_stale_runner_interrupt(client) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/stale-artifact-job",
            canonical_url="https://alpha.example.com/posts/stale-artifact-job",
            title="Stale Artifact Job",
            summary_text="Stale artifact summary.",
            content_text="Stale artifact content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_operation(
            session,
            feed_item.id,
            FeedItemInsightArtifactOperationCreate(mode="build_missing"),
        )
        operation.runner_id = "old-runner"
        session.add(operation)
        session.commit()
        operation_id = operation.id

    with get_session_factory()() as session:
        active_operations = operations_service.list_active_operations(session)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert any(operation.id == operation_id for operation in active_operations)
    assert operation is not None
    assert operation.status == "queued"
    assert operation.error_message is None
    assert job is not None
    assert job.status == JOB_STATUS_PENDING


def test_job_backed_saved_item_insight_operation_survives_stale_runner_interrupt(client) -> None:
    saved_item_id = _create_minimal_saved_item()
    with get_session_factory()() as session:
        operation = operations_service.create_saved_item_insight_operation(
            session,
            saved_item_id,
            SavedItemInsightOperationCreate(ai_route="general"),
        )
        operation.runner_id = "old-runner"
        session.add(operation)
        session.commit()
        operation_id = operation.id

    with get_session_factory()() as session:
        active_operations = operations_service.list_active_operations(session)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert any(operation.id == operation_id for operation in active_operations)
    assert operation is not None
    assert operation.status == "queued"
    assert operation.error_message is None
    assert job is not None
    assert job.status == JOB_STATUS_PENDING


def test_homepage_artifact_operation_runner_builds_missing_artifacts(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(monkeypatch, {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA)})
    sync_source_with_operation(client, source_id, artifact_limit=0)
    configure_ai(client)
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))

    create_response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "current_source", "source_id": source_id, "limit": 1},
    )
    assert create_response.status_code == 202
    operation_id = create_response.json()["id"]

    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["operation_type"] == "feed_item_insight_artifacts"
    assert operation["status"] == "queued", operation
    assert len(ai_requests_seen) == 0

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="test-ai-lane-homepage-artifacts",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )

    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["status"] == "completed", operation
    assert operation["progress"]["current_insight_total"] == 1
    assert operation["progress"]["insight_completed_count"] == 1
    assert operation["result"]["processed_feed_item_count"] == 1
    assert operation["result"]["artifact_rows_created"] == 4
    assert len(ai_requests_seen) == 1

    with get_session_factory()() as session:
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.job_type == operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD
        assert job.payload_json["mode"] == "build_missing"
        assert job.payload_json["source_id"] == source_id
        assert job.payload_json["feed_item_id"] is not None
        _assert_feed_item_insight_enqueue_context(job.payload_json, feed_item_id=job.payload_json["feed_item_id"])
        assert job.status == JOB_STATUS_SUCCEEDED
        assert session.scalar(select(func.count()).select_from(InsightEngineArtifact)) == 4


def test_homepage_artifact_operation_zero_limit_completes_without_provider_request(client, monkeypatch) -> None:
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))

    create_response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "global", "limit": 0},
    )

    assert create_response.status_code == 202
    operation_id = create_response.json()["id"]
    operation_response = client.get(f"/api/operations/{operation_id}")
    assert operation_response.status_code == 200
    operation = operation_response.json()
    assert operation["status"] == "completed", operation
    assert operation["progress"]["stage"] == "completed"
    assert operation["result"] == {
        "scope": "all",
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
        "stopped": False,
    }
    assert ai_requests_seen == []

    with get_session_factory()() as session:
        job_count = session.scalar(select(func.count()).select_from(Job).where(Job.operation_id == operation_id))
        assert job_count == 0


def test_feed_item_insight_enqueue_context_validation_checks_operation_and_item_identity(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        payload = {
            "enqueue_context": operation_jobs._build_feed_item_insight_enqueue_context(
                session,
                operation_id=100,
                feed_item_id=200,
            )
        }
        try:
            operation_jobs._validate_feed_item_insight_enqueue_context(
                session,
                payload,
                operation_id=101,
                feed_item_id=201,
            )
        except operation_jobs.OperationJobPermanentError as exc:
            message = str(exc)
        else:
            raise AssertionError("Tampered enqueue context should fail validation")

    assert "operation_id" in message
    assert "feed_item_id" in message


def test_removed_homepage_artifacts_route_is_not_supported(client) -> None:
    response = client.post(
        "/api/operations/homepage-artifacts",
        json={"scope": "global", "limit": 0},
    )

    assert response.status_code == 405


def test_homepage_artifact_operation_duplicate_active_conflict_keeps_single_job(client) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        _create_missing_feed_item_for_source(session, source_id=source_id)
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="current_source", source_id=source_id, limit=1),
        )
        session.commit()
        operation_id = operation.id

    response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "current_source", "source_id": source_id, "limit": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["operation"]["id"] == operation_id
    with get_session_factory()() as session:
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id)))
    assert len(jobs) == 1
    assert jobs[0].job_type == operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD
    assert jobs[0].status == JOB_STATUS_PENDING


def test_failed_attempts_artifact_batch_operation_creates_one_operation_with_deduped_jobs(client) -> None:
    alpha_id = create_source_for_sync(client, "Failed Attempts Alpha", "https://failed-alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Failed Attempts Beta", "https://failed-beta.example.com/feed.xml")
    cutoff = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    recent = cutoff + timedelta(minutes=5)
    older = cutoff - timedelta(days=1)
    with get_session_factory()() as session:
        alpha_item = _create_missing_feed_item_for_source(session, source_id=alpha_id)
        alpha_duplicate_item = _create_missing_feed_item_for_source(session, source_id=alpha_id)
        alpha_sensitive_item = _create_missing_feed_item_for_source(session, source_id=alpha_id)
        alpha_old_item = _create_missing_feed_item_for_source(session, source_id=alpha_id)
        alpha_complete_item = _create_missing_feed_item_for_source(session, source_id=alpha_id)
        beta_item = _create_missing_feed_item_for_source(session, source_id=beta_id)
        _add_complete_feed_item_insight_artifacts(session, feed_item_id=alpha_complete_item.id)
        session.add_all(
            [
                InsightEngineAttempt(
                    feed_item_id=alpha_item.id,
                    source_id=alpha_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: temporarily unavailable",
                    artifact_count=0,
                    created_at=recent + timedelta(minutes=2),
                ),
                InsightEngineAttempt(
                    feed_item_id=alpha_item.id,
                    source_id=alpha_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: duplicate earlier attempt",
                    artifact_count=0,
                    created_at=recent,
                ),
                InsightEngineAttempt(
                    feed_item_id=alpha_duplicate_item.id,
                    source_id=alpha_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: temporarily unavailable",
                    artifact_count=0,
                    created_at=recent + timedelta(minutes=1),
                ),
                InsightEngineAttempt(
                    feed_item_id=alpha_sensitive_item.id,
                    source_id=alpha_id,
                    ai_route="sensitive",
                    profile="sensitive",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: temporarily unavailable",
                    artifact_count=0,
                    created_at=recent,
                ),
                InsightEngineAttempt(
                    feed_item_id=alpha_old_item.id,
                    source_id=alpha_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: old attempt",
                    artifact_count=0,
                    created_at=older,
                ),
                InsightEngineAttempt(
                    feed_item_id=alpha_complete_item.id,
                    source_id=alpha_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: already complete",
                    artifact_count=0,
                    created_at=recent + timedelta(minutes=3),
                ),
                InsightEngineAttempt(
                    feed_item_id=beta_item.id,
                    source_id=beta_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: beta attempt",
                    artifact_count=0,
                    created_at=recent,
                ),
                InsightEngineAttempt(
                    feed_item_id=None,
                    source_id=alpha_id,
                    ai_route="general",
                    profile="general",
                    provider="openai-compatible",
                    model="test-model",
                    status="failed",
                    stage="provider_request",
                    error_type="AIProviderHTTPError",
                    error_message="AI provider HTTP 503: no item",
                    artifact_count=0,
                    created_at=recent,
                ),
            ]
        )
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(
                scope="failed_attempts",
                source_id=alpha_id,
                profile="general",
                created_after=cutoff,
                limit=25,
            ),
        )
        session.commit()
        operation_id = operation.id
        expected_item_ids = {alpha_item.id, alpha_duplicate_item.id}

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id)))

    assert operation is not None
    assert operation.operation_type == "feed_item_insight_artifacts"
    assert operation.operation_kind == "failed_attempts"
    assert operation.source_id == alpha_id
    assert operation.status == "queued"
    assert operation.scope_json["scope"] == "failed_attempts"
    assert operation.scope_json["profile"] == "general"
    assert operation.scope_json["created_after"] == cutoff.isoformat()
    assert operation.progress_json["insight_total_count"] == 2
    assert operation.result_json["scope"] == f"failed_attempts:source:{alpha_id}"
    assert len(jobs) == 2
    assert {job.payload_json["feed_item_id"] for job in jobs} == expected_item_ids
    assert {job.payload_json["source_id"] for job in jobs} == {alpha_id}
    assert {job.operation_id for job in jobs} == {operation_id}
    assert {job.job_type for job in jobs} == {operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD}
    assert {job.queue_name for job in jobs} == {operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME}
    assert {job.status for job in jobs} == {JOB_STATUS_PENDING}


def test_failed_attempts_artifact_batch_operation_endpoint_accepts_scope(client) -> None:
    response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "failed_attempts", "profile": "general", "limit": 0},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["operation_type"] == "feed_item_insight_artifacts"
    assert payload["operation_kind"] == "failed_attempts"
    assert payload["status"] == "completed"
    assert payload["scope"]["scope"] == "failed_attempts"
    assert payload["scope"]["profile"] == "general"
    assert payload["progress"]["insight_total_count"] == 0
    assert payload["message"] == (
        "No current failed Insight Engine items are missing artifacts. "
        "Historical failed attempts may still exist."
    )


def test_failed_attempts_artifact_batch_operation_conflicts_with_active_global_operation(client) -> None:
    source_id = create_source_for_sync(client, "Failed Attempts Conflict", "https://failed-conflict.example.com/feed.xml")
    with get_session_factory()() as session:
        _create_missing_feed_item_for_source(session, source_id=source_id)
        active_operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="current_source", source_id=source_id, limit=1),
        )
        session.commit()
        active_operation_id = active_operation.id

    response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "failed_attempts", "source_id": source_id, "limit": 25},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["operation"]["id"] == active_operation_id
    with get_session_factory()() as session:
        operations = list(session.scalars(select(AppOperation).where(AppOperation.operation_type == "feed_item_insight_artifacts")))
        jobs = list(session.scalars(select(Job)))
    assert len(operations) == 1
    assert len(jobs) == 1
    assert jobs[0].operation_id == active_operation_id


def test_retryable_coverage_batch_operation_only_enqueues_retryable_items(client) -> None:
    configure_ai(client)
    retry_source_id = create_source_for_sync(
        client,
        "Retryable Coverage",
        "https://retryable-coverage.example.com/feed.xml",
    )
    disabled_source_id = create_source_for_sync(
        client,
        "Disabled Coverage",
        "https://disabled-coverage.example.com/feed.xml",
        ai_route="disabled",
    )
    with get_session_factory()() as session:
        failed_item = _create_missing_feed_item_for_source(session, source_id=retry_source_id)
        no_history_item = _create_missing_feed_item_for_source(session, source_id=retry_source_id)
        low_confidence_item = _create_missing_feed_item_for_source(session, source_id=retry_source_id)
        disabled_item = _create_missing_feed_item_for_source(session, source_id=disabled_source_id)
        complete_item = _create_missing_feed_item_for_source(session, source_id=retry_source_id)
        _add_complete_feed_item_insight_artifacts(session, feed_item_id=complete_item.id)

        _add_complete_feed_item_insight_artifacts(session, feed_item_id=failed_item.id)
        topics_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == failed_item.id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_topics_v1")
        )
        assert topics_artifact is not None
        session.delete(topics_artifact)

        low_confidence_item.content_text = "https://retryable-coverage.example.com/jump-only"
        low_confidence_item.summary_text = "https://retryable-coverage.example.com/jump-only"
        low_confidence_item.content_extraction_status = "failed"
        low_confidence_item.content_extraction_error = "Content extraction failed"
        session.add(
            InsightEngineAttempt(
                feed_item_id=failed_item.id,
                source_id=retry_source_id,
                ai_route="general",
                profile="general",
                provider="openai-compatible",
                model="test-model",
                status="failed",
                stage="provider_request",
                error_type="provider_error",
                error_message="provider timed out",
                artifact_count=3,
            )
        )
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="retryable_coverage", limit=25),
        )
        session.commit()
        operation_id = operation.id
        expected_item_ids = {failed_item.id, no_history_item.id}
        non_retryable_item_ids = {low_confidence_item.id, disabled_item.id, complete_item.id}

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id)))

    assert operation is not None
    assert operation.operation_type == "feed_item_insight_artifacts"
    assert operation.operation_kind == "retryable_coverage"
    assert operation.source_id is None
    assert operation.status == "queued"
    assert operation.scope_json["scope"] == "retryable_coverage"
    assert operation.progress_json["insight_total_count"] == 2
    assert operation.result_json["scope"] == "retryable_coverage"
    assert len(jobs) == 2
    assert {job.payload_json["feed_item_id"] for job in jobs} == expected_item_ids
    assert {job.payload_json["feed_item_id"] for job in jobs}.isdisjoint(non_retryable_item_ids)
    assert {job.payload_json["source_id"] for job in jobs} == {retry_source_id}
    assert {job.job_type for job in jobs} == {operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD}
    assert {job.status for job in jobs} == {JOB_STATUS_PENDING}


def test_retryable_coverage_batch_operation_can_filter_reason_codes(client) -> None:
    configure_ai(client)
    source_id = create_source_for_sync(
        client,
        "Retryable Coverage Filter",
        "https://retryable-coverage-filter.example.com/feed.xml",
    )
    with get_session_factory()() as session:
        failed_item = _create_missing_feed_item_for_source(session, source_id=source_id)
        no_history_item = _create_missing_feed_item_for_source(session, source_id=source_id)
        _add_complete_feed_item_insight_artifacts(session, feed_item_id=failed_item.id)
        topics_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == failed_item.id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_topics_v1")
        )
        assert topics_artifact is not None
        session.delete(topics_artifact)
        session.add(
            InsightEngineAttempt(
                feed_item_id=failed_item.id,
                source_id=source_id,
                ai_route="general",
                profile="general",
                provider="openai-compatible",
                model="test-model",
                status="failed",
                stage="provider_request",
                error_type="provider_error",
                error_message="provider timed out",
                artifact_count=3,
            )
        )
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(
                scope="retryable_coverage",
                reason_codes=["previous_failed_attempt", "partial_artifacts"],
                limit=25,
            ),
        )
        session.commit()
        operation_id = operation.id

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id)))

    assert operation is not None
    assert operation.operation_kind == "retryable_coverage"
    assert operation.scope_json["reason_codes"] == ["previous_failed_attempt", "partial_artifacts"]
    assert operation.progress_json["insight_total_count"] == 1
    assert len(jobs) == 1
    assert jobs[0].payload_json["feed_item_id"] == failed_item.id
    assert jobs[0].payload_json["feed_item_id"] != no_history_item.id


def test_retryable_coverage_batch_operation_endpoint_accepts_scope(client) -> None:
    response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "retryable_coverage", "limit": 0},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["operation_type"] == "feed_item_insight_artifacts"
    assert payload["operation_kind"] == "retryable_coverage"
    assert payload["status"] == "completed"
    assert payload["scope"]["scope"] == "retryable_coverage"
    assert payload["progress"]["insight_total_count"] == 0


def test_batch_operation_rejects_failed_attempt_only_filters_on_other_scopes(client) -> None:
    global_response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "global", "profile": "general", "limit": 0},
    )
    coverage_response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "retryable_coverage", "created_after": "2026-06-01T00:00:00Z", "limit": 0},
    )
    failed_attempt_reason_response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "failed_attempts", "reason_codes": ["previous_failed_attempt"], "limit": 0},
    )
    unknown_reason_response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "retryable_coverage", "reason_codes": ["unknown_reason"], "limit": 0},
    )

    assert global_response.status_code == 422
    assert coverage_response.status_code == 422
    assert failed_attempt_reason_response.status_code == 422
    assert unknown_reason_response.status_code == 422


def test_homepage_artifact_operation_pending_stop_cancels_job(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Stop Homepage Artifact Source",
            kind="rss",
            url="https://stop-homepage-artifact.example.com/feed.xml",
            site_url="https://stop-homepage-artifact.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        _create_missing_feed_item_for_source(session, source_id=source.id)
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="global", limit=1),
        )
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert job is not None
    assert job.status == JOB_STATUS_CANCELLED


def test_ai_artifact_claim_respects_profile_provider_concurrency(client, monkeypatch) -> None:
    class FakeSettings:
        database_url = "postgresql://test/test"

    monkeypatch.setattr(operation_jobs, "get_settings", lambda: FakeSettings())
    response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {"artifact_concurrency": 2},
                "sensitive": {"artifact_concurrency": 1},
            }
        },
    )
    assert response.status_code == 200

    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="queued",
            progress_json={},
            result_json={},
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        for index in range(3):
            operation_jobs.create_job(
                session,
                operation_id=operation.id,
                job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
                queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
                payload={
                    "feed_item_id": index + 1,
                    "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
                },
            )
        operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 10,
                "enqueue_context": {"ai_profile": "sensitive", "ai_provider": "openai-compatible"},
            },
        )
        claimed = operation_jobs._claim_ai_limited_jobs(
            session,
            worker_id="ai-concurrency-test",
            limit=4,
            queue_names=(operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,),
        )

    keys = [job.payload_json["enqueue_context"]["ai_profile"] for job in claimed]
    assert keys.count("general") == 2
    assert keys.count("sensitive") == 1


def test_ai_artifact_claim_scans_past_saturated_profile_window(client, monkeypatch) -> None:
    class FakeSettings:
        database_url = "postgresql://test/test"

    monkeypatch.setattr(operation_jobs, "get_settings", lambda: FakeSettings())
    response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {"artifact_concurrency": 1},
                "sensitive": {"artifact_concurrency": 1},
            }
        },
    )
    assert response.status_code == 200

    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="queued",
            progress_json={},
            result_json={},
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        running = operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 1,
                "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
            },
        )
        now = datetime.now(timezone.utc)
        running.status = JOB_STATUS_RUNNING
        running.locked_by = "already-running"
        running.locked_at = now
        running.lease_expires_at = now + timedelta(minutes=5)
        session.add(running)
        for index in range(65):
            operation_jobs.create_job(
                session,
                operation_id=operation.id,
                job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
                queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
                payload={
                    "feed_item_id": index + 2,
                    "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
                },
            )
        operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 10_000,
                "enqueue_context": {"ai_profile": "sensitive", "ai_provider": "openai-compatible"},
            },
        )
        claim_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        claimed = operation_jobs._claim_ai_limited_jobs(
            session,
            worker_id="ai-concurrency-window-test",
            limit=1,
            queue_names=(operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,),
            now=claim_time,
        )

    assert len(claimed) == 1
    assert claimed[0].payload_json["enqueue_context"]["ai_profile"] == "sensitive"
    assert claimed[0].payload_json["feed_item_id"] == 10_000


def test_ai_artifact_claim_forces_single_job_in_sqlite_test_environment(client) -> None:
    response = client.patch("/api/settings/ai", json={"profiles": {"general": {"artifact_concurrency": 32}}})
    assert response.status_code == 200

    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="queued",
            progress_json={},
            result_json={},
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        for index in range(3):
            operation_jobs.create_job(
                session,
                operation_id=operation.id,
                job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
                queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
                payload={
                    "feed_item_id": index + 1,
                    "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
                },
            )
        claimed = operation_jobs._claim_ai_limited_jobs(
            session,
            worker_id="sqlite-ai-concurrency-test",
            limit=4,
            queue_names=(operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,),
        )

    assert len(claimed) == 1
    assert claimed[0].status == JOB_STATUS_RUNNING


def test_ai_artifact_claim_waits_for_running_ai_job_in_sqlite_test_environment(client) -> None:
    response = client.patch("/api/settings/ai", json={"profiles": {"general": {"artifact_concurrency": 32}}})
    assert response.status_code == 200

    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="queued",
            progress_json={},
            result_json={},
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        running = operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 1,
                "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
            },
        )
        now = datetime.now(timezone.utc)
        running.status = JOB_STATUS_RUNNING
        running.locked_by = "already-running"
        running.locked_at = now
        running.lease_expires_at = now + timedelta(minutes=5)
        session.add(running)
        operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 2,
                "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
            },
        )

        claimed = operation_jobs._claim_ai_limited_jobs(
            session,
            worker_id="sqlite-ai-running-test",
            limit=4,
            queue_names=(operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,),
            now=now,
        )

    assert claimed == []


def test_global_operation_worker_does_not_claim_ai_lane_jobs(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="queued",
            progress_json={},
            result_json={},
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        job = operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 1,
                "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
            },
        )
        session.commit()
        job_id = job.id

    def fail_if_generic_worker_runs_ai_job(*args, **kwargs):
        raise AssertionError("generic operation worker must not execute AI lane jobs")

    monkeypatch.setattr(operation_jobs, "_run_claimed_job", fail_if_generic_worker_runs_ai_job)

    assert not operation_jobs.run_next_available_operation_job(worker_id="generic-worker")

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JOB_STATUS_PENDING


@pytest.mark.file_sqlite
def test_operation_job_worker_starts_ai_lane_and_processes_woken_job(client, monkeypatch) -> None:
    processed = threading.Event()
    operation_jobs.stop_operation_job_worker(timeout_seconds=1)

    def succeed_build(session, *, feed_item_id: int, mode: str, scope: str):
        processed.set()
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", succeed_build)

    try:
        assert operation_jobs.start_operation_job_worker(poll_interval_seconds=0.01)
        with get_session_factory()() as session:
            feed_item = FeedItem(
                original_url="https://alpha.example.com/posts/ai-lane-worker-wake",
                canonical_url="https://alpha.example.com/posts/ai-lane-worker-wake",
                title="AI Lane Worker Wake",
                summary_text="AI lane worker wake summary.",
                content_text="AI lane worker wake content.",
                content_text_source="feed_content",
                content_extraction_status="not_needed",
            )
            session.add(feed_item)
            session.flush()
            operation = operations_service.create_feed_item_insight_artifact_operation(
                session,
                feed_item.id,
                FeedItemInsightArtifactOperationCreate(mode="build_missing"),
            )
            session.commit()
            operation_id = operation.id

        assert processed.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with get_session_factory()() as session:
                operation = session.get(AppOperation, operation_id)
                job = session.scalar(select(Job).where(Job.operation_id == operation_id))
                if operation is not None and operation.status == "completed":
                    assert job is not None
                    assert job.status == JOB_STATUS_SUCCEEDED
                    assert job.locked_by is not None
                    assert ":ai-lane:" in job.locked_by
                    return
            time.sleep(0.02)
        raise AssertionError("AI lane worker did not complete the woken job")
    finally:
        operation_jobs.stop_operation_job_worker(timeout_seconds=2)


@pytest.mark.file_sqlite
def test_ai_lane_worker_recovers_after_engine_exception(client, monkeypatch) -> None:
    processed = threading.Event()
    operation_jobs.stop_operation_job_worker(timeout_seconds=1)
    original_engine = operation_jobs._run_ai_lane_engine
    engine_calls = 0

    def flaky_engine(*args, **kwargs):
        nonlocal engine_calls
        engine_calls += 1
        if engine_calls == 1:
            raise RuntimeError("simulated ai lane engine failure")
        return original_engine(*args, **kwargs)

    def succeed_build(session, *, feed_item_id: int, mode: str, scope: str):
        processed.set()
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "_run_ai_lane_engine", flaky_engine)
    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", succeed_build)

    try:
        assert operation_jobs.start_operation_job_worker(poll_interval_seconds=0.01)
        deadline = time.monotonic() + 2
        while engine_calls < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert engine_calls >= 1

        with get_session_factory()() as session:
            feed_item = FeedItem(
                original_url="https://alpha.example.com/posts/ai-lane-worker-recovery",
                canonical_url="https://alpha.example.com/posts/ai-lane-worker-recovery",
                title="AI Lane Worker Recovery",
                summary_text="AI lane worker recovery summary.",
                content_text="AI lane worker recovery content.",
                content_text_source="feed_content",
                content_extraction_status="not_needed",
            )
            session.add(feed_item)
            session.flush()
            operation = operations_service.create_feed_item_insight_artifact_operation(
                session,
                feed_item.id,
                FeedItemInsightArtifactOperationCreate(mode="build_missing"),
            )
            session.commit()
            operation_id = operation.id

        assert processed.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with get_session_factory()() as session:
                job = session.scalar(select(Job).where(Job.operation_id == operation_id))
                if job is not None and job.status == JOB_STATUS_SUCCEEDED:
                    assert job.status == JOB_STATUS_SUCCEEDED
                    assert engine_calls >= 2
                    return
            time.sleep(0.02)
        raise AssertionError("AI lane worker did not recover after engine failure")
    finally:
        operation_jobs.stop_operation_job_worker(timeout_seconds=2)


def test_ai_lane_until_idle_waits_for_existing_running_ai_job(client) -> None:
    worker_id = "other-ai-lane"
    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="running",
            progress_json={},
            result_json={},
            message="running",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        job = operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 1,
                "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
            },
        )
        now = datetime.now(timezone.utc)
        job.status = JOB_STATUS_RUNNING
        job.locked_by = worker_id
        job.locked_at = now
        job.lease_expires_at = now + timedelta(minutes=5)
        session.add(job)
        session.commit()
        job_id = job.id

    def complete_existing_job() -> None:
        time.sleep(0.05)
        with get_session_factory()() as session:
            complete_job(session, job_id, worker_id=worker_id, result={"ok": True})
            session.commit()

    completer = threading.Thread(target=complete_existing_job)
    completer.start()
    started_at = time.monotonic()
    try:
        ran_job = operation_jobs.run_ai_lane_jobs_until_idle(
            worker_id="ai-lane-existing-running-test",
            heartbeat_interval_seconds=0,
            poll_interval_seconds=0.01,
            max_drain_seconds=1.0,
        )
        elapsed = time.monotonic() - started_at
    finally:
        completer.join(timeout=1)

    assert not ran_job
    assert elapsed >= 0.04
    assert elapsed < 1.0
    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JOB_STATUS_SUCCEEDED


def test_ai_lane_refills_slot_without_waiting_for_full_batch(client, monkeypatch) -> None:
    class FakeJob:
        def __init__(self, job_id: int) -> None:
            self.id = job_id

    pending_job_ids = [1, 2, 3]
    claim_limits: list[int] = []
    job_2_started = threading.Event()
    release_job_2 = threading.Event()
    job_3_started = threading.Event()

    def fake_claim_ai_lane_jobs(*, worker_id: str, limit: int, lease_seconds: int):
        del worker_id, lease_seconds
        claim_limits.append(limit)
        claimed = pending_job_ids[:limit]
        del pending_job_ids[:limit]
        return [FakeJob(job_id) for job_id in claimed]

    def fake_run_claimed_job(job_id: int, **kwargs):
        del kwargs
        if job_id == 2:
            job_2_started.set()
            assert release_job_2.wait(timeout=5)
        elif job_id == 3:
            assert job_2_started.is_set()
            assert not release_job_2.is_set()
            job_3_started.set()

    monkeypatch.setattr(operation_jobs, "_claim_ai_lane_jobs", fake_claim_ai_lane_jobs)
    monkeypatch.setattr(operation_jobs, "_run_claimed_job", fake_run_claimed_job)
    monkeypatch.setattr(operation_jobs, "_sync_job_backed_operations_after_worker_activity", lambda: None)

    lane_thread = threading.Thread(
        target=operation_jobs.run_ai_lane_jobs_until_idle,
        kwargs={
            "worker_id": "sliding-window-test",
            "heartbeat_interval_seconds": 0,
            "poll_interval_seconds": 0.01,
            "max_concurrent_jobs": 2,
        },
    )
    lane_thread.start()

    assert job_2_started.wait(timeout=5)
    assert job_3_started.wait(timeout=5)
    release_job_2.set()
    lane_thread.join(timeout=5)

    assert not lane_thread.is_alive()
    assert claim_limits[:2] == [2, 1]


def test_saved_item_insight_claim_counts_against_ai_artifact_concurrency(client, monkeypatch) -> None:
    class FakeSettings:
        database_url = "postgresql://test/test"

    monkeypatch.setattr(operation_jobs, "get_settings", lambda: FakeSettings())
    response = client.patch("/api/settings/ai", json={"profiles": {"general": {"artifact_concurrency": 1}}})
    assert response.status_code == 200

    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="saved_item_insight",
            operation_kind="rebuild",
            status="queued",
            progress_json={},
            result_json={},
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        running = operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "feed_item_id": 1,
                "enqueue_context": {"ai_profile": "general", "ai_provider": "deepseek"},
            },
        )
        now = datetime.now(timezone.utc)
        running.status = JOB_STATUS_RUNNING
        running.locked_by = "already-running"
        running.locked_at = now
        running.lease_expires_at = now + timedelta(minutes=5)
        session.add(running)
        operation_jobs.create_job(
            session,
            operation_id=operation.id,
            job_type=operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT,
            queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
            payload={
                "saved_item_id": 1,
                "ai_route": "general",
                "enqueue_context": {
                    "saved_item_id": 1,
                    "ai_profile": "general",
                    "ai_provider": "deepseek",
                    "ai_route": "general",
                    "ai_model": "deepseek-v4-flash",
                },
            },
        )
        claimed = operation_jobs._claim_ai_limited_jobs(
            session,
            worker_id="saved-insight-concurrency-test",
            limit=1,
            queue_names=(operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
            job_types=(operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT,),
        )

    assert claimed == []


def test_homepage_artifact_operation_handler_runtime_failure_fails_without_retry_redacted(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Homepage Failure Source",
            kind="rss",
            url="https://homepage-failure.example.com/feed.xml",
            site_url="https://homepage-failure.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        _create_missing_feed_item_for_source(session, source_id=source.id)
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="global", limit=1),
        )
        session.commit()
        operation_id = operation.id

    def fail_artifact_item(session, *, feed_item_id: int, mode: str, scope: str):
        raise RuntimeError("homepage artifact item failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fail_artifact_item)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="homepage-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert operation.message == "Feed item insight artifact fill operation failed."
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == 1


def test_global_homepage_artifact_job_fails_when_source_profile_changes_before_item_build(client, monkeypatch) -> None:
    unique = uuid4().hex
    with get_session_factory()() as session:
        source = Source(
            name="Global Source Profile Stale",
            kind="rss",
            url=f"https://global-source-profile-stale-{unique}.example.com/feed.xml",
            site_url=f"https://global-source-profile-stale-{unique}.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        feed_item = FeedItem(
            original_url=f"https://global-source-profile-stale-{unique}.example.com/posts/1",
            canonical_url=f"https://global-source-profile-stale-{unique}.example.com/posts/1",
            title="Global Source Profile Stale Item",
            published_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
            summary_text="Global source profile stale summary.",
            content_text="Global source profile stale content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        session.add(ItemState(feed_item_id=feed_item.id, status="unread"))
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=feed_item.id,
                external_id=feed_item.canonical_url,
                original_url=feed_item.original_url,
                canonical_url=feed_item.canonical_url,
                published_at=feed_item.published_at,
                match_rule="external_id",
                match_value=feed_item.canonical_url,
            )
        )
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="global", limit=1),
        )
        session.commit()
        source_id = source.id
        operation_id = operation.id

    def build_should_not_run(session, *, feed_item_id: int, mode: str, scope: str):
        raise AssertionError("global source profile stale check should run before item build")

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", build_should_not_run)

    draft_response = client.put(
        f"/api/sources/{source_id}/preference-profile/draft",
        json={"subscription_reason": "Changed after global job enqueue."},
    )
    assert draft_response.status_code == 200
    activate_response = client.post(f"/api/sources/{source_id}/preference-profile/draft/activate")
    assert activate_response.status_code == 200

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="homepage-global-source-profile-stale-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "source_profile_version" in operation.error_message
    assert job is not None
    assert job.status == JOB_STATUS_FAILED


def test_feed_item_insight_artifact_operation_item_failure_preserves_progress_counters(client, monkeypatch) -> None:
    unique = uuid4().hex
    with get_session_factory()() as session:
        source = Source(
            name="Homepage Item Failure Source",
            kind="rss",
            url=f"https://homepage-item-failure-{unique}.example.com/feed.xml",
            site_url=f"https://homepage-item-failure-{unique}.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        feed_item = FeedItem(
            original_url=f"https://homepage-item-failure-{unique}.example.com/posts/1",
            canonical_url=f"https://homepage-item-failure-{unique}.example.com/posts/1",
            title="Homepage Item Failure",
            published_at=datetime(2026, 5, 22, 12, 5, tzinfo=timezone.utc),
            summary_text="Homepage item failure summary.",
            content_text="Homepage item failure content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        session.add(ItemState(feed_item_id=feed_item.id, status="unread"))
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=feed_item.id,
                external_id=feed_item.canonical_url,
                original_url=feed_item.original_url,
                canonical_url=feed_item.canonical_url,
                published_at=feed_item.published_at,
                match_rule="external_id",
                match_value=feed_item.canonical_url,
            )
        )
        session.flush()
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(scope="global", limit=1),
        )
        session.commit()
        operation_id = operation.id

    def fail_item_build(session, *, feed_item_id: int, mode: str, scope: str):
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=0,
            artifact_rows_created=0,
            failed_feed_item_count=1,
        )

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fail_item_build)

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="homepage-item-failure-test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["failed_feed_item_count"] == 1
    assert operation.progress_json["success_count"] == 0
    assert operation.progress_json["error_count"] == 1
    assert operation.progress_json["job_succeeded_count"] == 1
    assert operation.progress_json["job_failed_count"] == 0
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED


def test_feed_item_extract_operation_duplicate_active_conflict_keeps_single_job(client) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/duplicate-job",
            canonical_url="https://alpha.example.com/posts/duplicate-job",
            title="Duplicate Job Extract",
            summary_text="Duplicate extract summary.",
            content_text="Duplicate extract content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_extract_operation(session, feed_item.id)
        session.commit()
        feed_item_id = feed_item.id
        operation_id = operation.id

    response = client.post(f"/api/operations/feed-items/{feed_item_id}/extract-content")
    assert response.status_code == 409
    assert response.json()["detail"]["operation"]["id"] == operation_id

    with get_session_factory()() as session:
        job_count = session.scalar(select(func.count()).select_from(Job).where(Job.operation_id == operation_id))
    assert job_count == 1


def test_feed_item_extract_operation_stop_cancels_pending_job(client) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/stop-job",
            canonical_url="https://alpha.example.com/posts/stop-job",
            title="Stop Job Extract",
            summary_text="Stop extract summary.",
            content_text="Stop extract content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_extract_operation(session, feed_item.id)
        stopped = operations_service.request_stop_operation(session, operation.id)
        session.commit()
        operation_id = stopped.id
    _drain_operation(operation_id)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert operation.progress_json["stopped"] is True
    assert job is not None
    assert job.status == JOB_STATUS_CANCELLED


def test_feed_item_extract_operation_running_stop_finishes_cancelled(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/running-stop-job",
            canonical_url="https://alpha.example.com/posts/running-stop-job",
            title="Running Stop Job Extract",
            summary_text="Running stop extract summary.",
            content_text="Running stop extract content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_extract_operation(session, feed_item.id)
        session.commit()
        feed_item_id = feed_item.id
        operation_id = operation.id

    handler_started = threading.Event()
    handler_can_finish = threading.Event()

    class FakeReaderDetail:
        title = "Running Stop Job Extract"

        def model_dump(self, mode: str):
            return {"id": feed_item_id, "title": self.title}

    def fake_extract(session, item_id: int):
        assert item_id == feed_item_id
        handler_started.set()
        assert handler_can_finish.wait(timeout=5)
        return FakeReaderDetail()

    monkeypatch.setattr(operation_jobs, "extract_reader_item_content", fake_extract)

    worker_thread = threading.Thread(
        target=operation_jobs.run_operation_jobs_for_operation,
        kwargs={
            "operation_id": operation_id,
            "worker_id": "test-worker",
            "heartbeat_interval_seconds": 0,
        },
    )
    worker_thread.start()
    assert handler_started.wait(timeout=5)

    with get_session_factory()() as session:
        stopped = operations_service.request_stop_operation(session, operation_id)
        session.commit()
        assert stopped.stop_requested is True

    handler_can_finish.set()
    worker_thread.join(timeout=5)
    assert not worker_thread.is_alive()

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "stopped"
    assert operation.progress_json["stage"] == "stopped"
    assert job is not None
    assert job.status == JOB_STATUS_CANCELLED


def test_feed_item_extract_operation_worker_runtime_failure_fails_without_retry_redacted(client, monkeypatch) -> None:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://alpha.example.com/posts/failing-job",
            canonical_url="https://alpha.example.com/posts/failing-job",
            title="Failing Job Extract",
            summary_text="Failing extract summary.",
            content_text="Failing extract content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = operations_service.create_feed_item_extract_operation(session, feed_item.id)
        session.commit()
        operation_id = operation.id

    def fail_extract(session, feed_item_id: int):
        raise RuntimeError("provider failed api_key=sk-secret-token")

    monkeypatch.setattr(operation_jobs, "extract_reader_item_content", fail_extract)

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="test-worker",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=5,
        failure_backoff_seconds=1,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "failed"
    assert operation.error_message is not None
    assert "sk-secret-token" not in operation.error_message
    assert "[REDACTED]" in operation.error_message
    assert job is not None
    assert job.status == JOB_STATUS_FAILED
    assert job.attempts == 1


def create_source_for_sync(client, name: str, url: str, *, ai_route: str = "general") -> int:
    response = client.post(
        "/api/sources",
        json={
            "name": name,
            "kind": "rss",
            "url": url,
            "site_url": url.replace("/feed.xml", ""),
            "folder_path": None,
            "is_active": True,
            "ai_route": ai_route,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def sync_source_with_operation(client, source_id: int, *, artifact_limit: int | None = 0) -> int:
    response = client.post(
        "/api/operations/source-sync",
        json={
            "kind": "single",
            "source_id": source_id,
            "feed_item_insight_artifact_build_limit": artifact_limit,
        },
    )
    assert response.status_code == 202
    operation_id = response.json()["id"]
    _drain_operation(operation_id)
    return operation_id


def _create_missing_feed_item_for_source(session, *, source_id: int) -> FeedItem:
    source = session.get(Source, source_id)
    assert source is not None
    unique = uuid4().hex
    item = FeedItem(
        original_url=f"{source.site_url or source.url}/posts/{unique}",
        canonical_url=f"{source.site_url or source.url}/posts/{unique}",
        title=f"Missing Artifact Item {unique}",
        published_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        summary_text="Missing artifact summary.",
        content_text="Missing artifact content.",
        content_text_source="feed_content",
        content_extraction_status="not_needed",
    )
    session.add(item)
    session.flush()
    session.add(ItemState(feed_item_id=item.id, status="unread"))
    session.add(
        SourceItemOccurrence(
            source_id=source.id,
            feed_item_id=item.id,
            external_id=item.canonical_url,
            original_url=item.original_url,
            canonical_url=item.canonical_url,
            published_at=item.published_at,
            match_rule="external_id",
            match_value=item.canonical_url,
        )
    )
    session.flush()
    return item


def _add_complete_feed_item_insight_artifacts(session, *, feed_item_id: int) -> None:
    for artifact_kind in (
        "feed_item_summary_v1",
        "feed_item_topics_v1",
        "feed_item_ranking_v1",
        "feed_item_routing_v1",
    ):
        session.add(
            InsightEngineArtifact(
                feed_item_id=feed_item_id,
                artifact_kind=artifact_kind,
                model_name="test-model",
                prompt_version="test",
                artifact_payload={"schema_version": artifact_kind},
            )
        )
    session.flush()


def _create_channel_lifecycle_item(
    session,
    *,
    channel_id: str,
    secondary_channels: list[dict],
) -> FeedItem:
    source = Source(
        name=f"Lifecycle Source {channel_id}",
        kind="rss",
        url=f"https://{channel_id}.example.com/feed.xml",
        site_url=f"https://{channel_id}.example.com",
        flow_policy="normal",
        provenance="manual",
    )
    session.add(source)
    session.flush()
    item = FeedItem(
        original_url=f"https://{channel_id}.example.com/posts/1",
        canonical_url=f"https://{channel_id}.example.com/posts/1",
        title=f"Lifecycle Item {channel_id}",
        published_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        summary_text="Lifecycle summary.",
        content_text="Lifecycle content.",
        content_text_source="feed_content",
        content_extraction_status="not_needed",
    )
    session.add(item)
    session.flush()
    session.add(ItemState(feed_item_id=item.id, status="unread"))
    session.add(
        SourceItemOccurrence(
            source_id=source.id,
            feed_item_id=item.id,
            external_id=item.canonical_url,
            original_url=item.original_url,
            canonical_url=item.canonical_url,
            published_at=item.published_at,
            match_rule="external_id",
            match_value=item.canonical_url,
        )
    )
    session.add(
        InsightEngineArtifact(
            feed_item_id=item.id,
            artifact_kind="feed_item_routing_v1",
            model_name="test",
            prompt_version="test",
            artifact_payload={
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": channel_id,
                "secondary_channels": secondary_channels,
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.7,
                "reason_codes": ["content_topic_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Lifecycle fixture.",
                "source": "ai",
            },
        )
    )
    session.flush()
    return item


def _create_minimal_saved_item() -> int:
    with get_session_factory()() as session:
        saved_item = SavedItem(
            origin_type="manual_url",
            ai_route="sensitive",
            title="Mirror Job Saved Item",
            canonical_url="https://manual.example.com/mirror-job-saved-item",
            source_name="Manual",
        )
        session.add(saved_item)
        session.commit()
        return saved_item.id


def install_transport(monkeypatch, mapping: dict[str, httpx.Response]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = mapping[str(request.url)]
        response.request = request
        return response

    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(source_sync, "create_http_client", fake_create_http_client)
    monkeypatch.setattr(feed_content_extraction, "create_http_client", fake_create_http_client)


def configure_ai(client, *, provider: str = "openai-compatible", base_url: str = "https://openai.example.com/v1") -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": provider,
                    "enabled": True,
                    "base_url": base_url,
                    "model": "test-model",
                    "api_key": "test-key",
                }
            }
        },
    )
    assert response.status_code == 200


def install_ai_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(ai_provider, "create_http_client", fake_create_http_client)


def build_ai_success_handler(requests_seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        payload = {
            "summary": "AI generated summary",
            "topics": ["ai_technology", "knowledge_information"],
            "content_type": "analysis",
            "priority_score": 82,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "Specific insight", "weight": 82}],
            "low_priority_bucket": False,
            "routing": {
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "ai_agents_frontier",
                "secondary_channels": [
                    {"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "list"}
                ],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.82,
                "reason_codes": ["source_channel_tendency_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Test routing",
            },
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    return handler


def _assert_feed_item_insight_enqueue_context(payload: dict, *, feed_item_id: int | None = None) -> None:
    context = payload["enqueue_context"]
    assert context["feed_item_id"] == feed_item_id
    assert "source_id" in context
    assert context["profile_version"] == "default-2026-04-29"
    assert context["channel_catalog_version"] >= 1
    assert context["source_profile_version"] is None
    assert context["source_profile_is_empty"] is True
    assert context["feature_schema_version"] == "insight_feature_v1"
    assert context["score_version"] == "score_v1_hand_weighted"
    assert isinstance(context["operation_id"], int)


def _initial_progress() -> dict[str, object]:
    return {
        "stage": "idle",
        "total": 1,
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
