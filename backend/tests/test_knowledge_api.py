from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import (
    AppOperation,
    InsightEngineArtifact,
    FeedItemBehaviorEvent,
    Job,
    SavedItem,
    SavedItemAsset,
    SavedItemCaptureRun,
    SavedItemCaptureToolCall,
    SavedItemCuratedInsight,
    SavedItemCuratedInsightEvent,
    SavedSnapshot,
    SavedSnapshotAssetRef,
    Source,
)
from app.db.session import get_session_factory
from app.services import ai_provider, operation_jobs, operations as operations_service, source_sync
from app.services.capture_tools.models import (
    CaptureAssetPayload,
    CaptureToolDiagnostic,
    Crawl4AICaptureResult,
    SingleFileArchiveResult,
)
from app.services.knowledge_constants import (
    CANONICAL_SNAPSHOT_KIND,
    CAPTURE_STATUS_FAILED,
    CAPTURE_STATUS_PENDING,
    CAPTURE_STATUS_SUCCEEDED,
    CAPTURE_TOOL_STATUS_FAILED,
    CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
    CAPTURE_TRIGGER_FEED_SAVED_RETRY,
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    DEFAULT_CAPTURE_TARGETS,
    MANUAL_URL_CAPTURE_INPUT_KIND,
    ORIGINAL_PAGE_SNAPSHOT_KIND,
    PAGE_IMAGE_ASSET_KIND,
    SINGLEFILE_HTML_SNAPSHOT_KIND,
    UPLOADED_MARKDOWN_SNAPSHOT_KIND,
    UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
)
from app.services.knowledge_capture import create_capture_tool_call, finalize_capture_run
from app.services.knowledge_snapshot_assets import (
    build_archive_snapshot_download_filename,
    build_content_disposition_header,
)
from app.services.saved_item_insights import SAVED_ITEM_INSIGHT_ARTIFACT_KIND, select_saved_item_insight_input


RSS_SAMPLE_SINGLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Single Feed</title>
    <item>
      <guid>https://single.example.com/posts/1</guid>
      <title>Single Story</title>
      <link>https://single.example.com/posts/1</link>
      <description>Single summary</description>
      <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">Single content</content:encoded>
      <pubDate>Sat, 28 Mar 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


class _StatusOverrideResponse:
    def __init__(self, response, status_code: int) -> None:
        self._response = response
        self.status_code = status_code

    def __getattr__(self, name: str):
        return getattr(self._response, name)


def _saved_item_detail_after_operation(client, operation_response, *, success_status: int):
    if operation_response.status_code != 202:
        return operation_response
    operation_id = operation_response.json()["id"]
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)
    scope = operation_response.json().get("scope") or {}
    saved_item_id = scope.get("saved_item_id")
    assert saved_item_id is not None
    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    return _StatusOverrideResponse(detail_response, success_status)


def _post_manual_url_capture(client, **kwargs):
    return _saved_item_detail_after_operation(
        client,
        client.post("/api/operations/captures/manual-url", **kwargs),
        success_status=201,
    )


def _post_uploaded_material_capture(client, **kwargs):
    return _saved_item_detail_after_operation(
        client,
        client.post("/api/operations/captures/upload", **kwargs),
        success_status=201,
    )


def _post_saved_item_manual_import(client, saved_item_id: int, **kwargs):
    return _saved_item_detail_after_operation(
        client,
        client.post(f"/api/operations/saved-items/{saved_item_id}/capture-import", **kwargs),
        success_status=200,
    )


def _post_retry_capture(client, saved_item_id: int):
    return _saved_item_detail_after_operation(
        client,
        client.post(f"/api/operations/saved-items/{saved_item_id}/capture-runs"),
        success_status=201,
    )


def _post_local_mirror_export(client, saved_item_id: int):
    return _saved_item_detail_after_operation(
        client,
        client.post(f"/api/operations/saved-items/{saved_item_id}/local-mirror-exports"),
        success_status=201,
    )


def _post_saved_item_insight_rebuild(client, saved_item_id: int, **kwargs):
    operation_response = client.post(f"/api/operations/saved-items/{saved_item_id}/insight-rebuild", **kwargs)
    if operation_response.status_code == 202:
        operation_jobs.run_ai_lane_jobs_until_idle(
            worker_id="knowledge-api-test-ai-lane",
            heartbeat_interval_seconds=0,
            poll_interval_seconds=0.01,
            failure_backoff_seconds=0,
        )
    return _saved_item_detail_after_operation(
        client,
        operation_response,
        success_status=200,
    )


def test_upload_markdown_creates_source_asset_markdown_snapshot_and_rejects_capture_retry(client) -> None:
    response = _post_uploaded_material_capture(
        client,
        data={
            "material_type": "markdown",
            "title": "",
            "user_note": "  saved upload note  ",
            "ai_route": "general",
        },
        files={"file": ("note.md", b"# Upload Title\n\nImportant upload body.\n", "text/markdown")},
    )

    assert response.status_code == 201
    payload = response.json()
    saved_item_id = payload["id"]
    assert payload["origin_type"] == "markdown_upload"
    assert payload["ai_route"] == "general"
    assert payload["title"] == "Upload Title"
    assert payload["canonical_url"] is None
    assert payload["original_url"] is None
    assert payload["source_url"] is None
    assert payload["source_site_url"] is None
    assert payload["original_filename"] == "note.md"
    assert payload["mime_type"] == "text/markdown"
    assert payload["processing_status"] == "succeeded"
    assert payload["processing_error"] is None

    source_artifact = payload["source_material_artifact"]
    assert source_artifact["artifact_kind"] == UPLOADED_SOURCE_MATERIAL_ASSET_KIND
    assert source_artifact["status"] == "available"
    source_response = client.get(source_artifact["preview_url"])
    assert source_response.status_code == 200
    assert source_response.content == b"# Upload Title\n\nImportant upload body.\n"

    artifacts = {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}
    assert set(artifacts) == {UPLOADED_SOURCE_MATERIAL_ASSET_KIND, UPLOADED_MARKDOWN_SNAPSHOT_KIND}
    assert artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    markdown_response = client.get(artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["preview_url"])
    assert markdown_response.status_code == 200
    assert markdown_response.text == "# Upload Title\n\nImportant upload body.\n"

    retry_response = _post_retry_capture(client, saved_item_id)
    assert retry_response.status_code == 400
    assert "web capture pipeline" in retry_response.json()["detail"]

    session_factory = get_session_factory()
    with session_factory() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.canonical_url is None
        assert saved_item.source_asset_id is not None
        source_asset = session.get(SavedItemAsset, saved_item.source_asset_id)
        assert source_asset is not None
        assert source_asset.asset_kind == UPLOADED_SOURCE_MATERIAL_ASSET_KIND
        uploaded_snapshot = session.scalar(
            select(SavedSnapshot).where(
                SavedSnapshot.saved_item_id == saved_item_id,
                SavedSnapshot.snapshot_kind == UPLOADED_MARKDOWN_SNAPSHOT_KIND,
            )
        )
        assert uploaded_snapshot is not None
        selected_input = select_saved_item_insight_input(session, saved_item)
        assert selected_input.input_snapshot_kind == UPLOADED_MARKDOWN_SNAPSHOT_KIND
        assert "Important upload body" in selected_input.content_text

    mirror_paths = payload["local_mirror"]["paths"]
    assert mirror_paths["source_material"].endswith("note_source.md")
    assert mirror_paths["uploaded_markdown"].endswith("Upload Title_uploaded.md")


@pytest.mark.parametrize(
    ("material_type", "filename", "content_type", "expected_origin"),
    [
        ("html", "page.html", "text/html", "html_upload"),
        ("singlefile_html", "singlefile.html", "text/html", "singlefile_html_upload"),
    ],
)
def test_upload_html_materials_use_crawl4ai_markdown_derivation(
    client,
    material_type: str,
    filename: str,
    content_type: str,
    expected_origin: str,
) -> None:
    response = _post_uploaded_material_capture(
        client,
        data={"material_type": material_type},
        files={"file": (filename, b"<!doctype html><html><title>HTML Title</title><body>Body</body></html>", content_type)},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["origin_type"] == expected_origin
    assert payload["canonical_url"] is None
    assert payload["processing_status"] == "succeeded"
    assert payload["title"] == filename

    artifacts = {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}
    markdown_response = client.get(artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["preview_url"])
    assert markdown_response.status_code == 200
    assert "Uploaded fit markdown" in markdown_response.text

    source_response = client.get(artifacts[UPLOADED_SOURCE_MATERIAL_ASSET_KIND]["preview_url"])
    assert source_response.status_code == 200
    assert "Content-Security-Policy" in source_response.headers
    assert "sandbox" in source_response.headers["Content-Security-Policy"]


def test_upload_html_processing_failure_preserves_source_asset_without_markdown(client, monkeypatch) -> None:
    def fail_upload_capture(html: str, *, source_name: str | None = None) -> Crawl4AICaptureResult:
        raise RuntimeError("crawl4ai failed for uploaded html")

    monkeypatch.setattr("app.services.knowledge_uploads.capture_uploaded_html_markdown", fail_upload_capture)

    response = _post_uploaded_material_capture(
        client,
        data={"material_type": "html"},
        files={"file": ("broken.html", b"<!doctype html><html><body>Broken</body></html>", "text/html")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["processing_status"] == "failed"
    assert "crawl4ai failed" in payload["processing_error"]
    assert payload["source_material_artifact"]["status"] == "available"
    artifacts = {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}
    assert artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["status"] == "unavailable"


def test_upload_singlefile_antibot_page_preserves_source_asset_without_markdown(client) -> None:
    response = _post_uploaded_material_capture(
        client,
        data={"material_type": "singlefile_html"},
        files={"file": ("zhihu-singlefile.html", _zhihu_antibot_singlefile_html().encode("utf-8"), "text/html")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["origin_type"] == "singlefile_html_upload"
    assert payload["processing_status"] == "failed"
    assert "anti-bot" in payload["processing_error"]
    assert payload["source_material_artifact"]["status"] == "available"
    artifacts = {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}
    assert artifacts[UPLOADED_SOURCE_MATERIAL_ASSET_KIND]["status"] == "available"
    assert artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["status"] == "unavailable"


def test_manual_import_attaches_uploaded_singlefile_to_existing_saved_item(client, monkeypatch) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/article", "title": "Manual Article"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    import_response = _post_saved_item_manual_import(
        client,
        saved_item_id,
        data={"material_type": "singlefile_html"},
        files={
            "file": (
                "external-singlefile.html",
                b"<!doctype html><html><title>External Title</title><body><main>Readable external SingleFile body with enough detail for cleaning.</main></body></html>",
                "text/html",
            )
        },
    )

    assert import_response.status_code == 200
    payload = import_response.json()
    assert payload["id"] == saved_item_id
    assert payload["origin_type"] == "manual_url"
    assert payload["title"] == "Manual Article"
    assert payload["canonical_url"] == "https://manual.example.com/article"
    assert payload["original_filename"] == "external-singlefile.html"
    assert payload["processing_status"] == "succeeded"
    assert payload["source_material_artifact"]["status"] == "available"

    artifacts = {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}
    assert artifacts[UPLOADED_SOURCE_MATERIAL_ASSET_KIND]["status"] == "available"
    assert artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        selected_input = select_saved_item_insight_input(session, saved_item)
    assert selected_input.input_snapshot_kind == UPLOADED_MARKDOWN_SNAPSHOT_KIND


def test_manual_import_failure_preserves_previous_successful_material(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/reimport", "title": "Manual Reimport"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    first_import_response = _post_saved_item_manual_import(
        client,
        saved_item_id,
        data={"material_type": "markdown", "title": "First Imported", "user_note": "first note"},
        files={
            "file": (
                "first.md",
                b"# First Imported\n\nUseful imported markdown body with enough detail to keep.",
                "text/markdown",
            )
        },
    )
    assert first_import_response.status_code == 200
    first_payload = first_import_response.json()
    assert first_payload["processing_status"] == "succeeded"
    first_source_response = client.get(first_payload["source_material_artifact"]["preview_url"])
    assert first_source_response.status_code == 200
    assert b"Useful imported markdown body" in first_source_response.content
    first_artifacts = {artifact["artifact_kind"]: artifact for artifact in first_payload["archive_artifacts"]}
    first_markdown_response = client.get(first_artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["preview_url"])
    assert first_markdown_response.status_code == 200
    assert "Useful imported markdown body" in first_markdown_response.text

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        first_source_asset_id = saved_item.source_asset_id
        assert first_source_asset_id is not None

    failed_import_response = _post_saved_item_manual_import(
        client,
        saved_item_id,
        data={"material_type": "markdown", "title": "Broken Replacement", "user_note": "broken note"},
        files={"file": ("broken.md", b"# No\n", "text/markdown")},
    )

    assert failed_import_response.status_code == 200
    failed_payload = failed_import_response.json()
    assert failed_payload["processing_status"] == "failed"
    assert "does not contain enough readable text" in failed_payload["processing_error"]
    assert failed_payload["title"] == "First Imported"
    assert failed_payload["user_note"] == "first note"
    assert failed_payload["original_filename"] == "first.md"
    assert failed_payload["source_material_artifact"]["status"] == "available"
    failed_source_response = client.get(failed_payload["source_material_artifact"]["preview_url"])
    assert failed_source_response.status_code == 200
    assert failed_source_response.content == first_source_response.content
    failed_artifacts = {artifact["artifact_kind"]: artifact for artifact in failed_payload["archive_artifacts"]}
    failed_markdown_response = client.get(failed_artifacts[UPLOADED_MARKDOWN_SNAPSHOT_KIND]["preview_url"])
    assert failed_markdown_response.status_code == 200
    assert failed_markdown_response.text == first_markdown_response.text

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.source_asset_id == first_source_asset_id
        source_assets = list(
            session.scalars(
                select(SavedItemAsset)
                .where(
                    SavedItemAsset.saved_item_id == saved_item_id,
                    SavedItemAsset.asset_kind == UPLOADED_SOURCE_MATERIAL_ASSET_KIND,
                )
                .order_by(SavedItemAsset.id)
            )
        )
        assert len(source_assets) == 2
        assert source_assets[-1].metadata_json["pending_manual_import"] is True
        selected_input = select_saved_item_insight_input(session, saved_item)
        assert selected_input.input_snapshot_kind == UPLOADED_MARKDOWN_SNAPSHOT_KIND
        assert "Useful imported markdown body" in selected_input.content_text


def test_upload_rejects_invalid_material_without_creating_saved_item(client) -> None:
    response = _post_uploaded_material_capture(
        client,
        data={"material_type": "markdown"},
        files={"file": ("note.md", "闈?UTF-8".encode("gbk"), "text/markdown")},
    )

    assert response.status_code == 400
    session_factory = get_session_factory()
    with session_factory() as session:
        assert session.scalar(select(func.count(SavedItem.id))) == 0


def test_capture_manual_url_creates_null_feed_saved_item_initial_payload_and_succeeded_run(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_capture.archive_page_with_singlefile",
        _singlefile_archive_with_title("Captured Manual Page"),
    )

    response = _post_manual_url_capture(
        client,
        json={
            "url": " HTTPS://Example.COM/articles/1?ref=clip#section ",
            "title": "  Manual Story  ",
            "user_note": "  Keep this for non-RSS capture  ",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    saved_item_id = payload["id"]
    latest_run_id = payload["capture"]["latest_run_id"]
    assert payload["feed_item_id"] is None
    assert payload["origin_type"] == "manual_url"
    assert payload["ai_route"] == "sensitive"
    assert payload["title"] == "Manual Story"
    assert payload["canonical_url"] == "https://example.com/articles/1?ref=clip"
    assert payload["original_url"] == "https://example.com/articles/1?ref=clip"
    assert payload["source_name"] == "example.com"
    assert payload["source_kind"] is None
    assert payload["source_url"] == "https://example.com/"
    assert payload["source_site_url"] == "https://example.com/"
    assert payload["source_folder_path"] is None
    assert payload["published_at"] is None
    assert payload["user_note"] == "Keep this for non-RSS capture"
    assert payload["state"]["status"] == "saved"
    assert payload["snapshots"] == {
        "has_canonical_snapshot": False,
        "has_original_page_snapshot": False,
        "has_singlefile_html": True,
        "has_crawl4ai_raw_markdown": True,
        "has_crawl4ai_fit_markdown": True,
        "has_crawl4ai_fit_image_reinserted_markdown": True,
        "image_asset_count": 0,
    }
    assert payload["capture"]["status"] == "succeeded"
    assert payload["capture"]["latest_run_id"] == latest_run_id
    assert payload["capture"]["latest_error_summary"] is None
    assert payload["capture"]["latest_finished_at"] is not None
    assert payload["capture"]["retryable"] is False
    assert payload["capture"]["title_metadata"] == {
        "captured_at": payload["capture"]["title_metadata"]["captured_at"],
        "captured_page_title": "Captured Manual Page",
        "singlefile_title": "Captured Manual Page",
        "crawl4ai_title": None,
        "feed_title": None,
        "title_source": "user_title",
        "user_title_present": True,
        "title_updated": False,
        "saved_item_title_before": "Manual Story",
        "saved_item_title_after": "Manual Story",
    }
    assert payload["local_mirror"]["status"] == "succeeded"
    assert payload["local_mirror"]["source_capture_run_id"] == latest_run_id
    assert len(payload["capture_tool_calls"]) == 2
    assert payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert payload["capture_tool_calls"][0]["tool_version"] == "1.22.98"
    assert payload["capture_tool_calls"][0]["exit_code"] == 0
    assert payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"
    assert payload["capture_tool_calls"][1]["tool_version"] == "0.8.6"
    assert payload["capture_tool_calls"][1]["exit_code"] == 0
    assert payload["content_html"] is None
    archive_artifacts = _archive_artifacts_by_kind(payload)
    assert set(archive_artifacts) == {
        SINGLEFILE_HTML_SNAPSHOT_KIND,
        CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
        CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
        CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    }
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["asset_key"]
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["preview_url"].startswith("/api/knowledge/assets/")
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["download_url"].endswith("?download=true")
    assert archive_artifacts[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND]["snapshot_id"] is not None
    assert archive_artifacts[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND]["mime_type"] == "text/markdown"
    assert archive_artifacts[CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND]["snapshot_id"] is not None
    assert archive_artifacts[CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND]["snapshot_id"] is not None

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.feed_item_id is None
        assert saved_item.origin_type == "manual_url"
        assert saved_item.ai_route == "sensitive"
        assert saved_item.canonical_url == "https://example.com/articles/1?ref=clip"

        snapshots = session.scalars(select(SavedSnapshot).where(SavedSnapshot.saved_item_id == saved_item_id)).all()
        snapshots_by_kind = {snapshot.snapshot_kind: snapshot for snapshot in snapshots}
        assert set(snapshots_by_kind) == {
            MANUAL_URL_CAPTURE_INPUT_KIND,
            CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
            CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
            CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
            CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
        }
        input_snapshot = snapshots_by_kind[MANUAL_URL_CAPTURE_INPUT_KIND]
        assert input_snapshot.mime_type == "application/json"
        assert input_snapshot.capture_run_id is None
        snapshot_body = json.loads(input_snapshot.body)
        assert snapshot_body["capture_kind"] == MANUAL_URL_CAPTURE_INPUT_KIND
        assert snapshot_body["ai_route"] == "sensitive"
        assert snapshot_body["requested_url"] == "HTTPS://Example.COM/articles/1?ref=clip#section"
        assert snapshot_body["normalized_url"] == "https://example.com/articles/1?ref=clip"
        assert snapshot_body["title"] == "Manual Story"
        assert snapshot_body["user_note"] == "Keep this for non-RSS capture"
        metadata_body = json.loads(snapshots_by_kind[CAPTURE_PAGE_METADATA_SNAPSHOT_KIND].body)
        assert metadata_body["singlefile_title"] == "Captured Manual Page"
        assert metadata_body["captured_page_title"] == "Captured Manual Page"
        assert metadata_body["user_title_present"] is True
        assert metadata_body["title_source"] == "user_title"
        assert metadata_body["title_updated"] is False
        assert metadata_body["saved_item_title_after"] == "Manual Story"
        assert snapshots_by_kind[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND].capture_run_id == latest_run_id
        assert snapshots_by_kind[CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND].capture_run_id == latest_run_id
        assert (
            snapshots_by_kind[CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND].capture_run_id
            == latest_run_id
        )

        capture_run = session.get(SavedItemCaptureRun, latest_run_id)
        assert capture_run is not None
        assert capture_run.saved_item_id == saved_item_id
        assert capture_run.status == CAPTURE_STATUS_SUCCEEDED
        assert capture_run.requested_targets_json == {"targets": list(DEFAULT_CAPTURE_TARGETS)}
        assert capture_run.retry_of_run_id is None

        assets = session.scalars(
            select(SavedItemAsset).where(
                SavedItemAsset.saved_item_id == saved_item_id,
                SavedItemAsset.capture_run_id == latest_run_id,
            )
        ).all()
        assert len(assets) == 1
        assert assets[0].asset_kind == SINGLEFILE_HTML_SNAPSHOT_KIND
        assert assets[0].asset_role == "primary_archive"
        assert assets[0].mime_type == "text/html"

        operation = session.scalar(
            select(AppOperation)
            .where(AppOperation.operation_type == "knowledge_capture")
            .where(AppOperation.operation_kind == "manual_url")
            .order_by(AppOperation.id.desc())
            .limit(1)
        )
        assert operation is not None
        assert operation.status == "completed"
        assert operation.scope_json["saved_item_id"] == saved_item_id
        jobs = list(session.scalars(select(Job).where(Job.operation_id == operation.id).order_by(Job.id.asc())))
        assert [job.job_type for job in jobs] == [
            operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE,
            operation_jobs.JOB_TYPE_KNOWLEDGE_CAPTURE_LOCAL_MIRROR_EXPORT,
        ]
        assert {job.status for job in jobs} == {"succeeded"}

    singlefile_key = archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["asset_key"]
    html_response = client.get(f"/api/knowledge/assets/{singlefile_key}/content")
    assert html_response.status_code == 200
    assert html_response.headers["content-type"].startswith("text/html")
    assert "sandbox" in html_response.headers["content-security-policy"]


def test_legacy_knowledge_operation_endpoints_are_removed(client) -> None:
    removed_paths = [
        "/api/knowledge/captures/manual-url",
        "/api/knowledge/captures/upload",
        "/api/knowledge/saved-items/123/capture-runs",
        "/api/knowledge/saved-items/123/local-mirror-exports",
        "/api/knowledge/saved-items/123/insight/rebuild",
    ]

    for path in removed_paths:
        response = client.post(path)
        assert response.status_code == 404


def test_capture_manual_url_accepts_explicit_general_ai_route(client) -> None:
    response = _post_manual_url_capture(
        client,
        json={
            "url": "https://manual.example.com/general-route",
            "title": "General Route",
            "ai_route": "general",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ai_route"] == "general"

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, payload["id"])
        assert saved_item is not None
        assert saved_item.ai_route == "general"


def test_archive_markdown_snapshot_content_endpoint_supports_preview_download_and_errors(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/markdown-content"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    preview_response = client.get(
        f"/api/knowledge/saved-items/{saved_item_id}/snapshots/{CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND}/content"
    )
    assert preview_response.status_code == 200
    assert preview_response.headers["content-type"].startswith("text/markdown")
    assert "Archived markdown from https://manual.example.com/markdown-content" in preview_response.text
    assert "content-disposition" not in preview_response.headers

    download_response = client.get(
        f"/api/knowledge/saved-items/{saved_item_id}/snapshots/{CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND}/content",
        params={"download": "true"},
    )
    assert download_response.status_code == 200
    assert (
        download_response.headers["content-disposition"]
        == build_content_disposition_header(
            build_archive_snapshot_download_filename(
                saved_item_id,
                "https://manual.example.com/markdown-content",
                CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
            )
        )
    )

    reinserted_response = client.get(
        f"/api/knowledge/saved-items/{saved_item_id}/snapshots/"
        f"{CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND}/content"
    )
    assert reinserted_response.status_code == 200
    assert reinserted_response.headers["content-type"].startswith("text/markdown")
    assert "Archived fit markdown from https://manual.example.com/markdown-content" in reinserted_response.text

    invalid_kind_response = client.get(
        f"/api/knowledge/saved-items/{saved_item_id}/snapshots/{CANONICAL_SNAPSHOT_KIND}/content"
    )
    assert invalid_kind_response.status_code == 400
    assert invalid_kind_response.json()["detail"] == "Snapshot kind is not downloadable"

    missing_response = client.get(
        f"/api/knowledge/saved-items/9999/snapshots/{CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND}/content"
    )
    assert missing_response.status_code == 404
    assert missing_response.json()["detail"] == "Snapshot not found"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("   ", "Capture URL must not be empty"),
        ("ftp://example.com/article", "URL must be a valid http or https URL"),
        ("https://example.com:bad/article", "URL contains an invalid port"),
        ("http://127.0.0.1/internal", "non-public"),
        ("https://user:pass@example.com/article", "must not include embedded credentials"),
    ],
)
def test_capture_manual_url_rejects_invalid_urls(client, url: str, message: str) -> None:
    response = _post_manual_url_capture(client, json={"url": url})

    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_capture_manual_url_normalizes_blank_title_and_note(client) -> None:
    response = _post_manual_url_capture(
        client,
        json={
            "url": "https://example.com/articles/blank",
            "title": "   ",
            "user_note": "   ",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["title"] == "https://example.com/articles/blank"
    assert payload["user_note"] is None


def test_capture_manual_url_without_user_title_uses_captured_page_title(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_capture.archive_page_with_singlefile",
        _singlefile_archive_with_title("Captured Untitled Page"),
    )

    response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/no-title"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["title"] == "Captured Untitled Page - manual.example.com"
    assert payload["capture"]["title_metadata"]["captured_page_title"] == "Captured Untitled Page"
    assert payload["capture"]["title_metadata"]["title_source"] == "captured_page_title"
    assert payload["capture"]["title_metadata"]["user_title_present"] is False
    assert payload["capture"]["title_metadata"]["title_updated"] is True
    assert payload["capture"]["title_metadata"]["saved_item_title_before"] == "https://manual.example.com/no-title"
    assert payload["capture"]["title_metadata"]["saved_item_title_after"] == "Captured Untitled Page - manual.example.com"

    with get_session_factory()() as session:
        metadata_snapshot = session.scalar(
            select(SavedSnapshot).where(
                SavedSnapshot.saved_item_id == payload["id"],
                SavedSnapshot.snapshot_kind == CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
            )
        )
        assert metadata_snapshot is not None
        metadata = json.loads(metadata_snapshot.body)
        assert metadata["singlefile_title"] == "Captured Untitled Page"
        assert metadata["captured_page_title"] == "Captured Untitled Page"
        assert metadata["user_title_present"] is False
        assert metadata["title_source"] == "captured_page_title"
        assert metadata["title_updated"] is True
        assert metadata["saved_item_title_after"] == "Captured Untitled Page - manual.example.com"


def test_retry_saved_item_capture_refreshes_captured_title_metadata(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_capture.archive_page_with_singlefile",
        _singlefile_archive_with_title("First Captured Title"),
    )
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/retitle"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    assert create_response.json()["title"] == "First Captured Title - manual.example.com"

    monkeypatch.setattr(
        "app.services.knowledge_capture.archive_page_with_singlefile",
        _singlefile_archive_with_title("Second Captured Title"),
    )
    retry_response = _post_retry_capture(client, saved_item_id)

    assert retry_response.status_code == 201
    payload = retry_response.json()
    assert payload["title"] == "Second Captured Title - manual.example.com"
    assert payload["capture"]["title_metadata"]["captured_page_title"] == "Second Captured Title"
    assert payload["capture"]["title_metadata"]["title_source"] == "captured_page_title"
    assert payload["capture"]["title_metadata"]["user_title_present"] is False
    assert payload["capture"]["title_metadata"]["title_updated"] is True
    assert payload["capture"]["title_metadata"]["saved_item_title_before"] == "First Captured Title - manual.example.com"
    assert payload["capture"]["title_metadata"]["saved_item_title_after"] == "Second Captured Title - manual.example.com"
    assert payload["capture"]["title_metadata"]["captured_at"] is not None


def test_manual_url_saved_item_is_listed_detailed_and_searchable_without_content_snapshots(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={
            "url": "https://manual.example.com/deep/path",
            "title": "Manual URL Capture",
            "user_note": "Searchable manual note",
        },
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    list_response = client.get("/api/knowledge/saved-items")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert [item["id"] for item in list_payload] == [saved_item_id]
    assert list_payload[0]["capture"]["status"] == "succeeded"

    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["id"] == saved_item_id
    assert detail_payload["feed_item_id"] is None
    assert detail_payload["content_html"] is None
    assert len(detail_payload["capture_tool_calls"]) == 2
    assert detail_payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert detail_payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"

    search_response = client.get("/api/knowledge/saved-items", params={"query": "manual.example.com/deep"})
    assert search_response.status_code == 200
    assert [item["id"] for item in search_response.json()] == [saved_item_id]


def test_manual_url_saved_item_does_not_appear_in_reader_or_homepage(client) -> None:
    response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/outside-feed", "title": "Outside Feed"},
    )
    assert response.status_code == 201

    reader_response = client.get("/api/reader/items")
    assert reader_response.status_code == 200
    assert reader_response.json() == []

    homepage_response = client.get("/api/homepage/items")
    assert homepage_response.status_code == 200
    assert homepage_response.json()["items"] == []


def test_retry_saved_item_capture_executes_legacy_pending_run_without_tool_calls(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/pending"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    with get_session_factory()() as session:
        legacy_run = SavedItemCaptureRun(
            saved_item_id=saved_item_id,
            status=CAPTURE_STATUS_PENDING,
            requested_targets_json={"targets": list(DEFAULT_CAPTURE_TARGETS)},
            pipeline_version="archive_pipeline_v2",
            trigger_kind="manual_retry",
            retry_of_run_id=None,
            started_at=datetime.now(timezone.utc),
        )
        session.add(legacy_run)
        session.commit()
        legacy_run_id = legacy_run.id

    retry_response = _post_retry_capture(client, saved_item_id)

    assert retry_response.status_code == 201
    assert retry_response.json()["capture"]["latest_run_id"] == legacy_run_id
    assert retry_response.json()["capture"]["status"] == "succeeded"

    with get_session_factory()() as session:
        runs = session.scalars(
            select(SavedItemCaptureRun).where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        ).all()
        assert [run.status for run in runs] == [CAPTURE_STATUS_SUCCEEDED, CAPTURE_STATUS_SUCCEEDED]
        assert runs[-1].requested_targets_json == {"targets": list(DEFAULT_CAPTURE_TARGETS)}


def test_retry_saved_item_capture_creates_new_succeeded_run_after_failed_run(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/retry"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    first_run_id = create_response.json()["capture"]["latest_run_id"]

    with get_session_factory()() as session:
        failed_run = SavedItemCaptureRun(
            saved_item_id=saved_item_id,
            status=CAPTURE_STATUS_PENDING,
            requested_targets_json={"targets": [SINGLEFILE_HTML_SNAPSHOT_KIND]},
            pipeline_version="archive_pipeline_v2",
            trigger_kind="manual_retry",
            retry_of_run_id=first_run_id,
            started_at=datetime.now(timezone.utc),
        )
        session.add(failed_run)
        session.flush()
        create_capture_tool_call(
            session,
            capture_run_id=failed_run.id,
            tool_name="singlefile_extension",
            status=CAPTURE_TOOL_STATUS_FAILED,
            exit_code=1,
            error_summary="singlefile crashed",
        )
        finalize_capture_run(session, failed_run.id)
        session.commit()
        failed_run_id = failed_run.id

    retry_response = _post_retry_capture(client, saved_item_id)
    assert retry_response.status_code == 201
    retry_payload = retry_response.json()
    assert retry_payload["capture"]["status"] == "succeeded"
    assert retry_payload["capture"]["latest_run_id"] not in {first_run_id, failed_run_id}

    with get_session_factory()() as session:
        runs = list(
            session.scalars(
                select(SavedItemCaptureRun)
                .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
                .order_by(SavedItemCaptureRun.id.asc())
            )
        )
        assert len(runs) == 3
        assert runs[0].status == CAPTURE_STATUS_SUCCEEDED
        assert runs[1].status == CAPTURE_STATUS_FAILED
        assert runs[2].status == CAPTURE_STATUS_SUCCEEDED
        assert runs[2].retry_of_run_id == runs[1].id


def test_retry_saved_item_capture_returns_404_for_missing_item(client) -> None:
    response = _post_retry_capture(client, 9999)

    assert response.status_code == 404
    assert response.json()["detail"] == "Saved item not found"


def test_retry_saved_item_capture_allows_feed_based_saved_item(client, monkeypatch) -> None:
    saved_item_id, _ = create_saved_item_from_single_feed(client, monkeypatch)

    response = _post_retry_capture(client, saved_item_id)

    assert response.status_code == 201
    payload = response.json()
    assert payload["capture"]["status"] == "succeeded"
    assert payload["snapshots"]["has_crawl4ai_fit_image_reinserted_markdown"] is True

    with get_session_factory()() as session:
        latest_run = session.scalar(
            select(SavedItemCaptureRun)
            .where(SavedItemCaptureRun.saved_item_id == saved_item_id)
            .order_by(SavedItemCaptureRun.id.desc())
            .limit(1)
        )
        assert latest_run is not None
        assert latest_run.trigger_kind == CAPTURE_TRIGGER_FEED_SAVED_RETRY


def test_mark_saved_creates_saved_item_snapshots_capture_run_and_mirror_export(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_capture.archive_page_with_singlefile",
        _singlefile_archive_with_title("Captured Feed Page"),
    )
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE),
            "https://single.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article><p>Original page snapshot body</p></article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )

    sync_source_with_operation(client, source_id)
    item_id = client.get("/api/reader/items").json()[0]["id"]

    save_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert save_response.status_code == 200
    save_payload = save_response.json()
    saved_item_id = save_payload["saved_item_id"]
    assert saved_item_id is not None
    assert save_payload["has_canonical_snapshot"] is True
    assert save_payload["has_original_page_snapshot"] is False
    assert save_payload["has_singlefile_html"] is False
    assert save_payload["has_crawl4ai_raw_markdown"] is False
    assert save_payload["has_crawl4ai_fit_markdown"] is False
    assert save_payload["has_crawl4ai_fit_image_reinserted_markdown"] is False
    assert save_payload["image_asset_count"] == 0
    _drain_latest_feed_saved_capture(saved_item_id)

    knowledge_detail = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert knowledge_detail.status_code == 200
    detail_payload = knowledge_detail.json()
    assert detail_payload["title"] == "Captured Feed Page"
    assert detail_payload["capture"]["status"] == "succeeded"
    assert detail_payload["snapshots"] == {
        "has_canonical_snapshot": True,
        "has_original_page_snapshot": True,
        "has_singlefile_html": True,
        "has_crawl4ai_raw_markdown": True,
        "has_crawl4ai_fit_markdown": True,
        "has_crawl4ai_fit_image_reinserted_markdown": True,
        "image_asset_count": 0,
    }
    assert detail_payload["local_mirror"]["status"] == "succeeded"
    assert "<p>Single content</p>" in detail_payload["content_html"]

    with get_session_factory()() as session:
        snapshots = session.scalars(select(SavedSnapshot).order_by(SavedSnapshot.id.asc())).all()
        snapshot_kinds = [snapshot.snapshot_kind for snapshot in snapshots]
        assert snapshot_kinds == [
            CANONICAL_SNAPSHOT_KIND,
            ORIGINAL_PAGE_SNAPSHOT_KIND,
            CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
            CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
            CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
            CAPTURE_PAGE_METADATA_SNAPSHOT_KIND,
        ]
        assert snapshots[0].capture_run_id is None
        assert snapshots[1].capture_run_id is None
        assert all(snapshot.capture_run_id == detail_payload["capture"]["latest_run_id"] for snapshot in snapshots[2:])
        metadata = json.loads(snapshots[-1].body)
        assert metadata["singlefile_title"] == "Captured Feed Page"
        assert metadata["feed_title"] == "Single Story"
        assert metadata["user_title_present"] is False
        assert metadata["title_source"] == "captured_page_title"
        assert metadata["title_updated"] is True


def test_feed_saved_item_copies_source_ai_route_at_save_time(client, monkeypatch) -> None:
    source_id = create_source_for_sync(
        client,
        "Sensitive Single",
        "https://sensitive-single.example.com/feed.xml",
        ai_route="sensitive",
    )
    install_transport(
        monkeypatch,
        {
            "https://sensitive-single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE),
            "https://single.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article><p>Original page snapshot body</p></article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )

    sync_source_with_operation(client, source_id)
    item_id = client.get("/api/reader/items").json()[0]["id"]
    save_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert save_response.status_code == 200
    saved_item_id = save_response.json()["saved_item_id"]

    detail_payload = client.get(f"/api/knowledge/saved-items/{saved_item_id}").json()
    assert detail_payload["origin_type"] == "feed_saved"
    assert detail_payload["ai_route"] == "sensitive"

    with get_session_factory()() as session:
        source = session.get(Source, source_id)
        assert source is not None
        source.ai_route = "general"
        session.add(source)
        session.commit()

    detail_after_source_change = client.get(f"/api/knowledge/saved-items/{saved_item_id}").json()
    assert detail_after_source_change["ai_route"] == "sensitive"

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.ai_route == "sensitive"


def test_mark_saved_keeps_saved_item_when_original_page_snapshot_capture_fails(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE),
        },
    )

    sync_source_with_operation(client, source_id)
    item_id = client.get("/api/reader/items").json()[0]["id"]

    save_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert save_response.status_code == 200
    save_payload = save_response.json()
    saved_item_id = save_payload["saved_item_id"]
    assert saved_item_id is not None
    assert save_payload["has_canonical_snapshot"] is True
    assert save_payload["has_original_page_snapshot"] is False
    assert save_payload["has_singlefile_html"] is False
    assert save_payload["has_crawl4ai_raw_markdown"] is False
    assert save_payload["has_crawl4ai_fit_markdown"] is False
    assert save_payload["has_crawl4ai_fit_image_reinserted_markdown"] is False
    assert save_payload["image_asset_count"] == 0
    _drain_latest_feed_saved_capture(saved_item_id)

    knowledge_list = client.get("/api/knowledge/saved-items")
    assert knowledge_list.status_code == 200
    knowledge_payload = knowledge_list.json()
    assert len(knowledge_payload) == 1
    assert knowledge_payload[0]["snapshots"] == {
        "has_canonical_snapshot": True,
        "has_original_page_snapshot": False,
        "has_singlefile_html": True,
        "has_crawl4ai_raw_markdown": True,
        "has_crawl4ai_fit_markdown": True,
        "has_crawl4ai_fit_image_reinserted_markdown": True,
        "image_asset_count": 0,
    }
    assert knowledge_payload[0]["capture"]["status"] == "succeeded"
    assert knowledge_payload[0]["local_mirror"]["status"] == "succeeded"


def test_capture_run_status_becomes_partial_failed_when_singlefile_fails_but_crawl4ai_succeeds(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.services.knowledge_capture.archive_page_with_singlefile", _failed_singlefile_archive)
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/fail"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["capture"]["status"] == "partial_failed"
    assert detail_payload["capture"]["retryable"] is True
    assert "singlefile crashed" in detail_payload["capture"]["latest_error_summary"]
    assert len(detail_payload["capture_tool_calls"]) == 2
    assert detail_payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert detail_payload["capture_tool_calls"][0]["status"] == "failed"
    assert detail_payload["capture_tool_calls"][0]["exit_code"] == 1
    assert detail_payload["capture_tool_calls"][0]["error_summary"] == "singlefile crashed"
    assert detail_payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"
    assert detail_payload["capture_tool_calls"][1]["status"] == "succeeded"
    archive_artifacts = _archive_artifacts_by_kind(detail_payload)
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["status"] == "unavailable"
    assert archive_artifacts[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"


def test_singlefile_antibot_page_is_failed_tool_call_without_archive_asset(client, monkeypatch) -> None:
    def archived_antibot_page(url: str, output_path=None) -> SingleFileArchiveResult:
        html_bytes = _zhihu_antibot_singlefile_html().encode("utf-8")
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
                "profile": "singlefile_extension_archive_v1",
            },
        )

    monkeypatch.setattr("app.services.knowledge_capture.archive_page_with_singlefile", archived_antibot_page)
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/zhihu-antibot"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["capture"]["status"] == "partial_failed"
    assert detail_payload["snapshots"]["has_singlefile_html"] is False
    assert detail_payload["snapshots"]["has_crawl4ai_fit_markdown"] is True
    assert detail_payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert detail_payload["capture_tool_calls"][0]["status"] == CAPTURE_TOOL_STATUS_FAILED
    assert "anti-bot" in detail_payload["capture_tool_calls"][0]["error_summary"]


def test_singlefile_safety_rejection_records_failed_tool_call_and_continues_crawl4ai(
    client,
    monkeypatch,
) -> None:
    def reject_singlefile(url: str, output_path=None):
        raise ValueError("SingleFile capture URL resolved to a non-public IP address")

    monkeypatch.setattr("app.services.knowledge_capture.archive_page_with_singlefile", reject_singlefile)
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/singlefile-rejected"},
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["capture"]["status"] == "partial_failed"
    assert "non-public IP address" in payload["capture"]["latest_error_summary"]
    assert payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert payload["capture_tool_calls"][0]["status"] == "failed"
    assert payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"
    assert payload["capture_tool_calls"][1]["status"] == "succeeded"
    assert payload["snapshots"]["has_crawl4ai_raw_markdown"] is True


def test_singlefile_preflight_http_error_records_failed_tool_call_and_continues_crawl4ai(
    client,
    monkeypatch,
) -> None:
    def fail_singlefile_preflight(url: str, output_path=None):
        raise httpx.ConnectError("preflight failed")

    monkeypatch.setattr("app.services.knowledge_capture.archive_page_with_singlefile", fail_singlefile_preflight)
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/singlefile-preflight-failed"},
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["capture"]["status"] == "partial_failed"
    assert "preflight failed" in payload["capture"]["latest_error_summary"]
    assert payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert payload["capture_tool_calls"][0]["status"] == "failed"
    assert payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"
    assert payload["capture_tool_calls"][1]["status"] == "succeeded"


def test_crawl4ai_tool_args_validation_records_failed_tool_call(client, monkeypatch) -> None:
    def reject_crawl4ai_args(url: str):
        raise ValueError("Crawl4AI capture URL resolved to a non-public IP address")

    monkeypatch.setattr("app.services.knowledge_capture.build_crawl4ai_tool_args", reject_crawl4ai_args)
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/crawl4ai-args-rejected"},
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["capture"]["status"] == "partial_failed"
    assert "non-public IP address" in payload["capture"]["latest_error_summary"]
    crawl4ai_call = payload["capture_tool_calls"][1]
    assert crawl4ai_call["tool_name"] == "crawl4ai"
    assert crawl4ai_call["status"] == "failed"
    with get_session_factory()() as session:
        stored_call = session.get(SavedItemCaptureToolCall, crawl4ai_call["id"])
        assert stored_call is not None
        assert stored_call.tool_args_json["url"] == "[REDACTED]"


def test_capture_run_status_becomes_partial_failed_when_crawl4ai_fails(client, monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", _failed_crawl4ai_capture)

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/crawl-fail"},
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["capture"]["status"] == "partial_failed"
    assert payload["capture"]["retryable"] is True
    assert "crawl4ai crashed" in payload["capture"]["latest_error_summary"]
    assert payload["snapshots"]["has_singlefile_html"] is True
    assert payload["snapshots"]["has_crawl4ai_raw_markdown"] is False
    assert payload["snapshots"]["has_crawl4ai_fit_markdown"] is False
    assert payload["snapshots"]["has_crawl4ai_fit_image_reinserted_markdown"] is False
    assert payload["local_mirror"]["status"] == "succeeded"
    assert payload["capture_tool_calls"][0]["tool_name"] == "singlefile_extension"
    assert payload["capture_tool_calls"][0]["status"] == "succeeded"
    assert payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"
    assert payload["capture_tool_calls"][1]["status"] == "failed"
    assert payload["capture_tool_calls"][1]["error_summary"] == "crawl4ai crashed"
    archive_artifacts = _archive_artifacts_by_kind(payload)
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND]["status"] == "unavailable"
    assert archive_artifacts[CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND]["status"] == "unavailable"


def test_capture_run_writes_crawl4ai_markdown_image_assets_and_refs(client, monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", _image_crawl4ai_capture)

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/with-image"},
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    saved_item_id = payload["id"]
    latest_run_id = payload["capture"]["latest_run_id"]
    assert payload["capture"]["status"] == "succeeded"
    assert payload["snapshots"]["has_crawl4ai_raw_markdown"] is True
    assert payload["snapshots"]["has_crawl4ai_fit_markdown"] is True
    assert payload["snapshots"]["has_crawl4ai_fit_image_reinserted_markdown"] is True
    assert payload["snapshots"]["image_asset_count"] == 1
    assert payload["local_mirror"]["status"] == "succeeded"
    image_artifacts = [
        artifact for artifact in payload["archive_artifacts"] if artifact["artifact_kind"] == "page_image_v1"
    ]
    assert len(image_artifacts) == 1
    assert image_artifacts[0]["status"] == "available"
    assert image_artifacts[0]["preview_url"].startswith("/api/knowledge/assets/")
    assert image_artifacts[0]["download_url"].endswith("?download=true")

    with get_session_factory()() as session:
        raw_snapshot = session.scalar(
            select(SavedSnapshot).where(
                SavedSnapshot.saved_item_id == saved_item_id,
                SavedSnapshot.snapshot_kind == CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
            )
        )
        fit_snapshot = session.scalar(
            select(SavedSnapshot).where(
                SavedSnapshot.saved_item_id == saved_item_id,
                SavedSnapshot.snapshot_kind == CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
            )
        )
        reinserted_snapshot = session.scalar(
            select(SavedSnapshot).where(
                SavedSnapshot.saved_item_id == saved_item_id,
                SavedSnapshot.snapshot_kind == CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
            )
        )
        assert raw_snapshot is not None
        assert fit_snapshot is not None
        assert reinserted_snapshot is not None
        assert "ifa-asset://" in raw_snapshot.body
        assert "https://manual.example.com/image.png" not in raw_snapshot.body
        assert "ifa-asset://" not in fit_snapshot.body
        assert "ifa-asset://" in reinserted_snapshot.body
        assert "https://manual.example.com/image.png" not in reinserted_snapshot.body

        image_assets = session.scalars(
            select(SavedItemAsset).where(
                SavedItemAsset.saved_item_id == saved_item_id,
                SavedItemAsset.capture_run_id == latest_run_id,
                SavedItemAsset.asset_kind == "page_image_v1",
            )
        ).all()
        assert len(image_assets) == 1
        assert image_assets[0].source_url == "https://manual.example.com/image.png"
        assert image_assets[0].content_bytes == b"PNG!"

        refs = session.scalars(select(SavedSnapshotAssetRef).order_by(SavedSnapshotAssetRef.id.asc())).all()
        assert len(refs) == 2
        assert {ref.saved_snapshot_id for ref in refs} == {raw_snapshot.id, reinserted_snapshot.id}
        assert all(ref.ref_token == f"ifa-asset://{image_assets[0].asset_key}" for ref in refs)


def test_capture_run_reinserts_crawl4ai_images_near_cjk_fit_context(client, monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", _cjk_positioned_image_crawl4ai_capture)

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/cjk-images"},
    )

    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    with get_session_factory()() as session:
        reinserted_snapshot = session.scalar(
            select(SavedSnapshot).where(
                SavedSnapshot.saved_item_id == saved_item_id,
                SavedSnapshot.snapshot_kind == CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
            )
        )
        assert reinserted_snapshot is not None
        body = reinserted_snapshot.body
        work_heading_index = body.index("## \u5de5\u4f5c")
        hero_image_index = body.index("![Hero]")
        work_paragraph_index = body.index("\u8fd9\u662f\u7b2c\u4e00\u6bb5\u4e2d\u6587\u5185\u5bb9")
        life_heading_index = body.index("## \u751f\u6d3b")
        life_image_index = body.index("![Life]")
        life_paragraph_index = body.index("\u732b\u54aa\u5728\u65b0\u5bb6\u9002\u5e94\u751f\u6d3b")

        assert work_heading_index < hero_image_index < work_paragraph_index
        assert life_heading_index < life_image_index < life_paragraph_index
        assert body.count("ifa-asset://") == 2


def test_retry_capture_reuses_existing_page_image_assets(client, monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", _image_crawl4ai_capture)

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/with-reused-image"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    with get_session_factory()() as session:
        session.add(
            SavedItemAsset(
                saved_item_id=saved_item_id,
                capture_run_id=None,
                asset_kind=PAGE_IMAGE_ASSET_KIND,
                asset_role="inline_image",
                logical_name="stale-unreferenced-image",
                source_url="https://manual.example.com/stale.png",
                mime_type="image/png",
                file_ext=".png",
                byte_size=6,
                sha256=sha256(b"STALE!").hexdigest(),
                content_bytes=b"STALE!",
                metadata_json={},
            )
        )
        session.commit()

    retry_response = _post_retry_capture(client, saved_item_id)

    assert retry_response.status_code == 201
    payload = retry_response.json()
    assert payload["snapshots"]["image_asset_count"] == 1
    image_artifacts = [
        artifact for artifact in payload["archive_artifacts"] if artifact["artifact_kind"] == PAGE_IMAGE_ASSET_KIND
    ]
    assert len(image_artifacts) == 1

    with get_session_factory()() as session:
        image_assets = session.scalars(
            select(SavedItemAsset).where(
                SavedItemAsset.saved_item_id == saved_item_id,
                SavedItemAsset.asset_kind == PAGE_IMAGE_ASSET_KIND,
            )
        ).all()
        assert len(image_assets) == 1
        assert image_assets[0].logical_name != "stale-unreferenced-image"
        refs = session.scalars(select(SavedSnapshotAssetRef).order_by(SavedSnapshotAssetRef.id.asc())).all()
        assert len(refs) == 4
        assert all(ref.asset_id == image_assets[0].id for ref in refs)


def test_capture_run_succeeds_when_crawl4ai_markdown_succeeds_but_image_fails(client, monkeypatch) -> None:
    monkeypatch.setattr("app.services.knowledge_capture.capture_markdown", _image_failed_crawl4ai_capture)

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/image-fail"},
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["capture"]["status"] == "succeeded"
    assert payload["snapshots"]["has_singlefile_html"] is True
    assert payload["snapshots"]["has_crawl4ai_raw_markdown"] is True
    assert payload["snapshots"]["has_crawl4ai_fit_markdown"] is True
    assert payload["snapshots"]["has_crawl4ai_fit_image_reinserted_markdown"] is True
    assert payload["snapshots"]["image_asset_count"] == 0
    assert payload["local_mirror"]["status"] == "succeeded"
    assert payload["capture_tool_calls"][1]["tool_name"] == "crawl4ai"
    assert payload["capture_tool_calls"][1]["status"] == "succeeded"
    assert payload["capture_tool_calls"][1]["error_summary"] is None


def test_capture_run_status_becomes_partial_failed_when_some_targets_succeed(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/partial"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    run_id = create_response.json()["capture"]["latest_run_id"]

    with get_session_factory()() as session:
        partial_run = SavedItemCaptureRun(
            saved_item_id=saved_item_id,
            status=CAPTURE_STATUS_PENDING,
            requested_targets_json={"targets": list(DEFAULT_CAPTURE_TARGETS)},
            pipeline_version="archive_pipeline_v2",
            trigger_kind="manual_retry",
            retry_of_run_id=run_id,
            started_at=datetime.now(timezone.utc),
        )
        session.add(partial_run)
        session.flush()
        session.add(
            SavedItemAsset(
                saved_item_id=saved_item_id,
                capture_run_id=partial_run.id,
                asset_key="550e8400-e29b-41d4-a716-446655440000",
                asset_kind=SINGLEFILE_HTML_SNAPSHOT_KIND,
                asset_role="primary_archive",
                logical_name="singlefile",
                source_url="https://manual.example.com/partial",
                mime_type="text/html",
                file_ext="html",
                byte_size=len(b"<html><body>Archived</body></html>"),
                sha256="c9f9ec7dec3fd474f2d1e0fc7c13d8549ad64b0be050d4f8fef4fc6ca3ae4c1b",
                content_bytes=b"<html><body>Archived</body></html>",
                metadata_json=None,
            )
        )
        create_capture_tool_call(
            session,
            capture_run_id=partial_run.id,
            tool_name="crawl4ai",
            status=CAPTURE_TOOL_STATUS_FAILED,
            exit_code=2,
            error_summary="crawl4ai conversion failed",
        )
        finalize_capture_run(session, partial_run.id)
        session.commit()

    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["capture"]["status"] == "partial_failed"
    assert detail_payload["capture"]["retryable"] is True
    assert "Missing targets:" in detail_payload["capture"]["latest_error_summary"]
    assert detail_payload["snapshots"]["has_singlefile_html"] is True
    assert detail_payload["snapshots"]["image_asset_count"] == 0


def test_capture_run_status_becomes_succeeded_when_all_requested_targets_exist(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/success"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    run_id = create_response.json()["capture"]["latest_run_id"]

    with get_session_factory()() as session:
        session.add_all(
            [
                SavedItemAsset(
                    saved_item_id=saved_item_id,
                    capture_run_id=run_id,
                    asset_key="550e8400-e29b-41d4-a716-446655440001",
                    asset_kind=SINGLEFILE_HTML_SNAPSHOT_KIND,
                    asset_role="primary_archive",
                    logical_name="singlefile",
                    source_url="https://manual.example.com/success",
                    mime_type="text/html",
                    file_ext="html",
                    byte_size=len(b"<html><body>Archived</body></html>"),
                    sha256="c9f9ec7dec3fd474f2d1e0fc7c13d8549ad64b0be050d4f8fef4fc6ca3ae4c1b",
                    content_bytes=b"<html><body>Archived</body></html>",
                    metadata_json=None,
                ),
                SavedSnapshot(
                    saved_item_id=saved_item_id,
                    capture_run_id=run_id,
                    snapshot_kind=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
                    mime_type="text/markdown",
                    body="# Raw",
                ),
                SavedSnapshot(
                    saved_item_id=saved_item_id,
                    capture_run_id=run_id,
                    snapshot_kind=CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
                    mime_type="text/markdown",
                    body="# Fit",
                ),
            ]
        )
        finalize_capture_run(session, run_id)
        session.commit()

    list_response = client.get("/api/knowledge/saved-items")
    assert list_response.status_code == 200
    payload = list_response.json()[0]
    assert payload["capture"]["status"] == "succeeded"
    assert payload["capture"]["retryable"] is False
    assert payload["capture"]["latest_error_summary"] is None
    assert payload["snapshots"] == {
        "has_canonical_snapshot": False,
        "has_original_page_snapshot": False,
        "has_singlefile_html": True,
        "has_crawl4ai_raw_markdown": True,
        "has_crawl4ai_fit_markdown": True,
        "has_crawl4ai_fit_image_reinserted_markdown": True,
        "image_asset_count": 0,
    }


def test_remove_feed_saved_item_hides_active_views_and_resave_reuses_row(client, monkeypatch) -> None:
    saved_item_id, item_id = create_saved_item_from_single_feed(client, monkeypatch)

    remove_response = client.post(f"/api/knowledge/saved-items/{saved_item_id}/remove")
    assert remove_response.status_code == 200
    remove_payload = remove_response.json()
    assert remove_payload["saved_item_id"] == saved_item_id
    assert remove_payload["feed_item_id"] == item_id
    assert remove_payload["removed_at"] is not None
    assert remove_payload["state"]["saved_at"] is None
    assert remove_payload["state"]["status"] == "unread"

    assert client.get("/api/knowledge/saved-items").json() == []
    assert client.get("/api/knowledge/saved-items", params={"query": "single story"}).json() == []
    assert client.get(f"/api/knowledge/saved-items/{saved_item_id}").status_code == 404

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.removed_at is not None
        capture_run_count = session.scalar(
            select(func.count(SavedItemCaptureRun.id)).where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        )
        assert capture_run_count == 1

    resave_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert resave_response.status_code == 200
    assert resave_response.json()["saved_item_id"] == saved_item_id
    assert resave_response.json()["state"]["status"] == "saved"

    list_response = client.get("/api/knowledge/saved-items")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [saved_item_id]

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.removed_at is None
        capture_run_count = session.scalar(
            select(func.count(SavedItemCaptureRun.id)).where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        )
        assert capture_run_count == 1
        behavior_events = session.scalars(
            select(FeedItemBehaviorEvent)
            .where(FeedItemBehaviorEvent.feed_item_id == item_id)
            .order_by(FeedItemBehaviorEvent.created_at, FeedItemBehaviorEvent.id)
        ).all()
        save_events = [
            event for event in behavior_events if event.event_type in {"saved", "unsaved"}
        ]

    assert [event.event_type for event in save_events] == ["saved", "unsaved", "saved"]
    assert save_events[0].source == "knowledge_save"
    assert save_events[0].event_payload_json["changed"] is True
    assert save_events[1].source == "knowledge_unsave"
    assert save_events[1].event_payload_json["changed"] is True
    assert save_events[1].event_payload_json["first_removal"] is True
    assert save_events[2].event_payload_json["restored_from_removed"] is True


def test_remove_manual_saved_item_hides_and_same_url_capture_restores_row(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={
            "url": "https://manual.example.com/same-url?ref=1#first",
            "title": "Manual Original",
            "user_note": "first note",
        },
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    remove_response = client.post(f"/api/knowledge/saved-items/{saved_item_id}/remove")
    assert remove_response.status_code == 200
    assert remove_response.json()["state"] is None
    assert client.get("/api/knowledge/saved-items").json() == []
    assert client.get(f"/api/knowledge/saved-items/{saved_item_id}").status_code == 404

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.removed_at is not None
        capture_run_count = session.scalar(
            select(func.count(SavedItemCaptureRun.id)).where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        )
        assert capture_run_count == 1

    restore_response = _post_manual_url_capture(
        client,
        json={
            "url": " HTTPS://manual.example.com/same-url?ref=1#second ",
            "title": "Manual Restored",
            "user_note": "second note",
        },
    )
    assert restore_response.status_code == 201
    restore_payload = restore_response.json()
    assert restore_payload["id"] == saved_item_id
    assert restore_payload["title"] == "Manual Restored"
    assert restore_payload["user_note"] == "second note"
    assert restore_payload["state"]["status"] == "saved"

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.removed_at is None
        duplicate_count = session.scalar(
            select(func.count(SavedItem.id)).where(
                SavedItem.feed_item_id.is_(None),
                SavedItem.canonical_url == "https://manual.example.com/same-url?ref=1",
            )
        )
        assert duplicate_count == 1
        capture_run_count = session.scalar(
            select(func.count(SavedItemCaptureRun.id)).where(SavedItemCaptureRun.saved_item_id == saved_item_id)
        )
        assert capture_run_count == 1


def test_list_saved_items_query_matches_title(client, monkeypatch) -> None:
    saved_item_id, _ = create_saved_item_from_single_feed(client, monkeypatch)

    response = client.get("/api/knowledge/saved-items", params={"query": "single story"})

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [saved_item_id]


def test_list_saved_items_query_matches_user_note(client, monkeypatch) -> None:
    saved_item_id, _ = create_saved_item_from_single_feed(client, monkeypatch)
    patch_response = client.patch(
        f"/api/knowledge/saved-items/{saved_item_id}",
        json={"user_note": "Archive this for phase 3B note search"},
    )
    assert patch_response.status_code == 200

    response = client.get("/api/knowledge/saved-items", params={"query": "note search"})

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [saved_item_id]


def test_list_saved_items_query_matches_latest_canonical_snapshot_body(client, monkeypatch) -> None:
    saved_item_id, _ = create_saved_item_from_single_feed(client, monkeypatch)

    response = client.get("/api/knowledge/saved-items", params={"query": "single content"})

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [saved_item_id]


def test_list_saved_items_query_ignores_blank_query(client, monkeypatch) -> None:
    saved_item_id, _ = create_saved_item_from_single_feed(client, monkeypatch)

    response = client.get("/api/knowledge/saved-items", params={"query": "   "})

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == [saved_item_id]


def test_patch_saved_item_updates_and_clears_user_note(client, monkeypatch) -> None:
    saved_item_id, _ = create_saved_item_from_single_feed(client, monkeypatch)

    update_response = client.patch(
        f"/api/knowledge/saved-items/{saved_item_id}",
        json={"user_note": "  Keep this source for synthesis  "},
    )

    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["id"] == saved_item_id
    assert update_payload["user_note"] == "Keep this source for synthesis"

    list_response = client.get("/api/knowledge/saved-items")
    assert list_response.status_code == 200
    assert list_response.json()[0]["user_note"] == "Keep this source for synthesis"

    clear_response = client.patch(
        f"/api/knowledge/saved-items/{saved_item_id}",
        json={"user_note": "   "},
    )

    assert clear_response.status_code == 200
    assert clear_response.json()["user_note"] is None


def test_patch_saved_item_returns_404_for_missing_item(client) -> None:
    response = client.patch("/api/knowledge/saved-items/9999", json={"user_note": "Missing"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Saved item not found"


def test_saved_item_curated_insight_resolves_over_insight_engine_artifact_and_keeps_events(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/insight", "title": "Insight Manual"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    ai_payload = build_saved_item_insight_payload(summary="AI summary", long_term_value="AI value")
    with get_session_factory()() as session:
        artifact = InsightEngineArtifact(
            feed_item_id=None,
            saved_item_id=saved_item_id,
            artifact_kind=SAVED_ITEM_INSIGHT_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="saved-item-insight-v1",
            artifact_payload=ai_payload,
        )
        session.add(artifact)
        session.commit()

    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["ai_generated_insight"]["payload"]["summary"] == "AI summary"
    assert detail_payload["curated_insight"] is None
    assert detail_payload["resolved_insight"]["source"] == "ai_generated"
    assert detail_payload["resolved_insight"]["payload"]["long_term_value"] == "AI value"

    curated_payload = build_saved_item_insight_payload(
        summary="Curated summary",
        long_term_value="Curated value",
        actionable_takeaways=["Reuse this workflow"],
    )
    update_response = client.put(
        f"/api/knowledge/saved-items/{saved_item_id}/curated-insight",
        json={"payload": curated_payload, "edit_note": "manual correction"},
    )
    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["curated_insight"]["payload"]["summary"] == "Curated summary"
    assert update_payload["curated_insight"]["edit_note"] == "manual correction"
    assert update_payload["resolved_insight"]["source"] == "curated"
    assert update_payload["resolved_insight"]["payload"]["long_term_value"] == "Curated value"

    second_payload = build_saved_item_insight_payload(summary="Second curated summary")
    second_response = client.put(
        f"/api/knowledge/saved-items/{saved_item_id}/curated-insight",
        json={"payload": second_payload, "edit_note": "second pass"},
    )
    assert second_response.status_code == 200
    assert second_response.json()["resolved_insight"]["payload"]["summary"] == "Second curated summary"

    events_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}/curated-insight/events")
    assert events_response.status_code == 200
    assert [event["event_type"] for event in events_response.json()] == [
        "curated_insight_created",
        "curated_insight_updated",
    ]
    assert events_response.json()[1]["event_payload_json"]["diff"]["changed_fields"]["summary"]["after"] == (
        "Second curated summary"
    )

    clear_response = client.delete(f"/api/knowledge/saved-items/{saved_item_id}/curated-insight")
    assert clear_response.status_code == 200
    clear_payload = clear_response.json()
    assert clear_payload["curated_insight"] is None
    assert clear_payload["resolved_insight"]["source"] == "ai_generated"
    assert clear_payload["resolved_insight"]["payload"]["summary"] == "AI summary"

    events_after_clear = client.get(f"/api/knowledge/saved-items/{saved_item_id}/curated-insight/events").json()
    assert [event["event_type"] for event in events_after_clear] == [
        "curated_insight_created",
        "curated_insight_updated",
        "curated_insight_cleared",
    ]

    with get_session_factory()() as session:
        assert session.scalar(select(func.count(SavedItemCuratedInsight.id))) == 0
        assert session.scalar(select(func.count(SavedItemCuratedInsightEvent.id))) == 3


def test_saved_item_curated_insight_rejects_invalid_payload(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/bad-insight", "title": "Bad Insight"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    response = client.put(
        f"/api/knowledge/saved-items/{saved_item_id}/curated-insight",
        json={"payload": {"not_a_real_field": "x"}, "edit_note": "oops"},
    )

    assert response.status_code == 400


def test_rebuild_saved_item_insight_writes_saved_item_insight_engine_artifact(client, monkeypatch) -> None:
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Generated saved insight",
                                    "long_term_value": "Useful for future synthesis",
                                    "actionable_takeaways": ["Reuse as evidence"],
                                    "key_concepts": ["capture inbox"],
                                    "suggested_tags": ["knowledge-base"],
                                    "topic_ids": ["knowledge_information"],
                                    "content_type": "reference",
                                }
                            )
                        }
                    }
                ]
            },
            request=request,
        )

    install_ai_transport(monkeypatch, handler)
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "sensitive": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com/v1",
                    "model": "sensitive-saved-insight-model",
                    "api_key": "sk-saved-insight",
                }
            }
        },
    )
    assert settings_response.status_code == 200
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/rebuild-insight", "title": "Rebuild Insight"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    response = _post_saved_item_insight_rebuild(client, saved_item_id)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ai_route"] == "sensitive"
    assert payload["resolved_insight"]["source"] == "ai_generated"
    assert payload["resolved_insight"]["payload"]["summary"] == "Generated saved insight"
    assert payload["resolved_insight"]["payload"]["ai_route"] == "sensitive"
    assert payload["resolved_insight"]["payload"]["input_text_source"] == CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND
    assert requests_seen
    request_body = json.loads(requests_seen[0].content.decode("utf-8"))
    assert request_body["model"] == "sensitive-saved-insight-model"
    assert "Archived fit markdown from https://manual.example.com/rebuild-insight" in request_body["messages"][1]["content"]

    with get_session_factory()() as session:
        artifact = session.scalar(
            select(InsightEngineArtifact).where(
                InsightEngineArtifact.saved_item_id == saved_item_id,
                InsightEngineArtifact.artifact_kind == SAVED_ITEM_INSIGHT_ARTIFACT_KIND,
            )
        )
        assert artifact is not None
        assert artifact.feed_item_id is None
        assert artifact.model_name == "openai-compatible/sensitive-saved-insight-model"
        assert artifact.artifact_payload["ai_route"] == "sensitive"


def test_rebuild_saved_item_insight_uses_json_schema_for_lm_studio(client, monkeypatch) -> None:
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Generated saved insight",
                                    "long_term_value": "Useful for future synthesis",
                                    "actionable_takeaways": ["Reuse as evidence"],
                                    "key_concepts": ["capture inbox"],
                                    "suggested_tags": ["knowledge-base"],
                                    "topic_ids": ["knowledge_information"],
                                    "content_type": "reference",
                                }
                            )
                        }
                    }
                ]
            },
            request=request,
        )

    install_ai_transport(monkeypatch, handler)
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "sensitive": {
                    "provider": "lm-studio",
                    "enabled": True,
                    "base_url": "http://10.70.34.34:1234/v1",
                    "model": "qwen/qwen3.5-9b",
                    "api_key": None,
                }
            }
        },
    )
    assert settings_response.status_code == 200
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/rebuild-lmstudio", "title": "Rebuild LM Studio"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    response = _post_saved_item_insight_rebuild(client, saved_item_id)

    assert response.status_code == 200
    assert requests_seen
    request_body = json.loads(requests_seen[0].content.decode("utf-8"))
    assert request_body["model"] == "qwen/qwen3.5-9b"
    assert "authorization" not in requests_seen[0].headers
    response_format = request_body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "saved_item_insight"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["topic_ids"]["items"]["enum"]
    assert schema["properties"]["content_type"]["enum"]

    with get_session_factory()() as session:
        artifact = session.scalar(
            select(InsightEngineArtifact).where(
                InsightEngineArtifact.saved_item_id == saved_item_id,
                InsightEngineArtifact.artifact_kind == SAVED_ITEM_INSIGHT_ARTIFACT_KIND,
            )
        )
        assert artifact is not None
        assert artifact.model_name == "lm-studio/qwen/qwen3.5-9b"


def test_rebuild_saved_item_insight_can_use_explicit_route_override(client, monkeypatch) -> None:
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Generated with general route",
                                    "long_term_value": "Route override evidence",
                                    "actionable_takeaways": [],
                                    "key_concepts": [],
                                    "suggested_tags": [],
                                    "topic_ids": ["knowledge_information"],
                                    "content_type": "reference",
                                }
                            )
                        }
                    }
                ]
            },
            request=request,
        )

    install_ai_transport(monkeypatch, handler)
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://general-ai.example.com/v1",
                    "model": "general-saved-insight-model",
                    "api_key": "sk-general-saved-insight",
                }
            }
        },
    )
    assert settings_response.status_code == 200
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/rebuild-general", "title": "Rebuild General"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    assert create_response.json()["ai_route"] == "sensitive"

    response = _post_saved_item_insight_rebuild(
        client, saved_item_id,
        json={"ai_route": "general"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ai_route"] == "sensitive"
    assert payload["resolved_insight"]["payload"]["ai_route"] == "general"
    assert requests_seen
    request_body = json.loads(requests_seen[0].content.decode("utf-8"))
    assert request_body["model"] == "general-saved-insight-model"


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


def sync_source_with_operation(client, source_id: int) -> dict:
    response = client.post("/api/operations/source-sync", json={"kind": "single", "source_id": source_id})
    assert response.status_code == 202
    operation_id = response.json()["id"]
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)
    operation = client.get(f"/api/operations/{operation_id}").json()
    assert operation["status"] in {"completed", "failed", "stopped"}
    assert operation["result"] is not None
    return operation["result"]


def _drain_latest_feed_saved_capture(saved_item_id: int) -> None:
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
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)


def _archive_artifacts_by_kind(payload: dict) -> dict[str, dict]:
    return {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}


def create_saved_item_from_single_feed(client, monkeypatch) -> tuple[int, int]:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE),
            "https://single.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article><p>Original page snapshot body</p></article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )

    sync_source_with_operation(client, source_id)
    item_id = client.get("/api/reader/items").json()[0]["id"]
    save_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert save_response.status_code == 200
    saved_item_id = save_response.json()["saved_item_id"]
    _drain_latest_feed_saved_capture(saved_item_id)
    return saved_item_id, item_id


def install_transport(monkeypatch, handler_or_map) -> None:
    if callable(handler_or_map):
        handler = handler_or_map
    else:
        mapping = handler_or_map

        def handler(request: httpx.Request) -> httpx.Response:
            response = mapping[str(request.url)]
            response.request = request
            return response

    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(source_sync, "create_http_client", fake_create_http_client)


def install_ai_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(ai_provider, "create_http_client", fake_create_http_client)


def build_saved_item_insight_payload(
    *,
    summary: str = "Saved item summary",
    long_term_value: str = "Long-term reference value",
    actionable_takeaways: list[str] | None = None,
) -> dict:
    return {
        "summary": summary,
        "long_term_value": long_term_value,
        "actionable_takeaways": actionable_takeaways or ["Act on this later"],
        "key_concepts": ["knowledge base"],
        "suggested_tags": ["reference"],
        "topic_ids": ["knowledge_information"],
        "topic_labels": ["Knowledge management"],
        "content_type": "reference",
        "content_type_label": "Reference",
        "source_kind": "external_source",
        "input_snapshot_kind": CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
        "input_text_source": CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
        "input_char_count": 120,
        "input_text_was_truncated": False,
    }


def _singlefile_archive_with_title(page_title: str):
    def fake_archive(url: str, output_path=None) -> SingleFileArchiveResult:
        html_bytes = f"<!doctype html><html><head><title>{page_title}</title></head><body>Archived {url}</body></html>".encode(
            "utf-8"
        )
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

    return fake_archive


def _zhihu_antibot_singlefile_html() -> str:
    return (
        '<html><!-- Page saved with SingleFile url: https://www.zhihu.com/question/example -->'
        '<body><pre>{"error":{"message":"您当前请求存在异常，暂时限制本次访问。如有疑问，'
        '可以通过手机摇一摇或登录后私信知乎小管家反馈。","code":40362}}</pre></body></html>'
    )


def _failed_singlefile_archive(url: str, output_path=None) -> SingleFileArchiveResult:
    return SingleFileArchiveResult(
        succeeded=False,
        html_bytes=None,
        exit_code=1,
        stdout_tail="singlefile stdout",
        stderr_tail="singlefile stderr",
        error_summary="singlefile crashed",
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


def _failed_crawl4ai_capture(url: str) -> Crawl4AICaptureResult:
    raise RuntimeError("crawl4ai crashed")


def _image_crawl4ai_capture(url: str) -> Crawl4AICaptureResult:
    return Crawl4AICaptureResult(
        raw_markdown="# Raw\n\nImportant paragraph\n\n![Hero](https://manual.example.com/image.png)\n",
        fit_markdown="# Fit\n\nImportant paragraph\n",
        image_assets=[
            CaptureAssetPayload(
                logical_name="hero",
                source_url="https://manual.example.com/image.png",
                mime_type="image/png",
                file_ext="png",
                content_bytes=b"PNG!",
                alt_text="Hero",
                metadata={"tool_name": "crawl4ai", "tool_version": "0.8.6"},
            )
        ],
    )


def _cjk_positioned_image_crawl4ai_capture(url: str) -> Crawl4AICaptureResult:
    return Crawl4AICaptureResult(
        raw_markdown=(
            "# \u5468\u62a5\n\n"
            "## \u5de5\u4f5c\n\n"
            "![Hero](https://manual.example.com/hero.png)\n\n"
            "\u8fd9\u662f\u7b2c\u4e00\u6bb5\u4e2d\u6587\u5185\u5bb9\uff0c"
            "\u5173\u4e8e OpenClaw \u5b9e\u8df5\u548c\u642c\u5bb6\u8bb0\u5f55\u3002\n\n"
            "## \u751f\u6d3b\n\n"
            "\u4e0a\u4e00\u6bb5\u4ecb\u7ecd\u8f93\u5165\u548c\u8f93\u51fa\u3002\n\n"
            "![Life](https://manual.example.com/life.png)\n\n"
            "\u732b\u54aa\u5728\u65b0\u5bb6\u9002\u5e94\u751f\u6d3b\uff0c"
            "\u56fe\u7247\u8bb0\u5f55\u642c\u5bb6\u540e\u7684\u80fd\u91cf\u3002\n"
        ),
        fit_markdown=(
            "# \u5468\u62a5\n\n"
            "## \u5de5\u4f5c\n"
            "\u8fd9\u662f\u7b2c\u4e00\u6bb5\u4e2d\u6587\u5185\u5bb9\uff0c"
            "\u5173\u4e8e OpenClaw \u5b9e\u8df5\u548c\u642c\u5bb6\u8bb0\u5f55\u3002\n"
            "## \u751f\u6d3b\n"
            "\u4e0a\u4e00\u6bb5\u4ecb\u7ecd\u8f93\u5165\u548c\u8f93\u51fa\u3002\n"
            "\u732b\u54aa\u5728\u65b0\u5bb6\u9002\u5e94\u751f\u6d3b\uff0c"
            "\u56fe\u7247\u8bb0\u5f55\u642c\u5bb6\u540e\u7684\u80fd\u91cf\u3002\n"
        ),
        image_assets=[
            CaptureAssetPayload(
                logical_name="hero",
                source_url="https://manual.example.com/hero.png",
                mime_type="image/png",
                file_ext="png",
                content_bytes=b"HERO!",
                alt_text="Hero",
                metadata={"tool_name": "crawl4ai", "tool_version": "0.8.6"},
            ),
            CaptureAssetPayload(
                logical_name="life",
                source_url="https://manual.example.com/life.png",
                mime_type="image/png",
                file_ext="png",
                content_bytes=b"LIFE!",
                alt_text="Life",
                metadata={"tool_name": "crawl4ai", "tool_version": "0.8.6"},
            ),
        ],
    )


def _image_failed_crawl4ai_capture(url: str) -> Crawl4AICaptureResult:
    return Crawl4AICaptureResult(
        raw_markdown="# Raw\n\n![Hero](https://manual.example.com/missing.png)\n",
        fit_markdown="# Fit\n\n![Hero](https://manual.example.com/missing.png)\n",
        diagnostics=[
            CaptureToolDiagnostic(
                code="crawl4ai_image_download_failed",
                message="image failed",
                level="warning",
            )
        ],
    )
