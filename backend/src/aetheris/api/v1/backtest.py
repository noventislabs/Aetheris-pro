"""Historical simulation endpoints.

`GET`. A backtest computes and returns; it persists nothing, places nothing and
sets no leverage, so it is a read even though it does real work — worth more
than the convenience of a request body.

Phase 6 introduced the system's first writes, but they exist only under
`/paper`, against in-memory simulation state. A backtest stays firmly on the
read side of that line.

Every parameter is a typed, bounded field. The candle count is capped at the
venue's own `/klines` ceiling because this phase does not page.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from aetheris.analysis.backtest.engine import ASSUMPTIONS, MIN_TRADES_FOR_MEANINGFUL_METRICS
from aetheris.analysis.strategies.registry import STRATEGY_KEYS
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.api.deps import BacktestDep
from aetheris.core.errors import ValidationFailedError
from aetheris.domain.backtest import BacktestConfig, BacktestResult
from aetheris.domain.enums import Timeframe

router = APIRouter(prefix="/backtest", tags=["backtest"])

SymbolPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Instrument symbol, e.g. BTCUSDT",
        examples=["BTCUSDT"],
    ),
]


class BacktestMethodResponse(BaseModel):
    """What a simulation assumes, published rather than buried."""

    label: str
    assumptions: list[str]
    not_modelled: list[str]
    min_trades_for_meaningful_metrics: int
    strategies: list[str]
    disclaimer: str


@router.get(
    "/method",
    response_model=BacktestMethodResponse,
    summary="How the simulation is performed, and what it ignores",
)
async def method() -> BacktestMethodResponse:
    """Publish the fill model and the gaps in it.

    A backtest number is only interpretable alongside the assumptions that
    produced it. Serving them from the API means a client can show them next
    to the result rather than linking to documentation nobody opens.
    """
    return BacktestMethodResponse(
        label="HISTORICAL SIMULATION",
        assumptions=list(ASSUMPTIONS),
        not_modelled=[
            "Funding payments on perpetual positions",
            "Partial fills and order-book depth",
            "Maker rebates and fee tiers",
            "Borrow costs",
            "Exchange downtime and outages",
            "Per-symbol lot-size and minimum-notional filters",
            "Maintenance-margin liquidation tiers (a simpler model is used)",
        ],
        min_trades_for_meaningful_metrics=MIN_TRADES_FOR_MEANINGFUL_METRICS,
        strategies=list(STRATEGY_KEYS),
        disclaimer=(
            "A backtest reports what a rule set would have done over bars that already "
            "closed, under the assumptions above. It is NOT a prediction, an expected "
            "return, a probability of profit, or evidence that the rules will work "
            "again. Past results do not imply future results."
        ),
    )


@router.get(
    "/{symbol}",
    response_model=BacktestResult,
    summary="Simulate a strategy over historical candles",
)
async def run_symbol_backtest(
    service: BacktestDep,
    symbol: SymbolPath,
    # Depends(), not Query(): FastAPI will not flatten a query-parameter model
    # when the endpoint also declares sibling query parameters.
    config: Annotated[BacktestConfig, Depends()],
    params: Annotated[TrendMomentumParams, Depends()],
    strategy: Annotated[str, Query(max_length=40, pattern=r"^[a-z_]+$")] = "trend_momentum",
    timeframe: Annotated[Timeframe, Query()] = Timeframe.H1,
    limit: Annotated[
        int, Query(ge=60, le=1500, description="Historical candles to simulate over")
    ] = 500,
) -> BacktestResult:
    """Run a historical simulation and return trades, equity curve and metrics.

    The result is labelled `HISTORICAL SIMULATION` and carries both the
    assumptions the run made and any warnings about reading it — a thin trade
    sample, a position still open at the end, leverage in play.

    `leverage` here is a **simulation input**. It does not pass through the risk
    engine and authorises nothing; the live leverage chain is separate and still
    approves nothing.
    """
    if strategy not in STRATEGY_KEYS:
        raise ValidationFailedError(
            f"Unknown strategy {strategy!r}",
            details={"supported": list(STRATEGY_KEYS)},
        )
    return await service.run(symbol, timeframe, config=config, params=params, candle_limit=limit)
