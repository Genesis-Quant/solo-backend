"""FastAPI entry point."""

from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from config import DatabaseSettings, DolphinSchedulerSettings, SoloSettings
from core.apps.projects.views import router as projects_router
from core.apps.projects.versions import router as versions_router
from core.apps.system.views import health
from core.apps.system.views import router as system_router
from core.database.session import database_engine
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    DatabaseSettings.validate()
    if DolphinSchedulerSettings.ENABLED:
        from core.scheduler.workflows import sync_workflows

        application.state.workflows = await to_thread(sync_workflows)
    try:
        yield
    finally:
        database_engine().dispose()


app = FastAPI(title="Solo Backend", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SoloSettings.WEB_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(system_router)
app.include_router(projects_router)
app.include_router(versions_router)
app.add_api_route("/health", health, methods=["GET"], tags=["system"])
