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
from aetheris.adapters.persistence.accounts import PostgresAccountRepository
from aetheris.adapters.persistence.engine import (
    DatabaseUnavailableError,
    build_engine,
    build_session_factory,
    check_connectivity,
)
from aetheris.adapters.persistence.orders import PostgresOrderRepository
from aetheris.api.exception_handlers import register_exception_handlers
from aetheris.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from aetheris.api.v1.router import api_router
from aetheris.core.config import Settings, get_settings
from aetheris.core.freshness import utcnow
from aetheris.core.logging import configure_logging, get_logger
from aetheris.domain.enums import TradingMode
from aetheris.engines.order.engine import OrderLifecycleEngine
from aetheris.engines.paper.engine import PaperEngine, PaperEngineConfig
from aetheris.engines.paper.store import InMemoryPaperRepository
from aetheris.services.analysis import AnalysisService
from aetheris.services.autonomous import AutonomousLoop
from aetheris.services.backtest import BacktestService
from aetheris.services.market_data import MarketDataService
from aetheris.services.paper import PaperTradingService
from aetheris.services.scanner import ScannerService

_log = get_logger("app")


async def _open_order_store(app: FastAPI, settings: Settings) -> None:
    """Attach durable order storage, or attach nothing and say so.

    There is deliberately **no in-memory fallback here**. A development store
    standing in for a database would make the application look healthy while
    the one thing persistence exists for -- an order record surviving the
    process that created it -- silently did not happen. Without a configured
    database the order lifecycle is simply absent, readiness reports
    ``NOT_CONFIGURED``, and nothing claims crash recovery.

    An unreachable database is not fatal at startup either. ADR 0002 made the
    database a remote service, so a connection that is down now and up in a
    minute is an operational state; the pool stays, readiness reports
    ``UNAVAILABLE``, and the process does not enter a restart loop over it.
    """
    app.state.db_engine = None
    app.state.db_sessions = None
    app.state.order_engine = None
    app.state.order_account_id = None
    app.state.order_recovery = None

    if settings.database_url is None:
        _log.info(
            "database_not_configured",
            persistence="absent",
            order_crash_recovery=False,
            paper_crash_recovery=False,
        )
        return

    engine = build_engine(settings)
    app.state.db_engine = engine
    reachable, detail = await check_connectivity(engine)
    if not reachable:
        _log.warning("database_unreachable", detail=detail, order_crash_recovery=False)
        return

    factory = build_session_factory(engine)
    app.state.db_sessions = factory
    try:
        accounts = PostgresAccountRepository(factory)
        owner_id = await accounts.ensure_owner()
        account_id = await accounts.ensure_account(
            owner_id=owner_id,
            mode=TradingMode.PAPER,
            starting_balance=settings.risk.paper_starting_balance,
        )
    except DatabaseUnavailableError as exc:
        _log.warning("database_bootstrap_failed", detail=str(exc), order_crash_recovery=False)
        return

    repository = PostgresOrderRepository(factory, owner_id=owner_id, account_id=account_id)
    order_engine = OrderLifecycleEngine(repository)
    app.state.order_engine = order_engine
    app.state.order_account_id = account_id

    # The recovery pass. Not optional and not deferred: the value of a durable
    # order record is entirely in what is done with it on the next boot, and
    # until this ran, "crash recovery" named a protocol nobody invoked.
    report = await order_engine.recover(now=utcnow())
    app.state.order_recovery = report

    # The risk engine's RECONCILIATION_PENDING check was unreachable while the
    # count was hardcoded to zero. Binding the store is what makes it fire.
    app.state.paper_service.bind_order_engine(order_engine)

    _log.info(
        "database_ready",
        detail=detail,
        durable_orders=repository.durable,
        # Named per subsystem, because one of these is true and the other is
        # not. Paper account state is still in memory by design.
        order_crash_recovery=True,
        paper_crash_recovery=False,
        recovery_scanned=report.scanned,
        recovery_interrupted=len(report.interrupted),
        recovery_never_submitted=len(report.never_submitted),
        recovery_already_unresolved=len(report.already_unresolved),
        recovery_unreadable=len(report.unreadable),
        entries_blocked=report.blocking,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    loop: AutonomousLoop = app.state.autonomous_loop
    await _open_order_store(app, settings)
    _log.info(
        "startup",
        version=__version__,
        environment=settings.environment,
        default_mode=settings.default_mode,
        enabled_modes=[m.value for m in settings.enabled_modes],
        autonomous_trading_enabled=settings.autonomous_trading_enabled,
        autonomous_armed=loop.armed,
        autonomous_symbols=len(settings.autonomous.symbols),
        exchange=app.state.market_data_service.exchange_name,
        durable_orders=app.state.order_engine is not None,
    )
    # Creating the task is not arming it. The loop idles until somebody calls
    # the arm endpoint, and starts disarmed on every boot however it was left.
    loop.start()
    try:
        yield
    finally:
        # Both of these must run even if startup partly failed, and the loop
        # must stop before the connection pool it uses is closed underneath it.
        await loop.aclose()
        await app.state.market_data_service.aclose()
        if app.state.db_engine is not None:
            await app.state.db_engine.dispose()
        _log.info("shutdown")


def _build_paper_service(market_data: MarketDataService, settings: Settings) -> PaperTradingService:
    """Construct the paper engine over in-memory storage.

    The repository is the seam that keeps this honest. Swapping
    ``InMemoryPaperRepository`` for a database-backed one is the whole of what
    durable paper state requires -- the engine, the service and the API do not
    change. Until that exists, the account resets with the process and every
    response says so.
    """
    repository = InMemoryPaperRepository(
        starting_balance=settings.risk.paper_starting_balance, now=utcnow()
    )
    engine = PaperEngine(
        repository,
        PaperEngineConfig(
            starting_balance=settings.risk.paper_starting_balance,
            daily_profit_target=settings.risk.daily_profit_target,
            daily_loss_limit=settings.risk.daily_loss_limit,
            max_open_positions=settings.risk.max_open_positions,
            max_position_notional=settings.risk.max_position_notional,
            max_portfolio_exposure=settings.risk.max_portfolio_exposure,
            max_data_age_seconds=settings.risk.max_data_age_seconds,
            taker_fee_bps=settings.paper.taker_fee_bps,
            slippage_bps=settings.paper.slippage_bps,
            max_order_log=settings.paper.max_order_log,
            max_trade_log=settings.paper.max_trade_log,
        ),
    )
    return PaperTradingService(market_data, engine, settings)


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
    app.state.analysis_service = AnalysisService(exchange, settings.analysis)
    app.state.backtest_service = BacktestService(exchange, settings.backtest)
    app.state.paper_service = _build_paper_service(app.state.market_data_service, settings)
    app.state.autonomous_loop = AutonomousLoop(
        app.state.market_data_service, app.state.paper_service, settings
    )

    # Middleware executes bottom-up, so RequestContextMiddleware is added last
    # and therefore runs first -- every log line below it carries a request ID.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        # CORS advertises exactly the verbs that exist. POST arrives with the
        # paper trading routes and nothing else: there is still no PUT, PATCH
        # or DELETE anywhere, so none is offered.
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Request-ID", "X-Correlation-ID"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
