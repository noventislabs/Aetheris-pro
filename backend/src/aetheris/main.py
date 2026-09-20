"""Application factory and ASGI entrypoint.

Run in development with::

    uvicorn aetheris.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aetheris import __version__
from aetheris.adapters.exchange.binance.adapter import BinanceFuturesMarketDataAdapter
from aetheris.api.exception_handlers import register_exception_handlers
from aetheris.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from aetheris.api.v1.router import api_router
from aetheris.core.config import Settings, get_settings
from aetheris.core.logging import configure_logging, get_logger
from aetheris.services.market_data import MarketDataService
from aetheris.services.scanner import ScannerService

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
        exchange=app.state.market_data_service.exchange_name,
    )
    try:
        yield
    finally:
        # Release the exchange connection pool even if startup partly failed.
        await app.state.market_data_service.aclose()
        _log.info("shutdown")


def create_app(
    settings: Settings | None = None,
    *,
    exchange_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the ASGI application.

    Accepting settings as an argument keeps the app testable: a test can build
    an app with a hand-made configuration without touching the environment.
    ``exchange_transport`` serves the same purpose for the network -- tests
    inject a mock transport so the suite never depends on a venue being up.
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
    exchange = BinanceFuturesMarketDataAdapter(
        settings=settings.binance,
        market_data=settings.market_data,
        transport=exchange_transport,
    )
    # Both services share one adapter, so they share its connection pool and
    # its caches -- a scan and a chart request for the same symbol do not
    # fetch it twice.
    app.state.market_data_service = MarketDataService(exchange)
    app.state.scanner_service = ScannerService(exchange, settings.scanner, settings.market_data)

    # Middleware executes bottom-up, so RequestContextMiddleware is added last
    # and therefore runs first -- every log line below it carries a request ID.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        # This build is read-only, so CORS advertises only what exists.
        # Write methods are added back in the phase that introduces a
        # write endpoint, not in advance of one.
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Request-ID", "X-Correlation-ID"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
