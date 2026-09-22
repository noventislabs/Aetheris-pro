"""The decision orchestrator: one advisory answer per open position.

Pure, deterministic, and it acts on nothing. Given an observation it runs
every brain and walks a fixed ladder, returning the first condition that
matches. The output is advice a human or a later execution layer may act on;
this module submits no order, moves no stop, changes no leverage and opens
nothing.

## The ladder, and why it is not a vote

```
1. BLOCKED_BY_RISK     risk lock, or orders awaiting reconciliation
2. INSUFFICIENT_DATA   no usable mark, or no readable thesis
3. EXIT_CONDITION_MET  thesis failed AND a second reading confirms it
4. REVERSAL_WARNING    thesis failed, or the regime turned against it
5. TARGET_APPROACHING  most of the planned distance is behind us
6. THESIS_VALID        everything still holds
```

A weighted vote would let several working brains outvote one that failed,
which is backwards: a brain that cannot see is the strongest reason to stop,
not the weakest. So the ladder is ordered and short-circuits, and the two
"cannot proceed" rungs sit above every rung that could produce an action.

## Profit is not on the ladder

Nowhere in this file does unrealised PnL influence the decision. A position
that is deeply profitable with an intact thesis reaches rung 6 and is held;
one that is profitable with a broken thesis reaches rung 3 or 4 on the
strength of the thesis, not the profit. That asymmetry is the product
requirement, and it is enforced by the ladder simply never asking.

## Partial exit

``PARTIAL_EXIT`` exists in the vocabulary so the model is complete, and is
never returned as a decision, because no engine in this build can execute
one -- ``_close`` takes no quantity in paper and testnet has no close at all.
Every result reports ``partial_exit_available = False`` and names it in
``unavailable_capabilities`` so nobody has to infer that from silence.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aetheris.analysis.position.brains import (
    PositionObservation,
    exit_brain,
    market_brain,
    momentum_volume_brain,
    position_brain,
    regime_brain,
    risk_brain,
    structure_brain,
    technical_brain,
)
from aetheris.domain.intelligence import (
    BrainName,
    BrainResult,
    BrainState,
    DataQuality,
    PositionDecision,
    PositionIntelligence,
    ThesisAlignment,
    ThesisState,
)

__all__ = ["UNAVAILABLE_CAPABILITIES", "evaluate_position"]

_HUNDRED: Final = Decimal(100)
_CENT: Final = Decimal("0.01")

#: Named on every result, so a reader never infers absence from silence.
UNAVAILABLE_CAPABILITIES: Final[tuple[str, ...]] = (
    "PARTIAL_EXIT: no engine in this build can close part of a position.",
    "STRUCTURE/SMC: not implemented; no structure evidence is produced.",
    "AUTONOMOUS_EXECUTION: this evaluation is advisory and places no order.",
)


def _alignment(technical: BrainResult) -> ThesisAlignment | None:
    """How many of the original conditions still hold, as a plain count.

    Returns ``None`` rather than zero when there was nothing to count. A
    manual entry with no recorded conditions has no alignment to report, and
    reporting 0 would read as "every condition failed".
    """
    if not technical.state.is_measured:
        return None
    checked = [row for row in technical.evidence if row.satisfied is not None]
    if not checked:
        return None
    held = sum(1 for row in checked if row.satisfied)
    value = (Decimal(held) / Decimal(len(checked)) * _HUNDRED).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    return ThesisAlignment(value=value, conditions_held=held, conditions_total=len(checked))


def evaluate_position(observation: PositionObservation) -> PositionIntelligence:
    """Run every brain and resolve one decision. Side-effect free."""
    market = market_brain(observation)
    technical = technical_brain(observation)
    regime = regime_brain(observation)
    momentum = momentum_volume_brain(observation)
    structure = structure_brain(observation)
    position = position_brain(observation)
    risk = risk_brain(observation)
    upstream = (market, technical, regime, momentum, structure, position, risk)
    exit_result = exit_brain(observation, upstream=upstream)
    results = (*upstream, exit_result)

    reasons: list[str] = []
    decision, thesis_state = _resolve(observation, results, reasons)

    # The worst quality among the brains that were meant to be readable.
    # Structure is excluded: it is permanently unavailable by design, and
    # letting it drag every evaluation to UNAVAILABLE would make the field
    # useless for spotting a genuine data problem.
    qualities = [r.data_quality for r in results if r.brain is not BrainName.STRUCTURE]
    if DataQuality.UNAVAILABLE in qualities:
        quality = DataQuality.UNAVAILABLE
    elif DataQuality.DEGRADED in qualities:
        quality = DataQuality.DEGRADED
    else:
        quality = DataQuality.OK

    return PositionIntelligence(
        symbol=observation.position.symbol,
        position_id=observation.position.position_id,
        decision=decision,
        thesis_state=thesis_state,
        reasons=tuple(reasons),
        brain_results=results,
        thesis_alignment=_alignment(technical),
        risk_state=observation.risk_lock.value,
        data_quality=quality,
        evaluated_at=observation.now,
        partial_exit_available=False,
        unavailable_capabilities=UNAVAILABLE_CAPABILITIES,
    )


def _resolve(
    observation: PositionObservation,
    results: tuple[BrainResult, ...],
    reasons: list[str],
) -> tuple[PositionDecision, ThesisState]:
    """Walk the ladder. First match wins; nothing below it is consulted."""
    by_name = {result.brain: result for result in results}
    market = by_name[BrainName.MARKET]
    technical = by_name[BrainName.TECHNICAL]
    regime = by_name[BrainName.REGIME]
    position = by_name[BrainName.POSITION]
    risk = by_name[BrainName.RISK]
    exit_result = by_name[BrainName.EXIT]

    # 1. Risk. Above everything, including data problems: if the engine has
    #    locked the account there is nothing useful to say about the thesis.
    if observation.risk_lock.blocks_entries or observation.unreconciled_orders > 0:
        reasons.append(
            observation.risk_lock_reason
            or (
                f"{observation.unreconciled_orders} order(s) await reconciliation."
                if observation.unreconciled_orders
                else f"{observation.risk_lock.value} is engaged."
            )
        )
        reasons.append(
            "Risk state takes precedence over every other reading. No position "
            "advice is offered while it holds."
        )
        return PositionDecision.BLOCKED_BY_RISK, ThesisState.BLOCKED_BY_RISK

    # 2. Data. A verdict derived from a price nobody could read is worse than
    #    no verdict, because it looks identical to a real one.
    if not market.usable:
        reasons.append(market.evidence[0].detail if market.evidence else "No usable mark.")
        reasons.append("No decision is offered from data that could not be read.")
        return PositionDecision.INSUFFICIENT_DATA, ThesisState.INSUFFICIENT_DATA

    if not technical.state.is_measured:
        reasons.append(
            technical.evidence[0].detail
            if technical.evidence
            else "The original thesis could not be re-checked."
        )
        reasons.append(
            "Whether the thesis still holds is the question this answers, so it "
            "cannot be answered without one."
        )
        return PositionDecision.INSUFFICIENT_DATA, ThesisState.INSUFFICIENT_DATA

    # 3. Exit. Requires a failed thesis AND independent confirmation.
    if exit_result.state is BrainState.ADVERSE:
        reasons.append(
            "The original thesis has failed and a second independent reading confirms "
            "it. This is a reversal, not a fluctuation."
        )
        for row in exit_result.evidence:
            if row.satisfied is False:
                reasons.append(row.detail)
        return PositionDecision.EXIT, ThesisState.EXIT_CONDITION_MET

    # 4. Reversal warning. One adverse reading, unconfirmed.
    if technical.state is BrainState.ADVERSE or regime.state is BrainState.ADVERSE:
        reasons.append(
            "An adverse reading is present but unconfirmed by a second, so the "
            "response is protection rather than exit."
        )
        if technical.state is BrainState.ADVERSE:
            reasons.append("No original entry condition still holds.")
        if regime.state is BrainState.ADVERSE:
            reasons.append("The market regime now opposes the position.")
        return PositionDecision.PROTECT, ThesisState.REVERSAL_WARNING

    # 5. Target. Most of the planned distance is behind us, so tighten rather
    #    than take: the thesis is intact and the move may continue.
    near_target = any(row.name == "target_proximity" and row.satisfied for row in position.evidence)
    if near_target:
        reasons.append(
            "Most of the planned distance to target has been covered while the thesis "
            "still holds, so the suggestion is to trail rather than to close."
        )
        return PositionDecision.TRAIL, ThesisState.TARGET_APPROACHING

    # 6. Weakening, but intact. Still a hold -- weakening is not a reason to
    #    close, only to watch.
    if technical.state is BrainState.WEAKENING or exit_result.state is BrainState.WEAKENING:
        reasons.append(
            "Some of the original conditions no longer hold, but the thesis has not "
            "failed and no reversal is confirmed."
        )
        _append_risk_note(observation, reasons)
        return PositionDecision.HOLD, ThesisState.THESIS_WEAKENING

    # 7. Intact.
    reasons.append(
        "Every original entry condition still holds and nothing contradicts the position."
    )
    if risk.state is BrainState.WEAKENING:
        reasons.append("No stop is recorded, so this position has no defined risk.")
    _append_risk_note(observation, reasons)
    return PositionDecision.HOLD, ThesisState.THESIS_VALID


def _append_risk_note(observation: PositionObservation, reasons: list[str]) -> None:
    """State plainly that profit was not a factor, when there is profit.

    Worth saying out loud on exactly the screen where somebody is looking at
    a green number and wondering why nothing suggested taking it.
    """
    pnl = observation.metrics.unrealized_pnl
    if pnl is not None and pnl > 0:
        reasons.append(
            f"The position is {pnl} in profit. Profit is not a reason to close and did "
            f"not affect this decision."
        )
