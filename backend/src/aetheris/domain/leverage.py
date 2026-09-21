"""Leverage request and decision models.

The architecture supports a **candidate** range of 1x to 500x. That is the range
an analysis layer may *ask* for. It is emphatically not a range this system may
use: nothing here authorises leverage, and in this build nothing can.

Four values, deliberately separate fields rather than one number:

``requested_leverage``
    What analysis asked for, 1-500. A request, never a permission.

``exchange_max_leverage``
    The venue's real ceiling for this specific symbol. **Nullable, and null
    today**: venues typically serve leverage brackets only from an
    authenticated endpoint, and this build holds no credentials. It is never
    guessed, and never assumed to be 500 -- most perpetuals cap far below that,
    and the cap varies by symbol and by notional tier.

``risk_max_leverage``
    The ceiling the risk engine derives from volatility, stop distance,
    liquidation distance, account equity, position size and the daily loss
    limit. Nullable until that engine exists (phase 7).

``approved_leverage``
    What may actually be used. Null unless every constraint is known.

The rule, in one line::

    approved = min(requested, exchange_max, risk_max)   -- only if all are known

**Fail closed.** If any constraint is unknown the decision is a rejection with
a reason, not a fallback to the request. An unknown ceiling is not permission
to use the requested value.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: The architectural candidate range. Analysis may request anything in here;
#: approval is a separate question answered by the risk engine.
LEVERAGE_MIN: Final = Decimal(1)
LEVERAGE_MAX: Final = Decimal(500)


class LeverageOutcome(StrEnum):
    """What happened to a leverage request."""

    #: Every constraint known, and the request sits within all of them.
    APPROVED = "APPROVED"
    #: Every constraint known, but a lower ceiling binds; approved < requested.
    REDUCED = "REDUCED"
    #: No approved value at all. Always carries a reason.
    REJECTED = "REJECTED"

    @property
    def is_usable(self) -> bool:
        return self in (LeverageOutcome.APPROVED, LeverageOutcome.REDUCED)


class LeverageReason(StrEnum):
    """Machine-readable reason for the decision.

    Shares the ``RISK_REJECTED_*`` prefix used everywhere else a refusal is
    issued, so operators grep one vocabulary.
    """

    APPROVED_IN_FULL = "LEVERAGE_APPROVED_IN_FULL"
    BOUND_BY_EXCHANGE_MAX = "LEVERAGE_REDUCED_TO_EXCHANGE_MAX"
    BOUND_BY_RISK_MAX = "LEVERAGE_REDUCED_TO_RISK_MAX"

    #: Rejections. Each means a constraint could not be established.
    EXCHANGE_MAX_UNKNOWN = "RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN"
    RISK_MAX_UNKNOWN = "RISK_REJECTED_RISK_MAX_LEVERAGE_UNKNOWN"
    #: A constraint was supplied but is not a usable ceiling -- wrong type, or
    #: outside the modelled domain. Distinct from UNKNOWN: something was given,
    #: and it was wrong, which is a different problem to diagnose.
    EXCHANGE_MAX_INVALID = "RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_INVALID"
    RISK_MAX_INVALID = "RISK_REJECTED_RISK_MAX_LEVERAGE_INVALID"
    RISK_ENGINE_UNAVAILABLE = "RISK_REJECTED_RISK_ENGINE_UNAVAILABLE"
    REQUEST_OUT_OF_RANGE = "RISK_REJECTED_LEVERAGE_OUT_OF_RANGE"
    NO_DIRECTIONAL_BIAS = "RISK_REJECTED_NO_DIRECTIONAL_BIAS"
    INSUFFICIENT_DATA = "RISK_REJECTED_INSUFFICIENT_DATA_FOR_LEVERAGE"


class LeverageRequest(BaseModel):
    """A leverage candidate produced by analysis.

    Carrying the basis explicitly matters: a number with no stated derivation
    invites the reader to assume a model behind it. There is no model -- the
    basis is arithmetic over measured volatility, stated in ``basis``.
    """

    model_config = ConfigDict(frozen=True)

    requested_leverage: Decimal = Field(ge=LEVERAGE_MIN, le=LEVERAGE_MAX)
    basis: str = Field(description="How the candidate was derived, in plain language")
    inputs: dict[str, Decimal] = Field(
        default_factory=dict, description="The measurements the candidate came from"
    )


class LeverageDecision(BaseModel):
    """The full constraint chain and its outcome.

    Every field is reported even when null, so a reader can see *which* link in
    the chain was missing rather than just that the answer was no.
    """

    model_config = ConfigDict(frozen=True)

    requested_leverage: Decimal | None = None
    exchange_max_leverage: Decimal | None = Field(
        default=None,
        description="Venue ceiling for this symbol. Null when unknown; never guessed.",
    )
    risk_max_leverage: Decimal | None = Field(
        default=None, description="Risk engine ceiling. Null until that engine exists."
    )
    approved_leverage: Decimal | None = Field(
        default=None, description="Usable leverage. Null unless every constraint is known."
    )

    outcome: LeverageOutcome
    reason: LeverageReason
    detail: str
    #: Which ceiling bound the result, when one did.
    binding_constraint: str | None = None

    basis: str | None = None
    inputs: dict[str, Decimal] = Field(default_factory=dict)

    #: Restated on the model so it survives being copied into a log or payload.
    note: str = (
        "Leverage architecture only. Analysis may request leverage; the Risk Engine "
        "has final authority and no leverage is set, and no order is placed, by this "
        "system in any mode."
    )

    @property
    def is_usable(self) -> bool:
        return self.outcome.is_usable and self.approved_leverage is not None
