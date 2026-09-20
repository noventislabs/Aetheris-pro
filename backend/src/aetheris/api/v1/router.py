"""Version 1 API surface."""

from __future__ import annotations

from fastapi import APIRouter

from aetheris.api.v1 import health, markets, system

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(markets.router)
api_router.include_router(system.router)
