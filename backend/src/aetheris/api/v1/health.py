"""Liveness and readiness (spec section 34).

Liveness answers "is this process alive"; readiness answers "should traffic be
sent here". They are deliberately different: a database outage makes the
service unready but not dead, and conflating them causes restart loops that
make an outage worse.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from aetheris import __version__
from aetheris.adapters.persistence.engine import check_connectivity
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


async def _database_health(settings: Settings, engine: object | None) -> ComponentHealth:
    """Actually connect, rather than reporting on whether a string is set.

    Three outcomes, and the difference between the last two is the whole point
    of reporting at all:

    - ``NOT_CONFIGURED`` -- no URL. We never had a database.
    - ``OK`` -- a connection was opened and answered this instant.
    - ``UNAVAILABLE`` -- configured and unreachable. We had it and lost it,
      which is the only one of the three that is an incident.

    The detail names the failure class and never the connection string: this
    body is served to a browser.
    """
    if settings.database_url is None:
        return ComponentHealth(
            name="database",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No database configured; set AETHERIS_DATABASE_URL to enable persistence.",
        )
    if engine is None:
        return ComponentHealth(
            name="database",
            status=ComponentStatus.UNAVAILABLE,
            detail="Database configured but the connection pool failed to start.",
        )
    reachable, detail = await check_connectivity(engine)  # type: ignore[arg-type]
    return ComponentHealth(
        name="database",
        status=ComponentStatus.OK if reachable else ComponentStatus.UNAVAILABLE,
        detail=detail,
    )


def _check_components(settings: Settings, database: ComponentHealth) -> list[ComponentHealth]:
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
        database,
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
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    database = await _database_health(settings, getattr(request.app.state, "db_engine", None))
    components = _check_components(settings, database)
    # Phase 0 serves analysis-free endpoints only, so an unbuilt dependency
    # must not report the service as ready-for-trading. It is ready to serve
    # what exists; UNAVAILABLE (a real failure) is what blocks readiness.
    ready = all(c.status is not ComponentStatus.UNAVAILABLE for c in components)
    if not ready:
        response.status_code = 503
    return ReadinessResponse(ready=ready, checked_at=utcnow().isoformat(), components=components)
