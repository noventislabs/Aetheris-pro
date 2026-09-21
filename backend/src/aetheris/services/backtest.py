"""Backtest orchestration.

The only I/O in the backtest path: fetch history, normalise it, hand it to the
pure engine. Everything below this is arithmetic.

Unlike live analysis, a backtest does **not** gate on candle freshness. It is a
study of bars that already closed, and refusing to simulate because the newest
bar is a few minutes old would be applying a live-trading rule to history. What
it does still refuse is *unusable* data -- an unavailable series, or one whose
ordering cannot be established.
"""

from __future__ import annotations

from aetheris.adapters.exchange.ports import MarketDataPort
from aetheris.analysis.backtest.engine import run_backtest
from aetheris.analysis.indicators.prepare import prepare_candles
from aetheris.analysis.strategies import trend_momentum
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.core.config import BacktestSettings
from aetheris.core.freshness import utcnow
from aetheris.domain.backtest import BacktestConfig, BacktestResult, BacktestStatus
from aetheris.domain.enums import Timeframe


class BacktestService:
    """Run historical simulations over venue candle history."""

    def __init__(self, exchange: MarketDataPort, settings: BacktestSettings) -> None:
        self._exchange = exchange
        self._settings = settings

    async def run(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        config: BacktestConfig | None = None,
        params: TrendMomentumParams | None = None,
        candle_limit: int | None = None,
    ) -> BacktestResult:
        limit = (
            self._settings.default_candle_limit
            if candle_limit is None
            else max(60, min(candle_limit, self._settings.max_candle_limit))
        )
        observation = await self._exchange.get_klines(symbol, timeframe, limit=limit)
        now = utcnow()

        settings = config or BacktestConfig()
        base = {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "strategy": trend_momentum.STRATEGY_KEY,
            "strategy_version": trend_momentum.STRATEGY_VERSION,
            "config": settings,
            "source": observation.source,
            "data_status": observation.status.value,
            "ran_at": now,
        }

        # A stale series is still perfectly good history. Only an absent or
        # unusable one stops the simulation.
        if observation.value is None and observation.status.value in {"UNAVAILABLE", "ERROR"}:
            return BacktestResult(
                **base,  # type: ignore[arg-type]
                status=BacktestStatus.UNAVAILABLE,
                detail=observation.detail or "Candles are unavailable",
            )

        candles = observation.value.candles if observation.value is not None else ()
        if not candles:
            return BacktestResult(
                **base,  # type: ignore[arg-type]
                status=BacktestStatus.UNAVAILABLE,
                detail=(
                    observation.detail or "No candles were returned for this symbol and timeframe"
                ),
            )

        prepared = prepare_candles(candles, now)
        if not prepared.ok:
            return BacktestResult(
                **base,  # type: ignore[arg-type]
                status=BacktestStatus.UNAVAILABLE,
                detail=prepared.detail,
            )

        result = run_backtest(
            prepared.candles,
            symbol=symbol,
            timeframe=timeframe,
            config=settings,
            params=params,
            ran_at=now,
        )
        return result.model_copy(
            update={"source": observation.source, "data_status": observation.status.value}
        )
