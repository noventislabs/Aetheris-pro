"""Version 1 API surface."""

from __future__ import annotations

from fastapi import APIRouter

from aetheris.api.v1 import analysis, health, markets, scanner, system

api_router = APIRouter()
api_router.include_router(analysis.router)
api_router.include_router(health.router)
api_router.include_router(markets.router)
api_router.include_router(scanner.router)
api_router.include_router(system.router)
