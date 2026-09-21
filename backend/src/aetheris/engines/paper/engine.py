"""The paper trading engine.

**Simulation only. No order reaches any venue from this file or anything it
calls.** The engine imports no HTTP client, no adapter and no framework -- it
is given prices and it does arithmetic. That is not a stylistic choice: it is
what makes it impossible for a bug here to become a real order, and what lets
every path below be tested without a network.

The pipeline is fixed and one-directional::

    market data -> fill price -> size against venue filters -> RISK GATE
                -> order -> position -> management -> close -> PnL

The risk gate is the only door. A caller cannot skip it, and nothing
downstream can overrule it -- strategy output, a leverage candidate and a
button in the browser all arrive at the same function with the same authority,
which is none.

Where the simulation is thinner than reality, this module says so rather than
letting the numbers imply otherwise:

* **Fills are all-or-nothing** at a single observed price. The domain carries a
  list of fills so partial execution needs no reshaping, but nothing here
  produces one.
* **Management is poll-driven.** A stop is noticed on a tick, not the instant
  it is touched, so it fills at the price observed *then* -- which can be well
  past the stop level. Real stop-market orders slip too, so this errs toward
  reality rather than away from it, but it means a realised loss can exceed
  the stop distance.
* **Liquidation is modelled optimistically**, as loss equal to posted margin.
  Real venues liquidate earlier, on a maintenance-margin tier that varies by
  symbol and notional, and charge a fee for it.
* **Funding payments are not modelled at all.** A perpetual held across funding
  intervals pays or receives; this simulation does neither.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus
from aetheris.core.money import USDT_EXPONENT, ZERO, floor_to_step, quantize_usdt
from aetheris.domain.enums import OrderSide, OrderState, OrderType, PositionSide, TradingMode
from aetheris.domain.leverage import LeverageDecision
from aetheris.domain.market import SymbolFilters
from aetheris.domain.paper import (
    DailySession,
    PaperAccount,
    PaperExitReason,
    PaperFill,
    PaperOrder,
    PaperOrderResult,
    PaperPosition,
    PaperTrade,
    ReconciliationReport,
    RiskLockState,
)
from aetheris.engines.paper.risk import (
    RiskRefusal,
    check_authority,
    check_entry,
    check_market_data,
)
from aetheris.engines.paper.state import MutablePosition, MutableSession, PaperState
from aetheris.engines.paper.store import PaperRepository

__all__ = [
    "ASSUMPTIONS",
    "MarkPrice",
    "PaperEngine",
    "PaperEngineConfig",
    "SubmitOrderRequest",
]

BPS: Final = Decimal(10000)

#: A minimum representable size, used only to record the *shape* of a rejected
#: order whose quantity never resolved. It is never filled and never sized
#: from -- the domain model requires a positive quantity, and inventing a
#: plausible one would be worse than this obviously-nominal placeholder.
NOMINAL_QUANTITY: Final = Decimal("0.00000001")

#: Returned on every account snapshot. A simulated PnL without its fill model
#: is not interpretable, and burying this in documentation would let the number
#: travel further than the caveat.
ASSUMPTIONS: Final[tuple[str, ...]] = (
    "SIMULATION ONLY: no order is sent to any exchange, no API credential exists, "
    "and no real funds are involved.",
    "Fills use a real observed price: the venue's best ask for a buy and best bid "
    "for a sell, falling back to the last traded price moved adversely by modelled "
    "slippage when the venue publishes no book.",
    "An order is refused outright when market data is unavailable, stale, or of "
    "unverifiable age. No fill is ever computed from an invented price.",
    "Fills are all-or-nothing. Partial fills, order-book depth and queue position "
    "are NOT modelled.",
    "Position management is poll-driven: stops and targets are evaluated when a "
    "tick is requested, and fill at the price observed then, which may be past the "
    "level. A realised loss can therefore exceed the stop distance.",
    "Liquidation is modelled optimistically as a loss equal to posted margin. Real "
    "venues liquidate earlier at a maintenance-margin threshold that varies by "
    "symbol and notional tier, and charge a liquidation fee.",
    "NOT modelled: funding payments, borrow costs, maker rebates and fee tiers, "
    "exchange downtime, and order rejection by the venue.",
    "Daily profit-target and loss-limit locks are evaluated on REALISED PnL and "
    "stop new entries only. They never close an open position.",
)


@dataclass(frozen=True, slots=True)
class PaperEngineConfig:
    """The engine's whole envelope, passed in rather than read from settings.

    Keeping configuration an argument is what lets a test construct an engine
    with a two-position ceiling in one line, and keeps this layer free of the
    deployment concerns that belong to the service above it.
    """

    starting_balance: Decimal
    daily_profit_target: Decimal
    daily_loss_limit: Decimal
    max_open_positions: int
    max_position_notional: Decimal
    max_portfolio_exposure: Decimal
    max_data_age_seconds: float
    #: Per side, on notional. 5 bps matches the backtest engine's default so
    #: the two simulations are comparable.
    taker_fee_bps: Decimal = Decimal(5)
    #: Applied only when the venue publishes no bid/ask to fill against.
    slippage_bps: Decimal = Decimal(2)
    #: Memory ceilings. The target machine is small and these are lists that
    #: would otherwise grow for as long as the process runs.
    max_order_log: int = 200
    max_trade_log: int = 200


@dataclass(frozen=True, slots=True)
class MarkPrice:
    """A price observation, already unwrapped from its envelope.

    Everything the engine needs to decide whether it may act, and to fill
    against the right side of the book. ``status`` and ``age_seconds`` travel
    with the price so the engine cannot use one without the other.
    """

    symbol: str
    status: DataStatus
    source: str
    last_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    age_seconds: float | None = None
    detail: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.status is DataStatus.OK and self.last_price is not None


@dataclass(frozen=True, slots=True)
class SubmitOrderRequest:
    """What a caller asks for. Exactly one of ``quantity`` or ``margin``.

    Sizing by margin is the natural expression for an account this small --
    "put 12 USDT behind this" -- while sizing by quantity is what a venue
    actually takes. Both are accepted; neither is inferred when the other is
    absent, because guessing a size is guessing a risk.
    """

    symbol: str
    side: OrderSide
    quantity: Decimal | None = None
    margin: Decimal | None = None
    stop_loss_percent: Decimal | None = None
    take_profit_percent: Decimal | None = None
    trailing_stop_percent: Decimal | None = None
    client_order_id: str | None = None


def _fill_price(
    mark: MarkPrice, *, side_is_buy: bool, slippage_bps: Decimal
) -> tuple[Decimal, str]:
    """The price a market order would realistically get, and where it came from.

    Preferring the venue's own best ask (buy) or best bid (sell) makes the
    spread a *measured* cost rather than a modelled one. Only when the venue
    publishes no book does slippage become an assumption, and the returned
    basis string says which of the two happened -- so a fill never claims more
    precision than it had.
    """
    if side_is_buy and mark.ask_price is not None and mark.ask_price > ZERO:
        return mark.ask_price, f"{mark.source}:best_ask"
    if not side_is_buy and mark.bid_price is not None and mark.bid_price > ZERO:
        return mark.bid_price, f"{mark.source}:best_bid"

    # Fallback. The last trade is a price someone got, not one on offer, so it
    # moves adversely rather than being taken at face value.
    if mark.last_price is None:  # pragma: no cover - callers check is_usable first
        raise ValueError(f"{mark.symbol} has no usable price to fill against")
    adjustment = slippage_bps / BPS
    factor = (Decimal(1) + adjustment) if side_is_buy else (Decimal(1) - adjustment)
    return mark.last_price * factor, f"{mark.source}:last_price+{slippage_bps}bps_slippage"


def _liquidation_price(entry: Decimal, leverage: Decimal, *, is_long: bool) -> Decimal | None:
    """Where loss equals posted margin. ``None`` when that is unreachable.

    At 1x a long can only lose its margin at a price of zero, which is not a
    liquidation level but the asset ceasing to have value. Reporting ``None``
    says that plainly; reporting ``0.00`` would put a liquidation price on the
    screen that no mechanism can reach.
    """
    move = entry / leverage
    level = (entry - move) if is_long else (entry + move)
    return level if level > ZERO else None


class PaperEngine:
    """Simulated order execution and position accounting.

    One account, held by the repository. The engine mutates that state and
    returns frozen snapshots, so a caller can never hold a reference that
    changes underneath it.
    """

    def __init__(self, repository: PaperRepository, config: PaperEngineConfig) -> None:
        self._repository = repository
        self._config = config

    @property
    def config(self) -> PaperEngineConfig:
        return self._config

    def replay(
        self,
        client_order_id: str,
        *,
        now: datetime,
        marks: Mapping[str, MarkPrice] | None = None,
    ) -> PaperOrderResult | None:
        """The original outcome for an id already seen, or ``None``.

        Consulted *before* the risk engine rules, because a retry after a
        dropped response must return what happened the first time -- not a
        fresh verdict. Re-evaluating would mean a client that retried got a
        different answer to the one already applied, which is the exact failure
        idempotency exists to prevent: the first attempt opened a position and
        the second reports a cooldown refusal for it.
        """
        state = self._repository.load()
        existing = state.orders_by_client_id.get(client_order_id)
        if existing is None:
            return None
        self._ensure_session(state, now)
        all_marks = dict(marks or {})
        return PaperOrderResult(
            accepted=existing.is_filled,
            order=existing.model_copy(update={"idempotent_replay": True}),
            position=self._position_model(state.positions.get(existing.symbol), now, all_marks),
            account=self._build_account(state, now=now, marks=all_marks),
            detail=(
                f"Client order id {client_order_id} was already submitted; the original "
                "outcome is returned unchanged and nothing new was opened or ruled on."
            ),
        )

    @property
    def emergency_stopped(self) -> bool:
        return self._repository.load().emergency_stopped

    @property
    def emergency_reason(self) -> str | None:
        return self._repository.load().emergency_reason

    def refuse(
        self,
        request: SubmitOrderRequest,
        *,
        now: datetime,
        code: RiskRejectionCode,
        detail: str,
        leverage: LeverageDecision,
        marks: Mapping[str, MarkPrice] | None = None,
        checks_performed: tuple[str, ...] = (),
        risk_max_leverage: Decimal | None = None,
    ) -> PaperOrderResult:
        """Record a refusal the risk engine issued before the gate was reached.

        The order still enters the log with its code, exactly as a refusal from
        this engine's own gate would. A risk verdict that left no trace in the
        account history would make the authority invisible in the one place a
        user looks for it.
        """
        state = self._repository.load()
        self._ensure_session(state, now)
        symbol = request.symbol.upper()
        client_order_id = request.client_order_id or f"paper-{state.next_sequence()}"
        return self._reject(
            state,
            symbol,
            request,
            client_order_id,
            RiskRefusal(code, detail),
            now,
            dict(marks or {}),
            leverage=leverage,
            checks_performed=checks_performed,
            risk_max_leverage=risk_max_leverage,
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def repository(self) -> PaperRepository:
        """The store behind this engine, for startup wiring only.

        Exposed so a durable store can be attached beside it without the
        engine itself learning about databases. Nothing on the request path
        reaches through this.
        """
        return self._repository

    def state(self) -> PaperState:
        """The live state, for a durable store to write down.

        Read-only by convention: the caller serialises it and must not
        mutate it. Returning the live object rather than a copy keeps the
        write cheap, and every caller holds the service write lock, so
        nothing else can be changing it at the same moment.
        """
        return self._repository.load()

    def restore(self, state: PaperState) -> None:
        """Adopt state read back from durable storage at startup.

        Refused unless the repository supports whole-state replacement.
        A store that cannot be replaced has nothing to restore into, and
        pretending otherwise would leave the engine running on a fresh
        account while reporting a restored one.
        """
        replace = getattr(self._repository, "replace", None)
        if replace is None:  # pragma: no cover - only the in-memory store exists
            raise TypeError("this repository cannot adopt a restored state")
        replace(state)

    def reconcile(self, *, now: datetime) -> ReconciliationReport:
        """Ask the store whether local state agrees with an authority.

        Exposed now, before there is anything to reconcile against, so the
        durable implementation slots in without the callers changing.
        """
        return self._repository.reconcile(now)

    def open_symbols(self) -> tuple[str, ...]:
        """Symbols needing a price. The service fetches exactly these."""
        return tuple(self._repository.load().positions)

    def snapshot(
        self, *, now: datetime, marks: Mapping[str, MarkPrice] | None = None
    ) -> PaperAccount:
        """Mark the account to the supplied prices without changing anything.

        Deliberately read-only. Marking is what a read does; closing positions
        is what a tick does, and conflating the two would let a browser tab
        refreshing itself realise a loss.
        """
        state = self._repository.load()
        self._ensure_session(state, now)
        return self._build_account(state, now=now, marks=marks or {})

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def submit_order(
        self,
        request: SubmitOrderRequest,
        *,
        now: datetime,
        mark: MarkPrice,
        filters: SymbolFilters | None,
        leverage: LeverageDecision,
        paper_enabled: bool,
        marks: Mapping[str, MarkPrice] | None = None,
        risk_verdict_detail: str | None = None,
        checks_performed: tuple[str, ...] = (),
        risk_max_leverage: Decimal | None = None,
    ) -> PaperOrderResult:
        """Open a position, or explain precisely why not.

        ``risk_verdict_detail`` is provenance only: when a phase 7 risk engine
        ruled on this order first, its verdict is recorded on the result. It is
        **never** read as permission. Every check below runs identically
        whether or not something upstream already approved the order, which is
        what makes two independent gates two gates rather than one.
        """
        state = self._repository.load()
        self._ensure_session(state, now)
        symbol = request.symbol.upper()
        all_marks = dict(marks or {})
        all_marks.setdefault(symbol, mark)
        client_order_id = request.client_order_id or f"paper-{state.next_sequence()}"

        replay = state.orders_by_client_id.get(client_order_id)
        if replay is not None:
            # A retry after a dropped response must not open a second position.
            return PaperOrderResult(
                accepted=replay.is_filled,
                order=replay.model_copy(update={"idempotent_replay": True}),
                position=self._position_model(state.positions.get(symbol), now, all_marks),
                account=self._build_account(state, now=now, marks=all_marks),
                detail=(
                    f"Client order id {client_order_id} was already submitted; the "
                    "original outcome is returned unchanged and nothing new was opened."
                ),
            )

        # Whether this account may open anything at all, asked before anything
        # about the order is examined. A size cannot be validated before it is
        # computed, so without this an unsizeable order submitted during an
        # emergency stop would report a step-size problem and never mention the
        # stop. The full gate asks again; this is about which reason is
        # reported, not about whether the refusal happens.
        authority_problem = check_authority(state, paper_enabled=paper_enabled)
        if authority_problem is not None:
            return self._reject(
                state, symbol, request, client_order_id, authority_problem, now, all_marks
            )

        # A price must be usable before a size can be computed from it. The full
        # gate re-checks this; here it is what makes the arithmetic possible.
        data_problem = check_market_data(
            symbol,
            mark_status=mark.status,
            mark_age_seconds=mark.age_seconds,
            max_data_age_seconds=self._config.max_data_age_seconds,
        )
        if data_problem is not None or not mark.is_usable:
            refusal = data_problem or RiskRefusal(
                RiskRejectionCode.STALE_DATA,
                mark.detail or f"No usable price is available for {symbol}.",
            )
            return self._reject(state, symbol, request, client_order_id, refusal, now, all_marks)

        sizing_problem = self._validate_sizing(request)
        if sizing_problem is not None:
            return self._reject(
                state, symbol, request, client_order_id, sizing_problem, now, all_marks
            )

        # Leverage is resolved before sizing because margin and notional differ
        # by exactly that factor. An unapproved request stops here.
        if not leverage.is_usable or leverage.approved_leverage is None:
            return self._reject(
                state,
                symbol,
                request,
                client_order_id,
                RiskRefusal(
                    RiskRejectionCode.MAX_LEVERAGE,
                    f"Leverage was not approved: {leverage.detail}",
                ),
                now,
                all_marks,
                leverage=leverage,
                checks_performed=checks_performed,
                risk_max_leverage=risk_max_leverage,
            )
        approved_leverage = leverage.approved_leverage

        side_is_buy = request.side is OrderSide.BUY
        price, price_source = _fill_price(
            mark, side_is_buy=side_is_buy, slippage_bps=self._config.slippage_bps
        )

        quantity, quantity_problem = self._resolve_quantity(
            request, price=price, leverage=approved_leverage, filters=filters
        )
        if quantity_problem is not None:
            return self._reject(
                state,
                symbol,
                request,
                client_order_id,
                quantity_problem,
                now,
                all_marks,
                leverage=leverage,
                checks_performed=checks_performed,
                risk_max_leverage=risk_max_leverage,
            )

        notional = quantize_usdt(quantity * price)
        margin = quantize_usdt(notional / approved_leverage)
        if (
            filters is not None
            and filters.min_notional is not None
            and notional < filters.min_notional
        ):
            return self._reject(
                state,
                symbol,
                request,
                client_order_id,
                RiskRefusal(
                    RiskRejectionCode.MIN_NOTIONAL,
                    f"Notional {notional} is below the venue's published minimum of "
                    f"{filters.min_notional} for {symbol}.",
                ),
                now,
                all_marks,
                leverage=leverage,
                checks_performed=checks_performed,
                risk_max_leverage=risk_max_leverage,
            )

        # THE GATE.
        gate_refusal = check_entry(
            state,
            paper_enabled=paper_enabled,
            symbol=symbol,
            quantity=quantity,
            notional=notional,
            margin_required=margin,
            mark_status=mark.status,
            mark_age_seconds=mark.age_seconds,
            max_data_age_seconds=self._config.max_data_age_seconds,
            leverage=leverage,
            max_open_positions=self._config.max_open_positions,
            max_position_notional=self._config.max_position_notional,
            max_portfolio_exposure=self._config.max_portfolio_exposure,
            available_balance=self._available_balance(state),
        )
        if gate_refusal is not None:
            return self._reject(
                state,
                symbol,
                request,
                client_order_id,
                gate_refusal,
                now,
                all_marks,
                leverage=leverage,
                checks_performed=checks_performed,
                risk_max_leverage=risk_max_leverage,
            )

        return self._open_position(
            state,
            request,
            symbol=symbol,
            client_order_id=client_order_id,
            price=price,
            price_source=price_source,
            price_age_seconds=mark.age_seconds,
            quantity=quantity,
            notional=notional,
            margin=margin,
            leverage=leverage,
            now=now,
            marks=all_marks,
            risk_verdict_detail=risk_verdict_detail,
            checks_performed=checks_performed,
            risk_max_leverage=risk_max_leverage,
        )

    def tick(
        self, *, now: datetime, marks: Mapping[str, MarkPrice]
    ) -> tuple[PaperAccount, tuple[PaperTrade, ...]]:
        """Manage open positions against fresh prices.

        Returns the account and any trades this pass closed. A position whose
        price is unusable is left alone and reports no unrealised PnL, rather
        than being managed against a level derived from stale data.
        """
        state = self._repository.load()
        self._ensure_session(state, now)
        closed: list[PaperTrade] = []

        for symbol in tuple(state.positions):
            position = state.positions.get(symbol)
            if position is None:  # pragma: no cover - defensive
                continue
            mark = marks.get(symbol)
            usable = (
                mark is not None
                and mark.is_usable
                and check_market_data(
                    symbol,
                    mark_status=mark.status,
                    mark_age_seconds=mark.age_seconds,
                    max_data_age_seconds=self._config.max_data_age_seconds,
                )
                is None
            )
            if mark is None or not usable or mark.last_price is None:
                position.mark_price = None
                position.mark_source = mark.source if mark is not None else None
                position.mark_status = (
                    mark.status.value if mark is not None else DataStatus.UNAVAILABLE.value
                )
                continue

            position.mark_price = mark.last_price
            position.mark_source = mark.source
            position.mark_status = mark.status.value
            position.updated_at = now

            reason = self._triggered_exit(position, mark.last_price)
            if reason is not None:
                closed.append(self._close(state, position, mark=mark, reason=reason, now=now))
            else:
                self._advance_trail(position, mark.last_price)

        state.updated_at = now
        self._repository.save(state)
        return self._build_account(state, now=now, marks=marks), tuple(closed)

    def close_position(
        self,
        symbol: str,
        *,
        now: datetime,
        mark: MarkPrice,
        reason: PaperExitReason = PaperExitReason.MANUAL_CLOSE,
        marks: Mapping[str, MarkPrice] | None = None,
    ) -> PaperOrderResult:
        """Close one position at the current observed price."""
        state = self._repository.load()
        self._ensure_session(state, now)
        key = symbol.upper()
        all_marks = dict(marks or {})
        all_marks.setdefault(key, mark)
        position = state.positions.get(key)
        client_order_id = f"paper-close-{state.next_sequence()}"
        closing_request = SubmitOrderRequest(
            symbol=key,
            side=(
                OrderSide.SELL
                if position is None or position.side == PositionSide.LONG.value
                else OrderSide.BUY
            ),
        )

        if position is None:
            return self._reject(
                state,
                key,
                closing_request,
                client_order_id,
                RiskRefusal(
                    RiskRejectionCode.POSITION_SIZE,
                    f"No open paper position in {key} to close.",
                ),
                now,
                all_marks,
                reduce_only=True,
            )

        data_problem = check_market_data(
            key,
            mark_status=mark.status,
            mark_age_seconds=mark.age_seconds,
            max_data_age_seconds=self._config.max_data_age_seconds,
        )
        if data_problem is not None or not mark.is_usable:
            # A close is refused on bad data exactly as an open is. Closing at
            # an invented price would book a fabricated PnL into the balance,
            # which is worse than leaving the position open and saying why.
            return self._reject(
                state,
                key,
                closing_request,
                client_order_id,
                data_problem
                or RiskRefusal(
                    RiskRejectionCode.STALE_DATA,
                    mark.detail or f"No usable price is available to close {key}.",
                ),
                now,
                all_marks,
                reduce_only=True,
            )

        trade = self._close(state, position, mark=mark, reason=reason, now=now)
        order = PaperOrder(
            order_id=f"paper-order-{state.next_sequence()}",
            client_order_id=client_order_id,
            symbol=key,
            side=closing_request.side,
            order_type=OrderType.MARKET,
            state=OrderState.FILLED,
            reduce_only=True,
            requested_quantity=trade.quantity,
            filled_quantity=trade.quantity,
            average_fill_price=trade.exit_price,
            created_at=now,
            updated_at=now,
        )
        state.orders_by_client_id[client_order_id] = order
        self._record_order(state, order)
        state.updated_at = now
        self._repository.save(state)
        return PaperOrderResult(
            accepted=True,
            order=order,
            trade=trade,
            account=self._build_account(state, now=now, marks=all_marks),
            detail=(
                f"PAPER position in {key} closed at {trade.exit_price} for a net "
                f"{trade.net_pnl} USDT. Simulation only; no order was sent anywhere."
            ),
        )

    def reset(self, *, now: datetime, starting_balance: Decimal | None = None) -> PaperAccount:
        """Discard the account and start again."""
        balance = (
            starting_balance if starting_balance is not None else self._config.starting_balance
        )
        state = self._repository.reset(starting_balance=balance, now=now)
        self._ensure_session(state, now)
        return self._build_account(state, now=now, marks={})

    def set_emergency_stop(self, *, engaged: bool, reason: str, now: datetime) -> PaperAccount:
        """Block or unblock new entries.

        It stops *opening* and nothing else. Flattening the book is a separate,
        deliberate action, because an emergency switch that fires market orders
        is itself a way to lose money badly.
        """
        state = self._repository.load()
        self._ensure_session(state, now)
        state.emergency_stopped = engaged
        state.emergency_reason = reason if engaged else None
        state.updated_at = now
        self._repository.save(state)
        return self._build_account(state, now=now, marks={})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_session(self, state: PaperState, now: datetime) -> MutableSession:
        """Start a new UTC day when one has begun.

        The daily limits are a day's budget, so they reset on the day boundary
        -- including the lock. A lock that survived midnight would be a
        permanent stop wearing a daily name.
        """
        today = now.date()
        session = state.session
        if session is None or session.session_date != today:
            session = MutableSession(
                session_date=today,
                profit_target=self._config.daily_profit_target,
                loss_limit=self._config.daily_loss_limit,
            )
            state.session = session
        return session

    def _available_balance(self, state: PaperState) -> Decimal:
        """Cash not already posted as margin.

        Measured from realised balance rather than equity. Real cross margin
        lets unrealised profit fund new positions; refusing to do that here is
        the conservative direction, and a paper engine stricter than the venue
        never flatters a strategy.
        """
        margin_used = sum((p.margin for p in state.positions.values()), ZERO)
        return state.balance - margin_used

    def _validate_sizing(self, request: SubmitOrderRequest) -> RiskRefusal | None:
        if (request.quantity is None) == (request.margin is None):
            return RiskRefusal(
                RiskRejectionCode.POSITION_SIZE,
                "Specify exactly one of quantity or margin. Supplying neither leaves "
                "the size undefined, and supplying both invites them to disagree.",
            )
        for value, name in ((request.quantity, "quantity"), (request.margin, "margin")):
            if value is not None and value <= ZERO:
                return RiskRefusal(
                    RiskRejectionCode.POSITION_SIZE, f"{name} must be greater than zero."
                )
        for percent, name in (
            (request.stop_loss_percent, "stop_loss_percent"),
            (request.take_profit_percent, "take_profit_percent"),
            (request.trailing_stop_percent, "trailing_stop_percent"),
        ):
            if percent is not None and (percent <= ZERO or percent >= Decimal(100)):
                return RiskRefusal(
                    RiskRejectionCode.POSITION_SIZE,
                    f"{name} must be between 0 and 100 exclusive; {percent} is not.",
                )
        return None

    def _resolve_quantity(
        self,
        request: SubmitOrderRequest,
        *,
        price: Decimal,
        leverage: Decimal,
        filters: SymbolFilters | None,
    ) -> tuple[Decimal, RiskRefusal | None]:
        """Turn the request into a quantity the venue's filters would accept.

        The filters are the venue's real, published constraints -- not a guess
        -- so honouring them here means a paper size is one that could actually
        have been submitted. Quantity always truncates down: rounding up can
        spend margin that is not there.
        """
        if request.quantity is not None:
            quantity = request.quantity
        elif request.margin is not None:
            quantity = (request.margin * leverage) / price
        else:  # pragma: no cover - _validate_sizing rejects this first
            return ZERO, RiskRefusal(RiskRejectionCode.POSITION_SIZE, "No size was supplied.")

        if filters is not None:
            quantity = floor_to_step(quantity, filters.step_size)
            if quantity <= ZERO:
                return ZERO, RiskRefusal(
                    RiskRejectionCode.EXCHANGE_PRECISION,
                    f"After truncating to the venue's step size of {filters.step_size}, "
                    "the order rounds to zero: the requested size is smaller than one "
                    "tradable increment.",
                )
            if filters.min_quantity > ZERO and quantity < filters.min_quantity:
                return quantity, RiskRefusal(
                    RiskRejectionCode.EXCHANGE_PRECISION,
                    f"Quantity {quantity} is below the venue's minimum of {filters.min_quantity}.",
                )
            if filters.max_quantity is not None and quantity > filters.max_quantity:
                return quantity, RiskRefusal(
                    RiskRejectionCode.POSITION_SIZE,
                    f"Quantity {quantity} exceeds the venue's maximum of {filters.max_quantity}.",
                )
        else:
            # No published filters (a unit test, or a symbol whose metadata did
            # not resolve). Truncate to the internal accounting exponent so a
            # size can never carry more precision than the system accounts in.
            quantity = floor_to_step(quantity, USDT_EXPONENT)
            if quantity <= ZERO:
                return ZERO, RiskRefusal(
                    RiskRejectionCode.POSITION_SIZE, "The resolved quantity is not positive."
                )
        return quantity, None

    def _open_position(
        self,
        state: PaperState,
        request: SubmitOrderRequest,
        *,
        symbol: str,
        client_order_id: str,
        price: Decimal,
        price_source: str,
        price_age_seconds: float | None,
        quantity: Decimal,
        notional: Decimal,
        margin: Decimal,
        leverage: LeverageDecision,
        now: datetime,
        marks: Mapping[str, MarkPrice],
        risk_verdict_detail: str | None = None,
        checks_performed: tuple[str, ...] = (),
        risk_max_leverage: Decimal | None = None,
    ) -> PaperOrderResult:
        approved = leverage.approved_leverage
        if approved is None:  # pragma: no cover - checked by the caller
            raise ValueError("_open_position requires an approved leverage")
        is_long = request.side is OrderSide.BUY
        fee = quantize_usdt(notional * self._config.taker_fee_bps / BPS)

        session = self._ensure_session(state, now)
        # The fee is realised the moment it is paid, so it moves balance and
        # the day's realised PnL together. That keeps one invariant true at all
        # times: balance == starting_balance + realized_pnl.
        state.balance = quantize_usdt(state.balance - fee)
        state.realized_pnl = quantize_usdt(state.realized_pnl - fee)
        state.total_fees = quantize_usdt(state.total_fees + fee)
        session.realized_pnl = quantize_usdt(session.realized_pnl - fee)
        session.fees = quantize_usdt(session.fees + fee)
        session.orders_submitted += 1

        stop: Decimal | None = None
        target: Decimal | None = None
        if request.stop_loss_percent is not None:
            move = price * request.stop_loss_percent / Decimal(100)
            stop = price - move if is_long else price + move
        if request.take_profit_percent is not None:
            move = price * request.take_profit_percent / Decimal(100)
            target = price + move if is_long else price - move

        order_id = f"paper-order-{state.next_sequence()}"
        position = MutablePosition(
            position_id=f"paper-position-{state.next_sequence()}",
            symbol=symbol,
            side=(PositionSide.LONG if is_long else PositionSide.SHORT).value,
            quantity=quantity,
            entry_price=price,
            notional=notional,
            margin=margin,
            approved_leverage=approved,
            entry_fee=fee,
            opened_at=now,
            updated_at=now,
            opening_order_id=order_id,
            stop_price=stop,
            target_price=target,
            trailing_stop_percent=request.trailing_stop_percent,
            trail_extreme=price if request.trailing_stop_percent is not None else None,
            liquidation_price=_liquidation_price(price, approved, is_long=is_long),
            mark_price=price,
            mark_source=price_source,
            mark_status=DataStatus.OK.value,
        )
        state.positions[symbol] = position

        order = PaperOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            state=OrderState.FILLED,
            requested_quantity=quantity,
            filled_quantity=quantity,
            average_fill_price=price,
            fills=(
                PaperFill(
                    fill_id=f"paper-fill-{state.next_sequence()}",
                    price=price,
                    quantity=quantity,
                    fee=fee,
                    filled_at=now,
                    price_source=price_source,
                    price_age_seconds=price_age_seconds,
                ),
            ),
            leverage=leverage,
            created_at=now,
            updated_at=now,
            risk_verdict_detail=risk_verdict_detail,
        )
        state.orders_by_client_id[client_order_id] = order
        self._record_order(state, order)
        state.updated_at = now
        self._repository.save(state)

        return PaperOrderResult(
            accepted=True,
            order=order,
            position=self._position_model(position, now, marks),
            account=self._build_account(state, now=now, marks=marks),
            checks_performed=checks_performed,
            risk_max_leverage=risk_max_leverage,
            detail=(
                f"PAPER order filled: {quantity} {symbol} at {price} ({price_source}), "
                f"{approved}x, margin {margin} USDT, fee {fee} USDT. Simulation only "
                "-- nothing was sent to an exchange."
            ),
        )

    def _triggered_exit(self, position: MutablePosition, price: Decimal) -> PaperExitReason | None:
        """Which level this price has reached, resolved against the position.

        A single poll can jump several levels at once, and nothing in a point
        observation says which was touched first. The same rule the backtester
        uses applies here: among the adverse levels reached, the one nearest
        entry fires, because that is the one price must have passed first. An
        adverse level always beats the target, which is what stops the
        simulation from flattering itself on an ambiguous move.
        """
        is_long = position.side == PositionSide.LONG.value

        def reached(level: Decimal | None) -> bool:
            if level is None:
                return False
            return price <= level if is_long else price >= level

        candidates: list[tuple[Decimal, PaperExitReason]] = []
        if reached(position.liquidation_price) and position.liquidation_price is not None:
            candidates.append((position.liquidation_price, PaperExitReason.LIQUIDATION))
        if reached(position.stop_price) and position.stop_price is not None:
            candidates.append((position.stop_price, PaperExitReason.STOP_LOSS))
        trailing = self._trailing_level(position)
        if reached(trailing) and trailing is not None:
            candidates.append((trailing, PaperExitReason.TRAILING_STOP))

        if candidates:
            # Adverse levels sit below entry for a long and above it for a
            # short, so nearest-to-entry is the highest / lowest respectively.
            nearest = (
                max(candidates, key=lambda item: item[0])
                if is_long
                else min(candidates, key=lambda item: item[0])
            )
            return nearest[1]

        target = position.target_price
        if target is not None and (price >= target if is_long else price <= target):
            return PaperExitReason.TAKE_PROFIT
        return None

    @staticmethod
    def _trailing_level(position: MutablePosition) -> Decimal | None:
        if position.trailing_stop_percent is None or position.trail_extreme is None:
            return None
        move = position.trail_extreme * position.trailing_stop_percent / Decimal(100)
        if position.side == PositionSide.LONG.value:
            return position.trail_extreme - move
        return position.trail_extreme + move

    @staticmethod
    def _advance_trail(position: MutablePosition, price: Decimal) -> None:
        """Ratchet the trailing extreme. Only ever in the favourable direction."""
        if position.trailing_stop_percent is None:
            return
        if position.trail_extreme is None:
            position.trail_extreme = price
        elif position.side == PositionSide.LONG.value:
            position.trail_extreme = max(position.trail_extreme, price)
        else:
            position.trail_extreme = min(position.trail_extreme, price)

    def _close(
        self,
        state: PaperState,
        position: MutablePosition,
        *,
        mark: MarkPrice,
        reason: PaperExitReason,
        now: datetime,
    ) -> PaperTrade:
        is_long = position.side == PositionSide.LONG.value
        exit_price, _source = _fill_price(
            mark, side_is_buy=not is_long, slippage_bps=self._config.slippage_bps
        )
        gross = (exit_price - position.entry_price) * position.quantity
        if not is_long:
            gross = -gross
        gross = quantize_usdt(gross)
        exit_notional = quantize_usdt(position.quantity * exit_price)
        exit_fee = quantize_usdt(exit_notional * self._config.taker_fee_bps / BPS)
        net = quantize_usdt(gross - exit_fee - position.entry_fee)

        session = self._ensure_session(state, now)
        # The entry fee left the balance when the position opened, so only the
        # gross result and the exit fee move it now. Double-charging the entry
        # fee here is the easy mistake; an invariant test exists to catch it.
        state.balance = quantize_usdt(state.balance + gross - exit_fee)
        state.realized_pnl = quantize_usdt(state.realized_pnl + gross - exit_fee)
        state.total_fees = quantize_usdt(state.total_fees + exit_fee)
        session.realized_pnl = quantize_usdt(session.realized_pnl + gross - exit_fee)
        session.fees = quantize_usdt(session.fees + exit_fee)
        session.trades_closed += 1

        trade = PaperTrade(
            trade_id=f"paper-trade-{state.next_sequence()}",
            symbol=position.symbol,
            side=PositionSide.LONG if is_long else PositionSide.SHORT,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            notional=position.notional,
            margin=position.margin,
            approved_leverage=position.approved_leverage,
            opened_at=position.opened_at,
            closed_at=now,
            exit_reason=reason,
            gross_pnl=gross,
            fees=quantize_usdt(position.entry_fee + exit_fee),
            net_pnl=net,
            return_percent=(
                quantize_usdt(net / position.margin * Decimal(100))
                if position.margin > ZERO
                else ZERO
            ),
            balance_after=state.balance,
        )
        state.trades.append(trade)
        del state.trades[: max(0, len(state.trades) - self._config.max_trade_log)]
        state.positions.pop(position.symbol, None)
        state.updated_at = now
        self._apply_daily_lock(session, now)
        return trade

    def _apply_daily_lock(self, session: MutableSession, now: datetime) -> None:
        """Engage the day's brake on realised PnL.

        Realised, not unrealised: an open position's fluctuation would toggle
        the lock tick by tick, and a brake that engages and releases on noise
        is not a limit.
        """
        if session.lock_state is not RiskLockState.NONE:
            return
        if session.realized_pnl >= session.profit_target:
            session.lock_state = RiskLockState.DAILY_PROFIT_TARGET
            session.lock_reason = (
                f"Daily profit target of {session.profit_target} USDT reached "
                f"(realised {session.realized_pnl} USDT). No new entries today; open "
                "positions are still managed."
            )
            session.locked_at = now
        elif session.realized_pnl <= session.loss_limit:
            session.lock_state = RiskLockState.DAILY_LOSS_LIMIT
            session.lock_reason = (
                f"Daily loss limit of {session.loss_limit} USDT reached "
                f"(realised {session.realized_pnl} USDT). No new entries today; open "
                "positions are still managed."
            )
            session.locked_at = now

    def _reject(
        self,
        state: PaperState,
        symbol: str,
        request: SubmitOrderRequest,
        client_order_id: str,
        refusal: RiskRefusal,
        now: datetime,
        marks: Mapping[str, MarkPrice],
        *,
        leverage: LeverageDecision | None = None,
        reduce_only: bool = False,
        checks_performed: tuple[str, ...] = (),
        risk_max_leverage: Decimal | None = None,
    ) -> PaperOrderResult:
        """Record a refusal as a first-class order, not a bare error.

        A rejected order stays in the log with its code, so the account history
        shows what was *attempted* as well as what happened. A blank where an
        order was refused is how a risk limit becomes invisible.
        """
        session = self._ensure_session(state, now)
        session.orders_rejected += 1
        requested_quantity = (
            request.quantity
            if request.quantity is not None and request.quantity > ZERO
            else NOMINAL_QUANTITY
        )
        order = PaperOrder(
            order_id=f"paper-order-{state.next_sequence()}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            state=OrderState.REJECTED,
            reduce_only=reduce_only,
            requested_quantity=requested_quantity,
            leverage=leverage,
            created_at=now,
            updated_at=now,
            rejection_code=refusal.code,
            rejection_detail=refusal.detail,
        )
        state.orders_by_client_id[client_order_id] = order
        self._record_order(state, order)
        state.updated_at = now
        self._repository.save(state)
        return PaperOrderResult(
            accepted=False,
            order=order,
            account=self._build_account(state, now=now, marks=marks),
            detail=f"{refusal.code.value}: {refusal.detail}",
            checks_performed=checks_performed,
            risk_max_leverage=risk_max_leverage,
        )

    def _record_order(self, state: PaperState, order: PaperOrder) -> None:
        """Append to the log, evicting the oldest past the ceiling.

        The idempotency index is trimmed with the log so the two cannot drift.
        Evicting an id does mean a replay of a very old one would be treated as
        new; the ceiling is set far above any plausible retry window.
        """
        state.order_log.append(order)
        overflow = len(state.order_log) - self._config.max_order_log
        if overflow > 0:
            for evicted in state.order_log[:overflow]:
                state.orders_by_client_id.pop(evicted.client_order_id, None)
            del state.order_log[:overflow]

    def _position_model(
        self,
        position: MutablePosition | None,
        now: datetime,
        marks: Mapping[str, MarkPrice],
    ) -> PaperPosition | None:
        if position is None:
            return None
        mark = marks.get(position.symbol)
        mark_price = position.mark_price
        mark_source = position.mark_source
        mark_status = position.mark_status
        if mark is not None:
            usable = (
                mark.is_usable
                and check_market_data(
                    position.symbol,
                    mark_status=mark.status,
                    mark_age_seconds=mark.age_seconds,
                    max_data_age_seconds=self._config.max_data_age_seconds,
                )
                is None
            )
            mark_price = mark.last_price if usable else None
            mark_source = mark.source
            mark_status = mark.status.value if usable else DataStatus.STALE.value

        unrealized: Decimal | None = None
        if mark_price is not None:
            move = (mark_price - position.entry_price) * position.quantity
            if position.side == PositionSide.SHORT.value:
                move = -move
            unrealized = quantize_usdt(move)

        return PaperPosition(
            position_id=position.position_id,
            symbol=position.symbol,
            side=PositionSide(position.side),
            quantity=position.quantity,
            entry_price=position.entry_price,
            notional=position.notional,
            margin=position.margin,
            approved_leverage=position.approved_leverage,
            entry_fee=position.entry_fee,
            stop_price=position.stop_price,
            target_price=position.target_price,
            trailing_stop_percent=position.trailing_stop_percent,
            trail_extreme=position.trail_extreme,
            liquidation_price=position.liquidation_price,
            opened_at=position.opened_at,
            updated_at=now,
            opening_order_id=position.opening_order_id,
            mark_price=mark_price,
            mark_source=mark_source,
            mark_status=mark_status,
            unrealized_pnl=unrealized,
        )

    def _build_account(
        self, state: PaperState, *, now: datetime, marks: Mapping[str, MarkPrice]
    ) -> PaperAccount:
        positions = tuple(
            model
            for model in (
                self._position_model(position, now, marks) for position in state.positions.values()
            )
            if model is not None
        )

        margin_used = quantize_usdt(sum((p.margin for p in positions), ZERO))
        marked = [p.unrealized_pnl for p in positions if p.unrealized_pnl is not None]
        # Null, not zero, when a position could not be marked. Zero would
        # assert the open positions are flat, which is a claim no observation
        # made. With no positions at all, zero is a fact rather than a guess.
        unrealized: Decimal | None
        if not positions:
            unrealized = ZERO
        elif len(marked) == len(positions):
            unrealized = quantize_usdt(sum(marked, ZERO))
        else:
            unrealized = None

        equity = quantize_usdt(state.balance + (unrealized if unrealized is not None else ZERO))
        session: DailySession = self._ensure_session(state, now).to_model()

        freshest = max(
            (p for p in positions if p.mark_source is not None),
            key=lambda p: p.updated_at,
            default=None,
        )
        return PaperAccount(
            account_id=state.account_id,
            mode=TradingMode.PAPER,
            created_at=state.created_at,
            updated_at=state.updated_at,
            starting_balance=state.starting_balance,
            balance=state.balance,
            equity=equity,
            available_balance=quantize_usdt(state.balance - margin_used),
            margin_used=margin_used,
            realized_pnl=state.realized_pnl,
            unrealized_pnl=unrealized,
            total_fees=state.total_fees,
            positions=positions,
            open_orders=(),
            recent_orders=tuple(reversed(state.order_log[-25:])),
            recent_trades=tuple(reversed(state.trades[-25:])),
            session=session,
            autonomous_enabled=state.autonomous_enabled,
            durability=self._repository.durability,
            mark_source=freshest.mark_source if freshest is not None else None,
            mark_status=freshest.mark_status if freshest is not None else None,
        )
