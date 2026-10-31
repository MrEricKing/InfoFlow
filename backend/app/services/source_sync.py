from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import ParseResult, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.safety import (
    FEED_ENTRY_MAX_COUNT,
    FEED_RESPONSE_MAX_BYTES,
    URLSafetyError,
    XMLSafetyError,
    parse_xml_document,
    safe_http_get,
)
from app.db.models import FeedItem, ItemState, Source, SourceFetch, SourceItemOccurrence
from app.schemas.source import SingleSourceSyncResponse, FeedItemInsightArtifactBuildStats
from app.services.feed_content_extraction import (
    CONTENT_TEXT_SOURCE_FEED_CONTENT,
    CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    extract_feed_item_content,
)
from app.services.source_registry import normalize_url
from app.services.source_resolver import SourceValidationError, resolve_source_fetch_url
from app.services.source_icons import (
    ResolvedSourceIcon,
    apply_source_icon,
    resolve_feed_icon,
    resolve_source_site_icon,
)

ATOM_NAMESPACE = {"atom": "http://www.w3.org/2005/Atom"}
GLOBAL_EXTERNAL_ID_PREFIXES = ("http://", "https://", "urn:")
SOURCE_SYNC_ITEM_LIMIT = 50


class SourceSyncError(RuntimeError):
    pass


@dataclass(slots=True)
class NormalizedFeedEntry:
    external_id: str | None
    external_id_is_global: bool
    original_url: str
    canonical_url: str | None
    title: str
    author_name: str | None
    published_at: datetime | None
    summary_text: str | None
    feed_content_text: str | None
    content_text: str | None
    content_text_source: str
    raw_entry_json: dict[str, Any]


@dataclass(slots=True)
class NormalizedFeedDocument:
    title: str | None
    site_url: str | None
    description: str | None
    language: str | None
    icon_url: str | None
    icon_source: str | None
    entries: list[NormalizedFeedEntry]


def create_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={"Accept-Encoding": "identity"},
    )


def list_recent_fetches(session: Session, source_id: int, limit: int) -> list[SourceFetch]:
    statement = (
        select(SourceFetch)
        .where(SourceFetch.source_id == source_id)
        .order_by(SourceFetch.fetch_started_at.desc(), SourceFetch.id.desc())
        .limit(limit)
    )
    return list(session.scalars(statement))


def list_recent_fetches_across_sources(session: Session, limit: int) -> list[tuple[SourceFetch, str]]:
    statement = (
        select(SourceFetch, Source.name)
        .join(Source, Source.id == SourceFetch.source_id)
        .order_by(SourceFetch.fetch_started_at.desc(), SourceFetch.id.desc())
        .limit(limit)
    )
    return [(fetch, source_name) for fetch, source_name in session.execute(statement)]


def list_latest_fetches_by_source_ids(session: Session, source_ids: Iterable[int]) -> dict[int, SourceFetch]:
    source_id_values = list(source_ids)
    if not source_id_values:
        return {}

    ranked_fetch_ids = (
        select(
            SourceFetch.id.label("fetch_id"),
            func.row_number()
            .over(
                partition_by=SourceFetch.source_id,
                order_by=(SourceFetch.fetch_started_at.desc(), SourceFetch.id.desc()),
            )
            .label("row_number"),
        )
        .where(SourceFetch.source_id.in_(source_id_values))
        .subquery()
    )
    statement = (
        select(SourceFetch)
        .join(ranked_fetch_ids, SourceFetch.id == ranked_fetch_ids.c.fetch_id)
        .where(ranked_fetch_ids.c.row_number == 1)
    )
    return {fetch.source_id: fetch for fetch in session.scalars(statement)}


def get_latest_cache_headers(session: Session, source_id: int, requested_url: str) -> tuple[str | None, str | None]:
    statement = (
        select(SourceFetch)
        .where(
            SourceFetch.source_id == source_id,
            SourceFetch.requested_url == requested_url,
            SourceFetch.status.in_(("success", "not_modified")),
        )
        .order_by(SourceFetch.fetch_started_at.desc(), SourceFetch.id.desc())
    )
    for fetch in session.scalars(statement):
        if fetch.etag or fetch.last_modified:
            return fetch.etag, fetch.last_modified
    return None, None


def source_has_occurrences(session: Session, source_id: int) -> bool:
    statement = select(exists().where(SourceItemOccurrence.source_id == source_id))
    return bool(session.scalar(statement))


def sync_one_source(
    session: Session,
    source_id: int,
) -> SingleSourceSyncResponse:
    source = session.get(Source, source_id)
    if source is None:
        raise SourceSyncError(f"Source {source_id} was not found")
    return _sync_source(session, source)


def _sync_source(session: Session, source: Source) -> SingleSourceSyncResponse:
    try:
        target = resolve_source_fetch_url(source.kind, source.url, session)
    except SourceValidationError as exc:
        failure_stage = "rsshub_resolve_failed" if source.kind == "rsshub" else "source_resolve_failed"
        fetch = SourceFetch(
            source_id=source.id,
            requested_url=source.url,
            status="error",
            error_message=f"{failure_stage}: {exc}",
            items_seen_count=0,
            items_created_count=0,
            items_matched_count=0,
            items_invalid_count=0,
            fetch_finished_at=now_utc(),
        )
        session.add(fetch)
        session.flush()
        session.refresh(fetch)
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=fetch,
            created_feed_item_ids=[],
            feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
        )

    fetch = SourceFetch(
        source_id=source.id,
        requested_url=target.resolved_url,
        status="pending",
        items_seen_count=0,
        items_created_count=0,
        items_matched_count=0,
        items_invalid_count=0,
    )
    session.add(fetch)
    session.flush()

    etag, last_modified = (
        get_latest_cache_headers(session, source.id, target.resolved_url)
        if source_has_occurrences(session, source.id)
        else (None, None)
    )
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        with create_http_client() as client:
            response = safe_http_get(
                client,
                target.resolved_url,
                headers=headers,
                max_bytes=FEED_RESPONSE_MAX_BYTES,
                purpose="Feed sync",
                allow_private_network=source.kind == "rsshub",
            )
    except httpx.HTTPError as exc:
        finalize_fetch_error(fetch, message=f"fetch_failed: {exc}")
        session.add(fetch)
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=fetch,
            created_feed_item_ids=[],
            feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
        )
    except URLSafetyError as exc:
        finalize_fetch_error(fetch, message=f"url_safety_error: {exc}")
        session.add(fetch)
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=fetch,
            created_feed_item_ids=[],
            feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
        )

    fetch.http_status = response.status_code
    fetch.etag = response.headers.get("ETag") or etag
    fetch.last_modified = response.headers.get("Last-Modified") or last_modified

    if response.status_code == httpx.codes.NOT_MODIFIED:
        fetch.status = "not_modified"
        fetch.fetch_finished_at = now_utc()
        session.add(fetch)
        session.flush()
        session.refresh(fetch)
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=fetch,
            created_feed_item_ids=[],
            feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
        )

    response_text = response.text
    fetch.response_body = response_text

    if response.status_code != httpx.codes.OK:
        finalize_fetch_error(fetch, message=f"upstream_http_error: Unexpected HTTP status {response.status_code}")
        session.add(fetch)
        session.flush()
        session.refresh(fetch)
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=fetch,
            created_feed_item_ids=[],
            feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
        )

    try:
        document = parse_feed_document(response_text, base_url=str(response.url))
    except SourceSyncError as exc:
        finalize_fetch_error(fetch, message=f"feed_parse_failed: {exc}")
        session.add(fetch)
        session.flush()
        session.refresh(fetch)
        return SingleSourceSyncResponse(
            source_id=source.id,
            source_name=source.name,
            fetch=fetch,
            created_feed_item_ids=[],
            feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
        )

    source_icon = build_sync_icon(document, source, source_url=source.url if source.kind == "rss" else None)
    if apply_source_icon(source, source_icon):
        session.add(source)

    created_count = 0
    matched_count = 0
    invalid_count = 0
    touched_feed_item_ids: set[int] = set()
    created_feed_item_ids: set[int] = set()

    entries_to_process = select_entries_for_source_sync(document.entries)

    for entry in entries_to_process:
        if not entry.original_url or not entry.title:
            invalid_count += 1
            continue

        feed_item, match_rule, match_value, created = match_or_create_feed_item(session, entry)
        occurrence = SourceItemOccurrence(
            source_id=source.id,
            source_fetch_id=fetch.id,
            feed_item_id=feed_item.id,
            external_id=entry.external_id,
            original_url=entry.original_url,
            canonical_url=entry.canonical_url,
            published_at=entry.published_at,
            raw_entry_json=entry.raw_entry_json,
            match_rule=match_rule,
            match_value=match_value,
        )
        session.add(occurrence)
        touched_feed_item_ids.add(feed_item.id)
        if created:
            created_count += 1
            created_feed_item_ids.add(feed_item.id)
        else:
            matched_count += 1

    fetch.status = "success"
    fetch.items_seen_count = len(entries_to_process)
    fetch.items_created_count = created_count
    fetch.items_matched_count = matched_count
    fetch.items_invalid_count = invalid_count
    fetch.fetch_finished_at = now_utc()
    session.add(fetch)
    session.flush()

    if touched_feed_item_ids:
        for feed_item_id in sorted(touched_feed_item_ids):
            feed_item = session.get(FeedItem, feed_item_id)
            if feed_item is None:
                continue
            try:
                extract_feed_item_content(feed_item, record_debug=True)
                session.add(feed_item)
            except Exception:
                continue

    session.flush()
    session.refresh(fetch)

    return SingleSourceSyncResponse(
        source_id=source.id,
        source_name=source.name,
        fetch=fetch,
        created_feed_item_ids=sorted(created_feed_item_ids),
        feed_item_insight_artifacts=empty_feed_item_insight_artifact_stats(),
    )


def select_entries_for_source_sync(entries: list[NormalizedFeedEntry]) -> list[NormalizedFeedEntry]:
    if len(entries) <= SOURCE_SYNC_ITEM_LIMIT:
        return entries

    fallback_datetime = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        entries,
        key=lambda entry: entry.published_at or fallback_datetime,
        reverse=True,
    )[:SOURCE_SYNC_ITEM_LIMIT]


def empty_feed_item_insight_artifact_stats() -> FeedItemInsightArtifactBuildStats:
    return FeedItemInsightArtifactBuildStats(
        processed_feed_item_count=0,
        artifact_rows_created=0,
        failed_feed_item_count=0,
    )


def create_error_fetch(session: Session, source: Source, requested_url: str, message: str) -> SourceFetch:
    fetch = SourceFetch(
        source_id=source.id,
        requested_url=requested_url,
        status="error",
        error_message=message,
        items_seen_count=0,
        items_created_count=0,
        items_matched_count=0,
        items_invalid_count=0,
        fetch_finished_at=now_utc(),
    )
    session.add(fetch)
    session.flush()
    session.refresh(fetch)
    return fetch


def finalize_fetch_error(fetch: SourceFetch, message: str) -> None:
    fetch.status = "error"
    fetch.error_message = message
    fetch.fetch_finished_at = now_utc()


def build_sync_icon(document: NormalizedFeedDocument, source: Source, *, source_url: str | None) -> ResolvedSourceIcon:
    if document.icon_source:
        return ResolvedSourceIcon(icon_url=document.icon_url, icon_source=document.icon_source)  # type: ignore[arg-type]
    return resolve_source_site_icon(document.site_url or source.site_url, source_url)


def match_or_create_feed_item(
    session: Session,
    entry: NormalizedFeedEntry,
) -> tuple[FeedItem, str, str | None, bool]:
    if entry.external_id and entry.external_id_is_global:
        existing = find_by_external_id(session, entry.external_id)
        if existing is not None:
            apply_feed_content_fields(existing, entry)
            return existing, "external_id", entry.external_id, False

    if entry.canonical_url:
        existing = session.scalar(select(FeedItem).where(FeedItem.canonical_url == entry.canonical_url))
        if existing is not None:
            apply_feed_content_fields(existing, entry)
            return existing, "canonical_url", entry.canonical_url, False

    if entry.published_at:
        existing = session.scalar(
            select(FeedItem).where(
                FeedItem.title == entry.title,
                FeedItem.published_at == entry.published_at,
            )
        )
        if existing is not None:
            apply_feed_content_fields(existing, entry)
            return existing, "title_published_at", build_title_published_key(entry.title, entry.published_at), False

    try:
        with session.begin_nested():
            feed_item = FeedItem(
                original_url=entry.original_url,
                canonical_url=entry.canonical_url,
                title=entry.title,
                author_name=entry.author_name,
                published_at=entry.published_at,
                summary_text=entry.summary_text,
                feed_content_text=entry.feed_content_text,
                content_text=entry.content_text,
                content_text_source=entry.content_text_source,
                content_extraction_status="not_checked",
                topic_hints=None,
            )
            session.add(feed_item)
            session.flush()

            item_state = ItemState(feed_item_id=feed_item.id, status="unread")
            session.add(item_state)
            session.flush()
    except IntegrityError:
        if entry.canonical_url:
            existing = session.scalar(select(FeedItem).where(FeedItem.canonical_url == entry.canonical_url))
            if existing is not None:
                apply_feed_content_fields(existing, entry)
                return existing, "canonical_url", entry.canonical_url, False
        raise

    return feed_item, "created", entry.canonical_url or entry.original_url, True


def apply_feed_content_fields(feed_item: FeedItem, entry: NormalizedFeedEntry) -> None:
    if feed_item.feed_content_text is None and entry.feed_content_text is not None:
        feed_item.feed_content_text = entry.feed_content_text
    if not feed_item.content_text_source:
        if feed_item.content_text is not None and feed_item.content_text == entry.feed_content_text:
            feed_item.content_text_source = entry.content_text_source
        elif feed_item.content_text is not None and feed_item.content_text == entry.summary_text:
            feed_item.content_text_source = CONTENT_TEXT_SOURCE_FEED_SUMMARY


def find_by_external_id(session: Session, external_id: str) -> FeedItem | None:
    statement = (
        select(FeedItem)
        .join(SourceItemOccurrence, SourceItemOccurrence.feed_item_id == FeedItem.id)
        .where(SourceItemOccurrence.external_id == external_id)
        .order_by(FeedItem.id.asc())
    )
    return session.scalar(statement)


def parse_feed_document(document: str, *, base_url: str | None = None) -> NormalizedFeedDocument:
    try:
        root = parse_xml_document(document, error_message="Feed response is not valid XML")
    except XMLSafetyError as exc:
        raise SourceSyncError("Feed response is not valid XML") from exc

    tag = strip_namespace(root.tag)
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise SourceSyncError("RSS feed is missing channel")
        return parse_rss_channel(channel, base_url=base_url)
    if tag == "feed":
        return parse_atom_feed(root, base_url=base_url)

    raise SourceSyncError("Unsupported feed format; expected RSS or Atom")


def parse_rss_channel(channel: ElementTree.Element, *, base_url: str | None = None) -> NormalizedFeedDocument:
    site_url = normalize_optional_url(get_child_text(channel, "link"))
    icon = resolve_feed_icon(
        get_child_text(channel, "image/url"),
        "rss_channel_image",
        base_url=site_url or base_url,
    )
    items = channel.findall("item")
    _ensure_feed_entry_limit(len(items))
    return NormalizedFeedDocument(
        title=clean_text(get_child_text(channel, "title")),
        site_url=site_url,
        description=clean_text(get_child_text(channel, "description")),
        language=clean_text(get_child_text(channel, "language")),
        icon_url=icon.icon_url if icon else None,
        icon_source=icon.icon_source if icon else None,
        entries=[parse_rss_item(item) for item in items],
    )


def parse_atom_feed(feed: ElementTree.Element, *, base_url: str | None = None) -> NormalizedFeedDocument:
    language = clean_text(
        feed.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or feed.attrib.get("lang")
    )
    site_url = normalize_optional_url(extract_atom_feed_link(feed))
    icon = resolve_feed_icon(get_child_text(feed, "atom:icon", ATOM_NAMESPACE), "atom_icon", base_url=site_url or base_url)
    if icon is None:
        icon = resolve_feed_icon(get_child_text(feed, "atom:logo", ATOM_NAMESPACE), "atom_logo", base_url=site_url or base_url)
    entries = feed.findall("atom:entry", ATOM_NAMESPACE)
    _ensure_feed_entry_limit(len(entries))
    return NormalizedFeedDocument(
        title=clean_text(get_child_text(feed, "atom:title", ATOM_NAMESPACE)),
        site_url=site_url,
        description=clean_text(get_child_text(feed, "atom:subtitle", ATOM_NAMESPACE)),
        language=language,
        icon_url=icon.icon_url if icon else None,
        icon_source=icon.icon_source if icon else None,
        entries=[parse_atom_entry(entry) for entry in entries],
    )


def _ensure_feed_entry_limit(entry_count: int) -> None:
    if entry_count > FEED_ENTRY_MAX_COUNT:
        raise SourceSyncError(f"Feed response exceeds the {FEED_ENTRY_MAX_COUNT} entry limit")


def parse_rss_item(item: ElementTree.Element) -> NormalizedFeedEntry:
    original_url = normalize_optional_url(get_child_text(item, "link"))
    title = clean_text(get_child_text(item, "title")) or "(untitled)"
    external_id = clean_text(get_child_text(item, "guid"))
    published_at = parse_datetime_value(get_child_text(item, "pubDate"))
    author_name = clean_text(get_child_text(item, "author")) or clean_text(get_child_text(item, "{http://purl.org/dc/elements/1.1/}creator"))
    summary_text = clean_text(get_child_text(item, "description"))
    encoded_content = clean_text(get_child_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded"))
    feed_content_text = encoded_content or summary_text
    content_text_source = CONTENT_TEXT_SOURCE_FEED_CONTENT if encoded_content else CONTENT_TEXT_SOURCE_FEED_SUMMARY
    canonical_url = normalize_optional_url(original_url)

    return NormalizedFeedEntry(
        external_id=external_id,
        external_id_is_global=is_global_external_id(external_id),
        original_url=original_url or "",
        canonical_url=canonical_url,
        title=title,
        author_name=author_name,
        published_at=published_at,
        summary_text=summary_text,
        feed_content_text=feed_content_text,
        content_text=feed_content_text,
        content_text_source=content_text_source,
        raw_entry_json={
            "format": "rss",
            "title": title,
            "guid": external_id,
            "link": original_url,
            "published": get_child_text(item, "pubDate"),
        },
    )


def parse_atom_entry(entry: ElementTree.Element) -> NormalizedFeedEntry:
    original_url = normalize_optional_url(extract_atom_link(entry))
    title = clean_text(get_child_text(entry, "atom:title", ATOM_NAMESPACE)) or "(untitled)"
    external_id = clean_text(get_child_text(entry, "atom:id", ATOM_NAMESPACE))
    published_at = parse_datetime_value(
        get_child_text(entry, "atom:published", ATOM_NAMESPACE) or get_child_text(entry, "atom:updated", ATOM_NAMESPACE)
    )
    author_name = clean_text(get_child_text(entry.find("atom:author", ATOM_NAMESPACE), "atom:name", ATOM_NAMESPACE))
    summary_text = clean_text(get_child_text(entry, "atom:summary", ATOM_NAMESPACE))
    atom_content = clean_text(get_child_text(entry, "atom:content", ATOM_NAMESPACE))
    feed_content_text = atom_content or summary_text
    content_text_source = CONTENT_TEXT_SOURCE_FEED_CONTENT if atom_content else CONTENT_TEXT_SOURCE_FEED_SUMMARY
    canonical_url = normalize_optional_url(original_url)

    return NormalizedFeedEntry(
        external_id=external_id,
        external_id_is_global=is_global_external_id(external_id),
        original_url=original_url or "",
        canonical_url=canonical_url,
        title=title,
        author_name=author_name,
        published_at=published_at,
        summary_text=summary_text,
        feed_content_text=feed_content_text,
        content_text=feed_content_text,
        content_text_source=content_text_source,
        raw_entry_json={
            "format": "atom",
            "title": title,
            "id": external_id,
            "link": original_url,
            "published": get_child_text(entry, "atom:published", ATOM_NAMESPACE),
            "updated": get_child_text(entry, "atom:updated", ATOM_NAMESPACE),
        },
    )


def extract_atom_link(entry: ElementTree.Element) -> str | None:
    links = entry.findall("atom:link", ATOM_NAMESPACE)
    for link in links:
        rel = (link.attrib.get("rel") or "alternate").strip().lower()
        href = link.attrib.get("href")
        if rel == "alternate" and href:
            return href

    for link in links:
        href = link.attrib.get("href")
        if href:
            return href

    return None


def extract_atom_feed_link(feed: ElementTree.Element) -> str | None:
    links = feed.findall("atom:link", ATOM_NAMESPACE)
    for link in links:
        rel = (link.attrib.get("rel") or "alternate").strip().lower()
        href = link.attrib.get("href")
        if rel in {"alternate", "self"} and href:
            return href

    for link in links:
        href = link.attrib.get("href")
        if href:
            return href

    return None


def parse_datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None

    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None

    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = " ".join(value.split())
    return normalized or None


def normalize_optional_url(value: str | None) -> str | None:
    if value is None:
        return None

    try:
        normalized = normalize_url(value)
    except SourceValidationError:
        return None
    if normalized is None:
        return None
    return strip_url_fragment(normalized)


def strip_url_fragment(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse(ParseResult(parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def is_global_external_id(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized.startswith(GLOBAL_EXTERNAL_ID_PREFIXES)


def build_title_published_key(title: str, published_at: datetime) -> str:
    return f"{title}\n{published_at.isoformat()}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_child_text(
    node: ElementTree.Element | None,
    path: str,
    namespaces: dict[str, str] | None = None,
) -> str | None:
    if node is None:
        return None
    child = node.find(path, namespaces or {})
    if child is None:
        return None
    return "".join(child.itertext()).strip() or None


def strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1]
