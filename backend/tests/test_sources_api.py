from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import os
from xml.etree import ElementTree

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import httpx
from sqlalchemy import create_engine, func, inspect, select

from app.core.config import reset_settings_cache
from app.db.models import (
    AppOperation,
    FeedItemHomepageFacet,
    FeedItemHomepageReadModel,
    FeedItemBehaviorEvent,
    InsightEngineArtifact,
    InsightEngineAttempt,
    InsightChannel,
    FeedItem,
    ItemState,
    SavedItem,
    Source,
    SourceChannelTendency,
    SourceFetch,
    SourceImportRun,
    SourceItemOccurrence,
    SourcePreferenceProfile,
)
from app.db.session import get_session_factory
from app.db.session import reset_session_state
from app.services import ai_provider
from app.services import feed_content_extraction
from app.services import operation_jobs
from app.services import source_deletion
from app.services import source_preview
from app.services import operations as operations_service
from app.services import source_sync


RSS_SAMPLE_ALPHA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Alpha Feed</title>
    <item>
      <guid>https://shared.example.com/posts/1</guid>
      <title>Shared Story</title>
      <link>https://shared.example.com/posts/1?utm_source=alpha</link>
      <description>Alpha summary</description>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>alpha-local-guid</guid>
      <title>Alpha Local Story</title>
      <link>https://alpha.example.com/posts/local</link>
      <description>Alpha local summary</description>
      <pubDate>Sat, 28 Mar 2026 09:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

RSS_SAMPLE_BETA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Beta Feed</title>
    <item>
      <guid>https://shared.example.com/posts/1</guid>
      <title>Shared Story</title>
      <link>https://shared.example.com/posts/1</link>
      <description>Beta summary</description>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>beta-local-guid</guid>
      <title>Same Title Different Source</title>
      <link>https://beta.example.com/posts/local</link>
      <description>Beta local summary</description>
      <pubDate>Sat, 28 Mar 2026 10:15:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

RSS_SAMPLE_GAMMA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Gamma Feed</title>
    <item>
      <guid>gamma-guid-1</guid>
      <title>Title Match Story</title>
      <link>https://gamma.example.com/posts/one</link>
      <description>Gamma summary</description>
      <pubDate>Sat, 28 Mar 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

RSS_SAMPLE_DELTA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Delta Feed</title>
    <item>
      <guid>delta-guid-99</guid>
      <title>Title Match Story</title>
      <link>https://delta.example.com/posts/two</link>
      <description>Delta summary</description>
      <pubDate>Sat, 28 Mar 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

RSS_SAMPLE_PREVIEW = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Preview Feed</title>
    <link>https://preview.example.com/</link>
    <description>Preview feed description</description>
    <language>en-us</language>
    <image>
      <url>https://preview.example.com/icon.png</url>
    </image>
    <item>
      <guid>preview-guid-1</guid>
      <title>Preview Story One</title>
      <link>https://preview.example.com/posts/1</link>
      <description>Preview summary one</description>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>preview-guid-2</guid>
      <title>Preview Story Two</title>
      <link>https://preview.example.com/posts/2</link>
      <description>Preview summary two</description>
      <pubDate>Sat, 28 Mar 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE_ICON = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Icon Feed</title>
  <link href="https://atom.example.com/" rel="alternate" />
  <icon>/atom-icon.png</icon>
  <logo>https://atom.example.com/atom-logo.png</logo>
  <entry>
    <id>atom-icon-1</id>
    <title>Atom Story</title>
    <link href="https://atom.example.com/posts/1" rel="alternate" />
    <updated>2026-03-28T08:00:00Z</updated>
    <summary>Atom summary</summary>
  </entry>
</feed>
"""

ATOM_SAMPLE_LOGO = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Logo Feed</title>
  <link href="https://atom-logo.example.com/" rel="alternate" />
  <logo>/brand-logo.png</logo>
  <entry>
    <id>atom-logo-1</id>
    <title>Atom Logo Story</title>
    <link href="https://atom-logo.example.com/posts/1" rel="alternate" />
    <updated>2026-03-28T08:00:00Z</updated>
    <summary>Atom logo summary</summary>
  </entry>
</feed>
"""

RSS_SAMPLE_SHORT_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Short Feed</title>
    <item>
      <guid>short-guid-1</guid>
      <title>Short Story Extraction</title>
      <link>https://short.example.com/posts/1</link>
      <description>Read more https://short.example.com/posts/1</description>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def build_rss_sample_with_item_count(count: int) -> str:
    items = "\n".join(
        (
            lambda hour, minute: f"""
    <item>
      <guid>https://bulk.example.com/posts/{index}</guid>
      <title>Bulk Story {index}</title>
      <link>https://bulk.example.com/posts/{index}</link>
      <description>{"Bulk summary text. " * 24}</description>
      <pubDate>Sat, 28 Mar 2026 {hour:02d}:{minute:02d}:00 GMT</pubDate>
    </item>"""
        )(8 + index // 60, index % 60)
        for index in range(count)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Bulk Feed</title>
{items}
  </channel>
</rss>
"""


def _preview_opml(client, payload: str, *, filename: str = "sources.opml"):
    return client.post(
        "/api/sources/imports/opml/preview",
        files={"file": (filename, payload.encode("utf-8"), "text/x-opml")},
    )


def _commit_all_opml_candidates(client, preview_body: dict):
    return client.post(
        "/api/sources/imports/opml/commit",
        json={
            "import_run_id": preview_body["import_run_id"],
            "default_ai_route": "general",
            "selected_candidates": [
                {"candidate_id": candidate["candidate_id"], "ai_route": "general"}
                for candidate in preview_body["candidates"]
            ],
        },
    )


def _import_opml_by_preview_commit(client, payload: str, *, filename: str = "sources.opml"):
    preview_response = _preview_opml(client, payload, filename=filename)
    assert preview_response.status_code == 201
    return _commit_all_opml_candidates(client, preview_response.json())


def test_create_list_get_and_patch_source(client) -> None:
    create_response = client.post(
        "/api/sources",
        json={
            "name": "InfoFlow Feed",
            "kind": "rss",
            "url": "HTTPS://Example.com/feed",
            "site_url": "https://example.com",
            "folder_path": "Tech / Blogs",
            "is_active": True,
        },
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["provenance"] == "manual"
    assert created["url"] == "https://example.com/feed"
    assert created["site_url"] == "https://example.com/"
    assert created["ai_route"] == "general"
    assert created["icon_url"] == "https://example.com/favicon.ico"
    assert created["icon_source"] == "source_site_favicon"
    assert created["icon_updated_at"] is not None
    assert created["created_from_import_run_id"] is None
    assert created["item_count"] == 0
    assert created["latest_fetch_is_available"] is None
    assert created["latest_fetch_status"] is None
    assert created["latest_fetch_http_status"] is None
    assert created["latest_fetch_error_message"] is None
    assert created["latest_fetch_items_seen_count"] is None
    assert created["latest_fetch_items_created_count"] is None
    assert created["latest_fetch_items_matched_count"] is None
    assert created["latest_fetch_items_invalid_count"] is None
    assert created["latest_fetch_started_at"] is None
    assert created["latest_fetch_finished_at"] is None

    list_response = client.get("/api/sources")
    assert list_response.status_code == 200
    sources = list_response.json()
    assert len(sources) == 1
    assert sources[0]["id"] == created["id"]
    assert sources[0]["item_count"] == 0
    assert sources[0]["latest_fetch_is_available"] is None
    assert sources[0]["latest_fetch_status"] is None

    detail_response = client.get(f"/api/sources/{created['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["name"] == "InfoFlow Feed"
    assert detail["item_count"] == 0
    assert detail["latest_fetch_is_available"] is None

    patch_response = client.patch(
        f"/api/sources/{created['id']}",
        json={
            "name": "InfoFlow Feed Updated",
            "url": "https://example.com/feed-updated",
            "site_url": None,
            "folder_path": "Tech / Curated",
            "is_active": False,
            "ai_route": "sensitive",
        },
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["name"] == "InfoFlow Feed Updated"
    assert patched["url"] == "https://example.com/feed-updated"
    assert patched["site_url"] is None
    assert patched["folder_path"] == "Tech / Curated"
    assert patched["is_active"] is False
    assert patched["ai_route"] == "sensitive"
    assert patched["item_count"] == 0
    assert patched["latest_fetch_is_available"] is None


def test_create_source_rejects_duplicate_by_kind_and_url(client) -> None:
    payload = {
        "name": "Primary Feed",
        "kind": "rss",
        "url": "https://example.com/feed",
        "site_url": "https://example.com",
        "folder_path": "Folder",
        "is_active": True,
    }

    first_response = client.post("/api/sources", json=payload)
    assert first_response.status_code == 201

    duplicate_response = client.post(
        "/api/sources",
        json={
            **payload,
            "name": "Duplicate Feed",
            "url": "https://example.com/feed#fragment",
        },
    )
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"]["source_id"] == first_response.json()["id"]
    with get_session_factory()() as session:
        sources = list(session.scalars(select(Source)))
        assert len(sources) == 1
        assert sources[0].name == "Primary Feed"
        assert sources[0].url == "https://example.com/feed"


def test_create_source_validation_error_rolls_back_without_source(client) -> None:
    response = client.post(
        "/api/sources",
        json={
            "name": "Invalid Feed",
            "kind": "rss",
            "url": "file:///tmp/feed.xml",
            "site_url": None,
            "folder_path": "Bad",
            "is_active": True,
        },
    )

    assert response.status_code == 400
    assert "http or https" in response.json()["detail"]
    with get_session_factory()() as session:
        assert session.scalar(select(func.count(Source.id))) == 0


def test_update_source_duplicate_conflict_rolls_back_partial_field_changes(client) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")

    response = client.patch(
        f"/api/sources/{beta_id}",
        json={
            "name": "Beta Should Roll Back",
            "url": "https://alpha.example.com/feed.xml",
            "folder_path": "Rolled Back",
            "is_active": False,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["source_id"] == alpha_id
    with get_session_factory()() as session:
        beta = session.get(Source, beta_id)
        assert beta is not None
        assert beta.name == "Beta"
        assert beta.url == "https://beta.example.com/feed.xml"
        assert beta.folder_path is None
        assert beta.is_active is True


def test_update_source_insight_routing_negative_paths_roll_back(client) -> None:
    channels_response = client.get("/api/settings/insight-channels")
    assert channels_response.status_code == 200
    source_id = create_source_for_sync(client, "Routed Source", "https://routed.example.com/feed.xml")

    valid_response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "ai_agents_frontier",
                    "tendency": 0.8,
                    "is_primary": True,
                    "notes": "Initial routing",
                }
            ],
        },
    )
    assert valid_response.status_code == 200

    missing_response = client.patch(
        "/api/sources/999999/insight-routing",
        json={"flow_policy": "normal", "channel_tendencies": []},
    )
    assert missing_response.status_code == 404

    duplicate_response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "mixed_high_frequency",
            "channel_tendencies": [
                {"channel_id": "ai_agents_frontier", "tendency": 0.8, "is_primary": True, "notes": "A"},
                {"channel_id": "ai_agents_frontier", "tendency": 0.4, "is_primary": False, "notes": "B"},
            ],
        },
    )
    assert duplicate_response.status_code == 400
    assert "Duplicate channel ids" in duplicate_response.json()["detail"]

    unknown_response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "high_frequency_brief",
            "channel_tendencies": [
                {"channel_id": "unknown_channel", "tendency": 0.5, "is_primary": True, "notes": "Unknown"}
            ],
        },
    )
    assert unknown_response.status_code == 400
    assert "not active or does not exist" in unknown_response.json()["detail"]

    with get_session_factory()() as session:
        source = session.get(Source, source_id)
        assert source is not None
        assert source.flow_policy == "normal"
        tendencies = list(session.scalars(select(SourceChannelTendency).where(SourceChannelTendency.source_id == source_id)))
        assert len(tendencies) == 1
        assert tendencies[0].channel_id == "ai_agents_frontier"
        assert tendencies[0].tendency == 0.8
        assert tendencies[0].is_primary is True
        assert tendencies[0].notes == "Initial routing"


def test_list_sources_projects_latest_fetch_status(client) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    gamma_id = create_source_for_sync(client, "Gamma", "https://gamma.example.com/feed.xml")

    with get_session_factory()() as session:
        alpha_item = FeedItem(
            original_url="https://alpha.example.com/posts/1",
            title="Alpha One",
            summary_text="Alpha One summary.",
            content_text="Alpha One content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        alpha_duplicate_item = FeedItem(
            original_url="https://alpha.example.com/posts/duplicate",
            title="Alpha Duplicate",
            summary_text="Alpha Duplicate summary.",
            content_text="Alpha Duplicate content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        beta_item = FeedItem(
            original_url="https://beta.example.com/posts/1",
            title="Beta One",
            summary_text="Beta One summary.",
            content_text="Beta One content.",
            content_text_source="feed_content",
            content_extraction_status="not_needed",
        )
        session.add_all([alpha_item, alpha_duplicate_item, beta_item])
        session.flush()
        session.add_all(
            [
                SourceFetch(
                    source_id=alpha_id,
                    requested_url="https://alpha.example.com/feed.xml",
                    status="error",
                    http_status=502,
                    error_message="temporary upstream failure",
                    items_seen_count=0,
                    items_created_count=0,
                    items_matched_count=0,
                    items_invalid_count=0,
                    fetch_started_at=datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc),
                    fetch_finished_at=datetime(2026, 3, 28, 8, 1, tzinfo=timezone.utc),
                ),
                SourceFetch(
                    source_id=beta_id,
                    requested_url="https://beta.example.com/feed.xml",
                    status="error",
                    http_status=404,
                    error_message="feed disappeared",
                    items_seen_count=0,
                    items_created_count=0,
                    items_matched_count=0,
                    items_invalid_count=0,
                    fetch_started_at=datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc),
                    fetch_finished_at=datetime(2026, 3, 28, 9, 1, tzinfo=timezone.utc),
                ),
                SourceFetch(
                    source_id=alpha_id,
                    requested_url="https://alpha.example.com/feed.xml",
                    status="success",
                    http_status=200,
                    items_seen_count=3,
                    items_created_count=1,
                    items_matched_count=2,
                    items_invalid_count=0,
                    fetch_started_at=datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc),
                    fetch_finished_at=datetime(2026, 3, 28, 10, 1, tzinfo=timezone.utc),
                ),
                SourceItemOccurrence(
                    source_id=alpha_id,
                    feed_item_id=alpha_item.id,
                    original_url=alpha_item.original_url,
                    match_rule="created",
                ),
                SourceItemOccurrence(
                    source_id=alpha_id,
                    feed_item_id=alpha_duplicate_item.id,
                    original_url=alpha_duplicate_item.original_url,
                    match_rule="created",
                ),
                SourceItemOccurrence(
                    source_id=alpha_id,
                    feed_item_id=alpha_duplicate_item.id,
                    original_url=f"{alpha_duplicate_item.original_url}?second=1",
                    match_rule="external_id",
                ),
                SourceItemOccurrence(
                    source_id=beta_id,
                    feed_item_id=beta_item.id,
                    original_url=beta_item.original_url,
                    match_rule="created",
                ),
            ]
        )
        session.commit()

    response = client.get("/api/sources")

    assert response.status_code == 200
    sources_by_id = {source["id"]: source for source in response.json()}
    assert sources_by_id[gamma_id]["item_count"] == 0
    assert sources_by_id[gamma_id]["latest_fetch_is_available"] is None
    assert sources_by_id[gamma_id]["latest_fetch_status"] is None
    assert sources_by_id[gamma_id]["latest_fetch_items_seen_count"] is None
    assert sources_by_id[gamma_id]["latest_fetch_items_created_count"] is None
    assert sources_by_id[gamma_id]["latest_fetch_items_matched_count"] is None
    assert sources_by_id[gamma_id]["latest_fetch_items_invalid_count"] is None

    alpha = sources_by_id[alpha_id]
    assert alpha["item_count"] == 2
    assert alpha["latest_fetch_is_available"] is True
    assert alpha["latest_fetch_status"] == "success"
    assert alpha["latest_fetch_http_status"] == 200
    assert alpha["latest_fetch_error_message"] is None
    assert alpha["latest_fetch_items_seen_count"] == 3
    assert alpha["latest_fetch_items_created_count"] == 1
    assert alpha["latest_fetch_items_matched_count"] == 2
    assert alpha["latest_fetch_items_invalid_count"] == 0
    assert alpha["latest_fetch_started_at"].startswith("2026-03-28T10:00:00")
    assert alpha["latest_fetch_finished_at"].startswith("2026-03-28T10:01:00")

    beta = sources_by_id[beta_id]
    assert beta["item_count"] == 1
    assert beta["latest_fetch_is_available"] is False
    assert beta["latest_fetch_status"] == "error"
    assert beta["latest_fetch_http_status"] == 404
    assert beta["latest_fetch_error_message"] == "feed disappeared"
    assert beta["latest_fetch_items_seen_count"] == 0
    assert beta["latest_fetch_items_created_count"] == 0
    assert beta["latest_fetch_items_matched_count"] == 0
    assert beta["latest_fetch_items_invalid_count"] == 0
    assert beta["latest_fetch_started_at"].startswith("2026-03-28T09:00:00")
    assert beta["latest_fetch_finished_at"].startswith("2026-03-28T09:01:00")

    detail_response = client.get(f"/api/sources/{alpha_id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["item_count"] == alpha["item_count"]
    assert detail["latest_fetch_is_available"] == alpha["latest_fetch_is_available"]
    assert detail["latest_fetch_items_created_count"] == alpha["latest_fetch_items_created_count"]

    patch_response = client.patch(
        f"/api/sources/{beta_id}",
        json={
            "name": "Beta Updated",
            "url": "https://beta.example.com/feed.xml",
            "site_url": "https://beta.example.com",
            "folder_path": None,
            "is_active": True,
            "ai_route": "general",
        },
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["item_count"] == beta["item_count"]
    assert patched["latest_fetch_is_available"] == beta["latest_fetch_is_available"]
    assert patched["latest_fetch_items_created_count"] == beta["latest_fetch_items_created_count"]


def test_list_all_source_fetches_returns_recent_fetches_with_source_info(client) -> None:
    empty_response = client.get("/api/sources/fetches")
    assert empty_response.status_code == 200
    assert empty_response.json() == []

    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")

    with get_session_factory()() as session:
        session.add_all(
            [
                SourceFetch(
                    source_id=alpha_id,
                    requested_url="https://alpha.example.com/feed.xml",
                    status="success",
                    http_status=200,
                    items_seen_count=2,
                    items_created_count=2,
                    items_matched_count=0,
                    items_invalid_count=0,
                    fetch_started_at=datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc),
                    fetch_finished_at=datetime(2026, 3, 28, 8, 1, tzinfo=timezone.utc),
                ),
                SourceFetch(
                    source_id=beta_id,
                    requested_url="https://beta.example.com/feed.xml",
                    status="error",
                    http_status=502,
                    error_message="upstream failed",
                    items_seen_count=0,
                    items_created_count=0,
                    items_matched_count=0,
                    items_invalid_count=0,
                    fetch_started_at=datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc),
                    fetch_finished_at=datetime(2026, 3, 28, 9, 1, tzinfo=timezone.utc),
                ),
                SourceFetch(
                    source_id=alpha_id,
                    requested_url="https://alpha.example.com/feed.xml",
                    status="not_modified",
                    http_status=304,
                    items_seen_count=0,
                    items_created_count=0,
                    items_matched_count=0,
                    items_invalid_count=0,
                    fetch_started_at=datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc),
                    fetch_finished_at=datetime(2026, 3, 28, 10, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()

    response = client.get("/api/sources/fetches?limit=2")

    assert response.status_code == 200
    fetches = response.json()
    assert [fetch["source_name"] for fetch in fetches] == ["Alpha", "Beta"]
    assert [fetch["status"] for fetch in fetches] == ["not_modified", "error"]
    assert fetches[0]["source_id"] == alpha_id
    assert fetches[0]["http_status"] == 304
    assert fetches[1]["source_id"] == beta_id
    assert fetches[1]["error_message"] == "upstream failed"


def test_source_category_management_renames_and_deletes_exact_folder_paths(client) -> None:
    fixtures = [
        ("Tech One", "https://example.com/tech-one.xml", "Tech"),
        ("Tech Two", "https://example.com/tech-two.xml", "Tech"),
        ("Tech Blog", "https://example.com/tech-blog.xml", "Tech / Blogs"),
        ("Other Feed", "https://example.com/other.xml", "Other"),
        ("Loose Feed", "https://example.com/loose.xml", None),
    ]
    for name, url, folder_path in fixtures:
        response = client.post(
            "/api/sources",
            json={
                "name": name,
                "kind": "rss",
                "url": url,
                "site_url": None,
                "folder_path": folder_path,
                "is_active": True,
            },
        )
        assert response.status_code == 201

    categories_response = client.get("/api/sources/categories")
    assert categories_response.status_code == 200
    assert categories_response.json() == [
        {"path": "Other", "source_count": 1},
        {"path": "Tech", "source_count": 2},
        {"path": "Tech / Blogs", "source_count": 1},
    ]

    rename_response = client.post(
        "/api/sources/categories/rename",
        json={"old_path": "Tech", "new_path": "AI"},
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["updated_source_count"] == 2
    assert rename_response.json()["categories"] == [
        {"path": "AI", "source_count": 2},
        {"path": "Other", "source_count": 1},
        {"path": "Tech / Blogs", "source_count": 1},
    ]

    sources_after_rename = client.get("/api/sources").json()
    folder_by_name = {source["name"]: source["folder_path"] for source in sources_after_rename}
    assert folder_by_name["Tech One"] == "AI"
    assert folder_by_name["Tech Two"] == "AI"
    assert folder_by_name["Tech Blog"] == "Tech / Blogs"
    assert folder_by_name["Loose Feed"] is None

    delete_response = client.post("/api/sources/categories/delete", json={"path": "Tech / Blogs"})
    assert delete_response.status_code == 200
    assert delete_response.json()["updated_source_count"] == 1
    assert delete_response.json()["categories"] == [
        {"path": "AI", "source_count": 2},
        {"path": "Other", "source_count": 1},
    ]

    sources_after_delete = client.get("/api/sources").json()
    folder_by_name = {source["name"]: source["folder_path"] for source in sources_after_delete}
    assert folder_by_name["Tech Blog"] is None
    assert folder_by_name["Tech One"] == "AI"


def test_source_category_management_rejects_missing_or_same_path(client) -> None:
    response = client.post(
        "/api/sources",
        json={
            "name": "Category Feed",
            "kind": "rss",
            "url": "https://example.com/category.xml",
            "site_url": None,
            "folder_path": "Category",
            "is_active": True,
        },
    )
    assert response.status_code == 201

    same_response = client.post(
        "/api/sources/categories/rename",
        json={"old_path": "Category", "new_path": "Category"},
    )
    assert same_response.status_code == 400

    missing_rename_response = client.post(
        "/api/sources/categories/rename",
        json={"old_path": "Missing", "new_path": "New"},
    )
    assert missing_rename_response.status_code == 400

    missing_delete_response = client.post("/api/sources/categories/delete", json={"path": "Missing"})
    assert missing_delete_response.status_code == 400


def test_source_delete_preview_counts_and_delete_preserves_saved_and_shared_items(client) -> None:
    fixture = _create_source_delete_fixture()

    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["can_delete"] is True
    assert preview["counts"]["source_fetches"] == 1
    assert preview["counts"]["source_item_occurrences"] == 3
    assert preview["counts"]["exclusive_unsaved_feed_items"] == 1
    assert preview["counts"]["saved_items_retained"] == 1
    assert preview["counts"]["active_saved_items_retained"] == 1
    assert preview["counts"]["shared_feed_items_retained"] == 1
    assert preview["counts"]["source_preference_profiles"] == 1
    assert preview["counts"]["source_channel_tendencies"] == 1
    assert preview["counts"]["feed_item_insight_artifacts"] == 1
    assert preview["counts"]["diagnostic_operations_to_detach"] == 1
    assert preview["counts"]["diagnostic_attempts_to_detach"] == 1

    mismatch_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": "stale-token"},
    )
    assert mismatch_response.status_code == 409
    assert mismatch_response.json()["detail"]["preview"]["source"]["id"] == fixture["source_id"]

    delete_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": preview["preview_token"]},
    )
    assert delete_response.status_code == 200
    body = delete_response.json()
    assert body["deleted"] is True
    assert body["retained_saved_items"] == 1

    with get_session_factory()() as session:
        assert session.get(Source, fixture["source_id"]) is None
        assert session.get(SourceFetch, fixture["fetch_id"]) is None
        assert session.get(SourcePreferenceProfile, fixture["profile_id"]) is None
        assert session.get(SourceChannelTendency, fixture["routing_id"]) is None
        assert session.get(FeedItem, fixture["exclusive_feed_item_id"]) is None
        assert session.get(InsightEngineArtifact, fixture["artifact_id"]) is None
        assert session.get(FeedItem, fixture["saved_feed_item_id"]) is not None
        saved_item = session.get(SavedItem, fixture["saved_item_id"])
        assert saved_item is not None
        assert saved_item.feed_item_id == fixture["saved_feed_item_id"]
        assert session.get(FeedItem, fixture["shared_feed_item_id"]) is not None
        assert session.get(Source, fixture["other_source_id"]) is not None
        remaining_occurrences = list(
            session.scalars(
                select(SourceItemOccurrence).where(SourceItemOccurrence.feed_item_id == fixture["shared_feed_item_id"])
            )
        )
        assert [occurrence.source_id for occurrence in remaining_occurrences] == [fixture["other_source_id"]]
        diagnostic_operation = session.get(AppOperation, fixture["diagnostic_operation_id"])
        assert diagnostic_operation is not None
        assert diagnostic_operation.source_id is None
        diagnostic_attempt = session.get(InsightEngineAttempt, fixture["diagnostic_attempt_id"])
        assert diagnostic_attempt is not None
        assert diagnostic_attempt.source_id is None
        assert diagnostic_attempt.feed_item_id is None


def test_source_delete_retains_removed_saved_items(client) -> None:
    fixture = _create_source_delete_fixture()
    now = datetime(2026, 4, 11, 8, 0, tzinfo=timezone.utc)
    with get_session_factory()() as session:
        source = session.get(Source, fixture["source_id"])
        assert source is not None
        removed_feed_item = _source_delete_feed_item(
            "Removed Saved Retained",
            "https://delete.example.com/removed-saved",
            now,
        )
        session.add(removed_feed_item)
        session.flush()
        session.add(
            SourceItemOccurrence(
                source_id=source.id,
                source_fetch_id=fixture["fetch_id"],
                feed_item_id=removed_feed_item.id,
                external_id="removed-saved",
                original_url=removed_feed_item.original_url,
                canonical_url=removed_feed_item.canonical_url,
                published_at=now,
                raw_entry_json={"title": removed_feed_item.title},
                match_rule="created",
                match_value=removed_feed_item.canonical_url,
            )
        )
        removed_saved_item = SavedItem(
            feed_item_id=removed_feed_item.id,
            origin_type="feed_saved",
            ai_route="general",
            title=removed_feed_item.title,
            canonical_url=removed_feed_item.canonical_url,
            source_name=source.name,
            source_app=source.site_url,
            saved_at=now,
            removed_at=now,
        )
        session.add(removed_saved_item)
        session.flush()
        removed_feed_item_id = removed_feed_item.id
        removed_saved_item_id = removed_saved_item.id
        session.commit()

    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["counts"]["saved_items_retained"] == 2
    assert preview["counts"]["active_saved_items_retained"] == 1
    assert preview["counts"]["removed_saved_items_retained"] == 1
    assert preview["counts"]["exclusive_unsaved_feed_items"] == 1

    delete_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": preview["preview_token"]},
    )
    assert delete_response.status_code == 200

    with get_session_factory()() as session:
        removed_saved_item = session.get(SavedItem, removed_saved_item_id)
        assert removed_saved_item is not None
        assert removed_saved_item.removed_at is not None
        assert session.get(FeedItem, removed_feed_item_id) is not None
        assert (
            session.scalar(
                select(func.count(SourceItemOccurrence.id)).where(
                    SourceItemOccurrence.source_id == fixture["source_id"],
                    SourceItemOccurrence.feed_item_id == removed_feed_item_id,
                )
            )
            == 0
        )


def test_source_delete_requires_confirmed_current_preview(client) -> None:
    fixture = _create_source_delete_fixture()
    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200

    response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": False, "preview_token": preview_response.json()["preview_token"]},
    )
    assert response.status_code == 400

    with get_session_factory()() as session:
        assert session.get(Source, fixture["source_id"]) is not None


def test_source_delete_rejects_active_sync_or_build_operations(client) -> None:
    fixture = _create_source_delete_fixture(active_operation=True)

    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["can_delete"] is False
    assert preview["blockers"][0]["operation_type"] == "source_sync"

    delete_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": preview["preview_token"]},
    )
    assert delete_response.status_code == 409
    assert delete_response.json()["detail"]["preview"]["can_delete"] is False

    with get_session_factory()() as session:
        assert session.get(Source, fixture["source_id"]) is not None


def test_source_delete_rejects_active_feed_item_artifact_build_operation(client) -> None:
    fixture = _create_source_delete_fixture()
    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="feed_item_insight_artifacts",
            operation_kind="global",
            status="running",
            source_id=None,
            scope_json={"scope": "global"},
            progress_json={},
            message="running artifact build",
        )
        session.add(operation)
        session.commit()

    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["can_delete"] is False
    assert preview["blockers"][0]["operation_type"] == "feed_item_insight_artifacts"

    delete_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": preview["preview_token"]},
    )
    assert delete_response.status_code == 409


def test_source_delete_acquires_global_and_feed_item_operation_mutexes(client, monkeypatch) -> None:
    fixture = _create_source_delete_fixture()
    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200
    acquired_keys: list[str] = []

    def record_mutex_key(session, mutex_key: str) -> None:
        del session
        acquired_keys.append(mutex_key)

    monkeypatch.setattr(source_deletion, "_acquire_operation_mutex_lock", record_mutex_key)
    delete_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": preview_response.json()["preview_token"]},
    )
    assert delete_response.status_code == 200
    assert acquired_keys[0] == "global"
    for feed_item_id in (
        fixture["exclusive_feed_item_id"],
        fixture["saved_feed_item_id"],
        fixture["shared_feed_item_id"],
    ):
        assert f"feed_item_extract:feed_item:{feed_item_id}" in acquired_keys
        assert f"feed_item_insight_artifact:feed_item:{feed_item_id}" in acquired_keys


def test_source_delete_reuses_locked_candidate_set_for_confirmed_delete(client, monkeypatch) -> None:
    fixture = _create_source_delete_fixture()
    preview_response = client.get(f"/api/sources/{fixture['source_id']}/delete-preview")
    assert preview_response.status_code == 200

    original_build_candidate_set = source_deletion._build_candidate_set
    build_call_count = 0

    def count_build_candidate_set(*args, **kwargs):
        nonlocal build_call_count
        build_call_count += 1
        return original_build_candidate_set(*args, **kwargs)

    monkeypatch.setattr(source_deletion, "_build_candidate_set", count_build_candidate_set)
    delete_response = client.request(
        "DELETE",
        f"/api/sources/{fixture['source_id']}",
        json={"confirm": True, "preview_token": preview_response.json()["preview_token"]},
    )

    assert delete_response.status_code == 200
    assert build_call_count == 1


def test_import_opml_creates_matches_and_marks_invalid_entries(client) -> None:
    existing_response = client.post(
        "/api/sources",
        json={
            "name": "Existing Feed",
            "kind": "rss",
            "url": "https://example.com/existing.xml",
            "site_url": "https://example.com",
            "folder_path": None,
            "is_active": True,
        },
    )
    assert existing_response.status_code == 201

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <body>
    <outline text="Tech">
      <outline text="Feed One" xmlUrl="https://example.com/feed-one.xml" htmlUrl="https://example.com/one" />
      <outline text="Feed Two" xmlUrl="https://example.com/feed-one.xml" htmlUrl="https://example.com/one" />
      <outline text="Existing Feed Duplicate" xmlUrl="https://example.com/existing.xml" htmlUrl="https://example.com/existing" />
      <outline text="Broken Feed" htmlUrl="https://example.com/broken" />
    </outline>
  </body>
</opml>
"""

    response = _import_opml_by_preview_commit(client, payload, filename="sources.opml")

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "sources.opml"
    assert body["total_entries"] == 4
    assert body["created_count"] == 1
    assert body["matched_existing_count"] == 2
    assert body["invalid_count"] == 1

    created_entry = next(item for item in body["results"] if item["status"] == "created")
    assert created_entry["folder_path"] == "Tech"

    matched_existing = [
        item for item in body["results"] if item["status"] == "matched_existing" and item["name"] == "Existing Feed"
    ]
    assert len(matched_existing) == 1
    assert matched_existing[0]["folder_path"] == "Tech"

    invalid_entry = next(item for item in body["results"] if item["status"] == "invalid")
    assert invalid_entry["name"] == "Broken Feed"

    list_response = client.get("/api/sources")
    assert list_response.status_code == 200
    sources = list_response.json()
    assert len(sources) == 2
    imported_source = next(item for item in sources if item["name"] == "Feed One")
    assert imported_source["provenance"] == "opml_import"
    assert imported_source["created_from_import_run_id"] == body["import_run_id"]


def test_import_opml_canonicalizes_rsshub_routes_instances_and_keeps_nested_folders(client) -> None:
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <body>
    <outline text="Parent">
      <outline text="Child">
        <outline text="Folo Route" xmlUrl="rsshub://x/y" htmlUrl="https://example.com/x" />
        <outline text="Legacy Route" xmlUrl="rsshub:///legacy/path" htmlUrl="https://example.com/legacy" />
        <outline text="Official Duplicate" xmlUrl="https://rsshub.app/x/y" htmlUrl="https://example.com/x" />
        <outline text="Local Duplicate" xmlUrl="http://localhost:1200/x/y" htmlUrl="https://example.com/x" />
        <outline text="Regular RSS" xmlUrl="https://example.com/rss.xml" htmlUrl="https://example.com" />
        <outline text="Bad Route" xmlUrl="rsshub://../secret" />
      </outline>
    </outline>
  </body>
</opml>
"""

    response = _import_opml_by_preview_commit(client, payload, filename="rsshub.opml")

    assert response.status_code == 200
    body = response.json()
    assert body["total_entries"] == 6
    assert body["created_count"] == 2
    assert body["matched_existing_count"] == 2
    assert body["invalid_count"] == 2

    route_results = [item for item in body["results"] if item["url"] == "rsshub://x/y"]
    assert [item["status"] for item in route_results] == ["created", "matched_existing", "matched_existing"]
    assert {item["input_url"] for item in route_results} == {
        "rsshub://x/y",
        "https://rsshub.app/x/y",
        "http://localhost:1200/x/y",
    }
    assert all(item["kind"] == "rsshub" for item in route_results)
    assert all(item["folder_path"] == "Parent / Child" for item in route_results)

    legacy_result = next(item for item in body["results"] if item["name"] == "Legacy Route")
    assert legacy_result["status"] == "invalid"
    assert "must not start with a slash" in legacy_result["message"]

    regular_result = next(item for item in body["results"] if item["name"] == "Regular RSS")
    assert regular_result["kind"] == "rss"
    assert regular_result["url"] == "https://example.com/rss.xml"

    invalid_result = next(item for item in body["results"] if item["name"] == "Bad Route")
    assert invalid_result["status"] == "invalid"
    assert invalid_result["name"] == "Bad Route"
    assert "path traversal" in invalid_result["message"]

    list_response = client.get("/api/sources")
    assert list_response.status_code == 200
    sources = list_response.json()
    assert len(sources) == 2
    assert sum(1 for item in sources if item["url"] == "rsshub://x/y") == 1


def test_legacy_single_step_opml_import_endpoint_is_removed(client) -> None:
    response = client.post(
        "/api/sources/imports/opml",
        files={"file": ("sources.opml", b"<opml></opml>", "text/x-opml")},
    )

    assert response.status_code == 404


def test_preview_opml_rejects_invalid_xml(client) -> None:
    response = client.post(
        "/api/sources/imports/opml/preview",
        files={"file": ("broken.opml", b"<opml><body><outline>", "text/x-opml")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "OPML payload is not valid XML"


def test_preview_opml_creates_import_run_without_sources(client) -> None:
    existing_response = client.post(
        "/api/sources",
        json={
            "name": "Existing Feed",
            "kind": "rss",
            "url": "https://example.com/existing.xml",
            "site_url": "https://example.com",
            "folder_path": None,
            "is_active": True,
            "ai_route": "sensitive",
        },
    )
    assert existing_response.status_code == 201

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <body>
    <outline text="Tech">
      <outline text="Ready Feed" xmlUrl="https://example.com/ready.xml" htmlUrl="https://example.com/ready" />
      <outline text="Ready Duplicate" xmlUrl="https://example.com/ready.xml" htmlUrl="https://example.com/ready" />
      <outline text="Existing Duplicate" xmlUrl="https://example.com/existing.xml" htmlUrl="https://example.com/existing" />
      <outline text="Broken Feed" htmlUrl="https://example.com/broken" />
    </outline>
  </body>
</opml>
"""

    response = client.post(
        "/api/sources/imports/opml/preview",
        files={"file": ("sources.opml", payload.encode("utf-8"), "text/x-opml")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "sources.opml"
    assert body["total_entries"] == 4
    assert body["ready_count"] == 1
    assert body["matched_existing_count"] == 1
    assert body["duplicate_in_file_count"] == 1
    assert body["invalid_count"] == 1

    candidates = body["candidates"]
    assert [candidate["status"] for candidate in candidates] == [
        "ready",
        "duplicate_in_file",
        "matched_existing",
        "invalid",
    ]
    assert candidates[0]["candidate_id"].startswith("opml:0000:")
    assert candidates[0]["selected_by_default"] is True
    assert candidates[1]["duplicate_of_candidate_id"] == candidates[0]["candidate_id"]
    assert candidates[2]["source_id"] == existing_response.json()["id"]
    assert candidates[2]["suggested_ai_route"] == "sensitive"
    assert all(candidate["folder_path"] == "Tech" for candidate in candidates)
    assert candidates[0]["icon_url"] == "https://example.com/favicon.ico"
    assert candidates[0]["icon_source"] == "opml_site_favicon"
    assert candidates[3]["icon_url"] == "https://example.com/favicon.ico"
    assert candidates[3]["icon_source"] == "opml_site_favicon"

    list_response = client.get("/api/sources")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    with get_session_factory()() as session:
        import_run = session.query(SourceImportRun).one()
        assert import_run.id == body["import_run_id"]
        assert import_run.filename == "sources.opml"
        assert import_run.raw_payload == payload
        assert import_run.summary_json["preview"]["ready_count"] == 1


def test_commit_opml_import_creates_only_selected_sources_with_ai_routes(client) -> None:
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <body>
    <outline text="Tech">
      <outline text="Default Route Feed" xmlUrl="https://example.com/default.xml" htmlUrl="https://example.com/default" />
      <outline text="Sensitive Route Feed" xmlUrl="https://example.com/sensitive.xml" htmlUrl="https://example.com/sensitive" />
      <outline text="Skipped Feed" xmlUrl="https://example.com/skipped.xml" htmlUrl="https://example.com/skipped" />
    </outline>
  </body>
</opml>
"""
    preview_response = client.post(
        "/api/sources/imports/opml/preview",
        files={"file": ("routes.opml", payload.encode("utf-8"), "text/x-opml")},
    )
    assert preview_response.status_code == 201
    preview_body = preview_response.json()
    candidates = preview_body["candidates"]

    commit_response = client.post(
        "/api/sources/imports/opml/commit",
        json={
            "import_run_id": preview_body["import_run_id"],
            "default_ai_route": "disabled",
            "selected_candidates": [
                {"candidate_id": candidates[0]["candidate_id"], "folder_path": None},
                {
                    "candidate_id": candidates[1]["candidate_id"],
                    "ai_route": "sensitive",
                    "folder_path": "Custom / Sensitive",
                },
            ],
        },
    )

    assert commit_response.status_code == 200
    body = commit_response.json()
    assert body["selected_count"] == 2
    assert body["skipped_count"] == 1
    assert body["created_count"] == 2
    assert body["matched_existing_count"] == 0
    assert body["invalid_count"] == 0
    assert [result["ai_route"] for result in body["results"]] == ["disabled", "sensitive"]
    assert [result["folder_path"] for result in body["results"]] == [None, "Custom / Sensitive"]
    assert [result["icon_source"] for result in body["results"]] == ["opml_site_favicon", "opml_site_favicon"]
    assert [result["candidate_id"] for result in body["results"]] == [
        candidates[0]["candidate_id"],
        candidates[1]["candidate_id"],
    ]

    sources = client.get("/api/sources").json()
    assert len(sources) == 2
    assert {source["name"] for source in sources} == {"Default Route Feed", "Sensitive Route Feed"}
    assert {source["name"]: source["ai_route"] for source in sources} == {
        "Default Route Feed": "disabled",
        "Sensitive Route Feed": "sensitive",
    }
    assert {source["name"]: source["folder_path"] for source in sources} == {
        "Default Route Feed": None,
        "Sensitive Route Feed": "Custom / Sensitive",
    }
    assert {source["name"]: source["icon_url"] for source in sources} == {
        "Default Route Feed": "https://example.com/favicon.ico",
        "Sensitive Route Feed": "https://example.com/favicon.ico",
    }


def test_commit_opml_import_is_idempotent_for_existing_sources(client) -> None:
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <body>
    <outline text="Parent">
      <outline text="Child">
        <outline text="First Feed" xmlUrl="https://example.com/first.xml" htmlUrl="https://example.com/first" />
        <outline text="Second Feed" xmlUrl="https://example.com/second.xml" htmlUrl="https://example.com/second" />
      </outline>
    </outline>
  </body>
</opml>
"""

    first_preview = client.post(
        "/api/sources/imports/opml/preview",
        files={"file": ("idempotent.opml", payload.encode("utf-8"), "text/x-opml")},
    ).json()
    first_commit = client.post(
        "/api/sources/imports/opml/commit",
        json={
            "import_run_id": first_preview["import_run_id"],
            "default_ai_route": "general",
            "selected_candidates": [
                {"candidate_id": candidate["candidate_id"]}
                for candidate in first_preview["candidates"]
            ],
        },
    )
    assert first_commit.status_code == 200
    assert first_commit.json()["created_count"] == 2

    second_preview_response = client.post(
        "/api/sources/imports/opml/preview",
        files={"file": ("idempotent.opml", payload.encode("utf-8"), "text/x-opml")},
    )
    assert second_preview_response.status_code == 201
    second_preview = second_preview_response.json()
    assert [candidate["status"] for candidate in second_preview["candidates"]] == [
        "matched_existing",
        "matched_existing",
    ]

    second_commit = client.post(
        "/api/sources/imports/opml/commit",
        json={
            "import_run_id": second_preview["import_run_id"],
            "default_ai_route": "sensitive",
            "selected_candidates": [
                {"candidate_id": candidate["candidate_id"], "ai_route": "sensitive"}
                for candidate in second_preview["candidates"]
            ],
        },
    )

    assert second_commit.status_code == 200
    body = second_commit.json()
    assert body["created_count"] == 0
    assert body["matched_existing_count"] == 2
    assert body["invalid_count"] == 0
    assert all(result["status"] == "matched_existing" for result in body["results"])
    assert all(result["folder_path"] == "Parent / Child" for result in body["results"])

    sources = client.get("/api/sources").json()
    assert len(sources) == 2
    assert {source["ai_route"] for source in sources} == {"general"}


def test_export_opml_preserves_sources_nested_folders_and_infoflow_metadata(client) -> None:
    agent_response = client.post(
        "/api/sources",
        json={
            "name": "Agent Feed",
            "kind": "rss",
            "url": "https://agent.example.com/feed.xml",
            "site_url": "https://agent.example.com",
            "folder_path": "AI / Agents",
            "ai_route": "sensitive",
            "icon_url": "https://agent.example.com/favicon.ico",
            "icon_source": "source_site_favicon",
        },
    )
    assert agent_response.status_code == 201
    agent_source_id = agent_response.json()["id"]
    rsshub_response = client.post(
        "/api/sources",
        json={
            "name": "RSSHub Archive",
            "kind": "rsshub",
            "url": "rsshub://archive/latest",
            "site_url": "https://archive.example.com",
            "folder_path": "AI / Archives",
            "is_active": False,
            "ai_route": "disabled",
        },
    )
    assert rsshub_response.status_code == 201
    rsshub_source_id = rsshub_response.json()["id"]

    with get_session_factory()() as session:
        agent_source = session.get(Source, agent_source_id)
        assert agent_source is not None
        agent_source.flow_policy = "high_frequency_brief"
        session.add(agent_source)
        session.commit()

    response = client.get("/api/sources/export/opml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-opml")
    assert response.headers["content-disposition"] == 'attachment; filename="infoflow-sources.opml"'
    root = ElementTree.fromstring(response.content)
    assert root.tag == "opml"
    assert root.attrib["version"] == "2.0"
    assert root.findtext("./head/title") == "InfoFlow Aggregator Sources"

    agent_outline = root.find("./body/outline[@text='AI']/outline[@text='Agents']/outline[@text='Agent Feed']")
    assert agent_outline is not None
    assert agent_outline.attrib["title"] == "Agent Feed"
    assert agent_outline.attrib["type"] == "rss"
    assert agent_outline.attrib["xmlUrl"] == "https://agent.example.com/feed.xml"
    assert agent_outline.attrib["htmlUrl"] == "https://agent.example.com/"
    assert agent_outline.attrib["infoflowSourceId"] == str(agent_source_id)
    assert agent_outline.attrib["infoflowKind"] == "rss"
    assert agent_outline.attrib["infoflowAiRoute"] == "sensitive"
    assert agent_outline.attrib["infoflowActive"] == "true"
    assert agent_outline.attrib["infoflowFlowPolicy"] == "high_frequency_brief"
    assert agent_outline.attrib["infoflowProvenance"] == "manual"
    assert agent_outline.attrib["infoflowIconUrl"] == "https://agent.example.com/favicon.ico"
    assert agent_outline.attrib["infoflowIconSource"] == "source_site_favicon"

    rsshub_outline = root.find("./body/outline[@text='AI']/outline[@text='Archives']/outline[@text='RSSHub Archive']")
    assert rsshub_outline is not None
    assert rsshub_outline.attrib["xmlUrl"] == "rsshub://archive/latest"
    assert rsshub_outline.attrib["infoflowSourceId"] == str(rsshub_source_id)
    assert rsshub_outline.attrib["infoflowKind"] == "rsshub"
    assert rsshub_outline.attrib["infoflowAiRoute"] == "disabled"
    assert rsshub_outline.attrib["infoflowActive"] == "false"
    assert rsshub_outline.attrib["infoflowFlowPolicy"] == "normal"

    preview_response = client.post(
        "/api/sources/imports/opml/preview",
        files={"file": ("infoflow-sources.opml", response.content, "text/x-opml")},
    )
    assert preview_response.status_code == 201
    preview = preview_response.json()
    assert preview["total_entries"] == 2
    candidates_by_name = {candidate["name"]: candidate for candidate in preview["candidates"]}
    assert candidates_by_name["Agent Feed"]["status"] == "matched_existing"
    assert candidates_by_name["Agent Feed"]["folder_path"] == "AI / Agents"
    assert candidates_by_name["Agent Feed"]["url"] == "https://agent.example.com/feed.xml"
    assert candidates_by_name["RSSHub Archive"]["status"] == "matched_existing"
    assert candidates_by_name["RSSHub Archive"]["folder_path"] == "AI / Archives"
    assert candidates_by_name["RSSHub Archive"]["url"] == "rsshub://archive/latest"


def test_alembic_upgrade_creates_v2_baseline_schema(sqlite_database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    os.environ["DATABASE_URL"] = sqlite_database_url
    reset_settings_cache()
    reset_session_state()
    engine = create_engine(sqlite_database_url)

    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        source_columns = {column["name"] for column in inspector.get_columns("sources")}
        import_columns = {column["name"] for column in inspector.get_columns("source_import_runs")}
        fetch_columns = {column["name"] for column in inspector.get_columns("source_fetches")}
        occurrence_columns = {column["name"] for column in inspector.get_columns("source_item_occurrences")}
        feed_item_column_map = {column["name"]: column for column in inspector.get_columns("feed_items")}
        feed_item_columns = set(feed_item_column_map)
        saved_item_column_map = {column["name"]: column for column in inspector.get_columns("saved_items")}
        saved_item_columns = set(saved_item_column_map)
        saved_item_asset_columns = {column["name"] for column in inspector.get_columns("saved_item_assets")}
        mirror_export_columns = {column["name"] for column in inspector.get_columns("saved_item_mirror_exports")}
        mirror_export_file_columns = {column["name"] for column in inspector.get_columns("saved_item_mirror_export_files")}
        source_indexes = {index["name"] for index in inspector.get_indexes("sources")}
        asset_indexes = {index["name"]: index for index in inspector.get_indexes("saved_item_assets")}

        assert {"provenance", "created_from_import_run_id", "import_metadata_json"} <= source_columns
        assert "ai_route" in source_columns
        assert {"icon_url", "icon_source", "icon_updated_at"} <= source_columns
        assert {"import_type", "filename", "raw_payload", "summary_json"} <= import_columns
        assert {"items_seen_count", "items_created_count", "items_matched_count", "items_invalid_count"} <= fetch_columns
        assert {"source_id", "source_fetch_id", "feed_item_id", "external_id", "match_rule"} <= occurrence_columns
        assert "source_id" not in feed_item_columns
        assert "external_id" not in feed_item_columns
        assert "raw_entry_json" not in feed_item_columns
        assert {
            "feed_content_text",
            "content_text_source",
            "content_extraction_status",
            "content_extraction_attempted_at",
            "content_extracted_at",
            "content_extraction_error",
            "content_extraction_debug_json",
        } <= feed_item_columns
        assert "insight_feedback_text" in feed_item_columns
        assert "canonical_url" in saved_item_columns
        assert saved_item_column_map["canonical_url"]["nullable"] is True
        assert {
            "origin_type",
            "ai_route",
            "original_filename",
            "mime_type",
            "source_asset_id",
            "processing_status",
            "processing_error",
            "removed_at",
        } <= saved_item_columns
        assert {"asset_key", "asset_kind", "content_bytes", "sha256"} <= saved_item_asset_columns
        assert "ix_sources_kind" not in source_indexes
        assert bool(asset_indexes["ix_saved_item_assets_asset_key"]["unique"]) is True
        assert {"source_capture_run_id", "requested_targets_json", "status"} <= mirror_export_columns
        assert {"mirror_export_id", "relative_path", "verification_status"} <= mirror_export_file_columns
    finally:
        engine.dispose()
        os.environ.pop("DATABASE_URL", None)
        reset_settings_cache()
        reset_session_state()


def test_alembic_revision_ids_fit_default_version_table() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    revision_ids = sorted(revision.revision for revision in script.walk_revisions())

    assert all(len(revision_id) <= 32 for revision_id in revision_ids)


def test_non_baseline_migrations_do_not_add_sqlite_only_timestamp_defaults() -> None:
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    baseline_file = "20260601_v3_0_0_baseline.py"

    for migration_path in versions_dir.glob("*.py"):
        if migration_path.name == baseline_file:
            continue
        assert "(CURRENT_TIMESTAMP)" not in migration_path.read_text(encoding="utf-8")


def test_preview_source_returns_metadata_and_entries_without_persisting(client, monkeypatch) -> None:
    install_transport(
        monkeypatch,
        {
            "https://preview.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_PREVIEW),
        },
    )

    response = client.post(
        "/api/sources/preview",
        json={
            "url": "https://preview.example.com/feed.xml",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["detected_kind"] == "rss"
    assert body["source"]["canonical_url"] == "https://preview.example.com/feed.xml"
    assert body["source"]["resolved_url"] == "https://preview.example.com/feed.xml"
    assert body["source"]["title"] == "Preview Feed"
    assert body["source"]["site_url"] == "https://preview.example.com/"
    assert body["source"]["description"] == "Preview feed description"
    assert body["source"]["language"] == "en-us"
    assert body["source"]["icon_url"] == "https://preview.example.com/icon.png"
    assert body["source"]["icon_source"] == "rss_channel_image"
    assert len(body["entries"]) == 2
    assert body["entries"][0]["title"] == "Preview Story One"
    assert body["entries"][0]["original_url"] == "https://preview.example.com/posts/1"
    assert body["diagnostics"]["requested_url"] == "https://preview.example.com/feed.xml"
    assert body["diagnostics"]["http_status"] == 200

    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 0
        assert session.query(SourceItemOccurrence).count() == 0
        assert session.query(Source).count() == 0


def test_preview_source_reports_atom_icon_before_logo_and_logo_fallback(client, monkeypatch) -> None:
    install_transport(
        monkeypatch,
        {
            "https://atom.example.com/feed.xml": httpx.Response(200, text=ATOM_SAMPLE_ICON),
            "https://atom-logo.example.com/feed.xml": httpx.Response(200, text=ATOM_SAMPLE_LOGO),
        },
    )

    icon_response = client.post("/api/sources/preview", json={"url": "https://atom.example.com/feed.xml"})
    assert icon_response.status_code == 200
    icon_body = icon_response.json()
    assert icon_body["source"]["icon_url"] == "https://atom.example.com/atom-icon.png"
    assert icon_body["source"]["icon_source"] == "atom_icon"

    logo_response = client.post("/api/sources/preview", json={"url": "https://atom-logo.example.com/feed.xml"})
    assert logo_response.status_code == 200
    logo_body = logo_response.json()
    assert logo_body["source"]["icon_url"] == "https://atom-logo.example.com/brand-logo.png"
    assert logo_body["source"]["icon_source"] == "atom_logo"


def test_preview_source_supports_rsshub_routes(client, monkeypatch) -> None:
    install_transport(
        monkeypatch,
        {
            "http://localhost:1200/test/path": httpx.Response(200, text=RSS_SAMPLE_PREVIEW),
        },
    )

    response = client.post(
        "/api/sources/preview",
        json={
            "url": "rsshub://test/path",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["detected_kind"] == "rsshub"
    assert body["source"]["canonical_url"] == "rsshub://test/path"
    assert body["source"]["resolved_url"] == "http://localhost:1200/test/path"


def test_feed_http_client_requests_identity_encoding() -> None:
    with source_sync.create_http_client() as client:
        assert client.headers["Accept-Encoding"] == "identity"


def test_preview_source_rejects_private_http_url(client) -> None:
    response = client.post("/api/sources/preview", json={"url": "http://127.0.0.1/feed.xml"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "source_resolve_failed"
    assert "non-public" in detail["message"]


def test_preview_rejects_invalid_rsshub_route_with_explainable_error(client) -> None:
    response = client.post(
        "/api/sources/preview",
        json={
            "url": "rsshub://https://evil.example/feed.xml",
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "source_resolve_failed"
    assert detail["canonical_url"] is None
    assert detail["resolved_url"] is None
    assert "full external URL" in detail["message"]


def test_preview_and_sync_use_database_rsshub_base_url(client, monkeypatch) -> None:
    settings_response = client.patch("/api/settings/rsshub", json={"rsshub_base_url": "https://rsshub.example"})
    assert settings_response.status_code == 200
    assert settings_response.json()["source"] == "db"

    install_transport(
        monkeypatch,
        {
            "https://rsshub.example/test/path": httpx.Response(200, text=RSS_SAMPLE_PREVIEW, headers={"ETag": "etag-rsshub"}),
        },
    )

    preview_response = client.post(
        "/api/sources/preview",
        json={
            "url": "rsshub://test/path",
        },
    )
    assert preview_response.status_code == 200
    preview_body = preview_response.json()
    assert preview_body["source"]["canonical_url"] == "rsshub://test/path"
    assert preview_body["source"]["resolved_url"] == "https://rsshub.example/test/path"

    create_response = client.post(
        "/api/sources",
        json={
            "name": "RSSHub Test",
            "kind": "rsshub",
            "url": "rsshub://test/path",
            "site_url": None,
            "folder_path": None,
            "is_active": True,
        },
    )
    assert create_response.status_code == 201
    source = create_response.json()
    assert source["url"] == "rsshub://test/path"

    sync_response = sync_source_with_operation(client, source["id"])
    assert sync_response.status_code == 200
    fetch = sync_response.json()["fetch"]
    assert fetch["status"] == "success"
    assert fetch["requested_url"] == "https://rsshub.example/test/path"
    assert fetch["etag"] == "etag-rsshub"


def test_create_source_rejects_legacy_rsshub_triple_slash_route(client) -> None:
    response = client.post(
        "/api/sources",
        json={
            "name": "Legacy RSSHub Route",
            "kind": "rsshub",
            "url": "rsshub:///test/path",
            "site_url": None,
            "folder_path": None,
            "is_active": True,
        },
    )

    assert response.status_code == 400
    assert "must not start with a slash" in response.json()["detail"]


def test_preview_source_reports_upstream_failures(client, monkeypatch) -> None:
    install_transport(
        monkeypatch,
        {
            "https://preview.example.com/feed.xml": httpx.Response(500, text="boom"),
        },
    )

    response = client.post(
        "/api/sources/preview",
        json={
            "url": "https://preview.example.com/feed.xml",
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "Preview fetch returned HTTP 500"
    assert response.json()["detail"]["failure_stage"] == "upstream_http_error"


def test_source_sync_retries_retryable_upstream_http_access_failures(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(503, text="temporarily unavailable", request=request)

    install_transport(monkeypatch, handler)

    response = sync_source_with_operation(client, source_id, artifact_limit=0)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["status"] == "error"
    assert body["fetch"]["http_status"] == 503
    assert body["fetch"]["error_message"] == "upstream_http_error: Unexpected HTTP status 503"
    assert len(requests_seen) == 3


def test_source_sync_retries_fetch_failed_access_failures(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        raise httpx.TimeoutException("timed out", request=request)

    install_transport(monkeypatch, handler)

    response = sync_source_with_operation(client, source_id, artifact_limit=0)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["status"] == "error"
    assert body["fetch"]["http_status"] is None
    assert body["fetch"]["error_message"] == "fetch_failed: timed out"
    assert len(requests_seen) == 3


def test_source_sync_does_not_retry_non_retryable_upstream_http_errors(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(400, text="bad request", request=request)

    install_transport(monkeypatch, handler)

    response = sync_source_with_operation(client, source_id, artifact_limit=0)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["status"] == "error"
    assert body["fetch"]["http_status"] == 400
    assert body["fetch"]["error_message"] == "upstream_http_error: Unexpected HTTP status 400"
    assert len(requests_seen) == 1


def test_single_source_sync_creates_fetch_items_and_occurrences(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA, headers={"ETag": "etag-alpha"})},
    )

    response = sync_source_with_operation(client, source_id)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["status"] == "success"
    assert body["fetch"]["http_status"] == 200
    assert body["fetch"]["etag"] == "etag-alpha"
    assert body["fetch"]["items_seen_count"] == 2
    assert body["fetch"]["items_created_count"] == 2
    assert body["fetch"]["items_matched_count"] == 0
    assert body["fetch"]["items_invalid_count"] == 0

    fetches = client.get(f"/api/sources/{source_id}/fetches").json()
    assert len(fetches) == 1
    assert fetches[0]["id"] == body["fetch"]["id"]

    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 2
        assert session.query(SourceItemOccurrence).count() == 2
        assert session.query(InsightEngineArtifact).count() == 0


def test_single_source_sync_caps_ingested_entries_to_latest_items(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Bulk", "https://bulk.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://bulk.example.com/feed.xml": httpx.Response(
                200,
                text=build_rss_sample_with_item_count(source_sync.SOURCE_SYNC_ITEM_LIMIT + 10),
            )
        },
    )

    response = sync_source_with_operation(client, source_id, artifact_limit=0)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["status"] == "success"
    assert body["fetch"]["items_seen_count"] == source_sync.SOURCE_SYNC_ITEM_LIMIT
    assert body["fetch"]["items_created_count"] == source_sync.SOURCE_SYNC_ITEM_LIMIT
    assert len(body["created_feed_item_ids"]) == source_sync.SOURCE_SYNC_ITEM_LIMIT
    with get_session_factory()() as session:
        titles = {item.title for item in session.query(FeedItem).all()}
        assert len(titles) == source_sync.SOURCE_SYNC_ITEM_LIMIT
        assert "Bulk Story 59" in titles
        assert "Bulk Story 0" not in titles
        assert session.query(SourceItemOccurrence).count() == source_sync.SOURCE_SYNC_ITEM_LIMIT


def test_source_sync_upgrades_icon_without_downgrading_existing_feed_icon(client, monkeypatch) -> None:
    upgrade_source_id = create_source_for_sync(client, "Preview", "https://preview.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://preview.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_PREVIEW)},
    )

    upgrade_response = sync_source_with_operation(client, upgrade_source_id, artifact_limit=0)
    assert upgrade_response.status_code == 200

    upgraded = client.get(f"/api/sources/{upgrade_source_id}").json()
    assert upgraded["icon_url"] == "https://preview.example.com/icon.png"
    assert upgraded["icon_source"] == "rss_channel_image"
    assert upgraded["icon_updated_at"] is not None

    stable_source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    with get_session_factory()() as session:
        source = session.get(Source, stable_source_id)
        assert source is not None
        source.icon_url = "https://alpha.example.com/feed-icon.png"
        source.icon_source = "rss_channel_image"
        session.add(source)
        session.commit()

    install_transport(
        monkeypatch,
        {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA)},
    )

    stable_response = sync_source_with_operation(client, stable_source_id, artifact_limit=0)
    assert stable_response.status_code == 200

    stable = client.get(f"/api/sources/{stable_source_id}").json()
    assert stable["icon_url"] == "https://alpha.example.com/feed-icon.png"
    assert stable["icon_source"] == "rss_channel_image"


def test_source_sync_generates_insight_engine_artifacts_only_for_created_items(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA, headers={"ETag": "etag-alpha"})},
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com/v1",
                    "model": "test-model",
                    "api_key": "sk-test-secret",
                }
            },
        },
    )
    assert settings_response.status_code == 200

    first_response = sync_source_with_operation(client, source_id)

    assert first_response.status_code == 200
    assert first_response.json()["fetch"]["items_created_count"] == 2
    assert first_response.json()["feed_item_insight_artifacts"] == {
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
    }
    with get_session_factory()() as session:
        visible_followup = (
            session.query(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
            .order_by(AppOperation.id.desc())
            .first()
        )
        assert visible_followup is not None
        assert visible_followup.status == "queued"

    followup = run_latest_source_sync_homepage_followup()
    assert followup.result_json["processed_feed_item_count"] == 2
    assert followup.result_json["artifact_rows_created"] == 8
    assert len(ai_requests_seen) == 2

    second_response = sync_source_with_operation(client, source_id)
    assert second_response.status_code == 200
    assert second_response.json()["fetch"]["items_created_count"] == 0
    assert len(ai_requests_seen) == 2
    assert all(request.url == httpx.URL("https://ai.example.com/v1/chat/completions") for request in ai_requests_seen)
    assert all(request.headers["authorization"] == "Bearer sk-test-secret" for request in ai_requests_seen)

    with get_session_factory()() as session:
        artifacts = session.query(InsightEngineArtifact).order_by(InsightEngineArtifact.feed_item_id, InsightEngineArtifact.artifact_kind).all()
        assert len(artifacts) == 8
        assert {artifact.model_name for artifact in artifacts} == {"openai-compatible/test-model"}
        assert {artifact.prompt_version for artifact in artifacts} == {"insight-engine-v2-routing-v1"}
        assert {artifact.artifact_kind for artifact in artifacts} == {
            "feed_item_summary_v1",
            "feed_item_topics_v1",
            "feed_item_ranking_v1",
            "feed_item_routing_v1",
        }


def test_source_sync_auto_insight_engine_artifact_default_processes_all_new_items(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Bulk", "https://bulk.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://bulk.example.com/feed.xml": httpx.Response(200, text=build_rss_sample_with_item_count(25))},
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com/v1",
                    "model": "test-model",
                    "api_key": "sk-test-secret",
                }
            },
        },
    )
    assert settings_response.status_code == 200

    response = sync_source_with_operation(client, source_id)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["items_created_count"] == 25
    assert len(body["created_feed_item_ids"]) == 25
    assert body["feed_item_insight_artifacts"] == {
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
    }
    followup = run_latest_source_sync_homepage_followup()
    assert followup.result_json["processed_feed_item_count"] == 25
    assert followup.result_json["artifact_rows_created"] == 100
    assert len(ai_requests_seen) == 25
    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 25
        assert session.query(InsightEngineArtifact).count() == 100


def test_source_sync_artifact_null_limit_processes_all_created_items_after_feed_cap(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Bulk", "https://bulk.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://bulk.example.com/feed.xml": httpx.Response(
                200,
                text=build_rss_sample_with_item_count(source_sync.SOURCE_SYNC_ITEM_LIMIT + 96),
            )
        },
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com/v1",
                    "model": "test-model",
                    "api_key": "sk-test-secret",
                }
            },
        },
    )
    assert settings_response.status_code == 200

    response = sync_source_with_operation(client, source_id, artifact_limit=None)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["items_created_count"] == source_sync.SOURCE_SYNC_ITEM_LIMIT
    assert len(body["created_feed_item_ids"]) == source_sync.SOURCE_SYNC_ITEM_LIMIT
    assert body["feed_item_insight_artifacts"] == {
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
    }
    followup = run_latest_source_sync_homepage_followup()
    assert followup.result_json["processed_feed_item_count"] == source_sync.SOURCE_SYNC_ITEM_LIMIT
    assert followup.result_json["artifact_rows_created"] == source_sync.SOURCE_SYNC_ITEM_LIMIT * 4
    assert len(ai_requests_seen) == source_sync.SOURCE_SYNC_ITEM_LIMIT
    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == source_sync.SOURCE_SYNC_ITEM_LIMIT
        assert session.query(InsightEngineArtifact).count() == source_sync.SOURCE_SYNC_ITEM_LIMIT * 4


def test_source_sync_can_skip_automatic_feed_item_insight_artifacts_with_zero_limit(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Bulk", "https://bulk.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://bulk.example.com/feed.xml": httpx.Response(200, text=build_rss_sample_with_item_count(3))},
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com/v1",
                    "model": "test-model",
                    "api_key": "sk-test-secret",
                }
            },
        },
    )
    assert settings_response.status_code == 200

    response = sync_source_with_operation(client, source_id, artifact_limit=0)

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["items_created_count"] == 3
    assert len(body["created_feed_item_ids"]) == 3
    assert body["feed_item_insight_artifacts"] == {
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
    }
    assert ai_requests_seen == []
    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 3
        assert session.query(InsightEngineArtifact).count() == 0
        assert (
            session.query(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
            .count()
            == 0
        )


def test_source_sync_does_not_auto_fill_old_missing_insight_artifacts(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Old Missing", "https://old-missing.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://old-missing.example.com/feed.xml": httpx.Response(200, text=build_rss_sample_with_item_count(2))},
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com/v1",
                    "model": "test-model",
                    "api_key": "sk-test-secret",
                }
            },
        },
    )
    assert settings_response.status_code == 200

    first_response = sync_source_with_operation(client, source_id, artifact_limit=0)
    assert first_response.status_code == 200
    assert first_response.json()["fetch"]["items_created_count"] == 2
    assert first_response.json()["feed_item_insight_artifacts"]["processed_feed_item_count"] == 0

    second_response = sync_source_with_operation(client, source_id)

    assert second_response.status_code == 200
    assert second_response.json()["fetch"]["items_created_count"] == 0
    assert second_response.json()["fetch"]["items_matched_count"] == 2
    assert second_response.json()["feed_item_insight_artifacts"] == {
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
    }
    assert ai_requests_seen == []
    with get_session_factory()() as session:
        assert session.query(InsightEngineArtifact).count() == 0
        assert (
            session.query(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
            .count()
            == 0
        )


def test_source_sync_routes_insight_engine_artifacts_by_source_ai_route(client, monkeypatch) -> None:
    general_source_id = create_source_for_sync(client, "General", "https://alpha.example.com/feed.xml")
    sensitive_source_id = create_source_for_sync(
        client,
        "Sensitive",
        "https://gamma.example.com/feed.xml",
        ai_route="sensitive",
    )
    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://gamma.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_GAMMA),
        },
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "deepseek",
                    "enabled": True,
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "deepseek_thinking": "enabled",
                    "api_key": "sk-general-secret",
                },
                "sensitive": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://openai.example.com/v1",
                    "model": "gpt-4.1-mini",
                    "api_key": "sk-sensitive-secret",
                },
            }
        },
    )
    assert settings_response.status_code == 200

    general_response = sync_source_with_operation(client, general_source_id)
    assert general_response.status_code == 200
    general_followup = run_latest_source_sync_homepage_followup()
    assert general_followup.result_json["processed_feed_item_count"] == 2

    sensitive_response = sync_source_with_operation(client, sensitive_source_id)
    assert sensitive_response.status_code == 200
    sensitive_followup = run_latest_source_sync_homepage_followup()
    assert sensitive_followup.result_json["processed_feed_item_count"] == 1

    assert general_response.json()["fetch"]["items_created_count"] == 2
    assert sensitive_response.json()["fetch"]["items_created_count"] == 1
    assert [request.url for request in ai_requests_seen].count(httpx.URL("https://api.deepseek.com/chat/completions")) == 2
    assert [request.url for request in ai_requests_seen].count(httpx.URL("https://openai.example.com/v1/chat/completions")) == 1
    assert [
        request.headers["authorization"]
        for request in ai_requests_seen
        if request.url.host == "api.deepseek.com"
    ] == ["Bearer sk-general-secret", "Bearer sk-general-secret"]
    assert [
        request.headers["authorization"]
        for request in ai_requests_seen
        if request.url.host == "openai.example.com"
    ] == ["Bearer sk-sensitive-secret"]
    assert [
        json.loads(request.content.decode("utf-8"))["thinking"]
        for request in ai_requests_seen
        if request.url.host == "api.deepseek.com"
    ] == [{"type": "enabled"}, {"type": "enabled"}]
    assert all(
        "thinking" not in json.loads(request.content.decode("utf-8"))
        for request in ai_requests_seen
        if request.url.host == "openai.example.com"
    )

    with get_session_factory()() as session:
        artifacts = session.query(InsightEngineArtifact).all()
        assert len(artifacts) == 12
        assert {artifact.model_name for artifact in artifacts} == {
            "deepseek/deepseek-v4-flash",
            "openai-compatible/gpt-4.1-mini",
        }


def test_source_sync_skips_insight_engine_artifacts_for_disabled_source_route(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Disabled", "https://alpha.example.com/feed.xml", ai_route="disabled")
    install_transport(monkeypatch, {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA)})
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(monkeypatch, build_ai_success_handler(ai_requests_seen))
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "deepseek",
                    "enabled": True,
                    "base_url": "https://deepseek.example.com",
                    "model": "deepseek-v4-flash",
                    "api_key": "sk-general-secret",
                }
            }
        },
    )
    assert settings_response.status_code == 200

    response = sync_source_with_operation(client, source_id)

    assert response.status_code == 200
    assert response.json()["fetch"]["status"] == "success"
    assert ai_requests_seen == []
    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 2
        assert session.query(InsightEngineArtifact).count() == 0


def test_source_sync_skips_insight_engine_artifact_writes_when_ai_returns_invalid_json(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(monkeypatch, {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA)})
    install_ai_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
            request=request,
        ),
    )
    settings_response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                "general": {
                    "provider": "openai-compatible",
                    "enabled": True,
                    "base_url": "https://ai.example.com",
                    "model": "test-model",
                    "api_key": "sk-test-secret",
                }
            },
        },
    )
    assert settings_response.status_code == 200

    response = sync_source_with_operation(client, source_id)

    assert response.status_code == 200
    assert response.json()["fetch"]["status"] == "success"
    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 2
        assert session.query(InsightEngineArtifact).count() == 0


def test_source_sync_preserves_feed_content_and_extracts_short_item_body(client, monkeypatch) -> None:
    extracted_body = "Short Story Extraction full article body. " * 12
    source_id = create_source_for_sync(client, "Short", "https://short.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://short.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SHORT_CONTENT),
            "https://short.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article>placeholder</article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )
    monkeypatch.setattr(feed_content_extraction, "_extract_text_with_trafilatura", lambda html, url: extracted_body)

    response = sync_source_with_operation(client, source_id)

    assert response.status_code == 200
    with get_session_factory()() as session:
        item = session.query(FeedItem).one()
        assert item.feed_content_text == "Read more https://short.example.com/posts/1"
        assert item.content_text == extracted_body.strip()
        assert item.content_text_source == "extracted_page"
        assert item.content_extraction_status == "succeeded"
        assert item.content_extracted_at is not None


def test_sync_returns_not_modified_and_reuses_cache_headers(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    requests_seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(dict(request.headers))
        if len(requests_seen) == 1:
            return httpx.Response(200, text=RSS_SAMPLE_ALPHA, headers={"ETag": "etag-alpha", "Last-Modified": "Sat, 28 Mar 2026 08:05:00 GMT"})
        return httpx.Response(304, headers={})

    install_transport(monkeypatch, handler)

    first = sync_source_with_operation(client, source_id, artifact_limit=0)
    assert first.status_code == 200
    second = sync_source_with_operation(client, source_id, artifact_limit=0)
    assert second.status_code == 200
    assert second.json()["fetch"]["status"] == "not_modified"
    assert second.json()["fetch"]["items_seen_count"] == 0

    cache_request_headers = next(headers for headers in reversed(requests_seen) if headers.get("if-none-match"))
    assert cache_request_headers["if-none-match"] == "etag-alpha"
    assert cache_request_headers["if-modified-since"] == "Sat, 28 Mar 2026 08:05:00 GMT"

    source_response = client.get(f"/api/sources/{source_id}")
    assert source_response.status_code == 200
    source = source_response.json()
    assert source["item_count"] == 2
    assert source["latest_fetch_is_available"] is True
    assert source["latest_fetch_status"] == "not_modified"
    assert source["latest_fetch_http_status"] == 304
    assert source["latest_fetch_items_seen_count"] == 0
    assert source["latest_fetch_items_created_count"] == 0
    assert source["latest_fetch_items_matched_count"] == 0
    assert source["latest_fetch_items_invalid_count"] == 0


def test_sync_ignores_cache_headers_when_source_has_no_occurrences(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    requests_seen: list[dict[str, str]] = []

    with get_session_factory()() as session:
        session.add(
            SourceFetch(
                source_id=source_id,
                requested_url="https://alpha.example.com/feed.xml",
                status="success",
                http_status=200,
                etag="etag-alpha",
                last_modified="Sat, 28 Mar 2026 08:05:00 GMT",
                items_seen_count=2,
                items_created_count=2,
                items_matched_count=0,
                items_invalid_count=0,
            )
        )
        session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://alpha.example.com/feed.xml"):
            requests_seen.append(dict(request.headers))
            return httpx.Response(200, text=RSS_SAMPLE_ALPHA, headers={"ETag": "etag-alpha-next"})
        return httpx.Response(404, text="not found", request=request)

    install_transport(monkeypatch, handler)

    response = sync_source_with_operation(client, source_id)

    assert response.status_code == 200
    assert response.json()["fetch"]["status"] == "success"
    assert response.json()["fetch"]["items_created_count"] == 2
    assert len(requests_seen) == 1
    assert "if-none-match" not in requests_seen[0]
    assert "if-modified-since" not in requests_seen[0]


def test_source_sync_operation_returns_single_source_result(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    install_transport(monkeypatch, {"https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA)})

    response = sync_source_with_operation(client, source_id, artifact_limit=0)

    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == source_id
    assert body["fetch"]["items_created_count"] == 2
    with get_session_factory()() as session:
        operation = session.scalar(
            select(AppOperation).where(AppOperation.operation_type == "source_sync").order_by(AppOperation.id.desc())
        )
    assert operation is not None
    assert operation.status == "completed"
    assert operation.operation_kind == "single"
    assert operation.result_json["source_id"] == source_id


def test_removed_source_sync_endpoint_is_not_supported(client) -> None:
    response = client.post("/api/sources/sync")

    assert response.status_code == 405


def test_bulk_sync_dedups_across_sources_and_by_title_published_at(client, monkeypatch) -> None:
    create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    create_source_for_sync(client, "Gamma", "https://gamma.example.com/feed.xml")
    create_source_for_sync(client, "Delta", "https://delta.example.com/feed.xml")

    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://beta.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_BETA),
            "https://gamma.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_GAMMA),
            "https://delta.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_DELTA),
        },
    )

    response = sync_all_sources_with_operation(client)

    assert response.status_code == 200
    body = response.json()
    assert body["source_count"] == 4
    assert body["success_count"] == 4
    assert body["error_count"] == 0

    with get_session_factory()() as session:
        assert session.query(FeedItem).count() == 4
        assert session.query(SourceItemOccurrence).count() == 6

        shared_story = session.query(FeedItem).filter(FeedItem.title == "Shared Story").one()
        shared_occurrences = (
            session.query(SourceItemOccurrence)
            .filter(SourceItemOccurrence.feed_item_id == shared_story.id)
            .order_by(SourceItemOccurrence.id.asc())
            .all()
        )
        assert len(shared_occurrences) == 2
        assert {occ.match_rule for occ in shared_occurrences} == {"created", "external_id"}

        title_match_story = session.query(FeedItem).filter(FeedItem.title == "Title Match Story").one()
        title_occurrences = (
            session.query(SourceItemOccurrence)
            .filter(SourceItemOccurrence.feed_item_id == title_match_story.id)
            .order_by(SourceItemOccurrence.id.asc())
            .all()
        )
        assert len(title_occurrences) == 2
        assert {occ.match_rule for occ in title_occurrences} == {"created", "title_published_at"}


def test_bulk_sync_reports_http_errors_without_aborting_other_sources(client, monkeypatch) -> None:
    create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    create_source_for_sync(client, "Broken", "https://broken.example.com/feed.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://broken.example.com/feed.xml"):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=RSS_SAMPLE_ALPHA)

    install_transport(monkeypatch, handler)

    response = sync_all_sources_with_operation(client)

    assert response.status_code == 200
    body = response.json()
    assert body["source_count"] == 2
    assert body["success_count"] == 1
    assert body["error_count"] == 1
    assert {item["fetch"]["status"] for item in body["results"]} == {"success", "error"}


def run_latest_source_sync_homepage_followup() -> AppOperation:
    with get_session_factory()() as session:
        operation = (
            session.query(AppOperation)
            .where(AppOperation.operation_type == "feed_item_insight_artifacts")
            .where(AppOperation.operation_kind == "source_sync_created_feed_items")
            .order_by(AppOperation.id.desc())
            .first()
        )
        assert operation is not None
        operation_id = operation.id

    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="sources-api-test-ai-lane",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
    )

    with get_session_factory()() as session:
        operation = session.get(AppOperation, operation_id)
        assert operation is not None
        assert operation.status == "completed", operation.error_message
        assert isinstance(operation.result_json, dict)
        return operation


def _create_source_delete_fixture(*, active_operation: bool = False) -> dict[str, int]:
    now = datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc)
    with get_session_factory()() as session:
        source = Source(
            name="Delete Target",
            kind="rss",
            url="https://delete.example.com/feed.xml",
            site_url="https://delete.example.com",
            folder_path="Cleanup",
            is_active=True,
            ai_route="general",
        )
        other_source = Source(
            name="Other Source",
            kind="rss",
            url="https://other-delete.example.com/feed.xml",
            site_url="https://other-delete.example.com",
            folder_path=None,
            is_active=True,
            ai_route="general",
        )
        session.add_all([source, other_source])
        session.flush()

        fetch = SourceFetch(
            source_id=source.id,
            requested_url=source.url,
            status="success",
            http_status=200,
            response_body="<rss>raw</rss>",
            items_seen_count=3,
            items_created_count=3,
            fetch_started_at=now,
            fetch_finished_at=now,
        )
        session.add(fetch)
        channel = InsightChannel(id="delete_preview", label="Delete Preview", description="Preview routing channel")
        session.add(channel)
        session.flush()

        profile = SourcePreferenceProfile(
            source_id=source.id,
            status="active",
            schema_version="source_preference_profile_v1",
            profile_version="delete-v1",
            subscription_reason="Cleanup test",
            good_article_criteria_json=[],
            bad_article_patterns_json=[],
            preferred_topics_json=[],
            ignored_topics_json=[],
            preferred_content_types_json=[],
            positive_example_item_ids_json=[],
            negative_example_item_ids_json=[],
            positive_example_summaries_json=[],
            negative_example_summaries_json=[],
            prompt_context_json={},
            prompt_context_stats_json={},
            source_type="user_edit",
        )
        routing = SourceChannelTendency(
            source_id=source.id,
            channel_id=channel.id,
            tendency=0.8,
            is_primary=True,
            notes="cleanup test",
        )
        session.add_all([profile, routing])
        session.flush()

        exclusive_item = _source_delete_feed_item("Exclusive unsaved", "https://delete.example.com/exclusive", now)
        saved_item_feed = _source_delete_feed_item("Saved retained", "https://delete.example.com/saved", now)
        shared_item = _source_delete_feed_item("Shared retained", "https://shared-delete.example.com/item", now)
        session.add_all([exclusive_item, saved_item_feed, shared_item])
        session.flush()

        session.add_all(
            [
                ItemState(feed_item_id=exclusive_item.id, status="unread"),
                ItemState(feed_item_id=saved_item_feed.id, status="saved", saved_at=now),
                ItemState(feed_item_id=shared_item.id, status="unread"),
            ]
        )
        session.add_all(
            [
                SourceItemOccurrence(
                    source_id=source.id,
                    source_fetch_id=fetch.id,
                    feed_item_id=exclusive_item.id,
                    external_id="exclusive",
                    original_url=exclusive_item.original_url,
                    canonical_url=exclusive_item.canonical_url,
                    published_at=now,
                    raw_entry_json={"title": exclusive_item.title},
                    match_rule="created",
                    match_value=exclusive_item.canonical_url,
                ),
                SourceItemOccurrence(
                    source_id=source.id,
                    source_fetch_id=fetch.id,
                    feed_item_id=saved_item_feed.id,
                    external_id="saved",
                    original_url=saved_item_feed.original_url,
                    canonical_url=saved_item_feed.canonical_url,
                    published_at=now,
                    raw_entry_json={"title": saved_item_feed.title},
                    match_rule="created",
                    match_value=saved_item_feed.canonical_url,
                ),
                SourceItemOccurrence(
                    source_id=source.id,
                    source_fetch_id=fetch.id,
                    feed_item_id=shared_item.id,
                    external_id="shared-source",
                    original_url=shared_item.original_url,
                    canonical_url=shared_item.canonical_url,
                    published_at=now,
                    raw_entry_json={"title": shared_item.title},
                    match_rule="created",
                    match_value=shared_item.canonical_url,
                ),
                SourceItemOccurrence(
                    source_id=other_source.id,
                    source_fetch_id=None,
                    feed_item_id=shared_item.id,
                    external_id="shared-other",
                    original_url=shared_item.original_url,
                    canonical_url=shared_item.canonical_url,
                    published_at=now,
                    raw_entry_json={"title": shared_item.title},
                    match_rule="canonical_url",
                    match_value=shared_item.canonical_url,
                ),
            ]
        )
        saved_item = SavedItem(
            feed_item_id=saved_item_feed.id,
            origin_type="feed_saved",
            ai_route="general",
            title=saved_item_feed.title,
            canonical_url=saved_item_feed.canonical_url,
            source_name=source.name,
            source_app=source.site_url,
            saved_at=now,
        )
        artifact = InsightEngineArtifact(
            feed_item_id=exclusive_item.id,
            artifact_kind="feed_item_summary_v1",
            model_name="test",
            prompt_version="test",
            artifact_payload={"summary": "delete me"},
        )
        read_model = FeedItemHomepageReadModel(
            feed_item_id=exclusive_item.id,
            card_json={"id": exclusive_item.id},
            read_model_version="test",
            source_id=source.id,
            source_name=source.name,
            source_category_id="Cleanup",
            source_category_label="Cleanup",
            state="unread",
            saved=False,
            not_interested=False,
            published_at=now,
            priority_score=0,
            low_priority_bucket=False,
            flow_lane="main",
            flow_lane_facet="main",
            search_text=exclusive_item.title,
        )
        behavior_event = FeedItemBehaviorEvent(
            feed_item_id=exclusive_item.id,
            event_type="browsed",
            event_payload_json={},
            source="test",
            created_at=now,
            recorded_at=now,
        )
        diagnostic_operation = AppOperation(
            operation_type="source_sync",
            operation_kind="single",
            status="completed",
            source_id=source.id,
            scope_json={"kind": "single", "source_id": source.id},
            progress_json={},
            message="completed diagnostic",
        )
        diagnostic_attempt = InsightEngineAttempt(
            feed_item_id=exclusive_item.id,
            source_id=source.id,
            ai_route="general",
            profile="general",
            provider="test",
            model="test",
            status="failed",
            stage="request",
            error_type="test",
            error_message="delete diagnostic",
            artifact_count=0,
        )
        session.add_all([saved_item, artifact, read_model, behavior_event, diagnostic_operation, diagnostic_attempt])
        session.flush()
        session.add(
            FeedItemHomepageFacet(
                feed_item_id=exclusive_item.id,
                facet_kind="source",
                facet_id=str(source.id),
                facet_label=source.name,
            )
        )

        active_operation_id = None
        if active_operation:
            active = AppOperation(
                operation_type="source_sync",
                operation_kind="single",
                status="running",
                source_id=source.id,
                scope_json={"kind": "single", "source_id": source.id},
                progress_json={},
                message="running source sync",
            )
            session.add(active)
            session.flush()
            active_operation_id = active.id

        fixture = {
            "source_id": source.id,
            "other_source_id": other_source.id,
            "fetch_id": fetch.id,
            "profile_id": profile.id,
            "routing_id": routing.id,
            "exclusive_feed_item_id": exclusive_item.id,
            "saved_feed_item_id": saved_item_feed.id,
            "shared_feed_item_id": shared_item.id,
            "saved_item_id": saved_item.id,
            "artifact_id": artifact.id,
            "diagnostic_operation_id": diagnostic_operation.id,
            "diagnostic_attempt_id": diagnostic_attempt.id,
        }
        if active_operation_id is not None:
            fixture["active_operation_id"] = active_operation_id
        session.commit()
        return fixture


def _source_delete_feed_item(title: str, url: str, published_at: datetime) -> FeedItem:
    return FeedItem(
        original_url=url,
        canonical_url=url,
        title=title,
        author_name=None,
        published_at=published_at,
        summary_text=f"{title} summary",
        feed_content_text=f"{title} feed content",
        content_text=f"{title} feed content",
        content_text_source="feed_content",
    )


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


class _OperationResultResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def sync_source_with_operation(client, source_id: int, *, artifact_limit="__missing__") -> _OperationResultResponse:
    payload = {"kind": "single", "source_id": source_id}
    if artifact_limit != "__missing__":
        payload["feed_item_insight_artifact_build_limit"] = artifact_limit
    operation = _start_and_load_source_sync_operation(client, payload)
    result = operation["result"]
    assert result["source_id"] == source_id
    return _OperationResultResponse(result)


def sync_all_sources_with_operation(client, *, artifact_limit="__missing__") -> _OperationResultResponse:
    payload = {"kind": "all"}
    if artifact_limit != "__missing__":
        payload["feed_item_insight_artifact_build_limit"] = artifact_limit
    operation = _start_and_load_source_sync_operation(client, payload)
    return _OperationResultResponse(operation["result"])


def _start_and_load_source_sync_operation(client, payload: dict) -> dict:
    response = client.post("/api/operations/source-sync", json=payload)
    assert response.status_code == 202
    operation_id = response.json()["id"]
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)
    operation = client.get(f"/api/operations/{operation_id}").json()
    assert operation["status"] in {"completed", "failed", "stopped"}
    assert operation["result"] is not None
    return operation


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
    monkeypatch.setattr(feed_content_extraction, "create_http_client", fake_create_http_client)
    monkeypatch.setattr(source_preview, "create_http_client", fake_create_http_client)


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
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 82}],
            "low_priority_bucket": False,
            "routing": {
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "ai_agents_frontier",
                "secondary_channels": [
                    {"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "list"},
                ],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.76,
                "reason_codes": ["source_channel_tendency_match", "cross_channel_tooling"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Matches source tendency and article focus.",
            },
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    return handler
