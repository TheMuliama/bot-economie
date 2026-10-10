"""FastAPI application factory for the M Corp Admin Dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import DiscordOAuthClient
from .config import ConfigurationError, DashboardSettings
from .routes import build_router
from .scheduler import WeeklyProductionScheduler
from .services import DashboardService


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).parent


def create_app(
    settings: DashboardSettings | None = None,
    service: DashboardService | None = None,
    oauth: DiscordOAuthClient | None = None,
) -> FastAPI:
    settings = settings or DashboardSettings.from_env()
    service = service or DashboardService.from_database(settings.database_path, settings.institution_id)
    oauth = oauth or DiscordOAuthClient(settings)
    scheduler = WeeklyProductionScheduler(
        service.schedules, service.control, settings.institution_id,
        error_hook=lambda exc: LOGGER.exception("Scheduled production failed", exc_info=exc),
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        task = None
        if settings.scheduler_enabled:
            async def worker() -> None:
                while True:
                    try:
                        await asyncio.to_thread(scheduler.run_due_once)
                    except Exception:
                        LOGGER.exception("Weekly production scheduler iteration failed")
                    await asyncio.sleep(settings.scheduler_interval_seconds)
            task = asyncio.create_task(worker(), name="mcorp-weekly-production")
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    application = FastAPI(
        title="M Corp Admin Dashboard", docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )
    application.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret,
        session_cookie="mcorp_dashboard", max_age=settings.session_max_age_seconds,
        same_site="lax", https_only=settings.cookie_secure,
    )
    application.state.settings = settings
    application.state.service = service
    application.state.scheduler = scheduler
    application.state.templates = Jinja2Templates(directory=ROOT / "templates")
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    application.include_router(build_router(service, oauth))

    @application.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok", "database": settings.database_path.exists()}

    return application


def _configuration_error_app(exc: ConfigurationError) -> FastAPI:
    application = FastAPI(title="M Corp Admin Dashboard (configuration requise)")

    @application.get("/health")
    async def health():
        return JSONResponse({"status": "configuration_error", "detail": str(exc)}, status_code=503)

    return application


try:
    app = create_app()
except ConfigurationError as configuration_error:
    app = _configuration_error_app(configuration_error)
