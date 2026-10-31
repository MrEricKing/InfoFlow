from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from app.core.config import reset_settings_cache
from app.core.security import decrypt_secret, is_encrypted_value
from app.db.models import (
    AppOperation,
    AppSetting,
    FeedItem,
    FeedItemHomepageFacet,
    FeedItemHomepageReadModel,
    InsightEngineArtifact,
    ItemState,
    Job,
    SavedItem,
    Source,
    SourceItemOccurrence,
)
from app.db.session import get_session_factory, reset_session_state
from app.main import app
from app.schemas.settings import AISettingsUpdateRequest
from app.schemas.operations import SourceSyncOperationCreate
from app.services import (
    homepage as homepage_service,
    insight_engine_artifact_builder,
    insight_engine_artifacts,
    operation_jobs,
    operations as operations_service,
    source_sync,
)
from app.services.app_settings import resolve_ai_provider_config, update_ai_settings
from app.services.homepage_read_model import (
    HOMEPAGE_READ_MODEL_LOCK_NAMESPACE,
    rebuild_homepage_read_models_for_feed_items,
)
from app.services.insight_channels import ensure_default_channels
from app.services.job_queue import (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobLeaseError,
    claim_jobs,
    complete_job,
    create_job,
    extend_job_lease,
    manual_requeue_job,
)

pytestmark = pytest.mark.postgres

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "IcMtG9uOh01Ha0HCFhCqsXIEmZOh4TT73YvCBWX_iuw="
HOMEPAGE_SUMMARY_KIND = "feed_item_summary_v1"
HOMEPAGE_TOPICS_KIND = "feed_item_topics_v1"
SAVED_ITEM_INSIGHT_KIND = "saved_item_insight_v1"


@pytest.fixture()
def postgres_database_url(monkeypatch: pytest.MonkeyPatch) -> str:
    with PostgresContainer("postgres:16") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setenv("INFOFLOW_SETTINGS_ENCRYPTION_KEY", TEST_FERNET_KEY)
        reset_settings_cache()
        reset_session_state()
        yield database_url
        reset_session_state()
        reset_settings_cache()


def test_postgres_alembic_upgrade_and_encrypted_settings_roundtrip(postgres_database_url: str) -> None:
    _upgrade_head()

    payload = AISettingsUpdateRequest.model_validate(
        {
            "profiles": {
                "general": {
                    "provider": "deepseek",
                    "enabled": True,
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash",
                    "api_key": "sk-postgres-secret",
                }
            }
        }
    )
    with get_session_factory()() as session:
        update_ai_settings(session, payload)
        session.commit()

    with get_session_factory()() as session:
        stored = session.scalar(select(AppSetting.value).where(AppSetting.key == "ai_general_api_key"))
        config = resolve_ai_provider_config(session)

    assert stored is not None
    assert is_encrypted_value(stored)
    assert decrypt_secret(stored, TEST_FERNET_KEY) == "sk-postgres-secret"
    assert config.api_key == "sk-postgres-secret"


def test_postgres_stop_request_times_out_quickly_when_operation_row_is_locked(postgres_database_url: str) -> None:
    _upgrade_head()
    source_id = _create_source("Locked Stop Source", "https://locked-stop.example.com/feed.xml")
    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id),
        )
        session.commit()
        operation_id = operation.id

    locker_session = get_session_factory()()
    stop_session = get_session_factory()()
    try:
        locked_operation = locker_session.execute(
            select(AppOperation).where(AppOperation.id == operation_id).with_for_update()
        ).scalar_one()
        locked_operation.message = "holding row lock"
        locker_session.flush()

        started = time.monotonic()
        with pytest.raises(operations_service.OperationStopLockTimeoutError):
            operations_service.request_stop_operation(stop_session, operation_id)
            stop_session.commit()
        elapsed = time.monotonic() - started
        assert elapsed < 4.0
        stop_session.rollback()
    finally:
        locker_session.rollback()
        locker_session.close()
        stop_session.close()

    with get_session_factory()() as session:
        operation = operations_service.request_stop_operation(session, operation_id)
        session.commit()
        assert operation.stop_requested is True
        assert operation.status == "stopping"


def test_postgres_homepage_read_model_schema_constraints_and_indexes(postgres_database_url: str) -> None:
    _upgrade_head()

    with get_session_factory()() as session:
        read_model_constraints = {
            row.conname: (row.contype, row.confrelid, row.confdeltype)
            for row in session.execute(
                text(
                    """
                    SELECT conname, contype, confrelid::regclass::text AS confrelid, confdeltype
                    FROM pg_constraint
                    WHERE conrelid = 'feed_item_homepage_read_models'::regclass
                    """
                )
            )
        }
        facet_constraints = {
            row.conname: (row.contype, row.confrelid, row.confdeltype)
            for row in session.execute(
                text(
                    """
                    SELECT conname, contype, confrelid::regclass::text AS confrelid, confdeltype
                    FROM pg_constraint
                    WHERE conrelid = 'feed_item_homepage_facets'::regclass
                    """
                )
            )
        }
        read_model_indexes = set(
            session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename = 'feed_item_homepage_read_models'
                    """
                )
            ).scalars()
        )
        facet_indexes = set(
            session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename = 'feed_item_homepage_facets'
                    """
                )
            ).scalars()
        )

    assert read_model_constraints["feed_item_homepage_read_models_pkey"][0] == "p"
    assert ("f", "feed_items", "c") in read_model_constraints.values()
    assert ("f", "sources", "c") in read_model_constraints.values()
    assert facet_constraints["uq_homepage_facet_item_kind_id"][0] == "u"
    assert ("f", "feed_item_homepage_read_models", "c") in facet_constraints.values()

    assert {
        "ix_homepage_rm_newest",
        "ix_homepage_rm_priority",
        "ix_homepage_rm_source_sort",
        "ix_homepage_rm_stale",
        "ix_homepage_rm_read_model_version",
    }.issubset(read_model_indexes)
    assert {
        "ix_homepage_facets_item_kind",
        "ix_homepage_facets_kind_id",
        "ix_feed_item_homepage_facets_feed_item_id",
    }.issubset(facet_indexes)


def test_postgres_insight_artifact_owner_constraints_and_latest_indexes(postgres_database_url: str) -> None:
    _upgrade_head()

    with get_session_factory()() as session:
        constraint_names = set(
            session.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'insight_engine_artifacts'::regclass
                    AND contype = 'c'
                    """
                )
            ).scalars()
        )
        index_names = set(
            session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename = 'insight_engine_artifacts'
                    """
                )
            ).scalars()
        )

    assert "ck_ie_artifacts_exactly_one_owner" in constraint_names
    assert "ck_ie_artifacts_kind_owner" in constraint_names
    assert "ix_insight_engine_artifacts_kind_feed_latest" in index_names
    assert "ix_insight_engine_artifacts_kind_saved_latest" in index_names

    feed_item_id, saved_item_id = _create_artifact_owners()
    _insert_artifact(feed_item_id=feed_item_id, artifact_kind=HOMEPAGE_SUMMARY_KIND)
    _insert_artifact(saved_item_id=saved_item_id, artifact_kind=SAVED_ITEM_INSIGHT_KIND)

    invalid_cases = [
        {"artifact_kind": HOMEPAGE_SUMMARY_KIND},
        {"feed_item_id": feed_item_id, "saved_item_id": saved_item_id, "artifact_kind": HOMEPAGE_SUMMARY_KIND},
        {"feed_item_id": feed_item_id, "artifact_kind": SAVED_ITEM_INSIGHT_KIND},
        {"saved_item_id": saved_item_id, "artifact_kind": HOMEPAGE_TOPICS_KIND},
    ]
    for invalid_case in invalid_cases:
        with pytest.raises(IntegrityError):
            _insert_artifact(**invalid_case)

    with get_session_factory()() as session:
        session.execute(text("SET enable_seqscan = off"))
        homepage_plan = "\n".join(
            session.execute(
                text(
                    """
                    EXPLAIN (FORMAT TEXT)
                    SELECT id
                    FROM insight_engine_artifacts
                    WHERE feed_item_id = :feed_item_id
                    AND artifact_kind = 'feed_item_summary_v1'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"feed_item_id": feed_item_id},
            ).scalars()
        )
        saved_plan = "\n".join(
            session.execute(
                text(
                    """
                    EXPLAIN (FORMAT TEXT)
                    SELECT id
                    FROM insight_engine_artifacts
                    WHERE saved_item_id = :saved_item_id
                    AND artifact_kind = 'saved_item_insight_v1'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"saved_item_id": saved_item_id},
            ).scalars()
        )

    assert "ix_insight_engine_artifacts_kind_feed_latest" in homepage_plan
    assert "ix_insight_engine_artifacts_kind_saved_latest" in saved_plan


def test_postgres_migration_defaults_are_usable_after_fresh_upgrade(postgres_database_url: str) -> None:
    _upgrade_head()

    with get_session_factory()() as session:
        source_defaults = session.execute(
            text(
                """
                INSERT INTO sources (name, kind, url, is_active, ai_route, provenance)
                VALUES ('Default Source', 'rss', 'https://defaults.example.com/feed.xml', true, 'general', 'manual')
                RETURNING flow_policy, created_at, updated_at
                """
            )
        ).one()
        operation_defaults = session.execute(
            text(
                """
                INSERT INTO app_operations (operation_type, operation_kind, status)
                VALUES ('phase_1c_default_check', 'global', 'queued')
                RETURNING stop_requested, created_at, updated_at
                """
            )
        ).one()
        channel_defaults = session.execute(
            text(
                """
                INSERT INTO insight_channels (id, label, description, reading_depth, sort_order, enabled)
                VALUES ('phase_1c_defaults', 'Phase 1c Defaults', '', 'medium', 999, true)
                RETURNING focus_prompt, boundary_prompt, default_flow_policy, created_at, updated_at
                """
            )
        ).one()
        session.commit()

    assert source_defaults.flow_policy == "normal"
    assert source_defaults.created_at is not None
    assert source_defaults.updated_at is not None
    assert operation_defaults.stop_requested is False
    assert operation_defaults.created_at is not None
    assert operation_defaults.updated_at is not None
    assert channel_defaults.focus_prompt == ""
    assert channel_defaults.boundary_prompt == ""
    assert channel_defaults.default_flow_policy == "normal"
    assert channel_defaults.created_at is not None
    assert channel_defaults.updated_at is not None


def test_postgres_job_queue_schema_constraints_and_indexes(postgres_database_url: str) -> None:
    _upgrade_head()

    with get_session_factory()() as session:
        job_constraints = set(
            session.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'jobs'::regclass
                    """
                )
            ).scalars()
        )
        job_event_fk_count = session.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_constraint
                WHERE conrelid = 'job_events'::regclass
                AND contype = 'f'
                """
            )
        )
        job_indexes = set(
            session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename = 'jobs'
                    """
                )
            ).scalars()
        )
        job_event_indexes = set(
            session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename = 'job_events'
                    """
                )
            ).scalars()
        )

    assert "ck_jobs_status" in job_constraints
    assert "ck_jobs_attempts_non_negative" in job_constraints
    assert "ck_jobs_max_attempts_positive" in job_constraints
    assert job_event_fk_count == 1
    assert "uq_jobs_active_idempotency" in job_indexes
    assert "ix_jobs_claim_order" in job_indexes
    assert "ix_jobs_lease_reclaim" in job_indexes
    assert "ix_jobs_operation_status" in job_indexes
    assert "ix_job_events_job_created" in job_event_indexes


def test_postgres_job_queue_active_idempotency_partial_unique(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id = _create_operation()

    with get_session_factory()() as session:
        first = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            idempotency_key="source:1:sync",
        )
        session.commit()
        first_id = first.id

    with pytest.raises(IntegrityError):
        with get_session_factory()() as session:
            create_job(
                session,
                operation_id=operation_id,
                job_type="test_job",
                payload={"source_id": 1},
                idempotency_key="source:1:sync",
            )
            session.commit()

    with get_session_factory()() as session:
        first = session.get(Job, first_id)
        assert first is not None
        first.status = JOB_STATUS_SUCCEEDED
        first.finished_at = datetime.now(timezone.utc)
        session.add(first)
        session.commit()

    with get_session_factory()() as session:
        replacement = create_job(
            session,
            operation_id=operation_id,
            job_type="test_job",
            payload={"source_id": 1},
            idempotency_key="source:1:sync",
        )
        session.commit()

    assert replacement.id != first_id
    assert replacement.status == JOB_STATUS_PENDING


def test_postgres_feed_item_canonical_url_unique_index_rejects_duplicates(
    postgres_database_url: str,
) -> None:
    _upgrade_head()

    with get_session_factory()() as session:
        session.add_all(
            [
                FeedItem(
                    original_url="https://postgres.example.com/duplicate-a",
                    canonical_url="https://postgres.example.com/duplicate",
                    title="Duplicate A",
                    summary_text="Duplicate A summary.",
                    content_text="Duplicate A content.",
                    content_text_source="feed_content",
                    content_extraction_status="not_needed",
                ),
                FeedItem(
                    original_url="https://postgres.example.com/duplicate-b",
                    canonical_url="https://postgres.example.com/duplicate",
                    title="Duplicate B",
                    summary_text="Duplicate B summary.",
                    content_text="Duplicate B content.",
                    content_text_source="feed_content",
                    content_extraction_status="not_needed",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_postgres_feed_item_canonical_url_partial_unique_index(postgres_database_url: str) -> None:
    _upgrade_head()

    with get_session_factory()() as session:
        index_names = set(
            session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    AND tablename = 'feed_items'
                    """
                )
            ).scalars()
        )
    assert "uq_feed_items_canonical_url_not_null" in index_names

    with get_session_factory()() as session:
        session.add(
            FeedItem(
                original_url="https://postgres.example.com/item-a",
                canonical_url="https://postgres.example.com/shared-canonical",
                title="Item A",
                summary_text="Item A summary.",
                content_text="Item A content.",
                content_text_source="feed_content",
                content_extraction_status="not_needed",
            )
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with get_session_factory()() as session:
            session.add(
                FeedItem(
                    original_url="https://postgres.example.com/item-b",
                    canonical_url="https://postgres.example.com/shared-canonical",
                    title="Item B",
                    summary_text="Item B summary.",
                    content_text="Item B content.",
                    content_text_source="feed_content",
                    content_extraction_status="not_needed",
                )
            )
            session.commit()


def test_postgres_concurrent_source_jobs_reuse_canonical_feed_item(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_head()
    shared_url = "https://shared-postgres-sync.example.com/post"
    source_a_id = _create_source("Postgres A", "https://postgres-a.example.com/feed.xml")
    source_b_id = _create_source("Postgres B", "https://postgres-b.example.com/feed.xml")
    _install_source_sync_transport(
        monkeypatch,
        {
            "https://postgres-a.example.com/feed.xml": httpx.Response(
                200,
                text=_rss_feed("postgres-a-guid", "Postgres Shared Item A", shared_url),
            ),
            "https://postgres-b.example.com/feed.xml": httpx.Response(
                200,
                text=_rss_feed("postgres-b-guid", "Postgres Shared Item B", shared_url),
            ),
        },
    )
    original_match_or_create = source_sync.match_or_create_feed_item
    match_barrier = threading.Barrier(2)

    def synchronized_match_or_create(session, entry):
        match_barrier.wait(timeout=10)
        return original_match_or_create(session, entry)

    monkeypatch.setattr(source_sync, "match_or_create_feed_item", synchronized_match_or_create)

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="all", feed_item_insight_artifact_build_limit=0),
        )
        operation_id = operation.id
        session.commit()

    operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="postgres-source-sync-test",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.05,
        max_drain_seconds=10,
        max_concurrent_jobs=2,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "completed", operation.error_message
        assert operation.result_json["success_count"] == 2, operation.result_json
        assert session.scalar(select(FeedItem.id).where(FeedItem.canonical_url == shared_url)) is not None
        assert session.query(FeedItem).count() == 1
        assert session.query(ItemState).count() == 1
        occurrences = session.query(SourceItemOccurrence).order_by(SourceItemOccurrence.source_id.asc()).all()
        assert len(occurrences) == 2
        assert {occurrence.source_id for occurrence in occurrences} == {source_a_id, source_b_id}
        assert {occurrence.match_rule for occurrence in occurrences}.issubset({"created", "canonical_url"})


def test_postgres_source_sync_operation_creation_mutex_prevents_duplicate_active_operations(
    postgres_database_url: str,
) -> None:
    _upgrade_head()
    _create_source("Operation Mutex", "https://operation-mutex.example.com/feed.xml")
    first_created = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []
    conflicts: list[bool] = []

    def create_first() -> None:
        try:
            with get_session_factory()() as session:
                operations_service.create_source_sync_operation(
                    session,
                    SourceSyncOperationCreate(kind="all", feed_item_insight_artifact_build_limit=0),
                )
                first_created.set()
                assert release_first.wait(timeout=10)
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    def create_second() -> None:
        try:
            with get_session_factory()() as session:
                first_created.wait(timeout=5)
                try:
                    operations_service.create_source_sync_operation(
                        session,
                        SourceSyncOperationCreate(kind="all", feed_item_insight_artifact_build_limit=0),
                    )
                except operations_service.OperationConflictError:
                    conflicts.append(True)
                    session.rollback()
                    return
                session.commit()
                conflicts.append(False)
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first = threading.Thread(target=create_first, name="operation-mutex-first")
    second = threading.Thread(target=create_second, name="operation-mutex-second")
    first.start()
    assert first_created.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    assert not second_finished.is_set()
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert conflicts == [True]
    with get_session_factory()() as session:
        operations = list(session.scalars(select(AppOperation).where(AppOperation.operation_type == "source_sync")))
    assert len(operations) == 1
    assert operations[0].status == "queued"


def test_postgres_homepage_read_model_rebuild_serializes_same_feed_item(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_head()
    feed_item_id = _create_homepage_read_model_item()
    original_build = homepage_service._build_homepage_cards
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []

    def blocking_build_homepage_cards(session, *args, **kwargs):
        thread_name = threading.current_thread().name
        if thread_name == "homepage-rm-first":
            first_entered.set()
            if not release_first.wait(timeout=10):
                raise AssertionError("timed out waiting to release first homepage read model rebuild")
        elif thread_name == "homepage-rm-second":
            second_entered.set()
        return original_build(session, *args, **kwargs)

    monkeypatch.setattr(homepage_service, "_build_homepage_cards", blocking_build_homepage_cards)

    def rebuild_in_thread() -> None:
        try:
            with get_session_factory()() as session:
                rebuild_homepage_read_models_for_feed_items(session, [feed_item_id])
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=rebuild_in_thread, name="homepage-rm-first")
    second = threading.Thread(target=rebuild_in_thread, name="homepage-rm-second")
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert second_entered.is_set()

    with get_session_factory()() as session:
        row = session.get(FeedItemHomepageReadModel, feed_item_id)
        assert row is not None
        assert row.read_model_version == "homepage_read_model_v1"
        assert row.is_stale is False
        assert row.card_json["primary_channel_id"] == "ai_agents_frontier"
        facets = list(
            session.scalars(
                select(FeedItemHomepageFacet).where(FeedItemHomepageFacet.feed_item_id == feed_item_id)
            )
        )

    facet_keys = {(facet.facet_kind, facet.facet_id) for facet in facets}
    assert len(facet_keys) == len(facets)
    assert ("channel", "ai_agents_frontier") in facet_keys


def test_postgres_homepage_read_model_rebuild_sorts_overlapping_item_locks(
    postgres_database_url: str,
) -> None:
    _upgrade_head()
    first_item_id = _create_homepage_read_model_item("overlap-a")
    second_item_id = _create_homepage_read_model_item("overlap-b")
    smaller_id, larger_id = sorted([first_item_id, second_item_id])
    errors: list[BaseException] = []

    def rebuild_in_thread(feed_item_ids: list[int]) -> None:
        try:
            with get_session_factory()() as session:
                rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    holder_session = get_session_factory()()
    holder_session.execute(_homepage_read_model_lock_sql(), _homepage_read_model_lock_params(smaller_id))
    try:
        thread = threading.Thread(
            target=rebuild_in_thread,
            args=([larger_id, smaller_id],),
            name="homepage-rm-overlap-reversed",
        )
        thread.start()
        assert _wait_for_pending_advisory_lock()

        with get_session_factory()() as probe_session:
            larger_id_available = probe_session.scalar(
                _homepage_read_model_try_lock_sql(),
                _homepage_read_model_lock_params(larger_id),
            )
            probe_session.rollback()

        assert larger_id_available is True
    finally:
        holder_session.rollback()
        holder_session.close()

    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []

    reverse_thread = threading.Thread(
        target=rebuild_in_thread,
        args=([second_item_id, first_item_id],),
        name="homepage-rm-overlap-final",
    )
    reverse_thread.start()
    reverse_thread.join(timeout=10)

    assert not reverse_thread.is_alive()
    assert errors == []

    with get_session_factory()() as session:
        rows = list(
            session.scalars(
                select(FeedItemHomepageReadModel)
                .where(FeedItemHomepageReadModel.feed_item_id.in_([first_item_id, second_item_id]))
                .order_by(FeedItemHomepageReadModel.feed_item_id.asc())
            )
        )
        facets = list(
            session.scalars(
                select(FeedItemHomepageFacet).where(
                    FeedItemHomepageFacet.feed_item_id.in_([first_item_id, second_item_id])
                )
            )
        )

    assert [row.feed_item_id for row in rows] == [first_item_id, second_item_id]
    assert all(row.is_stale is False for row in rows)
    facet_keys = {(facet.feed_item_id, facet.facet_kind, facet.facet_id) for facet in facets}
    assert len(facet_keys) == len(facets)
    assert (first_item_id, "channel", "ai_agents_frontier") in facet_keys
    assert (second_item_id, "channel", "ai_agents_frontier") in facet_keys


def test_postgres_match_or_create_feed_item_rolls_back_item_when_item_state_fails(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_head()

    class FailingItemState:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("item state failed")

    monkeypatch.setattr(source_sync, "ItemState", FailingItemState)

    with pytest.raises(RuntimeError, match="item state failed"):
        with get_session_factory()() as session:
            source_sync.match_or_create_feed_item(
                session,
                source_sync.NormalizedFeedEntry(
                    external_id="postgres-orphan-test",
                    external_id_is_global=False,
                    original_url="https://postgres.example.com/orphan-test",
                    canonical_url="https://postgres.example.com/orphan-test",
                    title="Orphan Test",
                    author_name=None,
                    published_at=None,
                    summary_text="Summary long enough to avoid extraction. " * 8,
                    feed_content_text="Summary long enough to avoid extraction. " * 8,
                    content_text="Summary long enough to avoid extraction. " * 8,
                    content_text_source="feed_summary",
                    raw_entry_json={"id": "postgres-orphan-test"},
                ),
            )
            session.commit()

    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 0
        assert session.query(ItemState).count() == 0


def test_postgres_job_queue_claim_skip_locked_transactions_do_not_overlap(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id = _create_operation()
    _create_jobs(operation_id, source_ids=[1, 2, 3])
    claimed_at = datetime.now(timezone.utc)

    session_factory = get_session_factory()
    session_one = session_factory()
    session_two = session_factory()
    try:
        first_batch = claim_jobs(session_one, worker_id="worker-one", limit=2, now=claimed_at)
        second_batch = claim_jobs(session_two, worker_id="worker-two", limit=2, now=claimed_at)

        first_ids = {job.id for job in first_batch}
        second_ids = {job.id for job in second_batch}
        assert len(first_ids) == 2
        assert len(second_ids) == 1
        assert first_ids.isdisjoint(second_ids)
    finally:
        session_two.rollback()
        session_two.close()
        session_one.rollback()
        session_one.close()


def test_postgres_ai_artifact_claim_serializes_provider_slots_across_operations(
    postgres_database_url: str,
) -> None:
    _upgrade_head()
    with get_session_factory()() as session:
        update_ai_settings(
            session,
            AISettingsUpdateRequest.model_validate(
                {
                    "profiles": {
                        "general": {
                            "provider": "deepseek",
                            "enabled": True,
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-chat",
                            "api_key": "sk-postgres-claim",
                            "artifact_concurrency": 1,
                        }
                    }
                }
            ),
        )
        ensure_default_channels(session)
        operation_ids: list[int] = []
        for index in range(2):
            operation = AppOperation(
                operation_type=operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
                operation_kind=operation_jobs.OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_GLOBAL,
                status="queued",
                progress_json={},
                result_json={},
                message="queued",
                runner_id="test",
            )
            session.add(operation)
            session.flush()
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
            operation_ids.append(operation.id)
        session.commit()

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def claim_for_operation(operation_id: int, worker_id: str) -> None:
        try:
            with get_session_factory()() as session:
                barrier.wait(timeout=5)
                claimed = operation_jobs._claim_ai_limited_jobs(
                    session,
                    worker_id=worker_id,
                    limit=1,
                    queue_names=(operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,),
                    job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT_BUILD,),
                    operation_id=operation_id,
                    lease_seconds=60,
                )
                session.commit()
                results.append(len(claimed))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=claim_for_operation, args=(operation_ids[0], "worker-one")),
        threading.Thread(target=claim_for_operation, args=(operation_ids[1], "worker-two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(results) == [0, 1]
    with get_session_factory()() as session:
        statuses = list(session.scalars(select(Job.status).order_by(Job.id.asc())))
    assert statuses.count(JOB_STATUS_RUNNING) == 1
    assert statuses.count(JOB_STATUS_PENDING) == 1


def test_postgres_job_queue_lease_reclaim_is_slot_agnostic(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id = _create_operation()
    [job_id] = _create_jobs(operation_id, source_ids=[1])
    claimed_at = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        claimed = claim_jobs(session, worker_id="worker-one", limit=1, lease_seconds=60, now=claimed_at)
        assert [job.id for job in claimed] == [job_id]
        session.commit()

    with get_session_factory()() as session:
        assert claim_jobs(session, worker_id="worker-two", limit=1, now=claimed_at + timedelta(seconds=10)) == []
        session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = claimed_at - timedelta(seconds=1)
        session.add(job)
        session.commit()

    with get_session_factory()() as session:
        reclaimed = claim_jobs(session, worker_id="worker-two", limit=1, now=claimed_at + timedelta(seconds=1))
        session.commit()

    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].locked_by == "worker-two"
    assert reclaimed[0].attempts == 2
    assert reclaimed[0].status == JOB_STATUS_RUNNING


def test_postgres_job_queue_reclaimed_job_rejects_stale_worker_terminal_write(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id = _create_operation()
    [job_id] = _create_jobs(operation_id, source_ids=[1])
    claimed_at = datetime.now(timezone.utc)

    stale_session = get_session_factory()()
    try:
        stale_claimed = claim_jobs(stale_session, worker_id="worker-one", limit=1, lease_seconds=10, now=claimed_at)
        assert [job.id for job in stale_claimed] == [job_id]
        stale_session.commit()
        assert stale_claimed[0].locked_by == "worker-one"

        with get_session_factory()() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.lease_expires_at = claimed_at - timedelta(seconds=1)
            session.add(job)
            session.commit()

        with get_session_factory()() as session:
            reclaimed = claim_jobs(
                session,
                worker_id="worker-two",
                limit=1,
                lease_seconds=30,
                now=claimed_at + timedelta(seconds=1),
            )
            assert [job.id for job in reclaimed] == [job_id]
            session.commit()

        with pytest.raises(JobLeaseError, match="different worker"):
            complete_job(stale_session, job_id, worker_id="worker-one", now=claimed_at + timedelta(seconds=2))
        stale_session.rollback()
    finally:
        stale_session.close()

    with get_session_factory()() as session:
        completed = complete_job(session, job_id, worker_id="worker-two", now=claimed_at + timedelta(seconds=2))
        assert completed.status == JOB_STATUS_SUCCEEDED
        session.commit()


def test_postgres_job_queue_heartbeat_extends_lease(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id = _create_operation()
    [job_id] = _create_jobs(operation_id, source_ids=[1])
    claimed_at = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        claim_jobs(session, worker_id="worker-one", limit=1, lease_seconds=10, now=claimed_at)
        extended = extend_job_lease(
            session,
            job_id,
            worker_id="worker-one",
            lease_seconds=30,
            now=claimed_at + timedelta(seconds=5),
        )
        assert extended.lease_expires_at == claimed_at + timedelta(seconds=35)
        with pytest.raises(JobLeaseError):
            extend_job_lease(session, job_id, worker_id="worker-two")
        session.commit()


def test_postgres_operation_job_worker_dispatch_heartbeats_and_completes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    operation_id, job_id, feed_item_id = _create_feed_item_extract_operation()
    heartbeat_workers: list[str] = []
    original_extend_job_lease = operation_jobs.extend_job_lease

    class FakeReaderDetail:
        title = "Postgres Worker Item"

        def model_dump(self, mode: str):
            return {"id": feed_item_id, "title": self.title}

    def fake_extract_reader_item_content(session, item_id: int):
        assert item_id == feed_item_id
        time.sleep(0.08)
        return FakeReaderDetail()

    def recording_extend_job_lease(*args, **kwargs):
        heartbeat_workers.append(kwargs["worker_id"])
        return original_extend_job_lease(*args, **kwargs)

    monkeypatch.setattr(operation_jobs, "extract_reader_item_content", fake_extract_reader_item_content)
    monkeypatch.setattr(operation_jobs, "extend_job_lease", recording_extend_job_lease)

    assert operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="postgres-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.get(Job, job_id)

    assert heartbeat_workers
    assert set(heartbeat_workers) == {"postgres-worker"}
    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["feed_item_id"] == feed_item_id
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert job.locked_by == "postgres-worker"
    assert job.lease_expires_at is None


def test_postgres_app_startup_worker_recovers_pending_feed_item_extract_job(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    operation_id, _job_id, feed_item_id = _create_feed_item_extract_operation()

    class FakeReaderDetail:
        title = "Startup Worker Item"

        def model_dump(self, mode: str):
            return {"id": feed_item_id, "title": self.title}

    monkeypatch.setattr(
        operation_jobs,
        "extract_reader_item_content",
        lambda session, item_id: FakeReaderDetail(),
    )

    with TestClient(app):
        for _ in range(30):
            with get_session_factory()() as session:
                operation = session.get(AppOperation, operation_id)
                job = session.scalar(select(Job).where(Job.operation_id == operation_id))
                if operation is not None and job is not None and operation.status == "completed":
                    break
            time.sleep(0.1)

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))

    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["feed_item_id"] == feed_item_id
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert ":slot-0" in (job.locked_by or "")


def test_postgres_feed_item_extract_job_reclaim_is_slot_agnostic(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_extract_operation()
    claimed_at = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        claimed = claim_jobs(
            session,
            worker_id="worker-one",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT,),
            operation_id=operation_id,
            lease_seconds=60,
            now=claimed_at,
        )
        assert [job.id for job in claimed] == [job_id]
        session.commit()

    with get_session_factory()() as session:
        assert claim_jobs(
            session,
            worker_id="worker-two",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT,),
            operation_id=operation_id,
            now=claimed_at + timedelta(seconds=10),
        ) == []
        session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = claimed_at - timedelta(seconds=1)
        session.add(job)
        session.commit()

    with get_session_factory()() as session:
        reclaimed = claim_jobs(
            session,
            worker_id="worker-two",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT,),
            operation_id=operation_id,
            now=claimed_at + timedelta(seconds=1),
        )
        session.commit()

    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].locked_by == "worker-two"
    assert reclaimed[0].attempts == 2
    assert reclaimed[0].status == JOB_STATUS_RUNNING


def test_postgres_operation_job_default_drain_worker_ids_reject_stale_terminal_write(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_extract_operation()
    claimed_at = datetime.now(timezone.utc)
    old_worker_id = operation_jobs._new_drain_worker_id()
    new_worker_id = operation_jobs._new_drain_worker_id()
    assert old_worker_id != new_worker_id

    stale_session = get_session_factory()()
    try:
        stale_claimed = claim_jobs(
            stale_session,
            worker_id=old_worker_id,
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT,),
            operation_id=operation_id,
            lease_seconds=10,
            now=claimed_at,
        )
        assert [job.id for job in stale_claimed] == [job_id]
        stale_session.commit()

        with get_session_factory()() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.lease_expires_at = claimed_at - timedelta(seconds=1)
            session.add(job)
            session.commit()

        with get_session_factory()() as session:
            reclaimed = claim_jobs(
                session,
                worker_id=new_worker_id,
                limit=1,
                job_types=(operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT,),
                operation_id=operation_id,
                lease_seconds=30,
                now=claimed_at + timedelta(seconds=1),
            )
            assert [job.id for job in reclaimed] == [job_id]
            session.commit()

        with pytest.raises(JobLeaseError, match="different worker"):
            complete_job(stale_session, job_id, worker_id=old_worker_id, now=claimed_at + timedelta(seconds=2))
        stale_session.rollback()
    finally:
        stale_session.close()

    with get_session_factory()() as session:
        completed = complete_job(session, job_id, worker_id=new_worker_id, now=claimed_at + timedelta(seconds=2))
        assert completed.status == JOB_STATUS_SUCCEEDED
        session.commit()


def test_postgres_feed_item_extract_active_idempotency_and_manual_requeue(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, feed_item_id = _create_feed_item_extract_operation()

    with pytest.raises(IntegrityError):
        with get_session_factory()() as session:
            create_job(
                session,
                operation_id=operation_id,
                job_type=operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT,
                payload={"feed_item_id": feed_item_id},
                idempotency_key=f"operation:feed_item_extract:{feed_item_id}",
            )
            session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JOB_STATUS_SUCCEEDED
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        session.flush()
        requeued = manual_requeue_job(session, job_id, reason="manual pilot retry")
        session.commit()

    assert requeued.id != job_id
    assert requeued.status == JOB_STATUS_PENDING
    assert requeued.job_type == operation_jobs.JOB_TYPE_FEED_ITEM_EXTRACT
    assert requeued.idempotency_key == f"operation:feed_item_extract:{feed_item_id}"


def test_postgres_local_mirror_export_job_worker_dispatch_heartbeats_and_completes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    operation_id, job_id, saved_item_id = _create_local_mirror_export_operation()
    heartbeat_workers: list[str] = []
    original_extend_job_lease = operation_jobs.extend_job_lease

    class FakeSavedItemDetail:
        def model_dump(self, mode: str):
            return {"id": saved_item_id, "local_mirror": {"status": "succeeded"}}

    def fake_export_saved_item_local_mirror(session, item_id: int):
        assert item_id == saved_item_id
        time.sleep(0.08)
        return FakeSavedItemDetail()

    def recording_extend_job_lease(*args, **kwargs):
        heartbeat_workers.append(kwargs["worker_id"])
        return original_extend_job_lease(*args, **kwargs)

    monkeypatch.setattr(operation_jobs, "export_saved_item_local_mirror", fake_export_saved_item_local_mirror)
    monkeypatch.setattr(operation_jobs, "extend_job_lease", recording_extend_job_lease)

    assert operation_jobs.run_operation_jobs_for_operation(
        operation_id,
        worker_id="postgres-mirror-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.get(Job, job_id)

    assert heartbeat_workers
    assert set(heartbeat_workers) == {"postgres-mirror-worker"}
    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["saved_item_id"] == saved_item_id
    assert operation.result_json["saved_item"]["local_mirror"]["status"] == "succeeded"
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert job.locked_by == "postgres-mirror-worker"
    assert job.lease_expires_at is None


def test_postgres_local_mirror_export_job_reclaim_is_slot_agnostic(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _saved_item_id = _create_local_mirror_export_operation()
    claimed_at = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        claimed = claim_jobs(
            session,
            worker_id="worker-one",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT,),
            operation_id=operation_id,
            lease_seconds=60,
            now=claimed_at,
        )
        assert [job.id for job in claimed] == [job_id]
        session.commit()

    with get_session_factory()() as session:
        assert claim_jobs(
            session,
            worker_id="worker-two",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT,),
            operation_id=operation_id,
            now=claimed_at + timedelta(seconds=10),
        ) == []
        session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = claimed_at - timedelta(seconds=1)
        session.add(job)
        session.commit()

    with get_session_factory()() as session:
        reclaimed = claim_jobs(
            session,
            worker_id="worker-two",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT,),
            operation_id=operation_id,
            now=claimed_at + timedelta(seconds=1),
        )
        session.commit()

    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].locked_by == "worker-two"
    assert reclaimed[0].attempts == 2
    assert reclaimed[0].status == JOB_STATUS_RUNNING


def test_postgres_local_mirror_export_job_rejects_stale_worker_terminal_write(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _saved_item_id = _create_local_mirror_export_operation()
    claimed_at = datetime.now(timezone.utc)

    stale_session = get_session_factory()()
    try:
        stale_claimed = claim_jobs(
            stale_session,
            worker_id="worker-one",
            limit=1,
            job_types=(operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT,),
            operation_id=operation_id,
            lease_seconds=10,
            now=claimed_at,
        )
        assert [job.id for job in stale_claimed] == [job_id]
        stale_session.commit()

        with get_session_factory()() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.lease_expires_at = claimed_at - timedelta(seconds=1)
            session.add(job)
            session.commit()

        with get_session_factory()() as session:
            reclaimed = claim_jobs(
                session,
                worker_id="worker-two",
                limit=1,
                job_types=(operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT,),
                operation_id=operation_id,
                lease_seconds=30,
                now=claimed_at + timedelta(seconds=1),
            )
            assert [job.id for job in reclaimed] == [job_id]
            session.commit()

        with pytest.raises(JobLeaseError, match="different worker"):
            complete_job(stale_session, job_id, worker_id="worker-one", now=claimed_at + timedelta(seconds=2))
        stale_session.rollback()
    finally:
        stale_session.close()

    with get_session_factory()() as session:
        completed = complete_job(session, job_id, worker_id="worker-two", now=claimed_at + timedelta(seconds=2))
        assert completed.status == JOB_STATUS_SUCCEEDED
        session.commit()


def test_postgres_local_mirror_export_active_idempotency_and_manual_requeue(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, saved_item_id = _create_local_mirror_export_operation()

    with pytest.raises(IntegrityError):
        with get_session_factory()() as session:
            create_job(
                session,
                operation_id=operation_id,
                job_type=operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT,
                payload={"saved_item_id": saved_item_id},
                idempotency_key=f"operation:local_mirror_export:{saved_item_id}",
            )
            session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JOB_STATUS_SUCCEEDED
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        session.flush()
        requeued = manual_requeue_job(session, job_id, reason="manual mirror retry")
        session.commit()

    assert requeued.id != job_id
    assert requeued.status == JOB_STATUS_PENDING
    assert requeued.job_type == operation_jobs.JOB_TYPE_LOCAL_MIRROR_EXPORT
    assert requeued.idempotency_key == f"operation:local_mirror_export:{saved_item_id}"


def test_postgres_feed_item_insight_artifact_job_worker_dispatch_heartbeats_and_completes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    operation_id, job_id, feed_item_id = _create_feed_item_insight_artifacts_operation()
    expected_feed_item_id = feed_item_id
    heartbeat_workers: list[str] = []
    original_extend_job_lease = operation_jobs.extend_job_lease

    class Candidate:
        def __init__(self, item_id: int) -> None:
            self.feed_item_id = item_id

    def fake_build_feed_item_insight_artifacts(session, *, feed_item_id: int, mode: str, scope: str):
        assert feed_item_id == expected_feed_item_id
        assert mode == operation_jobs.OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING
        assert scope == "all"
        time.sleep(0.08)
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    def recording_extend_job_lease(*args, **kwargs):
        heartbeat_workers.append(kwargs["worker_id"])
        return original_extend_job_lease(*args, **kwargs)

    monkeypatch.setattr(operation_jobs, "list_missing_feed_item_insight_artifact_candidates", lambda session, source_id=None, limit=None: [Candidate(feed_item_id)])
    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fake_build_feed_item_insight_artifacts)
    monkeypatch.setattr(operation_jobs, "extend_job_lease", recording_extend_job_lease)

    assert operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="postgres-homepage-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.get(Job, job_id)

    assert heartbeat_workers
    assert set(heartbeat_workers) == {"postgres-homepage-worker"}
    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["artifact_rows_created"] == 4
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert job.locked_by == "postgres-homepage-worker"
    assert job.lease_expires_at is None


def test_postgres_feed_item_insight_artifacts_job_worker_dispatch_heartbeats_and_completes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    operation_id, job_id, feed_item_id = _create_feed_item_insight_artifact_operation()
    expected_feed_item_id = feed_item_id
    heartbeat_workers: list[str] = []
    original_extend_job_lease = operation_jobs.extend_job_lease

    def fake_build_feed_item_insight_artifacts(session, *, feed_item_id: int, mode: str, scope: str):
        assert feed_item_id == expected_feed_item_id
        assert mode == operation_jobs.OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING
        assert scope == f"feed_item:{feed_item_id}:missing"
        time.sleep(0.08)
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=f"feed_item:{feed_item_id}:missing",
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    def recording_extend_job_lease(*args, **kwargs):
        heartbeat_workers.append(kwargs["worker_id"])
        return original_extend_job_lease(*args, **kwargs)

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fake_build_feed_item_insight_artifacts)
    monkeypatch.setattr(operation_jobs, "extend_job_lease", recording_extend_job_lease)

    assert operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="postgres-artifact-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.get(Job, job_id)

    assert heartbeat_workers
    assert set(heartbeat_workers) == {"postgres-artifact-worker"}
    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["feed_item_id"] == feed_item_id
    assert operation.result_json["artifact_rows_created"] == 4
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert job.locked_by == "postgres-artifact-worker"
    assert job.lease_expires_at is None


def test_postgres_ai_lane_refills_slot_before_full_batch_finishes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade_head()
    with get_session_factory()() as session:
        update_ai_settings(
            session,
            AISettingsUpdateRequest.model_validate(
                {
                    "profiles": {
                        "general": {
                            "provider": "deepseek",
                            "enabled": True,
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-chat",
                            "api_key": "sk-postgres-ai-lane",
                            "artifact_concurrency": 2,
                        }
                    }
                }
            ),
        )
        ensure_default_channels(session)
        source = Source(
            name="Postgres AI Lane Sliding Source",
            kind="rss",
            url="https://postgres.example.com/ai-lane-sliding.xml",
            site_url="https://postgres.example.com",
            flow_policy="normal",
        )
        session.add(source)
        session.flush()
        feed_items: list[FeedItem] = []
        for index in range(3):
            feed_item = FeedItem(
                original_url=f"https://postgres.example.com/ai-lane-sliding/{index}",
                canonical_url=f"https://postgres.example.com/ai-lane-sliding/{index}",
                title=f"Postgres AI Lane Sliding {index}",
                summary_text="Postgres AI lane sliding summary.",
                content_text="Postgres AI lane sliding content.",
                content_text_source="feed_content",
                content_extraction_status="not_needed",
            )
            session.add(feed_item)
            session.flush()
            session.add(
                SourceItemOccurrence(
                    source_id=source.id,
                    feed_item_id=feed_item.id,
                    external_id=feed_item.canonical_url,
                    original_url=feed_item.original_url,
                    canonical_url=feed_item.canonical_url,
                    published_at=None,
                    raw_entry_json={"title": feed_item.title},
                    match_rule="created",
                    match_value=feed_item.canonical_url,
                )
            )
            feed_items.append(feed_item)
        operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
            operation_kind=operation_jobs.OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_GLOBAL,
            status="queued",
            scope_json={"scope": "global", "source_id": source.id, "limit": 3},
            progress_json=operation_jobs._initial_insight_progress("Postgres AI lane sliding", 3),
            result_json=operation_jobs._feed_item_insight_artifact_result(scope=f"source:{source.id}"),
            message="queued",
            runner_id="test",
        )
        session.add(operation)
        session.flush()
        operation_id = operation.id
        feed_item_ids = [feed_item.id for feed_item in feed_items]
        for feed_item in feed_items:
            operation_jobs.enqueue_feed_item_insight_artifact_build_job(
                session,
                operation,
                feed_item_id=feed_item.id,
                source_id=source.id,
            )
        session.commit()

    started_item_ids: list[int] = []
    started_lock = threading.Lock()
    second_started = threading.Event()
    third_started = threading.Event()
    release_second = threading.Event()

    def fake_build_feed_item_insight_artifacts(session, *, feed_item_id: int, mode: str, scope: str):
        del session, mode, scope
        with started_lock:
            started_item_ids.append(feed_item_id)
            started_count = len(started_item_ids)
        if started_count == 2:
            second_started.set()
            assert release_second.wait(timeout=10)
        if started_count == 3:
            assert second_started.is_set()
            assert not release_second.is_set()
            third_started.set()
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=f"source:{feed_item_ids.index(feed_item_id) if feed_item_id in feed_item_ids else feed_item_id}",
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fake_build_feed_item_insight_artifacts)

    lane_thread = threading.Thread(
        target=operation_jobs.run_ai_lane_jobs_until_idle,
        kwargs={
            "worker_id": "postgres-ai-lane-sliding-worker",
            "heartbeat_interval_seconds": 0,
            "poll_interval_seconds": 0.01,
            "max_concurrent_jobs": 2,
        },
    )
    lane_thread.start()
    assert second_started.wait(timeout=10)
    assert third_started.wait(timeout=10)
    release_second.set()
    lane_thread.join(timeout=10)
    assert not lane_thread.is_alive()

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation_id).order_by(Job.id.asc())))

    assert operation is not None
    assert operation.status == "completed"
    assert operation.progress_json["insight_completed_count"] == 3
    assert operation.result_json["processed_feed_item_count"] == 3
    assert operation.result_json["artifact_rows_created"] == 12
    assert [job.status for job in jobs] == [JOB_STATUS_SUCCEEDED, JOB_STATUS_SUCCEEDED, JOB_STATUS_SUCCEEDED]


def test_postgres_feed_item_artifact_builder_serializes_duplicate_build_missing(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    with get_session_factory()() as session:
        update_ai_settings(
            session,
            AISettingsUpdateRequest.model_validate(
                {
                    "profiles": {
                        "general": {
                            "provider": "deepseek",
                            "enabled": True,
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-chat",
                            "api_key": "sk-postgres-artifact",
                            "artifact_concurrency": 32,
                        }
                    }
                }
            ),
        )
        ensure_default_channels(session)
        source = Source(
            name="Postgres Duplicate Artifact Source",
            kind="rss",
            url="https://postgres.example.com/duplicate-feed.xml",
            site_url="https://postgres.example.com",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        feed_item = FeedItem(
            original_url="https://postgres.example.com/duplicate-artifact",
            canonical_url="https://postgres.example.com/duplicate-artifact",
            title="Postgres Duplicate Artifact",
            summary_text="Postgres duplicate artifact summary",
            content_text="Postgres duplicate artifact full content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=feed_item.id,
                external_id=feed_item.canonical_url,
                original_url=feed_item.original_url,
                canonical_url=feed_item.canonical_url,
                published_at=datetime(2026, 3, 28, tzinfo=timezone.utc),
                raw_entry_json={"title": feed_item.title},
                match_rule="created",
                match_value=feed_item.canonical_url,
            )
        )
        session.add(ItemState(feed_item_id=feed_item.id, status="unread"))
        session.commit()
        feed_item_id = feed_item.id

    request_started = threading.Event()
    request_count = 0
    request_count_lock = threading.Lock()

    def fake_request_ai_homepage_payload(config, request_payload):
        nonlocal request_count
        with request_count_lock:
            request_count += 1
        request_started.set()
        time.sleep(0.15)
        return insight_engine_artifact_builder.FeedItemInsightAIResponsePayload.model_validate(
            {
                "summary": "Serialized duplicate build summary.",
                "topics": ["software_systems"],
                "content_type": "analysis",
                "priority_score": 88,
                "priority_band": "high",
                "priority_reasons": [{"code": "specific_insight", "label": "Specific insight", "weight": 88}],
                "low_priority_bucket": False,
            }
        )

    monkeypatch.setattr(
        insight_engine_artifact_builder,
        "_request_ai_homepage_payload",
        fake_request_ai_homepage_payload,
    )

    stats: list[int] = []
    errors: list[BaseException] = []

    def run_builder() -> None:
        try:
            with get_session_factory()() as session:
                result = insight_engine_artifact_builder.build_feed_item_insight_artifacts_for_item(
                    session,
                    feed_item_id=feed_item_id,
                    mode="build_missing",
                    scope=f"feed_item:{feed_item_id}:missing",
                )
                session.commit()
                stats.append(result.artifact_rows_created)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run_builder)
    second = threading.Thread(target=run_builder)
    first.start()
    if not request_started.wait(timeout=15):
        first.join(timeout=5)
        assert False, f"AI request did not start; stats={stats!r}; errors={errors!r}"
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert request_count == 1
    assert sorted(stats) == [0, 4]
    with get_session_factory()() as session:
        artifacts = list(
            session.scalars(
                select(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == feed_item_id)
            )
        )
    assert len(artifacts) == 4


def test_postgres_saved_item_insight_job_worker_dispatch_heartbeats_and_completes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _upgrade_head()
    operation_id, job_id, saved_item_id = _create_saved_item_insight_operation()
    heartbeat_workers: list[str] = []
    original_extend_job_lease = operation_jobs.extend_job_lease

    class FakeSavedItemDetail:
        def model_dump(self, mode: str):
            return {"id": saved_item_id, "insight": {"status": "ready"}}

    def fake_rebuild_saved_item_insight(session, item_id: int, ai_route=None):
        assert item_id == saved_item_id
        assert ai_route == "general"
        time.sleep(0.08)
        return object()

    def recording_extend_job_lease(*args, **kwargs):
        heartbeat_workers.append(kwargs["worker_id"])
        return original_extend_job_lease(*args, **kwargs)

    monkeypatch.setattr(operation_jobs, "rebuild_saved_item_insight", fake_rebuild_saved_item_insight)
    monkeypatch.setattr(operation_jobs, "get_saved_item_detail", lambda session, item_id: FakeSavedItemDetail())
    monkeypatch.setattr(operation_jobs, "extend_job_lease", recording_extend_job_lease)

    assert operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="postgres-saved-insight-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        job = session.get(Job, job_id)

    assert heartbeat_workers
    assert set(heartbeat_workers) == {"postgres-saved-insight-worker"}
    assert operation is not None
    assert operation.status == "completed"
    assert operation.result_json["saved_item_id"] == saved_item_id
    assert operation.result_json["saved_item"]["insight"]["status"] == "ready"
    assert job is not None
    assert job.status == JOB_STATUS_SUCCEEDED
    assert job.locked_by == "postgres-saved-insight-worker"
    assert job.lease_expires_at is None


def test_postgres_feed_item_insight_artifact_job_reclaim_is_slot_agnostic(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_insight_artifact_operation()
    _assert_job_reclaim_is_slot_agnostic(
        operation_id,
        job_id,
        operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
    )


def test_postgres_feed_item_insight_artifacts_job_reclaim_is_slot_agnostic(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_insight_artifacts_operation()
    _assert_job_reclaim_is_slot_agnostic(
        operation_id,
        job_id,
        operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
    )


def test_postgres_saved_item_insight_job_reclaim_is_slot_agnostic(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _saved_item_id = _create_saved_item_insight_operation()
    _assert_job_reclaim_is_slot_agnostic(
        operation_id,
        job_id,
        operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT,
    )


def test_postgres_feed_item_insight_artifact_job_rejects_stale_worker_terminal_write(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_insight_artifact_operation()
    _assert_job_rejects_stale_worker_terminal_write(
        operation_id,
        job_id,
        operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
    )


def test_postgres_feed_item_insight_artifacts_job_rejects_stale_worker_terminal_write(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_insight_artifacts_operation()
    _assert_job_rejects_stale_worker_terminal_write(
        operation_id,
        job_id,
        operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
    )


def test_postgres_saved_item_insight_job_rejects_stale_worker_terminal_write(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _saved_item_id = _create_saved_item_insight_operation()
    _assert_job_rejects_stale_worker_terminal_write(
        operation_id,
        job_id,
        operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT,
    )


def test_postgres_feed_item_insight_artifact_active_idempotency_and_manual_requeue(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, feed_item_id = _create_feed_item_insight_artifact_operation()
    _assert_active_idempotency_and_manual_requeue(
        operation_id,
        job_id,
        job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
        payload={"feed_item_id": feed_item_id, "mode": "build_missing"},
        idempotency_key=(
            f"operation:feed_item_insight_artifact:{operation_id}:item:{feed_item_id}:mode:build_missing"
        ),
        queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
    )


def test_postgres_feed_item_insight_artifacts_active_idempotency_and_manual_requeue(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, _feed_item_id = _create_feed_item_insight_artifacts_operation()
    _assert_active_idempotency_and_manual_requeue(
        operation_id,
        job_id,
        job_type=operation_jobs.JOB_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
        payload={"scope": "global", "source_id": None, "limit": 1},
        idempotency_key="operation:feed_item_insight_artifacts:global",
        queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
    )


def test_postgres_source_sync_one_source_active_idempotency_key(postgres_database_url: str) -> None:
    _upgrade_head()
    source_id = _create_source("Idempotent Source Sync", "https://idempotent-sync.example.com/feed.xml")

    with get_session_factory()() as session:
        operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=0),
        )
        session.flush()
        job = session.scalar(select(Job).where(Job.operation_id == operation.id))
        assert job is not None
        assert job.job_type == operation_jobs.JOB_TYPE_SOURCE_SYNC_ONE_SOURCE
        assert job.queue_name == operation_jobs.SOURCE_SYNC_QUEUE_NAME
        assert job.idempotency_key == f"source_sync_one_source:{source_id}"
        operation_id = operation.id
        job_id = job.id
        session.commit()

    with pytest.raises(IntegrityError):
        with get_session_factory()() as session:
            operation = session.get(AppOperation, operation_id)
            assert operation is not None
            operation_jobs.enqueue_source_sync_job(
                session,
                operation,
                source_id=source_id,
                source_index=1,
                source_count=1,
            )
            session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JOB_STATUS_SUCCEEDED
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        session.flush()
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        replacement = operation_jobs.enqueue_source_sync_job(
            session,
            operation,
            source_id=source_id,
            source_index=1,
            source_count=1,
        )
        session.commit()

    assert replacement.id != job_id
    assert replacement.status == JOB_STATUS_PENDING
    assert replacement.idempotency_key == f"source_sync_one_source:{source_id}"


def test_postgres_source_sync_artifact_build_waits_for_source_sync_terminal(
    postgres_database_url: str,
) -> None:
    _upgrade_head()
    source_id = _create_source("Pipeline Gate Source Sync", "https://pipeline-gate.example.com/feed.xml")
    with get_session_factory()() as session:
        source_operation = operations_service.create_source_sync_operation(
            session,
            SourceSyncOperationCreate(kind="single", source_id=source_id, feed_item_insight_artifact_build_limit=None),
        )
        artifact_operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
            operation_kind=operation_jobs.OPERATION_KIND_SOURCE_SYNC_CREATED_FEED_ITEMS,
            status="queued",
            source_id=source_id,
            scope_json={
                "scope": "source_sync_created_feed_items",
                "source_sync_operation_id": source_operation.id,
                "source_id": source_id,
                "limit": None,
            },
            progress_json={
                "stage": "insight",
                "total": 1,
                "completed": 0,
                "success_count": 0,
                "error_count": 0,
                "stopped": False,
                "stop_requested": False,
                "current_insight_total": 1,
                "current_insight_completed": 1,
                "insight_total_count": 1,
                "insight_completed_count": 1,
            },
            result_json={
                "scope": f"source-sync:{source_operation.id}",
                "processed_feed_item_count": 1,
                "artifact_rows_created": 4,
                "failed_feed_item_count": 0,
                "stopped": False,
            },
        )
        session.add(artifact_operation)
        session.flush()
        artifact_job, _ = operation_jobs.enqueue_source_sync_artifact_build_job(
            session,
            artifact_operation,
            source_sync_operation_id=source_operation.id,
            feed_item_id=1001,
            source_id=source_id,
        )
        artifact_job.status = JOB_STATUS_SUCCEEDED
        artifact_job.finished_at = datetime.now(timezone.utc)
        session.add(artifact_job)
        source_operation_id = source_operation.id
        artifact_operation_id = artifact_operation.id
        session.commit()

    start_barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def sync_artifact_state() -> None:
        try:
            with get_session_factory()() as session:
                artifact_operation = session.get(AppOperation, artifact_operation_id)
                assert artifact_operation is not None
                start_barrier.wait(timeout=10)
                operation_jobs.sync_job_backed_operation_state(session, artifact_operation)
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=sync_artifact_state) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with get_session_factory()() as session:
        artifact_operation = session.get(AppOperation, artifact_operation_id)
        assert artifact_operation is not None
        assert artifact_operation.status == "running"
        assert artifact_operation.progress_json["stage"] == "waiting_source_sync"

        source_operation = session.get(AppOperation, source_operation_id)
        assert source_operation is not None
        source_operation.status = "completed"
        source_operation.completed_at = datetime.now(timezone.utc)
        session.add(source_operation)
        operation_jobs.sync_job_backed_operation_state(session, artifact_operation)
        session.commit()

    with get_session_factory()() as session:
        artifact_operation = session.get(AppOperation, artifact_operation_id)
        assert artifact_operation is not None
        assert artifact_operation.status == "completed"
        assert artifact_operation.result_json["processed_feed_item_count"] == 1


def test_postgres_saved_item_insight_active_idempotency_and_manual_requeue(postgres_database_url: str) -> None:
    _upgrade_head()
    operation_id, job_id, saved_item_id = _create_saved_item_insight_operation()
    _assert_active_idempotency_and_manual_requeue(
        operation_id,
        job_id,
        job_type=operation_jobs.JOB_TYPE_SAVED_ITEM_INSIGHT,
        payload={"saved_item_id": saved_item_id, "ai_route": "general"},
        idempotency_key=f"operation:saved_item_insight:{operation_id}:item:{saved_item_id}",
        queue_name=operation_jobs.FEED_ITEM_INSIGHT_ARTIFACT_QUEUE_NAME,
    )


def _upgrade_head() -> None:
    alembic_config = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(alembic_config, "head")


def _create_artifact_owners() -> tuple[int, int]:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://postgres.example.com/feed-item",
            canonical_url="https://postgres.example.com/feed-item",
            title="Postgres Feed Item",
            summary_text="Postgres feed item summary.",
            content_text="Postgres feed item content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        saved_item = SavedItem(
            origin_type="manual_url",
            ai_route="sensitive",
            title="Postgres Saved Item",
            canonical_url="https://postgres.example.com/saved-item",
            source_name="Manual",
        )
        session.add_all([feed_item, saved_item])
        session.commit()
        return feed_item.id, saved_item.id


def _insert_artifact(
    *,
    artifact_kind: str,
    feed_item_id: int | None = None,
    saved_item_id: int | None = None,
) -> None:
    with get_session_factory()() as session:
        session.add(
            InsightEngineArtifact(
                feed_item_id=feed_item_id,
                saved_item_id=saved_item_id,
                artifact_kind=artifact_kind,
                model_name="test/model",
                prompt_version="phase-1b-a-test",
                artifact_payload={"summary": "test"},
            )
        )
        session.commit()


def _create_operation() -> int:
    with get_session_factory()() as session:
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
        session.commit()
        return operation.id


def _create_source(name: str, url: str) -> int:
    with get_session_factory()() as session:
        source = Source(
            name=name,
            kind="rss",
            url=url,
            site_url=url.replace("/feed.xml", ""),
            is_active=True,
            ai_route="general",
        )
        session.add(source)
        session.commit()
        return source.id


def _homepage_read_model_lock_sql():
    return text("SELECT pg_advisory_xact_lock(hashtext(:lock_namespace), CAST(:lock_id AS integer))")


def _homepage_read_model_try_lock_sql():
    return text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_namespace), CAST(:lock_id AS integer))")


def _homepage_read_model_lock_params(feed_item_id: int) -> dict[str, object]:
    return {
        "lock_namespace": HOMEPAGE_READ_MODEL_LOCK_NAMESPACE,
        "lock_id": int(feed_item_id),
    }


def _wait_for_pending_advisory_lock() -> bool:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
    while datetime.now(timezone.utc) < deadline:
        with get_session_factory()() as session:
            pending_count = session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                    AND granted = false
                    """
                )
            )
        if pending_count:
            return True
        time.sleep(0.05)
    return False


def _create_homepage_read_model_item(slug: str = "1") -> int:
    with get_session_factory()() as session:
        ensure_default_channels(session)
        source = Source(
            name=f"Postgres Homepage RM Source {slug}",
            kind="rss",
            url=f"https://postgres-homepage-rm.example.com/{slug}/feed.xml",
            site_url="https://postgres-homepage-rm.example.com",
            folder_path="Postgres/HomepageRM",
            flow_policy="normal",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        feed_item = FeedItem(
            original_url=f"https://postgres-homepage-rm.example.com/posts/{slug}",
            canonical_url=f"https://postgres-homepage-rm.example.com/posts/{slug}",
            title=f"Postgres Homepage Read Model Item {slug}",
            published_at=datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc),
            summary_text="Postgres homepage read model summary.",
            content_text="Postgres homepage read model content.",
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
                raw_entry_json={"title": feed_item.title},
                match_rule="created",
                match_value=feed_item.canonical_url,
            )
        )
        session.add(
            InsightEngineArtifact(
                feed_item_id=feed_item.id,
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts.FEED_ITEM_ROUTING_ARTIFACT_KIND,
                model_name="test/model",
                prompt_version="postgres-homepage-rm-test",
                artifact_payload={
                    "schema_version": "feed_item_routing_v1",
                    "primary_channel_id": "ai_agents_frontier",
                    "secondary_channels": [],
                    "reading_depth": "medium",
                    "flow_lane": "main",
                    "confidence": 0.9,
                    "reason_codes": ["source_channel_tendency_match"],
                    "promotion_reason_codes": [],
                    "isolation_reason_codes": [],
                    "source_policy_applied": "normal",
                    "notes": "Postgres homepage read model concurrency test",
                    "source": "insight_engine_artifact",
                },
            )
        )
        session.commit()
        return feed_item.id


def _install_source_sync_transport(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, httpx.Response],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = mapping[str(request.url)]
        response.request = request
        return response

    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(source_sync, "create_http_client", fake_create_http_client)


def _rss_feed(guid: str, title: str, link: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Postgres Feed</title>
    <item>
      <guid>{guid}</guid>
      <title>{title}</title>
      <link>{link}</link>
      <description>{"Long summary. " * 20}</description>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def _create_jobs(operation_id: int, *, source_ids: list[int]) -> list[int]:
    with get_session_factory()() as session:
        jobs = [
            create_job(
                session,
                operation_id=operation_id,
                job_type="test_job",
                payload={"source_id": source_id},
            )
            for source_id in source_ids
        ]
        session.commit()
        return [job.id for job in jobs]


def _create_feed_item_extract_operation() -> tuple[int, int, int]:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://postgres.example.com/feed-item-extract",
            canonical_url="https://postgres.example.com/feed-item-extract",
            title="Postgres Feed Item Extract",
            summary_text="Postgres feed item extract summary.",
            content_text="Postgres feed item extract content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_FEED_ITEM_EXTRACT,
            operation_kind=operation_jobs.OPERATION_KIND_FEED_ITEM_MANUAL_EXTRACT,
            status="queued",
            scope_json={"feed_item_id": feed_item.id},
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
        job = operation_jobs.enqueue_feed_item_extract_job(session, operation, feed_item_id=feed_item.id)
        session.commit()
        return operation.id, job.id, feed_item.id


def _create_local_mirror_export_operation() -> tuple[int, int, int]:
    with get_session_factory()() as session:
        saved_item = SavedItem(
            origin_type="manual_url",
            ai_route="sensitive",
            title="Postgres Mirror Saved Item",
            canonical_url="https://postgres.example.com/mirror-saved-item",
            source_name="Manual",
        )
        session.add(saved_item)
        session.flush()
        operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_LOCAL_MIRROR_EXPORT,
            operation_kind=operation_jobs.OPERATION_KIND_LOCAL_MIRROR_MANUAL_REEXPORT,
            status="queued",
            scope_json={"saved_item_id": saved_item.id},
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
        job = operation_jobs.enqueue_local_mirror_export_job(session, operation, saved_item_id=saved_item.id)
        session.commit()
        return operation.id, job.id, saved_item.id


def _create_feed_item_insight_artifacts_operation() -> tuple[int, int, int]:
    with get_session_factory()() as session:
        source = Source(
            name="Postgres Feed Item Insight Source",
            url="https://postgres.example.com/feed.xml",
        )
        session.add(source)
        session.flush()
        feed_item = FeedItem(
            original_url="https://postgres.example.com/feed-item-insight-artifacts",
            canonical_url="https://postgres.example.com/feed-item-insight-artifacts",
            title="Postgres Feed Item Insight Artifacts",
            summary_text="Postgres feed item insight artifacts summary.",
            content_text="Postgres feed item insight artifacts content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                feed_item_id=feed_item.id,
                external_id=feed_item.canonical_url,
                original_url=feed_item.original_url,
                canonical_url=feed_item.canonical_url,
                published_at=None,
                raw_entry_json={"title": feed_item.title},
                match_rule="created",
                match_value=feed_item.canonical_url,
            )
        )
        session.flush()
        operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACTS,
            operation_kind=operation_jobs.OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_GLOBAL,
            status="queued",
            scope_json={"scope": "global", "source_id": None, "limit": 1},
            progress_json={
                "stage": "insight",
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
        job = operation_jobs.enqueue_feed_item_insight_artifacts_job(
            session,
            operation,
            scope="global",
            source_id=None,
            limit=1,
        )
        session.commit()
        return operation.id, job.id, feed_item.id


def _create_feed_item_insight_artifact_operation() -> tuple[int, int, int]:
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://postgres.example.com/feed-item-artifacts",
            canonical_url="https://postgres.example.com/feed-item-artifacts",
            title="Postgres Feed Item Artifacts",
            summary_text="Postgres feed item artifacts summary.",
            content_text="Postgres feed item artifacts content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add(feed_item)
        session.flush()
        operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
            operation_kind=operation_jobs.OPERATION_KIND_FEED_ITEM_INSIGHT_ARTIFACTS_BUILD_MISSING,
            status="queued",
            scope_json={"feed_item_id": feed_item.id, "mode": "build_missing"},
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
        job = operation_jobs.enqueue_feed_item_insight_artifact_job(
            session,
            operation,
            feed_item_id=feed_item.id,
            mode="build_missing",
        )
        session.commit()
        return operation.id, job.id, feed_item.id


def _create_saved_item_insight_operation() -> tuple[int, int, int]:
    with get_session_factory()() as session:
        saved_item = SavedItem(
            origin_type="manual_url",
            ai_route="general",
            title="Postgres Saved Item Insight",
            canonical_url="https://postgres.example.com/saved-item-insight",
            source_name="Manual",
        )
        session.add(saved_item)
        session.flush()
        operation = AppOperation(
            operation_type=operation_jobs.OPERATION_TYPE_SAVED_ITEM_INSIGHT,
            operation_kind=operation_jobs.OPERATION_KIND_SAVED_ITEM_INSIGHT_REBUILD,
            status="queued",
            scope_json={"saved_item_id": saved_item.id, "ai_route": "general"},
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
        job = operation_jobs.enqueue_saved_item_insight_job(
            session,
            operation,
            saved_item_id=saved_item.id,
            ai_route="general",
        )
        session.commit()
        return operation.id, job.id, saved_item.id


def _assert_job_reclaim_is_slot_agnostic(operation_id: int, job_id: int, job_type: str) -> None:
    claimed_at = datetime.now(timezone.utc)

    with get_session_factory()() as session:
        claimed = claim_jobs(
            session,
            worker_id="worker-one",
            limit=1,
            job_types=(job_type,),
            operation_id=operation_id,
            lease_seconds=60,
            now=claimed_at,
        )
        assert [job.id for job in claimed] == [job_id]
        session.commit()

    with get_session_factory()() as session:
        assert claim_jobs(
            session,
            worker_id="worker-two",
            limit=1,
            job_types=(job_type,),
            operation_id=operation_id,
            now=claimed_at + timedelta(seconds=10),
        ) == []
        session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = claimed_at - timedelta(seconds=1)
        session.add(job)
        session.commit()

    with get_session_factory()() as session:
        reclaimed = claim_jobs(
            session,
            worker_id="worker-two",
            limit=1,
            job_types=(job_type,),
            operation_id=operation_id,
            now=claimed_at + timedelta(seconds=1),
        )
        session.commit()

    assert [job.id for job in reclaimed] == [job_id]
    assert reclaimed[0].locked_by == "worker-two"
    assert reclaimed[0].attempts == 2
    assert reclaimed[0].status == JOB_STATUS_RUNNING


def _assert_job_rejects_stale_worker_terminal_write(operation_id: int, job_id: int, job_type: str) -> None:
    claimed_at = datetime.now(timezone.utc)

    stale_session = get_session_factory()()
    try:
        stale_claimed = claim_jobs(
            stale_session,
            worker_id="worker-one",
            limit=1,
            job_types=(job_type,),
            operation_id=operation_id,
            lease_seconds=10,
            now=claimed_at,
        )
        assert [job.id for job in stale_claimed] == [job_id]
        stale_session.commit()

        with get_session_factory()() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.lease_expires_at = claimed_at - timedelta(seconds=1)
            session.add(job)
            session.commit()

        with get_session_factory()() as session:
            reclaimed = claim_jobs(
                session,
                worker_id="worker-two",
                limit=1,
                job_types=(job_type,),
                operation_id=operation_id,
                lease_seconds=30,
                now=claimed_at + timedelta(seconds=1),
            )
            assert [job.id for job in reclaimed] == [job_id]
            session.commit()

        with pytest.raises(JobLeaseError, match="different worker"):
            complete_job(stale_session, job_id, worker_id="worker-one", now=claimed_at + timedelta(seconds=2))
        stale_session.rollback()
    finally:
        stale_session.close()

    with get_session_factory()() as session:
        completed = complete_job(session, job_id, worker_id="worker-two", now=claimed_at + timedelta(seconds=2))
        assert completed.status == JOB_STATUS_SUCCEEDED
        session.commit()


def _assert_active_idempotency_and_manual_requeue(
    operation_id: int,
    job_id: int,
    *,
    job_type: str,
    payload: dict[str, object],
    idempotency_key: str,
    queue_name: str = "default",
) -> None:
    with pytest.raises(IntegrityError):
        with get_session_factory()() as session:
            create_job(
                session,
                operation_id=operation_id,
                job_type=job_type,
                payload=payload,
                queue_name=queue_name,
                idempotency_key=idempotency_key,
            )
            session.commit()

    with get_session_factory()() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = JOB_STATUS_SUCCEEDED
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        session.flush()
        requeued = manual_requeue_job(session, job_id, reason="manual operation retry")
        session.commit()

    assert requeued.id != job_id
    assert requeued.status == JOB_STATUS_PENDING
    assert requeued.job_type == job_type
    assert requeued.idempotency_key == idempotency_key
