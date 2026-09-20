"""System status, trading-mode posture and capability disclosure."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

from aetheris import __version__
from aetheris.core.capabilities import CAPABILITIES, Capability
from aetheris.core.config import get_settings
from aetheris.core.freshness import utcnow
from aetheris.domain.enums import TradingMode

router = APIRouter(tags=["system"])


class ModeState(BaseModel):
    mode: TradingMode
    enabled: bool
    places_real_orders: bool
    risks_real_funds: bool


class RiskDefaultsView(BaseModel):
    paper_starting_balance: Decimal
    daily_profit_target: Decimal
    daily_loss_limit: Decimal
    max_leverage: Decimal
    max_open_positions: int


class SystemStatusResponse(BaseModel):
    name: str = "Aetheris Pro"
    version: str
    environment: str
    server_time: str
    default_mode: TradingMode
    autonomous_trading_enabled: bool
    modes: list[ModeState]
    risk_defaults: RiskDefaultsView


class CapabilitiesResponse(BaseModel):
    capabilities: list[Capability]


@router.get("/system/status", response_model=SystemStatusResponse, summary="System status")
async def system_status() -> SystemStatusResponse:
    settings = get_settings()
    return SystemStatusResponse(
        version=__version__,
        environment=settings.environment,
        server_time=utcnow().isoformat(),
        default_mode=settings.default_mode,
        autonomous_trading_enabled=settings.autonomous_trading_enabled,
        modes=[
            ModeState(
                mode=mode,
                enabled=settings.is_mode_enabled(mode),
                places_real_orders=mode.places_real_orders,
                risks_real_funds=mode.risks_real_funds,
            )
            for mode in TradingMode
        ],
        risk_defaults=RiskDefaultsView(
            paper_starting_balance=settings.risk.paper_starting_balance,
            daily_profit_target=settings.risk.daily_profit_target,
            daily_loss_limit=settings.risk.daily_loss_limit,
            max_leverage=settings.risk.max_leverage,
            max_open_positions=settings.risk.max_open_positions,
        ),
    )


@router.get(
    "/system/capabilities",
    response_model=CapabilitiesResponse,
    summary="What this build can actually do",
)
async def capabilities() -> CapabilitiesResponse:
    """Expose the capability registry verbatim.

    Clients (and Falcon) must render PLANNED capabilities as unavailable rather
    than hiding them, so the gap between intent and implementation stays
    visible instead of being quietly filled in.
    """
    return CapabilitiesResponse(capabilities=list(CAPABILITIES))
