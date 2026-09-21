"""Read-only indicator and strategy analysis endpoints.

Every route is a `GET`. Nothing here places, amends or cancels an order, and
the service it depends on reaches the venue through `MarketDataPort`, which has
no execution methods.

Indicator selection is a closed whitelist and parameters are typed, bounded
fields. There is no expression, formula string, callable name or `eval`
anywhere in this path -- the only thing a caller can choose is *which*
registered indicator to run and, within published ranges, with what periods.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from aetheris.analysis.indicators.registry import (
    INDICATOR_KEYS,
    MAX_INDICATORS_PER_REQUEST,
    IndicatorParams,
    describe_indicators,
)
from aetheris.analysis.setup import SetupParams
from aetheris.analysis.strategies.registry import STRATEGY_KEYS, describe_strategies
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.api.deps import AnalysisDep
from aetheris.core.errors import ValidationFailedError
from aetheris.domain.enums import Timeframe
from aetheris.domain.indicators import IndicatorDescriptor, IndicatorSet
from aetheris.domain.setup import StopModel, TradeSetup
from aetheris.domain.strategy import StrategyDescriptor, StrategyResult

router = APIRouter(prefix="/analysis", tags=["analysis"])

#: Same constraint the markets router uses: venue tickers are uppercase
#: alphanumerics. The value is then checked against the discovered universe
#: before any upstream call, so an unknown symbol is a 404 not a venue error.
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


class IndicatorCatalogue(BaseModel):
    """What this build can actually calculate."""

    indicators: list[IndicatorDescriptor]
    max_per_request: int
    note: str


class StrategyCatalogue(BaseModel):
    strategies: list[StrategyDescriptor]
    disclaimer: str


def _parse_indicator_keys(raw: str) -> list[str]:
    """Split and whitelist the requested indicator keys.

    An unknown key is a 422 rather than a silent omission: a caller asking for
    ``rsi,mcad`` should be told about the typo, not handed a response that
    quietly lacks a line they are about to plot.
    """
    keys = [piece.strip().lower() for piece in raw.split(",") if piece.strip()]
    if not keys:
        raise ValidationFailedError("At least one indicator must be requested")
    if len(keys) > MAX_INDICATORS_PER_REQUEST:
        raise ValidationFailedError(
            f"At most {MAX_INDICATORS_PER_REQUEST} indicators may be requested at "
            f"once; {len(keys)} were given"
        )
    unknown = [key for key in keys if key not in INDICATOR_KEYS]
    if unknown:
        raise ValidationFailedError(
            f"Unknown indicator(s): {', '.join(sorted(unknown))}",
            details={"supported": list(INDICATOR_KEYS)},
        )
    # Preserve request order but drop repeats, so "rsi,rsi" costs one.
    deduplicated: dict[str, None] = {}
    for key in keys:
        deduplicated.setdefault(key, None)
    return list(deduplicated)


@router.get(
    "/indicators",
    response_model=IndicatorCatalogue,
    summary="Indicators this build can calculate",
)
async def indicator_catalogue() -> IndicatorCatalogue:
    """Publish the catalogue, including each indicator's convention.

    The terminal reads this rather than hardcoding a list, so an indicator can
    never appear in the UI before its implementation exists.
    """
    return IndicatorCatalogue(
        indicators=list(describe_indicators()),
        max_per_request=MAX_INDICATORS_PER_REQUEST,
        note=(
            "Every indicator states the convention it follows. Values are computed "
            "from closed candles only and are never extrapolated; a bar inside the "
            "warm-up window reports WARMING_UP rather than a provisional number."
        ),
    )


@router.get(
    "/strategies",
    response_model=StrategyCatalogue,
    summary="Registered strategies",
)
async def strategy_catalogue() -> StrategyCatalogue:
    return StrategyCatalogue(
        strategies=list(describe_strategies()),
        disclaimer=(
            "Strategy analysis is a deterministic reading of indicator values. It is "
            "NOT a trade recommendation, a prediction of future price, a probability "
            "of profit, a win rate, or financial advice. No order is placed by this "
            "system in any mode."
        ),
    )


@router.get(
    "/{symbol}/indicators",
    response_model=IndicatorSet,
    summary="Calculate indicators over live candles",
)
async def symbol_indicators(
    service: AnalysisDep,
    symbol: SymbolPath,
    # Depends(), not Query(): FastAPI will not flatten a query-parameter
    # model when the endpoint also declares sibling query parameters.
    params: Annotated[IndicatorParams, Depends()],
    indicators: Annotated[
        str,
        Query(
            max_length=200,
            description="Comma-separated indicator keys, e.g. ema,rsi,macd",
            examples=["ema,rsi,macd"],
        ),
    ] = "ema,rsi,macd",
    timeframe: Annotated[Timeframe, Query()] = Timeframe.H1,
    limit: Annotated[int, Query(ge=20, le=1000, description="Candles to analyse")] = 300,
    series_points: Annotated[
        int,
        Query(
            ge=0,
            le=500,
            description="Trailing indicator points to return; 0 returns only the latest",
        ),
    ] = 0,
) -> IndicatorSet:
    """Compute a bounded set of indicators for one symbol and timeframe.

    Values come from closed candles only. A bar still inside an indicator's
    warm-up window reports ``WARMING_UP`` and carries no value — there is no
    provisional or partial number, and nothing is extrapolated.
    """
    keys = _parse_indicator_keys(indicators)
    return await service.indicators(
        symbol,
        timeframe,
        keys,
        params=params,
        candle_limit=limit,
        series_limit=series_points,
    )


@router.get(
    "/{symbol}/indicators/{indicator}",
    response_model=IndicatorSet,
    summary="Calculate one indicator over live candles",
)
async def symbol_indicator(
    service: AnalysisDep,
    symbol: SymbolPath,
    # Depends(), not Query(): FastAPI will not flatten a query-parameter
    # model when the endpoint also declares sibling query parameters.
    params: Annotated[IndicatorParams, Depends()],
    indicator: Annotated[str, Path(max_length=32, pattern=r"^[a-z_]+$")],
    timeframe: Annotated[Timeframe, Query()] = Timeframe.H1,
    limit: Annotated[int, Query(ge=20, le=1000)] = 300,
    series_points: Annotated[int, Query(ge=0, le=500)] = 0,
) -> IndicatorSet:
    """Single-indicator form, for a chart pane that needs one line."""
    keys = _parse_indicator_keys(indicator)
    return await service.indicators(
        symbol,
        timeframe,
        keys,
        params=params,
        candle_limit=limit,
        series_limit=series_points,
    )


@router.get(
    "/{symbol}/strategy",
    response_model=StrategyResult,
    summary="Evaluate a strategy — analysis only, never an instruction",
)
async def symbol_strategy(
    service: AnalysisDep,
    symbol: SymbolPath,
    params: Annotated[TrendMomentumParams, Depends()],
    strategy: Annotated[str, Query(max_length=40, pattern=r"^[a-z_]+$")] = "trend_momentum",
    timeframe: Annotated[Timeframe, Query()] = Timeframe.H1,
    limit: Annotated[int, Query(ge=20, le=1000)] = 300,
) -> StrategyResult:
    """Report whether a rule set's conditions currently agree on a direction.

    The result is an **analysis**, not a recommendation: `LONG_BIAS` says four
    named conditions are satisfied right now, and says nothing about what price
    will do next. Every condition is returned with the value it measured, so
    the verdict can be checked rather than trusted.

    No bias is reported at all when the underlying candles are stale, or when
    their freshness cannot be verified.
    """
    if strategy not in STRATEGY_KEYS:
        raise ValidationFailedError(
            f"Unknown strategy {strategy!r}",
            details={"supported": list(STRATEGY_KEYS)},
        )
    return await service.strategy(symbol, timeframe, strategy, params=params, candle_limit=limit)


@router.get(
    "/{symbol}/setup",
    response_model=TradeSetup,
    summary="Score the current setup — analysis only, never an instruction",
)
async def symbol_setup(
    service: AnalysisDep,
    symbol: SymbolPath,
    params: Annotated[TrendMomentumParams, Depends()],
    timeframe: Annotated[Timeframe, Query()] = Timeframe.H1,
    limit: Annotated[int, Query(ge=20, le=1000)] = 300,
    stop_model: Annotated[StopModel, Query()] = StopModel.ATR,
    stop_percent: Annotated[Decimal, Query(gt=0, lt=90)] = Decimal(2),
    atr_multiple: Annotated[Decimal, Query(gt=0, le=10)] = Decimal("1.5"),
    structure_lookback: Annotated[int, Query(ge=2, le=500)] = 20,
    take_profit_r: Annotated[Decimal, Query(gt=0, le=20)] = Decimal(2),
) -> TradeSetup:
    """Report the current setup for one instrument: direction, score and levels.

    **The score is strategy alignment on a 0-100 scale, not a probability of
    profit.** It measures how completely present measurements satisfy the
    published rule set and whether the resulting trade is worth its own risk.
    Nothing in this system produces a calibrated probability, and no field here
    should be read as one. Historical performance is not folded in; that lives
    in `/backtest/{symbol}` and is reported separately on purpose.

    Every component carries the raw measurement behind it, so the total can be
    recomputed by hand rather than trusted.

    Four outcomes are possible and they are distinct:

    * `ACTIONABLE` — a direction was found and real levels were derived.
    * `NO_ACTIONABLE_SETUP` — evaluated fine, but the rules are NEUTRAL or no
      safe stop exists under the chosen model. No level is invented to fill it.
    * `INSUFFICIENT_DATA` — an indicator has not warmed up over the candles
      requested. No value is substituted for a missing one.
    * `STALE` — the candles are not current, or their age cannot be verified.

    This route places no order. Any setup it reports remains a *proposal* to
    the risk engine, which has final authority and can refuse it regardless of
    score.
    """
    return await service.setup(
        symbol,
        timeframe,
        params=params,
        setup_params=SetupParams(
            stop_model=stop_model,
            stop_percent=stop_percent,
            atr_multiple=atr_multiple,
            structure_lookback=structure_lookback,
            take_profit_r=take_profit_r,
        ),
        candle_limit=limit,
    )
