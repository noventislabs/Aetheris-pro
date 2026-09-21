"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from aetheris.core.errors import UpstreamUnavailableError
from aetheris.services.analysis import AnalysisService
from aetheris.services.autonomous import AutonomousLoop
from aetheris.services.backtest import BacktestService
from aetheris.services.market_data import MarketDataService
from aetheris.services.paper import PaperTradingService
from aetheris.services.scanner import ScannerService
from aetheris.services.testnet import TestnetExecutionService


def get_market_data_service(request: Request) -> MarketDataService:
    """Resolve the process-wide market-data service.

    Built once at application startup so one HTTP connection pool is shared by
    every request, rather than a client per call.
    """
    service = getattr(request.app.state, "market_data_service", None)
    if not isinstance(service, MarketDataService):
        raise UpstreamUnavailableError("Market-data service is not configured")
    return service


MarketDataDep = Annotated[MarketDataService, Depends(get_market_data_service)]


def get_scanner_service(request: Request) -> ScannerService:
    """Resolve the process-wide scanner service."""
    service = getattr(request.app.state, "scanner_service", None)
    if not isinstance(service, ScannerService):
        raise UpstreamUnavailableError("Scanner service is not configured")
    return service


ScannerDep = Annotated[ScannerService, Depends(get_scanner_service)]


def get_analysis_service(request: Request) -> AnalysisService:
    """Resolve the process-wide analysis service."""
    service = getattr(request.app.state, "analysis_service", None)
    if not isinstance(service, AnalysisService):
        raise UpstreamUnavailableError("Analysis service is not configured")
    return service


AnalysisDep = Annotated[AnalysisService, Depends(get_analysis_service)]


def get_backtest_service(request: Request) -> BacktestService:
    """Resolve the process-wide backtest service."""
    service = getattr(request.app.state, "backtest_service", None)
    if not isinstance(service, BacktestService):
        raise UpstreamUnavailableError("Backtest service is not configured")
    return service


BacktestDep = Annotated[BacktestService, Depends(get_backtest_service)]


def get_paper_service(request: Request) -> PaperTradingService:
    """Resolve the process-wide paper trading service.

    Process-wide is load-bearing here rather than merely efficient: the paper
    account lives in this service's repository, so a per-request instance would
    hand every caller a fresh 100 USDT and no positions.
    """
    service = getattr(request.app.state, "paper_service", None)
    if not isinstance(service, PaperTradingService):
        raise UpstreamUnavailableError("Paper trading service is not configured")
    return service


PaperDep = Annotated[PaperTradingService, Depends(get_paper_service)]


def get_autonomous_loop(request: Request) -> AutonomousLoop:
    """Resolve the process-wide autonomous loop.

    Process-wide is load-bearing rather than merely efficient: the loop owns
    the armed flag, the decision log and the background task. A per-request
    instance would report a disarmed loop while the real one was trading.
    """
    loop = getattr(request.app.state, "autonomous_loop", None)
    if not isinstance(loop, AutonomousLoop):
        raise UpstreamUnavailableError("Autonomous trading is not configured")
    return loop


AutonomousDep = Annotated[AutonomousLoop, Depends(get_autonomous_loop)]


def get_testnet_service(request: Request) -> TestnetExecutionService:
    """Resolve the process-wide testnet execution service.

    Absent means testnet execution is not configured, and that is reported as
    unavailable rather than quietly served by something else. There is no
    fallback to paper here: a caller that asked to trade the testnet and was
    silently simulated would have no way to tell.
    """
    service = getattr(request.app.state, "testnet_service", None)
    if not isinstance(service, TestnetExecutionService):
        raise UpstreamUnavailableError(
            "Testnet execution is not configured. It requires testnet credentials "
            "and a reachable database, and does not fall back to paper."
        )
    return service


TestnetDep = Annotated[TestnetExecutionService, Depends(get_testnet_service)]
