from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.safety import MULTIPART_BODY_OVERHEAD_MAX_BYTES, OPML_UPLOAD_MAX_BYTES, SafetyError, read_request_body_with_limit
from app.db.models import Source, SourceFetch, SourcePreferenceProfile
from app.db.session import get_db_session
from app.schemas.source import (
    OpmlImportCommitRequest,
    OpmlImportPreviewResponse,
    SourceCategoryDeleteRequest,
    SourceCategoryMutationResponse,
    SourceCategoryRead,
    SourceCategoryRenameRequest,
    SourceCreate,
    SourceDeletePreviewRead,
    SourceDeleteRequest,
    SourceDeleteResult,
    SourceFetchRead,
    SourceFetchWithSourceRead,
    SourceImportResponse,
    SourceInsightRoutingRead,
    SourceInsightRoutingUpdateRequest,
    SourcePreferenceProfileActivateRequest,
    SourcePreferenceProfileDraftUpdateRequest,
    SourcePreferenceProfileRead,
    SourcePreferenceProfileStateRead,
    SourcePreviewRequest,
    SourcePreviewResponse,
    SourceRead,
    SourceUpdate,
)
from app.services.source_preview import SourcePreviewError, preview_source
from app.services.insight_channels import (
    SourceRoutingNotFoundError,
    SourceRoutingValidationError,
    get_source_routing,
    update_source_routing,
)
from app.services.source_registry import (
    SourceConflictError,
    SourceImportRunNotFoundError,
    SourceValidationError,
    commit_opml_import,
    create_source,
    delete_source_category,
    export_sources_as_opml,
    extract_uploaded_file_from_multipart,
    get_source_or_none,
    list_source_item_counts,
    list_source_categories,
    list_sources,
    preview_opml,
    rename_source_category,
    update_source,
)
from app.services.source_deletion import (
    SourceDeletionConflictError,
    SourceDeletionNotFoundError,
    SourceDeletionValidationError,
    build_source_delete_preview,
    delete_source_with_preview_token,
)
from app.services.source_sync import (
    list_latest_fetches_by_source_ids,
    list_recent_fetches,
    list_recent_fetches_across_sources,
)
from app.services.source_preference_profiles import (
    SourcePreferenceProfileNotFoundError,
    SourcePreferenceProfileValidationError,
    activate_source_preference_profile_draft,
    delete_source_preference_profile_draft,
    get_source_preference_profile_state,
    list_source_preference_profile_history,
    save_source_preference_profile_draft,
)
from app.services.homepage_read_model import (
    ensure_homepage_read_model_backfill_started,
    mark_homepage_read_models_stale,
)

router = APIRouter(prefix="/sources", tags=["sources"])


def build_source_read(
    source: Source,
    latest_fetch: SourceFetch | None = None,
    *,
    item_count: int = 0,
    profile_draft_source_type: str | None = None,
) -> SourceRead:
    payload = SourceRead.model_validate(source)
    update_payload = {
        "item_count": item_count,
        "source_profile_draft_source_type": profile_draft_source_type,
        "source_profile_pending_external_suggestion": profile_draft_source_type == "codex_import",
    }
    if latest_fetch is not None:
        update_payload.update(
            {
                "latest_fetch_is_available": get_latest_fetch_availability(latest_fetch),
                "latest_fetch_status": latest_fetch.status,
                "latest_fetch_http_status": latest_fetch.http_status,
                "latest_fetch_error_message": latest_fetch.error_message,
                "latest_fetch_items_seen_count": latest_fetch.items_seen_count,
                "latest_fetch_items_created_count": latest_fetch.items_created_count,
                "latest_fetch_items_matched_count": latest_fetch.items_matched_count,
                "latest_fetch_items_invalid_count": latest_fetch.items_invalid_count,
                "latest_fetch_started_at": latest_fetch.fetch_started_at,
                "latest_fetch_finished_at": latest_fetch.fetch_finished_at,
            }
        )
    return payload.model_copy(update=update_payload)


def get_latest_fetch_availability(latest_fetch: SourceFetch) -> bool | None:
    if latest_fetch.status in {"success", "not_modified"}:
        return True
    if latest_fetch.status == "error":
        return False
    return None


def _list_source_profile_draft_source_types(session: Session, source_ids: list[int]) -> dict[int, str]:
    unique_ids = list(dict.fromkeys(source_ids))
    if not unique_ids:
        return {}
    rows = session.execute(
        select(SourcePreferenceProfile.source_id, SourcePreferenceProfile.source_type).where(
            SourcePreferenceProfile.source_id.in_(unique_ids),
            SourcePreferenceProfile.status == "draft",
        )
    ).all()
    return {source_id: source_type for source_id, source_type in rows}


@router.get("", response_model=list[SourceRead])
def list_sources_endpoint(session: Session = Depends(get_db_session)) -> list[SourceRead]:
    sources = list_sources(session)
    source_ids = [source.id for source in sources]
    latest_fetches = list_latest_fetches_by_source_ids(session, source_ids)
    item_counts = list_source_item_counts(session, source_ids)
    profile_draft_source_types = _list_source_profile_draft_source_types(session, source_ids)
    return [
        build_source_read(
            source,
            latest_fetches.get(source.id),
            item_count=item_counts.get(source.id, 0),
            profile_draft_source_type=profile_draft_source_types.get(source.id),
        )
        for source in sources
    ]


@router.get("/fetches", response_model=list[SourceFetchWithSourceRead])
def list_all_source_fetches_endpoint(
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_db_session),
) -> list[SourceFetchWithSourceRead]:
    return [
        SourceFetchWithSourceRead(
            **SourceFetchRead.model_validate(fetch).model_dump(),
            source_name=source_name,
        )
        for fetch, source_name in list_recent_fetches_across_sources(session, limit)
    ]


@router.post("/preview", response_model=SourcePreviewResponse)
def preview_source_endpoint(
    payload: SourcePreviewRequest,
    session: Session = Depends(get_db_session),
) -> SourcePreviewResponse:
    try:
        return preview_source(session, payload)
    except SourceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": str(exc),
                "failure_stage": "source_resolve_failed",
                "detected_kind": payload.kind,
                "canonical_url": None,
                "resolved_url": None,
            },
        ) from exc
    except SourcePreviewError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.to_detail()) from exc


@router.get("/categories", response_model=list[SourceCategoryRead])
def list_source_categories_endpoint(session: Session = Depends(get_db_session)) -> list[SourceCategoryRead]:
    return list_source_categories(session)


@router.get("/export/opml")
def export_sources_opml_endpoint(session: Session = Depends(get_db_session)) -> Response:
    content = export_sources_as_opml(session)
    return Response(
        content=content,
        media_type="text/x-opml",
        headers={
            "Content-Disposition": 'attachment; filename="infoflow-sources.opml"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0",
        },
    )


@router.post("/categories/rename", response_model=SourceCategoryMutationResponse)
def rename_source_category_endpoint(
    payload: SourceCategoryRenameRequest,
    session: Session = Depends(get_db_session),
) -> SourceCategoryMutationResponse:
    try:
        response = rename_source_category(session, old_path=payload.old_path, new_path=payload.new_path)
        mark_homepage_read_models_stale(session, reason="source_category_renamed")
        session.commit()
        ensure_homepage_read_model_backfill_started("source_category_renamed")
    except SourceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return response


@router.post("/categories/delete", response_model=SourceCategoryMutationResponse)
def delete_source_category_endpoint(
    payload: SourceCategoryDeleteRequest,
    session: Session = Depends(get_db_session),
) -> SourceCategoryMutationResponse:
    try:
        response = delete_source_category(session, path=payload.path)
        mark_homepage_read_models_stale(session, reason="source_category_deleted")
        session.commit()
        ensure_homepage_read_model_backfill_started("source_category_deleted")
    except SourceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return response


@router.post("", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def create_source_endpoint(payload: SourceCreate, session: Session = Depends(get_db_session)) -> SourceRead:
    try:
        source = create_source(session, payload)
        session.commit()
    except SourceConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Source already exists", "source_id": exc.source.id},
        ) from exc
    except SourceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return build_source_read(source)


@router.get("/{source_id}/fetches", response_model=list[SourceFetchRead])
def list_source_fetches_endpoint(
    source_id: int,
    limit: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_db_session),
) -> list[SourceFetchRead]:
    source = get_source_or_none(session, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return [SourceFetchRead.model_validate(fetch) for fetch in list_recent_fetches(session, source_id, limit)]


@router.get("/{source_id}/delete-preview", response_model=SourceDeletePreviewRead)
def get_source_delete_preview_endpoint(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> SourceDeletePreviewRead:
    try:
        return build_source_delete_preview(session, source_id)
    except SourceDeletionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{source_id}", response_model=SourceDeleteResult)
def delete_source_endpoint(
    source_id: int,
    payload: SourceDeleteRequest,
    session: Session = Depends(get_db_session),
) -> SourceDeleteResult:
    try:
        result = delete_source_with_preview_token(
            session,
            source_id,
            confirm=payload.confirm,
            preview_token=payload.preview_token,
        )
        session.commit()
        ensure_homepage_read_model_backfill_started("source_deleted")
    except SourceDeletionNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SourceDeletionValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SourceDeletionConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "preview": exc.preview.model_dump(mode="json"),
            },
        ) from exc
    return result


@router.get("/{source_id}/insight-routing", response_model=SourceInsightRoutingRead)
def get_source_insight_routing_endpoint(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> SourceInsightRoutingRead:
    try:
        return get_source_routing(session, source_id)
    except SourceRoutingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch("/{source_id}/insight-routing", response_model=SourceInsightRoutingRead)
def update_source_insight_routing_endpoint(
    source_id: int,
    payload: SourceInsightRoutingUpdateRequest,
    session: Session = Depends(get_db_session),
) -> SourceInsightRoutingRead:
    try:
        result = update_source_routing(session, source_id, payload)
        mark_homepage_read_models_stale(session, source_ids=[source_id], reason="source_routing_updated")
        session.commit()
        ensure_homepage_read_model_backfill_started("source_routing_updated")
    except SourceRoutingNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SourceRoutingValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result


@router.get("/{source_id}/preference-profile", response_model=SourcePreferenceProfileStateRead)
def get_source_preference_profile_endpoint(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> SourcePreferenceProfileStateRead:
    try:
        return get_source_preference_profile_state(session, source_id)
    except SourcePreferenceProfileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{source_id}/preference-profile/draft", response_model=SourcePreferenceProfileStateRead)
def save_source_preference_profile_draft_endpoint(
    source_id: int,
    payload: SourcePreferenceProfileDraftUpdateRequest,
    session: Session = Depends(get_db_session),
) -> SourcePreferenceProfileStateRead:
    try:
        result = save_source_preference_profile_draft(session, source_id, payload)
        session.commit()
    except SourcePreferenceProfileNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SourcePreferenceProfileValidationError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Source Preference Profile validation failed.",
                "errors": [error.model_dump(mode="json") for error in exc.errors],
            },
        ) from exc
    return result


@router.post("/{source_id}/preference-profile/draft/activate", response_model=SourcePreferenceProfileStateRead)
def activate_source_preference_profile_draft_endpoint(
    source_id: int,
    payload: SourcePreferenceProfileActivateRequest | None = Body(default=None),
    session: Session = Depends(get_db_session),
) -> SourcePreferenceProfileStateRead:
    try:
        result = activate_source_preference_profile_draft(
            session,
            source_id,
            edit_note=payload.edit_note if payload is not None else None,
        )
        mark_homepage_read_models_stale(session, source_ids=[source_id], reason="source_preference_profile_activated")
        session.commit()
        ensure_homepage_read_model_backfill_started("source_preference_profile_activated")
    except SourcePreferenceProfileNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SourcePreferenceProfileValidationError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Source Preference Profile validation failed.",
                "errors": [error.model_dump(mode="json") for error in exc.errors],
            },
        ) from exc
    return result


@router.delete("/{source_id}/preference-profile/draft", response_model=SourcePreferenceProfileStateRead)
def delete_source_preference_profile_draft_endpoint(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> SourcePreferenceProfileStateRead:
    try:
        result = delete_source_preference_profile_draft(session, source_id)
        session.commit()
    except SourcePreferenceProfileNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return result


@router.get("/{source_id}/preference-profile/history", response_model=list[SourcePreferenceProfileRead])
def list_source_preference_profile_history_endpoint(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> list[SourcePreferenceProfileRead]:
    try:
        return list_source_preference_profile_history(session, source_id)
    except SourcePreferenceProfileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{source_id}", response_model=SourceRead)
def get_source_endpoint(source_id: int, session: Session = Depends(get_db_session)) -> SourceRead:
    source = get_source_or_none(session, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    latest_fetch = list_latest_fetches_by_source_ids(session, [source.id]).get(source.id)
    item_count = list_source_item_counts(session, [source.id]).get(source.id, 0)
    profile_draft_source_type = _list_source_profile_draft_source_types(session, [source.id]).get(source.id)
    return build_source_read(
        source,
        latest_fetch,
        item_count=item_count,
        profile_draft_source_type=profile_draft_source_type,
    )


@router.patch("/{source_id}", response_model=SourceRead)
def update_source_endpoint(
    source_id: int,
    payload: SourceUpdate,
    session: Session = Depends(get_db_session),
) -> SourceRead:
    source = get_source_or_none(session, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    try:
        source = update_source(session, source, payload)
        mark_homepage_read_models_stale(session, source_ids=[source_id], reason="source_updated")
        session.commit()
        ensure_homepage_read_model_backfill_started("source_updated")
    except SourceConflictError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Source already exists", "source_id": exc.source.id},
        ) from exc
    except SourceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    latest_fetch = list_latest_fetches_by_source_ids(session, [source.id]).get(source.id)
    item_count = list_source_item_counts(session, [source.id]).get(source.id, 0)
    profile_draft_source_type = _list_source_profile_draft_source_types(session, [source.id]).get(source.id)
    return build_source_read(
        source,
        latest_fetch,
        item_count=item_count,
        profile_draft_source_type=profile_draft_source_type,
    )


@router.post("/imports/opml/preview", response_model=OpmlImportPreviewResponse, status_code=status.HTTP_201_CREATED)
async def preview_sources_opml_endpoint(
    request: Request,
    session: Session = Depends(get_db_session),
) -> OpmlImportPreviewResponse:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Expected multipart/form-data with an uploaded OPML file.",
        )

    try:
        body = await read_request_body_with_limit(
            request,
            max_bytes=OPML_UPLOAD_MAX_BYTES + MULTIPART_BODY_OVERHEAD_MAX_BYTES,
            purpose="OPML preview",
        )
        filename, raw_payload = extract_uploaded_file_from_multipart(body, content_type)
        response = preview_opml(session, filename, raw_payload)
        session.commit()
    except (SafetyError, SourceValidationError) as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return response


@router.post("/imports/opml/commit", response_model=SourceImportResponse)
def commit_sources_opml_endpoint(
    payload: OpmlImportCommitRequest,
    session: Session = Depends(get_db_session),
) -> SourceImportResponse:
    try:
        response = commit_opml_import(session, payload)
        session.commit()
    except SourceImportRunNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SourceValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return response
