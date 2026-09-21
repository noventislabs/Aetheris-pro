"""Analysis orchestration.

Joins the read-only market-data layer to the pure analysis engines. This is the
only place in the analysis path that performs I/O; everything below it is
arithmetic over candles.

The freshness contract from phase 2 is carried through intact. Candles arrive
in an ``Observation``, and when that observation has no value -- stale,
unavailable -- no indicator is computed at all. Computing over the last known
bars and labelling the result ``READY`` would be exactly the fabrication the
system forbids.
"""

from __future__ import annotations

from collections.abc import Sequence

from aetheris.adapters.exchange.ports import MarketDataPort
from aetheris.analysis.indicators.engine import calculate_indicators
from aetheris.analysis.indicators.prepare import prepare_candles
from aetheris.analysis.indicators.registry import IndicatorParams
from aetheris.analysis.strategies.registry import DataContext, evaluate
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.core.config import AnalysisSettings
from aetheris.core.freshness import utcnow
from aetheris.domain.enums import Timeframe
from aetheris.domain.indicators import (
    IndicatorResult,
    IndicatorSet,
    IndicatorStatus,
)
from aetheris.domain.strategy import StrategyResult


class AnalysisService:
    """Read-only indicator and strategy analysis over live market data."""

    def __init__(self, exchange: MarketDataPort, settings: AnalysisSettings) -> None:
        self._exchange = exchange
        self._settings = settings

    async def indicators(
        self,
        symbol: str,
        timeframe: Timeframe,
        keys: Sequence[str],
        *,
        params: IndicatorParams | None = None,
        candle_limit: int | None = None,
        series_limit: int = 0,
    ) -> IndicatorSet:
        limit = self._bounded_limit(candle_limit)
        observation = await self._exchange.get_klines(symbol, timeframe, limit=limit)
        now = utcnow()

        if observation.value is None:
            # No candles means no indicators. Each requested indicator is
            # returned as UNAVAILABLE carrying the upstream reason, so the
            # caller learns which data failed rather than that "nothing
            # happened".
            return IndicatorSet(
                symbol=symbol.upper(),
                timeframe=timeframe,
                source=observation.source,
                data_status=observation.status.value,
                data_detail=observation.detail,
                data_age_seconds=observation.age_seconds,
                candle_count=0,
                indicators=tuple(
                    self._unavailable(key, observation.detail or observation.status.value)
                    for key in keys
                ),
            )

        prepared = prepare_candles(observation.value.candles, now)
        if not prepared.ok:
            return IndicatorSet(
                symbol=symbol.upper(),
                timeframe=timeframe,
                source=observation.source,
                data_status=observation.status.value,
                data_detail=prepared.detail,
                data_age_seconds=observation.age_seconds,
                candle_count=0,
                indicators=tuple(
                    self._unavailable(key, prepared.detail or prepared.problem.value)
                    for key in keys
                ),
            )

        results = calculate_indicators(prepared.candles, keys, params, series_limit=series_limit)
        return IndicatorSet(
            symbol=symbol.upper(),
            timeframe=timeframe,
            source=observation.source,
            data_status=observation.status.value,
            data_detail=observation.detail,
            data_age_seconds=observation.age_seconds,
            candle_count=len(prepared.candles),
            last_candle_time=prepared.candles[-1].open_time if prepared.candles else None,
            indicators=results,
        )

    async def strategy(
        self,
        symbol: str,
        timeframe: Timeframe,
        key: str,
        *,
        params: TrendMomentumParams | None = None,
        candle_limit: int | None = None,
    ) -> StrategyResult:
        limit = self._bounded_limit(candle_limit)
        observation = await self._exchange.get_klines(symbol, timeframe, limit=limit)
        now = utcnow()

        context = DataContext(
            symbol=symbol.upper(),
            timeframe=timeframe,
            source=observation.source,
            data_status=observation.status.value,
            age_seconds=observation.age_seconds,
        )
        candles = observation.value.candles if observation.value is not None else ()
        return evaluate(key, candles, context, now, params)

    def _bounded_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._settings.default_candle_limit
        return max(20, min(requested, self._settings.max_candle_limit))

    @staticmethod
    def _unavailable(key: str, detail: str) -> IndicatorResult:
        from aetheris.analysis.indicators.registry import INDICATORS

        spec = INDICATORS.get(key)
        return IndicatorResult(
            indicator=key,
            name=spec.name if spec else key,
            kind=spec.kind if spec else "OSCILLATOR",  # type: ignore[arg-type]
            status=IndicatorStatus.UNAVAILABLE,
            detail=detail,
            value_keys=spec.value_keys if spec else (),
            warmup_bars=0,
            candles_used=0,
        )
