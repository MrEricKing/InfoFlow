from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.homepage import (
    HomepageBootstrapRead,
    HomepageItemsRead,
    HomepageQuery,
    HomepageSummaryRead,
)
from app.services.homepage import (
    HomepageSnapshotError,
    get_homepage_bootstrap,
    get_homepage_summary,
    list_homepage_items,
)

router = APIRouter(prefix="/homepage", tags=["homepage"])


@router.get("/bootstrap", response_model=HomepageBootstrapRead)
def homepage_bootstrap_endpoint(
    query: HomepageQuery = Depends(),
    session: Session = Depends(get_db_session),
) -> HomepageBootstrapRead:
    try:
        return get_homepage_bootstrap(session, query)
    except HomepageSnapshotError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary", response_model=HomepageSummaryRead)
def homepage_summary_endpoint(
    query: HomepageQuery = Depends(),
    session: Session = Depends(get_db_session),
) -> HomepageSummaryRead:
    try:
        return get_homepage_summary(session, query)
    except HomepageSnapshotError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/items", response_model=HomepageItemsRead)
def homepage_items_endpoint(
    query: HomepageQuery = Depends(),
    session: Session = Depends(get_db_session),
) -> HomepageItemsRead:
    try:
        return list_homepage_items(session, query)
    except HomepageSnapshotError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
