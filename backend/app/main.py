from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.services.app_settings import validate_ai_secret_settings_for_startup
from app.services.homepage_read_model import ensure_homepage_read_model_backfill_started
from app.services.operation_jobs import start_operation_job_worker, stop_operation_job_worker


def create_app() -> FastAPI:
    _configure_application_logging()
    settings = get_settings()
    production_runtime = settings.environment.strip().lower() == "production"

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        openapi_url=None if production_runtime else f"{settings.api_prefix}/openapi.json",
        docs_url=None if production_runtime else f"{settings.api_prefix}/docs",
        redoc_url=None if production_runtime else f"{settings.api_prefix}/redoc",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.api_prefix)
    _mount_spa_static_files(app, settings.static_files_dir, settings.api_prefix)
    return app


def _configure_application_logging() -> None:
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    if app_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    app_logger.addHandler(handler)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    session_factory = get_session_factory()
    with session_factory() as session:
        validate_ai_secret_settings_for_startup(session)
        session.commit()
    worker_started = False
    if not settings.database_url.startswith("sqlite"):
        worker_started = start_operation_job_worker()
        ensure_homepage_read_model_backfill_started("app_startup")
    try:
        yield
    finally:
        if worker_started:
            stop_operation_job_worker()


def _mount_spa_static_files(app: FastAPI, static_files_dir: Path | None, api_prefix: str) -> None:
    if static_files_dir is None:
        return

    static_root = static_files_dir.resolve()
    index_file = static_root / "index.html"
    if not index_file.is_file():
        return

    @app.get("/{requested_path:path}", include_in_schema=False)
    async def serve_spa(requested_path: str) -> FileResponse:
        normalized_api_prefix = api_prefix.strip("/")
        if requested_path == normalized_api_prefix or requested_path.startswith(f"{normalized_api_prefix}/"):
            raise HTTPException(status_code=404)

        candidate = (static_root / requested_path).resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError as exc:
            raise HTTPException(status_code=404) from exc

        if candidate.is_file():
            return FileResponse(candidate)

        return FileResponse(index_file)


app = create_app()
