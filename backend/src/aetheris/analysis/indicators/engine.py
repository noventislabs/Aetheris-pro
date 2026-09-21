"""Indicator engine.

The single pure entry point for indicator calculation:

``calculate_indicators(candles, keys, params) -> tuple[IndicatorResult, ...]``

It takes normalized candles and returns deterministic results. It imports no
FastAPI, no HTTP client, no venue adapter, no database and no settings, so the
phase 5 backtester and the phase 6 paper engine can call exactly this function
over historical bars without standing up any of that machinery. The
architecture test enforces the absence of those imports.
"""

from __future__ import annotations

from collections.abc import Sequence

from aetheris.analysis.indicators.registry import (
    INDICATORS,
    IndicatorParams,
    IndicatorSpec,
)
from aetheris.domain.indicators import (
    IndicatorPoint,
    IndicatorResult,
    IndicatorStatus,
)
from aetheris.domain.market import Candle

__all__ = ["DEFAULT_SERIES_LIMIT", "MAX_SERIES_LIMIT", "calculate_indicators"]

#: Series output is opt-in and bounded: a 1500-bar MACD is 4500 numbers, and
#: sending that per indicator per request would dwarf the rest of the payload.
DEFAULT_SERIES_LIMIT = 0
MAX_SERIES_LIMIT = 500


def _build_result(
    spec: IndicatorSpec,
    candles: Sequence[Candle],
    params: IndicatorParams,
    series_limit: int,
) -> IndicatorResult:
    warmup = spec.warmup(params)
    used = spec.used_parameters(params)
    count = len(candles)

    if count <= warmup:
        # Not "return zeros and hope": this parameter set cannot produce a
        # single value from this many bars, and saying so is the answer.
        return IndicatorResult(
            indicator=spec.key,
            name=spec.name,
            kind=spec.kind,
            status=IndicatorStatus.INSUFFICIENT_DATA,
            detail=(f"{spec.name} needs more than {warmup} closed candles; {count} available"),
            parameters=used,
            value_keys=spec.value_keys,
            warmup_bars=warmup,
            candles_used=count,
        )

    lines = spec.compute(candles, params)
    last = count - 1
    latest = {key: lines[key][last] for key in spec.value_keys}
    complete = all(value is not None for value in latest.values())

    if complete:
        status = IndicatorStatus.READY
        detail = None
    else:
        status = IndicatorStatus.WARMING_UP
        missing = sorted(key for key, value in latest.items() if value is None)
        any_history = any(any(value is not None for value in lines[key]) for key in spec.value_keys)
        detail = (
            # Two different causes, distinguished rather than conflated: still
            # inside the warm-up window, versus mathematically undefined at
            # this particular bar (a flat window has no stochastic position).
            f"{', '.join(missing)} undefined at the latest candle"
            if any_history
            else f"{spec.name} is still warming up ({warmup} bars required)"
        )

    series: tuple[IndicatorPoint, ...] = ()
    if series_limit > 0:
        start = max(0, count - series_limit)
        series = tuple(
            IndicatorPoint(
                time=candles[index].open_time,
                values={key: lines[key][index] for key in spec.value_keys},
            )
            for index in range(start, count)
        )

    return IndicatorResult(
        indicator=spec.key,
        name=spec.name,
        kind=spec.kind,
        status=status,
        detail=detail,
        parameters=used,
        value_keys=spec.value_keys,
        latest=latest if status is IndicatorStatus.READY else None,
        latest_time=candles[last].open_time if status is IndicatorStatus.READY else None,
        warmup_bars=warmup,
        candles_used=count,
        series=series,
    )


def calculate_indicators(
    candles: Sequence[Candle],
    keys: Sequence[str],
    params: IndicatorParams | None = None,
    *,
    series_limit: int = DEFAULT_SERIES_LIMIT,
) -> tuple[IndicatorResult, ...]:
    """Compute the named indicators over ``candles``.

    ``candles`` must already be closed bars in ascending time order --
    :func:`aetheris.analysis.indicators.prepare.prepare_candles` produces
    exactly that, and is where ordering and duplicate handling live.

    Unknown keys are skipped rather than raising: the API validates the
    whitelist before calling, and a silent skip here is preferable to a partial
    result set being lost to one typo in a batch.
    """
    resolved = params or IndicatorParams()
    bounded_limit = max(0, min(series_limit, MAX_SERIES_LIMIT))
    results: list[IndicatorResult] = []
    for key in keys:
        spec = INDICATORS.get(key)
        if spec is None:
            continue
        results.append(_build_result(spec, candles, resolved, bounded_limit))
    return tuple(results)
