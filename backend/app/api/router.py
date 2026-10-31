from fastapi import APIRouter

from app.api.routes.feed_item_insight_artifacts import router as feed_item_insight_artifacts_router
from app.api.routes.health import router as health_router
from app.api.routes.homepage import router as homepage_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.operations import router as operations_router
from app.api.routes.reader import router as reader_router
from app.api.routes.settings import router as settings_router
from app.api.routes.sources import router as sources_router

api_router = APIRouter()
api_router.include_router(feed_item_insight_artifacts_router)
api_router.include_router(health_router)
api_router.include_router(homepage_router)
api_router.include_router(knowledge_router)
api_router.include_router(operations_router)
api_router.include_router(reader_router)
api_router.include_router(settings_router)
api_router.include_router(sources_router)
