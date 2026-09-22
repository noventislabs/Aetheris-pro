"""The brains: independent readings of one open position.

Each brain answers a narrow question about whether what it watches still
supports the position, and returns the evidence it used. None of them
decides anything, none performs I/O, and none can reach a venue -- they are
given an observation and return a verdict over it.

## Three rules every brain follows

**Measured or UNAVAILABLE, never a default.** A brain that could not read its
input says so. It does not return NEUTRAL, because "I looked and it is
balanced" and "I could not look" are different facts, and collapsing them is
how a blind system comes to look calm.

**Evidence over verdict.** The state is a summary; the evidence rows are the
answer. Each carries what was observed, what it was compared to, and whether
the comparison held, so a reader can re-derive the state rather than trust it.

**No scoring.** There is no confidence, no probability and no strength. A
brain reports a direction of support, not a magnitude of belief.

## The technical brain is the thesis check

It re-evaluates the *original* entry conditions against current values, by
name. That is the literal meaning of "does the thesis still hold" -- not
whether a fresh evaluation would open the trade today, which is a different
and much weaker question.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

from aetheris.analysis.indicators.engine import calculate_indicators
from aetheris.analysis.indicators.registry import IndicatorParams
from aetheris.analysis.strategies.trend_momentum import (
    TrendMomentumParams,
    compute_indicator_values,
    evaluate_conditions,
)
from aetheris.core.money import ZERO
from aetheris.domain.enums import PositionSide
from aetheris.domain.indicators import IndicatorStatus
from aetheris.domain.intelligence import (
    BrainName,
    BrainResult,
    BrainState,
    DataQuality,
    Evidence,
    PositionMetrics,
)
from aetheris.domain.market import Candle
from aetheris.domain.paper import PaperPosition, RiskLockState
from aetheris.domain.regime import MarketRegime, RegimeAssessment
from aetheris.domain.strategy import ConditionOutcome, StrategyBias

__all__ = [
    "PositionObservation",
    "exit_brain",
    "market_brain",
    "momentum_volume_brain",
    "position_brain",
    "regime_brain",
    "risk_brain",
    "structure_brain",
    "technical_brain",
]

#: Within this fraction of the way from entry to target, the target counts as
#: approaching. A judgement call, stated rather than buried.
TARGET_PROXIMITY: Final = Decimal("0.80")
#: Below this many multiples of the planned risk still ahead of the stop, the
#: position is close enough to its stop to call it out.
STOP_PROXIMITY_R: Final = Decimal("0.25")
#: Relative volume at or below this reads as participation draining away.
VOLUME_FADE: Final = Decimal("0.60")


@dataclass(frozen=True, slots=True)
class PositionObservation:
    """Everything the brains are allowed to see.

    Assembled by the caller, deliberately: a brain that could fetch its own
    data could fetch data from after the moment being evaluated, and the
    whole package would stop being replayable.
    """

    position: PaperPosition
    metrics: PositionMetrics
    now: datetime

    #: Closed candles up to and including the bar being evaluated. ``None``
    #: when they could not be fetched, which several brains turn into
    #: UNAVAILABLE rather than into a neutral reading.
    candles: Sequence[Candle] | None = None
    regime: RegimeAssessment | None = None
    strategy_params: TrendMomentumParams = field(default_factory=TrendMomentumParams)

    #: Risk posture, read from the engine rather than recomputed here.
    risk_lock: RiskLockState = RiskLockState.NONE
    risk_lock_reason: str | None = None
    unreconciled_orders: int = 0

    @property
    def is_long(self) -> bool:
        return self.position.side is PositionSide.LONG

    @property
    def mark(self) -> Decimal | None:
        return self.position.mark_price


def _unavailable(
    brain: BrainName, observation: PositionObservation, detail: str, *, name: str
) -> BrainResult:
    """The shape every brain uses when it could not read its input."""
    return BrainResult(
        brain=brain,
        state=BrainState.UNAVAILABLE,
        evidence=(Evidence(name=name, satisfied=None, detail=detail),),
        data_quality=DataQuality.UNAVAILABLE,
        source=observation.position.mark_source,
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Market
# ----------------------------------------------------------------------


def market_brain(observation: PositionObservation) -> BrainResult:
    """Is there a usable price at all, and how old is it?

    First in the ladder for a reason: every other reading is derived from a
    mark, so a mark nobody could read makes the rest of the panel a
    description of the past.
    """
    position = observation.position
    if position.mark_price is None:
        return _unavailable(
            BrainName.MARKET,
            observation,
            (
                f"No usable mark for {position.symbol}. The last poll reported "
                f"{position.mark_status or 'nothing'}, so no distance, PnL or "
                f"excursion can be measured from it."
            ),
            name="mark_usable",
        )

    evidence = [
        Evidence(
            name="mark_usable",
            observed=str(position.mark_price),
            reference=position.mark_source,
            satisfied=True,
            detail=f"Marked at {position.mark_price} from {position.mark_source}.",
        ),
        Evidence(
            name="mark_status",
            observed=position.mark_status,
            reference="OK",
            satisfied=position.mark_status == "OK",
            detail=f"The venue reported {position.mark_status} for this observation.",
        ),
    ]
    quality = DataQuality.OK if position.mark_status == "OK" else DataQuality.DEGRADED
    return BrainResult(
        brain=BrainName.MARKET,
        state=BrainState.SUPPORTIVE if quality is DataQuality.OK else BrainState.NEUTRAL,
        evidence=tuple(evidence),
        data_quality=quality,
        source=position.mark_source,
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Technical — the thesis check
# ----------------------------------------------------------------------


def _current_conditions(
    observation: PositionObservation,
) -> tuple[tuple[ConditionOutcome, ...], StrategyBias] | None:
    """Re-run the published rule set over current candles.

    Reuses the strategy's own evaluator rather than reimplementing it: a
    second copy of the rules would drift from the ones the position was
    opened under, and the comparison would quietly stop being like-for-like.
    """
    if not observation.candles:
        return None
    values = compute_indicator_values(observation.candles, observation.strategy_params)
    if values is None:
        return None
    long_conditions, short_conditions, bias = evaluate_conditions(
        ema_fast=values["ema_fast"],
        ema_slow=values["ema_slow"],
        adx=values["adx"],
        rsi=values["rsi"],
        histogram=values["histogram"],
        params=observation.strategy_params,
    )
    return (
        (long_conditions if observation.is_long else short_conditions),
        bias,
    )


def technical_brain(observation: PositionObservation) -> BrainResult:
    """Do the ORIGINAL entry conditions still hold, one by one?

    This is the literal thesis question. It is deliberately not "would the
    rule set open this trade now" -- that is a fresh evaluation, and a
    position can remain perfectly valid while a *new* entry would be refused
    for reasons that have nothing to do with it, such as a cooldown.
    """
    thesis = observation.position.thesis
    if thesis is None or thesis.strategy is None:
        return _unavailable(
            BrainName.TECHNICAL,
            observation,
            (
                "This position recorded no rule set at entry, so there are no original "
                "conditions to re-check. Alignment is not measurable -- which is "
                "different from it being low."
            ),
            name="entry_conditions_recorded",
        )

    original = thesis.strategy.entry_conditions
    if not original:
        return _unavailable(
            BrainName.TECHNICAL,
            observation,
            "The recorded thesis carried no conditions, so there is nothing to compare.",
            name="entry_conditions_recorded",
        )

    current = _current_conditions(observation)
    if current is None:
        return _unavailable(
            BrainName.TECHNICAL,
            observation,
            (
                "Indicators could not be recomputed: no candles, or at least one "
                "required indicator has not warmed up over the series supplied."
            ),
            name="indicators_available",
        )

    now_by_name = {condition.name: condition for condition in current[0]}
    evidence: list[Evidence] = []
    held = 0
    for condition in original:
        latest = now_by_name.get(condition.name)
        if latest is None:
            evidence.append(
                Evidence(
                    name=condition.name,
                    observed=None,
                    reference="satisfied at entry",
                    satisfied=None,
                    detail=(
                        f"{condition.name} was recorded at entry but is not present in "
                        f"the current rule set, so it cannot be re-checked."
                    ),
                )
            )
            continue
        if latest.satisfied:
            held += 1
        evidence.append(
            Evidence(
                name=condition.name,
                observed="holds" if latest.satisfied else "no longer holds",
                reference="satisfied at entry",
                satisfied=latest.satisfied,
                detail=latest.detail,
            )
        )

    total = len(original)
    if held == total:
        state = BrainState.SUPPORTIVE
    elif held * 3 <= total:
        # A third or fewer still hold. Deliberately not "none hold": some
        # conditions are non-directional gates -- trend_strength is an ADX
        # floor, which is satisfied just as well by a trend running the other
        # way. Requiring every condition to fail would make a reversal
        # unreachable through exactly the condition that cares least about
        # direction, and the exit rung above would be dead code.
        state = BrainState.ADVERSE
    else:
        state = BrainState.WEAKENING

    return BrainResult(
        brain=BrainName.TECHNICAL,
        state=state,
        evidence=tuple(evidence),
        data_quality=DataQuality.OK,
        source=thesis.strategy.data_source,
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Regime
# ----------------------------------------------------------------------


_SUPPORTIVE_REGIME: Final[dict[bool, MarketRegime]] = {
    True: MarketRegime.TREND_UP,
    False: MarketRegime.TREND_DOWN,
}
_OPPOSING_REGIME: Final[dict[bool, MarketRegime]] = {
    True: MarketRegime.TREND_DOWN,
    False: MarketRegime.TREND_UP,
}


def regime_brain(observation: PositionObservation) -> BrainResult:
    """Is the market still the kind of market this position was opened into?

    Compared against the regime recorded at entry when there is one. A regime
    that has flipped to oppose the position is the single clearest piece of
    reversal evidence this system can produce without SMC.
    """
    assessment = observation.regime
    if assessment is None or assessment.regime is MarketRegime.UNKNOWN:
        return _unavailable(
            BrainName.REGIME,
            observation,
            (
                "The regime could not be classified -- a required input has not warmed "
                "up. UNKNOWN is not RANGE, and is not treated as one."
            ),
            name="regime_classified",
        )

    thesis = observation.position.thesis
    entry_regime = thesis.strategy.regime if thesis and thesis.strategy else None
    current = assessment.regime
    supportive = _SUPPORTIVE_REGIME[observation.is_long]
    opposing = _OPPOSING_REGIME[observation.is_long]

    if current is opposing:
        state = BrainState.ADVERSE
    elif current is supportive:
        state = BrainState.SUPPORTIVE
    else:
        state = BrainState.NEUTRAL

    evidence = [
        Evidence(
            name="regime_now",
            observed=current.value,
            reference=supportive.value,
            satisfied=current is supportive,
            detail=assessment.reason,
        ),
        Evidence(
            name="volatility_band",
            observed=assessment.volatility_band.value,
            satisfied=None,
            detail=f"Volatility reads {assessment.volatility_band.value}.",
        ),
    ]
    if entry_regime is not None:
        evidence.append(
            Evidence(
                name="regime_since_entry",
                observed=current.value,
                reference=entry_regime.value,
                satisfied=current is entry_regime,
                detail=(
                    f"Entered in {entry_regime.value}; now {current.value}."
                    if current is not entry_regime
                    else f"Unchanged since entry: {current.value}."
                ),
            )
        )

    return BrainResult(
        brain=BrainName.REGIME,
        state=state,
        evidence=tuple(evidence),
        data_quality=DataQuality.OK,
        source=assessment.method,
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Momentum and volume
# ----------------------------------------------------------------------


def momentum_volume_brain(observation: PositionObservation) -> BrainResult:
    """Is the move still being pushed, and is anyone still trading it?

    Reuses the registered indicators rather than recomputing anything. A
    fading push is not a reversal and is reported as WEAKENING, not ADVERSE:
    the distinction is the difference between tightening a stop and closing.
    """
    candles = observation.candles
    if not candles:
        return _unavailable(
            BrainName.MOMENTUM_VOLUME,
            observation,
            "No candles were supplied, so momentum and volume cannot be measured.",
            name="candles_available",
        )

    params = IndicatorParams(
        rsi_period=observation.strategy_params.rsi_period,
        macd_fast=observation.strategy_params.macd_fast,
        macd_slow=observation.strategy_params.macd_slow,
        macd_signal=observation.strategy_params.macd_signal,
    )
    results = {
        result.indicator: result
        for result in calculate_indicators(candles, ("rsi", "macd", "roc"), params)
    }

    def latest(key: str, field_name: str) -> Decimal | None:
        result = results.get(key)
        if result is None or result.status is not IndicatorStatus.READY or result.latest is None:
            return None
        return result.latest.get(field_name)

    rsi = latest("rsi", "rsi")
    histogram = latest("macd", "histogram")
    roc = latest("roc", "roc")

    if rsi is None and histogram is None and roc is None:
        return _unavailable(
            BrainName.MOMENTUM_VOLUME,
            observation,
            "None of RSI, MACD or ROC warmed up over the candles supplied.",
            name="indicators_available",
        )

    is_long = observation.is_long
    evidence: list[Evidence] = []
    against = 0
    measured = 0

    if histogram is not None:
        measured += 1
        supports = histogram > ZERO if is_long else histogram < ZERO
        against += 0 if supports else 1
        evidence.append(
            Evidence(
                name="macd_histogram",
                observed=f"{histogram:.8f}",
                reference="positive" if is_long else "negative",
                satisfied=supports,
                detail=(
                    f"MACD histogram {histogram:.8f} "
                    f"{'still supports' if supports else 'no longer supports'} the position."
                ),
            )
        )

    if roc is not None:
        measured += 1
        supports = roc > ZERO if is_long else roc < ZERO
        against += 0 if supports else 1
        evidence.append(
            Evidence(
                name="rate_of_change",
                observed=f"{roc:.6f}",
                reference="positive" if is_long else "negative",
                satisfied=supports,
                detail=f"Rate of change {roc:.6f} over its lookback.",
            )
        )

    if rsi is not None:
        measured += 1
        midline = Decimal(50)
        supports = rsi > midline if is_long else rsi < midline
        against += 0 if supports else 1
        evidence.append(
            Evidence(
                name="rsi",
                observed=f"{rsi:.2f}",
                reference=f"{'above' if is_long else 'below'} 50",
                satisfied=supports,
                detail=f"RSI {rsi:.2f} against the 50 midline.",
            )
        )

    volume_ratio = _relative_volume(candles)
    if volume_ratio is None:
        evidence.append(
            Evidence(
                name="relative_volume",
                satisfied=None,
                detail="No usable volume baseline over the candles supplied.",
            )
        )
    else:
        fading = volume_ratio <= VOLUME_FADE
        evidence.append(
            Evidence(
                name="relative_volume",
                observed=f"{volume_ratio:.3f}",
                reference=f">{VOLUME_FADE}",
                satisfied=not fading,
                detail=(
                    f"Last bar traded {volume_ratio:.3f}x its trailing baseline"
                    f"{'; participation is draining' if fading else ''}."
                ),
            )
        )

    if measured == 0:  # pragma: no cover - guarded above
        state = BrainState.UNAVAILABLE
    elif against == 0:
        state = BrainState.SUPPORTIVE
    elif against == measured:
        state = BrainState.ADVERSE
    else:
        state = BrainState.WEAKENING

    return BrainResult(
        brain=BrainName.MOMENTUM_VOLUME,
        state=state,
        evidence=tuple(evidence),
        data_quality=DataQuality.OK if measured == 3 else DataQuality.DEGRADED,
        source=observation.position.mark_source,
        observed_at=observation.now,
    )


def _relative_volume(candles: Sequence[Candle], *, window: int = 20) -> Decimal | None:
    """Last bar's volume against the mean of the bars before it. Trailing only."""
    if len(candles) < 2:
        return None
    baseline_bars = candles[-(window + 1) : -1]
    if not baseline_bars:
        return None
    total = sum((candle.volume for candle in baseline_bars), ZERO)
    if total <= ZERO:
        return None
    baseline = total / Decimal(len(baseline_bars))
    if baseline <= ZERO:
        return None
    return candles[-1].volume / baseline


# ----------------------------------------------------------------------
# Structure — deliberately unbuilt
# ----------------------------------------------------------------------


def structure_brain(observation: PositionObservation) -> BrainResult:
    """Market structure. Not implemented, and says so.

    The interface exists so the orchestrator has a complete roster and a
    reader can see the gap. Returning a fabricated break of structure, order
    block or imbalance would be inventing the most authoritative-sounding
    evidence on the panel, which is precisely why it returns nothing.
    """
    return BrainResult(
        brain=BrainName.STRUCTURE,
        state=BrainState.UNAVAILABLE,
        evidence=(
            Evidence(
                name="smc_capability",
                observed="UNAVAILABLE",
                reference="analysis.smc",
                satisfied=None,
                detail=(
                    "SMC capability is not implemented. No break of structure, change of "
                    "character, fair value gap, order block or liquidity level is "
                    "calculated, and none is inferred from other indicators."
                ),
            ),
        ),
        data_quality=DataQuality.UNAVAILABLE,
        source=None,
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Position
# ----------------------------------------------------------------------


def position_brain(observation: PositionObservation) -> BrainResult:
    """Where the position sits between its own stop and its own target."""
    metrics = observation.metrics
    position = observation.position

    if observation.mark is None:
        return _unavailable(
            BrainName.POSITION,
            observation,
            "Unmarked, so the position's standing against its own levels is unknown.",
            name="mark_usable",
        )

    evidence: list[Evidence] = [
        Evidence(
            name="unrealized_pnl",
            observed=str(metrics.unrealized_pnl) if metrics.unrealized_pnl is not None else None,
            satisfied=None,
            detail=(
                f"Unrealised {metrics.unrealized_pnl}."
                if metrics.unrealized_pnl is not None
                else "Unrealised PnL is not computable without a usable mark."
            ),
        ),
        Evidence(
            name="time_in_trade",
            observed=f"{metrics.time_in_trade_seconds}s",
            satisfied=None,
            detail=f"Open for {metrics.time_in_trade_seconds} seconds.",
        ),
    ]

    for label, value in (
        ("mfe", metrics.mfe_percent),
        ("mae", metrics.mae_percent),
    ):
        evidence.append(
            Evidence(
                name=label,
                observed=f"{value}%" if value is not None else None,
                satisfied=None,
                detail=(
                    f"Observed {label.upper()} of {value}% of entry, from marks seen while open."
                    if value is not None
                    else f"{label.upper()} was never observed."
                ),
            )
        )

    near_target = False
    if metrics.target_distance_percent is not None and position.target_price is not None:
        span = abs(position.target_price - position.entry_price)
        travelled = abs(observation.mark - position.entry_price)
        if span > ZERO:
            near_target = (travelled / span) >= TARGET_PROXIMITY
        evidence.append(
            Evidence(
                name="target_proximity",
                observed=f"{metrics.target_distance_percent}%",
                reference=f"{TARGET_PROXIMITY * 100}% of the way",
                satisfied=near_target,
                detail=(
                    f"{metrics.target_distance_percent}% from target; "
                    f"{(travelled / span * 100) if span > ZERO else 0:.1f}% of the "
                    f"planned distance covered."
                ),
            )
        )

    near_stop = False
    if metrics.remaining_r is not None:
        near_stop = metrics.remaining_r <= STOP_PROXIMITY_R
        evidence.append(
            Evidence(
                name="remaining_r",
                observed=str(metrics.remaining_r),
                reference=str(STOP_PROXIMITY_R),
                satisfied=not near_stop,
                detail=(
                    f"{metrics.remaining_r}R of the planned risk still lies between the "
                    f"mark and the stop."
                ),
            )
        )
    else:
        evidence.append(
            Evidence(
                name="remaining_r",
                satisfied=None,
                detail=(
                    "No risk was planned at entry, so remaining R is not computable. "
                    "That is different from it being zero."
                ),
            )
        )

    if near_stop:
        state = BrainState.ADVERSE
    elif near_target:
        state = BrainState.WEAKENING
    else:
        state = BrainState.NEUTRAL

    return BrainResult(
        brain=BrainName.POSITION,
        state=state,
        evidence=tuple(evidence),
        data_quality=DataQuality.OK,
        source=position.mark_source,
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Risk
# ----------------------------------------------------------------------


def risk_brain(observation: PositionObservation) -> BrainResult:
    """The engine's current posture, read rather than recomputed.

    None of the risk *rules* live here. Duplicating them would create a
    second opinion about the same limits, and the point of ADR 0006 is that
    there is one.
    """
    evidence: list[Evidence] = [
        Evidence(
            name="risk_lock",
            observed=observation.risk_lock.value,
            reference=RiskLockState.NONE.value,
            satisfied=not observation.risk_lock.blocks_entries,
            detail=(
                observation.risk_lock_reason
                or (
                    "No daily lock is engaged."
                    if not observation.risk_lock.blocks_entries
                    else f"{observation.risk_lock.value} is engaged."
                )
            ),
        ),
        Evidence(
            name="reconciliation",
            observed=str(observation.unreconciled_orders),
            reference="0",
            satisfied=observation.unreconciled_orders == 0,
            detail=(
                f"{observation.unreconciled_orders} order(s) await reconciliation."
                if observation.unreconciled_orders
                else "No orders await reconciliation."
            ),
        ),
    ]

    liquidation = observation.metrics.liquidation_distance_percent
    evidence.append(
        Evidence(
            name="liquidation_distance",
            observed=f"{liquidation}%" if liquidation is not None else None,
            satisfied=None,
            detail=(
                f"Mark sits {liquidation}% from the modelled liquidation price. The "
                f"model is optimistic: real venues liquidate earlier, at a maintenance "
                f"margin this build does not know."
                if liquidation is not None
                else "No liquidation price is available for this position."
            ),
        )
    )

    stop_recorded = observation.position.stop_price is not None
    evidence.append(
        Evidence(
            name="stop_present",
            observed="present" if stop_recorded else "absent",
            reference="present",
            satisfied=stop_recorded,
            detail=(
                "A stop level is recorded on the position."
                if stop_recorded
                else "No stop is recorded, so this position has no defined risk."
            ),
        )
    )

    blocked = observation.risk_lock.blocks_entries or observation.unreconciled_orders > 0
    state = BrainState.ADVERSE if blocked else BrainState.SUPPORTIVE
    if not blocked and not stop_recorded:
        state = BrainState.WEAKENING

    return BrainResult(
        brain=BrainName.RISK,
        state=state,
        evidence=tuple(evidence),
        data_quality=DataQuality.OK,
        source="risk-engine",
        observed_at=observation.now,
    )


# ----------------------------------------------------------------------
# Exit
# ----------------------------------------------------------------------


def exit_brain(observation: PositionObservation, *, upstream: Sequence[BrainResult]) -> BrainResult:
    """Whether anything observed amounts to a reason to stop holding.

    Reads the other brains rather than the market. It is the only brain that
    sees the others, and it still decides nothing -- the orchestrator owns
    the ladder. What this produces is the summary the ladder reads.

    Profit is not an input. A position being up is not a reason to close it,
    and there is deliberately no evidence row here that mentions it.
    """
    by_name = {result.brain: result for result in upstream}
    technical = by_name.get(BrainName.TECHNICAL)
    regime = by_name.get(BrainName.REGIME)
    momentum = by_name.get(BrainName.MOMENTUM_VOLUME)
    position = by_name.get(BrainName.POSITION)

    evidence: list[Evidence] = []

    thesis_broken = technical is not None and technical.state is BrainState.ADVERSE
    regime_opposed = regime is not None and regime.state is BrainState.ADVERSE
    momentum_opposed = momentum is not None and momentum.state is BrainState.ADVERSE
    # A mark past the stop recorded at entry. The most objective confirmation
    # available and the only one that is not an indicator reading: it is the
    # price disagreeing with the level the thesis itself nominated.
    levels_violated = position is not None and position.state is BrainState.ADVERSE

    evidence.append(
        Evidence(
            name="thesis_conditions",
            observed=technical.state.value if technical else None,
            reference=BrainState.SUPPORTIVE.value,
            satisfied=technical.state is BrainState.SUPPORTIVE if technical else None,
            detail=(
                "Every original entry condition still holds."
                if technical and technical.state is BrainState.SUPPORTIVE
                else "One or more original entry conditions no longer hold."
                if technical and technical.state.is_measured
                else "The original conditions could not be re-checked."
            ),
        )
    )
    evidence.append(
        Evidence(
            name="regime_opposed",
            observed=regime.state.value if regime else None,
            satisfied=not regime_opposed if regime and regime.state.is_measured else None,
            detail=(
                "The regime now opposes the position."
                if regime_opposed
                else "The regime does not oppose the position."
                if regime and regime.state.is_measured
                else "The regime could not be classified."
            ),
        )
    )

    # Two independent readings both against is the reversal signature. One
    # alone is weakening: a single indicator rolling over is ordinary noise,
    # and closing on it would be the "do not hold blindly" rule overshooting
    # into its opposite.
    confirmed_reversal = thesis_broken and (regime_opposed or momentum_opposed or levels_violated)
    evidence.append(
        Evidence(
            name="position_levels",
            observed="violated" if levels_violated else "intact",
            satisfied=not levels_violated,
            detail=(
                "The mark has passed the stop recorded at entry."
                if levels_violated
                else "The mark is still inside the levels recorded at entry."
            ),
        )
    )
    evidence.append(
        Evidence(
            name="confirmed_reversal",
            observed=str(confirmed_reversal).lower(),
            reference="thesis broken AND (regime, momentum or levels against)",
            satisfied=not confirmed_reversal,
            detail=(
                "The original thesis has failed and a second independent reading confirms it."
                if confirmed_reversal
                else "No confirmed reversal: a single adverse reading is not enough."
            ),
        )
    )

    if confirmed_reversal:
        state = BrainState.ADVERSE
    elif (
        thesis_broken
        or regime_opposed
        or (technical is not None and technical.state is BrainState.WEAKENING)
    ):
        state = BrainState.WEAKENING
    elif technical is None or not technical.state.is_measured:
        state = BrainState.UNAVAILABLE
    else:
        state = BrainState.SUPPORTIVE

    quality = DataQuality.UNAVAILABLE if state is BrainState.UNAVAILABLE else DataQuality.OK
    return BrainResult(
        brain=BrainName.EXIT,
        state=state,
        evidence=tuple(evidence),
        data_quality=quality,
        source="position-intelligence",
        observed_at=observation.now,
    )
