from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.cli import feed_content_extraction_maintenance
from app.db.models import FeedItem
from app.db.session import get_session_factory


def test_retry_repairs_existing_404_failure_without_network(client: TestClient) -> None:
    del client
    with get_session_factory()() as session:
        item = FeedItem(
            original_url="https://example.com/missing",
            canonical_url="https://example.com/missing",
            title="Missing article",
            author_name=None,
            published_at=None,
            summary_text="Read more https://example.com/missing",
            feed_content_text=None,
            content_text="Read more https://example.com/missing",
            content_text_source="feed_summary",
            content_extraction_status="failed",
            content_extraction_error="fetch_failed: HTTP 404",
            topic_hints=None,
        )
        session.add(item)
        session.commit()
        item_id = item.id

    payload = feed_content_extraction_maintenance.retry_failed_extractions(progress=False)

    assert payload["retry_result_counts"] == {"skipped": 1}
    assert payload["retry_reason_counts"] == {"dead_link": 1}
    assert payload["not_attempted_reason_counts"] == {"dead_link": 1}
    assert payload["status_counts"]["skipped"] == 1
    assert payload["remaining_host_counts"] == {"skipped/dead_link: example.com": 1}

    with get_session_factory()() as session:
        repaired = session.get(FeedItem, item_id)
        assert repaired is not None
        assert repaired.content_extraction_status == "skipped"
        assert repaired.content_extraction_error is None
        assert repaired.content_extraction_debug_json["terminal"]["reason"] == "dead_link"


def test_export_json_command_writes_full_report(client: TestClient, capsys) -> None:
    del client
    feed_content_extraction_maintenance.main(["export", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["status_counts"] == {}
    assert payload["remaining_items"] == []
