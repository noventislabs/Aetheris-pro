"""Deriving the risk engine's leverage ceiling.

This is the link the phase 4 chain has always reported as missing. It produces
``risk_max_leverage`` from **measured** inputs, and it is worth being explicit
about what it is not: it is not a recommendation, not an opinion about the
trade, and not influenced by anything a strategy thinks. Bias and condition
counts are not parameters here and never will be.

Two independent constraints, and the tighter one wins.

**Liquidation must sit outside the stop.** At leverage L, a position is
liquidated when price moves 1/L against it (the simplified model this system
uses; real venues liquidate earlier). If the stop is s% away, then leverage
high enough that 1/L <= s/100 means the position is liquidated *before* the
stop can fire -- the stop becomes decoration. So L < 100/s, with a safety
factor because the liquidation model is optimistic.

**A stop-out must not exceed the day's remaining loss budget.** Loss at the
stop is margin x L x s/100. Requiring that to stay within what is left of the
daily loss limit bounds L again. This is the constraint that tightens as a bad
day progresses, which is the behaviour a daily limit is for.

Both need a stop distance. Without one there is no derivation, and the honest
answer is ``None`` -- which the chain turns into a rejection, not a default.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from aetheris.core.money import ZERO
from aetheris.domain.leverage import LEVERAGE_MAX, LEVERAGE_MIN

__all__ = ["LIQUIDATION_SAFETY_FACTOR", "derive_risk_max_leverage"]

#: The liquidation model here is optimistic -- it assumes loss equal to posted
#: margin, while real venues liquidate earlier at a maintenance-margin
#: threshold that varies by symbol and notional tier. Keeping the stop well
#: inside the modelled liquidation level is how that optimism is paid for.
LIQUIDATION_SAFETY_FACTOR: Decimal = Decimal("2")

_HUNDRED = Decimal(100)


def derive_risk_max_leverage(
    *,
    stop_loss_percent: Decimal | None,
    requested_margin: Decimal,
    session_realized_pnl: Decimal,
    daily_loss_limit: Decimal,
    configured_max_leverage: Decimal,
) -> Decimal | None:
    """The highest leverage this account may use for this trade, or ``None``.

    ``None`` means "could not be derived", which the constraint chain reports
    as ``RISK_MAX_LEVERAGE_UNKNOWN``. It never means "unlimited", and there is
    no path here that returns a fallback.
    """
    if stop_loss_percent is None or stop_loss_percent <= ZERO:
        # No stop distance, no derivation. A position with no stop is not
        # something to pick a leverage ceiling for by guessing.
        return None
    if requested_margin <= ZERO or configured_max_leverage < LEVERAGE_MIN:
        return None

    # 1. Liquidation must stay outside the stop, with room to spare.
    from_liquidation = _HUNDRED / (stop_loss_percent * LIQUIDATION_SAFETY_FACTOR)

    # 2. A stop-out must fit inside what is left of the day's loss budget.
    #    daily_loss_limit is negative; remaining budget is how much further the
    #    day may fall before the limit engages.
    remaining_budget = session_realized_pnl - daily_loss_limit
    if remaining_budget <= ZERO:
        # The budget is already spent. The daily lock refuses the entry on its
        # own authority; returning the domain minimum here keeps this function
        # from being the thing that reports it, so the refusal carries the
        # DAILY_LOSS_LIMIT code rather than a leverage one.
        return LEVERAGE_MIN

    loss_per_leverage = requested_margin * stop_loss_percent / _HUNDRED
    from_budget = remaining_budget / loss_per_leverage if loss_per_leverage > ZERO else LEVERAGE_MAX

    ceiling = min(from_liquidation, from_budget, configured_max_leverage, LEVERAGE_MAX)
    # Truncate: a fractional ceiling rounded up would be a ceiling the
    # derivation did not support.
    ceiling = ceiling.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    if ceiling < LEVERAGE_MIN:
        # Every constraint says less than 1x, which is not a leverage the
        # domain can express. 1x borrows nothing, so it is the floor rather
        # than a refusal -- the position size is what should shrink, and the
        # sizing checks are what enforce that.
        return LEVERAGE_MIN
    return ceiling
