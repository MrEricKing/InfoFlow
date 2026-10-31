from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AppOperation, Job


def operation_correlation_id(operation: AppOperation | None) -> str | None:
    if operation is None:
        return None
    scope = operation.scope_json if isinstance(operation.scope_json, dict) else {}
    value = scope.get("correlation_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if operation.id is not None:
        return f"operation:{operation.id}"
    return None


def log_operation_event(
    logger: logging.Logger,
    event: str,
    *,
    operation: AppOperation | None = None,
    job: Job | None = None,
    worker_id: str | None = None,
    **fields: Any,
) -> None:
    context = _context(operation=operation, job=job, worker_id=worker_id, **fields)
    logger.info("%s %s", event, json.dumps(context, sort_keys=True), extra={"infoflow_context": context})


def log_job_event(
    logger: logging.Logger,
    event: str,
    session: Session,
    job: Job,
    *,
    worker_id: str | None = None,
    **fields: Any,
) -> None:
    operation = session.get(AppOperation, job.operation_id) if job.operation_id is not None else None
    log_operation_event(logger, event, operation=operation, job=job, worker_id=worker_id, **fields)


def _context(
    *,
    operation: AppOperation | None,
    job: Job | None,
    worker_id: str | None,
    **fields: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    correlation_id = operation_correlation_id(operation)
    if correlation_id is not None:
        context["correlation_id"] = correlation_id
    if operation is not None:
        context["operation_id"] = operation.id
        context["operation_type"] = operation.operation_type
        context["operation_status"] = operation.status
    if job is not None:
        context["job_id"] = job.id
        context["job_type"] = job.job_type
        context["job_status"] = job.status
        context["job_attempts"] = job.attempts
    if worker_id:
        context["worker_id"] = worker_id
    for key, value in fields.items():
        if value is not None:
            context[key] = value
    return context
