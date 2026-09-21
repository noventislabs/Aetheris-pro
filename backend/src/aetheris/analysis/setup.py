"""Setup construction: rules, regime and risk/reward into one scored reading.

Pure. Candles in, a ``TradeSetup`` out. No HTTP, no venue, no settings, no
account state -- so the same function runs over a fixture, over live history,
and inside the backtest loop without behaving differently.

## The pipeline

```
candles -> indicators -> strategy conditions -> direction
                      -> market regime
                      -> stop / target        -> risk/reward
                      -> setup score
```

Direction comes from the existing published ``trend_momentum`` rule set. This
module does not invent a second opinion about direction: if the strategy says
NEUTRAL, the setup is ``NO_ACTIONABLE_SETUP``. Scoring a direction the rule set
did not call would make the score disagree with the rules it claims to measure.

## The weights, and why these

The score is a weighted sum of six components, each normalized to 0-1, summing
to 100. The weights are chosen from what the existing architecture actually
knows, not from a template:

``trend_alignment`` -- **0.25.** The rule set's own primary axis: EMA
direction plus the ADX gate that says direction is readable at all.

``momentum_alignment`` -- **0.20.** The rule set's second axis: RSI band and
MACD histogram. Together with trend this is 0.45, because these four
conditions *are* the published strategy: the score must not disagree with the
bias it is scoring.

``regime_agreement`` -- **0.20.** Independent corroboration, computed from
different arithmetic. Weighted level with momentum because a setup the regime
contradicts deserves to score materially lower even when every rule passes.

``risk_reward`` -- **0.20.** A perfectly aligned setup with a 0.5R target is
not a good setup. This is the only component that can see whether the trade
is worth its own risk.

``volatility_fitness`` -- **0.10.** A filter, not a thesis: too quiet and the
target is unreachable, too violent and the stop sits inside noise.

``volume_confirmation`` -- **0.05.** Deliberately the smallest. The baseline
rule set does not read volume at all, so weighting it heavily would let the
score drift away from the rules it claims to measure.

**"Historical stability" is deliberately excluded.** The obvious sixth factor
would be past performance, and folding it in is exactly the merge that turns a
present-tense alignment score into something that reads like a probability.
Backtest metrics are reported separately, and a caller that wants both gets
both, unmixed.

## Normalization references

``RR_REFERENCE = 3`` -- a 3R target scores full marks on the risk/reward axis.
``VOLUME_REFERENCE = Decimal("1.5")`` -- a bar at 1.5x its trailing baseline
reads as fully confirmed. Both are judgement calls, stated here and versioned
in ``SETUP_METHOD`` so a stored score is never compared against one computed
from different constants.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aetheris.analysis.regime import (
    ATR_HIGH_PERCENT,
    ATR_LOW_PERCENT,
    RegimeParams,
    classify_regime,
)
from aetheris.analysis.strategies import trend_momentum
from aetheris.analysis.strategies.registry import DataContext
from aetheris.analysis.strategies.trend_momentum import TrendMomentumParams
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Candle
from aetheris.domain.regime import MarketRegime, RegimeAssessment
from aetheris.domain.setup import (
    RiskReward,
    SetupDirection,
    SetupScore,
    SetupScoreComponent,
    SetupStatus,
    StopModel,
    TradeSetup,
)
from aetheris.domain.strategy import ConditionOutcome, StrategyBias

__all__ = [
    "RR_REFERENCE",
    "SETUP_METHOD",
    "VOLUME_REFERENCE",
    "WEIGHTS",
    "SetupParams",
    "build_setup",
    "derive_risk_reward",
    "evaluate_setup",
]

#: Bump when any weight, reference or rule below changes.
SETUP_METHOD: Final = "setup-score/v1"

WEIGHTS: Final[dict[str, Decimal]] = {
    "trend_alignment": Decimal("0.25"),
    "momentum_alignment": Decimal("0.20"),
    "regime_agreement": Decimal("0.20"),
    "risk_reward": Decimal("0.20"),
    "volatility_fitness": Decimal("0.10"),
    "volume_confirmation": Decimal("0.05"),
}

RR_REFERENCE: Final = Decimal(3)
VOLUME_REFERENCE: Final = Decimal("1.5")

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)
_CENT: Final = Decimal("0.01")

#: Trailing bars the volume baseline averages over, excluding the bar itself.
VOLUME_BASELINE_BARS: Final = 20


class SetupParams:
    """How stops and targets are derived. Validated on construction."""

    __slots__ = (
        "atr_multiple",
        "stop_model",
        "stop_percent",
        "structure_lookback",
        "take_profit_r",
    )

    def __init__(
        self,
        *,
        stop_model: StopModel = StopModel.ATR,
        stop_percent: Decimal = Decimal(2),
        atr_multiple: Decimal = Decimal("1.5"),
        structure_lookback: int = 20,
        take_profit_r: Decimal = Decimal(2),
    ) -> None:
        if stop_percent <= 0 or stop_percent >= 90:
            raise ValueError("stop_percent must be in (0, 90)")
        if atr_multiple <= 0 or atr_multiple > 10:
            raise ValueError("atr_multiple must be in (0, 10]")
        if structure_lookback < 2 or structure_lookback > 500:
            raise ValueError("structure_lookback must be in [2, 500]")
        if take_profit_r <= 0 or take_profit_r > 20:
            raise ValueError("take_profit_r must be in (0, 20]")
        self.stop_model = stop_model
        self.stop_percent = stop_percent
        self.atr_multiple = atr_multiple
        self.structure_lookback = structure_lookback
        self.take_profit_r = take_profit_r


def _clamp_unit(value: Decimal) -> Decimal:
    if value < _ZERO:
        return _ZERO
    return _ONE if value > _ONE else value


def _round_cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _component(
    name: str, normalized: Decimal, raw: Decimal | None, detail: str
) -> SetupScoreComponent:
    weight = WEIGHTS[name]
    bounded = _clamp_unit(normalized)
    return SetupScoreComponent(
        name=name,
        raw_value=raw,
        normalized=bounded,
        weight=weight,
        contribution=_round_cent(bounded * weight * _HUNDRED),
        detail=detail,
    )


def _atr_percent(assessment: RegimeAssessment) -> Decimal | None:
    for measurement in assessment.measurements:
        if measurement.name == "volatility":
            return measurement.value
    return None


def _structure_level(
    candles: Sequence[Candle], *, lookback: int, direction: SetupDirection
) -> Decimal | None:
    """The most recent swing extreme over the lookback, excluding the last bar.

    The last bar is excluded on purpose. Using it would place the stop at the
    extreme of the very candle that produced the signal, which in a backtest
    is indistinguishable from placing it where the loss already happened.
    """
    window = candles[-(lookback + 1) : -1]
    if not window:
        return None
    if direction is SetupDirection.LONG:
        return min(candle.low for candle in window)
    return max(candle.high for candle in window)


def derive_risk_reward(
    candles: Sequence[Candle],
    *,
    direction: SetupDirection,
    params: SetupParams,
    atr_percent: Decimal | None,
) -> RiskReward | None:
    """Turn a direction into real levels, or return ``None``.

    ``None`` means no safe stop or target could be derived, and the caller
    must report ``NO_ACTIONABLE_SETUP``. It never means "use a default": a
    fabricated stop is the one error in this module that would reach money.
    """
    if direction is SetupDirection.NO_SIGNAL or not candles:
        return None
    entry = candles[-1].close
    if entry <= 0:
        return None

    stop: Decimal | None
    if params.stop_model is StopModel.FIXED_PERCENT:
        offset = entry * params.stop_percent / _HUNDRED
        stop = entry - offset if direction is SetupDirection.LONG else entry + offset
    elif params.stop_model is StopModel.ATR:
        if atr_percent is None or atr_percent <= 0:
            return None
        offset = entry * (atr_percent * params.atr_multiple) / _HUNDRED
        stop = entry - offset if direction is SetupDirection.LONG else entry + offset
    else:
        stop = _structure_level(candles, lookback=params.structure_lookback, direction=direction)

    if stop is None or stop <= 0:
        return None

    # The stop must be on the losing side of entry. A structure level can fail
    # this legitimately -- a long whose recent swing low sits above the current
    # close has no stop under this model, and that is a real answer.
    if direction is SetupDirection.LONG and stop >= entry:
        return None
    if direction is SetupDirection.SHORT and stop <= entry:
        return None

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    reward = risk * params.take_profit_r
    target = entry + reward if direction is SetupDirection.LONG else entry - reward
    if target <= 0:
        return None

    return RiskReward(
        entry_price=entry,
        stop_price=stop,
        take_profit_price=target,
        risk_per_unit=risk,
        reward_per_unit=reward,
        risk_reward_ratio=reward / risk,
        stop_model=params.stop_model,
        stop_distance_percent=risk / entry * _HUNDRED,
        take_profit_r_multiple=params.take_profit_r,
    )


def _volume_ratio(candles: Sequence[Candle]) -> Decimal | None:
    """Last bar's volume against the mean of the bars before it.

    Strictly trailing: the baseline never includes the bar being judged, and
    never includes anything after it.
    """
    if len(candles) < 2:
        return None
    window = candles[-(VOLUME_BASELINE_BARS + 1) : -1]
    if not window:
        return None
    total = sum((candle.volume for candle in window), _ZERO)
    if total <= 0:
        return None
    baseline = total / Decimal(len(window))
    if baseline <= 0:
        return None
    return candles[-1].volume / baseline


def _regime_agreement(regime: MarketRegime, direction: SetupDirection) -> tuple[Decimal, str]:
    """How well the independently computed regime corroborates the direction."""
    if regime is MarketRegime.UNKNOWN:
        return _ZERO, "Regime could not be classified, so it corroborates nothing."
    aligned = (regime is MarketRegime.TREND_UP and direction is SetupDirection.LONG) or (
        regime is MarketRegime.TREND_DOWN and direction is SetupDirection.SHORT
    )
    if aligned:
        return _ONE, f"Regime {regime.value} agrees with a {direction.value} setup."
    opposed = (regime is MarketRegime.TREND_DOWN and direction is SetupDirection.LONG) or (
        regime is MarketRegime.TREND_UP and direction is SetupDirection.SHORT
    )
    if opposed:
        return _ZERO, f"Regime {regime.value} directly opposes a {direction.value} setup."
    # RANGE, HIGH_VOLATILITY, LOW_VOLATILITY: no directional opinion either way.
    return (
        Decimal("0.5"),
        f"Regime {regime.value} is non-directional, so it neither confirms nor opposes.",
    )


def _volatility_fitness(atr_percent: Decimal | None) -> tuple[Decimal, str]:
    """Full marks inside the workable band, tapering outside it.

    Below the band a target is unlikely to be reached before the thesis
    expires; above it the stop sits inside ordinary noise.
    """
    if atr_percent is None or atr_percent <= 0:
        return _ZERO, "ATR could not be measured, so volatility fitness is unscored."
    if atr_percent < ATR_LOW_PERCENT:
        return (
            atr_percent / ATR_LOW_PERCENT,
            f"ATR {atr_percent:.4f}% is below the {ATR_LOW_PERCENT}% floor: little room to move.",
        )
    if atr_percent > ATR_HIGH_PERCENT:
        excess = (atr_percent - ATR_HIGH_PERCENT) / ATR_HIGH_PERCENT
        return (
            _clamp_unit(_ONE - excess),
            f"ATR {atr_percent:.4f}% is above the {ATR_HIGH_PERCENT}% ceiling: a stop here "
            f"sits inside ordinary noise.",
        )
    return (
        _ONE,
        f"ATR {atr_percent:.4f}% sits inside the workable "
        f"{ATR_LOW_PERCENT}%-{ATR_HIGH_PERCENT}% band.",
    )


def _score(
    *,
    conditions: Sequence[ConditionOutcome],
    regime: RegimeAssessment,
    direction: SetupDirection,
    risk_reward: RiskReward,
    atr_percent: Decimal | None,
    volume_ratio: Decimal | None,
) -> SetupScore:
    by_name = {condition.name: condition for condition in conditions}

    trend_hits = sum(
        1 for key in ("trend", "trend_strength") if by_name.get(key) and by_name[key].satisfied
    )
    trend_norm = Decimal(trend_hits) / Decimal(2)

    momentum_hits = sum(
        1 for key in ("momentum", "macd") if by_name.get(key) and by_name[key].satisfied
    )
    momentum_norm = Decimal(momentum_hits) / Decimal(2)

    regime_norm, regime_detail = _regime_agreement(regime.regime, direction)
    volatility_norm, volatility_detail = _volatility_fitness(atr_percent)
    rr_norm = _clamp_unit(risk_reward.risk_reward_ratio / RR_REFERENCE)

    if volume_ratio is None:
        volume_norm = _ZERO
        volume_detail = "Volume baseline unavailable, so this component contributes nothing."
    else:
        volume_norm = _clamp_unit(volume_ratio / VOLUME_REFERENCE)
        volume_detail = (
            f"Last bar's volume is {volume_ratio:.3f}x its {VOLUME_BASELINE_BARS}-bar "
            f"trailing baseline; {VOLUME_REFERENCE}x reads as fully confirmed."
        )

    components = (
        _component(
            "trend_alignment",
            trend_norm,
            Decimal(trend_hits),
            f"{trend_hits} of 2 trend rules satisfied (direction and ADX gate).",
        ),
        _component(
            "momentum_alignment",
            momentum_norm,
            Decimal(momentum_hits),
            f"{momentum_hits} of 2 momentum rules satisfied (RSI band and MACD histogram).",
        ),
        _component("regime_agreement", regime_norm, None, regime_detail),
        _component(
            "risk_reward",
            rr_norm,
            risk_reward.risk_reward_ratio,
            f"Risk/reward {risk_reward.risk_reward_ratio:.3f} against a {RR_REFERENCE}R "
            f"reference for full marks.",
        ),
        _component("volatility_fitness", volatility_norm, atr_percent, volatility_detail),
        _component("volume_confirmation", volume_norm, volume_ratio, volume_detail),
    )

    total = sum((component.contribution for component in components), _ZERO)
    return SetupScore(value=_round_cent(total), components=components, method=SETUP_METHOD)


def build_setup(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: Timeframe,
    strategy_params: TrendMomentumParams | None = None,
    setup_params: SetupParams | None = None,
    data_source: str | None = None,
    data_status: str | None = None,
    data_age_seconds: float | None = None,
    evaluated_at: datetime | None = None,
) -> TradeSetup:
    """Evaluate one instrument and return a complete, explainable setup.

    Every exit from this function is either ``ACTIONABLE`` with real levels, or
    a named non-actionable status. There is no path that returns a direction
    without a stop, and none that substitutes a default for a measurement.
    """
    strategy_params = strategy_params or TrendMomentumParams()
    setup_params = setup_params or SetupParams()

    def emit(
        *,
        status: SetupStatus,
        direction: SetupDirection,
        detail: str,
        score: SetupScore | None = None,
        risk_reward: RiskReward | None = None,
        regime: RegimeAssessment | None = None,
        conditions: tuple[ConditionOutcome, ...] = (),
        opposing_conditions: tuple[ConditionOutcome, ...] = (),
    ) -> TradeSetup:
        """Attach the invariant fields to whichever outcome was reached.

        A typed constructor rather than a dict of common keys: every exit
        below carries identical provenance, and a spread dict would let one
        of them silently omit a field the type says is always present.
        """
        return TradeSetup(
            symbol=symbol,
            timeframe=timeframe,
            strategy=trend_momentum.STRATEGY_KEY,
            strategy_version=trend_momentum.STRATEGY_VERSION,
            status=status,
            direction=direction,
            score=score,
            risk_reward=risk_reward,
            regime=regime,
            conditions=conditions,
            opposing_conditions=opposing_conditions,
            detail=detail,
            indicators_used=(*trend_momentum.REQUIRED_INDICATORS, "atr", "bollinger"),
            candles_used=len(candles),
            last_candle_time=candles[-1].close_time if candles else None,
            data_source=data_source,
            data_status=data_status,
            data_age_seconds=data_age_seconds,
            evaluated_at=evaluated_at,
        )

    values = trend_momentum.compute_indicator_values(candles, strategy_params)
    if values is None:
        return emit(
            status=SetupStatus.INSUFFICIENT_DATA,
            direction=SetupDirection.NO_SIGNAL,
            detail=(
                f"At least one required indicator has not warmed up over {len(candles)} "
                f"candles. No value is substituted for a missing one."
            ),
        )

    long_conditions, short_conditions, bias = trend_momentum.evaluate_conditions(
        ema_fast=values["ema_fast"],
        ema_slow=values["ema_slow"],
        adx=values["adx"],
        rsi=values["rsi"],
        histogram=values["histogram"],
        params=strategy_params,
    )

    regime = classify_regime(
        candles,
        RegimeParams(
            ema_fast=strategy_params.ema_fast,
            ema_slow=strategy_params.ema_slow,
            adx_period=strategy_params.adx_period,
        ),
    )
    atr_percent = _atr_percent(regime)

    if bias is StrategyBias.LONG_BIAS:
        direction = SetupDirection.LONG
        conditions, opposing = long_conditions, short_conditions
    elif bias is StrategyBias.SHORT_BIAS:
        direction = SetupDirection.SHORT
        conditions, opposing = short_conditions, long_conditions
    else:
        long_met = sum(1 for c in long_conditions if c.satisfied)
        short_met = sum(1 for c in short_conditions if c.satisfied)
        return emit(
            status=SetupStatus.NO_ACTIONABLE_SETUP,
            direction=SetupDirection.NO_SIGNAL,
            regime=regime,
            conditions=long_conditions,
            opposing_conditions=short_conditions,
            detail=(
                f"The rule set is NEUTRAL: {long_met}/4 long and {short_met}/4 short "
                f"conditions hold, and this strategy requires all four. Partial "
                f"agreement is not a signal."
            ),
        )

    risk_reward = derive_risk_reward(
        candles, direction=direction, params=setup_params, atr_percent=atr_percent
    )
    if risk_reward is None:
        return emit(
            status=SetupStatus.NO_ACTIONABLE_SETUP,
            direction=direction,
            regime=regime,
            conditions=conditions,
            opposing_conditions=opposing,
            detail=(
                f"The rule set reports a {direction.value} bias, but no safe stop could be "
                f"derived under the {setup_params.stop_model.value} model. No level is "
                f"invented to fill the gap, so there is nothing actionable here."
            ),
        )

    score = _score(
        conditions=conditions,
        regime=regime,
        direction=direction,
        risk_reward=risk_reward,
        atr_percent=atr_percent,
        volume_ratio=_volume_ratio(candles),
    )

    return emit(
        status=SetupStatus.ACTIONABLE,
        direction=direction,
        score=score,
        risk_reward=risk_reward,
        regime=regime,
        conditions=conditions,
        opposing_conditions=opposing,
        detail=(
            f"All four {direction.value} rules hold. Setup score {score.value} of 100 "
            f"measures rule alignment, not a probability of profit."
        ),
    )


def evaluate_setup(
    candles: Sequence[Candle],
    *,
    context: DataContext,
    now: datetime,
    strategy_params: TrendMomentumParams | None = None,
    setup_params: SetupParams | None = None,
) -> TradeSetup:
    """Build a setup for live analysis, with the freshness gate applied.

    The gate is the only difference between this and :func:`build_setup`, and
    it is the same gate ``strategies.registry.evaluate`` applies, for the same
    reason: a setup computed from stale candles looks identical to a real one.
    A second, laxer convention for what counts as fresh would be worse than no
    gate at all, because the two would disagree silently.

    Two conditions refuse, and both report ``STALE`` rather than a direction:

    * the venue reported anything but ``OK`` for this observation, and
    * the observation carries no verifiable age, which is not evidence of
      freshness even when the status is ``OK``.
    """

    def refused(detail: str) -> TradeSetup:
        return TradeSetup(
            symbol=context.symbol,
            timeframe=context.timeframe,
            strategy=trend_momentum.STRATEGY_KEY,
            strategy_version=trend_momentum.STRATEGY_VERSION,
            status=SetupStatus.STALE,
            direction=SetupDirection.NO_SIGNAL,
            detail=detail,
            indicators_used=(*trend_momentum.REQUIRED_INDICATORS, "atr", "bollinger"),
            candles_used=len(candles),
            last_candle_time=candles[-1].close_time if candles else None,
            data_source=context.source,
            data_status=context.data_status,
            data_age_seconds=context.age_seconds,
            evaluated_at=now,
        )

    if context.data_status != "OK":
        return refused(
            f"Candle data is {context.data_status}; no setup is reported from data "
            f"that is not current."
        )
    if context.age_seconds is None:
        return refused(
            "Candle freshness could not be verified (the venue supplied no event "
            "timestamp), so no setup is reported. An unverifiable age is not freshness."
        )

    return build_setup(
        candles,
        symbol=context.symbol,
        timeframe=context.timeframe,
        strategy_params=strategy_params,
        setup_params=setup_params,
        data_source=context.source,
        data_status=context.data_status,
        data_age_seconds=context.age_seconds,
        evaluated_at=now,
    )
