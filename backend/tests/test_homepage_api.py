from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from math import ceil
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import (
    AppOperation,
    InsightEngineArtifact,
    FeedItem,
    FeedItemBehaviorEvent,
    FeedItemHomepageReadModel,
    FeedItemStructuredFeedback,
    InsightEngineAttempt,
    ItemState,
    Source,
    SourceChannelTendency,
    SourceItemOccurrence,
)
from app.db.session import get_session_factory
from app.schemas.operations import FeedItemInsightArtifactBatchOperationCreate
from app.services import ai_provider
from app.services import homepage as homepage_service
from app.services import insight_engine_artifacts as insight_engine_artifacts_service
from app.services import feed_content_extraction
from app.services import operation_jobs
from app.services import operations as operations_service
from app.services import source_sync
from app.services.app_settings import resolve_ai_provider_config_for_source_route
from app.services.insight_engine_request_builder import (
    MAX_INSIGHT_ENGINE_INPUT_CHARS,
    build_insight_engine_request_payload,
    load_insight_engine_request_context,
)
from app.services.homepage_read_model import (
    mark_homepage_read_models_stale,
    rebuild_homepage_read_models_for_feed_items,
)


RSS_SAMPLE_ALPHA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Alpha Feed</title>
    <item>
      <guid>https://shared.example.com/posts/1</guid>
      <title>Shared Story</title>
      <link>https://alpha.example.com/posts/1?ref=alpha</link>
      <description>Alpha summary</description>
      <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">Alpha content</content:encoded>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
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
      <link>https://beta.example.com/posts/1</link>
      <description>Beta summary</description>
      <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">Beta content</content:encoded>
      <pubDate>Sat, 28 Mar 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

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


def test_homepage_openapi_schema_exposes_read_model_and_missing_artifact_fields(client) -> None:
    schema = client.get("/api/openapi.json").json()
    components = schema["components"]["schemas"]

    bootstrap_properties = components["HomepageBootstrapRead"]["properties"]
    items_properties = components["HomepageItemsRead"]["properties"]
    missing_candidates_properties = components["FeedItemInsightArtifactMissingCandidatesResponse"]["properties"]
    missing_candidate_properties = components["FeedItemInsightArtifactMissingCandidateRead"]["properties"]

    for properties in (bootstrap_properties, items_properties):
        assert "read_model_status" in properties
        assert "snapshot_token" in properties
        assert "snapshot_expires_at" in properties

    assert "total_count" in missing_candidates_properties
    assert "has_more" in missing_candidates_properties
    assert "candidates" in missing_candidates_properties
    assert "profile" in missing_candidate_properties
    assert "provider" in missing_candidate_properties


def test_homepage_cards_use_same_canonical_item_and_latest_occurrence_source_metadata(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml", folder_path="Tech/Feeds")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml", folder_path="Products/Betas")
    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://beta.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_BETA),
        },
    )

    assert sync_source_with_operation(client, alpha_id, artifact_limit=0).status_code == 200
    assert sync_source_with_operation(client, beta_id, artifact_limit=0).status_code == 200

    with get_session_factory()() as session:
        artifact_count = session.scalar(select(func.count()).select_from(InsightEngineArtifact))
        assert artifact_count == 0

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    cards = _homepage_items(response)
    assert len(cards) == 1

    card = cards[0]
    assert card["source_id"] == beta_id
    assert card["source_name"] == "Beta"
    assert card["original_url"] == "https://beta.example.com/posts/1"
    assert card["canonical_url"] == "https://alpha.example.com/posts/1?ref=alpha"
    assert card["summary_text"] == "<p>Alpha summary</p>"
    assert card["state"]["status"] == "unread"
    assert card["topic_labels"]
    assert card["priority_score"] == 38
    assert card["priority_band"] == "low"
    assert card["summary_source"] == "canonical_summary"
    assert card["topic_source"] == "source_folder"
    assert card["priority_source"] == "backend_scoring_v1"
    assert card["artifact_generated_at"] is None
    assert [(reason["code"], reason["weight"]) for reason in card["priority_reasons"]] == [
        ("has_extracted_content", 5),
        ("item_age_hours", -17),
    ]


def test_homepage_card_serializes_valid_routing_artifact(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Artifact Routing Source",
            kind="rss",
            url="https://artifact-routing.example.com/feed.xml",
            site_url="https://artifact-routing.example.com",
            folder_path="Routing/Artifact",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://artifact-routing.example.com/posts/1",
            canonical_url="https://artifact-routing.example.com/posts/1",
            title="Artifact Routed Story",
            published_at=datetime(2026, 3, 28, 9, 0, 0, tzinfo=timezone.utc),
            summary_text="Artifact routed summary",
            content_text="Artifact routed content",
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
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts_service.FEED_ITEM_ROUTING_ARTIFACT_KIND,
                model_name="test/model",
                prompt_version="insight-engine-v2-routing-v1",
                artifact_payload={
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
                    "notes": "AI route",
                    "source": "insight_engine_artifact",
                },
            )
        )
        item_id = item.id
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["primary_channel_id"] == "ai_agents_frontier"
    assert card["secondary_channels"] == [
        {"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "list"}
    ]
    assert card["reading_depth"] == "medium"
    assert card["flow_lane"] == "main"
    assert card["routing_confidence"] == 0.82
    assert card["routing_source"] == "insight_engine_artifact"
    assert card["routing_reason_codes"] == ["source_channel_tendency_match"]
    assert card["promotion_reason_codes"] == []
    assert card["isolation_reason_codes"] == []
    assert card["source_policy_applied"] == "normal"
    assert card["routing_notes"] == "AI route"


def test_homepage_routing_uses_user_channel_feedback(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="User Corrected Routing Source",
            kind="rss",
            url="https://user-corrected-routing.example.com/feed.xml",
            site_url="https://user-corrected-routing.example.com",
            folder_path="Routing/User",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://user-corrected-routing.example.com/posts/1",
            canonical_url="https://user-corrected-routing.example.com/posts/1",
            title="User Corrected Routed Story",
            published_at=datetime(2026, 3, 28, 9, 30, 0, tzinfo=timezone.utc),
            summary_text="User corrected routed summary",
            content_text="User corrected routed content",
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
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts_service.FEED_ITEM_ROUTING_ARTIFACT_KIND,
                model_name="test/model",
                prompt_version="insight-engine-v2-routing-v1",
                artifact_payload={
                    "schema_version": "feed_item_routing_v1",
                    "primary_channel_id": "tcp_artificial_muscle_core",
                    "secondary_channels": [{"channel_id": "cryptocurrency", "confidence": 0.4, "display_mode": "list"}],
                    "reading_depth": "medium",
                    "flow_lane": "main",
                    "confidence": 0.82,
                    "reason_codes": ["source_channel_tendency_match"],
                    "promotion_reason_codes": [],
                    "isolation_reason_codes": [],
                    "source_policy_applied": "normal",
                    "notes": "AI route",
                    "source": "insight_engine_artifact",
                },
            )
        )
        item_id = item.id
        session.commit()

    feedback_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "primary_channel_correction": "ai_agents_frontier",
                "channel_add_ids": ["open_source_tools_practice"],
                "channel_remove_ids": ["cryptocurrency"],
                "reading_depth_correction": "deep",
                "flow_lane_correction": "needs_review",
                "channel_correction_notes": "Correct to AI agent content.",
            }
        },
    )
    assert feedback_response.status_code == 200

    homepage_response = client.get("/api/homepage/items")
    assert homepage_response.status_code == 200
    item = _card_by_id(_homepage_items(homepage_response), item_id)
    assert item["primary_channel_id"] == "ai_agents_frontier"
    assert item["secondary_channels"] == [
        {"channel_id": "open_source_tools_practice", "confidence": 1.0, "display_mode": "list"}
    ]
    assert item["reading_depth"] == "deep"
    assert item["flow_lane"] == "needs_review"
    assert item["routing_confidence"] == 0.82
    assert item["routing_source"] == "user_corrected"
    assert item["routing_reason_codes"] == [
        "source_channel_tendency_match",
        "manual_override_user_corrected",
        "user_channel_correction_hint",
    ]
    assert item["routing_notes"] == "Correct to AI agent content."
    assert "channel_feedback" not in item


def test_homepage_routing_overlay_preserves_existing_secondary_confidence(
    client,
) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    assert (
        client.patch(
            "/api/settings/insight-channels/academic_horizon",
            json={
                "enabled": False,
                "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
            },
        ).status_code
        == 200
    )

    with get_session_factory()() as session:
        source = Source(
            name="Filtered Overlay Routing Source",
            kind="rss",
            url="https://filtered-overlay-routing.example.com/feed.xml",
            site_url="https://filtered-overlay-routing.example.com",
            folder_path="Routing/Overlay",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://filtered-overlay-routing.example.com/posts/1",
            canonical_url="https://filtered-overlay-routing.example.com/posts/1",
            title="Filtered Overlay Routed Story",
            published_at=datetime(2026, 3, 28, 9, 45, 0, tzinfo=timezone.utc),
            summary_text="Filtered overlay routed summary",
            content_text="Filtered overlay routed content",
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
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts_service.FEED_ITEM_ROUTING_ARTIFACT_KIND,
                model_name="test/model",
                prompt_version="insight-engine-v2-routing-v1",
                artifact_payload={
                    "schema_version": "feed_item_routing_v1",
                    "primary_channel_id": "ai_agents_frontier",
                    "secondary_channels": [
                        {"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "tag_only"},
                        {"channel_id": "cryptocurrency", "confidence": 0.33, "display_mode": "tag_only"},
                    ],
                    "reading_depth": "medium",
                    "flow_lane": "main",
                    "confidence": 0.82,
                    "reason_codes": ["source_channel_tendency_match"],
                    "promotion_reason_codes": [],
                    "isolation_reason_codes": [],
                    "source_policy_applied": "normal",
                    "notes": "AI route",
                    "source": "insight_engine_artifact",
                },
            )
        )
        item_id = item.id
        session.commit()

    feedback_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "channel_add_ids": ["tcp_artificial_muscle_core"],
                "channel_remove_ids": ["cryptocurrency"],
            }
        },
    )
    assert feedback_response.status_code == 200

    homepage_response = client.get("/api/homepage/items")
    assert homepage_response.status_code == 200
    item = _card_by_id(_homepage_items(homepage_response), item_id)
    assert item["secondary_channels"] == [
        {"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "tag_only"},
        {"channel_id": "tcp_artificial_muscle_core", "confidence": 1.0, "display_mode": "list"},
    ]
    assert item["routing_source"] == "user_corrected"


def test_homepage_routing_overlay_removes_current_primary_channel(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Remove Primary Routing Source",
            kind="rss",
            url="https://remove-primary-routing.example.com/feed.xml",
            site_url="https://remove-primary-routing.example.com",
            folder_path="Routing/RemovePrimary",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://remove-primary-routing.example.com/posts/1",
            canonical_url="https://remove-primary-routing.example.com/posts/1",
            title="Remove Primary Routed Story",
            published_at=datetime(2026, 3, 28, 9, 50, 0, tzinfo=timezone.utc),
            summary_text="Remove primary routed summary",
            content_text="Remove primary routed content",
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
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts_service.FEED_ITEM_ROUTING_ARTIFACT_KIND,
                model_name="test/model",
                prompt_version="insight-engine-v2-routing-v1",
                artifact_payload={
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
                    "notes": "AI route",
                    "source": "insight_engine_artifact",
                },
            )
        )
        item_id = item.id
        session.commit()

    feedback_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "channel_remove_ids": ["ai_agents_frontier"],
                "channel_correction_notes": "This does not belong in the current primary channel.",
            }
        },
    )
    assert feedback_response.status_code == 200

    homepage_response = client.get("/api/homepage/items")
    assert homepage_response.status_code == 200
    item = _card_by_id(_homepage_items(homepage_response), item_id)
    assert item["primary_channel_id"] is None
    assert item["secondary_channels"] == [
        {"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "list"}
    ]
    assert item["routing_source"] == "user_corrected"


def test_homepage_cards_source_scope_returns_all_source_items_even_when_channel_filtered(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Scoped Source",
            kind="rss",
            url="https://scoped-source.example.com/feed.xml",
            site_url="https://scoped-source.example.com",
            folder_path="Scoped/Source",
        )
        session.add(source)
        session.flush()
        first_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Source item in requested channel",
            path="source-channel-match",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime(2026, 3, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        _create_homepage_routed_item(
            session,
            source=source,
            title="Source item outside requested channel",
            path="source-channel-miss",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
            published_at=datetime(2026, 3, 28, 9, 0, 0, tzinfo=timezone.utc),
        )
        other_source = Source(
            name="Other Scoped Source",
            kind="rss",
            url="https://other-scoped-source.example.com/feed.xml",
            site_url="https://other-scoped-source.example.com",
            folder_path="Scoped/Other",
        )
        session.add(other_source)
        session.flush()
        _create_homepage_routed_item(
            session,
            source=other_source,
            title="Other source item in requested channel",
            path="other-source-channel-match",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
        )
        source_id = source.id
        session.commit()

    response = client.get(f"/api/homepage/items?source_id={source_id}&channel_id=ai_agents_frontier")
    assert response.status_code == 200
    assert {item["id"] for item in _homepage_items(response)} == {first_item_id}


def test_homepage_cards_source_category_scope_uses_uncategorized_label_and_ignores_channel(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        uncategorized_source = Source(
            name="Uncategorized Scoped Source",
            kind="rss",
            url="https://uncategorized-scoped.example.com/feed.xml",
            site_url="https://uncategorized-scoped.example.com",
            folder_path=None,
        )
        categorized_source = Source(
            name="Categorized Scoped Source",
            kind="rss",
            url="https://categorized-scoped.example.com/feed.xml",
            site_url="https://categorized-scoped.example.com",
            folder_path="Scoped/Category",
        )
        session.add_all([uncategorized_source, categorized_source])
        session.flush()
        uncategorized_item_id = _create_homepage_routed_item(
            session,
            source=uncategorized_source,
            title="Uncategorized item outside requested channel",
            path="uncategorized-channel-miss",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
        )
        _create_homepage_routed_item(
            session,
            source=categorized_source,
            title="Categorized item in requested channel",
            path="categorized-channel-match",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    response = client.get(
        "/api/homepage/items",
        params={"source_category": "__empty__"},
    )
    assert response.status_code == 200
    assert {item["id"] for item in _homepage_items(response)} == {uncategorized_item_id}


def test_homepage_cards_channel_scope_filters_by_primary_or_secondary(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Channel Scoped Source",
            kind="rss",
            url="https://channel-scoped.example.com/feed.xml",
            site_url="https://channel-scoped.example.com",
            folder_path="Scoped/Channel",
        )
        session.add(source)
        session.flush()
        primary_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Primary channel match",
            path="primary-channel-match",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        secondary_list_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Secondary list channel match",
            path="secondary-list-channel-match",
            primary_channel_id="cryptocurrency",
            secondary_channels=[
                {"channel_id": "ai_agents_frontier", "confidence": 0.6, "display_mode": "list"}
            ],
            reading_depth="medium",
            flow_lane="main",
        )
        tag_only_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Secondary tag-only channel miss",
            path="secondary-tag-only-channel-miss",
            primary_channel_id="cryptocurrency",
            secondary_channels=[
                {"channel_id": "ai_agents_frontier", "confidence": 0.6, "display_mode": "tag_only"}
            ],
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    response = client.get("/api/homepage/items?channel_id=ai_agents_frontier")
    assert response.status_code == 200
    items = _homepage_items(response)
    item_ids = {item["id"] for item in items}
    assert item_ids == {primary_item_id, secondary_list_item_id, tag_only_item_id}
    assert all(
        item["primary_channel_id"] == "ai_agents_frontier"
        or any(ch["channel_id"] == "ai_agents_frontier" for ch in item["secondary_channels"])
        for item in items
    )


def test_homepage_cards_flow_lane_scope_filters_brief_pool(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Flow Scoped Source",
            kind="rss",
            url="https://flow-scoped.example.com/feed.xml",
            site_url="https://flow-scoped.example.com",
            folder_path="Scoped/Flow",
        )
        session.add(source)
        session.flush()
        brief_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Brief pool item",
            path="brief-pool-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="light",
            flow_lane="brief_pool",
        )
        main_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Main lane item",
            path="main-lane-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    response = client.get("/api/homepage/items?flow_lane=brief_pool")
    assert response.status_code == 200
    item_ids = {item["id"] for item in _homepage_items(response)}
    assert item_ids == {brief_item_id}
    assert main_item_id not in item_ids


def test_homepage_cards_low_priority_flow_lane_includes_backend_scoring_bucket(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Low Priority Flow Scoped Source",
            kind="rss",
            url="https://low-priority-flow-scoped.example.com/feed.xml",
            site_url="https://low-priority-flow-scoped.example.com",
            folder_path="Scoped/Flow",
        )
        session.add(source)
        session.flush()
        explicit_low_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Explicit low priority lane item",
            path="explicit-low-priority-lane-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="light",
            flow_lane="low_priority",
        )
        scored_low_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Scored low priority main lane item",
            path="scored-low-priority-main-lane-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        main_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Main lane item",
            path="main-lane-low-priority-filter-miss",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime.now(timezone.utc),
        )
        session.add(
            InsightEngineArtifact(
                feed_item_id=scored_low_item_id,
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts_service.FEED_ITEM_RANKING_ARTIFACT_KIND,
                model_name="test/model",
                prompt_version="insight-engine-v2-ranking-v1",
                artifact_payload={
                    "priority_score": 99,
                    "priority_band": "high",
                    "priority_reasons": [{"code": "thin_content", "label": "Thin content", "weight": 100}],
                    "low_priority_bucket": False,
                },
            )
        )
        session.commit()

    settings_response = client.patch(
        "/api/settings/insight-engine",
        json={"auto_low_priority_archiving_enabled": True, "low_priority_score_threshold": 50},
    )
    assert settings_response.status_code == 200

    response = client.get("/api/homepage/items?flow_lane=low_priority")
    assert response.status_code == 200
    items = _homepage_items(response)
    item_ids = {item["id"] for item in items}
    assert item_ids == {explicit_low_item_id, scored_low_item_id}
    assert main_item_id not in item_ids

    scored_low_item = _card_by_id(items, scored_low_item_id)
    assert scored_low_item["flow_lane"] == "main"
    assert scored_low_item["low_priority_bucket"] is True
    assert scored_low_item["low_priority_bucket_reason"] == "auto_score_threshold"


def test_homepage_cards_reading_depth_scope_filters_deep(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Depth Scoped Source",
            kind="rss",
            url="https://depth-scoped.example.com/feed.xml",
            site_url="https://depth-scoped.example.com",
            folder_path="Scoped/Depth",
        )
        session.add(source)
        session.flush()
        deep_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Deep item",
            path="deep-item",
            primary_channel_id="tcp_artificial_muscle_core",
            reading_depth="deep",
            flow_lane="main",
        )
        medium_item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Medium item",
            path="medium-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    response = client.get("/api/homepage/items?reading_depth=deep")
    assert response.status_code == 200
    item_ids = {item["id"] for item in _homepage_items(response)}
    assert item_ids == {deep_item_id}
    assert medium_item_id not in item_ids


def test_homepage_bootstrap_returns_summary_and_default_first_window(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Bootstrap Window Source",
            kind="rss",
            url="https://bootstrap-window.example.com/feed.xml",
            site_url="https://bootstrap-window.example.com",
            folder_path="Scoped/Bootstrap",
        )
        session.add(source)
        session.flush()
        item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Bootstrap window item {index:02d}",
                path=f"bootstrap-window-{index:02d}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=index),
            )
            for index in range(35)
        ]
        session.commit()

    response = client.get("/api/homepage/bootstrap")
    assert response.status_code == 200
    payload = response.json()
    assert payload["page_size"] == 30
    assert payload["summary"]["total_count"] == 35
    assert payload["summary"]["unread_count"] == 35
    assert payload["summary"]["query_signature"] == payload["query_signature"]
    assert len(payload["items"]) == 30
    assert payload["has_more"] is True
    assert payload["next_cursor"]
    assert "content_text" not in payload["items"][0]

    first_window_ids = {item["id"] for item in payload["items"]}
    assert first_window_ids.issubset(set(item_ids))

    next_response = client.get("/api/homepage/items", params={"cursor": payload["next_cursor"]})
    assert next_response.status_code == 200
    next_payload = next_response.json()
    assert next_payload["page_size"] == 30
    assert len(next_payload["items"]) == 5
    assert next_payload["has_more"] is False
    assert next_payload["next_cursor"] is None
    assert first_window_ids.isdisjoint({item["id"] for item in next_payload["items"]})


def test_homepage_bootstrap_builds_cards_once(client, monkeypatch) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Single Build Source",
            kind="rss",
            url="https://single-build.example.com/feed.xml",
            site_url="https://single-build.example.com",
            folder_path="Single/Build",
        )
        session.add(source)
        session.flush()
        _create_homepage_routed_item(
            session,
            source=source,
            title="Single build item",
            path="single-build-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    original_build_cards = homepage_service._build_homepage_cards
    call_count = 0

    def counting_build_cards(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_build_cards(*args, **kwargs)

    monkeypatch.setattr(homepage_service, "_build_homepage_cards", counting_build_cards)

    response = client.get("/api/homepage/bootstrap")

    assert response.status_code == 200
    assert call_count == 1


def test_homepage_read_model_fresh_paths_do_not_call_live_projection(client, monkeypatch) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        source = Source(
            name="Read Model Fresh",
            kind="rss",
            url="https://read-model-fresh.example.com/feed.xml",
            site_url="https://read-model-fresh.example.com",
            folder_path="ReadModel/Fresh",
        )
        session.add(source)
        session.flush()
        first_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model AI main",
            path="read-model-ai-main",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc),
        )
        second_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model crypto brief",
            path="read-model-crypto-brief",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
            published_at=datetime(2026, 3, 28, 11, 0, tzinfo=timezone.utc),
        )
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, [first_id, second_id])
        session.commit()

    def fail_live_projection(*args, **kwargs):
        raise AssertionError("_build_homepage_cards should not be called for fresh homepage read model")

    monkeypatch.setattr(homepage_service, "_build_homepage_cards", fail_live_projection)

    bootstrap_response = client.get("/api/homepage/bootstrap", params={"channel_id": "ai_agents_frontier"})
    assert bootstrap_response.status_code == 200
    bootstrap = bootstrap_response.json()
    assert bootstrap["read_model_status"]["state"] == "fresh"
    assert bootstrap["summary"]["read_model_status"]["state"] == "fresh"
    assert [item["id"] for item in bootstrap["items"]] == [first_id]
    assert bootstrap["summary"]["total_count"] == 1
    assert {facet["id"]: facet["count"] for facet in bootstrap["summary"]["channel_counts"]} == {
        "ai_agents_frontier": 1,
        "cryptocurrency": 1,
    }

    summary_response = client.get("/api/homepage/summary", params={"channel_id": "ai_agents_frontier"})
    assert summary_response.status_code == 200
    assert summary_response.json()["read_model_status"]["state"] == "fresh"

    items_response = client.get("/api/homepage/items", params={"channel_id": "ai_agents_frontier"})
    assert items_response.status_code == 200
    assert items_response.json()["read_model_status"]["state"] == "fresh"
    assert [item["id"] for item in _homepage_items(items_response)] == [first_id]

    search_response = client.get("/api/homepage/items", params={"search_text": "ai_agents_frontier"})
    assert search_response.status_code == 200
    assert search_response.json()["read_model_status"]["state"] == "fresh"
    assert _homepage_items(search_response) == []


def test_homepage_read_model_matches_live_state_counts_and_literal_search(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Read Model Parity",
            kind="rss",
            url="https://read-model-parity.example.com/feed.xml",
            site_url="https://read-model-parity.example.com",
            folder_path="ReadModel/Parity",
        )
        session.add(source)
        session.flush()
        unread_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model parity unread",
            path="read-model-parity-unread",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        saved_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model parity saved",
            path="read-model-parity-saved",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        read_full_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model parity read full",
            path="read-model-parity-read-full",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        percent_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model parity 100% literal",
            path="read-model-parity-percent",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        now = datetime(2026, 3, 28, 13, 0, tzinfo=timezone.utc)
        saved_state = session.scalar(select(ItemState).where(ItemState.feed_item_id == saved_id))
        assert saved_state is not None
        saved_state.status = "saved"
        saved_state.saved_at = now
        read_full_state = session.scalar(select(ItemState).where(ItemState.feed_item_id == read_full_id))
        assert read_full_state is not None
        read_full_state.status = "read_full"
        read_full_state.read_full_at = now
        session.commit()

    summary_fields = ("total_count", "unread_count", "saved_count", "not_interested_count")
    live_summaries = {}
    for state in ("saved", "read_full", "unread"):
        response = client.get("/api/homepage/summary", params={"state": state})
        assert response.status_code == 200
        payload = response.json()
        assert payload["read_model_status"]["state"] == "fallback_live"
        live_summaries[state] = {field: payload[field] for field in summary_fields}

    live_search_response = client.get("/api/homepage/items", params={"search_text": "%"})
    assert live_search_response.status_code == 200
    assert [item["id"] for item in _homepage_items(live_search_response)] == [percent_id]

    with get_session_factory()() as session:
        rebuild_homepage_read_models_for_feed_items(session, [unread_id, saved_id, read_full_id, percent_id])
        session.commit()

    for state in ("saved", "read_full", "unread"):
        response = client.get("/api/homepage/summary", params={"state": state})
        assert response.status_code == 200
        payload = response.json()
        assert payload["read_model_status"]["state"] == "fresh"
        assert {field: payload[field] for field in summary_fields} == live_summaries[state]

    fresh_search_response = client.get("/api/homepage/items", params={"search_text": "%"})
    assert fresh_search_response.status_code == 200
    assert fresh_search_response.json()["read_model_status"]["state"] == "fresh"
    assert [item["id"] for item in _homepage_items(fresh_search_response)] == [percent_id]


def test_homepage_read_model_missing_returns_fallback_live_status(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Read Model Missing",
            kind="rss",
            url="https://read-model-missing.example.com/feed.xml",
            site_url="https://read-model-missing.example.com",
            folder_path="ReadModel/Missing",
        )
        session.add(source)
        session.flush()
        _create_homepage_routed_item(
            session,
            source=source,
            title="Read model missing item",
            path="read-model-missing-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_model_status"]["state"] == "fallback_live"
    assert payload["read_model_status"]["missing_count"] == 1
    assert payload["read_model_status"]["stale_count"] == 0


def test_homepage_read_model_stale_returns_fallback_live_status(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Read Model Stale",
            kind="rss",
            url="https://read-model-stale.example.com/feed.xml",
            site_url="https://read-model-stale.example.com",
            folder_path="ReadModel/Stale",
        )
        session.add(source)
        session.flush()
        item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model stale item",
            path="read-model-stale-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, [item_id])
        mark_homepage_read_models_stale(session, feed_item_ids=[item_id], reason="test_stale")
        session.commit()

    response = client.get("/api/homepage/summary")
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_model_status"]["state"] == "fallback_live"
    assert payload["read_model_status"]["missing_count"] == 0
    assert payload["read_model_status"]["stale_count"] == 1


def test_homepage_read_model_stale_when_new_source_occurrence_is_newer_than_rebuild(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Read Model Occurrence A",
            kind="rss",
            url="https://read-model-occurrence-a.example.com/feed.xml",
            site_url="https://read-model-occurrence-a.example.com",
            folder_path="ReadModel/OccurrenceA",
        )
        newer_source = Source(
            name="Read Model Occurrence B",
            kind="rss",
            url="https://read-model-occurrence-b.example.com/feed.xml",
            site_url="https://read-model-occurrence-b.example.com",
            folder_path="ReadModel/OccurrenceB",
        )
        session.add_all([source, newer_source])
        session.flush()
        item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model occurrence item",
            path="read-model-occurrence-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, [item_id])
        row = session.get(FeedItemHomepageReadModel, item_id)
        assert row is not None
        item = session.get(FeedItem, item_id)
        assert item is not None
        session.add(
            SourceItemOccurrence(
                source_id=newer_source.id,
                feed_item_id=item.id,
                external_id=item.canonical_url,
                original_url="https://read-model-occurrence-b.example.com/posts/newer",
                canonical_url=item.canonical_url,
                published_at=item.published_at,
                match_rule="canonical_url",
                match_value=item.canonical_url,
                created_at=row.rebuilt_at + timedelta(seconds=1),
            )
        )
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_model_status"]["state"] == "fallback_live"
    assert payload["read_model_status"]["missing_count"] == 0
    assert payload["read_model_status"]["stale_count"] == 1


def test_reader_state_mutation_refreshes_homepage_read_model(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="Read Model State",
            kind="rss",
            url="https://read-model-state.example.com/feed.xml",
            site_url="https://read-model-state.example.com",
            folder_path="ReadModel/State",
        )
        session.add(source)
        session.flush()
        item_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Read model state item",
            path="read-model-state-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, [item_id])
        session.commit()

    response = client.post(f"/api/reader/items/{item_id}/mark_saved", json={})
    assert response.status_code == 200

    with get_session_factory()() as session:
        row = session.get(FeedItemHomepageReadModel, item_id)
        assert row is not None
        assert row.state == "saved"
        assert row.saved is True
        assert row.card_json["state"]["status"] == "saved"


def test_homepage_bootstrap_preserves_full_dataset_facet_counts(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source_a = Source(
            name="Bootstrap Facet Source A",
            kind="rss",
            url="https://bootstrap-facet-a.example.com/feed.xml",
            site_url="https://bootstrap-facet-a.example.com",
            folder_path="Bootstrap/Facet A",
        )
        source_b = Source(
            name="Bootstrap Facet Source B",
            kind="rss",
            url="https://bootstrap-facet-b.example.com/feed.xml",
            site_url="https://bootstrap-facet-b.example.com",
            folder_path="Bootstrap/Facet B",
        )
        session.add_all([source_a, source_b])
        session.flush()
        source_a_item_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Bootstrap facet AI item",
            path="bootstrap-facet-ai",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        _create_homepage_routed_item(
            session,
            source=source_b,
            title="Bootstrap facet crypto item",
            path="bootstrap-facet-crypto",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
        )
        source_a_id = source_a.id
        session.commit()

    response = client.get(
        "/api/homepage/bootstrap",
        params={"source_id": source_a_id, "page_size": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_count"] == 1
    assert [item["id"] for item in payload["items"]] == [source_a_item_id]
    source_counts = {facet["label"]: facet["count"] for facet in payload["summary"]["source_counts"]}
    assert source_counts == {
        "Bootstrap Facet Source A": 1,
        "Bootstrap Facet Source B": 1,
    }
    category_counts = {facet["id"]: facet["count"] for facet in payload["summary"]["source_category_counts"]}
    assert category_counts == {
        "Bootstrap/Facet A": 1,
    }

    category_response = client.get(
        "/api/homepage/bootstrap",
        params={"source_category": "Bootstrap/Facet A", "page_size": 10},
    )
    assert category_response.status_code == 200
    category_payload = category_response.json()
    assert category_payload["summary"]["total_count"] == 1
    assert [item["id"] for item in category_payload["items"]] == [source_a_item_id]
    category_counts = {facet["id"]: facet["count"] for facet in category_payload["summary"]["source_category_counts"]}
    assert category_counts == {
        "Bootstrap/Facet A": 1,
        "Bootstrap/Facet B": 1,
    }


def test_homepage_bootstrap_matches_summary_and_items_for_filtered_queries(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    now = datetime.now(timezone.utc)
    with get_session_factory()() as session:
        source_a = Source(
            name="Bootstrap Parity Source A",
            kind="rss",
            url="https://bootstrap-parity-a.example.com/feed.xml",
            site_url="https://bootstrap-parity-a.example.com",
            folder_path="Bootstrap/Parity A",
        )
        source_b = Source(
            name="Bootstrap Parity Source B",
            kind="rss",
            url="https://bootstrap-parity-b.example.com/feed.xml",
            site_url="https://bootstrap-parity-b.example.com",
            folder_path="Bootstrap/Parity B",
        )
        session.add_all([source_a, source_b])
        session.flush()
        recent_ai_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Needle bootstrap parity AI main",
            path="bootstrap-parity-ai-main",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=now - timedelta(hours=2),
        )
        saved_ai_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Needle bootstrap parity saved",
            path="bootstrap-parity-saved",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=now - timedelta(hours=3),
        )
        brief_ai_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Bootstrap parity brief",
            path="bootstrap-parity-brief",
            primary_channel_id="ai_agents_frontier",
            reading_depth="light",
            flow_lane="brief_pool",
            published_at=now - timedelta(hours=4),
        )
        old_ai_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Needle bootstrap parity old",
            path="bootstrap-parity-old",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=now - timedelta(days=3),
        )
        not_interested_id = _create_homepage_routed_item(
            session,
            source=source_b,
            title="Bootstrap parity not interested",
            path="bootstrap-parity-not-interested",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
            published_at=now - timedelta(hours=1),
        )
        _add_test_topics_artifact(session, feed_item_id=recent_ai_id, topic_ids=["phase85a_topic"])
        _add_test_topics_artifact(session, feed_item_id=saved_ai_id, topic_ids=["phase85a_topic"])
        _add_test_topics_artifact(session, feed_item_id=brief_ai_id, topic_ids=["phase85a_brief_topic"])
        _add_test_topics_artifact(session, feed_item_id=old_ai_id, topic_ids=["phase85a_topic"])
        _add_test_topics_artifact(session, feed_item_id=not_interested_id, topic_ids=["phase85a_crypto_topic"])
        session.flush()

        saved_state = session.scalar(select(ItemState).where(ItemState.feed_item_id == saved_ai_id))
        assert saved_state is not None
        saved_state.status = "saved"
        saved_state.saved_at = now - timedelta(hours=2, minutes=30)
        not_interested_state = session.scalar(select(ItemState).where(ItemState.feed_item_id == not_interested_id))
        assert not_interested_state is not None
        not_interested_state.status = "not_interested"
        not_interested_state.not_interested_at = now - timedelta(minutes=30)
        session.commit()

    queries = [
        {"channel_id": "ai_agents_frontier", "search_text": "Needle", "sort": "newest", "page_size": 2},
        {
            "source_category": "Bootstrap/Parity A",
            "channel_id": "ai_agents_frontier",
            "sort": "newest",
            "page_size": 2,
        },
        {"topic": "phase85a_topic", "flow_lane": "main", "sort": "newest", "page_size": 2},
        {"state": "saved", "saved": True, "sort": "newest", "page_size": 2},
        {"not_interested": True, "sort": "newest", "page_size": 2},
        {"time_window": "24h", "sort": "newest", "page_size": 3},
    ]

    for params in queries:
        bootstrap_response = client.get("/api/homepage/bootstrap", params=params)
        summary_response = client.get("/api/homepage/summary", params=params)
        items_response = client.get("/api/homepage/items", params=params)

        assert bootstrap_response.status_code == 200, params
        assert summary_response.status_code == 200, params
        assert items_response.status_code == 200, params
        bootstrap = bootstrap_response.json()
        summary = summary_response.json()
        items = items_response.json()

        assert _summary_without_generated_at(bootstrap["summary"]) == _summary_without_generated_at(summary)
        assert [item["id"] for item in bootstrap["items"]] == [item["id"] for item in items["items"]]
        assert bootstrap["has_more"] == items["has_more"]
        assert bootstrap["page_size"] == items["page_size"]


def test_homepage_items_uses_snapshot_window_without_full_rebuild(client, monkeypatch) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Window Source",
            kind="rss",
            url="https://snapshot-window.example.com/feed.xml",
            site_url="https://snapshot-window.example.com",
            folder_path="Snapshot/Window",
        )
        session.add(source)
        session.flush()
        for index in range(25):
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot window item {index}",
                path=f"snapshot-window-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
        session.commit()

    original_build = homepage_service._build_homepage_cards
    build_calls: list[list[int] | None] = []

    def spy_build_homepage_cards(session, *args, **kwargs):
        feed_item_ids = kwargs.get("feed_item_ids")
        build_calls.append(list(feed_item_ids) if feed_item_ids is not None else None)
        return original_build(session, *args, **kwargs)

    monkeypatch.setattr(homepage_service, "_build_homepage_cards", spy_build_homepage_cards)

    bootstrap_response = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 10})
    assert bootstrap_response.status_code == 200
    bootstrap = bootstrap_response.json()
    assert bootstrap["snapshot_token"]
    assert bootstrap["snapshot_expires_at"]
    assert bootstrap["next_cursor"]

    next_response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 10,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert next_response.status_code == 200
    next_payload = next_response.json()
    assert next_payload["snapshot_token"] == bootstrap["snapshot_token"]
    assert next_payload["snapshot_expires_at"] == bootstrap["snapshot_expires_at"]
    assert len(next_payload["items"]) == 10
    assert not {item["id"] for item in bootstrap["items"]} & {item["id"] for item in next_payload["items"]}
    assert sum(call is None for call in build_calls) == 1
    assert [len(call) for call in build_calls if call is not None] == [10]


def test_homepage_snapshot_rejects_expired_invalidated_and_query_mismatch(client, monkeypatch) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Error Source",
            kind="rss",
            url="https://snapshot-error.example.com/feed.xml",
            site_url="https://snapshot-error.example.com",
            folder_path="Snapshot/Error",
        )
        session.add(source)
        session.flush()
        for index in range(3):
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot error item {index}",
                path=f"snapshot-error-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
        session.commit()

    mismatch_bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    mismatch_response = client.get(
        "/api/homepage/items",
        params={
            "sort": "priority",
            "page_size": 1,
            "cursor": mismatch_bootstrap["next_cursor"],
            "snapshot_token": mismatch_bootstrap["snapshot_token"],
        },
    )
    assert mismatch_response.status_code == 409
    assert mismatch_response.json()["detail"] == "snapshot_invalidated"

    invalidated_bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    homepage_service.invalidate_homepage_snapshots()
    invalidated_response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": invalidated_bootstrap["next_cursor"],
            "snapshot_token": invalidated_bootstrap["snapshot_token"],
        },
    )
    assert invalidated_response.status_code == 409
    assert invalidated_response.json()["detail"] == "snapshot_invalidated"

    monkeypatch.setattr(homepage_service, "HOMEPAGE_SNAPSHOT_TTL", timedelta(microseconds=-1))
    expired_bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    expired_response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": expired_bootstrap["next_cursor"],
            "snapshot_token": expired_bootstrap["snapshot_token"],
        },
    )
    assert expired_response.status_code == 409
    assert expired_response.json()["detail"] == "snapshot_expired"


def test_homepage_items_uses_read_model_seek_when_snapshot_is_not_stored(client, monkeypatch) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Cap Source",
            kind="rss",
            url="https://snapshot-cap.example.com/feed.xml",
            site_url="https://snapshot-cap.example.com",
            folder_path="Snapshot/Cap",
        )
        session.add(source)
        session.flush()
        feed_item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot cap item {index}",
                path=f"snapshot-cap-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
            for index in range(4)
        ]
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
        session.commit()

    monkeypatch.setattr(homepage_service, "HOMEPAGE_MAX_SORTED_IDS_PER_SNAPSHOT", 1)

    bootstrap_response = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 2})
    assert bootstrap_response.status_code == 200
    bootstrap = bootstrap_response.json()
    assert bootstrap["snapshot_token"] is None
    assert bootstrap["next_cursor"] is not None
    first_page_ids = [item["id"] for item in bootstrap["items"]]

    def fail_full_id_scan(*args, **kwargs):
        if kwargs.get("limit") is None:
            raise AssertionError("fresh read model load-more should use SQL seek pagination, not full id scan")
        return original_list_ids(*args, **kwargs)

    original_list_ids = homepage_service._list_homepage_read_model_item_ids
    monkeypatch.setattr(homepage_service, "_list_homepage_read_model_item_ids", fail_full_id_scan)

    response = client.get(
        "/api/homepage/items",
        params={"sort": "newest", "page_size": 2, "cursor": bootstrap["next_cursor"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_model_status"]["state"] == "fresh"
    assert payload["snapshot_token"] is None
    assert len(payload["items"]) == 2
    assert not set(first_page_ids) & {item["id"] for item in payload["items"]}


def test_homepage_snapshot_invalidates_when_operation_finishes(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Operation Source",
            kind="rss",
            url="https://snapshot-operation.example.com/feed.xml",
            site_url="https://snapshot-operation.example.com",
            folder_path="Snapshot/Operation",
        )
        session.add(source)
        session.flush()
        for index in range(2):
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot operation item {index}",
                path=f"snapshot-operation-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
        session.commit()

    bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    with get_session_factory()() as session:
        operation = AppOperation(
            operation_type="source_sync",
            operation_kind="test",
            status=operations_service.STATUS_RUNNING,
        )
        session.add(operation)
        session.flush()
        operations_service._finish_operation(
            session,
            operation,
            operations_service.STATUS_COMPLETED,
            message="Snapshot invalidation test complete.",
            result={"ok": True},
        )
        session.commit()

    response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot_invalidated"


def test_homepage_snapshot_invalidates_when_read_model_marked_stale(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Stale Source",
            kind="rss",
            url="https://snapshot-stale.example.com/feed.xml",
            site_url="https://snapshot-stale.example.com",
            folder_path="Snapshot/Stale",
        )
        session.add(source)
        session.flush()
        feed_item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot stale item {index}",
                path=f"snapshot-stale-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
            for index in range(2)
        ]
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
        session.commit()

    bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    assert bootstrap["read_model_status"]["state"] == "fresh"
    assert bootstrap["next_cursor"] is not None

    with get_session_factory()() as session:
        assert mark_homepage_read_models_stale(session, feed_item_ids=[feed_item_ids[0]], reason="test_stale") == 1
        session.commit()

    response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot_invalidated"


def test_homepage_snapshot_invalidates_when_reader_mutation_rebuilds_read_model(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Reader Mutation Source",
            kind="rss",
            url="https://snapshot-reader-mutation.example.com/feed.xml",
            site_url="https://snapshot-reader-mutation.example.com",
            folder_path="Snapshot/ReaderMutation",
        )
        session.add(source)
        session.flush()
        feed_item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot reader mutation item {index}",
                path=f"snapshot-reader-mutation-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
            for index in range(2)
        ]
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
        session.commit()

    bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    assert bootstrap["read_model_status"]["state"] == "fresh"
    assert bootstrap["next_cursor"] is not None

    mutation_response = client.post(f"/api/reader/items/{feed_item_ids[0]}/mark_browsed", json={})
    assert mutation_response.status_code == 200

    response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot_invalidated"


def test_source_category_rename_endpoint_marks_read_model_stale_and_invalidates_snapshot(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Source Category",
            kind="rss",
            url="https://snapshot-source-category.example.com/feed.xml",
            site_url="https://snapshot-source-category.example.com",
            folder_path="Snapshot/OldCategory",
        )
        session.add(source)
        session.flush()
        feed_item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot source category item {index}",
                path=f"snapshot-source-category-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
            for index in range(2)
        ]
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
        session.commit()

    bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    assert bootstrap["read_model_status"]["state"] == "fresh"

    rename_response = client.post(
        "/api/sources/categories/rename",
        json={"old_path": "Snapshot/OldCategory", "new_path": "Snapshot/NewCategory"},
    )
    assert rename_response.status_code == 200

    with get_session_factory()() as session:
        stale_rows = session.scalars(
            select(FeedItemHomepageReadModel).where(FeedItemHomepageReadModel.feed_item_id.in_(feed_item_ids))
        ).all()
        assert {row.stale_reason for row in stale_rows} == {"source_category_renamed"}
        assert all(row.is_stale for row in stale_rows)

    response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot_invalidated"


def test_insight_channel_create_endpoint_marks_read_model_stale_and_invalidates_snapshot(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    homepage_service.invalidate_homepage_snapshots()

    with get_session_factory()() as session:
        source = Source(
            name="Snapshot Channel Create",
            kind="rss",
            url="https://snapshot-channel-create.example.com/feed.xml",
            site_url="https://snapshot-channel-create.example.com",
            folder_path="Snapshot/ChannelCreate",
        )
        session.add(source)
        session.flush()
        feed_item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot channel create item {index}",
                path=f"snapshot-channel-create-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
            for index in range(2)
        ]
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
        session.commit()

    bootstrap = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1}).json()
    assert bootstrap["read_model_status"]["state"] == "fresh"

    create_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "snapshot_public_mutation",
            "label": "Snapshot Public Mutation",
            "description": "Public mutation invalidation test",
            "focus_prompt": "",
            "boundary_prompt": "",
            "reading_depth": "medium",
            "default_flow_policy": "normal",
            "sort_order": 990,
            "enabled": True,
            "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
        },
    )
    assert create_response.status_code == 201

    with get_session_factory()() as session:
        stale_rows = session.scalars(
            select(FeedItemHomepageReadModel).where(FeedItemHomepageReadModel.feed_item_id.in_(feed_item_ids))
        ).all()
        assert {row.stale_reason for row in stale_rows} == {"insight_channel_created"}
        assert all(row.is_stale for row in stale_rows)

    response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot_invalidated"


@pytest.mark.parametrize(
    ("reason", "mutate_name"),
    [
        ("source_category_deleted", "_mutate_source_category_delete"),
        ("source_updated", "_mutate_source_update"),
        ("source_routing_updated", "_mutate_source_routing_update"),
        ("source_preference_profile_activated", "_mutate_source_preference_profile_activate"),
        ("insight_channel_updated", "_mutate_insight_channel_update"),
        ("insight_engine_settings_updated", "_mutate_insight_engine_settings"),
        ("insight_profile_activated", "_mutate_insight_profile_activate"),
        ("insight_profile_rollback", "_mutate_insight_profile_rollback"),
        ("source_profile_candidate_applied", "_mutate_source_profile_candidate_apply"),
    ],
)
def test_public_homepage_mutation_endpoints_mark_read_model_stale_and_invalidate_snapshot(
    client,
    reason: str,
    mutate_name: str,
) -> None:
    fixture = _create_homepage_public_mutation_fixture(client, reason)
    mutation_response = globals()[mutate_name](client, fixture)
    assert mutation_response.status_code in {200, 201}, mutation_response.text
    _assert_public_mutation_marked_stale_and_invalidated_snapshot(client, fixture, reason)


def test_homepage_items_rejects_cursor_query_mismatch(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source_a = Source(
            name="Cursor Source A",
            kind="rss",
            url="https://cursor-source-a.example.com/feed.xml",
            site_url="https://cursor-source-a.example.com",
            folder_path="Cursor/A",
        )
        source_b = Source(
            name="Cursor Source B",
            kind="rss",
            url="https://cursor-source-b.example.com/feed.xml",
            site_url="https://cursor-source-b.example.com",
            folder_path="Cursor/B",
        )
        session.add_all([source_a, source_b])
        session.flush()
        for index in range(2):
            _create_homepage_routed_item(
                session,
                source=source_a,
                title=f"Cursor source A item {index}",
                path=f"cursor-source-a-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
            )
        _create_homepage_routed_item(
            session,
            source=source_b,
            title="Cursor source B item",
            path="cursor-source-b",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
        )
        source_a_id = source_a.id
        source_b_id = source_b.id
        session.commit()

    first_response = client.get("/api/homepage/items", params={"source_id": source_a_id, "page_size": 1})
    assert first_response.status_code == 200
    cursor = first_response.json()["next_cursor"]
    assert cursor

    mismatch_response = client.get(
        "/api/homepage/items",
        params={"source_id": source_b_id, "page_size": 1, "cursor": cursor},
    )
    assert mismatch_response.status_code == 400
    assert "does not match" in mismatch_response.json()["detail"]


def test_homepage_summary_source_category_facets_keep_empty_category_distinct(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        empty_source = Source(
            name="Empty Category Source",
            kind="rss",
            url="https://empty-category.example.com/feed.xml",
            site_url="https://empty-category.example.com",
            folder_path=None,
        )
        uncategorized_source = Source(
            name="Literal Uncategorized Source",
            kind="rss",
            url="https://literal-uncategorized.example.com/feed.xml",
            site_url="https://literal-uncategorized.example.com",
            folder_path="Uncategorized",
        )
        other_source = Source(
            name="Other Category Source",
            kind="rss",
            url="https://other-category.example.com/feed.xml",
            site_url="https://other-category.example.com",
            folder_path="Other",
        )
        session.add_all([empty_source, uncategorized_source, other_source])
        session.flush()
        empty_item_id = _create_homepage_routed_item(
            session,
            source=empty_source,
            title="Empty category item",
            path="empty-category-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        literal_item_id = _create_homepage_routed_item(
            session,
            source=uncategorized_source,
            title="Literal Uncategorized item",
            path="literal-uncategorized-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        _create_homepage_routed_item(
            session,
            source=other_source,
            title="Other category item",
            path="other-category-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
        )
        session.commit()

    summary_response = client.get("/api/homepage/summary", params={"source_category": "__empty__"})
    assert summary_response.status_code == 200
    summary_payload = summary_response.json()
    assert summary_payload["total_count"] == 1
    category_counts = {facet["id"]: facet for facet in summary_payload["source_category_counts"]}
    assert category_counts["__empty__"] == {"id": "__empty__", "label": "未分类", "count": 1}
    assert category_counts["Uncategorized"]["count"] == 1
    assert category_counts["Other"]["count"] == 1

    empty_items_response = client.get("/api/homepage/items", params={"source_category": "__empty__"})
    assert empty_items_response.status_code == 200
    assert [item["id"] for item in empty_items_response.json()["items"]] == [empty_item_id]

    literal_items_response = client.get("/api/homepage/items", params={"source_category": "Uncategorized"})
    assert literal_items_response.status_code == 200
    assert [item["id"] for item in literal_items_response.json()["items"]] == [literal_item_id]


def test_homepage_read_model_applies_backend_filters_search_facets_and_ordering(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source_a = Source(
            name="Read Model Source A",
            kind="rss",
            url="https://read-model-source-a.example.com/feed.xml",
            site_url="https://read-model-source-a.example.com",
            folder_path="Read Model/A",
        )
        source_b = Source(
            name="Read Model Source B",
            kind="rss",
            url="https://read-model-source-b.example.com/feed.xml",
            site_url="https://read-model-source-b.example.com",
            folder_path="Read Model/B",
        )
        session.add_all([source_a, source_b])
        session.flush()
        needle_ai_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Needle AI result",
            path="needle-ai-result",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        )
        needle_crypto_id = _create_homepage_routed_item(
            session,
            source=source_a,
            title="Needle Crypto result",
            path="needle-crypto-result",
            primary_channel_id="cryptocurrency",
            reading_depth="light",
            flow_lane="brief_pool",
            published_at=datetime(2026, 4, 3, 13, 0, tzinfo=timezone.utc),
        )
        other_ai_id = _create_homepage_routed_item(
            session,
            source=source_b,
            title="Other AI result",
            path="other-ai-result",
            primary_channel_id="ai_agents_frontier",
            reading_depth="deep",
            flow_lane="main",
            published_at=datetime(2026, 4, 3, 14, 0, tzinfo=timezone.utc),
        )
        session.commit()

    query_params = {"channel_id": "ai_agents_frontier", "search_text": "needle", "sort": "newest"}
    summary_response = client.get("/api/homepage/summary", params=query_params)
    assert summary_response.status_code == 200
    summary_payload = summary_response.json()
    assert summary_payload["total_count"] == 1
    channel_counts = {facet["id"]: facet["count"] for facet in summary_payload["channel_counts"]}
    assert channel_counts["ai_agents_frontier"] == 1
    assert channel_counts["cryptocurrency"] == 1
    source_counts = {facet["label"]: facet["count"] for facet in summary_payload["source_counts"]}
    assert source_counts == {"Read Model Source A": 1}

    items_response = client.get("/api/homepage/items", params={**query_params, "page_size": 10})
    assert items_response.status_code == 200
    item_ids = [item["id"] for item in items_response.json()["items"]]
    assert item_ids == [needle_ai_id]
    assert needle_crypto_id not in item_ids
    assert other_ai_id not in item_ids

    newest_response = client.get("/api/homepage/items", params={"sort": "newest", "page_size": 3})
    assert newest_response.status_code == 200
    newest_ids = [item["id"] for item in newest_response.json()["items"]]
    assert newest_ids == [other_ai_id, needle_crypto_id, needle_ai_id]


def test_homepage_read_model_applies_time_window_on_backend(client, monkeypatch) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    monkeypatch.setattr(
        homepage_service,
        "_now_utc",
        lambda: datetime(2026, 4, 3, 15, 0, 0, tzinfo=timezone.utc),
    )

    with get_session_factory()() as session:
        source = Source(
            name="Time Window Source",
            kind="rss",
            url="https://time-window-source.example.com/feed.xml",
            site_url="https://time-window-source.example.com",
            folder_path="Read Model/Time",
        )
        session.add(source)
        session.flush()
        recent_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Recent time-window item",
            path="recent-time-window-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime(2026, 4, 3, 14, 30, tzinfo=timezone.utc),
        )
        old_id = _create_homepage_routed_item(
            session,
            source=source,
            title="Old time-window item",
            path="old-time-window-item",
            primary_channel_id="ai_agents_frontier",
            reading_depth="medium",
            flow_lane="main",
            published_at=datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc),
        )
        session.commit()

    summary_response = client.get("/api/homepage/summary", params={"time_window": "1h"})
    assert summary_response.status_code == 200
    summary_payload = summary_response.json()
    assert summary_payload["total_count"] == 1
    assert summary_payload["source_counts"] == [{"id": str(source.id), "label": "Time Window Source", "count": 1}]

    items_response = client.get("/api/homepage/items", params={"time_window": "1h"})
    assert items_response.status_code == 200
    item_ids = [item["id"] for item in items_response.json()["items"]]
    assert item_ids == [recent_id]
    assert old_id not in item_ids

    all_items_response = client.get("/api/homepage/items", params={"time_window": "all", "sort": "newest"})
    assert all_items_response.status_code == 200
    all_item_ids = [item["id"] for item in all_items_response.json()["items"]]
    assert all_item_ids[:2] == [recent_id, old_id]


def test_homepage_card_serializes_source_tendency_routing_fallback(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Fallback Routing Source",
            kind="rss",
            url="https://fallback-routing.example.com/feed.xml",
            site_url="https://fallback-routing.example.com",
            folder_path="Routing/Fallback",
            flow_policy="high_frequency_brief",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://fallback-routing.example.com/posts/1",
            canonical_url="https://fallback-routing.example.com/posts/1",
            title="Fallback Routed Story",
            published_at=datetime(2026, 3, 28, 10, 0, 0, tzinfo=timezone.utc),
            summary_text="Fallback routed summary",
            content_text="Fallback routed content",
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
        session.add_all(
            [
                SourceChannelTendency(
                    source_id=source.id,
                    channel_id="tcp_artificial_muscle_core",
                    tendency=0.9,
                    is_primary=True,
                    notes="primary",
                ),
                SourceChannelTendency(
                    source_id=source.id,
                    channel_id="ai_agents_frontier",
                    tendency=0.5,
                    is_primary=False,
                    notes="secondary",
                ),
            ]
        )
        item_id = item.id
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["primary_channel_id"] == "tcp_artificial_muscle_core"
    assert card["secondary_channels"] == [{"channel_id": "ai_agents_frontier", "confidence": 0.5, "display_mode": "list"}]
    assert card["reading_depth"] == "deep"
    assert card["flow_lane"] == "brief_pool"
    assert card["routing_confidence"] == 0.9
    assert card["routing_source"] == "deterministic_fallback"
    assert card["routing_reason_codes"] == [
        "source_channel_tendency_match",
        "source_flow_policy_high_frequency",
    ]
    assert card["isolation_reason_codes"] == ["source_policy_high_frequency_brief"]
    assert card["source_policy_applied"] == "high_frequency_brief"


def test_homepage_card_fallback_uses_latest_topics_to_choose_between_source_tendencies(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        source = Source(
            name="Topic Routed Source",
            kind="rss",
            url="https://topic-routing.example.com/feed.xml",
            site_url="https://topic-routing.example.com",
            folder_path="AI/Tools",
            flow_policy="normal",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://topic-routing.example.com/posts/1",
            canonical_url="https://topic-routing.example.com/posts/1",
            title="Agent routing story",
            published_at=datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
            summary_text="Agent routing summary",
            content_text="Agent routing content",
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
        session.add_all(
            [
                SourceChannelTendency(
                    source_id=source.id,
                    channel_id="open_source_tools_practice",
                    tendency=0.82,
                    is_primary=False,
                    notes="engineering source tendency",
                ),
                SourceChannelTendency(
                    source_id=source.id,
                    channel_id="ai_agents_frontier",
                    tendency=0.72,
                    is_primary=False,
                    notes="AI source tendency",
                ),
                InsightEngineArtifact(
                    feed_item_id=item.id,
                    saved_item_id=None,
                    artifact_kind=insight_engine_artifacts_service.FEED_ITEM_TOPICS_ARTIFACT_KIND,
                    model_name="test/model",
                    prompt_version="insight-engine-v2-routing-v1",
                    artifact_payload={
                        "labels": [],
                        "source_kind": "ai_generated",
                        "topic_ids": ["ai_technology"],
                        "content_type": "analysis",
                    },
                ),
            ]
        )
        item_id = item.id
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["primary_channel_id"] == "ai_agents_frontier"
    assert card["secondary_channels"] == [
        {"channel_id": "open_source_tools_practice", "confidence": 0.82, "display_mode": "list"}
    ]
    assert card["reading_depth"] == "medium"
    assert card["flow_lane"] == "main"
    assert card["routing_reason_codes"] == ["source_channel_tendency_match", "content_topic_match"]


def test_homepage_card_exposes_content_extraction_diagnostics(client, monkeypatch) -> None:
    source_id = create_source_for_sync(
        client,
        "Single",
        "https://single.example.com/feed.xml",
        folder_path="Build/Test",
    )
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id, artifact_limit=0).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.content_text_source = "extracted_page"
        item.content_extraction_status = "succeeded"
        item.content_extraction_debug_json = {
            "fetch": {"url": "https://single.example.com/posts/final"},
            "extractor": {
                "name": "readability-lxml",
                "output_format": "html",
                "extracted_visible_char_count": 321,
            },
            "quality": {"passed": True, "reason": "passed"},
        }
        session.add(item)
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _homepage_items(response)[0]
    assert card["content_extraction_method"] == "readability-lxml"
    assert card["content_extraction_final_url"] == "https://single.example.com/posts/final"
    assert card["content_extraction_visible_char_count"] == 321


def test_homepage_items_order_before_page_size_window_and_support_full_read(client) -> None:
    published_base = datetime(2026, 3, 28, 8, 0, 0, tzinfo=timezone.utc)

    with get_session_factory()() as session:
        source = Source(
            name="Ordering Source",
            kind="rss",
            url="https://ordering.example.com/feed.xml",
            site_url="https://ordering.example.com",
            folder_path="Ordering",
        )
        session.add(source)
        session.flush()

        def create_item(index: int, published_at: datetime) -> FeedItem:
            item = FeedItem(
                original_url=f"https://ordering.example.com/posts/{index}",
                canonical_url=f"https://ordering.example.com/posts/{index}",
                title=f"Story {index}",
                published_at=published_at,
                summary_text=f"Summary {index}",
                content_text=f"Content {index}",
                content_text_source="feed_content",
            )
            session.add(item)
            session.flush()
            session.add(ItemState(feed_item_id=item.id, status="unread"))
            session.add(
                SourceItemOccurrence(
                    source_id=source.id,
                    feed_item_id=item.id,
                    external_id=f"ordering-{index}",
                    original_url=item.original_url,
                    canonical_url=item.canonical_url,
                    published_at=published_at,
                    match_rule="external_id",
                    match_value=f"ordering-{index}",
                )
            )
            return item

        for index in range(200):
            create_item(index, published_base - timedelta(days=index + 1))
        newest_item = create_item(999, published_base + timedelta(days=1))
        session.commit()

    limited_response = client.get("/api/homepage/items", params={"page_size": 100})
    assert limited_response.status_code == 200
    limited_cards = _homepage_items(limited_response)
    assert len(limited_cards) == 100
    assert newest_item.id in {card["id"] for card in limited_cards}

    full_response = client.get("/api/homepage/items")
    assert full_response.status_code == 200
    full_cards = _homepage_items(full_response)
    assert len(full_cards) == 30
    assert newest_item.id in {card["id"] for card in full_cards}


def test_homepage_cards_prefer_rebuilt_topic_artifact_and_mark_not_interested_as_low_priority(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Should/Not/Win")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.topic_hints = {"labels": ["AI", "Phase 2A", "AI"]}
        session.add(item)
        session.commit()

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "AI summary for topic artifact",
            "topics": ["software_systems", "tools_products"],
            "content_type": "tutorial",
            "priority_score": 91,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 91}],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, limit=20)
    assert rebuild_response.status_code == 200
    assert client.post(f"/api/reader/items/{item_id}/mark_not_interested").status_code == 200

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    cards = _homepage_items(response)
    assert len(cards) == 1

    card = cards[0]
    assert card["topic_labels"]
    assert card["low_priority_bucket"] is True
    assert card["low_priority_bucket_reason"] == "not_interested"
    assert card["auto_low_priority_cleared"] is False
    assert card["priority_score"] < 40
    assert card["priority_band"] == "low"
    assert card["summary_source"] == "insight_engine_artifact"
    assert card["topic_source"] == "insight_engine_artifact"
    assert card["priority_source"] == "backend_scoring_v1"
    assert ("marked_not_interested", -60) in [
        (reason["code"], reason["weight"]) for reason in card["priority_reasons"]
    ]

    reader_detail = client.get(f"/api/reader/items/{item_id}")
    assert reader_detail.status_code == 200
    assert reader_detail.json()["state"]["status"] == "not_interested"

    undo_response = client.post(f"/api/reader/items/{item_id}/unmark_not_interested")
    assert undo_response.status_code == 200
    assert undo_response.json()["state"]["status"] == "unread"

    restored_response = client.get("/api/homepage/items")
    assert restored_response.status_code == 200
    restored_card = _homepage_items(restored_response)[0]
    assert restored_card["priority_score"] > 0
    assert restored_card["priority_band"] != "low"
    assert restored_card["low_priority_bucket"] is False
    assert restored_card["low_priority_bucket_reason"] is None
    assert "marked_not_interested" not in [reason["code"] for reason in restored_card["priority_reasons"]]


def test_rebuild_endpoint_latest_artifact_wins_for_summary(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Build/Test")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.summary_text = "Updated canonical summary for rebuild"
        session.add(item)
        session.commit()

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "AI updated summary for rebuild",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 4,
        "failed_feed_item_count": 0,
    }

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _homepage_items(response)[0]
    assert card["summary_text"] == "<p>AI updated summary for rebuild</p>"
    assert card["summary_source"] == "insight_engine_artifact"


def test_rebuild_endpoint_accepts_fenced_json_after_response_format_fallback(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Build/Test")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    configure_ai(client)
    requests_seen: list[httpx.Request] = []
    payload = {
        "summary": "Fenced JSON summary accepted after fallback.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_score": 84,
        "priority_band": "high",
        "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
        "low_priority_bucket": False,
        "routing": _valid_routing_payload(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported response_format"}}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"```json\n{json.dumps(payload)}\n```"}}]},
            request=request,
        )

    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(ai_provider, "create_http_client", fake_create_http_client)

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)

    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["artifact_rows_created"] == 4
    assert len(requests_seen) == 2

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    assert _homepage_items(response)[0]["summary_text"] == "<p>Fenced JSON summary accepted after fallback.</p>"


def test_rebuild_provider_schema_uses_dynamic_channel_catalog(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Build/Test")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    create_channel_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "dynamic_rebuild_channel",
            "label": "Dynamic Rebuild Channel",
            "description": "Dynamic channel emitted through actual rebuild provider schema.",
            "sort_order": 66,
            "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
        },
    )
    assert create_channel_response.status_code == 201
    configure_ai(
        client,
        provider="lm-studio",
        base_url="http://10.70.34.34:1234/v1",
        model="qwen/qwen3.5-9b",
        api_key=None,
    )

    requests_seen: list[httpx.Request] = []
    payload = {
        "summary": "Dynamic rebuild channel summary.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_score": 82,
        "priority_band": "high",
        "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 82}],
        "low_priority_bucket": False,
        "routing": {
            **_valid_routing_payload(),
            "primary_channel_id": "dynamic_rebuild_channel",
            "secondary_channels": [],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, handler)

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)

    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["artifact_rows_created"] == 4
    assert len(requests_seen) == 1
    completion_body = json.loads(requests_seen[0].content.decode("utf-8"))
    response_schema = completion_body["response_format"]["json_schema"]["schema"]
    routing_schema = response_schema["properties"]["routing"]["properties"]
    assert "dynamic_rebuild_channel" in routing_schema["primary_channel_id"]["enum"]
    assert "dynamic_rebuild_channel" in routing_schema["secondary_channels"]["items"]["properties"]["channel_id"][
        "enum"
    ]

    item_id = client.get("/api/reader/items").json()[0]["id"]
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "dynamic_rebuild_channel"
    assert routing_payload["source"] == "insight_engine_artifact"


def test_single_item_rebuild_operation_forces_new_feed_item_insight_artifacts(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "AI summary for rebuild test.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 65,
            "priority_band": "medium",
            "priority_reasons": [{"code": "specific_insight", "label": "Specific insight", "weight": 65}],
            "low_priority_bucket": False,
        },
    )
    first_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert first_response.status_code == 200
    assert first_response.json()["artifact_rows_created"] == 4

    install_ai_transport(
        monkeypatch,
        {
            "summary": "AI summary after explicit rebuild.",
            "topics": ["tools_products"],
            "content_type": "release_note",
            "priority_score": 91,
            "priority_band": "high",
            "priority_reasons": [{"code": "implementation_detail", "label": "Implementation detail", "weight": 91}],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_operation(client, item_id, mode="rebuild")
    assert rebuild_response.status_code == 200
    assert rebuild_response.json() == {
        "feed_item_id": item_id,
        "scope": f"feed_item:{item_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 4,
        "failed_feed_item_count": 0,
    }

    with get_session_factory()() as session:
        artifact_count = session.scalar(
            select(func.count()).select_from(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == item_id)
        )
        assert artifact_count == 8

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _homepage_items(response)[0]
    assert card["summary_text"] == "<p>AI summary after explicit rebuild.</p>"
    assert card["topic_labels"]
    assert card["priority_score"] == 73
    assert card["priority_band"] == "high"
    assert card["priority_source"] == "backend_scoring_v1"


def test_single_item_rebuild_uses_sensitive_source_ai_route(client, monkeypatch) -> None:
    source_id = create_source_for_sync(
        client,
        "Sensitive",
        "https://single.example.com/feed.xml",
        folder_path="Private",
        ai_route="sensitive",
    )
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id, artifact_limit=0).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(
        client,
        profile="sensitive",
        base_url="https://openai.example.com/v1",
        model="gpt-4.1-mini",
        api_key="sk-sensitive-secret",
    )
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Sensitive route summary",
            "topics": ["security_privacy"],
            "content_type": "analysis",
            "priority_score": 90,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "Sensitive route", "weight": 90}],
            "low_priority_bucket": False,
        },
        requests_seen=ai_requests_seen,
    )

    rebuild_response = run_feed_item_insight_artifact_operation(client, item_id, mode="rebuild")

    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["artifact_rows_created"] == 4
    assert len(ai_requests_seen) == 1
    assert ai_requests_seen[0].url == httpx.URL("https://openai.example.com/v1/chat/completions")
    assert ai_requests_seen[0].headers["authorization"] == "Bearer sk-sensitive-secret"
    with get_session_factory()() as session:
        artifacts = session.query(InsightEngineArtifact).filter(InsightEngineArtifact.feed_item_id == item_id).all()
        assert {artifact.model_name for artifact in artifacts} == {"openai-compatible/gpt-4.1-mini"}


def test_single_item_insight_artifact_operation_returns_404_for_unknown_item(client) -> None:
    response = client.post("/api/operations/feed-items/999999/insight-artifacts", json={"mode": "rebuild"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Feed item not found"


def test_feed_item_insight_artifact_operation_unknown_source_returns_404(client) -> None:
    response = client.post(
        "/api/operations/feed-item-insight-artifacts",
        json={"scope": "current_source", "source_id": 999999, "limit": 20},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found"


def test_feed_item_insight_artifact_rebuild_failed_job_returns_failed_stats(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id, artifact_limit=0).status_code == 200

    def fail_build(session, *, feed_item_id: int, mode: str, scope: str):
        raise RuntimeError("feed item insight artifact build failed")

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", fail_build)

    response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)

    assert response.status_code == 200
    assert response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 1,
    }


def test_feed_item_insight_artifact_rebuild_keeps_stats_shape_during_active_operation(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id, artifact_limit=0).status_code == 200

    with get_session_factory()() as session:
        operation = operations_service.create_feed_item_insight_artifact_batch_operation(
            session,
            FeedItemInsightArtifactBatchOperationCreate(
                scope="current_source",
                source_id=source_id,
                limit=1,
            ),
        )
        session.commit()
        active_operation_id = operation.id

    def succeed_build(session, *, feed_item_id: int, mode: str, scope: str):
        return operation_jobs.InsightEngineArtifactRebuildStats(
            scope=scope,
            processed_feed_item_count=1,
            artifact_rows_created=4,
            failed_feed_item_count=0,
        )

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", succeed_build)

    response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=1)

    assert response.status_code == 200
    assert set(response.json()) == {"scope", "processed_feed_item_count", "artifact_rows_created", "failed_feed_item_count"}
    assert response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 4,
        "failed_feed_item_count": 0,
    }
    with get_session_factory()() as session:
        active_operation = session.get(AppOperation, active_operation_id)
        assert active_operation is not None
        assert active_operation.status == "completed"
        assert active_operation.result_json["processed_feed_item_count"] == 1


def test_feed_item_insight_artifact_rebuild_adds_hard_failed_job_to_partial_stats(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Partial", "https://partial.example.com/feed.xml", folder_path="Blog")
    with get_session_factory()() as session:
        _create_missing_feed_item_for_source(session, source_id=source_id)
        _create_missing_feed_item_for_source(session, source_id=source_id)
        session.commit()

    call_count = 0

    def build_then_fail(session, *, feed_item_id: int, mode: str, scope: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return operation_jobs.InsightEngineArtifactRebuildStats(
                scope=scope,
                processed_feed_item_count=1,
                artifact_rows_created=4,
                failed_feed_item_count=0,
            )
        raise RuntimeError("feed item insight partial artifact hard failure")

    monkeypatch.setattr(operation_jobs, "build_feed_item_insight_artifacts_for_item", build_then_fail)

    response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=2)

    assert response.status_code == 200
    assert response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 2,
        "artifact_rows_created": 4,
        "failed_feed_item_count": 1,
    }


def test_missing_artifact_candidates_and_single_item_missing_build_do_not_overwrite(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id, artifact_limit=0).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client, provider="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat")

    all_candidates = client.get("/api/feed-item-insight-artifacts/missing-candidates")
    assert all_candidates.status_code == 200
    expected_candidate = {
        "feed_item_id": item_id,
        "source_id": source_id,
        "ai_route": "general",
        "profile": "general",
        "provider": "deepseek",
    }
    assert all_candidates.json() == {
        "scope": "all",
        "total_count": 1,
        "has_more": False,
        "candidates": [expected_candidate],
    }

    source_candidates = client.get(f"/api/feed-item-insight-artifacts/missing-candidates?source_id={source_id}&limit=1")
    assert source_candidates.status_code == 200
    assert source_candidates.json() == {
        "scope": f"source:{source_id}",
        "total_count": 1,
        "has_more": False,
        "candidates": [expected_candidate],
    }

    install_ai_transport(
        monkeypatch,
        {
            "summary": "Missing-only summary.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
        },
    )

    first_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert first_build.status_code == 200
    assert first_build.json() == {
        "feed_item_id": item_id,
        "scope": f"feed_item:{item_id}:missing",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 4,
        "failed_feed_item_count": 0,
    }

    second_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert second_build.status_code == 200
    assert second_build.json()["artifact_rows_created"] == 0

    rebuild = run_feed_item_insight_artifact_operation(client, item_id, mode="rebuild")
    assert rebuild.status_code == 200
    assert rebuild.json()["artifact_rows_created"] == 4
    with get_session_factory()() as session:
        artifact_count = session.scalar(
            select(func.count()).select_from(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == item_id)
        )
    assert artifact_count == 8


def test_missing_artifact_build_writes_needs_review_routing_when_provider_omits_routing(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    requests_seen: list[httpx.Request] = []

    payload_without_routing = {
        "summary": "Legacy three-artifact summary.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_score": 84,
        "priority_band": "high",
        "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
        "low_priority_bucket": False,
    }

    def handler_without_routing(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload_without_routing)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, handler_without_routing)
    first_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert first_build.status_code == 200
    assert first_build.json()["artifact_rows_created"] == 4

    install_ai_transport(
        monkeypatch,
        {
            "summary": "Routing-only fill response should not duplicate existing artifacts.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
        },
        requests_seen=requests_seen,
    )
    second_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert second_build.status_code == 200
    assert second_build.json()["artifact_rows_created"] == 0

    third_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert third_build.status_code == 200
    assert third_build.json()["artifact_rows_created"] == 0

    with get_session_factory()() as session:
        artifact_kinds = [
            artifact.artifact_kind
            for artifact in session.scalars(
                select(InsightEngineArtifact)
                .where(InsightEngineArtifact.feed_item_id == item_id)
                .order_by(InsightEngineArtifact.id.asc())
            )
        ]
    assert artifact_kinds == [
        "feed_item_summary_v1",
        "feed_item_topics_v1",
        "feed_item_ranking_v1",
        "feed_item_routing_v1",
    ]
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert "invalid_channel_reference" in routing_payload["reason_codes"]

    detail_response = client.get(f"/api/settings/insight-engine/attempts?item_id={item_id}&limit=5")
    assert detail_response.status_code == 200
    routing_attempt = next(item for item in detail_response.json()["items"] if item["stage"] == "routing_validation")
    attempt_detail = client.get(f"/api/settings/insight-engine/attempts/{routing_attempt['id']}")
    assert attempt_detail.status_code == 200
    snapshot = attempt_detail.json()["request_snapshot"]
    assert snapshot["validation"]["parse"] == {"status": "succeeded", "parse_attempt_count": 1}
    assert snapshot["validation"]["schema"] == {"status": "succeeded"}
    assert snapshot["validation"]["channel"]["status"] == "failed"
    assert snapshot["validation"]["channel"]["result"] == "omitted"
    assert len(requests_seen) == 1


def test_missing_ranking_reuses_existing_routing_for_scoring(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Initial valid routing summary.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
        },
    )
    first_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert first_build.status_code == 200
    assert first_build.json()["artifact_rows_created"] == 4
    original_routing_payload = _latest_routing_payload_for_item(item_id)
    assert original_routing_payload["primary_channel_id"] == "ai_agents_frontier"

    with get_session_factory()() as session:
        ranking_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_ranking_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert ranking_artifact is not None
        session.delete(ranking_artifact)
        session.commit()

    requests_seen: list[httpx.Request] = []
    repair_payload_without_routing = {
        "summary": "Ranking-only repair response should not alter existing routing.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_reasons": [{"code": "specific_insight", "label": "Specific insight", "weight": 84}],
    }

    def handler_without_routing(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(repair_payload_without_routing)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, handler_without_routing)
    second_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert second_build.status_code == 200
    assert second_build.json()["artifact_rows_created"] == 1
    assert len(requests_seen) == 1

    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "ai_agents_frontier"
    with get_session_factory()() as session:
        ranking_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_ranking_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert ranking_artifact is not None
        ranking_payload = ranking_artifact.artifact_payload
    assert ranking_payload["feature_vector"]["routing"]["uses_needs_review_fallback"] == 0
    fallback_contribution = next(
        entry
        for entry in ranking_payload["score_breakdown"]["contributions"]
        if entry["feature"] == "routing.uses_needs_review_fallback"
    )
    assert fallback_contribution["value"] == 0.0
    assert fallback_contribution["contribution"] == 0.0


def test_missing_ranking_preserves_existing_needs_review_fallback_scoring(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    initial_payload_without_routing = {
        "summary": "Initial fallback routing summary.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_reasons": [{"code": "specific_insight", "label": "Specific insight", "weight": 84}],
    }

    def initial_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(initial_payload_without_routing)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, initial_handler)
    first_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert first_build.status_code == 200
    assert first_build.json()["artifact_rows_created"] == 4
    original_routing_payload = _latest_routing_payload_for_item(item_id)
    assert original_routing_payload["primary_channel_id"] == "needs_review"

    with get_session_factory()() as session:
        ranking_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_ranking_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert ranking_artifact is not None
        session.delete(ranking_artifact)
        session.commit()

    install_ai_transport(
        monkeypatch,
        {
            "summary": "Ranking repair with valid routing candidate should not override existing fallback.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_reasons": [{"code": "specific_insight", "label": "Specific insight", "weight": 84}],
        },
    )
    second_build = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert second_build.status_code == 200
    assert second_build.json()["artifact_rows_created"] == 1

    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    with get_session_factory()() as session:
        ranking_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_ranking_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert ranking_artifact is not None
        ranking_payload = ranking_artifact.artifact_payload
    assert ranking_payload["feature_vector"]["routing"]["uses_needs_review_fallback"] == 1
    fallback_contribution = next(
        entry
        for entry in ranking_payload["score_breakdown"]["contributions"]
        if entry["feature"] == "routing.uses_needs_review_fallback"
    )
    assert fallback_contribution["value"] == 1.0
    assert fallback_contribution["contribution"] == -10.0


def test_invalid_routing_channel_id_writes_other_feed_item_insight_artifacts(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Valid insight but invalid routing.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
            "routing": {
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "unknown_channel",
                "secondary_channels": [],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.7,
                "reason_codes": ["content_topic_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Invalid channel id",
            },
        },
    )

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 4
    with get_session_factory()() as session:
        artifact_kinds = {
            artifact.artifact_kind
            for artifact in session.scalars(select(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == item_id))
        }
    assert artifact_kinds == {"feed_item_summary_v1", "feed_item_topics_v1", "feed_item_ranking_v1", "feed_item_routing_v1"}
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert "invalid_channel_reference" in routing_payload["reason_codes"]


def test_null_primary_routing_channel_id_writes_needs_review_routing(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Valid insight with null primary routing.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
            "routing": {
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": None,
                "secondary_channels": [],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.7,
                "reason_codes": ["content_topic_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Null primary should fallback",
            },
        },
    )

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 4
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert routing_payload["routing_breakdown"]["decision"]["fallback_reason"] == (
        "AI response routing selected needs_review because primary_channel_id was not specified."
    )
    assert routing_payload["reason_codes"] == ["ambiguous_channel_needs_review"]
    with get_session_factory()() as session:
        routing_attempt_count = session.scalar(
            select(func.count())
            .select_from(InsightEngineAttempt)
            .where(InsightEngineAttempt.feed_item_id == item_id)
            .where(InsightEngineAttempt.stage == "routing_validation")
        )
    assert routing_attempt_count == 0


@pytest.mark.parametrize("routing_payload", ["not an object", ["not", "an", "object"]])
def test_non_object_routing_payload_writes_other_feed_item_insight_artifacts(client, monkeypatch, routing_payload) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Valid insight with non-object routing.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
            "routing": routing_payload,
        },
    )

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 4
    with get_session_factory()() as session:
        artifact_kinds = {
            artifact.artifact_kind
            for artifact in session.scalars(select(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == item_id))
        }
    assert artifact_kinds == {"feed_item_summary_v1", "feed_item_topics_v1", "feed_item_ranking_v1", "feed_item_routing_v1"}
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert "invalid_channel_reference" in routing_payload["reason_codes"]


def test_blank_primary_routing_channel_id_writes_other_feed_item_insight_artifacts(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Valid insight with blank primary routing.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
            "routing": {
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "",
                "secondary_channels": [],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.7,
                "reason_codes": ["content_topic_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Blank primary should be invalid",
            },
        },
    )

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 4
    with get_session_factory()() as session:
        artifact_kinds = {
            artifact.artifact_kind
            for artifact in session.scalars(select(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == item_id))
        }
    assert artifact_kinds == {"feed_item_summary_v1", "feed_item_topics_v1", "feed_item_ranking_v1", "feed_item_routing_v1"}
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert "invalid_channel_reference" in routing_payload["reason_codes"]


@pytest.mark.parametrize(
    "routing_payload",
    [
        {
            "schema_version": "feed_item_routing_v1",
            "primary_channel_id": "ai_agents_frontier",
            "secondary_channels": [
                {"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "list"},
                {"channel_id": "open_source_tools_practice", "confidence": 0.3, "display_mode": "list"},
            ],
            "reading_depth": "medium",
            "flow_lane": "main",
            "confidence": 0.7,
            "reason_codes": ["content_topic_match"],
            "promotion_reason_codes": [],
            "isolation_reason_codes": [],
            "source_policy_applied": "normal",
            "notes": "Duplicate secondary",
        },
        {
            "schema_version": "feed_item_routing_v1",
            "primary_channel_id": "ai_agents_frontier",
            "secondary_channels": [{"channel_id": "ai_agents_frontier", "confidence": 0.4, "display_mode": "list"}],
            "reading_depth": "medium",
            "flow_lane": "main",
            "confidence": 0.7,
            "reason_codes": ["content_topic_match"],
            "promotion_reason_codes": [],
            "isolation_reason_codes": [],
            "source_policy_applied": "normal",
            "notes": "Primary duplicated in secondary",
        },
    ],
)
def test_duplicate_routing_channels_write_other_feed_item_insight_artifacts(client, monkeypatch, routing_payload) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Valid insight with invalid duplicate routing.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 84,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
            "low_priority_bucket": False,
            "routing": routing_payload,
        },
    )

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 4
    with get_session_factory()() as session:
        artifact_kinds = {
            artifact.artifact_kind
            for artifact in session.scalars(select(InsightEngineArtifact).where(InsightEngineArtifact.feed_item_id == item_id))
        }
    assert artifact_kinds == {"feed_item_summary_v1", "feed_item_topics_v1", "feed_item_ranking_v1", "feed_item_routing_v1"}
    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert "invalid_channel_reference" in routing_payload["reason_codes"]


@pytest.mark.parametrize(
    ("routing_key_present", "routing_payload", "expected_error_type"),
    [
        (False, None, "omitted"),
        (True, "not an object", "invalid_shape"),
    ],
)
def test_missing_or_invalid_routing_writes_needs_review_fallback(
    client,
    monkeypatch,
    routing_key_present,
    routing_payload,
    expected_error_type,
) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    initial_payload = {
        "summary": "Legacy three-artifact summary.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_score": 84,
        "priority_band": "high",
        "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
        "low_priority_bucket": False,
    }

    def initial_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(initial_payload)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, initial_handler)
    first_response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert first_response.status_code == 200
    assert first_response.json()["artifact_rows_created"] == 4

    second_payload = dict(initial_payload)
    if routing_key_present:
        second_payload["routing"] = routing_payload

    def second_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(second_payload)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, second_handler)
    second_response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert second_response.status_code == 200
    assert second_response.json()["artifact_rows_created"] == 0

    routing_payload = _latest_routing_payload_for_item(item_id)
    assert routing_payload["primary_channel_id"] == "needs_review"
    assert "invalid_channel_reference" in routing_payload["reason_codes"]

    with get_session_factory()() as session:
        attempts = list(
            session.scalars(
                select(InsightEngineAttempt)
                .where(InsightEngineAttempt.feed_item_id == item_id)
                .where(InsightEngineAttempt.stage == "routing_validation")
                .where(InsightEngineAttempt.artifact_count == 0)
                .order_by(InsightEngineAttempt.id.desc())
            )
        )
    assert attempts == []


def test_deepseek_retries_429_and_503_before_success(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    configure_ai(client, provider="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat")
    sleeps: list[float] = []
    requests_seen: list[httpx.Request] = []
    monkeypatch.setattr(ai_provider.time, "sleep", lambda seconds: sleeps.append(seconds))

    payload = {
        "summary": "DeepSeek retry success summary.",
        "topics": ["software_systems"],
        "content_type": "analysis",
        "priority_score": 84,
        "priority_band": "high",
        "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 84}],
        "low_priority_bucket": False,
        "routing": _valid_routing_payload(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if len(requests_seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"message": "rate limited"}}, request=request)
        if len(requests_seen) == 2:
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, handler)

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 4,
        "failed_feed_item_count": 0,
    }
    assert len(requests_seen) == 3
    assert sleeps == [3.0, 15.0]


def test_deepseek_retry_exhaustion_counts_artifact_failure(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    configure_ai(client, provider="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat")
    sleeps: list[float] = []
    requests_seen: list[httpx.Request] = []
    monkeypatch.setattr(ai_provider.time, "sleep", lambda seconds: sleeps.append(seconds))

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}}, request=request)

    install_ai_handler(monkeypatch, handler)

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 1,
    }
    assert len(requests_seen) == 9
    assert sleeps.count(15.0) == 6
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(InsightEngineArtifact)) == 0

    attempts_response = client.get("/api/settings/insight-engine/attempts?status=failed")
    assert attempts_response.status_code == 200
    attempts_payload = attempts_response.json()
    assert attempts_payload["total"] == 3
    attempt = attempts_payload["items"][0]
    assert attempt["source_id"] == source_id
    assert attempt["profile"] == "general"
    assert attempt["provider"] == "deepseek"
    assert attempt["model"] == "deepseek-chat"
    assert attempt["status"] == "failed"
    assert attempt["stage"] == "provider_request"
    assert attempt["error_type"] == "AIProviderHTTPError"
    assert "temporarily unavailable" in attempt["error_message"]
    assert attempt["request_snapshot"] is None

    detail_response = client.get(f"/api/settings/insight-engine/attempts/{attempt['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["request_snapshot"]["provider"] == "deepseek"
    assert detail["request_snapshot"]["model"] == "deepseek-chat"
    assert "request_body" not in detail["request_snapshot"]
    assert "messages" not in detail["request_snapshot"]
    assert "user_message" not in detail["request_snapshot"]
    assert detail["request_snapshot"]["message_count"] == 2
    assert detail["request_snapshot"]["user_message_char_count"] > 0
    assert "authorization" not in str(detail["request_snapshot"]).lower()
    assert "sk-test-secret" not in str(detail["request_snapshot"])

    filtered_response = client.get(f"/api/settings/insight-engine/attempts?status=failed&source_id={source_id}&profile=general")
    assert filtered_response.status_code == 200
    assert filtered_response.json()["total"] == 3


def test_malformed_json_gets_one_repair_retry_and_response_parse_attempt(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    omitted_middle = "MIDDLE_SHOULD_BE_OMITTED"
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.content_text = (
            "A" * (MAX_INSIGHT_ENGINE_INPUT_CHARS // 2)
            + omitted_middle
            + "Z" * (MAX_INSIGHT_ENGINE_INPUT_CHARS // 2)
        )
        session.add(item)
        session.commit()
    configure_ai(client)
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not valid json"}}]},
            request=request,
        )

    install_ai_handler(monkeypatch, handler)

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 0
    assert response.json()["failed_feed_item_count"] == 1
    assert len(requests_seen) == 2
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(InsightEngineArtifact)) == 0

    attempts_response = client.get("/api/settings/insight-engine/attempts?status=failed")
    assert attempts_response.status_code == 200
    attempts_payload = attempts_response.json()
    assert attempts_payload["total"] == 1
    attempt = attempts_payload["items"][0]
    assert attempt["stage"] == "response_parse"
    assert attempt["error_type"] == "InsightEngineResponseParseError"

    detail_response = client.get(f"/api/settings/insight-engine/attempts/{attempt['id']}")
    assert detail_response.status_code == 200
    snapshot = detail_response.json()["request_snapshot"]
    response_parse = snapshot["response_parse"]
    assert response_parse == {"repair_retry_attempted": True, "parse_attempt_count": 2}
    assert snapshot["stats"]["source_content_char_count"] == MAX_INSIGHT_ENGINE_INPUT_CHARS + len(omitted_middle)
    assert snapshot["stats"]["content_text_char_count"] == MAX_INSIGHT_ENGINE_INPUT_CHARS
    assert snapshot["stats"]["content_text_was_truncated"] is True
    assert snapshot["validation"]["parse"] == {
        "status": "failed",
        "repair_retry_attempted": True,
        "parse_attempt_count": 2,
    }
    assert snapshot["validation"]["schema"]["status"] == "not_run"
    assert snapshot["validation"]["channel"]["status"] == "not_run"


def test_schema_validation_failure_records_validation_metadata(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Invalid schema response",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_reasons": [],
        },
    )

    response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")

    assert response.status_code == 200
    assert response.json()["artifact_rows_created"] == 0
    assert response.json()["failed_feed_item_count"] == 1
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(InsightEngineArtifact)) == 0

    attempts_response = client.get("/api/settings/insight-engine/attempts?status=failed")
    assert attempts_response.status_code == 200
    attempt = attempts_response.json()["items"][0]
    assert attempt["stage"] == "response_validation"
    assert attempt["error_type"] == "ValidationError"

    detail_response = client.get(f"/api/settings/insight-engine/attempts/{attempt['id']}")
    assert detail_response.status_code == 200
    snapshot = detail_response.json()["request_snapshot"]
    assert snapshot["validation"]["parse"] == {"status": "succeeded", "parse_attempt_count": 1}
    assert snapshot["validation"]["schema"] == {"status": "failed", "error_type": "ValidationError"}
    assert snapshot["validation"]["channel"]["status"] == "not_run"


def test_rebuild_strips_summary_boilerplate_from_content_html(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.summary_text = None
        item.content_text = """
        <div>
          <blockquote>闂傚倸鍊搁崐宄懊归崶褏鏆﹂柛顭戝亝閸欏繘鏌℃径瀣婵炲樊浜滈～鍛存煏閸繃鍣介柨娑氬枛濮婃椽鎳栭埞鐐珱闂佸憡鎸诲畝绋跨暦椤栨娲敂閸涱垰骞堥梻浣规灱閺呮盯宕导鏉戠叀濠㈣埖鍔栭悡銏ゆ煕閹板吀绨婚柡瀣洴閺屸€崇暆閳ь剟宕伴弽顓犲祦闁硅揪绠戠粻娑欍亜閹烘垵鈧摜鏁崸妤佲拻?Shiro API 闂傚倸鍊搁崐鎼佸磹閻戣姤鍊块柨鏇炲€归崕鎴犳喐閻楀牆绗掗柛銊ュ€搁埞鎴︽偐鐎圭姴顥濈紓浣瑰姈椤ㄥ﹪寮婚悢鍏煎亱闁割偆鍠撻崙锟犳⒑閹肩偛濡奸柛濠傜秺楠炲牓濡搁妷搴ｅ枛瀹曞綊顢欓幆褍缂氶梻鍌欑閹碱偊骞忕€ｎ喖绀堥柣鏃傚帶缁犳牗鎱ㄥ璇蹭壕闂佽鍠楅悷鈺呭箠濠婂啠鏋旈柟绋垮瘨濡嫰姊绘担钘夊惞濠殿喗濞婇幃鐐烘晝閸屾稑浜楅梺璺ㄥ枔婵挳鎷戦悢鍏肩厪闁割偅绻冪粈宀勬煕鐎ｎ偅宕岀€规洜鍏橀、姗€鎮欓幇鍓佺ɑ闁靛洤瀚版俊鐑藉Ψ椤旇В鏋呴梻浣告惈閻寰婃ィ鍐祦濠电姴娲﹂悡鏇㈡煛閸愶絽浜鹃梺鎼炲妿閺佸骞冩ィ鍐╁€婚柤鎭掑劚閳ь剙顭烽弻锕€螣閻氬绀嗛梺鍐叉惈閹冲繘鎮￠妷鈺傜厸闁搞儺鐓堝▓鏂棵瑰鍐ㄢ挃缂佽鲸鎸婚幏鍛嫚閳╁啰鏆﹂梻浣告贡閹虫挸煤椤撶儐鍤曞┑鐘崇閺呮煡鏌涘☉妯衡偓鐟拔涢崘顔兼槬闁逞屽墯閵囧嫰骞掗幋婵愪痪闂佺顑呴澶愬蓟閻斿吋鍊锋い鎺戝€婚悰顔尖攽閻愭潙鐏熼柛銊︽そ瀹曠數鈧綆鍓涘Λ顖炴煛婢跺鍎ユ俊鍙夋そ閺岋綁顢橀悤浣圭暥闂佸疇顫夐崹鍧椼€佸▎鎴犻┏閻庯綆鍋嗛弳銉╂⒒娴ｈ櫣甯涙い銊ユ噽閹广垽骞掗弬鍨亰闂佸壊鍋嗛崰搴ｆ閻愮儤鐓欓梺顓ㄧ畱楠炴﹢鏌涘Ο缁樺€愭慨濠冩そ瀹曟粓骞撻幒宥囧嚬闂備胶鍘у﹢閬嶅磻閹惧墎鐭夌€广儱顦伴崐閿嬨亜閹烘垵鈧顢欓弴銏＄厸濠㈣泛锕︽晶鏇烆熆瑜岀划娆忕暦濮橆厼顕遍悗娑欘焽閸樺崬鈹戦埥鍡楃仴鐎规洖鐏氶弲鑸垫綇閳哄啰锛滈柡澶婄墐閺呮稒绂掕閺屽秷顧侀柛鎾卞妿缁辩偤宕卞☉妯硷紱闂佸湱鍎ら崹鐔煎几閺冨牊鐓熼柨婵嗘噹椤︻剟鏌涚€ｎ偅宕岀€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€骞夐敓鐘茬闁哄稁鍘肩壕鍧楁煕閹邦厾銈撮柡瀣閺岋紕浠︾拠鎻掑闂佹娊鏀遍崹鍧楀蓟濞戞ǚ妲堟慨妤€鐗婇弫楣冩煟韫囨挻绂嬮柛妯哄⒔濡叉劙骞掗幘宕囩獮婵犵數濮存绋库枔鐠鸿　鏀介柣鎰级椤ユ捇鏌涢妸銉хШ闁挎繄鍋涢埞鎴犫偓锝庡亜濞堟繈姊洪崜鑼帥闁稿鎳橀幃?
            <a href="https://single.example.com/posts/1">https://single.example.com/posts/1</a>
          </blockquote>
          <p>闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曚綅閸ヮ剦鏁冮柨鏇楀亾闁汇倗鍋撶换婵囩節閸屾粌顤€闂佺顑戠换婵嬪蓟閵娾晛鍗虫俊銈傚亾濞存粓绠栭弻娑樜旈崨顒佸枤闂佸搫鏈粙鎴﹀煡婢跺娼ㄩ柛顐ｇ箓椤娀姊绘担鍝ワ紞缂侇噮鍨拌灋闁告劦鍠栭拑鐔衡偓骞垮劚閻楁粌顬婇妸鈺傗拺闁告稑锕ョ亸浼存煟閻斿弶娅婄€规洘妞芥慨鈧柕鍫濇噽椤旀帞绱撻崒娆戝妽闁挎艾霉閻樿櫕銇濇慨濠冩そ瀹曟粓鎳犻鈧敮銉╂⒑缁嬫鍎忛悗姘嵆楠炲啴鎮欓悽鐢电厯闁圭厧鐡ㄩ幐濠氬棘閳ь剚淇婇悙顏勨偓鏍ь潖婵犳艾鐤炬い鎰╁€楁稉宥夋煙濞堝灝鏋ょ痪鎯с偢閺屻倗鍠婇崡鐐差潽濠电偛鎳忓ú鐔煎蓟閵堝悿娲敂閸愨晛鏋ら梻浣烘嚀绾绢厽绻涢埀顒併亜閵忊槅娈曢柟宄版嚇瀹曨偊濡烽妷鈺佸綑闂傚倸鍊搁崐椋庢濮橆剦鐒界憸鏃囨濡炪倖鎸堕崹鍦閸ф鐓曟俊銈呭暙閳ь剦鍋婇獮鎺楀箻瀹曞洨鐛梻浣哥秺閸嬪﹪宕㈡ィ鍐ㄥ偍闁芥ê顦弨浠嬫煟濡鍤嬬€规悶鍎甸幃妤€顫濋悡搴＄濠电偛妫庨崹钘夌暦閿濆棗绶炵€光偓閳ь剟鎯侀崼銉︹拺闂侇偆鍋涢懟顖涙櫠鐎涙ǜ浜滈柕蹇婂墲椤ュ牏鈧娲︽禍顏勭暦濮椻偓椤㈡棃宕遍鐘插灡闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢妶鍥╃厯婵炴挻鍩冮崑鎾垛偓瑙勬磸閸ㄥジ濡甸幇鏉跨闁瑰濮存慨锔戒繆閻愵亜鈧牕顔忔繝姘；闁圭偓鐣禍婊堟煥閺冨倸浜鹃柡鍡╁墴閺岋絽鈽夐崡鐐寸亪闁剧粯鐗犻弻宥堫檨闁告挻绻堥幃姗€骞掗幋鏃€鏂€闂佺粯鍔曞鍫曀夐悩缁樼厱閻庯綆鍋呭畷宀勬煙椤栨俺瀚伴摶鏍煕濞戝崬鏋撻柟椋庣帛缁绘稒娼忛崜褎鍋ч梺纭呮珪閹告儳顕ユ繝鍐瘈婵﹩鍘鹃崢钘夘渻閵堝棗绗掗柛濠傛憸閺侇噣宕奸弴鐔哄帗缂傚倷鐒﹁摫鐎规洖鏈〃銉╂倷瀹割喖鍓伴梺瀹狀潐閸ㄥ潡鐛崶顒夋晣闁绘梻琛ラ崑鎾诲礃椤旇В鎷绘繛杈剧悼閹虫捇顢氬鍛＜閻犲洦褰冮埀顒佺摃閻忓鈹戦悙鍙夘棡闁圭顭峰畷鎰板垂椤愮姳绨婚梺鍝勬处椤ㄥ懏绂嶆ィ鍐┾拺闁告繂瀚﹢浼存煟閳哄﹤鐏﹂柣娑卞櫍瀹曞爼顢楁径瀣珜濠电偠鎻徊浠嬪箟閿熺媭鏁傞柣妯哄帠缁诲棝鏌ｉ幇鍏哥盎闁逞屽墯閻楁洟锝炶箛姘ヤ汗闁圭儤鍨归ˇ顔尖攽閻愬弶顥為柛鏃€甯為幑?/p>
          <p>缂傚倸鍊搁崐鎼佸磹閹间礁纾归柣鎴ｅГ閸婂潡鏌ㄩ弴鐐测偓鍫曞焵椤掆偓閸熷磭绮诲☉妯锋婵☆垳鈷堝Σ顖涚節閻㈤潧浠﹂柛銊ㄥ煐缁岄亶鎮滃Ο璇插伎闂佽鍎兼慨銈夋偂閸愵喗鐓曟繝闈涙椤忣亪鏌涢敐鍛Ш闁哄矉缍侀、姗€鎮ゆ担鍦澒闂備礁鎼張顒€煤濡偐鍗氶柣鏃傗拡閺佸秵鎱ㄥ鍡楀⒒濞寸姵鍎抽埞鎴︽倷閼碱剙顣堕梺鎼炲姀濞咃綁骞戦姀鐘婵﹫绲芥禍鐐箾閹寸偟鎳愰柣鎺嶇矙閺岋綁顢橀悜鍥ㄦ暰缂備胶绮粙鎾诲焵椤掑倹鏆╂い顓炵墦瀹曘垽骞橀鐣屽幈闂侀潧顭粻鎴﹀礉閵堝鎳氶柨婵嗩槹閻撴洟鐓崶銊︾閻㈩垵鍩栭妵鍕償閵忋垺鍒涘┑顔硷攻濡炶棄螞閸愩劉妲堟慨姗嗗墻閺嗩偅绻濈喊妯活潑闁稿鎳橀幃鐑藉煛娴ｇ儤娈炬繛鏉戝悑濞兼瑥顔忓┑瀣€垫繛鎴烆仾椤忓懐顩锋い鎾卞灪閳锋垿鏌涘┑鍡楊仾濠㈣泛瀚惀顏堝级鐠恒剱褔鎸婂┑鍡╂富閻庯綆浜濋幑锝囩棯閹佸仮鐎殿喖鐖煎畷鐓庮潩椤撶喓褰呮俊鐐€х徊浠嬪Χ閹间礁绠栨俊銈呮噺閸嬨劑姊婚崼鐔衡槈濞寸姵锕㈤弻褎瀵煎▎鎴犘滈梺鍝勬湰缁嬫垿鍩為幋锕€骞㈡俊銈咃梗閹綁鏌ｆ惔銏╁晱闁哥姵顨婇妴鍐幢濞嗘劕搴婂┑鐐村灟閸ㄦ椽寮查弻銉︾厱婵炴垵宕弸娑欍亜閿旇娅囩紒杈ㄦ尰閹峰懐绮电€ｎ亝顔勯梺璇插绾板秴顭垮鈧、姘舵晲婢跺﹦顔呴梺鑺ッˇ閬嶅礉閿曗偓椤啴濡堕崱妤冪憪闂佺粯甯粻鎾崇暦閿濆牏鐤€闁哄啫鍊婚惁鍫ユ⒑閸撹尙鍘涢柛瀣噽娴滄悂鎮介悽鐢碉紲闁诲函缍嗘禍鍫曟偂婵傚憡鐓涚€光偓鐎ｎ剛锛熼梺閫炲苯澧剧紓宥呮缁傚秴顭ㄩ崼婵囪緢闂佹寧绻傞ˇ浼存偂濞嗘挻鐓ｉ煫鍥风到娴滄繈鎮樿箛娑欐锭闂囧鏌ｅ▎蹇斿櫧闁伙絿鏁婚弻鐔兼惞椤愩倕鈷岄梺绯曟櫔缁绘繂鐣峰鈧崺鈩冩媴閸︻厺绨梻鍌氬€搁崐鎼佸磹妞嬪海鐭嗗〒姘ｅ亾妤犵偞鐗犻、鏇㈠Χ閸屾矮澹曞┑顔矫畷顒勫储鐎电硶鍋撶憴鍕妞ゎ偄顦遍埀顒勬涧閵堟悂鐛弽顓ф晣闁绘柨鎲￠悵銊╂⒒閸屾瑧顦﹂柟璇х節閵嗗啴宕奸悢濂夊殼濠电娀娼ч悧濠囧窗閸℃稒鐓曢柡鍥ュ妼楠炴﹢鏌熼搹顐ょ疄婵﹥妞介弻鍛存倷閼艰泛顏繝鈷€鍐憼妞ゃ劊鍎甸幃娆撳箹椤撶喓鏆︾紓鍌欐祰濞咃綁寮查銈呭灊婵炲棙鍨跺畷澶嬨亜椤撶喎鐏ラ柡浣风矙濮婂宕掑▎鎴犵崲濠电偘鍖犻崵韬插姂閸┾偓妞ゆ帒鍊荤壕濂告煕濞嗗浚妲归柕鍥ㄧ箘閳?/p>
        </div>
        """
        session.add(item)
        session.commit()

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Actual article body for homepage summary",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 75,
            "priority_band": "high",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 75}],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _homepage_items(response)[0]
    assert card["summary_source"] == "insight_engine_artifact"
    assert "\u524d\u5f80\u539f\u7ad9\u67e5\u770b" not in card["summary_text"]
    assert "Actual article body for homepage summary" in card["summary_text"]
    assert "\u524d\u5f80\u539f\u7ad9\u67e5\u770b" not in card["summary_text"]


def test_insight_engine_request_builder_exposes_sanitized_payload_contract(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Builder")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client, base_url="https://ai.example.com/v1", model="builder-model", api_key="sk-builder-secret")

    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        source = session.get(Source, source_id)
        assert item is not None
        assert source is not None
        item.content_text = "Insight builder preferred content body. " * 120
        item.summary_text = "Fallback summary should not be sent when content_text exists."
        session.add(item)
        session.flush()

        config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
        assert config is not None
        context = load_insight_engine_request_context(
            session=session,
            config=config,
            feed_item=item,
            source=source,
        )
        payload = build_insight_engine_request_payload(context)

        assert payload.profile == "general"
        assert payload.provider == "openai-compatible"
        assert payload.model == "builder-model"
        assert payload.endpoint == "https://ai.example.com/v1/chat/completions"
        assert payload.temperature == 0.2
        assert payload.max_tokens == 4096
        assert payload.system_message == payload.messages[0]["content"]
        assert payload.user_message == payload.messages[1]["content"]
        assert payload.request_body["messages"] == payload.messages
        assert payload.request_body["response_format"] == {"type": "json_object"}
        assert payload.fallback_request_body["messages"] == payload.messages
        assert "response_format" not in payload.fallback_request_body
        assert "status" not in payload.taxonomy
        assert "version" not in payload.taxonomy
        assert "software_systems" in [item["id"] for item in payload.taxonomy["items"]]
        assert "status" not in payload.content_type_options
        assert "version" not in payload.content_type_options
        assert "analysis" in [option["id"] for option in payload.content_type_options["options"]]
        assert "status" not in payload.reason_codes
        assert "version" not in payload.reason_codes
        assert "implementation_detail" in payload.reason_codes["ai_allowed_codes"]
        assert "positive_source_history" not in payload.reason_codes["ai_allowed_codes"]
        assert "positive_source_history" not in [entry["code"] for entry in payload.reason_codes["codes"]]
        assert all("source" not in entry and "ai_output_allowed" not in entry for entry in payload.reason_codes["codes"])
        assert payload.active_insight_profile_context["summary_guidelines"]
        assert "status" not in payload.active_insight_profile_context
        assert "schema_version" not in payload.active_insight_profile_context
        assert "profile_id" not in payload.active_insight_profile_context
        assert payload.human_evidence == {"channel_corrections": None}
        assert payload.content_extraction_status == item.content_extraction_status
        assert payload.source_folder == "Builder"
        assert payload.content_text_source == "content_text"
        assert not payload.content_text.endswith("...")
        assert len(payload.content_text) <= MAX_INSIGHT_ENGINE_INPUT_CHARS
        assert payload.stats.content_text_was_truncated is False
        assert payload.stats.content_text_char_count == len(payload.content_text)
        assert payload.stats.content_text_estimated_token_count == ceil(len(payload.content_text) / 4)
        assert payload.profile_version == "default-2026-04-29"
        assert payload.source_profile_version is None
        assert payload.source_profile_is_empty is True
        assert payload.feature_schema_version == "insight_feature_v1"
        assert payload.score_version == "score_v1_hand_weighted"
        assert "Insight builder preferred content body" in payload.user_message
        assert "Human evidence channel corrections:" in payload.user_message
        assert "Fallback summary should not be sent" not in payload.user_message
        assert {entry.field for entry in payload.excluded_fields} == {"URL", "Published at", "Reader status"}
        serialized = json.dumps(payload.model_dump(), ensure_ascii=False)
        assert "sk-builder-secret" not in serialized

        session.add(
            FeedItemStructuredFeedback(
                feed_item_id=item.id,
                primary_channel_correction="ai_agents_frontier",
                channel_add_ids_json=["open_source_tools_practice"],
                channel_remove_ids_json=["cryptocurrency"],
                reading_depth_correction="deep",
                flow_lane_correction="needs_review",
                channel_correction_notes="agent engineering practice",
            )
        )
        session.flush()
        corrected_context = load_insight_engine_request_context(
            session=session,
            config=config,
            feed_item=item,
            source=source,
        )
        corrected_payload = build_insight_engine_request_payload(corrected_context)
        assert corrected_payload.human_evidence["channel_corrections"] == {
            "primary_channel_correction": "ai_agents_frontier",
            "channel_add_ids": ["open_source_tools_practice"],
            "channel_remove_ids": ["cryptocurrency"],
            "reading_depth_correction": "deep",
            "flow_lane_correction": "needs_review",
            "channel_correction_notes": "agent engineering practice",
        }

        item.content_text = None
        item.summary_text = "Fallback summary body for empty content_text."
        session.add(item)
        session.flush()
        fallback_context = load_insight_engine_request_context(
            session=session,
            config=config,
            feed_item=item,
            source=source,
        )
        fallback_payload = build_insight_engine_request_payload(fallback_context)

    assert fallback_payload.content_text_source == "summary_text_fallback"
    assert fallback_payload.content_text == "Fallback summary body for empty content_text."
    assert "Fallback summary body for empty content_text." in fallback_payload.user_message


def test_insight_engine_oversized_runtime_content_is_middle_truncated_before_provider_request(
    client, monkeypatch
) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Builder")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client, base_url="https://ai.example.com/v1", model="builder-model", api_key="sk-builder-secret")

    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Oversized content was truncated before request.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_reasons": [{"code": "specific_insight", "weight": 7}],
        },
        requests_seen=ai_requests_seen,
    )

    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.content_text = "A" * 10000 + "MIDDLE_SHOULD_BE_OMITTED" + "Z" * 10000
        session.add(item)
        session.commit()

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=1)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["failed_feed_item_count"] == 0
    assert rebuild_response.json()["artifact_rows_created"] == 4
    assert len(ai_requests_seen) == 1
    request_body = json.loads(ai_requests_seen[0].content.decode("utf-8"))
    user_message = request_body["messages"][1]["content"]
    assert "A" * 10000 in user_message
    assert "Z" * 10000 in user_message
    assert "MIDDLE_SHOULD_BE_OMITTED" not in user_message

    attempts_response = client.get(f"/api/settings/insight-engine/attempts?item_id={item_id}&limit=1")
    assert attempts_response.status_code == 200
    assert not any(
        attempt["stage"] == "request_build"
        and attempt["error_type"] == "content_too_large_for_provider_budget"
        for attempt in attempts_response.json()["items"]
    )


def test_insight_engine_request_builder_uses_json_schema_for_lm_studio(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Builder")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(
        client,
        provider="lm-studio",
        base_url="http://10.70.34.34:1234/v1",
        model="qwen/qwen3.5-9b",
        api_key=None,
    )
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    create_channel_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": "dynamic_provider_channel",
            "label": "Dynamic Provider Channel",
            "description": "Dynamic channel emitted through the provider JSON schema.",
            "sort_order": 65,
            "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
        },
    )
    assert create_channel_response.status_code == 201

    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        source = session.get(Source, source_id)
        assert item is not None
        assert source is not None
        config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
        assert config is not None
        context = load_insight_engine_request_context(
            session=session,
            config=config,
            feed_item=item,
            source=source,
        )
        payload = build_insight_engine_request_payload(context)

        assert payload.provider == "lm-studio"
        assert payload.endpoint == "http://10.70.34.34:1234/v1/chat/completions"
        response_format = payload.request_body["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "feed_item_insight"
        assert response_format["json_schema"]["strict"] is True
        schema = response_format["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["content_type"]["enum"]
        assert schema["properties"]["priority_reasons"]["items"]["properties"]["code"]["enum"]
        routing_schema = schema["properties"]["routing"]["properties"]
        assert "dynamic_provider_channel" in routing_schema["primary_channel_id"]["enum"]
        assert "dynamic_provider_channel" in routing_schema["secondary_channels"]["items"]["properties"]["channel_id"][
            "enum"
        ]
        assert any(channel.id == "dynamic_provider_channel" for channel in context.channels)
        assert "response_format" not in payload.fallback_request_body


def test_rebuild_uses_extracted_content_when_summary_is_url_only(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    extracted_text = "Single Story extracted article body with enough readable detail. " * 8
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.summary_text = "Read more https://single.example.com/posts/1"
        item.feed_content_text = item.summary_text
        item.content_text = extracted_text
        item.content_text_source = "extracted_page"
        item.content_extraction_status = "succeeded"
        session.add(item)
        session.commit()

    ai_requests_seen: list[httpx.Request] = []
    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Single Story extracted article body with enough readable detail.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 76,
            "priority_band": "high",
            "priority_reasons": [{"code": "implementation_detail", "label": "ignored by contract", "weight": 10}],
            "low_priority_bucket": False,
        },
        requests_seen=ai_requests_seen,
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert any("Single Story extracted article body" in request.content.decode("utf-8") for request in ai_requests_seen)
    completion_body = json.loads(ai_requests_seen[0].content.decode("utf-8"))
    system_prompt = completion_body["messages"][0]["content"]
    user_prompt = completion_body["messages"][1]["content"]
    assert "strict JSON object" in system_prompt
    assert "summary and priority_reasons[].label must be written in Simplified Chinese." in user_prompt
    assert "topics, content_type, and priority_reasons[].code must stay as controlled English ids/codes." in user_prompt
    assert "Single Story extracted article body" in user_prompt
    assert "Read more" not in user_prompt
    assert "https://single.example.com/posts/1" not in user_prompt
    assert "Reader status:" not in user_prompt
    assert "Published at:" not in user_prompt
    assert "URL:" not in user_prompt
    assert "RSS summary:" not in user_prompt

    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        source = session.get(Source, source_id)
        assert item is not None
        assert source is not None
        config = resolve_ai_provider_config_for_source_route(session, source.ai_route)
        assert config is not None
        context = load_insight_engine_request_context(
            session=session,
            config=config,
            feed_item=item,
            source=source,
        )
        builder_payload = build_insight_engine_request_payload(context)

    assert completion_body == builder_payload.request_body

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _homepage_items(response)[0]
    assert card["summary_source"] == "insight_engine_artifact"
    assert "Single Story extracted article body" in card["summary_text"]
    assert "\u524d\u5f80\u539f\u7ad9\u67e5\u770b" not in card["summary_text"]


def test_rebuild_injects_active_insight_profile_context(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200

    profile_response = client.post(
        "/api/settings/insight-profile/versions",
        json={
            "profile_json": build_test_insight_profile_for_homepage(),
            "source_type": "codex_import",
            "edit_note": "Homepage request profile",
            "activate": True,
        },
    )
    assert profile_response.status_code == 200

    configure_ai(client)
    ai_requests_seen: list[httpx.Request] = []
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Profile-aware summary.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 86,
            "priority_band": "high",
            "priority_reasons": [{"code": "implementation_detail", "label": "ignored by contract", "weight": 86}],
            "low_priority_bucket": False,
        },
        requests_seen=ai_requests_seen,
    )

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)

    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["artifact_rows_created"] == 4
    item_id = client.get("/api/reader/items").json()[0]["id"]
    request_body = json.loads(ai_requests_seen[0].content.decode("utf-8"))
    user_prompt = request_body["messages"][1]["content"]
    assert '"profile_version": "homepage-profile-v1"' not in user_prompt
    assert '"profile_id"' not in user_prompt
    assert '"schema_version": "insight_profile_v1"' not in user_prompt
    assert "Prioritize local deployment and data-boundary details" in user_prompt
    assert '"topic_id": "software_systems"' in user_prompt
    with get_session_factory()() as session:
        ranking_artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_ranking_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert ranking_artifact is not None
        assert ranking_artifact.artifact_payload["feature_vector"]["context"]["profile_version"] == "homepage-profile-v1"


def test_backend_scoring_ignores_ai_score_and_applies_signed_ai_reasons(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Scoring summary.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 99,
            "priority_band": "high",
            "priority_reasons": [
                {"code": "high_actionability", "label": "ignored", "weight": 30},
                {"code": "thin_content", "label": "ignored", "weight": 100},
            ],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["priority_source"] == "backend_scoring_v1"
    assert card["priority_score"] == 41
    assert card["priority_band"] == "medium"
    assert card["low_priority_bucket"] is False
    assert card["low_priority_bucket_reason"] is None
    assert card["auto_low_priority_cleared"] is False
    assert [(reason["code"], reason["weight"]) for reason in card["priority_reasons"]] == [
        ("high_actionability", 7),
        ("deep_reference_value", 5),
        ("thin_content", -14),
        ("routing_candidate_confidence", 5),
        ("has_extracted_content", 5),
        ("item_age_hours", -17),
    ]

    settings_response = client.patch(
        "/api/settings/insight-engine",
        json={"auto_low_priority_archiving_enabled": True, "low_priority_score_threshold": 50},
    )
    assert settings_response.status_code == 200
    enabled_response = client.get("/api/homepage/items")
    assert enabled_response.status_code == 200
    enabled_card = _card_by_id(_homepage_items(enabled_response), item_id)
    assert enabled_card["priority_score"] == 41
    assert enabled_card["priority_band"] == "medium"
    assert enabled_card["low_priority_bucket"] is True
    assert enabled_card["low_priority_bucket_reason"] == "auto_score_threshold"
    assert enabled_card["auto_low_priority_cleared"] is False


def test_backend_scoring_active_profile_changes_score_without_artifact_rebuild(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Profile scoring summary.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 50,
            "priority_band": "medium",
            "priority_reasons": [{"code": "implementation_detail", "label": "ignored", "weight": 50}],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200

    initial_cards = client.get("/api/homepage/items").json()["items"]
    assert _card_by_id(initial_cards, item_id)["priority_score"] == 61
    with get_session_factory()() as session:
        artifact_count_before = session.scalar(select(func.count()).select_from(InsightEngineArtifact))

    profile_response = client.post(
        "/api/settings/insight-profile/versions",
        json={
            "profile_json": build_test_insight_profile_for_homepage(),
            "source_type": "codex_import",
            "edit_note": "Read-time scoring profile",
            "activate": True,
        },
    )
    assert profile_response.status_code == 200

    rescored_cards = client.get("/api/homepage/items").json()["items"]
    card = _card_by_id(rescored_cards, item_id)
    assert card["priority_score"] == 72
    with get_session_factory()() as session:
        artifact_count_after = session.scalar(select(func.count()).select_from(InsightEngineArtifact))
    assert artifact_count_after == artifact_count_before


def test_backend_scoring_applies_positive_source_history_excluding_current_item(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    with get_session_factory()() as session:
        _create_history_item(session, source_id=source_id, title="Saved source history", event_type="saved")
        _create_history_item(session, source_id=source_id, title="Read source history", event_type="read_full_in_app")
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["priority_score"] == 46
    assert ("positive_source_history", 8) in [
        (reason["code"], reason["weight"]) for reason in card["priority_reasons"]
    ]


def test_backend_scoring_ignores_undone_not_interested_history_and_undo_is_neutral(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    with get_session_factory()() as session:
        history_ids = [
            _create_history_item(session, source_id=source_id, title="Dismissed source history A", event_type="not_interested"),
            _create_history_item(session, source_id=source_id, title="Dismissed source history B", event_type="not_interested"),
        ]
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["priority_score"] == 24
    assert ("negative_source_history", -14) in [
        (reason["code"], reason["weight"]) for reason in card["priority_reasons"]
    ]

    with get_session_factory()() as session:
        for history_id in history_ids:
            item_state = session.query(ItemState).filter(ItemState.feed_item_id == history_id).one()
            item_state.status = "browsed"
            item_state.not_interested_at = None
            session.add(item_state)
            session.add(
                FeedItemBehaviorEvent(
                    feed_item_id=history_id,
                    event_type="not_interested_cleared",
                    event_payload_json={"changed": True, "status": "browsed"},
                    source="test_fixture",
                    created_at=datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc),
                )
            )
        session.commit()

    restored_response = client.get("/api/homepage/items")
    assert restored_response.status_code == 200
    restored_card = _card_by_id(_homepage_items(restored_response), item_id)
    assert restored_card["priority_score"] == 38
    reason_codes = [reason["code"] for reason in restored_card["priority_reasons"]]
    assert "negative_source_history" not in reason_codes
    assert "positive_source_history" not in reason_codes


def test_backend_scoring_applies_positive_category_history_from_topic_ids_only(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    with get_session_factory()() as session:
        _add_test_topics_artifact(session, feed_item_id=item_id, topic_ids=["software_systems"])
        _create_history_item(
            session,
            source_id=None,
            title="Saved category history",
            event_type="saved",
            topic_ids=["software_systems"],
        )
        _create_history_item(
            session,
            source_id=None,
            title="Read category history",
            event_type="read_full_in_app",
            topic_ids=["software_systems"],
        )
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _card_by_id(_homepage_items(response), item_id)
    assert card["priority_score"] == 49
    assert ("positive_category_history", 6) in [
        (reason["code"], reason["weight"]) for reason in card["priority_reasons"]
    ]


def test_homepage_ignores_existing_jump_prompt_summary_artifact(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    jump_prompt = '<p>\u524d\u5f80\u539f\u7ad9\u67e5\u770b: <a href="https://single.example.com/posts/1">https://single.example.com/posts/1</a></p>'
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.summary_text = jump_prompt
        item.feed_content_text = jump_prompt
        item.content_text = jump_prompt
        item.content_text_source = "feed_summary"
        item.content_extraction_status = "failed"
        item.content_extraction_error = "quality_failed: extracted text is too short"
        session.add(item)
        session.add(
            InsightEngineArtifact(
                feed_item_id=item_id,
                saved_item_id=None,
                artifact_kind=insight_engine_artifacts_service.FEED_ITEM_SUMMARY_ARTIFACT_KIND,
                model_name="system/deterministic-homepage-v1",
                prompt_version="test",
                artifact_payload={
                    "text": "<p>\u524d\u5f80\u539f\u7ad9\u67e5\u770b: https://single.example.com/posts/1</p>",
                    "source_kind": "canonical_summary",
                },
            )
        )
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = _homepage_items(response)[0]
    assert card["summary_text"] is None
    assert card["summary_source"] == "none"


def test_rebuild_filters_jump_prompt_from_summary_and_ranking(client, monkeypatch) -> None:
    monkeypatch.setattr(
        insight_engine_artifacts_service,
        "_now_utc",
        lambda: datetime(2026, 3, 28, 11, 0, 0, tzinfo=timezone.utc),
    )

    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    jump_prompt = '<p>\u524d\u5f80\u539f\u7ad9\u67e5\u770b: <a href="https://single.example.com/posts/1">https://single.example.com/posts/1</a></p>'
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.summary_text = jump_prompt
        item.feed_content_text = jump_prompt
        item.content_text = jump_prompt
        item.content_text_source = "feed_summary"
        item.content_extraction_status = "failed"
        item.content_extraction_error = "quality_failed: extracted text is too short"
        session.add(item)
        session.commit()

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Read more https://single.example.com/posts/1",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 61,
            "priority_band": "medium",
            "priority_reasons": [{"code": "specific_insight", "label": "ignored by contract", "weight": 10}],
            "low_priority_bucket": False,
        },
    )
    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json() == {
        "scope": f"source:{source_id}",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 1,
    }
    with get_session_factory()() as session:
        assert session.query(InsightEngineArtifact).count() == 0


def test_missing_artifact_candidates_use_latest_occurrence_source_scope(client, monkeypatch) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml", folder_path="Tech/Alpha")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml", folder_path="Tech/Beta")
    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://beta.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_BETA),
        },
    )
    assert sync_source_with_operation(client, alpha_id, artifact_limit=0).status_code == 200
    assert sync_source_with_operation(client, beta_id, artifact_limit=0).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    configure_ai(client, provider="deepseek", base_url="https://api.deepseek.com", model="deepseek-chat")

    alpha_candidates = client.get(f"/api/feed-item-insight-artifacts/missing-candidates?source_id={alpha_id}&limit=10")
    beta_candidates = client.get(f"/api/feed-item-insight-artifacts/missing-candidates?source_id={beta_id}&limit=10")

    assert alpha_candidates.status_code == 200
    assert alpha_candidates.json() == {
        "scope": f"source:{alpha_id}",
        "total_count": 0,
        "has_more": False,
        "candidates": [],
    }
    assert beta_candidates.status_code == 200
    assert beta_candidates.json() == {
        "scope": f"source:{beta_id}",
        "total_count": 1,
        "has_more": False,
        "candidates": [
            {
                "feed_item_id": item_id,
                "source_id": beta_id,
                "ai_route": "general",
                "profile": "general",
                "provider": "deepseek",
            }
        ],
    }

def test_missing_artifact_build_skips_failed_url_only_content(client, monkeypatch) -> None:
    source_id = create_source_for_sync(
        client,
        "Single",
        "https://single.example.com/feed.xml",
        folder_path="Blog",
    )
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]
    jump_prompt = (
        '<p>Read more <a href="https://single.example.com/posts/1">'
        "https://single.example.com/posts/1</a></p>"
    )
    with get_session_factory()() as session:
        item = session.get(FeedItem, item_id)
        assert item is not None
        item.summary_text = jump_prompt
        item.feed_content_text = jump_prompt
        item.content_text = jump_prompt
        item.content_text_source = "feed_summary"
        item.content_extraction_status = "failed"
        item.content_extraction_error = "quality_failed: extracted text is too short"
        session.add(item)
        session.commit()

    configure_ai(client)
    build_response = run_feed_item_insight_artifact_operation(client, item_id, mode="build_missing")
    assert build_response.status_code == 200
    assert build_response.json() == {
        "feed_item_id": item_id,
        "scope": f"feed_item:{item_id}:missing",
        "processed_feed_item_count": 1,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 1,
    }
    with get_session_factory()() as session:
        assert session.query(InsightEngineArtifact).count() == 0


def test_rebuild_falls_back_for_unknown_topic_and_content_type(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "Controlled contract fallback summary.",
            "topics": ["not_a_real_topic"],
            "content_type": "not_a_real_content_type",
            "priority_score": 80,
            "priority_band": "high",
            "priority_reasons": [
                {"code": "made_up_reason", "label": "should be filtered", "weight": 40},
                {"code": "specific_insight", "label": "will be replaced", "weight": 140},
            ],
            "low_priority_bucket": False,
        },
    )

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["artifact_rows_created"] == 4

    with get_session_factory()() as session:
        topic_artifact = (
            session.query(InsightEngineArtifact)
            .filter(
                InsightEngineArtifact.feed_item_id == item_id,
                InsightEngineArtifact.artifact_kind == insight_engine_artifacts_service.FEED_ITEM_TOPICS_ARTIFACT_KIND,
            )
            .one()
        )
        ranking_artifact = (
            session.query(InsightEngineArtifact)
            .filter(
                InsightEngineArtifact.feed_item_id == item_id,
                InsightEngineArtifact.artifact_kind == insight_engine_artifacts_service.FEED_ITEM_RANKING_ARTIFACT_KIND,
            )
            .one()
        )

    assert topic_artifact.artifact_payload["topic_ids"] == ["uncategorized"]
    assert topic_artifact.artifact_payload["labels"]
    assert topic_artifact.artifact_payload["content_type"] == "unknown"
    assert topic_artifact.artifact_payload["content_type_label"]
    assert ranking_artifact.artifact_payload["priority_reasons"][0]["code"] == "specific_insight"
    assert ranking_artifact.artifact_payload["priority_reasons"][0]["weight"] == 34
    assert ranking_artifact.artifact_payload["feature_schema_version"] == "insight_feature_v1"
    assert ranking_artifact.artifact_payload["score_version"] == "score_v1_hand_weighted"


    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    assert _homepage_items(response)[0]["topic_labels"]


def test_rebuild_rejects_ai_response_without_allowed_reason_codes(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml", folder_path="Blog")
    install_transport(monkeypatch, {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200

    configure_ai(client)
    install_ai_transport(
        monkeypatch,
        {
            "summary": "This response has no controlled reasons.",
            "topics": ["software_systems"],
            "content_type": "analysis",
            "priority_score": 72,
            "priority_band": "medium",
            "priority_reasons": [{"code": "topic_match", "label": "Topic match", "weight": 72}],
            "low_priority_bucket": False,
        },
    )

    rebuild_response = run_feed_item_insight_artifact_rebuild_operations(client, source_id=source_id, limit=20)
    assert rebuild_response.status_code == 200
    assert rebuild_response.json()["processed_feed_item_count"] == 1
    assert rebuild_response.json()["artifact_rows_created"] == 0
    assert rebuild_response.json()["failed_feed_item_count"] == 1
    with get_session_factory()() as session:
        assert session.query(InsightEngineArtifact).count() == 0


def _create_homepage_public_mutation_fixture(client, slug: str) -> dict:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    channel_catalog_version = seed_response.json()[0]["channel_catalog_version"]
    channel_id = f"public_mutation_{uuid4().hex[:8]}"
    channel_response = client.post(
        "/api/settings/insight-channels",
        json={
            "id": channel_id,
            "label": f"Public Mutation {slug}",
            "description": "Reusable public mutation invalidation fixture",
            "focus_prompt": "",
            "boundary_prompt": "",
            "reading_depth": "medium",
            "default_flow_policy": "normal",
            "sort_order": 980,
            "enabled": True,
            "expected_channel_catalog_version": channel_catalog_version,
        },
    )
    assert channel_response.status_code == 201
    homepage_service.invalidate_homepage_snapshots()

    folder_path = f"Snapshot/PublicMutation/{slug}"
    with get_session_factory()() as session:
        source = Source(
            name=f"Snapshot Public Mutation {slug}",
            kind="rss",
            url=f"https://snapshot-public-mutation.example.com/{slug}/feed.xml",
            site_url=f"https://snapshot-public-mutation.example.com/{slug}",
            folder_path=folder_path,
        )
        session.add(source)
        session.flush()
        feed_item_ids = [
            _create_homepage_routed_item(
                session,
                source=source,
                title=f"Snapshot public mutation {slug} item {index}",
                path=f"snapshot-public-mutation-{slug}-{index}",
                primary_channel_id="ai_agents_frontier",
                reading_depth="medium",
                flow_lane="main",
                published_at=datetime(2026, 3, 28, 12, index, tzinfo=timezone.utc),
            )
            for index in range(2)
        ]
        session.flush()
        rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
        session.commit()
        source_id = source.id
        source_name = source.name

    bootstrap_response = client.get("/api/homepage/bootstrap", params={"sort": "newest", "page_size": 1})
    assert bootstrap_response.status_code == 200
    bootstrap = bootstrap_response.json()
    assert bootstrap["read_model_status"]["state"] == "fresh"
    assert bootstrap["next_cursor"] is not None
    assert bootstrap["snapshot_token"] is not None
    return {
        "bootstrap": bootstrap,
        "channel_id": channel_id,
        "channel_catalog_version": channel_response.json()["channel_catalog_version"],
        "feed_item_ids": feed_item_ids,
        "folder_path": folder_path,
        "source_id": source_id,
        "source_name": source_name,
    }


def _assert_public_mutation_marked_stale_and_invalidated_snapshot(client, fixture: dict, reason: str) -> None:
    with get_session_factory()() as session:
        stale_rows = session.scalars(
            select(FeedItemHomepageReadModel).where(
                FeedItemHomepageReadModel.feed_item_id.in_(fixture["feed_item_ids"])
            )
        ).all()
        assert len(stale_rows) == len(fixture["feed_item_ids"])
        assert {row.stale_reason for row in stale_rows} == {reason}
        assert all(row.is_stale for row in stale_rows)

    bootstrap = fixture["bootstrap"]
    response = client.get(
        "/api/homepage/items",
        params={
            "sort": "newest",
            "page_size": 1,
            "cursor": bootstrap["next_cursor"],
            "snapshot_token": bootstrap["snapshot_token"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot_invalidated"


def _mutate_source_category_delete(client, fixture: dict):
    return client.post("/api/sources/categories/delete", json={"path": fixture["folder_path"]})


def _mutate_source_update(client, fixture: dict):
    return client.patch(f"/api/sources/{fixture['source_id']}", json={"name": f"{fixture['source_name']} Updated"})


def _mutate_source_routing_update(client, fixture: dict):
    return client.patch(
        f"/api/sources/{fixture['source_id']}/insight-routing",
        json={
            "flow_policy": "high_frequency_brief",
            "channel_tendencies": [
                {
                    "channel_id": "ai_agents_frontier",
                    "tendency": 0.9,
                    "is_primary": True,
                    "notes": "Public mutation invalidation test",
                }
            ],
        },
    )


def _mutate_source_preference_profile_activate(client, fixture: dict):
    draft_response = client.put(
        f"/api/sources/{fixture['source_id']}/preference-profile/draft",
        json={
            "subscription_reason": "Track implementation-heavy source updates.",
            "good_article_criteria": ["Includes reproducible debugging detail."],
            "preferred_topics": ["software_systems"],
            "source_type": "user_edit",
        },
    )
    assert draft_response.status_code == 200
    return client.post(
        f"/api/sources/{fixture['source_id']}/preference-profile/draft/activate",
        json={"edit_note": "Activate source profile for invalidation test."},
    )


def _mutate_insight_channel_update(client, fixture: dict):
    return client.patch(
        f"/api/settings/insight-channels/{fixture['channel_id']}",
        json={
            "label": "Public Mutation Updated",
            "expected_channel_catalog_version": fixture["channel_catalog_version"],
        },
    )


def _mutate_insight_engine_settings(client, fixture: dict):
    return client.patch(
        "/api/settings/insight-engine",
        json={"auto_low_priority_archiving_enabled": True, "low_priority_score_threshold": 45},
    )


def _mutate_insight_profile_activate(client, fixture: dict):
    create_response = client.post(
        "/api/settings/insight-profile/versions",
        json={
            "profile_json": build_test_insight_profile_for_homepage(),
            "source_type": "manual_edit",
            "edit_note": "Create inactive profile for invalidation test.",
            "activate": False,
        },
    )
    assert create_response.status_code == 200
    profile_id = create_response.json()["versions"][-1]["id"]
    return client.post(
        f"/api/settings/insight-profile/versions/{profile_id}/activate",
        json={"note": "Activate profile for invalidation test."},
    )


def _mutate_insight_profile_rollback(client, fixture: dict):
    create_response = client.post(
        "/api/settings/insight-profile/versions",
        json={
            "profile_json": build_test_insight_profile_for_homepage(),
            "source_type": "manual_edit",
            "edit_note": "Create active profile for rollback invalidation test.",
            "activate": True,
        },
    )
    assert create_response.status_code == 200
    profile_id = create_response.json()["versions"][-1]["id"]
    return client.post(
        f"/api/settings/insight-profile/versions/{profile_id}/rollback",
        json={"note": "Rollback profile for invalidation test."},
    )


def _mutate_source_profile_candidate_apply(client, fixture: dict):
    candidate = {
        "candidate_version": "profile_candidate_v1",
        "summary": "Evidence suggests a more implementation-heavy source profile.",
        "based_on_package": {"package_version": "insight_evidence_package_v2"},
        "global_profile_changes": {"profile_json": build_test_insight_profile_for_homepage()},
        "source_profile_changes": [
            {
                "source_id": fixture["source_id"],
                "subscription_reason": "Track implementation-heavy source updates.",
                "good_article_criteria": ["Includes reproducible debugging detail."],
                "bad_article_patterns": ["Thin launch announcements."],
                "preferred_topics": ["software_systems"],
                "preferred_content_types": ["tutorial"],
                "quality_notes": "Imported source evidence should be reviewed before activation.",
                "edit_note": "Candidate source profile for invalidation test.",
            }
        ],
        "channel_prompt_boundary_suggestions": [],
        "warnings": [],
    }
    return client.post(
        "/api/settings/insight-profile/candidate/source-drafts",
        json={"candidate_json": candidate, "source_ids": [fixture["source_id"]]},
    )


def _card_by_id(cards: list[dict], item_id: int) -> dict:
    return next(card for card in cards if card["id"] == item_id)


def _homepage_items(response) -> list[dict]:
    return response.json()["items"]


def _summary_without_generated_at(summary: dict) -> dict:
    return {key: value for key, value in summary.items() if key != "generated_at"}


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
    response = client.post("/api/operations/source-sync", json=payload)
    assert response.status_code == 202
    operation_id = response.json()["id"]
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)
    operation = client.get(f"/api/operations/{operation_id}").json()
    assert operation["status"] in {"completed", "failed", "stopped"}
    assert operation["result"] is not None
    return _OperationResultResponse(operation["result"])


def run_feed_item_insight_artifact_operation(
    client,
    feed_item_id: int,
    *,
    mode: str,
) -> _OperationResultResponse:
    response = client.post(f"/api/operations/feed-items/{feed_item_id}/insight-artifacts", json={"mode": mode})
    if response.status_code != 202:
        return response
    operation_id = response.json()["id"]
    operation_jobs.run_ai_lane_jobs_until_idle(
        worker_id="homepage-api-test-ai-lane",
        heartbeat_interval_seconds=0,
        poll_interval_seconds=0.01,
        failure_backoff_seconds=0,
    )
    operation = client.get(f"/api/operations/{operation_id}").json()
    assert operation["status"] in {"completed", "failed", "stopped"}
    result = operation["result"]
    if result is None:
        result = {
            "feed_item_id": feed_item_id,
            "scope": f"feed_item:{feed_item_id}" if mode == "rebuild" else f"feed_item:{feed_item_id}:missing",
            "processed_feed_item_count": 1,
            "artifact_rows_created": 0,
            "failed_feed_item_count": 1 if operation["status"] == "failed" else 0,
        }
    return _OperationResultResponse(result)


def run_feed_item_insight_artifact_rebuild_operations(
    client,
    *,
    source_id: int | None = None,
    limit: int | None = None,
) -> _OperationResultResponse:
    feed_item_ids = _feed_item_ids_for_artifact_operation(source_id=source_id, limit=limit)
    aggregate = {
        "scope": f"source:{source_id}" if source_id is not None else "all",
        "processed_feed_item_count": 0,
        "artifact_rows_created": 0,
        "failed_feed_item_count": 0,
    }
    for feed_item_id in feed_item_ids:
        result = run_feed_item_insight_artifact_operation(client, feed_item_id, mode="rebuild").json()
        aggregate["processed_feed_item_count"] += int(result.get("processed_feed_item_count") or 0)
        aggregate["artifact_rows_created"] += int(result.get("artifact_rows_created") or 0)
        aggregate["failed_feed_item_count"] += int(result.get("failed_feed_item_count") or 0)
    return _OperationResultResponse(aggregate)


def _feed_item_ids_for_artifact_operation(*, source_id: int | None, limit: int | None) -> list[int]:
    with get_session_factory()() as session:
        statement = select(FeedItem.id).join(SourceItemOccurrence, SourceItemOccurrence.feed_item_id == FeedItem.id)
        if source_id is not None:
            statement = statement.where(SourceItemOccurrence.source_id == source_id)
        statement = statement.order_by(FeedItem.id.asc()).distinct()
        if limit is not None:
            statement = statement.limit(limit)
        return list(session.scalars(statement))


def _create_homepage_routed_item(
    session,
    *,
    source: Source,
    title: str,
    path: str,
    primary_channel_id: str,
    reading_depth: str,
    flow_lane: str,
    secondary_channels: list[dict] | None = None,
    published_at: datetime | None = None,
) -> int:
    timestamp = published_at or datetime(2026, 3, 28, 12, 0, 0, tzinfo=timezone.utc)
    item = FeedItem(
        original_url=f"https://{path}.example.com/posts/1",
        canonical_url=f"https://{path}.example.com/posts/1",
        title=title,
        published_at=timestamp,
        summary_text=f"Summary for {title}",
        content_text=f"Content for {title}",
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
            saved_item_id=None,
            artifact_kind=insight_engine_artifacts_service.FEED_ITEM_ROUTING_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="insight-engine-v2-routing-v1",
            artifact_payload={
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": primary_channel_id,
                "secondary_channels": secondary_channels or [],
                "reading_depth": reading_depth,
                "flow_lane": flow_lane,
                "confidence": 0.9,
                "reason_codes": ["source_channel_tendency_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Scoped query test routing",
                "source": "insight_engine_artifact",
            },
        )
    )
    return item.id


def _create_history_item(
    session,
    *,
    source_id: int | None,
    title: str,
    event_type: str,
    topic_ids: list[str] | None = None,
) -> int:
    timestamp = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    item_slug = title.lower().replace(" ", "-")
    if source_id is None:
        source = Source(
            name=f"History {title}",
            kind="rss",
            url=f"https://history-{item_slug}.example.com/feed.xml",
            site_url=f"https://history-{item_slug}.example.com",
            folder_path="History/Signals",
            is_active=True,
            ai_route="general",
            provenance="manual",
        )
        session.add(source)
        session.flush()
        source_id = source.id

    item = FeedItem(
        original_url=f"https://history.example.com/posts/{item_slug}",
        canonical_url=f"https://history.example.com/posts/{item_slug}",
        title=title,
        published_at=timestamp,
        summary_text=f"Summary for {title}",
        feed_content_text=f"Feed content for {title}",
        content_text=f"Content for {title}",
        content_text_source="feed_content",
        content_extraction_status="not_needed",
    )
    session.add(item)
    session.flush()
    session.add(
        SourceItemOccurrence(
            source_id=source_id,
            feed_item_id=item.id,
            external_id=item.canonical_url,
            original_url=item.original_url,
            canonical_url=item.canonical_url,
            published_at=timestamp,
            raw_entry_json={"title": title},
            match_rule="created",
            match_value=item.canonical_url,
            created_at=timestamp,
        )
    )
    session.add(ItemState(feed_item_id=item.id, status=_status_for_history_event(event_type)))
    session.add(
        FeedItemBehaviorEvent(
            feed_item_id=item.id,
            event_type=event_type,
            event_payload_json={"test_history": True},
            source="test_fixture",
            created_at=timestamp,
        )
    )
    if topic_ids:
        _add_test_topics_artifact(session, feed_item_id=item.id, topic_ids=topic_ids)
    return item.id


def _add_test_topics_artifact(session, *, feed_item_id: int, topic_ids: list[str]) -> None:
    session.add(
        InsightEngineArtifact(
            feed_item_id=feed_item_id,
            saved_item_id=None,
            artifact_kind=insight_engine_artifacts_service.FEED_ITEM_TOPICS_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="homepage-ai-v1",
            artifact_payload={
                "topic_ids": topic_ids,
                "labels": [],
                "content_type": "analysis",
                "content_type_label": None,
                "source_kind": "ai_generated",
            },
        )
    )


def _status_for_history_event(event_type: str) -> str:
    return {
        "read_full_in_app": "read_full",
        "saved": "saved",
        "not_interested": "not_interested",
        "unsaved": "browsed",
    }.get(event_type, "browsed")


def create_source_for_sync(
    client,
    name: str,
    url: str,
    *,
    folder_path: str | None = None,
    ai_route: str = "general",
) -> int:
    response = client.post(
        "/api/sources",
        json={
            "name": name,
            "kind": "rss",
            "url": url,
            "site_url": url.replace("/feed.xml", ""),
            "folder_path": folder_path,
            "is_active": True,
            "ai_route": ai_route,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _create_missing_feed_item_for_source(session, *, source_id: int) -> FeedItem:
    source = session.get(Source, source_id)
    assert source is not None
    unique = uuid4().hex
    published_at = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    item = FeedItem(
        original_url=f"{source.site_url or source.url}/posts/{unique}",
        canonical_url=f"{source.site_url or source.url}/posts/{unique}",
        title=f"Missing Homepage Artifact {unique}",
        published_at=published_at,
        summary_text="Missing homepage artifact summary.",
        content_text="Missing homepage artifact content.",
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


def configure_ai(
    client,
    *,
    profile: str = "general",
    provider: str = "openai-compatible",
    base_url: str = "https://ai.example.com",
    model: str = "test-model",
    api_key: str = "sk-test-secret",
) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    response = client.patch(
        "/api/settings/ai",
        json={
            "profiles": {
                profile: {
                    "provider": provider,
                    "enabled": True,
                    "base_url": base_url,
                    "model": model,
                    "api_key": api_key,
                }
            },
        },
    )
    assert response.status_code == 200


def install_ai_transport(
    monkeypatch,
    payload: dict,
    *,
    requests_seen: list[httpx.Request] | None = None,
) -> None:
    payload = {
        "routing": _valid_routing_payload(),
        **payload,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if requests_seen is not None:
            requests_seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(ai_provider, "create_http_client", fake_create_http_client)


def install_ai_handler(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_create_http_client() -> httpx.Client:
        return httpx.Client(transport=transport)

    monkeypatch.setattr(ai_provider, "create_http_client", fake_create_http_client)


def _latest_routing_payload_for_item(item_id: int) -> dict:
    with get_session_factory()() as session:
        artifact = session.scalar(
            select(InsightEngineArtifact)
            .where(InsightEngineArtifact.feed_item_id == item_id)
            .where(InsightEngineArtifact.artifact_kind == "feed_item_routing_v1")
            .order_by(InsightEngineArtifact.id.desc())
            .limit(1)
        )
        assert artifact is not None
        assert isinstance(artifact.artifact_payload, dict)
        return artifact.artifact_payload


def _valid_routing_payload() -> dict:
    return {
        "schema_version": "feed_item_routing_v1",
        "primary_channel_id": "ai_agents_frontier",
        "secondary_channels": [{"channel_id": "open_source_tools_practice", "confidence": 0.42, "display_mode": "list"}],
        "reading_depth": "medium",
        "flow_lane": "main",
        "confidence": 0.82,
        "reason_codes": ["source_channel_tendency_match"],
        "promotion_reason_codes": [],
        "isolation_reason_codes": [],
        "source_policy_applied": "normal",
        "notes": "Test routing",
    }


def build_test_insight_profile_for_homepage() -> dict:
    return {
        "schema_version": "insight_profile_v1",
        "profile_name": "Homepage Test Insight Profile",
        "profile_version": "homepage-profile-v1",
        "summary_guidelines": ["Mention concrete value for the reader."],
        "active_focus": [
            {
                "topic_id": "software_systems",
                "note": "Prioritize local deployment and data-boundary details.",
                "weight": 90,
            }
        ],
        "long_term_interests": [],
        "positive_signals": ["Implementation details and reproducible steps."],
        "negative_signals": ["Thin marketing updates."],
        "topic_preferences": [],
        "content_type_preferences": [],
        "evidence_references": [],
    }
