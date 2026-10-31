from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, InsightEngineAttempt, Source
from app.schemas.settings import InsightEngineAttemptListRead, InsightEngineAttemptRead
from app.services.app_settings import AIProviderConfig

ATTEMPT_STATUS_FAILED = "failed"
ATTEMPT_STATUS_SKIPPED = "skipped"
MAX_ERROR_MESSAGE_CHARS = 4000

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "x-api-key",
    "x_api_key",
    "access_token",
    "refresh_token",
    "password",
    "secret",
}


def record_insight_engine_attempt(
    session: Session,
    *,
    feed_item: FeedItem | None,
    source: Source | None,
    config: AIProviderConfig | None,
    status: str,
    stage: str,
    error_type: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    artifact_count: int = 0,
    request_snapshot: dict[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> InsightEngineAttempt:
    attempt = InsightEngineAttempt(
        feed_item_id=feed_item.id if feed_item is not None else None,
        source_id=source.id if source is not None else None,
        ai_route=(source.ai_route if source is not None else None),
        profile=config.profile if config is not None else "disabled",
        provider=config.provider if config is not None else "disabled",
        model=config.model if config is not None else None,
        status=status,
        stage=stage,
        error_type=error_type,
        error_message=_truncate_error(error_message),
        duration_ms=duration_ms,
        artifact_count=artifact_count,
        request_snapshot_json=sanitize_request_snapshot(request_snapshot),
        completed_at=completed_at or _now_utc(),
    )
    session.add(attempt)
    session.flush()
    return attempt


def list_insight_engine_attempts(
    session: Session,
    *,
    status: str | None = None,
    source_id: int | None = None,
    profile: str | None = None,
    item_id: int | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> InsightEngineAttemptListRead:
    conditions = _build_attempt_filters(
        status=status,
        source_id=source_id,
        profile=profile,
        item_id=item_id,
        created_after=created_after,
        created_before=created_before,
    )
    total = session.scalar(select(func.count()).select_from(InsightEngineAttempt).where(*conditions)) or 0
    statement = (
        select(InsightEngineAttempt, FeedItem.title, Source.name)
        .outerjoin(FeedItem, FeedItem.id == InsightEngineAttempt.feed_item_id)
        .outerjoin(Source, Source.id == InsightEngineAttempt.source_id)
        .where(*conditions)
        .order_by(InsightEngineAttempt.created_at.desc(), InsightEngineAttempt.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [
        _build_attempt_read(attempt, feed_item_title=feed_item_title, source_name=source_name, include_snapshot=False)
        for attempt, feed_item_title, source_name in session.execute(statement).all()
    ]
    return InsightEngineAttemptListRead(items=items, total=total, limit=limit, offset=offset)


def get_insight_engine_attempt(session: Session, attempt_id: int) -> InsightEngineAttemptRead | None:
    statement = (
        select(InsightEngineAttempt, FeedItem.title, Source.name)
        .outerjoin(FeedItem, FeedItem.id == InsightEngineAttempt.feed_item_id)
        .outerjoin(Source, Source.id == InsightEngineAttempt.source_id)
        .where(InsightEngineAttempt.id == attempt_id)
    )
    row = session.execute(statement).first()
    if row is None:
        return None
    attempt, feed_item_title, source_name = row
    return _build_attempt_read(
        attempt,
        feed_item_title=feed_item_title,
        source_name=source_name,
        include_snapshot=True,
    )


def sanitize_request_snapshot(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    sanitized = _sanitize_value(value)
    return sanitized if isinstance(sanitized, dict) else None


def _build_attempt_filters(
    *,
    status: str | None,
    source_id: int | None,
    profile: str | None,
    item_id: int | None,
    created_after: datetime | None,
    created_before: datetime | None,
) -> list[Any]:
    conditions: list[Any] = []
    if status:
        conditions.append(InsightEngineAttempt.status == status)
    if source_id is not None:
        conditions.append(InsightEngineAttempt.source_id == source_id)
    if profile:
        conditions.append(InsightEngineAttempt.profile == profile)
    if item_id is not None:
        conditions.append(InsightEngineAttempt.feed_item_id == item_id)
    if created_after is not None:
        conditions.append(InsightEngineAttempt.created_at >= created_after)
    if created_before is not None:
        conditions.append(InsightEngineAttempt.created_at <= created_before)
    return conditions


def _build_attempt_read(
    attempt: InsightEngineAttempt,
    *,
    feed_item_title: str | None,
    source_name: str | None,
    include_snapshot: bool,
) -> InsightEngineAttemptRead:
    return InsightEngineAttemptRead(
        id=attempt.id,
        feed_item_id=attempt.feed_item_id,
        feed_item_title=feed_item_title,
        source_id=attempt.source_id,
        source_name=source_name,
        ai_route=attempt.ai_route,
        profile=attempt.profile,
        provider=attempt.provider,
        model=attempt.model,
        status=attempt.status,
        stage=attempt.stage,
        error_type=attempt.error_type,
        error_message=attempt.error_message,
        duration_ms=attempt.duration_ms,
        artifact_count=attempt.artifact_count,
        created_at=attempt.created_at,
        completed_at=attempt.completed_at,
        request_snapshot=attempt.request_snapshot_json if include_snapshot else None,
    )


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, entry in value.items():
            if _is_sensitive_key(key):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_value(entry)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(entry) for entry in value]
    return value


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or "authorization" in normalized


def _truncate_error(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_ERROR_MESSAGE_CHARS:
        return normalized
    return f"{normalized[:MAX_ERROR_MESSAGE_CHARS]}..."


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
