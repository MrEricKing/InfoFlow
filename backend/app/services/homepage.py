from __future__ import annotations

import base64
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import threading

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.db.models import (
    InsightEngineArtifact,
    FeedItem,
    FeedItemHomepageFacet,
    FeedItemHomepageReadModel,
    FeedItemStructuredFeedback,
    InsightChannel,
    ItemState,
    Source,
    SourceChannelTendency,
    SourceItemOccurrence,
)
from app.schemas.homepage import (
    HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID,
    HomepageBootstrapRead,
    HomepageCardRead,
    HomepageFacetCountRead,
    HomepageItemsRead,
    HomepagePriorityReasonRead,
    HomepageQuery,
    HomepageReadModelStatusRead,
    HomepageSecondaryChannelRead,
    HomepageSummaryRead,
)
from app.services.homepage_read_model import (
    HOMEPAGE_READ_MODEL_FACET_CHANNEL,
    HOMEPAGE_READ_MODEL_FACET_TOPIC,
    HOMEPAGE_READ_MODEL_VERSION,
    build_homepage_read_model_status,
    ensure_homepage_read_model_backfill_started,
    get_homepage_read_model_health,
)
from app.services.insight_engine_artifacts import (
    build_latest_occurrence_subquery,
    load_latest_feed_item_insight_artifacts,
    resolve_feed_item_insight_artifacts,
    resolve_routing,
)
from app.services.feed_content_extraction import read_content_extraction_diagnostics
from app.services.insight_scoring import (
    ArticleScoringSignals,
    InsightScoringContext,
    build_insight_scoring_context,
    extract_article_scoring_signals,
    score_feed_item_priority,
)
from app.services.reader import build_reader_item_state
from app.services.structured_feedback import (
    apply_structured_channel_feedback_to_routing,
    load_structured_feedback_map,
    serialize_structured_feedback,
)
from app.services.insight_channels import channel_is_active, ensure_default_channels

EMPTY_SOURCE_CATEGORY_LABEL = "未分类"
HOMEPAGE_CURSOR_VERSION = 1
HOMEPAGE_SNAPSHOT_TTL = timedelta(minutes=3)
HOMEPAGE_MAX_SNAPSHOTS = 20
HOMEPAGE_MAX_SORTED_IDS_PER_SNAPSHOT = 50_000
TIME_WINDOW_DELTA = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
}


@dataclass(frozen=True)
class _HomepageSnapshot:
    token: str
    query_signature: str
    generated_at: datetime
    expires_at: datetime
    sort: str
    page_size: int
    time_window_reference: datetime
    sorted_item_ids: tuple[int, ...]
    summary: HomepageSummaryRead
    generation: int


class HomepageSnapshotError(ValueError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


_snapshot_lock = threading.RLock()
_snapshot_generation = 0
_homepage_snapshots: OrderedDict[str, _HomepageSnapshot] = OrderedDict()


def get_homepage_bootstrap(session: Session, query: HomepageQuery) -> HomepageBootstrapRead:
    query = _fresh_bootstrap_query(query)
    generated_at = _now_utc()
    query_signature = build_homepage_query_signature(query)
    time_window_reference = _time_window_reference_for_query(query, generated_at)
    read_model_health = get_homepage_read_model_health(session)
    if read_model_health.is_fresh:
        read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=False)
        sorted_item_ids = _list_homepage_read_model_item_ids(
            session,
            query,
            time_window_reference=time_window_reference,
            limit=max(HOMEPAGE_MAX_SORTED_IDS_PER_SNAPSHOT + 1, query.page_size + 1),
        )
        page_ids = sorted_item_ids[: query.page_size]
        items = _load_homepage_read_model_cards(session, page_ids)
        has_more = len(sorted_item_ids) > len(page_ids)
        next_cursor = (
            _encode_cursor_for_anchor(
                page_ids[-1],
                query=query,
                query_signature=query_signature,
                time_window_reference=time_window_reference,
            )
            if has_more and page_ids
            else None
        )
        summary = _build_homepage_summary_from_read_model(
            session,
            query,
            query_signature=query_signature,
            generated_at=generated_at,
            time_window_reference=time_window_reference,
            read_model_status=read_model_status,
        )
        snapshot_token, snapshot_expires_at = _store_homepage_snapshot(
            query=query,
            query_signature=query_signature,
            generated_at=generated_at,
            time_window_reference=time_window_reference,
            sorted_item_ids=sorted_item_ids,
            summary=summary,
        )
        return HomepageBootstrapRead(
            query_signature=query_signature,
            generated_at=generated_at,
            summary=summary,
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            page_size=query.page_size,
            snapshot_token=snapshot_token,
            snapshot_expires_at=snapshot_expires_at,
            read_model_status=read_model_status,
        )

    ensure_homepage_read_model_backfill_started("homepage_bootstrap")
    read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=True)
    all_cards = _build_homepage_cards(session)
    matching_cards = _filter_cards_by_query(
        all_cards,
        query,
        exclude_facet=None,
        time_window_reference=time_window_reference,
    )
    sorted_cards = _sort_cards_for_query(matching_cards, query.sort)
    summary = _build_homepage_summary_from_cards(
        all_cards,
        query,
        query_signature=query_signature,
        generated_at=generated_at,
        time_window_reference=time_window_reference,
        read_model_status=read_model_status,
    )
    items, next_cursor, has_more = _window_cards(
        sorted_cards,
        query=query,
        query_signature=query_signature,
        time_window_reference=time_window_reference,
    )
    snapshot_token, snapshot_expires_at = _store_homepage_snapshot(
        query=query,
        query_signature=query_signature,
        generated_at=generated_at,
        time_window_reference=time_window_reference,
        sorted_item_ids=[card.id for card in sorted_cards],
        summary=summary,
    )
    return HomepageBootstrapRead(
        query_signature=query_signature,
        generated_at=generated_at,
        summary=summary,
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        page_size=query.page_size,
        snapshot_token=snapshot_token,
        snapshot_expires_at=snapshot_expires_at,
        read_model_status=read_model_status,
    )


def get_homepage_summary(
    session: Session,
    query: HomepageQuery,
    *,
    generated_at: datetime | None = None,
) -> HomepageSummaryRead:
    generated_at = generated_at or _now_utc()
    query_signature = build_homepage_query_signature(query)
    read_model_health = get_homepage_read_model_health(session)
    if read_model_health.is_fresh:
        read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=False)
        return _build_homepage_summary_from_read_model(
            session,
            query,
            query_signature=query_signature,
            generated_at=generated_at,
            time_window_reference=generated_at,
            read_model_status=read_model_status,
        )

    ensure_homepage_read_model_backfill_started("homepage_summary")
    read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=True)
    scoped_cards = _build_homepage_cards(
        session,
        source_id=query.source_id,
        source_category=query.source_category,
    )
    return _build_homepage_summary_from_cards(
        scoped_cards,
        query,
        query_signature=query_signature,
        generated_at=generated_at,
        time_window_reference=generated_at,
        source_cards_for_facets=lambda facet: _cards_for_summary_facet(session, query, scoped_cards, facet),
        read_model_status=read_model_status,
    )


def _build_homepage_summary_from_cards(
    cards: list[HomepageCardRead],
    query: HomepageQuery,
    *,
    query_signature: str,
    generated_at: datetime,
    time_window_reference: datetime,
    source_cards_for_facets=None,
    read_model_status: HomepageReadModelStatusRead | None = None,
) -> HomepageSummaryRead:
    matching_cards = _filter_cards_by_query(
        cards,
        query,
        exclude_facet=None,
        time_window_reference=time_window_reference,
    )

    def cards_for_facet(facet: str) -> list[HomepageCardRead]:
        return source_cards_for_facets(facet) if source_cards_for_facets is not None else cards

    return HomepageSummaryRead(
        query_signature=query_signature,
        generated_at=generated_at,
        total_count=len(matching_cards),
        unread_count=sum(1 for card in matching_cards if card.state.status == "unread"),
        saved_count=sum(1 for card in matching_cards if card.state.status == "saved"),
        not_interested_count=sum(1 for card in matching_cards if card.state.status == "not_interested"),
        channel_counts=_count_channel_facets(
            _filter_cards_by_query(
                cards_for_facet("channel"),
                query,
                exclude_facet="channel",
                time_window_reference=time_window_reference,
            )
        ),
        source_counts=_count_source_facets(
            _filter_cards_by_query(
                cards_for_facet("source"),
                query,
                exclude_facet="source",
                time_window_reference=time_window_reference,
            )
        ),
        source_category_counts=_count_source_category_facets(
            _filter_cards_by_query(
                cards_for_facet("source_category"),
                query,
                exclude_facet="source_category",
                time_window_reference=time_window_reference,
            )
        ),
        topic_counts=_count_text_facets(
            _filter_cards_by_query(
                cards_for_facet("topic"),
                query,
                exclude_facet="topic",
                time_window_reference=time_window_reference,
            ),
            lambda card: card.topic_labels,
        ),
        flow_lane_counts=_count_flow_lane_facets(
            _filter_cards_by_query(
                cards_for_facet("flow_lane"),
                query,
                exclude_facet="flow_lane",
                time_window_reference=time_window_reference,
            )
        ),
        state_counts=_count_state_facets(
            _filter_cards_by_query(
                cards_for_facet("state"),
                query,
                exclude_facet="state",
                time_window_reference=time_window_reference,
            )
        ),
        read_model_status=read_model_status,
    )


def _build_homepage_summary_from_read_model(
    session: Session,
    query: HomepageQuery,
    *,
    query_signature: str,
    generated_at: datetime,
    time_window_reference: datetime,
    read_model_status: HomepageReadModelStatusRead,
) -> HomepageSummaryRead:
    return HomepageSummaryRead(
        query_signature=query_signature,
        generated_at=generated_at,
        total_count=_count_homepage_read_model_rows(
            session,
            query,
            exclude_facet=None,
            time_window_reference=time_window_reference,
        ),
        unread_count=_count_homepage_read_model_rows(
            session,
            query,
            exclude_facet=None,
            time_window_reference=time_window_reference,
            extra_conditions=[FeedItemHomepageReadModel.state == "unread"],
        ),
        saved_count=_count_homepage_read_model_rows(
            session,
            query,
            exclude_facet=None,
            time_window_reference=time_window_reference,
            extra_conditions=[FeedItemHomepageReadModel.state == "saved"],
        ),
        not_interested_count=_count_homepage_read_model_rows(
            session,
            query,
            exclude_facet=None,
            time_window_reference=time_window_reference,
            extra_conditions=[FeedItemHomepageReadModel.state == "not_interested"],
        ),
        channel_counts=_count_homepage_read_model_joined_facets(
            session,
            query,
            facet_kind=HOMEPAGE_READ_MODEL_FACET_CHANNEL,
            exclude_facet="channel",
            time_window_reference=time_window_reference,
        ),
        source_counts=_count_homepage_read_model_column_facets(
            session,
            query,
            id_column=FeedItemHomepageReadModel.source_id,
            label_column=FeedItemHomepageReadModel.source_name,
            exclude_facet="source",
            time_window_reference=time_window_reference,
        ),
        source_category_counts=_count_homepage_read_model_column_facets(
            session,
            query,
            id_column=FeedItemHomepageReadModel.source_category_id,
            label_column=FeedItemHomepageReadModel.source_category_label,
            exclude_facet="source_category",
            time_window_reference=time_window_reference,
        ),
        topic_counts=_count_homepage_read_model_joined_facets(
            session,
            query,
            facet_kind=HOMEPAGE_READ_MODEL_FACET_TOPIC,
            exclude_facet="topic",
            time_window_reference=time_window_reference,
        ),
        flow_lane_counts=_count_homepage_read_model_column_facets(
            session,
            query,
            id_column=FeedItemHomepageReadModel.flow_lane_facet,
            label_column=FeedItemHomepageReadModel.flow_lane_facet,
            exclude_facet="flow_lane",
            time_window_reference=time_window_reference,
        ),
        state_counts=_count_homepage_read_model_column_facets(
            session,
            query,
            id_column=FeedItemHomepageReadModel.state,
            label_column=FeedItemHomepageReadModel.state,
            exclude_facet="state",
            time_window_reference=time_window_reference,
        ),
        read_model_status=read_model_status,
    )


def _count_homepage_read_model_rows(
    session: Session,
    query: HomepageQuery,
    *,
    exclude_facet: str | None,
    time_window_reference: datetime,
    extra_conditions: Sequence[object] | None = None,
) -> int:
    statement = select(func.count()).select_from(FeedItemHomepageReadModel).where(
        *_homepage_read_model_conditions(query, exclude_facet=exclude_facet, time_window_reference=time_window_reference),
        *(extra_conditions or []),
    )
    return int(session.scalar(statement) or 0)


def _count_homepage_read_model_column_facets(
    session: Session,
    query: HomepageQuery,
    *,
    id_column,
    label_column,
    exclude_facet: str,
    time_window_reference: datetime,
) -> list[HomepageFacetCountRead]:
    statement = (
        select(id_column, label_column, func.count())
        .select_from(FeedItemHomepageReadModel)
        .where(
            *_homepage_read_model_conditions(
                query,
                exclude_facet=exclude_facet,
                time_window_reference=time_window_reference,
            )
        )
        .group_by(id_column, label_column)
    )
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for facet_id, label, count in session.execute(statement):
        key = str(facet_id)
        counter[key] += int(count)
        labels[key] = str(label)
    return _facet_counts_from_counter(counter, labels)


def _count_homepage_read_model_joined_facets(
    session: Session,
    query: HomepageQuery,
    *,
    facet_kind: str,
    exclude_facet: str,
    time_window_reference: datetime,
) -> list[HomepageFacetCountRead]:
    statement = (
        select(FeedItemHomepageFacet.facet_id, FeedItemHomepageFacet.facet_label, func.count())
        .select_from(FeedItemHomepageFacet)
        .join(
            FeedItemHomepageReadModel,
            FeedItemHomepageReadModel.feed_item_id == FeedItemHomepageFacet.feed_item_id,
        )
        .where(
            FeedItemHomepageFacet.facet_kind == facet_kind,
            *_homepage_read_model_conditions(
                query,
                exclude_facet=exclude_facet,
                time_window_reference=time_window_reference,
            ),
        )
        .group_by(FeedItemHomepageFacet.facet_id, FeedItemHomepageFacet.facet_label)
    )
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for facet_id, label, count in session.execute(statement):
        counter[str(facet_id)] += int(count)
        labels[str(facet_id)] = str(label)
    return _facet_counts_from_counter(counter, labels)


def list_homepage_items(
    session: Session,
    query: HomepageQuery,
    *,
    generated_at: datetime | None = None,
) -> HomepageItemsRead:
    generated_at = generated_at or _now_utc()
    query_signature = build_homepage_query_signature(query)
    if query.snapshot_token:
        snapshot = _load_homepage_snapshot(query, query_signature=query_signature, generated_at=generated_at)
        read_model_health = get_homepage_read_model_health(session)
        read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=not read_model_health.is_fresh)
        if read_model_health.is_fresh:
            items, next_cursor, has_more = _window_cards_from_snapshot(
                session,
                snapshot=snapshot,
                query=query,
                prefer_read_model=True,
            )
        else:
            ensure_homepage_read_model_backfill_started("homepage_items_snapshot")
            items, next_cursor, has_more = _window_cards_from_snapshot(session, snapshot=snapshot, query=query)
        return HomepageItemsRead(
            query_signature=snapshot.query_signature,
            generated_at=snapshot.generated_at,
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            page_size=query.page_size,
            snapshot_token=snapshot.token,
            snapshot_expires_at=snapshot.expires_at,
            read_model_status=read_model_status,
        )

    time_window_reference = _time_window_reference_for_query(query, generated_at)
    read_model_health = get_homepage_read_model_health(session)
    if read_model_health.is_fresh:
        read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=False)
        page_ids, next_cursor, has_more = _list_homepage_read_model_page_item_ids(
            session,
            query,
            query_signature=query_signature,
            time_window_reference=time_window_reference,
        )
        return HomepageItemsRead(
            query_signature=query_signature,
            generated_at=generated_at,
            items=_load_homepage_read_model_cards(session, page_ids),
            next_cursor=next_cursor,
            has_more=has_more,
            page_size=query.page_size,
            read_model_status=read_model_status,
        )

    ensure_homepage_read_model_backfill_started("homepage_items")
    read_model_status = build_homepage_read_model_status(read_model_health, fallback_live=True)
    cards = _list_cards_for_homepage_query(
        session,
        query,
        time_window_reference=time_window_reference,
    )
    items, next_cursor, has_more = _window_cards(
        cards,
        query=query,
        query_signature=query_signature,
        time_window_reference=time_window_reference,
    )
    return HomepageItemsRead(
        query_signature=query_signature,
        generated_at=generated_at,
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        page_size=query.page_size,
        read_model_status=read_model_status,
    )


def invalidate_homepage_snapshots() -> int:
    global _snapshot_generation
    with _snapshot_lock:
        _snapshot_generation += 1
        _prune_homepage_snapshots_locked(_now_utc())
        return _snapshot_generation


def build_homepage_query_signature(query: HomepageQuery) -> str:
    payload = {
        "channel_id": query.channel_id or None,
        "source_id": query.source_id,
        "source_category": _normalize_source_category_query_value(query.source_category),
        "flow_lane": query.flow_lane or None,
        "reading_depth": query.reading_depth or None,
        "state": query.state or None,
        "saved": query.saved,
        "not_interested": query.not_interested,
        "topic": query.topic or None,
        "search_text": _normalize_search_text(query.search_text),
        "time_window": query.time_window,
        "sort": query.sort,
        "page_size": query.page_size,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _build_homepage_cards(
    session: Session,
    *,
    source_id: int | None = None,
    source_category: str | None = None,
    feed_item_ids: Sequence[int] | None = None,
) -> list[HomepageCardRead]:
    if feed_item_ids is not None and not feed_item_ids:
        return []

    latest_occurrences = build_latest_occurrence_subquery()
    sort_timestamp = func.coalesce(
        FeedItem.published_at,
        SourceItemOccurrence.published_at,
        SourceItemOccurrence.created_at,
    )
    statement = (
        select(FeedItem, ItemState, SourceItemOccurrence, Source)
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id) & (latest_occurrences.c.occurrence_rank == 1),
        )
        .join(SourceItemOccurrence, SourceItemOccurrence.id == latest_occurrences.c.occurrence_id)
        .join(Source, Source.id == SourceItemOccurrence.source_id)
        .order_by(sort_timestamp.desc(), FeedItem.id.desc())
    )
    if source_id is not None:
        statement = statement.where(Source.id == source_id)
    if source_category is not None:
        normalized_category = _normalize_source_category_query_value(source_category)
        if normalized_category == HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID:
            statement = statement.where((Source.folder_path.is_(None)) | (Source.folder_path == ""))
        else:
            statement = statement.where(Source.folder_path == normalized_category)
    if feed_item_ids is not None:
        statement = statement.where(FeedItem.id.in_(list(feed_item_ids)))

    rows = session.execute(statement).all()
    structured_feedback_map = load_structured_feedback_map(
        session,
        [feed_item.id for feed_item, _, _, _ in rows],
    )
    artifact_map = load_latest_feed_item_insight_artifacts(
        session,
        feed_item_ids=[feed_item.id for feed_item, _, _, _ in rows],
    )
    channels_by_id = _load_channels_by_id(session)
    source_tendencies_by_source_id = _load_source_tendencies_by_source_id(
        session,
        source_ids=[source.id for _, _, _, source in rows],
    )
    scoring_signals_by_item_id = {
        feed_item.id: extract_article_scoring_signals(artifact_map.get(feed_item.id))
        for feed_item, _, _, _ in rows
    }
    scoring_context = build_insight_scoring_context(
        session,
        feed_item_ids=[feed_item.id for feed_item, _, _, _ in rows],
        source_ids=[source.id for _, _, _, source in rows],
        target_topic_ids=[
            topic_id
            for signals in scoring_signals_by_item_id.values()
            for topic_id in signals.topic_ids
        ],
    )
    cards = [
        _build_homepage_card(
            feed_item,
            item_state,
            occurrence,
            source,
            artifact_map.get(feed_item.id),
            scoring_signals_by_item_id.get(feed_item.id, ArticleScoringSignals()),
            scoring_context,
            structured_feedback_map.get(feed_item.id),
            channels_by_id,
            source_tendencies_by_source_id.get(source.id, []),
        )
        for feed_item, item_state, occurrence, source in rows
    ]
    if feed_item_ids is not None:
        order_by_item_id = {feed_item_id: index for index, feed_item_id in enumerate(feed_item_ids)}
        return sorted(cards, key=lambda card: order_by_item_id.get(card.id, len(order_by_item_id)))

    cards = sorted(
        cards,
        key=lambda card: (
            card.low_priority_bucket,
            -card.priority_score,
            -_timestamp_for_sort(card.published_at),
            -card.id,
        ),
    )
    return cards


def _fresh_bootstrap_query(query: HomepageQuery) -> HomepageQuery:
    return query.model_copy(update={"cursor": None, "snapshot_token": None})


def _store_homepage_snapshot(
    *,
    query: HomepageQuery,
    query_signature: str,
    generated_at: datetime,
    time_window_reference: datetime,
    sorted_item_ids: Sequence[int],
    summary: HomepageSummaryRead,
) -> tuple[str | None, datetime | None]:
    if len(sorted_item_ids) > HOMEPAGE_MAX_SORTED_IDS_PER_SNAPSHOT:
        return None, None
    token = secrets.token_urlsafe(18)
    expires_at = generated_at + HOMEPAGE_SNAPSHOT_TTL
    with _snapshot_lock:
        _prune_homepage_snapshots_locked(generated_at)
        snapshot = _HomepageSnapshot(
            token=token,
            query_signature=query_signature,
            generated_at=generated_at,
            expires_at=expires_at,
            sort=query.sort,
            page_size=query.page_size,
            time_window_reference=time_window_reference,
            sorted_item_ids=tuple(sorted_item_ids),
            summary=summary,
            generation=_snapshot_generation,
        )
        _homepage_snapshots[token] = snapshot
        _homepage_snapshots.move_to_end(token)
        while len(_homepage_snapshots) > HOMEPAGE_MAX_SNAPSHOTS:
            _homepage_snapshots.popitem(last=False)
    return token, expires_at


def _load_homepage_snapshot(
    query: HomepageQuery,
    *,
    query_signature: str,
    generated_at: datetime,
) -> _HomepageSnapshot:
    token = query.snapshot_token
    if not token:
        raise HomepageSnapshotError("snapshot_expired")
    with _snapshot_lock:
        _prune_homepage_snapshots_locked(generated_at)
        snapshot = _homepage_snapshots.get(token)
        if snapshot is None:
            raise HomepageSnapshotError("snapshot_expired")
        if snapshot.generation != _snapshot_generation:
            raise HomepageSnapshotError("snapshot_invalidated")
        if snapshot.expires_at <= generated_at:
            _homepage_snapshots.pop(token, None)
            raise HomepageSnapshotError("snapshot_expired")
        if (
            snapshot.query_signature != query_signature
            or snapshot.sort != query.sort
            or snapshot.page_size != query.page_size
        ):
            raise HomepageSnapshotError("snapshot_invalidated")
        _homepage_snapshots.move_to_end(token)
        return snapshot


def _prune_homepage_snapshots_locked(now: datetime) -> None:
    expired_tokens = [
        token
        for token, snapshot in _homepage_snapshots.items()
        if snapshot.expires_at <= now
    ]
    for token in expired_tokens:
        _homepage_snapshots.pop(token, None)


def _window_cards_from_snapshot(
    session: Session,
    *,
    snapshot: _HomepageSnapshot,
    query: HomepageQuery,
    prefer_read_model: bool = False,
) -> tuple[list[HomepageCardRead], str | None, bool]:
    page_ids, next_cursor, has_more = _window_item_ids_from_snapshot(snapshot=snapshot, query=query)
    cards = _load_homepage_read_model_cards(session, page_ids) if prefer_read_model else _build_homepage_cards(session, feed_item_ids=page_ids)
    cards_by_id = {card.id: card for card in cards}
    return [cards_by_id[item_id] for item_id in page_ids if item_id in cards_by_id], next_cursor, has_more


def _window_item_ids_from_snapshot(
    *,
    snapshot: _HomepageSnapshot,
    query: HomepageQuery,
) -> tuple[list[int], str | None, bool]:
    start_index = 0
    if query.cursor:
        cursor = _decode_cursor(query.cursor)
        if cursor.get("version") != HOMEPAGE_CURSOR_VERSION:
            raise ValueError("Unsupported homepage cursor version")
        if cursor.get("query_signature") != snapshot.query_signature or cursor.get("sort") != query.sort:
            raise ValueError("Homepage cursor does not match the current query")
        anchor_item_id = cursor.get("anchor_item_id")
        for index, item_id in enumerate(snapshot.sorted_item_ids):
            if item_id == anchor_item_id:
                start_index = index + 1
                break
        else:
            raise ValueError("Homepage cursor anchor is no longer available")

    page_ids = list(snapshot.sorted_item_ids[start_index : start_index + query.page_size])
    has_more = start_index + len(page_ids) < len(snapshot.sorted_item_ids)
    next_cursor = (
        _encode_cursor_for_anchor(
            page_ids[-1],
            query=query,
            query_signature=snapshot.query_signature,
            time_window_reference=snapshot.time_window_reference,
        )
        if has_more and page_ids
        else None
    )
    return page_ids, next_cursor, has_more


def _window_item_ids_from_sorted_ids(
    sorted_item_ids: Sequence[int],
    *,
    query: HomepageQuery,
    query_signature: str,
    time_window_reference: datetime | None,
) -> tuple[list[int], str | None, bool]:
    start_index = 0
    if query.cursor:
        cursor = _decode_cursor(query.cursor)
        if cursor.get("version") != HOMEPAGE_CURSOR_VERSION:
            raise ValueError("Unsupported homepage cursor version")
        if cursor.get("query_signature") != query_signature or cursor.get("sort") != query.sort:
            raise ValueError("Homepage cursor does not match the current query")
        anchor_item_id = cursor.get("anchor_item_id")
        for index, item_id in enumerate(sorted_item_ids):
            if item_id == anchor_item_id:
                start_index = index + 1
                break
        else:
            raise ValueError("Homepage cursor anchor is no longer available")

    page_ids = list(sorted_item_ids[start_index : start_index + query.page_size])
    has_more = start_index + len(page_ids) < len(sorted_item_ids)
    next_cursor = (
        _encode_cursor_for_anchor(
            page_ids[-1],
            query=query,
            query_signature=query_signature,
            time_window_reference=time_window_reference,
        )
        if has_more and page_ids
        else None
    )
    return page_ids, next_cursor, has_more


def _list_homepage_read_model_item_ids(
    session: Session,
    query: HomepageQuery,
    *,
    time_window_reference: datetime,
    limit: int | None,
) -> list[int]:
    statement = (
        select(FeedItemHomepageReadModel.feed_item_id)
        .where(
            *_homepage_read_model_conditions(
                query,
                exclude_facet=None,
                time_window_reference=time_window_reference,
            )
        )
        .order_by(*_homepage_read_model_order_by(query.sort))
    )
    if limit is not None:
        statement = statement.limit(limit)
    return [int(feed_item_id) for feed_item_id in session.scalars(statement)]


def _list_homepage_read_model_page_item_ids(
    session: Session,
    query: HomepageQuery,
    *,
    query_signature: str,
    time_window_reference: datetime,
) -> tuple[list[int], str | None, bool]:
    statement = (
        select(FeedItemHomepageReadModel.feed_item_id)
        .where(
            *_homepage_read_model_conditions(
                query,
                exclude_facet=None,
                time_window_reference=time_window_reference,
            )
        )
        .order_by(*_homepage_read_model_order_by(query.sort))
        .limit(query.page_size + 1)
    )
    if query.cursor:
        statement = statement.where(
            _homepage_read_model_seek_condition(
                session,
                query=query,
                query_signature=query_signature,
            )
        )
    ids = [int(feed_item_id) for feed_item_id in session.scalars(statement)]
    page_ids = ids[: query.page_size]
    has_more = len(ids) > query.page_size
    next_cursor = (
        _encode_cursor_for_anchor(
            page_ids[-1],
            query=query,
            query_signature=query_signature,
            time_window_reference=time_window_reference,
        )
        if has_more and page_ids
        else None
    )
    return page_ids, next_cursor, has_more


def _homepage_read_model_seek_condition(session: Session, *, query: HomepageQuery, query_signature: str):
    cursor = _decode_cursor(query.cursor)
    if cursor.get("version") != HOMEPAGE_CURSOR_VERSION:
        raise ValueError("Unsupported homepage cursor version")
    if cursor.get("query_signature") != query_signature or cursor.get("sort") != query.sort:
        raise ValueError("Homepage cursor does not match the current query")
    anchor_item_id = cursor.get("anchor_item_id")
    if not isinstance(anchor_item_id, int):
        raise ValueError("Homepage cursor anchor is invalid")
    anchor = session.get(FeedItemHomepageReadModel, anchor_item_id)
    if anchor is None:
        raise ValueError("Homepage cursor anchor is no longer available")
    published_at = anchor.published_at
    if query.sort == "newest":
        return or_(
            FeedItemHomepageReadModel.published_at < published_at,
            and_(
                FeedItemHomepageReadModel.published_at == published_at,
                FeedItemHomepageReadModel.feed_item_id < anchor.feed_item_id,
            ),
        )
    if query.sort == "source":
        source_name = (anchor.source_name or "").casefold()
        source_name_expr = func.lower(FeedItemHomepageReadModel.source_name)
        return or_(
            source_name_expr > source_name,
            and_(
                source_name_expr == source_name,
                FeedItemHomepageReadModel.published_at < published_at,
            ),
            and_(
                source_name_expr == source_name,
                FeedItemHomepageReadModel.published_at == published_at,
                FeedItemHomepageReadModel.feed_item_id < anchor.feed_item_id,
            ),
        )
    return or_(
        FeedItemHomepageReadModel.low_priority_bucket > anchor.low_priority_bucket,
        and_(
            FeedItemHomepageReadModel.low_priority_bucket == anchor.low_priority_bucket,
            FeedItemHomepageReadModel.priority_score < anchor.priority_score,
        ),
        and_(
            FeedItemHomepageReadModel.low_priority_bucket == anchor.low_priority_bucket,
            FeedItemHomepageReadModel.priority_score == anchor.priority_score,
            FeedItemHomepageReadModel.published_at < published_at,
        ),
        and_(
            FeedItemHomepageReadModel.low_priority_bucket == anchor.low_priority_bucket,
            FeedItemHomepageReadModel.priority_score == anchor.priority_score,
            FeedItemHomepageReadModel.published_at == published_at,
            FeedItemHomepageReadModel.feed_item_id < anchor.feed_item_id,
        ),
    )


def _load_homepage_read_model_cards(session: Session, feed_item_ids: Sequence[int]) -> list[HomepageCardRead]:
    ids = list(feed_item_ids)
    if not ids:
        return []
    rows = session.scalars(
        select(FeedItemHomepageReadModel).where(FeedItemHomepageReadModel.feed_item_id.in_(ids))
    ).all()
    rows_by_id = {row.feed_item_id: row for row in rows}
    cards: list[HomepageCardRead] = []
    for feed_item_id in ids:
        row = rows_by_id.get(feed_item_id)
        if row is None:
            continue
        cards.append(HomepageCardRead.model_validate(row.card_json))
    return cards


def _homepage_read_model_order_by(sort: str):
    if sort == "newest":
        return (
            FeedItemHomepageReadModel.published_at.desc(),
            FeedItemHomepageReadModel.feed_item_id.desc(),
        )
    if sort == "source":
        return (
            func.lower(FeedItemHomepageReadModel.source_name).asc(),
            FeedItemHomepageReadModel.published_at.desc(),
            FeedItemHomepageReadModel.feed_item_id.desc(),
        )
    return (
        FeedItemHomepageReadModel.low_priority_bucket.asc(),
        FeedItemHomepageReadModel.priority_score.desc(),
        FeedItemHomepageReadModel.published_at.desc(),
        FeedItemHomepageReadModel.feed_item_id.desc(),
    )


def _homepage_read_model_conditions(
    query: HomepageQuery,
    *,
    exclude_facet: str | None,
    time_window_reference: datetime,
) -> list[object]:
    conditions: list[object] = [
        FeedItemHomepageReadModel.read_model_version == HOMEPAGE_READ_MODEL_VERSION,
        FeedItemHomepageReadModel.is_stale.is_(False),
    ]
    if exclude_facet != "source" and query.source_id is not None:
        conditions.append(FeedItemHomepageReadModel.source_id == query.source_id)
    if exclude_facet != "source_category" and query.source_category is not None:
        conditions.append(
            FeedItemHomepageReadModel.source_category_id
            == _normalize_source_category_query_value(query.source_category)
        )
    if exclude_facet != "channel" and query.channel_id:
        conditions.append(_homepage_read_model_has_facet(HOMEPAGE_READ_MODEL_FACET_CHANNEL, query.channel_id))
    if exclude_facet != "flow_lane" and query.flow_lane:
        if query.flow_lane == "low_priority":
            conditions.append(
                or_(
                    FeedItemHomepageReadModel.flow_lane == "low_priority",
                    FeedItemHomepageReadModel.low_priority_bucket.is_(True),
                )
            )
        else:
            conditions.append(FeedItemHomepageReadModel.flow_lane == query.flow_lane)
    if query.reading_depth:
        conditions.append(FeedItemHomepageReadModel.reading_depth == query.reading_depth)
    if exclude_facet != "state" and query.state:
        conditions.append(FeedItemHomepageReadModel.state == query.state)
    if query.saved is not None:
        conditions.append(FeedItemHomepageReadModel.saved.is_(query.saved))
    if query.not_interested is not None:
        conditions.append(FeedItemHomepageReadModel.not_interested.is_(query.not_interested))
    if exclude_facet != "topic" and query.topic:
        conditions.append(_homepage_read_model_has_facet(HOMEPAGE_READ_MODEL_FACET_TOPIC, query.topic))
    search_text = _normalize_search_text(query.search_text)
    if search_text:
        conditions.append(FeedItemHomepageReadModel.search_text.contains(search_text, autoescape=True))
    if query.time_window != "all":
        delta = TIME_WINDOW_DELTA.get(query.time_window)
        if delta is not None:
            conditions.append(FeedItemHomepageReadModel.time_window_at >= _normalize_utc(time_window_reference) - delta)
    return conditions


def _homepage_read_model_has_facet(facet_kind: str, facet_id: str):
    facet = aliased(FeedItemHomepageFacet)
    return exists().where(
        facet.feed_item_id == FeedItemHomepageReadModel.feed_item_id,
        facet.facet_kind == facet_kind,
        facet.facet_id == facet_id,
    )


def _list_cards_for_homepage_query(
    session: Session,
    query: HomepageQuery,
    *,
    exclude_facet: str | None = None,
    time_window_reference: datetime | None = None,
) -> list[HomepageCardRead]:
    source_id = None if exclude_facet == "source" else query.source_id
    source_category = None if exclude_facet == "source_category" else query.source_category
    cards = _build_homepage_cards(
        session,
        source_id=source_id,
        source_category=source_category,
    )
    cards = _filter_cards_by_query(
        cards,
        query,
        exclude_facet=exclude_facet,
        time_window_reference=time_window_reference,
    )
    return _sort_cards_for_query(cards, query.sort)


def _cards_for_summary_facet(
    session: Session,
    query: HomepageQuery,
    scoped_cards: list[HomepageCardRead],
    facet: str,
) -> list[HomepageCardRead]:
    if facet == "source" and query.source_id is not None:
        return _build_homepage_cards(
            session,
            source_id=None,
            source_category=query.source_category,
        )
    if facet == "source_category" and query.source_category is not None:
        return _build_homepage_cards(
            session,
            source_id=query.source_id,
            source_category=None,
        )
    return scoped_cards


def _filter_cards_by_query(
    cards: list[HomepageCardRead],
    query: HomepageQuery,
    *,
    exclude_facet: str | None,
    time_window_reference: datetime | None = None,
) -> list[HomepageCardRead]:
    filtered = cards
    if exclude_facet != "source" and query.source_id is not None:
        filtered = [card for card in filtered if card.source_id == query.source_id]
    if exclude_facet != "source_category" and query.source_category is not None:
        normalized_category = _normalize_source_category_query_value(query.source_category)
        filtered = [card for card in filtered if _source_category_id(card.source_folder_path) == normalized_category]
    if exclude_facet != "channel" and query.channel_id:
        filtered = [card for card in filtered if _card_matches_channel(card, query.channel_id)]
    if exclude_facet != "flow_lane" and query.flow_lane:
        filtered = [card for card in filtered if _card_matches_flow_lane(card, query.flow_lane)]
    if query.reading_depth:
        filtered = [card for card in filtered if card.reading_depth == query.reading_depth]
    if exclude_facet != "state" and query.state:
        filtered = [card for card in filtered if card.state.status == query.state]
    if query.saved is not None:
        filtered = [card for card in filtered if (card.state.status == "saved") == query.saved]
    if query.not_interested is not None:
        filtered = [card for card in filtered if (card.state.status == "not_interested") == query.not_interested]
    if exclude_facet != "topic" and query.topic:
        filtered = [card for card in filtered if query.topic in card.topic_labels]
    search_text = _normalize_search_text(query.search_text)
    if search_text:
        filtered = [card for card in filtered if _card_matches_search(card, search_text)]
    if query.time_window != "all":
        reference_time = time_window_reference or _now_utc()
        filtered = [card for card in filtered if _card_matches_time_window(card, query.time_window, reference_time)]
    return filtered


def _sort_cards_for_query(cards: list[HomepageCardRead], sort: str) -> list[HomepageCardRead]:
    if sort == "newest":
        return sorted(cards, key=lambda card: (-_timestamp_for_sort(card.published_at), -card.id))
    if sort == "source":
        return sorted(
            cards,
            key=lambda card: (
                card.source_name.casefold(),
                -_timestamp_for_sort(card.published_at),
                -card.id,
            ),
        )
    return sorted(
        cards,
        key=lambda card: (
            card.low_priority_bucket,
            -card.priority_score,
            -_timestamp_for_sort(card.published_at),
            -card.id,
        ),
    )


def _window_cards(
    cards: list[HomepageCardRead],
    *,
    query: HomepageQuery,
    query_signature: str,
    time_window_reference: datetime | None,
) -> tuple[list[HomepageCardRead], str | None, bool]:
    start_index = 0
    if query.cursor:
        cursor = _decode_cursor(query.cursor)
        if cursor.get("version") != HOMEPAGE_CURSOR_VERSION:
            raise ValueError("Unsupported homepage cursor version")
        if cursor.get("query_signature") != query_signature or cursor.get("sort") != query.sort:
            raise ValueError("Homepage cursor does not match the current query")
        anchor_item_id = cursor.get("anchor_item_id")
        for index, card in enumerate(cards):
            if card.id == anchor_item_id:
                start_index = index + 1
                break
        else:
            raise ValueError("Homepage cursor anchor is no longer available")

    page = cards[start_index : start_index + query.page_size]
    has_more = start_index + len(page) < len(cards)
    next_cursor = (
        _encode_cursor(
            page[-1],
            query=query,
            query_signature=query_signature,
            time_window_reference=time_window_reference,
        )
        if has_more and page
        else None
    )
    return page, next_cursor, has_more


def _encode_cursor(
    card: HomepageCardRead,
    *,
    query: HomepageQuery,
    query_signature: str,
    time_window_reference: datetime | None,
) -> str:
    return _encode_cursor_for_anchor(
        card.id,
        query=query,
        query_signature=query_signature,
        time_window_reference=time_window_reference,
        anchor_sort_tuple=_cursor_sort_tuple(card, query.sort),
    )


def _encode_cursor_for_anchor(
    anchor_item_id: int,
    *,
    query: HomepageQuery,
    query_signature: str,
    time_window_reference: datetime | None,
    anchor_sort_tuple: list[object] | None = None,
) -> str:
    payload = {
        "version": HOMEPAGE_CURSOR_VERSION,
        "query_signature": query_signature,
        "sort": query.sort,
        "anchor_item_id": anchor_item_id,
    }
    if anchor_sort_tuple is not None:
        payload["anchor_sort_tuple"] = anchor_sort_tuple
    if query.time_window != "all" and time_window_reference is not None:
        payload["time_window_reference"] = time_window_reference.isoformat()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> dict[str, object]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
        payload = json.loads(decoded)
    except Exception as exc:
        raise ValueError("Invalid homepage cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid homepage cursor")
    return payload


def _cursor_sort_tuple(card: HomepageCardRead, sort: str) -> list[object]:
    timestamp = _timestamp_for_sort(card.published_at)
    if sort == "newest":
        return [timestamp, card.id]
    if sort == "source":
        return [card.source_name, timestamp, card.id]
    return [card.low_priority_bucket, card.priority_score, timestamp, card.id]


def _time_window_reference_for_query(query: HomepageQuery, generated_at: datetime) -> datetime:
    if query.time_window == "all" or not query.cursor:
        return generated_at
    cursor = _decode_cursor(query.cursor)
    value = cursor.get("time_window_reference")
    if not isinstance(value, str):
        raise ValueError("Homepage cursor is missing the time window reference")
    try:
        return _normalize_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ValueError("Homepage cursor has an invalid time window reference") from exc


def _card_matches_time_window(card: HomepageCardRead, time_window: str, reference_time: datetime) -> bool:
    delta = TIME_WINDOW_DELTA.get(time_window)
    if delta is None:
        return True
    timestamp = _card_time_window_timestamp(card)
    return _normalize_utc(timestamp) >= _normalize_utc(reference_time) - delta


def _card_time_window_timestamp(card: HomepageCardRead) -> datetime:
    return (
        card.published_at
        or card.state.saved_at
        or card.state.read_full_at
        or card.state.seen_at
        or card.state.browsed_at
        or card.state.not_interested_at
        or card.state.updated_at
    )


def _count_channel_facets(cards: list[HomepageCardRead]) -> list[HomepageFacetCountRead]:
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for card in cards:
        channel_ids = set()
        if card.primary_channel_id:
            channel_ids.add(card.primary_channel_id)
        channel_ids.update(secondary.channel_id for secondary in card.secondary_channels)
        for channel_id in channel_ids:
            counter[channel_id] += 1
            labels.setdefault(channel_id, channel_id)
    return _facet_counts_from_counter(counter, labels)


def _count_source_facets(cards: list[HomepageCardRead]) -> list[HomepageFacetCountRead]:
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for card in cards:
        source_key = str(card.source_id)
        counter[source_key] += 1
        labels[source_key] = card.source_name
    return _facet_counts_from_counter(counter, labels)


def _count_source_category_facets(cards: list[HomepageCardRead]) -> list[HomepageFacetCountRead]:
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for card in cards:
        category_id = _source_category_id(card.source_folder_path)
        counter[category_id] += 1
        labels[category_id] = EMPTY_SOURCE_CATEGORY_LABEL if category_id == HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID else category_id
    return _facet_counts_from_counter(counter, labels)


def _count_flow_lane_facets(cards: list[HomepageCardRead]) -> list[HomepageFacetCountRead]:
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for card in cards:
        flow_lane = "low_priority" if card.low_priority_bucket else card.flow_lane
        counter[flow_lane] += 1
        labels[flow_lane] = flow_lane
    return _facet_counts_from_counter(counter, labels)


def _count_state_facets(cards: list[HomepageCardRead]) -> list[HomepageFacetCountRead]:
    counter = Counter(card.state.status for card in cards)
    return _facet_counts_from_counter(counter, {state: state for state in counter})


def _count_text_facets(
    cards: list[HomepageCardRead],
    values_for_card,
) -> list[HomepageFacetCountRead]:
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for card in cards:
        for value in set(values_for_card(card)):
            if value:
                counter[value] += 1
                labels[value] = value
    return _facet_counts_from_counter(counter, labels)


def _facet_counts_from_counter(counter: Counter[str], labels: dict[str, str]) -> list[HomepageFacetCountRead]:
    return [
        HomepageFacetCountRead(id=facet_id, label=labels.get(facet_id, facet_id), count=count)
        for facet_id, count in sorted(counter.items(), key=lambda item: (-item[1], labels.get(item[0], item[0])))
    ]


def _filter_cards_by_homepage_scope(
    cards: list[HomepageCardRead],
    *,
    channel_id: str | None,
    flow_lane: str | None,
    reading_depth: str | None,
) -> list[HomepageCardRead]:
    filtered = cards
    if channel_id:
        filtered = [card for card in filtered if _card_matches_channel(card, channel_id)]
    if flow_lane:
        filtered = [card for card in filtered if _card_matches_flow_lane(card, flow_lane)]
    if reading_depth:
        filtered = [card for card in filtered if card.reading_depth == reading_depth]
    return filtered


def _card_matches_channel(card: HomepageCardRead, channel_id: str) -> bool:
    if card.primary_channel_id == channel_id:
        return True
    return any(secondary.channel_id == channel_id for secondary in card.secondary_channels)


def _card_matches_flow_lane(card: HomepageCardRead, flow_lane: str) -> bool:
    if flow_lane == "low_priority":
        return card.flow_lane == flow_lane or card.low_priority_bucket
    return card.flow_lane == flow_lane


def _card_matches_search(card: HomepageCardRead, search_text: str) -> bool:
    haystack = " ".join(
        part
        for part in [
            card.title,
            card.summary_text,
            card.source_name,
            card.source_folder_path,
            " ".join(card.topic_labels),
        ]
        if part
    ).casefold()
    return search_text in haystack


def _normalize_search_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.strip().casefold().split())
    return normalized or None


def _source_category_id(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID


def _normalize_source_category_query_value(
    value: str | None,
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized == HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID:
        return HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID
    return normalized


def _build_homepage_card(
    feed_item: FeedItem,
    item_state: ItemState,
    occurrence: SourceItemOccurrence,
    source: Source,
    artifacts_by_kind: dict[str, InsightEngineArtifact] | None,
    scoring_signals: ArticleScoringSignals,
    scoring_context: InsightScoringContext,
    structured_feedback: FeedItemStructuredFeedback | None,
    channels_by_id: dict[str, InsightChannel],
    source_tendencies: list[SourceChannelTendency],
) -> HomepageCardRead:
    published_at = feed_item.published_at or occurrence.published_at or occurrence.created_at
    extraction_diagnostics = read_content_extraction_diagnostics(feed_item)
    resolved = resolve_feed_item_insight_artifacts(
        feed_item=feed_item,
        item_state=item_state,
        occurrence=occurrence,
        source=source,
        artifacts_by_kind=artifacts_by_kind,
    )
    priority = score_feed_item_priority(
        feed_item_id=feed_item.id,
        item_status=item_state.status,
        source_id=source.id,
        signals=scoring_signals,
        context=scoring_context,
        feed_item=feed_item,
        item_state=item_state,
        occurrence=occurrence,
        source=source,
    )
    routing = resolve_routing(
        feed_item=feed_item,
        source=source,
        artifacts_by_kind=artifacts_by_kind,
        channels_by_id=channels_by_id,
        source_tendencies=source_tendencies,
    )
    routing = apply_structured_channel_feedback_to_routing(
        routing,
        structured_feedback,
        allowed_channel_ids={channel_id for channel_id, channel in channels_by_id.items() if channel_is_active(channel)},
    )

    return HomepageCardRead(
        id=feed_item.id,
        title=feed_item.title,
        author_name=feed_item.author_name,
        published_at=published_at,
        summary_text=resolved.summary_text,
        original_url=occurrence.original_url,
        canonical_url=feed_item.canonical_url,
        source_id=source.id,
        source_name=source.name,
        source_kind=source.kind,
        source_url=source.url,
        source_site_url=source.site_url,
        source_folder_path=source.folder_path,
        content_text_source=feed_item.content_text_source,
        content_extraction_status=feed_item.content_extraction_status,
        content_extraction_attempted_at=feed_item.content_extraction_attempted_at,
        content_extracted_at=feed_item.content_extracted_at,
        content_extraction_error=feed_item.content_extraction_error,
        content_extraction_method=extraction_diagnostics.method,
        content_extraction_final_url=extraction_diagnostics.final_url,
        content_extraction_visible_char_count=extraction_diagnostics.visible_char_count,
        insight_feedback_text=feed_item.insight_feedback_text,
        structured_feedback=serialize_structured_feedback(structured_feedback),
        state=build_reader_item_state(item_state),
        topic_labels=resolved.topic_labels,
        priority_score=priority.score,
        priority_band=priority.band,
        priority_reasons=[
            HomepagePriorityReasonRead(code=reason.code, label=reason.label, weight=reason.weight)
            for reason in priority.reasons
        ],
        low_priority_bucket=priority.low_priority_bucket,
        low_priority_bucket_reason=priority.low_priority_bucket_reason,
        auto_low_priority_cleared=priority.auto_low_priority_cleared,
        summary_source=resolved.summary_source,
        topic_source=resolved.topic_source,
        priority_source=priority.source,
        artifact_generated_at=resolved.artifact_generated_at,
        primary_channel_id=routing.primary_channel_id,
        secondary_channels=[
            HomepageSecondaryChannelRead(
                channel_id=secondary.channel_id,
                confidence=secondary.confidence,
                display_mode=secondary.display_mode,
            )
            for secondary in routing.secondary_channels
        ],
        reading_depth=routing.reading_depth,
        flow_lane=routing.flow_lane,
        routing_confidence=routing.confidence,
        routing_source=routing.source,
        routing_reason_codes=routing.reason_codes,
        promotion_reason_codes=routing.promotion_reason_codes,
        isolation_reason_codes=routing.isolation_reason_codes,
        source_policy_applied=routing.source_policy_applied,
        routing_notes=routing.notes,
    )


def _load_channels_by_id(session: Session) -> dict[str, InsightChannel]:
    ensure_default_channels(session)
    channels = session.scalars(select(InsightChannel)).all()
    return {channel.id: channel for channel in channels}


def _load_source_tendencies_by_source_id(
    session: Session,
    *,
    source_ids: list[int],
) -> dict[int, list[SourceChannelTendency]]:
    unique_source_ids = list(dict.fromkeys(source_ids))
    if not unique_source_ids:
        return {}

    statement = select(SourceChannelTendency).where(SourceChannelTendency.source_id.in_(unique_source_ids))
    tendencies_by_source_id: dict[int, list[SourceChannelTendency]] = {}
    for tendency in session.scalars(statement):
        tendencies_by_source_id.setdefault(tendency.source_id, []).append(tendency)
    return tendencies_by_source_id


def _timestamp_for_sort(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    return _normalize_utc(value).timestamp()


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
