"""Liveness and readiness (spec section 34).

Liveness answers "is this process alive"; readiness answers "should traffic be
sent here". They are deliberately different: a database outage makes the
service unready but not dead, and conflating them causes restart loops that
make an outage worse.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter, Response
from pydantic import BaseModel

from aetheris import __version__
from aetheris.core.config import Settings, get_settings
from aetheris.core.freshness import utcnow

router = APIRouter(tags=["health"])


class ComponentStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class ComponentHealth(BaseModel):
    name: str
    status: ComponentStatus
    detail: str


class LivenessResponse(BaseModel):
    status: str
    version: str


class ReadinessResponse(BaseModel):
    ready: bool
    checked_at: str
    components: list[ComponentHealth]


def _check_components(settings: Settings) -> list[ComponentHealth]:
    """Report each dependency truthfully, including the ones not yet built.

    NOT_CONFIGURED is distinct from UNAVAILABLE: the first means we never had
    it, the second means we had it and lost it. Only the second is an incident.
    """
    return [
        ComponentHealth(
            name="api",
            status=ComponentStatus.OK,
            detail="Application is serving requests.",
        ),
        ComponentHealth(
            name="database",
            status=(
                ComponentStatus.NOT_CONFIGURED
                if settings.database_url is None
                else ComponentStatus.UNAVAILABLE
            ),
            detail=(
                "No database configured; persistence lands in phase 1."
                if settings.database_url is None
                else "Database configured but connectivity checks are not implemented yet."
            ),
        ),
        ComponentHealth(
            name="exchange",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No exchange adapter configured; Binance Futures lands in phase 2.",
        ),
        ComponentHealth(
            name="market_data",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="Market-data ingestion lands in phase 2.",
        ),
    ]


@router.get("/health/live", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    return LivenessResponse(status="alive", version=__version__)


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(response: Response) -> ReadinessResponse:
    settings = get_settings()
    components = _check_components(settings)
    # Phase 0 serves analysis-free endpoints only, so an unbuilt dependency
    # must not report the service as ready-for-trading. It is ready to serve
    # what exists; UNAVAILABLE (a real failure) is what blocks readiness.
    ready = all(c.status is not ComponentStatus.UNAVAILABLE for c in components)
    if not ready:
        response.status_code = 503
    return ReadinessResponse(ready=ready, checked_at=utcnow().isoformat(), components=components)
