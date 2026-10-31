from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.db.models import SavedItem
from app.db.session import get_db_session
from app.schemas.knowledge import (
    SavedItemDetailRead,
    SavedItemCuratedInsightEventRead,
    SavedItemCuratedInsightUpdateRequest,
    SavedItemListRead,
    SavedItemRemoveResponse,
    SavedItemUpdateRequest,
)
from app.services.knowledge_constants import ARCHIVE_SNAPSHOT_CONTENT_KINDS, HTML_CONTENT_TYPES
from app.services.knowledge_read_model import get_saved_item_detail, list_saved_items
from app.services.knowledge_saved_items import remove_saved_item, update_saved_item_note
from app.services.knowledge_snapshot_assets import (
    build_archive_asset_download_filename,
    build_archive_snapshot_download_filename,
    build_content_disposition_header,
    get_saved_item_archive_snapshot,
    get_saved_item_asset,
)
from app.services.saved_item_insights import (
    SavedItemInsightValidationError,
    clear_curated_saved_item_insight,
    list_curated_saved_item_insight_events,
    upsert_curated_saved_item_insight,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/saved-items", response_model=list[SavedItemListRead])
def list_saved_items_endpoint(
    query: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_db_session),
) -> list[SavedItemListRead]:
    return list_saved_items(session, limit=limit, query=query)


@router.post("/saved-items/{saved_item_id}/remove", response_model=SavedItemRemoveResponse)
def remove_saved_item_endpoint(
    saved_item_id: int,
    session: Session = Depends(get_db_session),
) -> SavedItemRemoveResponse:
    result = remove_saved_item(session, saved_item_id)
    if result is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Saved item not found")

    session.commit()
    return result


@router.get("/assets/{asset_key}/content")
def get_saved_item_asset_content_endpoint(
    asset_key: str,
    download: bool = Query(default=False),
    session: Session = Depends(get_db_session),
) -> Response:
    asset = get_saved_item_asset(session, asset_key)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    saved_item = session.get(SavedItem, asset.saved_item_id)
    return Response(
        content=asset.content_bytes,
        media_type=asset.mime_type,
        headers=_build_content_headers(
            filename=build_archive_asset_download_filename(
                asset.saved_item_id,
                saved_item.title if saved_item is not None else None,
                asset,
            ),
            download=download,
            sandbox_html=asset.mime_type in HTML_CONTENT_TYPES and not download,
        ),
    )


@router.get("/saved-items/{saved_item_id}/snapshots/{snapshot_kind}/content")
def get_saved_item_snapshot_content_endpoint(
    saved_item_id: int,
    snapshot_kind: str,
    download: bool = Query(default=False),
    session: Session = Depends(get_db_session),
) -> Response:
    if snapshot_kind not in ARCHIVE_SNAPSHOT_CONTENT_KINDS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Snapshot kind is not downloadable")

    snapshot = get_saved_item_archive_snapshot(
        session,
        saved_item_id=saved_item_id,
        snapshot_kind=snapshot_kind,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    saved_item = session.get(SavedItem, saved_item_id)
    return Response(
        content=snapshot.body.encode("utf-8"),
        media_type=snapshot.mime_type,
        headers=_build_content_headers(
            filename=build_archive_snapshot_download_filename(
                saved_item_id,
                saved_item.title if saved_item is not None else None,
                snapshot_kind,
            ),
            download=download,
        ),
    )


@router.get("/saved-items/{saved_item_id}", response_model=SavedItemDetailRead)
def get_saved_item_detail_endpoint(
    saved_item_id: int,
    session: Session = Depends(get_db_session),
) -> SavedItemDetailRead:
    item = get_saved_item_detail(session, saved_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Saved item not found")
    return item


@router.put("/saved-items/{saved_item_id}/curated-insight", response_model=SavedItemDetailRead)
def update_saved_item_curated_insight_endpoint(
    saved_item_id: int,
    payload: SavedItemCuratedInsightUpdateRequest,
    session: Session = Depends(get_db_session),
) -> SavedItemDetailRead:
    try:
        curated = upsert_curated_saved_item_insight(
            session,
            saved_item_id,
            payload=payload.payload,
            edit_note=payload.edit_note,
        )
    except SavedItemInsightValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if curated is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Saved item not found")

    session.commit()
    item = get_saved_item_detail(session, saved_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Saved item not found")
    return item


@router.delete("/saved-items/{saved_item_id}/curated-insight", response_model=SavedItemDetailRead)
def clear_saved_item_curated_insight_endpoint(
    saved_item_id: int,
    session: Session = Depends(get_db_session),
) -> SavedItemDetailRead:
    cleared = clear_curated_saved_item_insight(session, saved_item_id)
    if cleared is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Saved item not found")

    session.commit()
    item = get_saved_item_detail(session, saved_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Saved item not found")
    return item


@router.get(
    "/saved-items/{saved_item_id}/curated-insight/events",
    response_model=list[SavedItemCuratedInsightEventRead],
)
def list_saved_item_curated_insight_events_endpoint(
    saved_item_id: int,
    session: Session = Depends(get_db_session),
) -> list[SavedItemCuratedInsightEventRead]:
    events = list_curated_saved_item_insight_events(session, saved_item_id)
    if events is None:
        raise HTTPException(status_code=404, detail="Saved item not found")
    return events


@router.patch("/saved-items/{saved_item_id}", response_model=SavedItemDetailRead)
def update_saved_item_endpoint(
    saved_item_id: int,
    payload: SavedItemUpdateRequest,
    session: Session = Depends(get_db_session),
) -> SavedItemDetailRead:
    item = update_saved_item_note(session, saved_item_id, user_note=payload.user_note)
    if item is None:
        session.rollback()
        raise HTTPException(status_code=404, detail="Saved item not found")

    session.commit()
    return item


def _build_content_headers(*, filename: str, download: bool, sandbox_html: bool = False) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=0",
    }
    if download:
        headers["Content-Disposition"] = build_content_disposition_header(filename)
    if sandbox_html:
        headers["Content-Security-Policy"] = (
            "sandbox; "
            "default-src 'none'; "
            "img-src data: blob:; "
            "style-src 'unsafe-inline' data:; "
            "font-src data:; "
            "media-src data: blob:"
        )
    return headers
