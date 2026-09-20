"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from aetheris.core.errors import UpstreamUnavailableError
from aetheris.services.market_data import MarketDataService
from aetheris.services.scanner import ScannerService


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
