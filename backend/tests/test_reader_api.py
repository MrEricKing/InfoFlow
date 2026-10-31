from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db.models import (
    FeedItem,
    FeedItemBehaviorEvent,
    FeedItemStructuredFeedback,
    InsightChannel,
    ItemState,
    SavedItem,
    Source,
    SourceChannelTendency,
)
from app.db.session import get_session_factory
from app.services import feed_content_extraction
from app.services import operations as operations_service
from app.services import source_sync


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


class _OperationResultResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def sync_source_with_operation(client, source_id: int) -> _OperationResultResponse:
    response = client.post(
        "/api/operations/source-sync",
        json={"kind": "single", "source_id": source_id, "feed_item_insight_artifact_build_limit": 0},
    )
    assert response.status_code == 202
    operation_id = response.json()["id"]
    operations_service.run_operation_background(operation_id, max_drain_seconds=None)
    operation = client.get(f"/api/operations/{operation_id}").json()
    assert operation["status"] in {"completed", "failed", "stopped"}
    assert operation["result"] is not None
    return _OperationResultResponse(operation["result"])


def _homepage_items(response) -> list[dict]:
    return response.json()["items"]


def create_synced_single_item(client, monkeypatch) -> int:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    return client.get("/api/reader/items").json()[0]["id"]


def test_channel_corrections_live_on_structured_feedback_model() -> None:
    from app.db.base import Base

    table = Base.metadata.tables["feed_item_structured_feedback"]
    assert "primary_channel_correction" in table.c
    assert "channel_add_ids_json" in table.c
    assert "channel_remove_ids_json" in table.c
    assert "reading_depth_correction" in table.c
    assert "flow_lane_correction" in table.c
    assert "channel_correction_notes" in table.c
    removed_table_name = "feed_item_" + "channel_feedback"
    assert removed_table_name not in Base.metadata.tables


def test_structured_feedback_saves_channel_corrections(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "overall_value": "high",
                "primary_channel_correction": "ai_agents_frontier",
                "channel_add_ids": ["open_source_tools_practice"],
                "channel_remove_ids": ["cryptocurrency"],
                "reading_depth_correction": "deep",
                "flow_lane_correction": "needs_review",
                "channel_correction_notes": "agent engineering practice",
            }
        },
    )
    assert response.status_code == 200
    structured = response.json()["structured_feedback"]
    assert structured["primary_channel_correction"] == "ai_agents_frontier"
    assert structured["channel_add_ids"] == ["open_source_tools_practice"]
    assert structured["channel_remove_ids"] == ["cryptocurrency"]
    assert structured["reading_depth_correction"] == "deep"
    assert structured["flow_lane_correction"] == "needs_review"
    assert structured["channel_correction_notes"] == "agent engineering practice"
    assert "channel_feedback" not in response.json()


def test_structured_feedback_rejects_disabled_channel_for_new_correction(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    with get_session_factory()() as session:
        channel = session.get(InsightChannel, "cryptocurrency")
        assert channel is not None
        channel.enabled = False
        session.add(channel)
        session.commit()

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"primary_channel_correction": "cryptocurrency"}},
    )

    assert response.status_code in {400, 422}
    assert "Insight channel is not active or does not exist" in str(response.json()["detail"])


def test_structured_feedback_preserves_channel_correction_when_channel_fields_are_absent(
    client,
    monkeypatch,
) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    initial_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "primary_channel_correction": "ai_agents_frontier",
                "channel_add_ids": ["open_source_tools_practice"],
                "channel_remove_ids": ["cryptocurrency"],
                "reading_depth_correction": "deep",
                "flow_lane_correction": "needs_review",
                "channel_correction_notes": "agent engineering practice",
            }
        },
    )
    assert initial_response.status_code == 200

    update_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"overall_value": "high"}},
    )

    assert update_response.status_code == 200
    structured = update_response.json()["structured_feedback"]
    assert structured["overall_value"] == "high"
    assert structured["primary_channel_correction"] == "ai_agents_frontier"
    assert structured["channel_add_ids"] == ["open_source_tools_practice"]
    assert structured["channel_remove_ids"] == ["cryptocurrency"]
    assert structured["reading_depth_correction"] == "deep"
    assert structured["flow_lane_correction"] == "needs_review"
    assert structured["channel_correction_notes"] == "agent engineering practice"


def test_structured_feedback_can_clear_channel_correction_while_saving_regular_feedback(
    client,
    monkeypatch,
) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    initial_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "overall_value": "high",
                "primary_channel_correction": "ai_agents_frontier",
                "channel_add_ids": ["open_source_tools_practice"],
                "channel_remove_ids": ["cryptocurrency"],
                "reading_depth_correction": "deep",
                "flow_lane_correction": "needs_review",
                "channel_correction_notes": "agent engineering practice",
            }
        },
    )
    assert initial_response.status_code == 200

    clear_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "overall_value": "very_useful",
                "primary_channel_correction": None,
                "channel_add_ids": [],
                "channel_remove_ids": [],
                "reading_depth_correction": None,
                "flow_lane_correction": None,
                "channel_correction_notes": "",
            }
        },
    )

    assert clear_response.status_code == 200
    structured = clear_response.json()["structured_feedback"]
    assert structured["overall_value"] == "very_useful"
    assert structured["primary_channel_correction"] is None
    assert structured["channel_add_ids"] == []
    assert structured["channel_remove_ids"] == []
    assert structured["reading_depth_correction"] is None
    assert structured["flow_lane_correction"] is None
    assert structured["channel_correction_notes"] == ""


def test_structured_feedback_preserves_regular_feedback_when_saving_channel_correction_only(
    client,
    monkeypatch,
) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    regular_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "overall_value": "very_useful",
                "topic_add_ids": ["software_systems"],
                "reason_code_add_ids": ["implementation_detail"],
                "issue_markers": ["wrong_priority"],
            }
        },
    )
    assert regular_response.status_code == 200

    channel_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "primary_channel_correction": "ai_agents_frontier",
                "channel_add_ids": ["open_source_tools_practice"],
                "reading_depth_correction": "deep",
                "flow_lane_correction": "needs_review",
            }
        },
    )

    assert channel_response.status_code == 200
    structured = channel_response.json()["structured_feedback"]
    assert structured["overall_value"] == "very_useful"
    assert structured["topic_add_ids"] == ["software_systems"]
    assert structured["reason_code_add_ids"] == ["implementation_detail"]
    assert structured["issue_markers"] == ["wrong_priority"]
    assert structured["primary_channel_correction"] == "ai_agents_frontier"
    assert structured["channel_add_ids"] == ["open_source_tools_practice"]
    assert structured["reading_depth_correction"] == "deep"
    assert structured["flow_lane_correction"] == "needs_review"


def test_structured_feedback_preserves_regular_feedback_when_clearing_channel_correction_only(
    client,
    monkeypatch,
) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    initial_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "overall_value": "very_useful",
                "topic_add_ids": ["software_systems"],
                "primary_channel_correction": "ai_agents_frontier",
                "channel_add_ids": ["open_source_tools_practice"],
                "reading_depth_correction": "deep",
                "flow_lane_correction": "needs_review",
            }
        },
    )
    assert initial_response.status_code == 200

    clear_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "primary_channel_correction": None,
                "channel_add_ids": [],
                "channel_remove_ids": [],
                "reading_depth_correction": None,
                "flow_lane_correction": None,
                "channel_correction_notes": "",
            }
        },
    )

    assert clear_response.status_code == 200
    structured = clear_response.json()["structured_feedback"]
    assert structured["overall_value"] == "very_useful"
    assert structured["topic_add_ids"] == ["software_systems"]
    assert structured["primary_channel_correction"] is None
    assert structured["channel_add_ids"] == []
    assert structured["channel_remove_ids"] == []
    assert structured["reading_depth_correction"] is None
    assert structured["flow_lane_correction"] is None
    assert structured["channel_correction_notes"] == ""


def test_structured_feedback_rejects_unknown_channel(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"primary_channel_correction": "missing_channel"}},
    )

    assert response.status_code in {400, 422}
    assert "Insight channel is not active or does not exist" in str(response.json()["detail"])


def test_structured_feedback_rejects_blank_primary_channel_correction(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"primary_channel_correction": "   "}},
    )

    assert response.status_code in {400, 422}
    assert "primary_channel_correction must not be blank" in str(response.json()["detail"])
    assert client.get(f"/api/reader/items/{item_id}").json()["structured_feedback"] is None


def test_structured_feedback_rejects_duplicate_channel_ids(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "channel_add_ids": ["ai_agents_frontier", "ai_agents_frontier"],
                "channel_remove_ids": ["cryptocurrency"],
            }
        },
    )

    assert response.status_code in {400, 422}
    assert "duplicate channel ids" in str(response.json()["detail"])


def test_structured_feedback_rejects_overlapping_channel_ids(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "channel_add_ids": ["ai_agents_frontier"],
                "channel_remove_ids": ["ai_agents_frontier"],
            }
        },
    )

    assert response.status_code in {400, 422}
    assert "both added and removed" in str(response.json()["detail"])


def test_structured_feedback_rejects_empty_channel_arrays_only_payload(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"channel_add_ids": [], "channel_remove_ids": []}},
    )

    assert response.status_code in {400, 422}
    assert "at least one routing correction" in str(response.json()["detail"])
    assert client.get(f"/api/reader/items/{item_id}").json()["structured_feedback"] is None


def test_structured_feedback_rejects_invalid_channel_correction_enums(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    reading_depth_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"reading_depth_correction": "novel"}},
    )
    flow_lane_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"flow_lane_correction": "parking_lot"}},
    )

    assert reading_depth_response.status_code == 422
    assert flow_lane_response.status_code == 422


def test_structured_feedback_rejects_channel_notes_only_payload(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={"structured_feedback": {"channel_correction_notes": "notes alone should not create a correction"}},
    )

    assert response.status_code in {400, 422}
    assert "at least one routing correction" in str(response.json()["detail"])
    assert client.get(f"/api/reader/items/{item_id}").json()["structured_feedback"] is None


def test_combined_feedback_rolls_back_insight_feedback_when_structured_channel_feedback_is_invalid(
    client,
    monkeypatch,
) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "insight_feedback_text": "useful but wrong channel",
            "structured_feedback": {
                "primary_channel_correction": "missing_channel",
            },
        },
    )

    assert response.status_code in {400, 422}
    detail = client.get(f"/api/reader/items/{item_id}").json()
    assert detail["insight_feedback_text"] is None
    assert detail["structured_feedback"] is None
    assert [
        event["event_type"]
        for event in load_behavior_events(item_id)
        if event["event_type"] in {"insight_feedback_saved", "structured_feedback_saved"}
    ] == []


def test_structured_channel_correction_records_structured_feedback_events(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    save_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "primary_channel_correction": "ai_agents_frontier",
                "channel_remove_ids": ["cryptocurrency"],
                "reading_depth_correction": "deep",
                "channel_correction_notes": "explicit channel evidence",
            }
        },
    )
    assert save_response.status_code == 200

    clear_response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "structured_feedback": {
                "overall_value": "useful",
                "primary_channel_correction": None,
                "channel_add_ids": [],
                "channel_remove_ids": [],
                "reading_depth_correction": None,
                "flow_lane_correction": None,
            }
        },
    )
    assert clear_response.status_code == 200

    structured_events = [
        event
        for event in load_behavior_events(item_id)
        if event["event_type"] == "structured_feedback_saved"
    ]
    assert len(structured_events) == 2
    saved_payload = structured_events[0]["payload"]
    assert saved_payload["structured_feedback"]["primary_channel_correction"] == "ai_agents_frontier"
    assert saved_payload["structured_feedback"]["channel_remove_ids"] == ["cryptocurrency"]
    assert saved_payload["structured_feedback"]["reading_depth_correction"] == "deep"
    assert saved_payload["structured_feedback"]["channel_correction_notes"] == "explicit channel evidence"
    assert saved_payload["previous_structured_feedback_present"] is False
    cleared_payload = structured_events[1]["payload"]
    assert cleared_payload["structured_feedback"]["overall_value"] == "useful"
    assert cleared_payload["structured_feedback"]["primary_channel_correction"] is None
    assert cleared_payload["structured_feedback"]["channel_remove_ids"] == []
    assert "channel_feedback" not in saved_payload
    assert "previous_" + "channel_feedback" not in saved_payload


def test_noop_structured_feedback_save_does_not_record_duplicate_behavior_event(client, monkeypatch) -> None:
    item_id = create_synced_single_item(client, monkeypatch)
    payload = {
        "structured_feedback": {
            "overall_value": "useful",
            "primary_channel_correction": "ai_agents_frontier",
            "reading_depth_correction": "deep",
        }
    }

    first_response = client.put(f"/api/reader/items/{item_id}/feedback", json=payload)
    second_response = client.put(f"/api/reader/items/{item_id}/feedback", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    structured_events = [
        event
        for event in load_behavior_events(item_id)
        if event["event_type"] == "structured_feedback_saved"
    ]
    assert len(structured_events) == 1
    assert structured_events[0]["payload"]["changed"] is True


def test_standalone_channel_feedback_endpoint_is_removed(
    client,
    monkeypatch,
) -> None:
    item_id = create_synced_single_item(client, monkeypatch)

    removed_path = f"/api/reader/items/{item_id}/channel-" + "feedback"
    put_response = client.put(removed_path, json={})
    delete_response = client.delete(removed_path)

    assert put_response.status_code == 404
    assert delete_response.status_code == 404


def test_reader_list_and_detail_use_latest_occurrence_source_metadata(client, monkeypatch) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://beta.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_BETA),
        },
    )

    assert sync_source_with_operation(client, alpha_id).status_code == 200
    assert sync_source_with_operation(client, beta_id).status_code == 200

    list_response = client.get("/api/reader/items")
    assert list_response.status_code == 200
    items = list_response.json()
    assert len(items) == 1

    item = items[0]
    assert item["source_id"] == beta_id
    assert item["source_name"] == "Beta"
    assert item["original_url"] == "https://beta.example.com/posts/1"
    assert item["canonical_url"] == "https://alpha.example.com/posts/1?ref=alpha"
    assert item["state"]["status"] == "unread"

    detail_response = client.get(f"/api/reader/items/{item['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["source_id"] == beta_id
    assert detail["source_name"] == "Beta"
    assert detail["original_url"] == "https://beta.example.com/posts/1"
    assert detail["feed_content_text"] == "Alpha content"
    assert detail["content_text"] == "Alpha content"


def test_reader_insight_feedback_save_read_and_clear(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    list_before = client.get("/api/reader/items")
    assert list_before.status_code == 200
    assert list_before.json()[0]["insight_feedback_text"] is None
    assert list_before.json()[0]["structured_feedback"] is None

    structured_payload = {
        "overall_value": "very_useful",
        "priority_correction": "too_low",
        "topic_add_ids": ["software_systems"],
        "topic_remove_ids": ["tools_products"],
        "content_type_correction": "tutorial",
        "reason_code_add_ids": ["implementation_detail"],
        "reason_code_remove_ids": ["thin_content"],
        "issue_markers": ["wrong_topic", "thin_content"],
    }

    update_response = client.put(
        f"/api/reader/items/{item_id}/insight-feedback",
        json={
            "insight_feedback_text": "  Summary is too broad; classify as system design.  ",
            "structured_feedback": structured_payload,
        },
    )
    assert update_response.status_code == 200
    update_payload = update_response.json()
    assert update_payload["item_id"] == item_id
    assert update_payload["insight_feedback_text"] == "Summary is too broad; classify as system design."
    assert {key: update_payload["structured_feedback"][key] for key in structured_payload} == structured_payload

    list_after = client.get("/api/reader/items")
    assert list_after.status_code == 200
    assert list_after.json()[0]["insight_feedback_text"] == "Summary is too broad; classify as system design."
    assert list_after.json()[0]["structured_feedback"]["overall_value"] == "very_useful"

    detail_after = client.get(f"/api/reader/items/{item_id}")
    assert detail_after.status_code == 200
    assert detail_after.json()["insight_feedback_text"] == "Summary is too broad; classify as system design."
    assert detail_after.json()["structured_feedback"]["topic_add_ids"] == ["software_systems"]

    homepage_after = client.get("/api/homepage/items")
    assert homepage_after.status_code == 200
    assert _homepage_items(homepage_after)[0]["insight_feedback_text"] == "Summary is too broad; classify as system design."
    assert _homepage_items(homepage_after)[0]["structured_feedback"]["reason_code_add_ids"] == ["implementation_detail"]

    with get_session_factory()() as session:
        feed_item = session.get(FeedItem, item_id)
        assert feed_item is not None
        assert feed_item.insight_feedback_text == "Summary is too broad; classify as system design."
        structured_feedback = session.scalar(
            select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == item_id)
        )
        assert structured_feedback is not None
        assert structured_feedback.topic_add_ids_json == ["software_systems"]

    clear_response = client.put(
        f"/api/reader/items/{item_id}/insight-feedback",
        json={"insight_feedback_text": "   ", "structured_feedback": None},
    )
    assert clear_response.status_code == 200
    assert clear_response.json() == {"item_id": item_id, "insight_feedback_text": None, "structured_feedback": None}
    cleared_detail = client.get(f"/api/reader/items/{item_id}").json()
    assert cleared_detail["insight_feedback_text"] is None
    assert cleared_detail["structured_feedback"] is None
    with get_session_factory()() as session:
        assert (
            session.scalar(select(FeedItemStructuredFeedback).where(FeedItemStructuredFeedback.feed_item_id == item_id))
            is None
        )

    feedback_events = load_behavior_events(item_id)
    assert [event["event_type"] for event in feedback_events] == [
        "insight_feedback_saved",
        "structured_feedback_saved",
        "insight_feedback_cleared",
        "structured_feedback_cleared",
    ]
    assert feedback_events[0]["source"] == "reader_feedback_api"
    assert feedback_events[0]["payload"]["changed"] is True
    assert feedback_events[0]["payload"]["feedback_text"] == "Summary is too broad; classify as system design."
    assert feedback_events[1]["payload"]["changed"] is True
    assert feedback_events[1]["payload"]["structured_feedback"]["overall_value"] == "very_useful"
    assert feedback_events[2]["payload"]["changed"] is True
    assert feedback_events[2]["payload"]["feedback_text"] is None
    assert feedback_events[3]["payload"]["changed"] is True
    assert feedback_events[3]["payload"]["structured_feedback"] is None


def test_reader_clear_auto_low_priority_records_correction_and_preserves_not_interested_override(
    client,
    monkeypatch,
) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    settings_response = client.patch(
        "/api/settings/insight-engine",
        json={"auto_low_priority_archiving_enabled": True, "low_priority_score_threshold": 60},
    )
    assert settings_response.status_code == 200

    initial_card = client.get("/api/homepage/items").json()["items"][0]
    assert initial_card["id"] == item_id
    assert initial_card["priority_score"] < 60
    assert initial_card["low_priority_bucket"] is True
    assert initial_card["low_priority_bucket_reason"] == "auto_score_threshold"
    assert initial_card["auto_low_priority_cleared"] is False

    clear_response = client.post(f"/api/reader/items/{item_id}/clear_auto_low_priority")
    assert clear_response.status_code == 200
    clear_payload = clear_response.json()
    assert clear_payload["item_id"] == item_id
    assert clear_payload["structured_feedback"]["priority_correction"] == "too_low"
    assert "wrong_priority" in clear_payload["structured_feedback"]["issue_markers"]

    restored_card = client.get("/api/homepage/items").json()["items"][0]
    assert restored_card["low_priority_bucket"] is False
    assert restored_card["low_priority_bucket_reason"] is None
    assert restored_card["auto_low_priority_cleared"] is True

    behavior_events = load_behavior_events(item_id)
    clear_events = [event for event in behavior_events if event["event_type"] == "auto_low_priority_cleared"]
    assert len(clear_events) == 1
    assert clear_events[0]["source"] == "reader_priority_api"
    assert clear_events[0]["payload"]["priority_correction"] == "too_low"
    assert clear_events[0]["payload"]["issue_marker"] == "wrong_priority"

    assert client.post(f"/api/reader/items/{item_id}/mark_not_interested").status_code == 200
    not_interested_card = client.get("/api/homepage/items").json()["items"][0]
    assert not_interested_card["low_priority_bucket"] is True
    assert not_interested_card["low_priority_bucket_reason"] == "not_interested"
    assert not_interested_card["auto_low_priority_cleared"] is True


def test_reader_state_mutation_apis_are_idempotent_and_follow_projection_priority(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE),
            "https://single.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article><p>Original page snapshot</p></article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )
    sync_response = sync_source_with_operation(client, source_id)
    assert sync_response.status_code == 200

    item_id = client.get("/api/reader/items").json()[0]["id"]

    browsed = client.post(f"/api/reader/items/{item_id}/mark_browsed")
    assert browsed.status_code == 200
    browsed_state = browsed.json()["state"]
    assert browsed_state["status"] == "browsed"
    assert browsed_state["browsed_at"] is not None
    assert browsed_state["seen_at"] is None
    assert browsed_state["read_full_at"] is None

    seen = client.post(f"/api/reader/items/{item_id}/mark_seen")
    assert seen.status_code == 200
    seen_state = seen.json()["state"]
    assert seen_state["status"] == "seen"
    assert seen_state["browsed_at"] == browsed_state["browsed_at"]
    assert seen_state["seen_at"] is not None
    assert seen_state["read_full_at"] is None

    read_full = client.post(f"/api/reader/items/{item_id}/mark_read_full")
    assert read_full.status_code == 200
    read_full_state = read_full.json()["state"]
    assert read_full_state["status"] == "read_full"
    assert read_full_state["browsed_at"] == browsed_state["browsed_at"]
    assert read_full_state["seen_at"] == seen_state["seen_at"]
    assert read_full_state["read_full_at"] is not None

    unmark_read_full = client.post(f"/api/reader/items/{item_id}/unmark_read_full")
    assert unmark_read_full.status_code == 200
    unmark_read_full_state = unmark_read_full.json()["state"]
    assert unmark_read_full_state["status"] == "seen"
    assert unmark_read_full_state["browsed_at"] == browsed_state["browsed_at"]
    assert unmark_read_full_state["seen_at"] == seen_state["seen_at"]
    assert unmark_read_full_state["read_full_at"] is None

    unmark_seen = client.post(f"/api/reader/items/{item_id}/unmark_seen")
    assert unmark_seen.status_code == 200
    unmark_seen_state = unmark_seen.json()["state"]
    assert unmark_seen_state["status"] == "browsed"
    assert unmark_seen_state["browsed_at"] == browsed_state["browsed_at"]
    assert unmark_seen_state["seen_at"] is None
    assert unmark_seen_state["read_full_at"] is None

    repeated_unmark_seen = client.post(f"/api/reader/items/{item_id}/unmark_seen")
    assert repeated_unmark_seen.status_code == 200
    assert repeated_unmark_seen.json()["state"]["status"] == "browsed"

    repeated_unmark_read_full = client.post(f"/api/reader/items/{item_id}/unmark_read_full")
    assert repeated_unmark_read_full.status_code == 200
    assert repeated_unmark_read_full.json()["state"]["status"] == "browsed"

    read_full = client.post(f"/api/reader/items/{item_id}/mark_read_full")
    assert read_full.status_code == 200
    read_full_state = read_full.json()["state"]

    saved = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert saved.status_code == 200
    saved_payload = saved.json()
    saved_state = saved_payload["state"]
    assert saved_state["status"] == "saved"
    assert saved_state["saved_at"] is not None
    assert saved_state["read_full_at"] == read_full_state["read_full_at"]
    assert saved_payload["saved_item_id"] is not None
    assert saved_payload["has_canonical_snapshot"] is True
    assert saved_payload["has_original_page_snapshot"] is False
    assert saved_payload["has_singlefile_html"] is False
    assert saved_payload["has_crawl4ai_raw_markdown"] is False
    assert saved_payload["has_crawl4ai_fit_markdown"] is False
    assert saved_payload["has_crawl4ai_fit_image_reinserted_markdown"] is False
    assert saved_payload["image_asset_count"] == 0

    saved_list = client.get("/api/reader/items", params={"status": "saved"})
    assert saved_list.status_code == 200
    assert [item["id"] for item in saved_list.json()] == [item_id]

    repeated_browsed = client.post(f"/api/reader/items/{item_id}/mark_browsed")
    assert repeated_browsed.status_code == 200
    repeated_browsed_state = repeated_browsed.json()["state"]
    assert repeated_browsed_state["status"] == "saved"
    assert repeated_browsed_state["browsed_at"] == browsed_state["browsed_at"]

    not_interested = client.post(f"/api/reader/items/{item_id}/mark_not_interested")
    assert not_interested.status_code == 200
    not_interested_state = not_interested.json()["state"]
    assert not_interested_state["status"] == "not_interested"
    assert not_interested_state["saved_at"] == saved_state["saved_at"]
    assert not_interested_state["not_interested_at"] is not None

    saved_list_after = client.get("/api/reader/items", params={"status": "saved"})
    assert saved_list_after.status_code == 200
    assert saved_list_after.json() == []

    unmark_not_interested = client.post(f"/api/reader/items/{item_id}/unmark_not_interested")
    assert unmark_not_interested.status_code == 200
    unmark_not_interested_state = unmark_not_interested.json()["state"]
    assert unmark_not_interested_state["status"] == "saved"
    assert unmark_not_interested_state["saved_at"] == saved_state["saved_at"]
    assert unmark_not_interested_state["not_interested_at"] is None

    saved_list_restored = client.get("/api/reader/items", params={"status": "saved"})
    assert saved_list_restored.status_code == 200
    assert [item["id"] for item in saved_list_restored.json()] == [item_id]

    with get_session_factory()() as session:
        item_state = session.query(ItemState).filter(ItemState.feed_item_id == item_id).one()
        assert item_state.status == "saved"
        assert item_state.browsed_at is not None
        assert item_state.seen_at is None
        assert item_state.read_full_at is not None
        assert item_state.saved_at is not None
        assert item_state.not_interested_at is None

    behavior_events = load_behavior_events(item_id)
    event_types = [event["event_type"] for event in behavior_events]
    assert event_types.count("card_opened") == 2
    assert event_types.count("marked_seen_from_list") == 1
    assert event_types.count("seen_cleared") == 2
    assert event_types.count("read_full_in_app") == 2
    assert event_types.count("read_full_cleared") == 2
    assert "saved" in event_types
    assert "not_interested" in event_types
    assert "not_interested_cleared" in event_types
    card_events = [event for event in behavior_events if event["event_type"] == "card_opened"]
    assert card_events[0]["payload"]["changed"] is True
    assert card_events[1]["payload"]["changed"] is False
    clear_events = [event for event in behavior_events if event["event_type"] == "read_full_cleared"]
    assert clear_events[0]["payload"]["changed"] is True
    assert clear_events[0]["payload"]["status"] == "seen"
    assert clear_events[1]["payload"]["changed"] is False
    seen_clear_events = [event for event in behavior_events if event["event_type"] == "seen_cleared"]
    assert seen_clear_events[0]["payload"]["changed"] is True
    assert seen_clear_events[0]["payload"]["previous_status"] == "seen"
    assert seen_clear_events[0]["payload"]["status"] == "browsed"
    assert seen_clear_events[1]["payload"]["changed"] is False
    undo_events = [event for event in behavior_events if event["event_type"] == "not_interested_cleared"]
    assert undo_events[0]["payload"]["changed"] is True
    assert undo_events[0]["payload"]["previous_status"] == "not_interested"
    assert undo_events[0]["payload"]["status"] == "saved"


def test_reader_unmark_saved_soft_removes_saved_item_and_reprojects_state(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE),
            "https://single.example.com/posts/1": httpx.Response(
                200,
                text="<html><body><article><p>Original page snapshot</p></article></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        },
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    assert client.post(f"/api/reader/items/{item_id}/mark_read_full").status_code == 200
    saved_response = client.post(f"/api/reader/items/{item_id}/mark_saved")
    assert saved_response.status_code == 200
    saved_item_id = saved_response.json()["saved_item_id"]

    unmark_saved = client.post(f"/api/reader/items/{item_id}/unmark_saved")
    assert unmark_saved.status_code == 200
    payload = unmark_saved.json()
    assert payload["saved_item_id"] == saved_item_id
    assert payload["state"]["status"] == "read_full"
    assert payload["state"]["saved_at"] is None
    assert payload["state"]["read_full_at"] is not None

    repeated_unmark_saved = client.post(f"/api/reader/items/{item_id}/unmark_saved")
    assert repeated_unmark_saved.status_code == 200
    assert repeated_unmark_saved.json()["state"]["status"] == "read_full"

    assert client.get("/api/knowledge/saved-items").json() == []

    with get_session_factory()() as session:
        saved_item = session.get(SavedItem, saved_item_id)
        assert saved_item is not None
        assert saved_item.removed_at is not None
        item_state = session.query(ItemState).filter(ItemState.feed_item_id == item_id).one()
    assert item_state.status == "read_full"
    assert item_state.saved_at is None

    save_events = [
        event
        for event in load_behavior_events(item_id)
        if event["event_type"] in {"saved", "unsaved"}
    ]
    assert [event["event_type"] for event in save_events] == ["saved", "unsaved", "unsaved"]
    assert save_events[0]["source"] == "knowledge_save"
    assert save_events[0]["payload"]["changed"] is True
    assert save_events[1]["source"] == "knowledge_unsave"
    assert save_events[1]["payload"]["changed"] is True
    assert save_events[1]["payload"]["first_removal"] is True
    assert save_events[2]["payload"]["changed"] is False
    assert save_events[2]["payload"]["first_removal"] is False


def test_reader_open_original_marks_read_full_and_records_extraction_diagnostics(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Single", "https://single.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://single.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    with get_session_factory()() as session:
        feed_item = session.get(FeedItem, item_id)
        assert feed_item is not None
        feed_item.content_text_source = "feed_summary"
        feed_item.content_extraction_status = "failed"
        feed_item.content_extraction_error = "fixture extraction failed"
        feed_item.content_extraction_debug_json = {
            "extractor": {
                "name": "fixture-extractor",
                "url": "https://single.example.com/posts/1",
                "extracted_visible_char_count": 42,
            }
        }
        session.add(feed_item)
        session.commit()

    response = client.post(f"/api/reader/items/{item_id}/open_original")

    assert response.status_code == 200
    state = response.json()["state"]
    assert state["status"] == "read_full"
    assert state["read_full_at"] is not None

    repeated_response = client.post(f"/api/reader/items/{item_id}/open_original")
    assert repeated_response.status_code == 200
    assert repeated_response.json()["state"]["status"] == "read_full"
    assert repeated_response.json()["state"]["read_full_at"] == state["read_full_at"]

    open_events = [
        event
        for event in load_behavior_events(item_id)
        if event["event_type"] == "opened_original_link"
    ]
    assert len(open_events) == 2
    assert open_events[0]["source"] == "reader_state_api"
    assert open_events[0]["payload"]["changed"] is True
    assert open_events[0]["payload"]["read_full_at_set"] is True
    assert open_events[0]["payload"]["previous_status"] == "unread"
    assert open_events[0]["payload"]["status"] == "read_full"
    assert open_events[0]["payload"]["original_url"] == "https://single.example.com/posts/1"
    assert open_events[0]["payload"]["content_text_source"] == "feed_summary"
    assert open_events[0]["payload"]["content_extraction_status"] == "failed"
    assert open_events[0]["payload"]["content_extraction_error"] == "fixture extraction failed"
    assert open_events[0]["payload"]["content_extraction_visible_char_count"] == 42
    assert open_events[1]["payload"]["changed"] is False
    assert open_events[1]["payload"]["read_full_at_set"] is False


def test_reader_direct_manual_extract_endpoint_is_removed(client, monkeypatch) -> None:
    source_id = create_source_for_sync(client, "Manual", "https://manual.example.com/feed.xml")
    install_transport(monkeypatch, {"https://manual.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)})
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    response = client.post(f"/api/reader/items/{item_id}/extract-content")

    assert response.status_code == 404


def test_reader_bulk_mark_read_full_endpoint_is_removed(client) -> None:
    response = client.post("/api/reader/items/bulk/mark_read_full", json={"source_id": 1})

    assert response.status_code == 422


def test_reader_bulk_mark_seen_uses_latest_occurrence_source(client, monkeypatch) -> None:
    alpha_id = create_source_for_sync(client, "Alpha", "https://alpha.example.com/feed.xml")
    beta_id = create_source_for_sync(client, "Beta", "https://beta.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {
            "https://alpha.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_ALPHA),
            "https://beta.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_BETA),
        },
    )

    assert sync_source_with_operation(client, alpha_id).status_code == 200
    assert sync_source_with_operation(client, beta_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]

    alpha_bulk = client.post("/api/reader/items/bulk/mark_seen", json={"source_id": alpha_id})
    assert alpha_bulk.status_code == 200
    assert alpha_bulk.json() == {
        "source_id": alpha_id,
        "matched_item_count": 0,
        "updated_item_count": 0,
    }

    beta_bulk = client.post("/api/reader/items/bulk/mark_seen", json={"source_id": beta_id})
    assert beta_bulk.status_code == 200
    assert beta_bulk.json() == {
        "source_id": beta_id,
        "matched_item_count": 1,
        "updated_item_count": 1,
    }

    item_after_bulk = client.get(f"/api/reader/items/{item_id}").json()
    assert item_after_bulk["state"]["status"] == "seen"
    assert item_after_bulk["state"]["seen_at"] is not None
    assert item_after_bulk["state"]["read_full_at"] is None

    repeated_bulk = client.post("/api/reader/items/bulk/mark_seen", json={"source_id": beta_id})
    assert repeated_bulk.status_code == 200
    assert repeated_bulk.json()["matched_item_count"] == 1
    assert repeated_bulk.json()["updated_item_count"] == 0

    bulk_events = [
        event
        for event in load_behavior_events(item_id)
        if event["event_type"] == "marked_seen_bulk"
    ]
    assert len(bulk_events) == 2
    assert bulk_events[0]["source"] == "reader_bulk_api"
    assert bulk_events[0]["payload"]["source_id"] == beta_id
    assert bulk_events[0]["payload"]["changed"] is True
    assert bulk_events[0]["payload"]["previous_status"] == "unread"
    assert bulk_events[0]["payload"]["status"] == "seen"
    assert bulk_events[1]["payload"]["source_id"] == beta_id
    assert bulk_events[1]["payload"]["changed"] is False

    missing_source = client.post("/api/reader/items/bulk/mark_seen", json={"source_id": 999})
    assert missing_source.status_code == 404


def test_bulk_seen_event_records_routing_context(client, monkeypatch) -> None:
    source_id, item_id = create_routed_single_item(client, monkeypatch)
    view_context = {
        "surface": "ai_homepage",
        "scope_type": "source",
        "source_id": source_id,
    }

    response = client.post(
        "/api/reader/items/bulk/mark_seen",
        json={"source_id": source_id, "view_context": view_context},
    )

    assert response.status_code == 200
    event = latest_behavior_event(item_id, "marked_seen_bulk")
    assert event["payload"]["view_context"]["surface"] == "ai_homepage"
    assert event["payload"]["view_context"]["scope_type"] == "source"
    assert event["payload"]["view_context"]["source_id"] == source_id
    assert event["payload"]["routing_context"]["flow_lane"] == "brief_pool"
    assert event["payload"]["routing_context"]["reading_depth"] == "light"
    assert event["payload"]["routing_context"]["source_flow_policy"] == "high_frequency_brief"
    assert event["payload"]["routing_context"]["channel_context"]["primary_channel_id"] == "cryptocurrency"


def test_reader_state_events_record_view_and_routing_context(client, monkeypatch) -> None:
    source_id, item_id = create_routed_single_item(client, monkeypatch)
    view_context = {
        "surface": "ai_homepage",
        "scope_type": "source",
        "source_id": source_id,
    }

    assert client.post(f"/api/reader/items/{item_id}/open_original", json={"view_context": view_context}).status_code == 200
    assert client.post(f"/api/reader/items/{item_id}/mark_saved", json={"view_context": view_context}).status_code == 200
    assert (
        client.post(f"/api/reader/items/{item_id}/mark_not_interested", json={"view_context": view_context}).status_code
        == 200
    )

    for event_type in ["opened_original_link", "saved", "not_interested"]:
        event = latest_behavior_event(item_id, event_type)
        assert event["payload"]["view_context"]["surface"] == "ai_homepage"
        assert event["payload"]["view_context"]["scope_type"] == "source"
        assert event["payload"]["view_context"]["source_id"] == source_id
        assert event["payload"]["routing_context"]["source_id"] == source_id
        assert event["payload"]["routing_context"]["flow_lane"] == "brief_pool"
        assert event["payload"]["routing_context"]["channel_context"]["primary_channel_id"] == "cryptocurrency"


def test_feedback_events_record_prior_and_corrected_routing_context(client, monkeypatch) -> None:
    source_id, item_id = create_routed_single_item(client, monkeypatch)
    view_context = {
        "surface": "ai_homepage",
        "scope_type": "source",
        "source_id": source_id,
    }

    response = client.put(
        f"/api/reader/items/{item_id}/feedback",
        json={
            "view_context": view_context,
            "structured_feedback": {
                "primary_channel_correction": "open_source_tools_practice",
                "reading_depth_correction": "medium",
                "flow_lane_correction": "needs_review",
            },
        },
    )

    assert response.status_code == 200
    structured_event = latest_behavior_event(item_id, "structured_feedback_saved")
    assert structured_event["payload"]["view_context"]["surface"] == "ai_homepage"
    assert structured_event["payload"]["view_context"]["scope_type"] == "source"
    assert structured_event["payload"]["view_context"]["source_id"] == source_id
    assert structured_event["payload"]["prior_routing"]["channel_context"]["primary_channel_id"] == "cryptocurrency"
    assert structured_event["payload"]["corrected_routing"]["channel_context"]["primary_channel_id"] == (
        "open_source_tools_practice"
    )
    assert structured_event["payload"]["corrected_routing"]["flow_lane"] == "needs_review"

    assert structured_event["payload"]["routing_context"]["routing_source"] == "user_corrected"


def test_reader_item_endpoints_return_404_for_unknown_item(client) -> None:
    detail_response = client.get("/api/reader/items/999")
    assert detail_response.status_code == 404

    mutation_response = client.post("/api/reader/items/999/mark_saved")
    assert mutation_response.status_code == 404

    mark_seen_response = client.post("/api/reader/items/999/mark_seen")
    assert mark_seen_response.status_code == 404

    unmark_seen_response = client.post("/api/reader/items/999/unmark_seen")
    assert unmark_seen_response.status_code == 404

    unmark_read_response = client.post("/api/reader/items/999/unmark_read_full")
    assert unmark_read_response.status_code == 404

    open_original_response = client.post("/api/reader/items/999/open_original")
    assert open_original_response.status_code == 404

    unmark_saved_response = client.post("/api/reader/items/999/unmark_saved")
    assert unmark_saved_response.status_code == 404

    unmark_not_interested_response = client.post("/api/reader/items/999/unmark_not_interested")
    assert unmark_not_interested_response.status_code == 404

    extract_response = client.post("/api/reader/items/999/extract-content")
    assert extract_response.status_code == 404

    feedback_response = client.put("/api/reader/items/999/insight-feedback", json={"insight_feedback_text": "test"})
    assert feedback_response.status_code == 404


def create_source_for_sync(client, name: str, url: str) -> int:
    response = client.post(
        "/api/sources",
        json={
            "name": name,
            "kind": "rss",
            "url": url,
            "site_url": url.replace("/feed.xml", ""),
            "folder_path": None,
            "is_active": True,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


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


def create_routed_single_item(client, monkeypatch) -> tuple[int, int]:
    assert client.get("/api/settings/insight-channels").status_code == 200
    source_id = create_source_for_sync(client, "Routed", "https://routed.example.com/feed.xml")
    install_transport(
        monkeypatch,
        {"https://routed.example.com/feed.xml": httpx.Response(200, text=RSS_SAMPLE_SINGLE)},
    )
    assert sync_source_with_operation(client, source_id).status_code == 200
    item_id = client.get("/api/reader/items").json()[0]["id"]
    with get_session_factory()() as session:
        source = session.get(Source, source_id)
        assert source is not None
        source.flow_policy = "high_frequency_brief"
        session.add(
            SourceChannelTendency(
                source_id=source_id,
                channel_id="cryptocurrency",
                tendency=0.9,
                is_primary=True,
                notes="test routing context",
            )
        )
        session.commit()
    return source_id, item_id


def latest_behavior_event(feed_item_id: int, event_type: str) -> dict:
    events = [event for event in load_behavior_events(feed_item_id) if event["event_type"] == event_type]
    assert events
    return events[-1]


def load_behavior_events(feed_item_id: int) -> list[dict]:
    with get_session_factory()() as session:
        events = session.scalars(
            select(FeedItemBehaviorEvent)
            .where(FeedItemBehaviorEvent.feed_item_id == feed_item_id)
            .order_by(FeedItemBehaviorEvent.created_at, FeedItemBehaviorEvent.id)
        ).all()
        return [
            {
                "event_type": event.event_type,
                "payload": event.event_payload_json,
                "source": event.source,
            }
            for event in events
        ]
