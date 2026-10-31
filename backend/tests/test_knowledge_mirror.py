from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.core.config import reset_settings_cache
from app.db.models import (
    FeedItem,
    SavedItem,
    SavedItemAsset,
    SavedItemCaptureRun,
    SavedItemMirrorExport,
    SavedSnapshot,
    SavedSnapshotAssetRef,
)
from app.db.session import get_session_factory
from app.services import operations as operations_service
from app.services.knowledge_constants import (
    CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
    CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
    PAGE_IMAGE_ASSET_KIND,
    SINGLEFILE_HTML_SNAPSHOT_KIND,
)
from app.services.knowledge_snapshot_assets import (
    build_archive_asset_download_filename,
    build_content_disposition_header,
    build_local_mirror_asset_filename,
    build_local_mirror_markdown_filename,
    build_local_mirror_singlefile_filename,
    build_saved_item_archive_dir_name,
    build_saved_item_title_file_stem,
)
from app.services.knowledge_mirror import (
    MIRROR_STATUS_SUCCEEDED,
    MIRROR_TARGET_FIT_IMAGE_REINSERTED_MARKDOWN,
    MIRROR_TARGET_RAW_MARKDOWN,
    MIRROR_TRIGGER_FULL_REBUILD,
    MIRROR_TRIGGER_MANUAL_EXPORT,
    MIRROR_TRIGGER_SOFT_REMOVE_ARCHIVE,
    export_saved_item_to_local_mirror,
    rebuild_local_mirror_from_db,
)


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


def _post_local_mirror_export(client, saved_item_id: int):
    return _saved_item_detail_after_operation(
        client,
        client.post(f"/api/operations/saved-items/{saved_item_id}/local-mirror-exports"),
        success_status=201,
    )


def test_saved_item_title_filename_stem_avoids_windows_reserved_names() -> None:
    assert build_saved_item_title_file_stem("CON") == "CON_"
    assert build_local_mirror_singlefile_filename("CON") == "CON_.html"
    assert build_local_mirror_markdown_filename("NUL", "raw") == "NUL__raw.md"


def test_saved_item_asset_content_endpoint_returns_binary_payload(client) -> None:
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/asset"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    run_id = create_response.json()["capture"]["latest_run_id"]

    with get_session_factory()() as session:
        session.add(
            SavedItemAsset(
                saved_item_id=saved_item_id,
                capture_run_id=run_id,
                asset_key="550e8400-e29b-41d4-a716-446655440002",
                asset_kind=PAGE_IMAGE_ASSET_KIND,
                asset_role="inline_image",
                logical_name="hero",
                source_url="https://manual.example.com/image.png",
                mime_type="image/png",
                file_ext="png",
                byte_size=4,
                sha256="0f4636c78f65d3639ece5a064b5ae753e3408614a14fb18ab4d7540d2c248543",
                content_bytes=b"PNG!",
                metadata_json={"width": 1, "height": 1},
            )
        )
        session.commit()

    response = client.get("/api/knowledge/assets/550e8400-e29b-41d4-a716-446655440002/content")

    assert response.status_code == 200
    assert response.content == b"PNG!"
    assert response.headers["content-type"].startswith("image/png")
    assert "content-disposition" not in response.headers

    download_response = client.get(
        "/api/knowledge/assets/550e8400-e29b-41d4-a716-446655440002/content",
        params={"download": "true"},
    )
    assert download_response.status_code == 200
    assert download_response.content == b"PNG!"
    assert download_response.headers["content-type"].startswith("image/png")
    assert (
        download_response.headers["content-disposition"]
        == build_content_disposition_header(
            build_archive_asset_download_filename(
                saved_item_id,
                "https://manual.example.com/asset",
                SavedItemAsset(
                    saved_item_id=saved_item_id,
                    capture_run_id=run_id,
                    asset_key="550e8400-e29b-41d4-a716-446655440002",
                    asset_kind=PAGE_IMAGE_ASSET_KIND,
                    asset_role="inline_image",
                    logical_name="hero",
                    source_url="https://manual.example.com/image.png",
                    mime_type="image/png",
                    file_ext="png",
                    byte_size=4,
                    sha256="0f4636c78f65d3639ece5a064b5ae753e3408614a14fb18ab4d7540d2c248543",
                    content_bytes=b"PNG!",
                    metadata_json={"width": 1, "height": 1},
                ),
            )
        )
    )


def test_export_saved_item_to_local_mirror_rewrites_asset_refs_and_updates_detail(client, tmp_path, monkeypatch) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/export", "title": "Mirror Export"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    run_id = create_response.json()["capture"]["latest_run_id"]

    with get_session_factory()() as session:
        raw_snapshot = SavedSnapshot(
            saved_item_id=saved_item_id,
            capture_run_id=run_id,
            snapshot_kind=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
            mime_type="text/markdown",
            body="# Raw\n\n![Hero](ifa-asset://550e8400-e29b-41d4-a716-446655440003)\n",
        )
        fit_snapshot = SavedSnapshot(
            saved_item_id=saved_item_id,
            capture_run_id=run_id,
            snapshot_kind=CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND,
            mime_type="text/markdown",
            body="# Fit\n",
        )
        reinserted_snapshot = SavedSnapshot(
            saved_item_id=saved_item_id,
            capture_run_id=run_id,
            snapshot_kind=CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND,
            mime_type="text/markdown",
            body="# Fit Images\n\n![Hero](ifa-asset://550e8400-e29b-41d4-a716-446655440003)\n",
        )
        image_asset = SavedItemAsset(
            saved_item_id=saved_item_id,
            capture_run_id=run_id,
            asset_key="550e8400-e29b-41d4-a716-446655440003",
            asset_kind=PAGE_IMAGE_ASSET_KIND,
            asset_role="inline_image",
            logical_name="hero",
            source_url="https://manual.example.com/image.png",
            mime_type="image/png",
            file_ext="png",
            byte_size=4,
            sha256="0f4636c78f65d3639ece5a064b5ae753e3408614a14fb18ab4d7540d2c248543",
            content_bytes=b"PNG!",
            metadata_json=None,
        )
        stale_image_asset = SavedItemAsset(
            saved_item_id=saved_item_id,
            capture_run_id=run_id,
            asset_key="550e8400-e29b-41d4-a716-446655440009",
            asset_kind=PAGE_IMAGE_ASSET_KIND,
            asset_role="inline_image",
            logical_name="stale",
            source_url="https://manual.example.com/stale.png",
            mime_type="image/png",
            file_ext="png",
            byte_size=6,
            sha256="d15dbfbbde528d39010f1f22304775e54a92076e0a7bd8577d7a8f3d39f4f2f5",
            content_bytes=b"STALE!",
            metadata_json=None,
        )
        singlefile_asset = SavedItemAsset(
            saved_item_id=saved_item_id,
            capture_run_id=run_id,
            asset_key="550e8400-e29b-41d4-a716-446655440004",
            asset_kind=SINGLEFILE_HTML_SNAPSHOT_KIND,
            asset_role="primary_archive",
            logical_name="singlefile",
            source_url="https://manual.example.com/export",
            mime_type="text/html",
            file_ext="html",
            byte_size=len(b"<html><body>Archived</body></html>"),
            sha256="c9f9ec7dec3fd474f2d1e0fc7c13d8549ad64b0be050d4f8fef4fc6ca3ae4c1b",
            content_bytes=b"<html><body>Archived</body></html>",
            metadata_json=None,
        )
        session.add_all(
            [raw_snapshot, fit_snapshot, reinserted_snapshot, image_asset, stale_image_asset, singlefile_asset]
        )
        session.flush()
        session.add_all(
            [
                SavedSnapshotAssetRef(
                    saved_snapshot_id=raw_snapshot.id,
                    asset_id=image_asset.id,
                    ref_kind="markdown_image",
                    ref_token="ifa-asset://550e8400-e29b-41d4-a716-446655440003",
                    alt_text="Hero",
                    ordinal=0,
                    source_url=image_asset.source_url,
                ),
                SavedSnapshotAssetRef(
                    saved_snapshot_id=reinserted_snapshot.id,
                    asset_id=image_asset.id,
                    ref_kind="markdown_image",
                    ref_token="ifa-asset://550e8400-e29b-41d4-a716-446655440003",
                    alt_text="Hero",
                    ordinal=0,
                    source_url=image_asset.source_url,
                ),
            ]
        )
        export = export_saved_item_to_local_mirror(
            session,
            saved_item_id,
            trigger_kind=MIRROR_TRIGGER_MANUAL_EXPORT,
            root_dir=mirror_root,
        )
        session.commit()

        assert export.status == MIRROR_STATUS_SUCCEEDED

    article_dir_name = build_saved_item_archive_dir_name("Mirror Export", "https://manual.example.com/export")
    article_dir = mirror_root / article_dir_name
    raw_file = article_dir / build_local_mirror_markdown_filename("Mirror Export", "raw")
    fit_file = article_dir / build_local_mirror_markdown_filename("Mirror Export", "fit")
    reinserted_file = article_dir / build_local_mirror_markdown_filename("Mirror Export", "fit-images")
    manifest_file = article_dir / ".infoflow" / "manifest.json"
    image_file = article_dir / "assets" / build_local_mirror_asset_filename(
        "Mirror Export",
        "https://manual.example.com/export",
        image_asset,
        index=1,
    )
    stale_image_file = article_dir / "assets" / "Mirror Export--ignored_stale.png"
    singlefile_file = article_dir / build_local_mirror_singlefile_filename("Mirror Export")

    assert raw_file.exists()
    assert fit_file.exists()
    assert reinserted_file.exists()
    assert manifest_file.exists()
    assert image_file.exists()
    assert not stale_image_file.exists()
    assert singlefile_file.exists()
    assert "ifa-asset://" not in raw_file.read_text(encoding="utf-8")
    assert f"assets/{image_file.name}" in raw_file.read_text(encoding="utf-8")
    assert "ifa-asset://" not in reinserted_file.read_text(encoding="utf-8")
    assert f"assets/{image_file.name}" in reinserted_file.read_text(encoding="utf-8")
    manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    raw_file_entry = next(entry for entry in manifest_payload["files"] if entry["role"] == MIRROR_TARGET_RAW_MARKDOWN)
    reinserted_file_entry = next(
        entry for entry in manifest_payload["files"] if entry["role"] == MIRROR_TARGET_FIT_IMAGE_REINSERTED_MARKDOWN
    )
    image_file_entry = next(
        entry
        for entry in manifest_payload["files"]
        if entry["role"] == "assets" and entry["asset_key"] == "550e8400-e29b-41d4-a716-446655440003"
    )
    assert not any(
        entry.get("asset_key") == "550e8400-e29b-41d4-a716-446655440009"
        for entry in manifest_payload["files"]
    )
    singlefile_entry = next(entry for entry in manifest_payload["files"] if entry["role"] == "singlefile_html")
    assert manifest_payload["manifest_version"] == "knowledge_base_manifest_v1"
    assert manifest_payload["saved_item_id"] == saved_item_id
    assert raw_file_entry["relative_path"] == f"{article_dir_name}/Mirror Export_raw.md"
    assert raw_file_entry["snapshot_kind"] == CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND
    assert raw_file_entry["source_kind"] == "snapshot"
    assert raw_file_entry["byte_size"] == raw_file.stat().st_size
    assert len(raw_file_entry["sha256"]) == 64
    assert raw_file_entry["verification_status"] == "matched"
    assert (
        reinserted_file_entry["relative_path"]
        == f"{article_dir_name}/Mirror Export_fit-images.md"
    )
    assert reinserted_file_entry["snapshot_kind"] == CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND
    assert image_file_entry["relative_path"] == f"{article_dir_name}/assets/{image_file.name}"
    assert image_file_entry["asset_kind"] == PAGE_IMAGE_ASSET_KIND
    assert image_file_entry["mime_type"] == "image/png"
    assert image_file_entry["source_url"] == "https://manual.example.com/image.png"
    assert (
        singlefile_entry["relative_path"]
        == f"{article_dir_name}/Mirror Export.html"
    )
    assert singlefile_entry["asset_kind"] == SINGLEFILE_HTML_SNAPSHOT_KIND

    detail_response = client.get(f"/api/knowledge/saved-items/{saved_item_id}")
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["snapshots"]["has_singlefile_html"] is True
    assert detail_payload["snapshots"]["image_asset_count"] == 1
    assert detail_payload["local_mirror"]["status"] == "succeeded"
    assert detail_payload["local_mirror"]["source_capture_run_id"] == run_id
    assert detail_payload["local_mirror"]["written_file_count"] == 6
    assert Path(detail_payload["local_mirror"]["paths"]["raw_markdown"]) == raw_file
    assert Path(detail_payload["local_mirror"]["paths"]["fit_image_reinserted_markdown"]) == reinserted_file
    assert Path(detail_payload["local_mirror"]["paths"]["markdown_assets_dir"]) == article_dir / "assets"
    assert Path(detail_payload["local_mirror"]["paths"]["article_dir"]) == article_dir

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_local_mirror_names_feed_saved_item_images_with_title_url_hash_and_capture_name(
    client,
    tmp_path,
    monkeypatch,
) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    with get_session_factory()() as session:
        feed_item = FeedItem(
            original_url="https://rss.example.com/posts/openclaw",
            canonical_url="https://rss.example.com/posts/openclaw",
            title="周报 #106 - OpenClaw 实践、搬家与生活的能量",
            author_name=None,
            published_at=now,
            summary_text="RSS summary",
            content_text="RSS content",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
            topic_hints=None,
        )
        session.add(feed_item)
        session.flush()

        saved_item = SavedItem(
            feed_item_id=feed_item.id,
            title=feed_item.title,
            canonical_url=feed_item.canonical_url or feed_item.original_url,
            source_name="RSS Example",
            source_app="https://rss.example.com/feed.xml",
            user_note=None,
            saved_at=now,
        )
        session.add(saved_item)
        session.flush()

        capture_run = SavedItemCaptureRun(
            saved_item_id=saved_item.id,
            status="succeeded",
            requested_targets_json={"targets": ["raw_markdown", "assets"]},
            pipeline_version="test",
            trigger_kind="feed_saved",
            retry_of_run_id=None,
            error_summary=None,
            started_at=now,
            finished_at=now,
        )
        session.add(capture_run)
        session.flush()

        raw_snapshot = SavedSnapshot(
            saved_item_id=saved_item.id,
            capture_run_id=capture_run.id,
            snapshot_kind=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
            mime_type="text/markdown",
            body="# RSS Raw\n\n![OpenClaw](ifa-asset://550e8400-e29b-41d4-a716-446655440010)\n",
        )
        image_asset = SavedItemAsset(
            saved_item_id=saved_item.id,
            capture_run_id=capture_run.id,
            asset_key="550e8400-e29b-41d4-a716-446655440010",
            asset_kind=PAGE_IMAGE_ASSET_KIND,
            asset_role="inline_image",
            logical_name="telegram_openclaw_channel",
            source_url="https://rss.example.com/openclaw.png",
            mime_type="image/png",
            file_ext="png",
            byte_size=4,
            sha256="0f4636c78f65d3639ece5a064b5ae753e3408614a14fb18ab4d7540d2c248543",
            content_bytes=b"PNG!",
            metadata_json=None,
        )
        session.add_all([raw_snapshot, image_asset])
        session.flush()
        session.add(
            SavedSnapshotAssetRef(
                saved_snapshot_id=raw_snapshot.id,
                asset_id=image_asset.id,
                ref_kind="markdown_image",
                ref_token="ifa-asset://550e8400-e29b-41d4-a716-446655440010",
                alt_text="OpenClaw",
                ordinal=0,
                source_url=image_asset.source_url,
            )
        )

        export = export_saved_item_to_local_mirror(
            session,
            saved_item.id,
            trigger_kind=MIRROR_TRIGGER_MANUAL_EXPORT,
            root_dir=mirror_root,
        )
        session.commit()

    assert export.status == MIRROR_STATUS_SUCCEEDED
    article_dir_name = build_saved_item_archive_dir_name(
        "周报 #106 - OpenClaw 实践、搬家与生活的能量",
        "https://rss.example.com/posts/openclaw",
    )
    expected_image_name = build_local_mirror_asset_filename(
        "周报 #106 - OpenClaw 实践、搬家与生活的能量",
        "https://rss.example.com/posts/openclaw",
        image_asset,
        index=1,
    )
    image_file = mirror_root / article_dir_name / "assets" / expected_image_name
    raw_file = mirror_root / article_dir_name / build_local_mirror_markdown_filename(
        "周报 #106 - OpenClaw 实践、搬家与生活的能量",
        "raw",
    )

    assert image_file.exists()
    assert expected_image_name.endswith("_telegram_openclaw_channel.png")
    assert f"assets/{expected_image_name}" in raw_file.read_text(encoding="utf-8")

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_rebuild_local_mirror_from_db_removes_orphan_directories(client, tmp_path, monkeypatch) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    orphan_dir = mirror_root / "orphan-title--aaaaaaaa"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / ".infoflow").mkdir()
    (orphan_dir / ".infoflow" / "manifest.json").write_text('{"saved_item_id": 9999}', encoding="utf-8")

    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/rebuild"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    run_id = create_response.json()["capture"]["latest_run_id"]

    with get_session_factory()() as session:
        session.add(
            SavedSnapshot(
                saved_item_id=saved_item_id,
                capture_run_id=run_id,
                snapshot_kind=CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND,
                mime_type="text/markdown",
                body="# Raw\n",
            )
        )
        batch = rebuild_local_mirror_from_db(session, root_dir=mirror_root)
        session.commit()

        assert batch.trigger_kind == MIRROR_TRIGGER_FULL_REBUILD
        assert batch.status == MIRROR_STATUS_SUCCEEDED

    assert not orphan_dir.exists()
    article_dir = mirror_root / build_saved_item_archive_dir_name(
        "https://manual.example.com/rebuild",
        "https://manual.example.com/rebuild",
    )
    rebuilt_raw = article_dir / build_local_mirror_markdown_filename(
        "https://manual.example.com/rebuild",
        "raw",
    )
    assert rebuilt_raw.exists()

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_local_mirror_soft_remove_archives_and_restore_reactivates_item(client, tmp_path, monkeypatch) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/archive-restore", "title": "Archive Restore"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]
    article_dir_name = build_saved_item_archive_dir_name(
        "Archive Restore",
        "https://manual.example.com/archive-restore",
    )
    active_dir = mirror_root / article_dir_name
    archive_dir = mirror_root / ".archive" / article_dir_name
    assert active_dir.exists()

    remove_response = client.post(f"/api/knowledge/saved-items/{saved_item_id}/remove")
    assert remove_response.status_code == 200
    assert not active_dir.exists()
    assert archive_dir.exists()
    assert (archive_dir / ".infoflow" / "manifest.json").exists()

    with get_session_factory()() as session:
        latest_export = session.scalar(
            select(SavedItemMirrorExport)
            .where(SavedItemMirrorExport.saved_item_id == saved_item_id)
            .order_by(SavedItemMirrorExport.id.desc())
            .limit(1)
        )
        assert latest_export is not None
        assert latest_export.trigger_kind == MIRROR_TRIGGER_SOFT_REMOVE_ARCHIVE

    restore_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/archive-restore", "title": "Archive Restore"},
    )
    assert restore_response.status_code == 201
    assert restore_response.json()["id"] == saved_item_id
    assert active_dir.exists()
    assert not archive_dir.exists()

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_rebuild_local_mirror_keeps_removed_items_in_archive_and_skips_archive_root_cleanup(
    client,
    tmp_path,
    monkeypatch,
) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    active_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/rebuild-active", "title": "Rebuild Active"},
    )
    removed_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/rebuild-removed", "title": "Rebuild Removed"},
    )
    assert active_response.status_code == 201
    assert removed_response.status_code == 201
    active_saved_item_id = active_response.json()["id"]
    removed_saved_item_id = removed_response.json()["id"]
    assert client.post(f"/api/knowledge/saved-items/{removed_saved_item_id}/remove").status_code == 200

    orphan_active_dir = mirror_root / "orphan-active--aaaaaaaa"
    orphan_active_dir.mkdir(parents=True)
    (orphan_active_dir / ".infoflow").mkdir()
    (orphan_active_dir / ".infoflow" / "manifest.json").write_text('{"saved_item_id": 9999}', encoding="utf-8")
    orphan_archive_dir = mirror_root / ".archive" / "orphan-archive--bbbbbbbb"
    orphan_archive_dir.mkdir(parents=True)
    (orphan_archive_dir / ".infoflow").mkdir()
    (orphan_archive_dir / ".infoflow" / "manifest.json").write_text('{"saved_item_id": 9998}', encoding="utf-8")

    with get_session_factory()() as session:
        batch = rebuild_local_mirror_from_db(session, root_dir=mirror_root)
        session.commit()
        assert batch.status == MIRROR_STATUS_SUCCEEDED

    active_dir = mirror_root / build_saved_item_archive_dir_name(
        "Rebuild Active",
        "https://manual.example.com/rebuild-active",
    )
    removed_archive_dir = mirror_root / ".archive" / build_saved_item_archive_dir_name(
        "Rebuild Removed",
        "https://manual.example.com/rebuild-removed",
    )
    removed_active_dir = mirror_root / build_saved_item_archive_dir_name(
        "Rebuild Removed",
        "https://manual.example.com/rebuild-removed",
    )

    assert active_dir.exists()
    assert removed_archive_dir.exists()
    assert not removed_active_dir.exists()
    assert (mirror_root / ".archive").exists()
    assert not orphan_active_dir.exists()
    assert not orphan_archive_dir.exists()

    with get_session_factory()() as session:
        active_item = session.get(SavedItem, active_saved_item_id)
        removed_item = session.get(SavedItem, removed_saved_item_id)
        assert active_item is not None and active_item.removed_at is None
        assert removed_item is not None and removed_item.removed_at is not None

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_local_mirror_reexport_endpoint_writes_manual_export_and_returns_paths(client, tmp_path, monkeypatch) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/reexport"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    response = _post_local_mirror_export(client, saved_item_id)

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"] == saved_item_id
    assert payload["local_mirror"]["status"] == "succeeded"
    assert payload["local_mirror"]["written_file_count"] == 5
    article_dir = mirror_root / build_saved_item_archive_dir_name(
        "https://manual.example.com/reexport",
        "https://manual.example.com/reexport",
    )
    assert Path(payload["local_mirror"]["paths"]["knowledge_base_root"]) == mirror_root
    assert Path(payload["local_mirror"]["paths"]["article_dir"]) == article_dir
    assert Path(payload["local_mirror"]["paths"]["raw_markdown"]) == article_dir / build_local_mirror_markdown_filename(
        "https://manual.example.com/reexport",
        "raw",
    )
    assert Path(payload["local_mirror"]["paths"]["fit_markdown"]) == article_dir / build_local_mirror_markdown_filename(
        "https://manual.example.com/reexport",
        "fit",
    )
    assert Path(payload["local_mirror"]["paths"]["fit_image_reinserted_markdown"]) == article_dir / build_local_mirror_markdown_filename(
        "https://manual.example.com/reexport",
        "fit-images",
    )
    assert Path(payload["local_mirror"]["paths"]["manifest"]) == article_dir / ".infoflow" / "manifest.json"
    assert Path(payload["local_mirror"]["paths"]["markdown_assets_dir"]) == article_dir / "assets"
    assert Path(payload["local_mirror"]["paths"]["singlefile_html"]) == article_dir / build_local_mirror_singlefile_filename(
        "https://manual.example.com/reexport"
    )

    with get_session_factory()() as session:
        latest_export = session.scalar(
            select(SavedItemMirrorExport)
            .where(SavedItemMirrorExport.saved_item_id == saved_item_id)
            .order_by(SavedItemMirrorExport.id.desc())
            .limit(1)
        )
        assert latest_export is not None
        assert latest_export.trigger_kind == MIRROR_TRIGGER_MANUAL_EXPORT

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_local_mirror_reexport_endpoint_returns_404_for_missing_saved_item(client) -> None:
    response = _post_local_mirror_export(client, 9999)

    assert response.status_code == 404
    assert response.json()["detail"] == "Saved item not found"


def test_local_mirror_reexport_endpoint_records_failure_without_removing_archive(
    client,
    tmp_path,
    monkeypatch,
) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    monkeypatch.setenv("KNOWLEDGE_BASE_ROOT", str(mirror_root))
    reset_settings_cache()

    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/reexport-fail"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    def fail_clean_item_root_paths(paths):
        raise OSError("cannot clean mirror directory")

    monkeypatch.setattr("app.services.knowledge_mirror._clean_item_root_paths", fail_clean_item_root_paths)

    response = _post_local_mirror_export(client, saved_item_id)

    assert response.status_code == 201
    payload = response.json()
    assert payload["local_mirror"]["status"] == "failed"
    assert payload["local_mirror"]["latest_error_summary"] == "cannot clean mirror directory"
    assert payload["local_mirror"]["written_file_count"] == 0
    assert payload["snapshots"]["has_singlefile_html"] is True
    assert payload["snapshots"]["has_crawl4ai_raw_markdown"] is True
    assert payload["snapshots"]["has_crawl4ai_fit_markdown"] is True
    assert payload["snapshots"]["has_crawl4ai_fit_image_reinserted_markdown"] is True
    archive_artifacts = {artifact["artifact_kind"]: artifact for artifact in payload["archive_artifacts"]}
    assert archive_artifacts[SINGLEFILE_HTML_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_RAW_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_FIT_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"
    assert archive_artifacts[CRAWL4AI_FIT_IMAGE_REINSERTED_MARKDOWN_SNAPSHOT_KIND]["status"] == "available"

    os.environ.pop("KNOWLEDGE_BASE_ROOT", None)
    reset_settings_cache()


def test_export_saved_item_to_local_mirror_records_top_level_filesystem_failure(client, tmp_path, monkeypatch) -> None:
    mirror_root = tmp_path / "KnowledgeBase"
    create_response = _post_manual_url_capture(
        client,
        json={"url": "https://manual.example.com/mirror-fail"},
    )
    assert create_response.status_code == 201
    saved_item_id = create_response.json()["id"]

    def fail_clean_item_root_paths(paths):
        raise OSError("cannot clean mirror directory")

    monkeypatch.setattr("app.services.knowledge_mirror._clean_item_root_paths", fail_clean_item_root_paths)

    with get_session_factory()() as session:
        export = export_saved_item_to_local_mirror(
            session,
            saved_item_id,
            trigger_kind=MIRROR_TRIGGER_MANUAL_EXPORT,
            root_dir=mirror_root,
        )
        session.commit()

        assert export.status == "failed"
        assert export.error_summary == "cannot clean mirror directory"
