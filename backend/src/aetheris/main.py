"""Application factory and ASGI entrypoint.

Run in development with::

    uvicorn aetheris.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aetheris import __version__
from aetheris.api.exception_handlers import register_exception_handlers
from aetheris.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from aetheris.api.v1.router import api_router
from aetheris.core.config import Settings, get_settings
from aetheris.core.logging import configure_logging, get_logger

_log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    _log.info(
        "startup",
        version=__version__,
        environment=settings.environment,
        default_mode=settings.default_mode,
        enabled_modes=[m.value for m in settings.enabled_modes],
        autonomous_trading_enabled=settings.autonomous_trading_enabled,
    )
    yield
    _log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Accepting settings as an argument keeps the app testable: a test can build
    an app with a hand-made configuration without touching the environment.
    """
    settings = settings or get_settings()
    configure_logging(debug=settings.debug, level="DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title="Aetheris Pro",
        version=__version__,
        summary="Professional crypto trading and quantitative research platform",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Middleware executes bottom-up, so RequestContextMiddleware is added last
    # and therefore runs first -- every log line below it carries a request ID.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Request-ID", "X-Correlation-ID"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
