"""FastAPI application factory and process lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import ALL_ROUTERS
from app.core.config import get_settings
from app.core.exceptions import CrawlerError, UnknownPlatformError
from app.core.logging import configure_logging, get_logger
from app.crawler import BrowserManager
from app.db.session import dispose_db, healthcheck, init_db
from app.services import ExportService, JobManager, SessionService

logger = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Starting social-crawler %s", __version__)

    await init_db(settings)

    browser = BrowserManager(settings)
    await browser.start()

    job_manager = JobManager(browser, settings)
    await job_manager.start()

    app.state.settings = settings
    app.state.browser = browser
    app.state.job_manager = job_manager
    app.state.session_service = SessionService(browser, settings)
    app.state.export_service = ExportService(settings)

    try:
        yield
    finally:
        logger.info("Shutting down")
        await job_manager.stop()
        await app.state.session_service.shutdown()
        await browser.stop()
        await dispose_db()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Social Crawler",
        version=__version__,
        description="Keyword-driven crawler for Threads, Instagram, Facebook and TikTok.",
        lifespan=lifespan,
    )

    for router in ALL_ROUTERS:
        application.include_router(router)

    @application.exception_handler(UnknownPlatformError)
    async def _unknown_platform(_request, error: UnknownPlatformError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @application.exception_handler(CrawlerError)
    async def _crawler_error(_request, error: CrawlerError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(error)})

    @application.get("/api/health", tags=["meta"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok" if await healthcheck() else "degraded",
            "version": __version__,
            "worker": application.state.job_manager.snapshot(),
        }

    if WEB_DIR.is_dir():

        @application.middleware("http")
        async def _no_cache_static(request, call_next):  # noqa: ANN001
            """Serve the UI unversioned but always fresh.

            Without this a browser keeps a cached app.js after an edit, which
            looks exactly like a bug in the code that was just changed.
            """
            response = await call_next(request)
            if request.url.path.startswith("/static") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

        application.mount(
            "/static", StaticFiles(directory=str(WEB_DIR)), name="static"
        )

        @application.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return application


app = create_app()
