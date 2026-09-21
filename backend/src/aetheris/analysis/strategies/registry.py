"""Strategy catalogue and evaluation.

Two entry points, deliberately separated:

``evaluate_from_candles(candles, params)``
    Pure. Takes bars, returns a verdict. No freshness, no venue, no clock
    beyond what is passed in. **This is what the phase 5 backtester calls**,
    because historical bars have no meaningful "age" and gating on one would
    make every backtested bar unusable.

``evaluate(key, candles, context, params, now)``
    The live path. Wraps the pure evaluation in the freshness gate required of
    anything trading-oriented: stale data, or data whose age cannot be
    verified, yields no bias at all.

Like the indicator registry this is a closed whitelist. A strategy key maps to
a registered implementation or to nothing; there is no dynamic import, no
expression evaluation and no user-supplied rule text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from aetheris.analysis.indicators.prepare import prepare_candles
from aetheris.analysis.leverage import propose_leverage, resolve_leverage
from aetheris.analysis.strategies import trend_momentum
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.domain.enums import Timeframe
from aetheris.domain.leverage import LeverageDecision, LeverageOutcome, LeverageReason
from aetheris.domain.market import Candle
from aetheris.domain.strategy import (
    StrategyBias,
    StrategyDescriptor,
    StrategyResult,
    StrategyStatus,
)

__all__ = [
    "STRATEGY_KEYS",
    "DataContext",
    "describe_strategies",
    "evaluate",
    "evaluate_from_candles",
]

STRATEGY_KEYS: Final[tuple[str, ...]] = (trend_momentum.STRATEGY_KEY,)

#: Every timeframe the market-data layer serves. The rules are scale-free --
#: they read indicator relationships, not absolute durations -- so none is
#: excluded. Shorter frames are noisier, which is a property of the data
#: rather than a limitation of the rules.
SUPPORTED_TIMEFRAMES: Final[tuple[Timeframe, ...]] = tuple(Timeframe)


@dataclass(frozen=True, slots=True)
class DataContext:
    """Provenance of the candles an evaluation is based on."""

    symbol: str
    timeframe: Timeframe
    source: str
    data_status: str
    age_seconds: float | None


def describe_strategies() -> tuple[StrategyDescriptor, ...]:
    """Catalogue for the terminal and for later phases.

    Only registered, implemented strategies appear, and each is marked
    ``available`` only because its evaluation path exists and is tested.
    """
    defaults = TrendMomentumParams()
    return (
        StrategyDescriptor(
            key=trend_momentum.STRATEGY_KEY,
            name=trend_momentum.STRATEGY_NAME,
            version=trend_momentum.STRATEGY_VERSION,
            description=(
                "Reports whether trend, trend strength, momentum and MACD currently "
                "agree on a direction. Analysis only -- not a trade recommendation."
            ),
            required_indicators=trend_momentum.REQUIRED_INDICATORS,
            supported_timeframes=SUPPORTED_TIMEFRAMES,
            parameters=defaults.as_decimals(),
            rules=(
                "LONG_BIAS requires all four: EMA(fast) above EMA(slow); "
                "ADX at or above the minimum; RSI between 50 and the overbought "
                "level; MACD histogram positive.",
                "SHORT_BIAS requires all four mirrored: EMA(fast) below EMA(slow); "
                "ADX at or above the minimum; RSI between the oversold level and 50; "
                "MACD histogram negative.",
                "Anything else, including three conditions out of four, is NEUTRAL.",
                "ADX gates rather than votes: it measures trend strength without "
                "direction, so a weak-trend reading blocks both directions.",
                "No bias is reported at all when candles are stale or their "
                "freshness cannot be verified.",
            ),
            available=True,
        ),
    )


def evaluate_from_candles(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    params: TrendMomentumParams | None = None,
    last_candle_time: datetime | None = None,
) -> StrategyResult:
    """Evaluate the baseline strategy over prepared candles. Pure.

    Callers are responsible for passing closed, ascending, unique bars -- use
    :func:`~aetheris.analysis.indicators.prepare.prepare_candles`.
    """
    resolved = params or TrendMomentumParams()
    base = {
        "strategy": trend_momentum.STRATEGY_KEY,
        "name": trend_momentum.STRATEGY_NAME,
        "version": trend_momentum.STRATEGY_VERSION,
        "symbol": symbol,
        "timeframe": timeframe,
        "parameters": resolved.as_decimals(),
        "indicators_used": trend_momentum.REQUIRED_INDICATORS,
        "candles_used": len(candles),
        "conditions_total": 4,
    }

    values = trend_momentum.compute_indicator_values(candles, resolved)
    if values is None:
        needed = trend_momentum.warmup_bars(resolved)
        return StrategyResult(
            **base,  # type: ignore[arg-type]
            status=StrategyStatus.INSUFFICIENT_DATA,
            detail=(
                f"Needs more than {needed} closed candles for every required "
                f"indicator to warm up; {len(candles)} available"
            ),
        )

    long_conditions, short_conditions, bias = trend_momentum.evaluate_conditions(
        ema_fast=values["ema_fast"],
        ema_slow=values["ema_slow"],
        adx=values["adx"],
        rsi=values["rsi"],
        histogram=values["histogram"],
        params=resolved,
    )
    long_met = sum(1 for c in long_conditions if c.satisfied)
    short_met = sum(1 for c in short_conditions if c.satisfied)

    return StrategyResult(
        **base,  # type: ignore[arg-type]
        status=StrategyStatus.READY,
        bias=bias,
        detail=_summarise(bias, long_conditions, short_conditions),
        long_conditions=long_conditions,
        short_conditions=short_conditions,
        long_conditions_met=long_met,
        short_conditions_met=short_met,
        leverage=_leverage_decision(candles, resolved, bias, max(long_met, short_met)),
        last_candle_time=last_candle_time or (candles[-1].open_time if candles else None),
    )


def _summarise(
    bias: StrategyBias,
    long_conditions: Sequence[object],
    short_conditions: Sequence[object],
) -> str:
    if bias is StrategyBias.LONG_BIAS:
        return "All four long conditions are satisfied."
    if bias is StrategyBias.SHORT_BIAS:
        return "All four short conditions are satisfied."
    long_met = sum(1 for c in long_conditions if getattr(c, "satisfied", False))
    short_met = sum(1 for c in short_conditions if getattr(c, "satisfied", False))
    return (
        f"Conditions conflict: {long_met}/4 long and {short_met}/4 short are "
        "satisfied, so neither rule set fires."
    )


def evaluate(
    key: str,
    candles: Sequence[Candle],
    context: DataContext,
    now: datetime,
    params: TrendMomentumParams | None = None,
) -> StrategyResult:
    """Evaluate a strategy for live analysis, with the freshness gate applied.

    The gate is the difference between this and the pure path. Trading-oriented
    analysis on data that is stale -- or whose age cannot be established -- is
    worse than no analysis, because it looks identical to the real thing.
    """
    resolved = params or TrendMomentumParams()
    base = {
        "strategy": key,
        "name": trend_momentum.STRATEGY_NAME,
        "version": trend_momentum.STRATEGY_VERSION,
        "symbol": context.symbol,
        "timeframe": context.timeframe,
        "parameters": resolved.as_decimals(),
        "indicators_used": trend_momentum.REQUIRED_INDICATORS,
        "conditions_total": 4,
        "source": context.source,
        "data_status": context.data_status,
        "data_age_seconds": context.age_seconds,
        "evaluated_at": now,
    }

    if key not in STRATEGY_KEYS:
        return StrategyResult(
            **base,  # type: ignore[arg-type]
            status=StrategyStatus.UNAVAILABLE,
            detail=f"No strategy registered under {key!r}",
        )

    if context.data_status != "OK":
        return StrategyResult(
            **base,  # type: ignore[arg-type]
            status=StrategyStatus.STALE,
            detail=(
                f"Candle data is {context.data_status}; no bias is reported from "
                "data that is not current"
            ),
        )

    if context.age_seconds is None:
        # Phase 2 rule: OK with no verifiable age means the venue supplied no
        # event timestamp. That is not evidence of freshness, and anything
        # trading-oriented must refuse it.
        return StrategyResult(
            **base,  # type: ignore[arg-type]
            status=StrategyStatus.STALE,
            detail=(
                "Candle freshness could not be verified (the venue supplied no event "
                "timestamp), so the analysis is not usable"
            ),
        )

    prepared = prepare_candles(candles, now)
    if not prepared.ok:
        return StrategyResult(
            **base,  # type: ignore[arg-type]
            status=StrategyStatus.UNAVAILABLE,
            detail=prepared.detail,
        )

    pure = evaluate_from_candles(
        prepared.candles,
        symbol=context.symbol,
        timeframe=context.timeframe,
        params=resolved,
    )
    return pure.model_copy(
        update={
            "source": context.source,
            "data_status": context.data_status,
            "data_age_seconds": context.age_seconds,
            "evaluated_at": now,
        }
    )


def _leverage_decision(
    candles: Sequence[Candle],
    params: TrendMomentumParams,
    bias: StrategyBias,
    conditions_met: int,
) -> LeverageDecision:
    """Produce the leverage constraint chain for this evaluation.

    In this build it always resolves to a rejection, and that is the correct
    answer rather than a limitation being worked around: the venue's per-symbol
    ceiling is unavailable without credentials, and the risk engine that would
    weigh volatility, stop distance, liquidation distance, equity, position
    size and the daily loss limit is phase 7 work. Both facts are reported as
    named reasons.
    """
    if bias is StrategyBias.NEUTRAL:
        return LeverageDecision(
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.NO_DIRECTIONAL_BIAS,
            detail=(
                "No directional bias, so no leverage is requested. Leverage is only "
                "ever a question once a direction is identified."
            ),
        )

    atr_percent = trend_momentum.measure_atr_percent(candles, params)
    request = (
        propose_leverage(
            atr_percent=atr_percent,
            conditions_met=conditions_met,
            conditions_total=4,
        )
        if atr_percent is not None
        else None
    )
    return resolve_leverage(
        request,
        # Null, not guessed: leverage brackets need an authenticated endpoint.
        exchange_max_leverage=None,
        risk_max_leverage=None,
        # Phase 7. Until then nothing can be approved, by design.
        risk_engine_available=False,
    )
