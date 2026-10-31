from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sys
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.db.models import FeedItem
from app.db.session import get_session_factory
from app.services.feed_content_extraction import (
    CONTENT_EXTRACTION_STATUS_FAILED,
    CONTENT_EXTRACTION_STATUS_SKIPPED,
    CONTENT_EXTRACTION_SKIP_REASON_DEAD_LINK,
    create_http_client,
    extract_feed_item_content,
    is_dead_link_fetch_failure,
    mark_feed_item_extraction_dead_link,
)


@dataclass(frozen=True, slots=True)
class MaintenanceSelection:
    statuses: tuple[str, ...]
    hosts: tuple[str, ...]
    limit: int | None


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "retry":
        payload = retry_failed_extractions(
            selection=MaintenanceSelection(
                statuses=tuple(args.status or [CONTENT_EXTRACTION_STATUS_FAILED]),
                hosts=tuple(args.host),
                limit=args.limit,
            ),
            force_network=args.force_network,
            dry_run=args.dry_run,
            progress=not args.no_progress,
        )
        _write_payload(payload, json_output=args.json)
        return

    if args.command == "export":
        payload = build_extraction_report()
        _write_payload(payload, json_output=args.json)
        return

    if args.command == "probe-url":
        payload = probe_url(args.url)
        _write_payload(payload, json_output=args.json)
        return

    parser.error(f"unknown command: {args.command}")


def retry_failed_extractions(
    *,
    selection: MaintenanceSelection | None = None,
    force_network: bool = False,
    dry_run: bool = False,
    progress: bool = True,
) -> dict[str, object]:
    resolved_selection = selection or MaintenanceSelection(
        statuses=(CONTENT_EXTRACTION_STATUS_FAILED,),
        hosts=(),
        limit=None,
    )
    payload = _empty_report_payload()
    payload["dry_run"] = dry_run
    payload["force_network"] = force_network
    payload["selection"] = {
        "statuses": list(resolved_selection.statuses),
        "hosts": list(resolved_selection.hosts),
        "limit": resolved_selection.limit,
    }

    retry_result_counts: Counter[str] = Counter()
    retry_reason_counts: Counter[str] = Counter()
    not_attempted_reason_counts: Counter[str] = Counter()

    SessionFactory = get_session_factory()
    with SessionFactory() as session:
        items = _select_retry_items(session, resolved_selection)
        payload["retried_items"] = len(items)
        if progress:
            print(f"retrying_items={len(items)}", file=sys.stderr, flush=True)

        for index, item in enumerate(items, start=1):
            item_id = item.id
            before = item.content_extraction_status
            previous_error = item.content_extraction_error
            try:
                if not force_network and is_dead_link_fetch_failure(previous_error):
                    status = CONTENT_EXTRACTION_STATUS_SKIPPED
                    reason = CONTENT_EXTRACTION_SKIP_REASON_DEAD_LINK
                    attempted = False
                    if not dry_run:
                        mark_feed_item_extraction_dead_link(item, error_message=previous_error)
                else:
                    result = extract_feed_item_content(item, force=force_network, record_debug=True)
                    status = item.content_extraction_status or result.status
                    reason = _result_reason(item) or result.decision.reason
                    attempted = result.attempted

                retry_result_counts[status] += 1
                retry_reason_counts[reason] += 1
                if not attempted:
                    not_attempted_reason_counts[reason] += 1

                after = item.content_extraction_status
                if dry_run:
                    session.rollback()
                    after = before
                else:
                    session.commit()

                if before != after or dry_run:
                    payload["changed_items"].append(
                        {
                            "id": item.id,
                            "title": item.title,
                            "url": item.original_url,
                            "host": _host_of(item),
                            "before": before,
                            "after": status if dry_run else after,
                            "reason": reason,
                            "dry_run": dry_run,
                        }
                    )

                if progress and (index % 10 == 0 or index == len(items)):
                    print(f"processed={index}/{len(items)}", file=sys.stderr, flush=True)
            except Exception as exc:
                session.rollback()
                retry_result_counts["exception"] += 1
                payload["exceptions"].append({"id": item_id, "error": _short(exc, 600)})

        payload["retry_result_counts"] = dict(retry_result_counts)
        payload["retry_reason_counts"] = dict(retry_reason_counts)
        payload["not_attempted_reason_counts"] = dict(not_attempted_reason_counts)
        _extend_report_from_session(payload, session)

    return payload


def build_extraction_report() -> dict[str, object]:
    payload = _empty_report_payload()
    SessionFactory = get_session_factory()
    with SessionFactory() as session:
        _extend_report_from_session(payload, session)
    return payload


def probe_url(url: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "generated_at": _now_iso(),
        "url": url,
        "current_extractor_client": None,
        "browser_like_client": None,
    }

    with create_http_client() as client:
        payload["current_extractor_client"] = _probe_with_client(client, url)

    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
        },
    ) as client:
        payload["browser_like_client"] = _probe_with_client(client, url)

    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Feed content extraction maintenance tools.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    retry_parser = subparsers.add_parser("retry", help="Retry or repair failed feed content extraction rows.")
    retry_parser.add_argument(
        "--status",
        action="append",
        default=None,
        help="Content extraction status to select. Repeatable; defaults to failed.",
    )
    retry_parser.add_argument("--host", action="append", default=[], help="Limit to one URL host. Repeatable.")
    retry_parser.add_argument("--limit", type=int, default=None, help="Maximum rows to process after host filtering.")
    retry_parser.add_argument("--force-network", action="store_true", help="Ignore automatic cooldown decisions.")
    retry_parser.add_argument("--dry-run", action="store_true", help="Classify rows without committing changes.")
    retry_parser.add_argument("--json", action="store_true", help="Write the full JSON report to stdout.")
    retry_parser.add_argument("--no-progress", action="store_true", help="Suppress progress lines on stderr.")

    export_parser = subparsers.add_parser("export", help="Export current extraction status without changing rows.")
    export_parser.add_argument("--json", action="store_true", help="Write the full JSON report to stdout.")

    probe_parser = subparsers.add_parser("probe-url", help="Fetch one URL with extractor headers for diagnostics.")
    probe_parser.add_argument("url")
    probe_parser.add_argument("--json", action="store_true", help="Write the full JSON report to stdout.")

    return parser


def _select_retry_items(session, selection: MaintenanceSelection) -> list[FeedItem]:
    rows = session.execute(
        select(FeedItem)
        .where(FeedItem.content_extraction_status.in_(selection.statuses))
        .order_by(FeedItem.content_extraction_status, FeedItem.id)
    ).scalars()

    items = [item for item in rows if _host_matches(item, selection.hosts)]
    if selection.limit is not None:
        return items[: max(0, selection.limit)]
    return items


def _extend_report_from_session(payload: dict[str, object], session) -> None:
    statuses = session.execute(select(FeedItem.content_extraction_status)).all()
    payload["status_counts"] = dict(Counter((row[0] or "null") for row in statuses))

    remaining = session.execute(
        select(FeedItem)
        .where(
            FeedItem.content_extraction_status.in_(
                [CONTENT_EXTRACTION_STATUS_FAILED, CONTENT_EXTRACTION_STATUS_SKIPPED]
            )
        )
        .order_by(FeedItem.content_extraction_status, FeedItem.id)
    ).scalars()

    failed_errors: Counter[str] = Counter()
    host_counts: Counter[str] = Counter()

    for item in remaining:
        host = _host_of(item)
        terminal_reason = _result_reason(item)
        host_key = (
            f"{item.content_extraction_status}/{terminal_reason}: {host}"
            if terminal_reason
            else f"{item.content_extraction_status}: {host}"
        )
        host_counts[host_key] += 1

        if item.content_extraction_status == CONTENT_EXTRACTION_STATUS_FAILED:
            failed_errors[_short(item.content_extraction_error)] += 1

        payload["remaining_items"].append(
            {
                "id": item.id,
                "title": item.title,
                "url": item.original_url,
                "canonical_url": item.canonical_url,
                "host": host,
                "status": item.content_extraction_status,
                "text_source": item.content_text_source,
                "error": _short(item.content_extraction_error, 600),
                "reason": terminal_reason or _debug_value(item, "decision", "reason"),
                "decision_reason": _debug_value(item, "decision", "reason"),
                "terminal_reason": terminal_reason,
                "fetch_status_code": _debug_value(item, "fetch", "http_status"),
                "extractor": _debug_value(item, "extractor", "name"),
                "attempted_at": _iso(item.content_extraction_attempted_at),
                "extracted_at": _iso(item.content_extracted_at),
            }
        )

    payload["failed_error_counts"] = {key or "null": value for key, value in failed_errors.most_common()}
    payload["remaining_host_counts"] = dict(host_counts.most_common())


def _empty_report_payload() -> dict[str, object]:
    return {
        "generated_at": _now_iso(),
        "retried_items": 0,
        "dry_run": False,
        "force_network": False,
        "selection": {},
        "retry_result_counts": {},
        "retry_reason_counts": {},
        "not_attempted_reason_counts": {},
        "changed_items": [],
        "exceptions": [],
        "status_counts": {},
        "failed_error_counts": {},
        "remaining_host_counts": {},
        "remaining_items": [],
    }


def _write_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    summary = {
        "generated_at": payload.get("generated_at"),
        "retried_items": payload.get("retried_items"),
        "retry_result_counts": payload.get("retry_result_counts"),
        "retry_reason_counts": payload.get("retry_reason_counts"),
        "status_counts": payload.get("status_counts"),
        "failed_error_counts": payload.get("failed_error_counts"),
        "remaining_host_counts": payload.get("remaining_host_counts"),
        "exceptions": len(payload.get("exceptions", [])),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _probe_with_client(client: httpx.Client, url: str) -> dict[str, object]:
    try:
        response = client.get(url)
        return {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "final_url": str(response.url),
            "content_type": response.headers.get("content-type"),
            "content_length": len(response.content),
            "body_preview": response.text[:300],
        }
    except Exception as exc:
        return {"ok": False, "error": _short(exc, 600)}


def _result_reason(item: FeedItem) -> str | None:
    return _debug_value(item, "terminal", "reason")


def _debug_value(item: FeedItem, *keys: str) -> object | None:
    value: object = item.content_extraction_debug_json
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _host_matches(item: FeedItem, hosts: tuple[str, ...]) -> bool:
    if not hosts:
        return True
    item_host = _host_of(item)
    return any(item_host == host.lower() or item_host.endswith(f".{host.lower()}") for host in hosts)


def _host_of(item: FeedItem) -> str:
    url = item.canonical_url or item.original_url or ""
    return (urlparse(url).hostname or "").lower()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _short(value, limit: int = 300) -> str | None:
    if not value:
        return None
    return str(value).replace("\n", " ").strip()[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
