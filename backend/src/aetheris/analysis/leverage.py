"""Leverage candidate derivation and constraint resolution.

Two pure functions, both callable without a venue, a database or a framework,
so the phase 7 risk engine and the phase 5 backtester use the same arithmetic
the terminal shows.

**Nothing here authorises leverage.** ``resolve_leverage`` computes what *would*
be permissible given constraints it is handed; it does not discover those
constraints, and it refuses to produce a number when any of them is missing.
There is no code path from this module to an exchange, and none to an order.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Final

from aetheris.domain.leverage import (
    LEVERAGE_MAX,
    LEVERAGE_MIN,
    LeverageDecision,
    LeverageOutcome,
    LeverageReason,
    LeverageRequest,
)

__all__ = ["TARGET_ADVERSE_MOVE_PERCENT", "propose_leverage", "resolve_leverage"]

#: The candidate is sized so that a move of roughly this magnitude against the
#: position is the reference scale. It is a normalisation constant, not a stop
#: loss, not a risk budget and not a prediction -- the risk engine sets real
#: stop distances from account equity in phase 7.
TARGET_ADVERSE_MOVE_PERCENT: Final = Decimal("2")

_ONE: Final = Decimal(1)


def propose_leverage(
    *,
    atr_percent: Decimal,
    conditions_met: int,
    conditions_total: int,
) -> LeverageRequest | None:
    """Derive a leverage **candidate** from measured volatility.

    The reasoning is deliberately simple and stated in full on the result: a
    more volatile instrument moves further against a position for the same
    market event, so it warrants less leverage. The candidate is therefore
    inversely proportional to ATR as a percentage of price::

        candidate = (target_adverse_move% / atr%) x agreement

    ``agreement`` is the deterministic fraction of the strategy's conditions
    that hold — a count divided by a count, nothing more. It is **not** an AI
    confidence, a probability of profit, a win probability, an AI certainty, or
    an AI-selected execution leverage, and it authorises nothing. It is the
    *only* way analysis influences the number, and it can only ever scale the
    candidate **down**: 4/4 leaves it untouched, 1/4 quarters it.

    The full chain, of which this function is one early link:

        market data -> indicators -> strategy analysis -> **requested leverage**
        -> exchange constraint -> risk engine -> approved leverage -> execution

    Returns ``None`` — no candidate at all — when any input is unusable:

    * volatility is not measurable (``atr_percent <= 0``)
    * the condition counts are incoherent (``conditions_total <= 0``,
      ``conditions_met < 0``, or ``conditions_met > conditions_total``)
    * no condition holds (``0/N``), which is a valid input with no candidate

    ``None`` rather than a fallback: a candidate derived from an unknown ATR
    would be a fabricated number, and defaulting to 1x would read as a
    deliberate conservative choice rather than an absence.
    """
    if atr_percent <= 0:
        return None

    # Incoherent counts are refused outright. Agreement is a ratio of counts,
    # and a ratio above 1 would scale a candidate *up* on the strength of a
    # bug -- the one direction this function must never move.
    if conditions_total <= 0:
        return None
    if conditions_met < 0 or conditions_met > conditions_total:
        return None

    agreement = Decimal(conditions_met) / Decimal(conditions_total)
    if agreement <= 0:
        # A valid 0/N: every count is coherent, simply nothing agrees, so
        # there is no candidate to make.
        return None

    # Quantised for the human-readable basis only; `inputs` keeps full
    # precision, because the text is for reading and the numbers are for maths.
    shown_atr = atr_percent.quantize(Decimal("0.0001"))
    raw = (TARGET_ADVERSE_MOVE_PERCENT / atr_percent) * agreement
    candidate = raw.quantize(_ONE, rounding=ROUND_DOWN)
    clamped = max(LEVERAGE_MIN, min(candidate, LEVERAGE_MAX))

    return LeverageRequest(
        requested_leverage=clamped,
        basis=(
            "ATR volatility + deterministic strategy-condition agreement -> leverage "
            f"candidate: a {TARGET_ADVERSE_MOVE_PERCENT}% reference adverse move divided "
            f"by an ATR of {shown_atr}% of price, scaled by "
            f"{conditions_met}/{conditions_total} condition agreement, then clamped to "
            f"{LEVERAGE_MIN}-{LEVERAGE_MAX}x. This is a candidate request for the risk "
            "engine to rule on — not an authorisation, not an AI confidence, and not a "
            "probability of anything."
        ),
        inputs={
            "atr_percent": atr_percent,
            "target_adverse_move_percent": TARGET_ADVERSE_MOVE_PERCENT,
            "agreement": agreement,
            "raw_candidate": raw,
        },
    )


def _validate_ceiling(value: object, name: str) -> str | None:
    """Check one constraint ceiling. Returns a problem description, or None.

    Every failure mode here is a rejection, never a correction:

    * **Not a Decimal.** A float ceiling has already lost precision before it
      arrived, and an int is a caller who did not go through the money rules.
      Coercing it would hide where the value came from.
    * **Below 1x.** Leverage below 1 is not a ceiling, it is nonsense; treating
      it as "no leverage" would be guessing at intent.
    * **Above the declared 1-500x domain.** Refused rather than clamped to 500.
      Clamping would invent a domain rule that nothing declares, and silently
      inventing rules about leverage limits is precisely what this chain
      exists to prevent. If a venue genuinely permits more, the domain range
      is what must change -- deliberately, in a commit.
    """
    if not isinstance(value, Decimal):
        return f"{name} must be a Decimal, got {type(value).__name__}"
    if value.is_nan() or not value.is_finite():
        return f"{name} is not a finite number"
    if value < LEVERAGE_MIN:
        return f"{name} of {value}x is below the minimum {LEVERAGE_MIN}x"
    if value > LEVERAGE_MAX:
        return (
            f"{name} of {value}x exceeds the declared candidate domain of "
            f"{LEVERAGE_MIN}-{LEVERAGE_MAX}x; it is refused rather than clamped, "
            "because no rule declares how to reduce it"
        )
    return None


def _domain_minimum_decision(
    common: dict[str, object],
    *,
    requested: Decimal,
    exchange_max_leverage: Decimal | None,
    risk_max_leverage: Decimal | None,
) -> LeverageDecision | None:
    """Approve a request for exactly 1x, even with the ceilings unknown.

    This is the *only* value an unknown ceiling cannot block, and the reason is
    arithmetic rather than judgement. ``_validate_ceiling`` defines a usable
    ceiling as one in ``[LEVERAGE_MIN, LEVERAGE_MAX]``. So for any ceiling this
    domain would accept, ``min(1, ceiling) == 1``. Learning the ceilings could
    therefore not change the answer, which means refusing here would not be
    failing closed -- it would be refusing a value already proven safe against
    every constraint the chain can express.

    It is also the boundary between borrowing and not borrowing. At 1x, margin
    equals notional: nothing is lent, and the leverage-driven liquidation the
    rest of this chain exists to prevent has no mechanism. Approving it grants
    no leverage at all.

    Two things it deliberately does **not** do. It never applies above 1x --
    2x genuinely needs the ceilings, and no amount of reasoning substitutes for
    them. And it declines (returning ``None``, so the chain reports the real
    problem) when a ceiling *was* supplied and is malformed: a broken input is
    a defect to surface, not to route around.
    """
    if requested != LEVERAGE_MIN:
        return None
    for value, name in (
        (exchange_max_leverage, "exchange_max_leverage"),
        (risk_max_leverage, "risk_max_leverage"),
    ):
        if value is not None and _validate_ceiling(value, name) is not None:
            return None
    return LeverageDecision(
        **common,  # type: ignore[arg-type]
        approved_leverage=LEVERAGE_MIN,
        outcome=LeverageOutcome.APPROVED,
        reason=LeverageReason.APPROVED_AT_DOMAIN_MINIMUM,
        binding_constraint="domain_minimum",
        detail=(
            f"{LEVERAGE_MIN}x approved at the domain minimum. Not every constraint is "
            f"known, but every ceiling this domain accepts is at least {LEVERAGE_MIN}x, "
            f"so min({LEVERAGE_MIN}, any valid ceiling) is {LEVERAGE_MIN} whatever the "
            f"missing values turn out to be. {LEVERAGE_MIN}x is unlevered exposure -- "
            "margin equals notional and nothing is borrowed. Anything above it still "
            "requires the venue ceiling and the risk engine."
        ),
    )


def resolve_leverage(
    request: LeverageRequest | None,
    *,
    exchange_max_leverage: Decimal | None,
    risk_max_leverage: Decimal | None,
    risk_engine_available: bool,
) -> LeverageDecision:
    """Apply the constraint chain, failing closed on anything unknown.

    ``approved = min(requested, exchange_max, risk_max)`` — but only when all
    three are known. Each missing constraint is a distinct rejection reason, so
    a reader learns which link was absent rather than just that the answer was
    no.

    The order of the checks is the order of authority: the request is only
    meaningful if it exists and is in range; the venue's ceiling is a hard
    physical limit; the risk engine has the final word.

    One request resolves without the ceilings: exactly ``LEVERAGE_MIN``. See
    ``_domain_minimum_decision`` for why that is arithmetic rather than a
    loophole.
    """
    if request is None:
        return LeverageDecision(
            exchange_max_leverage=exchange_max_leverage,
            risk_max_leverage=risk_max_leverage,
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.INSUFFICIENT_DATA,
            detail=(
                "No leverage candidate could be derived: volatility was not measurable "
                "from the available candles."
            ),
        )

    requested = request.requested_leverage
    common: dict[str, object] = {
        "requested_leverage": requested,
        "exchange_max_leverage": exchange_max_leverage,
        "risk_max_leverage": risk_max_leverage,
        "basis": request.basis,
        "inputs": request.inputs,
    }

    # Re-checked here rather than trusted from the model: this function is the
    # gate, and a gate that assumes its input was already validated is not one.
    request_problem = _validate_ceiling(requested, "requested_leverage")
    if request_problem is not None:
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.REQUEST_OUT_OF_RANGE,
            detail=f"{request_problem}. No leverage is approved.",
        )

    # The domain minimum is resolvable without the ceilings; see the helper.
    # Checked only when the full chain cannot complete, so a fully-known chain
    # still reports APPROVED_IN_FULL as it always did.
    if exchange_max_leverage is None or not risk_engine_available or risk_max_leverage is None:
        minimum = _domain_minimum_decision(
            common,
            requested=requested,
            exchange_max_leverage=exchange_max_leverage,
            risk_max_leverage=risk_max_leverage,
        )
        if minimum is not None:
            return minimum

    if exchange_max_leverage is None:
        # The single most important fail-closed case. Venues publish
        # per-symbol leverage brackets only to authenticated callers, and the
        # ceilings differ per symbol and per notional tier. Assuming 500x --
        # or any number -- would be inventing an exchange constraint.
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.EXCHANGE_MAX_UNKNOWN,
            detail=(
                "The venue's maximum leverage for this symbol is unknown, because "
                "leverage brackets are served only from an authenticated endpoint and "
                "this build holds no credentials. No leverage is approved: an unknown "
                "ceiling is not permission to use the requested value."
            ),
        )

    exchange_problem = _validate_ceiling(exchange_max_leverage, "exchange_max_leverage")
    if exchange_problem is not None:
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.EXCHANGE_MAX_INVALID,
            detail=f"{exchange_problem}. No leverage is approved.",
        )

    if not risk_engine_available:
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.RISK_ENGINE_UNAVAILABLE,
            detail=(
                "The risk engine is not implemented yet (phase 7), so volatility, stop "
                "distance, liquidation distance, account equity, position size and the "
                "daily loss limit have not been evaluated. The risk engine has final "
                "authority, so no leverage is approved without it."
            ),
        )

    if risk_max_leverage is None:
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.RISK_MAX_UNKNOWN,
            detail="The risk engine did not produce a maximum, so none is approved.",
        )

    risk_problem = _validate_ceiling(risk_max_leverage, "risk_max_leverage")
    if risk_problem is not None:
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            outcome=LeverageOutcome.REJECTED,
            reason=LeverageReason.RISK_MAX_INVALID,
            detail=f"{risk_problem}. No leverage is approved.",
        )

    approved = min(requested, exchange_max_leverage, risk_max_leverage)
    if approved >= requested:
        return LeverageDecision(
            **common,  # type: ignore[arg-type]
            approved_leverage=approved,
            outcome=LeverageOutcome.APPROVED,
            reason=LeverageReason.APPROVED_IN_FULL,
            detail=f"{approved}x approved; within every known constraint.",
        )

    bound_by_exchange = approved == exchange_max_leverage
    return LeverageDecision(
        **common,  # type: ignore[arg-type]
        approved_leverage=approved,
        outcome=LeverageOutcome.REDUCED,
        reason=(
            LeverageReason.BOUND_BY_EXCHANGE_MAX
            if bound_by_exchange
            else LeverageReason.BOUND_BY_RISK_MAX
        ),
        binding_constraint=("exchange_max_leverage" if bound_by_exchange else "risk_max_leverage"),
        detail=(
            f"Requested {requested}x reduced to {approved}x by "
            f"{'the venue ceiling' if bound_by_exchange else 'the risk engine ceiling'}."
        ),
    )
