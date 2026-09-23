"""FastAPI entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import DatabaseSettings, SoloSettings
from core.apps.system.views import health, router as system_router
from core.database.session import database_engine


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    DatabaseSettings.validate()
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
app.add_api_route("/health", health, methods=["GET"], tags=["system"])
