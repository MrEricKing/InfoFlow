from pathlib import Path
from datetime import datetime, timezone
import os

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

from app.core.config import reset_settings_cache
from app.db.base import Base
from app.db.models import (
    InsightEngineArtifact,
    FeedItem,
    InsightChannel,
    ItemState,
    SavedItemCuratedInsight,
    SavedItemCuratedInsightEvent,
    Source,
    SourceChannelTendency,
    SourceItemOccurrence,
)
from app.db.session import get_session_factory, reset_session_state
from app.services.insight_engine_artifacts import (
    FEED_ITEM_ROUTING_ARTIFACT_KIND,
    FeedItemInsightRoutingArtifactPayload,
    FeedItemInsightTopicsArtifactPayload,
    resolve_routing,
)
from app.services.insight_channels import DEFAULT_INSIGHT_CHANNELS as DEFAULT_INSIGHT_CHANNEL_FIXTURES


EXPECTED_DEFAULT_CHANNELS = [
    {
        **row,
        "enabled": True,
        "is_system": bool(row.get("is_system")),
        "retired_at": None,
        "channel_catalog_version": 1,
    }
    for row in DEFAULT_INSIGHT_CHANNEL_FIXTURES
]


def test_get_insight_channels_seeds_defaults_when_database_is_empty(client) -> None:
    response = client.get("/api/settings/insight-channels")

    assert response.status_code == 200
    assert response.json() == EXPECTED_DEFAULT_CHANNELS


def test_get_insight_channels_inserts_missing_defaults_without_overwriting_existing_rows(client) -> None:
    with get_session_factory()() as session:
        session.add(
            InsightChannel(
                id="academic_horizon",
                label="Custom academic channel",
                description="Custom description",
                reading_depth="light",
                sort_order=3,
                enabled=False,
            )
        )
        session.commit()

    response = client.get("/api/settings/insight-channels")

    assert response.status_code == 200
    channels = {channel["id"]: channel for channel in response.json()}
    assert set(channels) == {channel["id"] for channel in EXPECTED_DEFAULT_CHANNELS}
    academic_defaults = next(channel for channel in EXPECTED_DEFAULT_CHANNELS if channel["id"] == "academic_horizon")
    assert channels["academic_horizon"] == {
        "id": "academic_horizon",
        "label": "Custom academic channel",
        "description": "Custom description",
        "focus_prompt": academic_defaults["focus_prompt"],
        "boundary_prompt": academic_defaults["boundary_prompt"],
        "reading_depth": "light",
        "default_flow_policy": academic_defaults["default_flow_policy"],
        "sort_order": 3,
        "enabled": False,
        "is_system": False,
        "retired_at": None,
        "channel_catalog_version": 1,
    }
    cryptocurrency_defaults = next(channel for channel in EXPECTED_DEFAULT_CHANNELS if channel["id"] == "cryptocurrency")
    assert channels["cryptocurrency"] == cryptocurrency_defaults


def test_disabled_insight_channels_remain_readable_by_settings_api(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200

    patch_response = client.patch(
        "/api/settings/insight-channels/cryptocurrency",
        json={"enabled": False, "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"]},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["enabled"] is False

    read_response = client.get("/api/settings/insight-channels")
    assert read_response.status_code == 200
    channels = {channel["id"]: channel for channel in read_response.json()}
    assert channels["cryptocurrency"]["enabled"] is False


def test_patch_insight_channel_updates_all_editable_fields(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200

    response = client.patch(
        "/api/settings/insight-channels/academic_horizon",
        json={
            "label": "Academic Horizon Updated",
            "description": "Updated description",
            "reading_depth": "light",
            "sort_order": 5,
            "enabled": False,
            "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
        },
    )

    assert response.status_code == 200
    academic_defaults = next(channel for channel in EXPECTED_DEFAULT_CHANNELS if channel["id"] == "academic_horizon")
    assert response.json() == {
        "id": "academic_horizon",
        "label": "Academic Horizon Updated",
        "description": "Updated description",
        "focus_prompt": academic_defaults["focus_prompt"],
        "boundary_prompt": academic_defaults["boundary_prompt"],
        "reading_depth": "light",
        "default_flow_policy": academic_defaults["default_flow_policy"],
        "sort_order": 5,
        "enabled": False,
        "is_system": False,
        "retired_at": None,
        "channel_catalog_version": 2,
    }

    persisted = client.get("/api/settings/insight-channels").json()
    updated = next(channel for channel in persisted if channel["id"] == "academic_horizon")
    assert updated["label"] == "Academic Horizon Updated"
    assert updated["sort_order"] == 5


def test_patch_insight_channel_returns_404_for_unknown_channel(client) -> None:
    response = client.patch(
        "/api/settings/insight-channels/missing_channel",
        json={"enabled": False, "expected_channel_catalog_version": 1},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Insight channel not found"


def test_patch_insight_channel_rejects_invalid_reading_depth(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    response = client.patch(
        "/api/settings/insight-channels/academic_horizon",
        json={
            "reading_depth": "full",
            "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
        },
    )

    assert response.status_code == 422


def test_patch_insight_channel_rejects_explicit_null_editable_fields(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    response = client.patch(
        "/api/settings/insight-channels/academic_horizon",
        json={"label": None, "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"]},
    )

    assert response.status_code == 422


def test_patch_insight_channel_rejects_extra_fields(client) -> None:
    seed_response = client.get("/api/settings/insight-channels")
    assert seed_response.status_code == 200
    response = client.patch(
        "/api/settings/insight-channels/academic_horizon",
        json={
            "enabled": True,
            "expected_channel_catalog_version": seed_response.json()[0]["channel_catalog_version"],
            "unexpected": "value",
        },
    )

    assert response.status_code == 422


def test_get_source_insight_routing_returns_default_policy_and_empty_tendencies(client) -> None:
    source_id = _create_source(client)

    response = client.get(f"/api/sources/{source_id}/insight-routing")

    assert response.status_code == 200
    assert response.json() == {
        "source_id": source_id,
        "flow_policy": "normal",
        "channel_tendencies": [],
    }


def test_get_source_insight_routing_returns_404_for_unknown_source(client) -> None:
    response = client.get("/api/sources/999999/insight-routing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found"


def test_patch_source_insight_routing_persists_policy_and_sorted_tendencies(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "mixed_high_frequency",
            "channel_tendencies": [
                {
                    "channel_id": "ai_agents_frontier",
                    "tendency": 0.9,
                    "is_primary": False,
                    "notes": "secondary",
                },
                {
                    "channel_id": "tcp_artificial_muscle_core",
                    "tendency": 0.4,
                    "is_primary": True,
                    "notes": "main",
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "source_id": source_id,
        "flow_policy": "mixed_high_frequency",
        "channel_tendencies": [
            {
                "channel_id": "tcp_artificial_muscle_core",
                "tendency": 0.4,
                "is_primary": True,
                "notes": "main",
            },
            {
                "channel_id": "ai_agents_frontier",
                "tendency": 0.9,
                "is_primary": False,
                "notes": "secondary",
            },
        ],
    }

    read_response = client.get(f"/api/sources/{source_id}/insight-routing")
    assert read_response.status_code == 200
    assert read_response.json() == response.json()


def test_source_insight_routing_sorts_primary_first_then_tendency_and_channel_id(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "cryptocurrency",
                    "tendency": 0.4,
                    "is_primary": False,
                    "notes": "lowest",
                },
                {
                    "channel_id": "open_source_tools_practice",
                    "tendency": 0.8,
                    "is_primary": False,
                    "notes": "tie second by id",
                },
                {
                    "channel_id": "tcp_artificial_muscle_core",
                    "tendency": 0.2,
                    "is_primary": True,
                    "notes": "primary despite lower tendency",
                },
                {
                    "channel_id": "ai_agents_frontier",
                    "tendency": 0.8,
                    "is_primary": False,
                    "notes": "tie first by id",
                },
            ],
        },
    )

    assert response.status_code == 200
    assert [item["channel_id"] for item in response.json()["channel_tendencies"]] == [
        "tcp_artificial_muscle_core",
        "ai_agents_frontier",
        "open_source_tools_practice",
        "cryptocurrency",
    ]


def test_source_insight_routing_update_rejects_unknown_channel_ids(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {"channel_id": "missing_channel", "tendency": 0.5, "is_primary": True, "notes": ""},
            ],
        },
    )

    assert response.status_code == 400
    assert "Insight channel is not active or does not exist" in response.json()["detail"]


def test_source_insight_routing_update_rejects_multiple_primary_tendencies(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "tcp_artificial_muscle_core",
                    "tendency": 0.6,
                    "is_primary": True,
                    "notes": "",
                },
                {"channel_id": "cryptocurrency", "tendency": 0.2, "is_primary": True, "notes": ""},
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "At most one source channel tendency can be primary"


def test_source_insight_routing_update_rejects_invalid_flow_policy(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "urgent",
            "channel_tendencies": [],
        },
    )

    assert response.status_code == 422


def test_source_insight_routing_update_rejects_out_of_range_tendency(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "tcp_artificial_muscle_core",
                    "tendency": 1.1,
                    "is_primary": False,
                    "notes": "",
                },
            ],
        },
    )

    assert response.status_code == 422


def test_source_insight_routing_update_rejects_extra_fields(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [],
            "unexpected": "value",
        },
    )

    assert response.status_code == 422


def test_source_insight_routing_update_rejects_nested_extra_fields(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "tcp_artificial_muscle_core",
                    "tendency": 0.5,
                    "is_primary": False,
                    "notes": "",
                    "unexpected": "value",
                }
            ],
        },
    )

    assert response.status_code == 422


def test_source_insight_routing_update_accepts_zero_primary_tendencies(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "ai_agents_frontier",
                    "tendency": 0.8,
                    "is_primary": False,
                    "notes": "non-primary",
                },
                {
                    "channel_id": "cryptocurrency",
                    "tendency": 0.2,
                    "is_primary": False,
                    "notes": "also non-primary",
                },
            ],
        },
    )

    assert response.status_code == 200
    assert all(item["is_primary"] is False for item in response.json()["channel_tendencies"])


def test_source_insight_routing_update_returns_404_for_unknown_source(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    response = client.patch(
        "/api/sources/999999/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [],
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found"


def test_source_insight_routing_update_replaces_tendencies_atomically(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = _create_source(client)

    initial_response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "normal",
            "channel_tendencies": [
                {
                    "channel_id": "tcp_artificial_muscle_core",
                    "tendency": 0.8,
                    "is_primary": True,
                    "notes": "keep until valid replacement",
                },
                {"channel_id": "cryptocurrency", "tendency": 0.3, "is_primary": False, "notes": "old"},
            ],
        },
    )
    assert initial_response.status_code == 200

    replacement_response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "high_frequency_brief",
            "channel_tendencies": [
                {
                    "channel_id": "open_source_tools_practice",
                    "tendency": 1.0,
                    "is_primary": True,
                    "notes": "replacement",
                }
            ],
        },
    )
    assert replacement_response.status_code == 200
    assert replacement_response.json()["channel_tendencies"] == [
        {
            "channel_id": "open_source_tools_practice",
            "tendency": 1.0,
            "is_primary": True,
            "notes": "replacement",
        }
    ]

    invalid_response = client.patch(
        f"/api/sources/{source_id}/insight-routing",
        json={
            "flow_policy": "mixed_high_frequency",
            "channel_tendencies": [
                {"channel_id": "missing_channel", "tendency": 0.1, "is_primary": False, "notes": "invalid"}
            ],
        },
    )
    assert invalid_response.status_code == 400

    read_response = client.get(f"/api/sources/{source_id}/insight-routing")
    assert read_response.status_code == 200
    assert read_response.json() == replacement_response.json()


def test_homepage_routing_resolver_uses_valid_insight_engine_artifact(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        source = Source(
            name="Artifact Routed Source",
            kind="rss",
            url="https://artifact-routing.example.com/feed.xml",
            is_active=True,
            ai_route="general",
            provenance="manual",
        )
        artifact = InsightEngineArtifact(
            feed_item_id=1,
            saved_item_id=None,
            artifact_kind=FEED_ITEM_ROUTING_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="insight-engine-v2-routing-v1",
            artifact_payload={
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "ai_agents_frontier",
                "secondary_channels": [
                    {"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "list"}
                ],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.8,
                "reason_codes": ["source_channel_tendency_match"],
                "promotion_reason_codes": ["high_confidence_primary_channel"],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "AI route",
                "source": "insight_engine_artifact",
            },
        )
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}

        resolution = resolve_routing(
            feed_item=FeedItem(title="Artifact routed item"),
            source=source,
            artifacts_by_kind={FEED_ITEM_ROUTING_ARTIFACT_KIND: artifact},
            channels_by_id=channels_by_id,
            source_tendencies=[],
        )

    assert resolution.model_dump() == {
        "primary_channel_id": "ai_agents_frontier",
        "secondary_channels": [{"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "list"}],
        "reading_depth": "medium",
        "flow_lane": "main",
        "confidence": 0.8,
        "reason_codes": ["source_channel_tendency_match"],
        "promotion_reason_codes": ["high_confidence_primary_channel"],
        "isolation_reason_codes": [],
        "source_policy_applied": "normal",
        "notes": "AI route",
        "source": "insight_engine_artifact",
    }


@pytest.mark.parametrize("artifact_flow_lane", ["main", "brief_pool"])
def test_homepage_routing_resolver_forces_failed_extraction_artifact_to_needs_review(
    client, artifact_flow_lane
) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        source = Source(
            name="Failed Extraction Artifact Source",
            kind="rss",
            url="https://failed-extraction-artifact.example.com/feed.xml",
            is_active=True,
            ai_route="general",
            provenance="manual",
        )
        feed_item = FeedItem(
            title="Failed extraction artifact item",
            summary_text="https://failed-extraction-artifact.example.com/posts/1",
            content_text=None,
            content_extraction_status="failed",
        )
        artifact = InsightEngineArtifact(
            feed_item_id=1,
            saved_item_id=None,
            artifact_kind=FEED_ITEM_ROUTING_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="insight-engine-v2-routing-v1",
            artifact_payload={
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "ai_agents_frontier",
                "secondary_channels": [
                    {"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "list"}
                ],
                "reading_depth": "medium",
                "flow_lane": artifact_flow_lane,
                "confidence": 0.8,
                "reason_codes": ["source_channel_tendency_match"],
                "promotion_reason_codes": ["high_confidence_primary_channel"],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "AI route",
                "source": "insight_engine_artifact",
            },
        )
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}

        resolution = resolve_routing(
            feed_item=feed_item,
            source=source,
            artifacts_by_kind={FEED_ITEM_ROUTING_ARTIFACT_KIND: artifact},
            channels_by_id=channels_by_id,
            source_tendencies=[],
        )

    assert resolution.primary_channel_id == "ai_agents_frontier"
    assert resolution.secondary_channels[0].channel_id == "open_source_tools_practice"
    assert resolution.reading_depth == "medium"
    assert resolution.flow_lane == "needs_review"
    assert resolution.confidence == 0.8
    assert resolution.reason_codes == ["source_channel_tendency_match", "extraction_failed_needs_review"]
    assert resolution.isolation_reason_codes == ["extraction_failed_needs_review"]
    assert resolution.reason_codes.count("extraction_failed_needs_review") == 1
    assert resolution.isolation_reason_codes.count("extraction_failed_needs_review") == 1
    assert resolution.source_policy_applied == "normal"
    assert resolution.notes == "AI route"
    assert resolution.source == "insight_engine_artifact"


def test_homepage_routing_resolver_falls_back_for_unknown_artifact_channel(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        source = Source(
            name="Fallback Routed Source",
            kind="rss",
            url="https://fallback-routing.example.com/feed.xml",
            is_active=True,
            ai_route="general",
            flow_policy="high_frequency_brief",
            provenance="manual",
        )
        artifact = InsightEngineArtifact(
            feed_item_id=1,
            saved_item_id=None,
            artifact_kind=FEED_ITEM_ROUTING_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="insight-engine-v2-routing-v1",
            artifact_payload={
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "missing_channel",
                "secondary_channels": [],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.8,
                "reason_codes": ["content_topic_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Invalid route",
                "source": "insight_engine_artifact",
            },
        )
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}
        tendencies = [
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

        resolution = resolve_routing(
            feed_item=FeedItem(title="Fallback item"),
            source=source,
            artifacts_by_kind={FEED_ITEM_ROUTING_ARTIFACT_KIND: artifact},
            channels_by_id=channels_by_id,
            source_tendencies=tendencies,
        )

    assert resolution.primary_channel_id == "tcp_artificial_muscle_core"
    assert resolution.reading_depth == "deep"
    assert resolution.flow_lane == "brief_pool"
    assert resolution.confidence == 0.9
    assert resolution.secondary_channels[0].channel_id == "ai_agents_frontier"
    assert resolution.source == "deterministic_fallback"


def test_homepage_routing_resolver_falls_back_for_blank_primary_channel(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        source = Source(
            name="Blank Primary Routed Source",
            kind="rss",
            url="https://blank-primary-routing.example.com/feed.xml",
            is_active=True,
            ai_route="general",
            provenance="manual",
        )
        artifact = InsightEngineArtifact(
            feed_item_id=1,
            saved_item_id=None,
            artifact_kind=FEED_ITEM_ROUTING_ARTIFACT_KIND,
            model_name="test/model",
            prompt_version="insight-engine-v2-routing-v1",
            artifact_payload={
                "schema_version": "feed_item_routing_v1",
                "primary_channel_id": "",
                "secondary_channels": [],
                "reading_depth": "medium",
                "flow_lane": "main",
                "confidence": 0.8,
                "reason_codes": ["content_topic_match"],
                "promotion_reason_codes": [],
                "isolation_reason_codes": [],
                "source_policy_applied": "normal",
                "notes": "Blank primary",
                "source": "insight_engine_artifact",
            },
        )
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}
        tendencies = [
            SourceChannelTendency(
                source_id=source.id,
                channel_id="ai_agents_frontier",
                tendency=0.7,
                is_primary=True,
                notes="fallback primary",
            )
        ]

        resolution = resolve_routing(
            feed_item=FeedItem(title="Fallback item"),
            source=source,
            artifacts_by_kind={FEED_ITEM_ROUTING_ARTIFACT_KIND: artifact},
            channels_by_id=channels_by_id,
            source_tendencies=tendencies,
        )

    assert resolution.primary_channel_id == "ai_agents_frontier"
    assert resolution.source == "deterministic_fallback"


def test_feed_item_routing_v1_accepts_full_spec_payload(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    payload = {
        "schema_version": "feed_item_routing_v1",
        "primary_channel_id": "ai_agents_frontier",
        "secondary_channels": [
            {"channel_id": "open_source_tools_practice", "confidence": 0.82, "display_mode": "list"},
            {"channel_id": "academic_horizon", "confidence": 0.48, "display_mode": "tag_only"},
        ],
        "reading_depth": "medium",
        "flow_lane": "main",
        "confidence": 0.86,
        "reason_codes": ["source_channel_tendency_match", "cross_channel_tooling"],
        "promotion_reason_codes": [],
        "isolation_reason_codes": [],
        "source_policy_applied": "mixed_high_frequency",
        "notes": "Technical implementation update with reusable engineering value.",
        "channel_catalog_version": 3,
        "routing_breakdown": {
            "channel_catalog_version": 3,
            "candidates": [
                {
                    "channel_id": "ai_agents_frontier",
                    "source": "llm_candidate",
                    "confidence": 0.86,
                    "reason_codes": ["source_channel_tendency_match"],
                    "accepted": True,
                    "rejected_reason": None,
                    "rank": 1,
                }
            ],
            "decision": {
                "primary_channel_id": "ai_agents_frontier",
                "secondary_channel_ids": ["open_source_tools_practice", "academic_horizon"],
                "routing_source": "insight_engine_artifact",
                "routing_reason_codes": ["source_channel_tendency_match", "cross_channel_tooling"],
                "fallback_reason": None,
            },
        },
    }

    parsed = FeedItemInsightRoutingArtifactPayload.model_validate(payload)

    assert parsed.schema_version == "feed_item_routing_v1"
    assert parsed.secondary_channels[1].display_mode == "tag_only"
    assert parsed.channel_catalog_version == 3
    assert parsed.routing_breakdown["decision"]["primary_channel_id"] == "ai_agents_frontier"


def test_homepage_routing_payload_rejects_removed_homepage_schema_version(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    payload = {
        "schema_version": "homepage_routing_v1",
        "primary_channel_id": "ai_agents_frontier",
        "secondary_channels": [],
        "reading_depth": "medium",
        "flow_lane": "main",
        "confidence": 0.86,
        "reason_codes": ["source_channel_tendency_match"],
        "promotion_reason_codes": [],
        "isolation_reason_codes": [],
        "source_policy_applied": "normal",
        "notes": "Removed legacy schema.",
    }

    with pytest.raises(ValueError, match="feed_item_routing_v1"):
        FeedItemInsightRoutingArtifactPayload.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "homepage_routing_v0"),
        ("reading_depth", "full"),
        ("flow_lane", "urgent"),
        ("reason_codes", ["unknown_routing_reason"]),
        ("promotion_reason_codes", ["unknown_promotion_reason"]),
        ("isolation_reason_codes", ["unknown_isolation_reason"]),
    ],
)
def test_homepage_routing_payload_rejects_invalid_contract_fields(field, value) -> None:
    payload = _full_routing_payload()
    payload[field] = value

    with pytest.raises(ValueError):
        FeedItemInsightRoutingArtifactPayload.model_validate(payload)


def test_homepage_routing_payload_allows_dynamic_channel_ids() -> None:
    payload = _full_routing_payload()
    payload["primary_channel_id"] = "custom_dynamic_channel"
    payload["secondary_channels"] = [{"channel_id": "another_dynamic_channel", "confidence": 0.4, "display_mode": "list"}]

    parsed = FeedItemInsightRoutingArtifactPayload.model_validate(payload)

    assert parsed.primary_channel_id == "custom_dynamic_channel"
    assert parsed.secondary_channels[0].channel_id == "another_dynamic_channel"


@pytest.mark.parametrize(
    "secondary_channels",
    [
        [
            {"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "list"},
            {"channel_id": "open_source_tools_practice", "confidence": 0.3, "display_mode": "list"},
        ],
        [{"channel_id": "ai_agents_frontier", "confidence": 0.4, "display_mode": "list"}],
        [{"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "card"}],
    ],
)
def test_homepage_routing_payload_rejects_invalid_secondary_channels(secondary_channels) -> None:
    payload = _full_routing_payload()
    payload["secondary_channels"] = secondary_channels

    with pytest.raises(ValueError):
        FeedItemInsightRoutingArtifactPayload.model_validate(payload)


def test_homepage_routing_resolver_fallback_uses_high_frequency_flow_policy_without_channel() -> None:
    source = Source(
        name="No Routing Source",
        kind="rss",
        url="https://no-routing.example.com/feed.xml",
        is_active=True,
        ai_route="general",
        flow_policy="high_frequency_brief",
        provenance="manual",
    )

    resolution = resolve_routing(feed_item=FeedItem(title="No signal item"), source=source, artifacts_by_kind=None)

    assert resolution.model_dump() == {
        "primary_channel_id": "needs_review",
        "secondary_channels": [],
        "reading_depth": None,
        "flow_lane": "brief_pool",
        "confidence": 0.0,
        "reason_codes": ["source_flow_policy_high_frequency", "ambiguous_channel_needs_review"],
        "promotion_reason_codes": [],
        "isolation_reason_codes": ["source_policy_high_frequency_brief", "ambiguous_channel_needs_review"],
        "source_policy_applied": "high_frequency_brief",
        "notes": None,
        "source": "deterministic_fallback",
    }


@pytest.mark.parametrize(
    ("flow_policy", "expected_flow_lane", "expected_reasons"),
    [
        (None, "needs_review", ["ambiguous_channel_needs_review"]),
        ("normal", "needs_review", ["ambiguous_channel_needs_review"]),
        ("mixed_high_frequency", "needs_review", ["source_flow_policy_high_frequency", "ambiguous_channel_needs_review"]),
    ],
)
def test_homepage_routing_resolver_fallback_flow_policy_defaults_to_main_without_channel(
    flow_policy, expected_flow_lane, expected_reasons
) -> None:
    source = Source(
        name="No Routing Source",
        kind="rss",
        url="https://no-routing.example.com/feed.xml",
        is_active=True,
        ai_route="general",
        provenance="manual",
    )
    source.flow_policy = flow_policy

    resolution = resolve_routing(feed_item=FeedItem(title="No signal item"), source=source, artifacts_by_kind=None)

    assert resolution.primary_channel_id == "needs_review"
    assert resolution.confidence == 0.0
    assert resolution.flow_lane == expected_flow_lane
    assert resolution.reason_codes == expected_reasons


def test_fallback_without_any_signal_routes_to_needs_review(client) -> None:
    with get_session_factory()() as session:
        source = Source(
            name="No Signal Source",
            kind="rss",
            url="https://no-signal.example.com/feed.xml",
            site_url="https://no-signal.example.com",
            folder_path=None,
            flow_policy="normal",
        )
        session.add(source)
        session.flush()
        item = FeedItem(
            original_url="https://no-signal.example.com/posts/1",
            canonical_url="https://no-signal.example.com/posts/1",
            title="No Signal Story",
            published_at=datetime(2026, 3, 28, 10, 0, 0, tzinfo=timezone.utc),
            summary_text="Plain summary without routing clues.",
            content_text="Plain content without routing clues.",
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
        session.commit()

    response = client.get("/api/homepage/items")
    assert response.status_code == 200
    card = response.json()["items"][0]
    assert card["routing_source"] == "deterministic_fallback"
    assert card["flow_lane"] == "needs_review"
    assert card["primary_channel_id"] == "needs_review"


def test_homepage_routing_fallback_single_strong_source_tendency_becomes_primary(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}
        source = Source(
            name="Single Strong Source",
            kind="rss",
            url="https://single-strong.example.com/feed.xml",
            flow_policy="normal",
            provenance="manual",
        )
        tendency = SourceChannelTendency(
            source_id=1,
            channel_id="ai_agents_frontier",
            tendency=0.88,
            is_primary=False,
            notes="strong source tendency",
        )

        resolution = resolve_routing(
            feed_item=FeedItem(title="Single strong tendency"),
            source=source,
            artifacts_by_kind=None,
            channels_by_id=channels_by_id,
            source_tendencies=[tendency],
        )

    assert resolution.primary_channel_id == "ai_agents_frontier"
    assert resolution.secondary_channels == []
    assert resolution.flow_lane == "main"
    assert resolution.confidence == 0.88
    assert resolution.reason_codes == ["source_channel_tendency_match"]


def test_homepage_routing_fallback_multiple_tendencies_use_topics_for_primary(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}
        source = Source(
            name="Multi Tendency Source",
            kind="rss",
            url="https://multi-tendency.example.com/feed.xml",
            flow_policy="normal",
            provenance="manual",
        )
        tendencies = [
            SourceChannelTendency(
                source_id=1,
                channel_id="open_source_tools_practice",
                tendency=0.82,
                is_primary=False,
                notes="engineering source tendency",
            ),
            SourceChannelTendency(
                source_id=1,
                channel_id="ai_agents_frontier",
                tendency=0.72,
                is_primary=False,
                notes="AI source tendency",
            ),
        ]

        resolution = resolve_routing(
            feed_item=FeedItem(title="Agent routing update"),
            source=source,
            artifacts_by_kind=None,
            latest_topics_payload=FeedItemInsightTopicsArtifactPayload(
                labels=[],
                source_kind="ai_generated",
                topic_ids=["ai_technology"],
                content_type="analysis",
            ),
            channels_by_id=channels_by_id,
            source_tendencies=tendencies,
        )

    assert resolution.primary_channel_id == "ai_agents_frontier"
    assert [secondary.channel_id for secondary in resolution.secondary_channels] == ["open_source_tools_practice"]
    assert resolution.reason_codes == ["source_channel_tendency_match", "content_topic_match"]


def test_homepage_routing_fallback_high_frequency_defaults_to_brief_pool(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        channels_by_id = {channel.id: channel for channel in session.query(InsightChannel).all()}
        source = Source(
            name="High Frequency Source",
            kind="rss",
            url="https://brief.example.com/feed.xml",
            flow_policy="high_frequency_brief",
            provenance="manual",
        )
        tendency = SourceChannelTendency(
            source_id=1,
            channel_id="open_source_tools_practice",
            tendency=0.7,
            is_primary=False,
            notes="release stream",
        )

        resolution = resolve_routing(
            feed_item=FeedItem(title="Brief release"),
            source=source,
            artifacts_by_kind=None,
            latest_topics_payload=FeedItemInsightTopicsArtifactPayload(
                labels=[],
                source_kind="ai_generated",
                topic_ids=["tools_products"],
                content_type="release_note",
            ),
            channels_by_id=channels_by_id,
            source_tendencies=[tendency],
        )

    assert resolution.primary_channel_id == "open_source_tools_practice"
    assert resolution.flow_lane == "brief_pool"
    assert resolution.reason_codes == ["source_channel_tendency_match", "source_flow_policy_high_frequency"]
    assert resolution.isolation_reason_codes == ["source_policy_high_frequency_brief"]


@pytest.mark.parametrize(
    ("content_type", "expected_depth"),
    [
        ("research_paper", "deep"),
        ("reference", "medium"),
    ],
)
def test_homepage_routing_fallback_long_content_type_depth_without_channel_default(
    content_type, expected_depth
) -> None:
    source = Source(
        name="Long Form Source",
        kind="rss",
        url="https://long-form.example.com/feed.xml",
        flow_policy="normal",
        provenance="manual",
    )
    tendency = SourceChannelTendency(
        source_id=1,
        channel_id="academic_horizon",
        tendency=0.76,
        is_primary=False,
        notes="academic source",
    )

    resolution = resolve_routing(
        feed_item=FeedItem(title="Research content"),
        source=source,
        artifacts_by_kind=None,
        latest_topics_payload=FeedItemInsightTopicsArtifactPayload(
            labels=[],
            source_kind="ai_generated",
            topic_ids=["science_research"],
            content_type=content_type,
        ),
        channels_by_id=None,
        source_tendencies=[tendency],
    )

    assert resolution.reading_depth == expected_depth
    assert "reading_depth_policy" in resolution.reason_codes


@pytest.mark.parametrize("content_type", ["release_note", "news_update"])
def test_homepage_routing_fallback_short_update_content_type_depth_without_channel_default(content_type) -> None:
    source = Source(
        name="Short Update Source",
        kind="rss",
        url="https://short-update.example.com/feed.xml",
        flow_policy="normal",
        provenance="manual",
    )
    tendency = SourceChannelTendency(
        source_id=1,
        channel_id="open_source_tools_practice",
        tendency=0.76,
        is_primary=False,
        notes="update stream",
    )

    resolution = resolve_routing(
        feed_item=FeedItem(title="Short update"),
        source=source,
        artifacts_by_kind=None,
        latest_topics_payload=FeedItemInsightTopicsArtifactPayload(
            labels=[],
            source_kind="ai_generated",
            topic_ids=["tools_products"],
            content_type=content_type,
        ),
        channels_by_id=None,
        source_tendencies=[tendency],
    )

    assert resolution.reading_depth == "light"
    assert "reading_depth_policy" in resolution.reason_codes


def test_homepage_routing_fallback_failed_empty_extraction_needs_review() -> None:
    source = Source(
        name="Extraction Failed Source",
        kind="rss",
        url="https://extract-failed.example.com/feed.xml",
        flow_policy="normal",
        provenance="manual",
    )
    feed_item = FeedItem(
        title="Extraction failed",
        summary_text="https://extract-failed.example.com/post",
        content_text=None,
        content_extraction_status="failed",
    )

    resolution = resolve_routing(feed_item=feed_item, source=source, artifacts_by_kind=None)

    assert resolution.primary_channel_id == "needs_review"
    assert resolution.flow_lane == "needs_review"
    assert "extraction_failed_needs_review" in resolution.reason_codes
    assert "extraction_failed_needs_review" in resolution.isolation_reason_codes


def test_homepage_routing_fallback_high_frequency_failed_extraction_needs_review() -> None:
    source = Source(
        name="High Frequency Extraction Failed Source",
        kind="rss",
        url="https://high-frequency-extract-failed.example.com/feed.xml",
        flow_policy="high_frequency_brief",
        provenance="manual",
    )
    feed_item = FeedItem(
        title="High frequency extraction failed",
        summary_text="https://high-frequency-extract-failed.example.com/post",
        content_text=None,
        content_extraction_status="failed",
    )
    tendency = SourceChannelTendency(
        source_id=1,
        channel_id="academic_horizon",
        tendency=0.9,
        is_primary=True,
        notes="research-heavy source",
    )

    resolution = resolve_routing(
        feed_item=feed_item,
        source=source,
        artifacts_by_kind=None,
        latest_topics_payload=FeedItemInsightTopicsArtifactPayload(
            labels=[],
            source_kind="ai_generated",
            topic_ids=["science_research"],
            content_type="research_paper",
        ),
        source_tendencies=[tendency],
    )

    assert resolution.primary_channel_id == "academic_horizon"
    assert resolution.flow_lane == "needs_review"
    assert "extraction_failed_needs_review" in resolution.reason_codes
    assert "extraction_failed_needs_review" in resolution.isolation_reason_codes
    assert "long_analysis_promoted_from_high_frequency" not in resolution.promotion_reason_codes


def test_insight_engine_v2_tables_are_registered_in_metadata() -> None:
    assert "insight_engine_artifacts" in Base.metadata.tables
    removed_table_name = "feed_item_" + "channel_feedback"
    assert removed_table_name not in Base.metadata.tables
    assert "insight_channels" in Base.metadata.tables
    assert "source_channel_tendencies" in Base.metadata.tables


def test_source_flow_policy_model_default_semantics_are_present() -> None:
    flow_policy = Source.__table__.c.flow_policy

    assert flow_policy.type.length == 64
    assert flow_policy.nullable is False
    assert flow_policy.default is not None
    assert flow_policy.default.arg == "normal"
    assert flow_policy.server_default is not None
    assert flow_policy.server_default.arg == "normal"


def test_insight_channel_required_columns_and_defaults_are_present() -> None:
    table = InsightChannel.__table__

    assert table.c.id.primary_key is True
    assert table.c.id.type.length == 128
    assert table.c.label.type.length == 255
    assert table.c.label.nullable is False
    assert table.c.description.nullable is False
    assert table.c.description.default is not None
    assert table.c.description.default.arg == ""
    assert table.c.focus_prompt.nullable is False
    assert table.c.focus_prompt.default is not None
    assert table.c.focus_prompt.default.arg == ""
    assert table.c.boundary_prompt.nullable is False
    assert table.c.boundary_prompt.default is not None
    assert table.c.boundary_prompt.default.arg == ""
    assert table.c.reading_depth.type.length == 32
    assert table.c.reading_depth.nullable is False
    assert table.c.reading_depth.default is not None
    assert table.c.reading_depth.default.arg == "medium"
    assert table.c.default_flow_policy.type.length == 32
    assert table.c.default_flow_policy.nullable is False
    assert table.c.default_flow_policy.default is not None
    assert table.c.default_flow_policy.default.arg == "normal"
    assert table.c.sort_order.nullable is False
    assert table.c.sort_order.default is not None
    assert table.c.sort_order.default.arg == 0
    assert table.c.enabled.nullable is False
    assert table.c.enabled.default is not None
    assert table.c.enabled.default.arg is True
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None
    assert table.c.updated_at.nullable is False
    assert table.c.updated_at.server_default is not None
    assert table.c.updated_at.onupdate is not None


def test_source_channel_tendency_required_columns_defaults_and_links_are_present() -> None:
    table = SourceChannelTendency.__table__

    assert table.c.id.primary_key is True
    assert not table.c.id.index
    assert table.c.source_id.nullable is False
    assert table.c.source_id.index is True
    assert {fk.target_fullname for fk in table.c.source_id.foreign_keys} == {"sources.id"}
    assert table.c.channel_id.type.length == 128
    assert table.c.channel_id.nullable is False
    assert table.c.channel_id.index is True
    assert {fk.target_fullname for fk in table.c.channel_id.foreign_keys} == {"insight_channels.id"}
    assert table.c.tendency.nullable is False
    assert table.c.tendency.default is not None
    assert table.c.tendency.default.arg == 0.5
    assert table.c.is_primary.nullable is False
    assert table.c.is_primary.default is not None
    assert table.c.is_primary.default.arg is False
    assert table.c.notes.nullable is False
    assert table.c.notes.default is not None
    assert table.c.notes.default.arg == ""
    assert table.c.created_at.nullable is False
    assert table.c.created_at.server_default is not None
    assert table.c.updated_at.nullable is False
    assert table.c.updated_at.server_default is not None
    assert table.c.updated_at.onupdate is not None
    unique_constraints = {constraint.name for constraint in table.constraints if len(constraint.columns) == 2}
    assert "uq_source_channel_tendencies_source_channel" in unique_constraints
    assert SourceChannelTendency.source.property.mapper.class_ is Source
    assert SourceChannelTendency.channel.property.mapper.class_ is InsightChannel


def test_insight_engine_artifact_is_primary_application_model_name() -> None:
    assert InsightEngineArtifact.__tablename__ == "insight_engine_artifacts"


def test_curated_insight_artifact_foreign_keys_point_to_insight_engine_artifacts() -> None:
    curated_fk_targets = {
        fk.target_fullname for fk in SavedItemCuratedInsight.__table__.c.base_insight_engine_artifact_id.foreign_keys
    }
    event_fk_targets = {
        fk.target_fullname for fk in SavedItemCuratedInsightEvent.__table__.c.base_insight_engine_artifact_id.foreign_keys
    }

    assert curated_fk_targets == {"insight_engine_artifacts.id"}
    assert event_fk_targets == {"insight_engine_artifacts.id"}


def test_alembic_fresh_baseline_applies_insight_engine_v2_routing_schema(sqlite_database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    os.environ["DATABASE_URL"] = sqlite_database_url
    reset_settings_cache()
    reset_session_state()
    engine = create_engine(sqlite_database_url)

    try:
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO sources (name, kind, url, is_active, ai_route, provenance)
                    VALUES ('Existing Source', 'rss', 'https://example.com/feed.xml', 1, 'general', 'manual')
                    """
                )
            )

        upgraded_inspector = inspect(engine)
        upgraded_tables = set(upgraded_inspector.get_table_names())

        assert "insight_engine_artifacts" in upgraded_tables
        assert "insight_channels" in upgraded_tables
        assert "insight_channel_catalog_state" in upgraded_tables
        assert "source_channel_tendencies" in upgraded_tables
        channel_column_map = {column["name"]: column for column in upgraded_inspector.get_columns("insight_channels")}
        assert "is_system" in channel_column_map
        assert "retired_at" in channel_column_map
        catalog_column_map = {
            column["name"]: column for column in upgraded_inspector.get_columns("insight_channel_catalog_state")
        }
        assert "channel_catalog_version" in catalog_column_map

        source_column_map = {column["name"]: column for column in upgraded_inspector.get_columns("sources")}
        assert "flow_policy" in source_column_map
        assert source_column_map["flow_policy"]["nullable"] is False

        with engine.connect() as connection:
            flow_policy = connection.scalar(
                text("SELECT flow_policy FROM sources WHERE url = 'https://example.com/feed.xml'")
            )

        assert flow_policy == "normal"

        tendency_unique_constraints = {
            constraint["name"]
            for constraint in upgraded_inspector.get_unique_constraints("source_channel_tendencies")
        }
        assert "uq_source_channel_tendencies_source_channel" in tendency_unique_constraints

        curated_fk_targets = {
            fk["referred_table"]
            for fk in upgraded_inspector.get_foreign_keys("saved_item_curated_insights")
            if fk["constrained_columns"] == ["base_insight_engine_artifact_id"]
        }
        event_fk_targets = {
            fk["referred_table"]
            for fk in upgraded_inspector.get_foreign_keys("saved_item_curated_insight_events")
            if fk["constrained_columns"] == ["base_insight_engine_artifact_id"]
        }
        assert curated_fk_targets == {"insight_engine_artifacts"}
        assert event_fk_targets == {"insight_engine_artifacts"}

        command.downgrade(config, "base")
        downgraded_tables = set(inspect(engine).get_table_names())
        assert "insight_engine_artifacts" not in downgraded_tables
    finally:
        engine.dispose()
        os.environ.pop("DATABASE_URL", None)
        reset_settings_cache()
        reset_session_state()


def _create_source(client, name: str = "Routing Source", url: str = "https://routing.example.com/feed.xml") -> int:
    response = client.post(
        "/api/sources",
        json={
            "name": name,
            "kind": "rss",
            "url": url,
            "is_active": True,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _full_routing_payload() -> dict:
    return {
        "schema_version": "feed_item_routing_v1",
        "primary_channel_id": "ai_agents_frontier",
        "secondary_channels": [{"channel_id": "open_source_tools_practice", "confidence": 0.4, "display_mode": "list"}],
        "reading_depth": "medium",
        "flow_lane": "main",
        "confidence": 0.8,
        "reason_codes": ["source_channel_tendency_match"],
        "promotion_reason_codes": [],
        "isolation_reason_codes": [],
        "source_policy_applied": "normal",
        "notes": "Valid routing",
    }
