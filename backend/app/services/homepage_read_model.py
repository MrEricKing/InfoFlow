from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    FeedItem,
    FeedItemHomepageFacet,
    FeedItemHomepageReadModel,
    FeedItemStructuredFeedback,
    InsightEngineArtifact,
    ItemState,
    SourceItemOccurrence,
)
from app.db.session import get_session_factory
from app.schemas.homepage import (
    HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID,
    HomepageCardRead,
    HomepageReadModelStatusRead,
)
from app.services.insight_engine_artifacts import build_latest_occurrence_subquery

logger = logging.getLogger(__name__)

HOMEPAGE_READ_MODEL_VERSION = "homepage_read_model_v1"
HOMEPAGE_READ_MODEL_FACET_CHANNEL = "channel"
HOMEPAGE_READ_MODEL_FACET_TOPIC = "topic"
HOMEPAGE_READ_MODEL_BACKFILL_BATCH_SIZE = 100
HOMEPAGE_READ_MODEL_MAX_SYNC_REBUILD_ITEMS = 500
HOMEPAGE_READ_MODEL_LOCK_NAMESPACE = "infoflow_homepage_read_model"


@dataclass(frozen=True, slots=True)
class HomepageReadModelHealth:
    missing_count: int
    stale_count: int
    rebuilding: bool = False

    @property
    def is_fresh(self) -> bool:
        return self.missing_count == 0 and self.stale_count == 0


_backfill_lock = threading.Lock()
_backfill_thread: threading.Thread | None = None


def get_homepage_read_model_health(session: Session) -> HomepageReadModelHealth:
    eligible_items = _eligible_homepage_item_ids_subquery()
    missing_count = session.scalar(
        select(func.count())
        .select_from(eligible_items)
        .outerjoin(
            FeedItemHomepageReadModel,
            FeedItemHomepageReadModel.feed_item_id == eligible_items.c.feed_item_id,
        )
        .where(FeedItemHomepageReadModel.feed_item_id.is_(None))
    ) or 0
    stale_count = session.scalar(
        select(func.count())
        .select_from(eligible_items)
        .join(
            FeedItemHomepageReadModel,
            FeedItemHomepageReadModel.feed_item_id == eligible_items.c.feed_item_id,
        )
        .join(FeedItem, FeedItem.id == eligible_items.c.feed_item_id)
        .join(ItemState, ItemState.feed_item_id == eligible_items.c.feed_item_id)
        .where(
            or_(
                FeedItemHomepageReadModel.is_stale.is_(True),
                FeedItemHomepageReadModel.read_model_version != HOMEPAGE_READ_MODEL_VERSION,
                FeedItem.updated_at > FeedItemHomepageReadModel.rebuilt_at,
                ItemState.updated_at > FeedItemHomepageReadModel.rebuilt_at,
                _source_occurrence_newer_than_read_model_exists(),
                _structured_feedback_newer_than_read_model_exists(),
                _insight_artifact_newer_than_read_model_exists(),
            )
        )
    ) or 0
    return HomepageReadModelHealth(
        missing_count=int(missing_count),
        stale_count=int(stale_count),
        rebuilding=homepage_read_model_backfill_is_running(),
    )


def build_homepage_read_model_status(
    health: HomepageReadModelHealth,
    *,
    fallback_live: bool,
) -> HomepageReadModelStatusRead:
    if health.is_fresh and not fallback_live:
        return HomepageReadModelStatusRead(state="fresh", missing_count=0, stale_count=0)
    if fallback_live:
        return HomepageReadModelStatusRead(
            state="fallback_live",
            missing_count=health.missing_count,
            stale_count=health.stale_count,
            message="首页 read model 正在补建，本次使用实时兜底；补建完成后刷新会更快。",
        )
    return HomepageReadModelStatusRead(
        state="rebuilding",
        missing_count=health.missing_count,
        stale_count=health.stale_count,
        message="首页 read model 正在后台补建。",
    )


def homepage_read_model_backfill_is_running() -> bool:
    with _backfill_lock:
        return _backfill_thread is not None and _backfill_thread.is_alive()


def ensure_homepage_read_model_backfill_started(reason: str = "unknown") -> bool:
    global _backfill_thread
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        return False
    with _backfill_lock:
        if _backfill_thread is not None and _backfill_thread.is_alive():
            return False
        _backfill_thread = threading.Thread(
            target=_run_homepage_read_model_backfill,
            kwargs={"reason": reason},
            name="homepage-read-model-backfill",
            daemon=True,
        )
        _backfill_thread.start()
        return True


def rebuild_homepage_read_models_for_feed_items(session: Session, feed_item_ids: Sequence[int]) -> int:
    unique_ids = _unique_ints(feed_item_ids)
    if not unique_ids:
        return 0

    _acquire_homepage_read_model_database_locks(session, unique_ids)
    rebuild_started_at = _now_utc()
    from app.services.homepage import _build_homepage_cards

    cards = _build_homepage_cards(session, feed_item_ids=unique_ids)
    card_by_id = {card.id: card for card in cards}
    facet_delete = session.execute(
        delete(FeedItemHomepageFacet).where(FeedItemHomepageFacet.feed_item_id.in_(unique_ids))
    )
    changed = bool(facet_delete.rowcount and facet_delete.rowcount > 0)

    rebuilt_count = 0
    for feed_item_id in unique_ids:
        card = card_by_id.get(feed_item_id)
        if card is None:
            row_delete = session.execute(
                delete(FeedItemHomepageReadModel).where(FeedItemHomepageReadModel.feed_item_id == feed_item_id)
            )
            if row_delete.rowcount and row_delete.rowcount > 0:
                changed = True
            continue
        row = session.get(FeedItemHomepageReadModel, feed_item_id)
        if row is None:
            row = FeedItemHomepageReadModel(feed_item_id=feed_item_id)
        _apply_card_to_read_model(row, card, rebuilt_at=rebuild_started_at, updated_at=_now_utc())
        session.add(row)
        session.flush()
        for facet in _build_facets_for_card(card):
            session.add(facet)
        rebuilt_count += 1
        changed = True

    session.flush()
    if changed:
        _invalidate_homepage_snapshots()
    return rebuilt_count


def rebuild_homepage_read_models_for_source(session: Session, source_id: int) -> int:
    if _session_uses_sqlite(session):
        return 0
    feed_item_ids = list(
        session.scalars(
            select(SourceItemOccurrence.feed_item_id)
            .where(SourceItemOccurrence.source_id == source_id)
            .distinct()
            .limit(HOMEPAGE_READ_MODEL_MAX_SYNC_REBUILD_ITEMS + 1)
        )
    )
    if len(feed_item_ids) > HOMEPAGE_READ_MODEL_MAX_SYNC_REBUILD_ITEMS:
        mark_homepage_read_models_stale(session, source_ids=[source_id], reason="source_sync")
        return 0
    return rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)


def mark_homepage_read_models_stale(
    session: Session,
    *,
    feed_item_ids: Sequence[int] | None = None,
    source_ids: Sequence[int] | None = None,
    reason: str,
) -> int:
    statement = (
        update(FeedItemHomepageReadModel)
        .values(is_stale=True, stale_reason=reason[:128], updated_at=_now_utc())
    )
    if feed_item_ids is not None:
        ids = _unique_ints(feed_item_ids)
        if not ids:
            return 0
        statement = statement.where(FeedItemHomepageReadModel.feed_item_id.in_(ids))
    if source_ids is not None:
        ids = _unique_ints(source_ids)
        if not ids:
            return 0
        statement = statement.where(FeedItemHomepageReadModel.source_id.in_(ids))
    result = session.execute(statement)
    rowcount = int(result.rowcount or 0)
    if rowcount > 0:
        _invalidate_homepage_snapshots()
    return rowcount


def _run_homepage_read_model_backfill(*, reason: str) -> None:
    logger.info("homepage_read_model_backfill_start reason=%s", reason)
    session_factory = get_session_factory()
    try:
        while True:
            with session_factory() as session:
                feed_item_ids = _list_missing_or_stale_feed_item_ids(
                    session,
                    limit=HOMEPAGE_READ_MODEL_BACKFILL_BATCH_SIZE,
                )
                if not feed_item_ids:
                    break
                rebuild_homepage_read_models_for_feed_items(session, feed_item_ids)
                session.commit()
    except Exception:
        logger.exception("homepage_read_model_backfill_failed reason=%s", reason)
    finally:
        logger.info("homepage_read_model_backfill_stop reason=%s", reason)


def _list_missing_or_stale_feed_item_ids(session: Session, *, limit: int) -> list[int]:
    eligible_items = _eligible_homepage_item_ids_subquery()
    statement = (
        select(eligible_items.c.feed_item_id)
        .select_from(eligible_items)
        .outerjoin(
            FeedItemHomepageReadModel,
            FeedItemHomepageReadModel.feed_item_id == eligible_items.c.feed_item_id,
        )
        .join(FeedItem, FeedItem.id == eligible_items.c.feed_item_id)
        .join(ItemState, ItemState.feed_item_id == eligible_items.c.feed_item_id)
        .where(
            or_(
                FeedItemHomepageReadModel.feed_item_id.is_(None),
                FeedItemHomepageReadModel.is_stale.is_(True),
                FeedItemHomepageReadModel.read_model_version != HOMEPAGE_READ_MODEL_VERSION,
                FeedItem.updated_at > FeedItemHomepageReadModel.rebuilt_at,
                ItemState.updated_at > FeedItemHomepageReadModel.rebuilt_at,
                _source_occurrence_newer_than_read_model_exists(),
                _structured_feedback_newer_than_read_model_exists(),
                _insight_artifact_newer_than_read_model_exists(),
            )
        )
        .order_by(eligible_items.c.feed_item_id.asc())
        .limit(limit)
    )
    return [int(feed_item_id) for feed_item_id in session.scalars(statement)]


def _eligible_homepage_item_ids_subquery():
    latest_occurrences = build_latest_occurrence_subquery()
    return (
        select(FeedItem.id.label("feed_item_id"))
        .join(ItemState, ItemState.feed_item_id == FeedItem.id)
        .join(
            latest_occurrences,
            (latest_occurrences.c.feed_item_id == FeedItem.id)
            & (latest_occurrences.c.occurrence_rank == 1),
        )
        .subquery()
    )


def _structured_feedback_newer_than_read_model_exists():
    return exists().where(
        FeedItemStructuredFeedback.feed_item_id == FeedItemHomepageReadModel.feed_item_id,
        FeedItemStructuredFeedback.updated_at > FeedItemHomepageReadModel.rebuilt_at,
    )


def _source_occurrence_newer_than_read_model_exists():
    return exists().where(
        SourceItemOccurrence.feed_item_id == FeedItemHomepageReadModel.feed_item_id,
        SourceItemOccurrence.created_at > FeedItemHomepageReadModel.rebuilt_at,
    )


def _insight_artifact_newer_than_read_model_exists():
    return exists().where(
        InsightEngineArtifact.feed_item_id == FeedItemHomepageReadModel.feed_item_id,
        InsightEngineArtifact.created_at > FeedItemHomepageReadModel.rebuilt_at,
    )


def _apply_card_to_read_model(
    row: FeedItemHomepageReadModel,
    card: HomepageCardRead,
    *,
    rebuilt_at: datetime,
    updated_at: datetime,
) -> None:
    source_category_id = _source_category_id(card.source_folder_path)
    row.card_json = card.model_dump(mode="json")
    row.read_model_version = HOMEPAGE_READ_MODEL_VERSION
    row.source_id = card.source_id
    row.source_name = card.source_name
    row.source_category_id = source_category_id
    row.source_category_label = (
        "未分类" if source_category_id == HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID else source_category_id
    )
    row.state = card.state.status
    row.saved = card.state.status == "saved"
    row.not_interested = card.state.status == "not_interested"
    row.published_at = card.published_at
    row.time_window_at = _card_time_window_timestamp(card)
    row.priority_score = card.priority_score
    row.low_priority_bucket = card.low_priority_bucket
    row.flow_lane = card.flow_lane
    row.flow_lane_facet = "low_priority" if card.low_priority_bucket else card.flow_lane
    row.reading_depth = card.reading_depth
    row.search_text = _search_text_for_card(card)
    row.is_stale = False
    row.stale_reason = None
    row.rebuilt_at = rebuilt_at
    row.updated_at = updated_at


def _build_facets_for_card(card: HomepageCardRead) -> list[FeedItemHomepageFacet]:
    facets: list[FeedItemHomepageFacet] = []
    channel_ids = set()
    if card.primary_channel_id:
        channel_ids.add(card.primary_channel_id)
    channel_ids.update(secondary.channel_id for secondary in card.secondary_channels)
    for channel_id in sorted(channel_ids):
        facets.append(
            FeedItemHomepageFacet(
                feed_item_id=card.id,
                facet_kind=HOMEPAGE_READ_MODEL_FACET_CHANNEL,
                facet_id=channel_id,
                facet_label=channel_id,
            )
        )
    for topic in sorted({topic for topic in card.topic_labels if topic}):
        facets.append(
            FeedItemHomepageFacet(
                feed_item_id=card.id,
                facet_kind=HOMEPAGE_READ_MODEL_FACET_TOPIC,
                facet_id=topic,
                facet_label=topic,
            )
        )
    return facets


def _search_text_for_card(card: HomepageCardRead) -> str:
    parts = [
        card.title,
        card.summary_text,
        card.source_name,
        card.source_folder_path,
        " ".join(card.topic_labels),
    ]
    return " ".join(part for part in parts if part).casefold()


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


def _source_category_id(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID


def _unique_ints(values: Sequence[int]) -> list[int]:
    return sorted({int(value) for value in values if value is not None})


def _acquire_homepage_read_model_database_locks(session: Session, feed_item_ids: Sequence[int]) -> None:
    if _session_uses_sqlite(session):
        return
    if session.get_bind().dialect.name != "postgresql":
        return
    for feed_item_id in _unique_ints(feed_item_ids):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_namespace), CAST(:lock_id AS integer))"),
            {
                "lock_namespace": HOMEPAGE_READ_MODEL_LOCK_NAMESPACE,
                "lock_id": int(feed_item_id),
            },
        )


def _invalidate_homepage_snapshots() -> None:
    from app.services.homepage import invalidate_homepage_snapshots

    invalidate_homepage_snapshots()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _session_uses_sqlite(session: Session) -> bool:
    bind = session.get_bind()
    return bind.dialect.name == "sqlite"
