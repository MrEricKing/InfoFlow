from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.db.models import (
    AppOperation,
    FeedItem,
    FeedItemBehaviorEvent,
    FeedItemHomepageFacet,
    FeedItemHomepageReadModel,
    FeedItemStructuredFeedback,
    InsightEngineArtifact,
    InsightEngineAttempt,
    ItemState,
    SavedItem,
    Source,
    SourceChannelTendency,
    SourceFetch,
    SourceItemOccurrence,
    SourcePreferenceProfile,
)
from app.schemas.source import (
    SourceDeleteBlockerRead,
    SourceDeletePlanRead,
    SourceDeletePreviewCounts,
    SourceDeletePreviewRead,
    SourceDeleteResult,
    SourceDeleteSourceRead,
)
from app.services.homepage import invalidate_homepage_snapshots
from app.services.operations import (
    OPERATION_TYPE_FEED_ITEM_EXTRACT,
    OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT,
    _acquire_operation_mutex_lock,
)

ACTIVE_OPERATION_STATUSES = {"queued", "running", "stopping"}
BLOCKING_GLOBAL_OPERATION_TYPES = {
    "source_sync",
    "feed_item_insight_artifacts",
    "channel_lifecycle",
}
BLOCKING_FEED_ITEM_OPERATION_TYPES = {
    "feed_item_extract",
    "feed_item_insight_artifact",
}


class SourceDeletionNotFoundError(ValueError):
    pass


class SourceDeletionValidationError(ValueError):
    pass


class SourceDeletionConflictError(RuntimeError):
    def __init__(self, message: str, preview: SourceDeletePreviewRead) -> None:
        self.preview = preview
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _SourceDeleteCandidateSet:
    source_feed_item_ids: list[int]
    exclusive_unsaved_feed_item_ids: list[int]
    shared_feed_item_ids: list[int]
    saved_feed_item_ids: list[int]
    saved_item_ids: list[int]
    active_saved_item_ids: list[int]
    removed_saved_item_ids: list[int]
    source_fetch_ids: list[int]
    source_item_occurrence_ids: list[int]
    source_preference_profile_ids: list[int]
    source_channel_tendency_ids: list[int]
    homepage_read_model_feed_item_ids: list[int]
    feed_item_insight_artifact_ids: list[int]
    diagnostic_operation_ids: list[int]
    diagnostic_attempt_ids: list[int]


def build_source_delete_preview(session: Session, source_id: int) -> SourceDeletePreviewRead:
    source = session.get(Source, source_id)
    if source is None:
        raise SourceDeletionNotFoundError("Source not found")

    candidates = _build_candidate_set(session, source_id)
    return _build_preview_for_candidates(session, source, candidates)


def _build_preview_for_candidates(
    session: Session,
    source: Source,
    candidates: _SourceDeleteCandidateSet,
) -> SourceDeletePreviewRead:
    blockers = _list_delete_blockers(session, source_id=source.id, source_feed_item_ids=candidates.source_feed_item_ids)
    counts = _build_counts(candidates)
    return SourceDeletePreviewRead(
        source=_build_source_summary(source, item_count=len(candidates.source_feed_item_ids)),
        can_delete=not blockers,
        blockers=blockers,
        counts=counts,
        preview_token=_build_preview_token(source, candidates, counts),
        delete_plan=_build_delete_plan(counts),
    )


def delete_source_with_preview_token(
    session: Session,
    source_id: int,
    *,
    confirm: bool,
    preview_token: str,
) -> SourceDeleteResult:
    if not confirm:
        raise SourceDeletionValidationError("Source deletion requires confirm=true.")
    if not preview_token.strip():
        raise SourceDeletionValidationError("Source deletion requires a preview_token.")

    source = session.get(Source, source_id)
    if source is None:
        raise SourceDeletionNotFoundError("Source not found")
    _acquire_operation_mutex_lock(session, "global")
    source_feed_item_ids = _list_source_feed_item_ids(session, source_id)
    _acquire_source_feed_item_mutexes(session, source_feed_item_ids)

    candidates = _build_candidate_set(session, source_id, source_feed_item_ids=source_feed_item_ids)
    preview = _build_preview_for_candidates(session, source, candidates)
    if preview.blockers:
        raise SourceDeletionConflictError("Source cannot be deleted while related operations are active.", preview)
    if preview.preview_token != preview_token:
        raise SourceDeletionConflictError("Source delete preview changed; refresh the preview and retry.", preview)

    _detach_diagnostic_rows(session, source_id, candidates.exclusive_unsaved_feed_item_ids)
    _delete_feed_item_owned_rows(session, candidates.exclusive_unsaved_feed_item_ids)
    _delete_homepage_read_model_rows(session, candidates.homepage_read_model_feed_item_ids)
    session.execute(delete(SourceItemOccurrence).where(SourceItemOccurrence.source_id == source_id))
    session.execute(delete(SourceChannelTendency).where(SourceChannelTendency.source_id == source_id))
    session.execute(delete(SourcePreferenceProfile).where(SourcePreferenceProfile.source_id == source_id))
    session.execute(delete(SourceFetch).where(SourceFetch.source_id == source_id))
    session.delete(source)
    session.flush()
    invalidate_homepage_snapshots()

    return SourceDeleteResult(
        source=preview.source,
        deleted=True,
        counts=preview.counts,
        retained_saved_items=preview.counts.saved_items_retained,
    )


def _acquire_source_feed_item_mutexes(session: Session, feed_item_ids: Sequence[int]) -> None:
    for feed_item_id in sorted({int(next_id) for next_id in feed_item_ids}):
        _acquire_operation_mutex_lock(session, f"{OPERATION_TYPE_FEED_ITEM_EXTRACT}:feed_item:{feed_item_id}")
        _acquire_operation_mutex_lock(session, f"{OPERATION_TYPE_FEED_ITEM_INSIGHT_ARTIFACT}:feed_item:{feed_item_id}")


def _build_candidate_set(
    session: Session,
    source_id: int,
    *,
    source_feed_item_ids: Sequence[int] | None = None,
) -> _SourceDeleteCandidateSet:
    source_feed_item_ids = (
        _list_source_feed_item_ids(session, source_id)
        if source_feed_item_ids is None
        else sorted({int(next_id) for next_id in source_feed_item_ids})
    )
    exclusive_unsaved_feed_item_ids = _list_exclusive_unsaved_feed_item_ids(session, source_id)
    shared_feed_item_ids = _list_shared_feed_item_ids(session, source_id)
    saved_feed_item_ids = _list_saved_feed_item_ids(session, source_id)
    saved_item_ids = _list_saved_item_ids(session, source_id)
    active_saved_item_ids = _list_saved_item_ids(session, source_id, active=True)
    removed_saved_item_ids = _list_saved_item_ids(session, source_id, removed=True)
    homepage_read_model_feed_item_ids = _list_homepage_read_model_feed_item_ids(
        session,
        source_id=source_id,
        exclusive_unsaved_feed_item_ids=exclusive_unsaved_feed_item_ids,
    )
    feed_item_insight_artifact_ids = _list_ids_for_feed_items(
        session,
        InsightEngineArtifact.id,
        InsightEngineArtifact.feed_item_id,
        exclusive_unsaved_feed_item_ids,
    )
    diagnostic_operation_ids = list(
        session.scalars(select(AppOperation.id).where(AppOperation.source_id == source_id).order_by(AppOperation.id))
    )
    diagnostic_attempt_ids = _list_diagnostic_attempt_ids(session, source_id, exclusive_unsaved_feed_item_ids)
    return _SourceDeleteCandidateSet(
        source_feed_item_ids=source_feed_item_ids,
        exclusive_unsaved_feed_item_ids=exclusive_unsaved_feed_item_ids,
        shared_feed_item_ids=shared_feed_item_ids,
        saved_feed_item_ids=saved_feed_item_ids,
        saved_item_ids=saved_item_ids,
        active_saved_item_ids=active_saved_item_ids,
        removed_saved_item_ids=removed_saved_item_ids,
        source_fetch_ids=list(
            session.scalars(select(SourceFetch.id).where(SourceFetch.source_id == source_id).order_by(SourceFetch.id))
        ),
        source_item_occurrence_ids=list(
            session.scalars(
                select(SourceItemOccurrence.id).where(SourceItemOccurrence.source_id == source_id).order_by(SourceItemOccurrence.id)
            )
        ),
        source_preference_profile_ids=list(
            session.scalars(
                select(SourcePreferenceProfile.id)
                .where(SourcePreferenceProfile.source_id == source_id)
                .order_by(SourcePreferenceProfile.id)
            )
        ),
        source_channel_tendency_ids=list(
            session.scalars(
                select(SourceChannelTendency.id)
                .where(SourceChannelTendency.source_id == source_id)
                .order_by(SourceChannelTendency.id)
            )
        ),
        homepage_read_model_feed_item_ids=homepage_read_model_feed_item_ids,
        feed_item_insight_artifact_ids=feed_item_insight_artifact_ids,
        diagnostic_operation_ids=diagnostic_operation_ids,
        diagnostic_attempt_ids=diagnostic_attempt_ids,
    )


def _source_feed_items_subquery(source_id: int):
    return (
        select(SourceItemOccurrence.feed_item_id.label("feed_item_id"))
        .where(SourceItemOccurrence.source_id == source_id)
        .distinct()
        .subquery()
    )


def _list_source_feed_item_ids(session: Session, source_id: int) -> list[int]:
    rows = _source_feed_items_subquery(source_id)
    return list(session.scalars(select(rows.c.feed_item_id).order_by(rows.c.feed_item_id)))


def _list_exclusive_unsaved_feed_item_ids(session: Session, source_id: int) -> list[int]:
    rows = _source_feed_items_subquery(source_id)
    other_occurrence = aliased(SourceItemOccurrence)
    other_occurrence_exists = exists(
        select(1)
        .select_from(other_occurrence)
        .where(
            other_occurrence.feed_item_id == rows.c.feed_item_id,
            other_occurrence.source_id != source_id,
        )
    )
    saved_item_exists = exists(select(1).select_from(SavedItem).where(SavedItem.feed_item_id == rows.c.feed_item_id))
    return list(
        session.scalars(
            select(rows.c.feed_item_id)
            .where(~other_occurrence_exists, ~saved_item_exists)
            .order_by(rows.c.feed_item_id)
        )
    )


def _list_shared_feed_item_ids(session: Session, source_id: int) -> list[int]:
    rows = _source_feed_items_subquery(source_id)
    other_occurrence = aliased(SourceItemOccurrence)
    return list(
        session.scalars(
            select(rows.c.feed_item_id)
            .where(
                exists(
                    select(1)
                    .select_from(other_occurrence)
                    .where(
                        other_occurrence.feed_item_id == rows.c.feed_item_id,
                        other_occurrence.source_id != source_id,
                    )
                )
            )
            .order_by(rows.c.feed_item_id)
        )
    )


def _list_saved_feed_item_ids(session: Session, source_id: int) -> list[int]:
    rows = _source_feed_items_subquery(source_id)
    return list(
        session.scalars(
            select(rows.c.feed_item_id)
            .where(exists(select(1).select_from(SavedItem).where(SavedItem.feed_item_id == rows.c.feed_item_id)))
            .order_by(rows.c.feed_item_id)
        )
    )


def _list_saved_item_ids(
    session: Session,
    source_id: int,
    *,
    active: bool = False,
    removed: bool = False,
) -> list[int]:
    rows = _source_feed_items_subquery(source_id)
    statement = select(SavedItem.id).join(rows, SavedItem.feed_item_id == rows.c.feed_item_id)
    if active:
        statement = statement.where(SavedItem.removed_at.is_(None))
    if removed:
        statement = statement.where(SavedItem.removed_at.is_not(None))
    return list(session.scalars(statement.order_by(SavedItem.id)))


def _list_homepage_read_model_feed_item_ids(
    session: Session,
    *,
    source_id: int,
    exclusive_unsaved_feed_item_ids: Sequence[int],
) -> list[int]:
    ids = set(
        session.scalars(
            select(FeedItemHomepageReadModel.feed_item_id).where(FeedItemHomepageReadModel.source_id == source_id)
        )
    )
    ids.update(
        _list_ids_for_feed_items(
            session,
            FeedItemHomepageReadModel.feed_item_id,
            FeedItemHomepageReadModel.feed_item_id,
            exclusive_unsaved_feed_item_ids,
        )
    )
    return sorted(ids)


def _list_diagnostic_attempt_ids(
    session: Session,
    source_id: int,
    exclusive_unsaved_feed_item_ids: Sequence[int],
) -> list[int]:
    conditions = [InsightEngineAttempt.source_id == source_id]
    if exclusive_unsaved_feed_item_ids:
        conditions.append(InsightEngineAttempt.feed_item_id.in_(list(exclusive_unsaved_feed_item_ids)))
    return list(session.scalars(select(InsightEngineAttempt.id).where(or_(*conditions)).order_by(InsightEngineAttempt.id)))


def _list_ids_for_feed_items(session: Session, id_column: Any, feed_item_column: Any, feed_item_ids: Sequence[int]) -> list[int]:
    if not feed_item_ids:
        return []
    ids: list[int] = []
    for chunk in _chunks(feed_item_ids):
        ids.extend(session.scalars(select(id_column).where(feed_item_column.in_(chunk)).order_by(id_column)))
    return ids


def _build_counts(candidates: _SourceDeleteCandidateSet) -> SourceDeletePreviewCounts:
    return SourceDeletePreviewCounts(
        source_fetches=len(candidates.source_fetch_ids),
        source_item_occurrences=len(candidates.source_item_occurrence_ids),
        exclusive_unsaved_feed_items=len(candidates.exclusive_unsaved_feed_item_ids),
        retained_feed_items=len(candidates.source_feed_item_ids) - len(candidates.exclusive_unsaved_feed_item_ids),
        shared_feed_items_retained=len(candidates.shared_feed_item_ids),
        saved_items_retained=len(candidates.saved_item_ids),
        active_saved_items_retained=len(candidates.active_saved_item_ids),
        removed_saved_items_retained=len(candidates.removed_saved_item_ids),
        source_preference_profiles=len(candidates.source_preference_profile_ids),
        source_channel_tendencies=len(candidates.source_channel_tendency_ids),
        homepage_read_models=len(candidates.homepage_read_model_feed_item_ids),
        feed_item_insight_artifacts=len(candidates.feed_item_insight_artifact_ids),
        diagnostic_operations_to_detach=len(candidates.diagnostic_operation_ids),
        diagnostic_attempts_to_detach=len(candidates.diagnostic_attempt_ids),
    )


def _build_source_summary(source: Source, *, item_count: int) -> SourceDeleteSourceRead:
    return SourceDeleteSourceRead(
        id=source.id,
        name=source.name,
        kind=source.kind,
        url=source.url,
        site_url=source.site_url,
        folder_path=source.folder_path,
        is_active=source.is_active,
        ai_route=source.ai_route,
        item_count=item_count,
    )


def _build_delete_plan(counts: SourceDeletePreviewCounts) -> SourceDeletePlanRead:
    return SourceDeletePlanRead(
        deletes=[
            "来源记录",
            f"{counts.source_fetches} 行来源刷新原始响应",
            f"{counts.source_item_occurrences} 行来源条目关联",
            f"{counts.exclusive_unsaved_feed_items} 条未保存且仅属于该来源的条目及其派生数据",
            f"{counts.source_preference_profiles} 行来源画像历史",
            f"{counts.source_channel_tendencies} 行来源路由倾向及来源流策略",
        ],
        retains=[
            f"{counts.saved_items_retained} 条知识库已保存记录",
            f"{counts.shared_feed_items_retained} 条同时属于其他来源的条目",
            "历史操作/job JSON 诊断记录作为审计记录保留",
        ],
        detaches=[
            f"{counts.diagnostic_operations_to_detach} 个将清空的操作诊断来源引用",
            f"{counts.diagnostic_attempts_to_detach} 个将清空的洞察引擎尝试诊断引用",
        ],
    )


def _build_preview_token(
    source: Source,
    candidates: _SourceDeleteCandidateSet,
    counts: SourceDeletePreviewCounts,
) -> str:
    payload = {
        "source_id": source.id,
        "source_updated_at": source.updated_at.isoformat() if source.updated_at is not None else None,
        "counts": counts.model_dump(mode="json"),
        "hashes": {
            "source_feed_item_ids": _hash_ids(candidates.source_feed_item_ids),
            "exclusive_unsaved_feed_item_ids": _hash_ids(candidates.exclusive_unsaved_feed_item_ids),
            "saved_item_ids": _hash_ids(candidates.saved_item_ids),
            "source_fetch_ids": _hash_ids(candidates.source_fetch_ids),
            "source_item_occurrence_ids": _hash_ids(candidates.source_item_occurrence_ids),
            "source_preference_profile_ids": _hash_ids(candidates.source_preference_profile_ids),
            "source_channel_tendency_ids": _hash_ids(candidates.source_channel_tendency_ids),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _list_delete_blockers(
    session: Session,
    *,
    source_id: int,
    source_feed_item_ids: Sequence[int],
) -> list[SourceDeleteBlockerRead]:
    source_feed_item_id_set = set(source_feed_item_ids)
    blockers: list[SourceDeleteBlockerRead] = []
    operations = session.scalars(
        select(AppOperation)
        .where(AppOperation.status.in_(ACTIVE_OPERATION_STATUSES))
        .order_by(AppOperation.created_at.asc(), AppOperation.id.asc())
    )
    for operation in operations:
        reason = _operation_block_reason(operation, source_id=source_id, source_feed_item_ids=source_feed_item_id_set)
        if reason is None:
            continue
        blockers.append(
            SourceDeleteBlockerRead(
                operation_id=operation.id,
                operation_type=operation.operation_type,
                operation_kind=operation.operation_kind,
                status=operation.status,
                source_id=operation.source_id,
                reason=reason,
                message=operation.message,
            )
        )
    return blockers


def _operation_block_reason(
    operation: AppOperation,
    *,
    source_id: int,
    source_feed_item_ids: set[int],
) -> str | None:
    if operation.operation_type in BLOCKING_GLOBAL_OPERATION_TYPES:
        return "正在运行的来源刷新或构建任务必须先结束，才能删除来源。"
    if operation.operation_type not in BLOCKING_FEED_ITEM_OPERATION_TYPES:
        return None
    scope = operation.scope_json if isinstance(operation.scope_json, dict) else {}
    feed_item_id = _scope_int(scope, "feed_item_id")
    if feed_item_id is not None and feed_item_id in source_feed_item_ids:
        return "正在运行的条目构建或提取任务覆盖该来源，必须先结束。"
    if operation.source_id == source_id:
        return "正在运行的条目构建或提取任务关联该来源，必须先结束。"
    return None


def _detach_diagnostic_rows(
    session: Session,
    source_id: int,
    exclusive_unsaved_feed_item_ids: Sequence[int],
) -> None:
    session.execute(update(AppOperation).where(AppOperation.source_id == source_id).values(source_id=None))
    session.execute(update(InsightEngineAttempt).where(InsightEngineAttempt.source_id == source_id).values(source_id=None))
    if exclusive_unsaved_feed_item_ids:
        for chunk in _chunks(exclusive_unsaved_feed_item_ids):
            session.execute(
                update(InsightEngineAttempt)
                .where(InsightEngineAttempt.feed_item_id.in_(chunk))
                .values(feed_item_id=None)
            )


def _delete_feed_item_owned_rows(session: Session, exclusive_unsaved_feed_item_ids: Sequence[int]) -> None:
    if not exclusive_unsaved_feed_item_ids:
        return
    for model, column in (
        (InsightEngineArtifact, InsightEngineArtifact.feed_item_id),
        (FeedItemStructuredFeedback, FeedItemStructuredFeedback.feed_item_id),
        (FeedItemBehaviorEvent, FeedItemBehaviorEvent.feed_item_id),
        (ItemState, ItemState.feed_item_id),
        (FeedItem, FeedItem.id),
    ):
        for chunk in _chunks(exclusive_unsaved_feed_item_ids):
            session.execute(delete(model).where(column.in_(chunk)))


def _delete_homepage_read_model_rows(session: Session, homepage_read_model_feed_item_ids: Sequence[int]) -> None:
    if not homepage_read_model_feed_item_ids:
        return
    for chunk in _chunks(homepage_read_model_feed_item_ids):
        session.execute(delete(FeedItemHomepageFacet).where(FeedItemHomepageFacet.feed_item_id.in_(chunk)))
        session.execute(delete(FeedItemHomepageReadModel).where(FeedItemHomepageReadModel.feed_item_id.in_(chunk)))


def _scope_int(scope: dict[str, Any], key: str) -> int | None:
    value = scope.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hash_ids(ids: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for value in sorted({int(next_id) for next_id in ids}):
        digest.update(str(value).encode("ascii"))
        digest.update(b",")
    return digest.hexdigest()


def _chunks(values: Sequence[int], size: int = 500) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])
