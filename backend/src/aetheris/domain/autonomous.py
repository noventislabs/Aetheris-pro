"""Autonomous paper-trading domain model.

The audit trail lives here. **Every iteration produces a record for every
symbol it looked at, including the ones where nothing happened** -- "the loop
did nothing for six hours" has to be distinguishable from "the loop was not
running", and both from "the loop died and nobody noticed". A decision log with
only the interesting entries in it cannot tell those apart.

Nothing in this module predicts anything. A decision carries the *counts* of
which named conditions held, the provenance of the data it read, and the
machine-readable reason it acted or refused. There is no confidence, no
probability, no score and no forecast, and the vocabulary test asserts that
none appears.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, PositionSide, Timeframe
from aetheris.domain.leverage import LeverageDecision
from aetheris.domain.paper import PaperExitReason


class AutonomousAction(StrEnum):
    """What the loop did about one symbol in one iteration."""

    #: A position was opened.
    ENTERED = "ENTERED"
    #: An entry was proposed and refused. Always carries a rejection code.
    REFUSED = "REFUSED"
    #: An open position was marked and its levels evaluated; nothing fired.
    MANAGED = "MANAGED"
    #: An open position was closed, by a level or by a signal flip.
    CLOSED = "CLOSED"
    #: The analysis ran and produced no directional bias. A finding, not a gap.
    NO_SIGNAL = "NO_SIGNAL"
    #: The symbol could not be evaluated: unusable data, or the analysis could
    #: not run. Distinct from NO_SIGNAL, which means it ran and said neutral.
    SKIPPED = "SKIPPED"


class AutonomousLoopState(StrEnum):
    """Whether the loop is running, and why not."""

    #: Autonomy is not permitted by configuration. The task is never created.
    DISABLED_BY_CONFIG = "DISABLED_BY_CONFIG"
    #: Permitted, but nobody has armed it. The default on every start.
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    #: The task raised. Autonomy was disarmed and is not restarted on its own.
    FAILED = "FAILED"


class AutonomousDecision(BaseModel):
    """One symbol, one iteration, one recorded outcome."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    sequence: int = Field(ge=0)
    decided_at: datetime

    symbol: str
    timeframe: Timeframe
    action: AutonomousAction
    #: One sentence a human can read without decoding the enum.
    detail: str

    #: The close time of the bar this decision was made on. Taken from the
    #: candle series rather than the clock, which is what makes the idempotency
    #: key stable across iterations within one bar.
    bar_close_time: datetime | None = None

    # --- what the analysis read -------------------------------------------
    strategy: str | None = None
    strategy_status: str | None = None
    bias: str | None = None
    #: Counts, deliberately not a ratio. 3/4 says exactly what it says and
    #: cannot be mistaken for a likelihood.
    conditions_met: int | None = Field(default=None, ge=0)
    conditions_total: int | None = Field(default=None, ge=0)

    # --- what the risk engine said ----------------------------------------
    rejection_code: RiskRejectionCode | None = None
    rejection_detail: str | None = None
    leverage: LeverageDecision | None = None
    risk_max_leverage: Decimal | None = None
    #: Named checks the risk engine actually performed, in order. An audit
    #: trail that only records the failing check cannot show what passed.
    checks_performed: tuple[str, ...] = ()

    # --- what it touched ---------------------------------------------------
    side: OrderSide | None = None
    position_side: PositionSide | None = None
    exit_reason: PaperExitReason | None = None
    client_order_id: str | None = None
    order_id: str | None = None
    position_id: str | None = None
    trade_id: str | None = None
    proposed_margin: Decimal | None = None
    realized_pnl: Decimal | None = None

    # --- provenance of what it decided on ----------------------------------
    source: str | None = None
    data_status: str | None = None
    data_age_seconds: float | None = None
    atr_percent: Decimal | None = None

    label: str = "PAPER / AUTONOMOUS / SIMULATION ONLY / NO REAL ORDER"


class AutonomousStatus(BaseModel):
    """The loop's own state, reported honestly including when it is off."""

    model_config = ConfigDict(frozen=True)

    state: AutonomousLoopState
    enabled: bool = Field(description="Whether the loop is armed. False on every process start.")
    permitted_by_config: bool = Field(
        description="AETHERIS_AUTONOMOUS_TRADING_ENABLED. Gates arming; never arms."
    )
    paper_mode_enabled: bool

    symbols: tuple[str, ...] = ()
    #: Symbols configured but dropped for this session, with the reason.
    excluded_symbols: dict[str, str] = Field(default_factory=dict)
    timeframe: Timeframe | None = None
    interval_seconds: float | None = None

    iterations: int = Field(default=0, ge=0)
    decisions_recorded: int = Field(default=0, ge=0)
    entries: int = Field(default=0, ge=0)
    refusals: int = Field(default=0, ge=0)
    closes: int = Field(default=0, ge=0)
    last_iteration_at: datetime | None = None
    last_iteration_duration_seconds: float | None = None
    #: Set only when state is FAILED. The exception type and message, so a
    #: silent death is impossible to mistake for a quiet market.
    failure_detail: str | None = None
    #: True while the venue has failed every symbol for several iterations.
    venue_outage: bool = False

    disclaimer: str = (
        "Autonomous paper trading is a simulation running against real public market "
        "data. No order is sent to any exchange, no API credential exists, and no real "
        "funds are involved. The risk engine has final authority over every proposal "
        "and nothing here can override it."
    )
