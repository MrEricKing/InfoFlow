from __future__ import annotations

from pathlib import Path
import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_engine, reset_session_state
from app.main import app
from app.services import operation_jobs, operations as operations_service
from app.services.capture_tools.models import Crawl4AICaptureResult, SingleFileArchiveResult


@pytest.fixture(autouse=True)
def fast_operation_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    original_run_operation_background = operations_service.run_operation_background

    def run_operation_background_without_retry_wait(operation_id: int, *args, **kwargs) -> None:
        if kwargs.get("failure_backoff_seconds") is None:
            kwargs["failure_backoff_seconds"] = 0
        original_run_operation_background(operation_id, *args, **kwargs)

    monkeypatch.setattr(operations_service, "run_operation_background", run_operation_background_without_retry_wait)


@pytest.fixture()
def sqlite_database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path.joinpath('test.db').as_posix()}"


@pytest.fixture()
def client(tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Keep API tests isolated without paying the per-test file SQLite DDL cost.
    # Migration tests still use sqlite_database_url so Alembic runs against a real file.
    operation_jobs.stop_operation_job_worker(timeout_seconds=1)
    database_url = (
        f"sqlite:///{tmp_path.joinpath('test.db').as_posix()}"
        if request.node.get_closest_marker("file_sqlite")
        else "sqlite://"
    )
    os.environ["DATABASE_URL"] = database_url
    os.environ["KNOWLEDGE_BASE_ROOT"] = str(tmp_path / "KnowledgeBase")
    os.environ["INFOFLOW_SETTINGS_ENCRYPTION_KEY"] = "IcMtG9uOh01Ha0HCFhCqsXIEmZOh4TT73YvCBWX_iuw="
    monkeypatch.delenv("RSSHUB_BASE_URL", raising=False)
    monkeypatch.setattr("app.services.knowledge_capture.archive_page_with_singlefile", _fake_singlefile_archive_success)
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", _fake_crawl4ai_capture_success)
    monkeypatch.setattr("app.services.knowledge_uploads.capture_uploaded_html_markdown", _fake_crawl4ai_uploaded_html_success)
    reset_settings_cache()
    reset_session_state()

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestClient(app) as test_client:
        yield test_client

    operation_jobs.stop_operation_job_worker(timeout_seconds=1)
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    os.environ.pop("INFOFLOW_SETTINGS_ENCRYPTION_KEY", None)
    reset_settings_cache()
    reset_session_state()


def _fake_singlefile_archive_success(url: str, output_path: Path | None = None) -> SingleFileArchiveResult:
    html_bytes = f"<!doctype html><html><body>Archived {url}</body></html>".encode("utf-8")
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(html_bytes)
    return SingleFileArchiveResult(
        succeeded=True,
        html_bytes=html_bytes,
        exit_code=0,
        stdout_tail="saved",
        stderr_tail=None,
        error_summary=None,
        tool_args_json={
            "url": url,
            "integration_mode": "mv3_extension",
            "extension_id": "jkeljiefjnokjkioahopjglagkbocpbh",
            "browser_executable_path": None,
            "profile": "singlefile_extension_archive_v1",
            "output_path": str(output_path) if output_path is not None else None,
            "timeout_seconds": 120,
        },
    )


def _fake_crawl4ai_capture_success(url: str) -> Crawl4AICaptureResult:
    return Crawl4AICaptureResult(
        raw_markdown=f"# Raw\n\nArchived markdown from {url}\n",
        fit_markdown=f"# Fit\n\nArchived fit markdown from {url}\n",
    )


def _fake_crawl4ai_uploaded_html_success(html: str, *, source_name: str | None = None) -> Crawl4AICaptureResult:
    title = source_name or "Uploaded HTML"
    return Crawl4AICaptureResult(
        raw_markdown=f"# {title}\n\nUploaded raw markdown\n",
        fit_markdown=f"# {title}\n\nUploaded fit markdown\n",
        page_title=title,
        raw_html=html,
    )
