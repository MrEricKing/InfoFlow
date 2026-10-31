from fastapi import APIRouter

from app.core.config import get_settings
from app.core.runtime_info import get_runtime_version_info

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
        **get_runtime_version_info(settings.app_version),
    }
