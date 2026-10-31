from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from app.db.models import (
    FeedItem,
    FeedItemStructuredFeedback,
    InsightChannel,
    Source,
    SourceChannelTendency,
    SourceItemOccurrence,
)
from app.db.session import get_session_factory
from app.services.app_settings import AI_DEEPSEEK_THINKING_DISABLED, AIProviderConfig
from app.services.insight_engine_request_builder import (
    INSIGHT_ENGINE_INPUT_HEAD_CHARS,
    INSIGHT_ENGINE_INPUT_TAIL_CHARS,
    MAX_INSIGHT_ENGINE_INPUT_CHARS,
    InsightEngineRequestPayload,
    build_insight_engine_request_payload,
    load_insight_engine_request_context,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "insight_engine_request_payloads"


def test_request_builder_output_matches_normal_item_snapshot(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        feed_item, source = _create_feed_item(
            session,
            title="Normal Snapshot Story",
            source_name="Normal Snapshot Feed",
            content_text="Normal request builder body with practical implementation detail.",
        )
        context = load_insight_engine_request_context(
            session=session,
            config=_provider_config(),
            feed_item=feed_item,
            source=source,
        )
        payload = build_insight_engine_request_payload(context)

    _assert_payload_matches_snapshot("normal_item.json", payload)


def test_request_builder_output_matches_source_channel_context_snapshot(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        feed_item, source = _create_feed_item(
            session,
            title="Source Context Snapshot Story",
            source_name="Source Context Feed",
            source_folder="AI / Agents",
            content_text="Source context body about agent tooling and implementation workflows.",
            flow_policy="high_frequency_brief",
        )
        session.add(
            SourceChannelTendency(
                source_id=source.id,
                channel_id="ai_agents_frontier",
                tendency=0.91,
                is_primary=True,
                notes="source routinely covers agent frontier work",
            )
        )
        session.flush()
        context = load_insight_engine_request_context(
            session=session,
            config=_provider_config(),
            feed_item=feed_item,
            source=source,
        )
        payload = build_insight_engine_request_payload(context)

    _assert_payload_matches_snapshot("source_channel_context.json", payload)


def test_request_builder_output_matches_human_evidence_snapshot(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200

    with get_session_factory()() as session:
        feed_item, source = _create_feed_item(
            session,
            title="Human Evidence Snapshot Story",
            source_name="Human Evidence Feed",
            content_text="Human correction body about an open-source agent tool.",
        )
        session.add(
            FeedItemStructuredFeedback(
                feed_item_id=feed_item.id,
                primary_channel_correction="ai_agents_frontier",
                channel_add_ids_json=["open_source_tools_practice"],
                channel_remove_ids_json=["cryptocurrency"],
                reading_depth_correction="deep",
                flow_lane_correction="needs_review",
                channel_correction_notes="agent engineering practice",
            )
        )
        session.flush()
        context = load_insight_engine_request_context(
            session=session,
            config=_provider_config(),
            feed_item=feed_item,
            source=source,
        )
        payload = build_insight_engine_request_payload(context)

    _assert_payload_matches_snapshot("human_evidence_context.json", payload)


def test_request_builder_is_pure_for_loaded_context(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    with get_session_factory()() as session:
        feed_item, source = _create_feed_item(
            session,
            title="Pure Builder Snapshot Story",
            source_name="Pure Builder Feed",
            content_text="Original loaded context body.",
        )
        context = load_insight_engine_request_context(
            session=session,
            config=_provider_config(),
            feed_item=feed_item,
            source=source,
        )
        first_payload = build_insight_engine_request_payload(context).model_dump(mode="json")

        feed_item.content_text = "Mutated database body that should not affect an already loaded context."
        source.folder_path = "Mutated / Folder"
        session.add_all([feed_item, source])
        session.flush()

        second_payload = build_insight_engine_request_payload(context).model_dump(mode="json")

    assert second_payload == first_payload
    assert "Original loaded context body." in second_payload["user_message"]
    assert "Mutated database body" not in second_payload["user_message"]
    assert second_payload["source_folder"] == ""


def test_request_builder_middle_truncates_oversized_content(client) -> None:
    assert client.get("/api/settings/insight-channels").status_code == 200
    omitted_middle = "M"
    oversized_content = (
        "A" * INSIGHT_ENGINE_INPUT_HEAD_CHARS
        + omitted_middle
        + "Z" * INSIGHT_ENGINE_INPUT_TAIL_CHARS
    )
    with get_session_factory()() as session:
        feed_item, source = _create_feed_item(
            session,
            title="Oversized Snapshot Story",
            source_name="Oversized Snapshot Feed",
            content_text=oversized_content,
        )
        context = load_insight_engine_request_context(
            session=session,
            config=_provider_config(),
            feed_item=feed_item,
            source=source,
        )
        payload = build_insight_engine_request_payload(context)

    assert payload.content_text == (
        "A" * INSIGHT_ENGINE_INPUT_HEAD_CHARS
        + "Z" * INSIGHT_ENGINE_INPUT_TAIL_CHARS
    )
    assert omitted_middle not in payload.content_text
    assert len(payload.content_text) == MAX_INSIGHT_ENGINE_INPUT_CHARS
    assert payload.stats.source_content_char_count == MAX_INSIGHT_ENGINE_INPUT_CHARS + len(omitted_middle)
    assert payload.stats.content_text_char_count == MAX_INSIGHT_ENGINE_INPUT_CHARS
    assert payload.stats.content_text_was_truncated is True


def test_request_builder_fails_when_channel_catalog_is_missing(client) -> None:
    with get_session_factory()() as session:
        feed_item, source = _create_feed_item(
            session,
            title="Missing Catalog Story",
            source_name="Missing Catalog Feed",
            content_text="Catalog-free request should fail clearly.",
        )
        try:
            load_insight_engine_request_context(
                session=session,
                config=_provider_config(),
                feed_item=feed_item,
                source=source,
            )
        except ValueError as exc:
            assert "Insight channel catalog is empty" in str(exc)
        else:
            raise AssertionError("Missing channel catalog should fail")


def test_request_builder_fails_when_needs_review_channel_is_missing(client) -> None:
    with get_session_factory()() as session:
        session.add(
            InsightChannel(
                id="custom_channel",
                label="Custom Channel",
                description="Only custom channel.",
                reading_depth="medium",
                sort_order=1,
                enabled=True,
            )
        )
        feed_item, source = _create_feed_item(
            session,
            title="Missing Needs Review Story",
            source_name="Missing Needs Review Feed",
            content_text="Catalog without needs_review should fail clearly.",
        )
        try:
            load_insight_engine_request_context(
                session=session,
                config=_provider_config(),
                feed_item=feed_item,
                source=source,
            )
        except ValueError as exc:
            assert "missing active system channel needs_review" in str(exc)
        else:
            raise AssertionError("Catalog without needs_review should fail")


def _create_feed_item(
    session,
    *,
    title: str,
    source_name: str,
    source_folder: str | None = None,
    content_text: str,
    flow_policy: str = "normal",
) -> tuple[FeedItem, Source]:
    timestamp = datetime(2026, 5, 14, 8, 0, tzinfo=timezone.utc)
    source_slug = source_name.lower().replace(" ", "-")
    item_slug = title.lower().replace(" ", "-")
    source = Source(
        name=source_name,
        kind="rss",
        url=f"https://{source_slug}.example.com/feed.xml",
        site_url=f"https://{source_slug}.example.com",
        folder_path=source_folder,
        is_active=True,
        ai_route="general",
        flow_policy=flow_policy,
        provenance="manual",
    )
    feed_item = FeedItem(
        original_url=f"https://{source_slug}.example.com/posts/{item_slug}",
        canonical_url=f"https://{source_slug}.example.com/posts/{item_slug}",
        title=title,
        published_at=timestamp,
        summary_text="Summary fallback that should stay out when content_text exists.",
        feed_content_text="Summary fallback that should stay out when content_text exists.",
        content_text=content_text,
        content_text_source="feed_content",
        content_extraction_status="not_checked",
    )
    session.add_all([source, feed_item])
    session.flush()
    session.add(
        SourceItemOccurrence(
            source_id=source.id,
            feed_item_id=feed_item.id,
            external_id=feed_item.canonical_url,
            original_url=feed_item.original_url,
            canonical_url=feed_item.canonical_url,
            published_at=timestamp,
            raw_entry_json={"title": title},
            match_rule="created",
            match_value=feed_item.canonical_url,
        )
    )
    session.flush()
    return feed_item, source


def _provider_config() -> AIProviderConfig:
    return AIProviderConfig(
        profile="general",
        provider="openai-compatible",
        enabled=True,
        base_url="https://ai.example.com",
        model="snapshot-model",
        api_key="sk-snapshot-secret",
        deepseek_thinking=AI_DEEPSEEK_THINKING_DISABLED,
    )


def _assert_payload_matches_snapshot(filename: str, payload: InsightEngineRequestPayload) -> None:
    actual = _snapshot_payload(payload)
    expected = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
    assert actual == expected
    assert "sk-snapshot-secret" not in json.dumps(actual, ensure_ascii=False)


def _snapshot_payload(payload: InsightEngineRequestPayload) -> dict:
    data = payload.model_dump(mode="json")
    return {
        "prompt_version": data["prompt_version"],
        "profile": data["profile"],
        "provider": data["provider"],
        "model": data["model"],
        "endpoint": data["endpoint"],
        "temperature": data["temperature"],
        "max_tokens": data["max_tokens"],
        "system_message_sha256": _stable_hash(data["system_message"]),
        "user_message_sha256": _stable_hash(data["user_message"]),
        "request_body_sha256": _stable_hash(data["request_body"]),
        "fallback_request_body_sha256": _stable_hash(data["fallback_request_body"]),
        "active_insight_profile_context_sha256": _stable_hash(data["active_insight_profile_context"]),
        "profile_version": data["profile_version"],
        "source_preference_profile": data["source_preference_profile"],
        "source_profile_version": data["source_profile_version"],
        "source_profile_is_empty": data["source_profile_is_empty"],
        "feature_schema_version": data["feature_schema_version"],
        "score_version": data["score_version"],
        "channels": data["channels"],
        "channel_catalog_version": data["channel_catalog_version"],
        "source_channel_tendencies": data["source_channel_tendencies"],
        "source_flow_policy": data["source_flow_policy"],
        "human_evidence": data["human_evidence"],
        "content_extraction_status": data["content_extraction_status"],
        "source_folder": data["source_folder"],
        "content_text": data["content_text"],
        "content_text_source": data["content_text_source"],
        "stats": data["stats"],
        "excluded_fields": data["excluded_fields"],
    }


def _stable_hash(value) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
