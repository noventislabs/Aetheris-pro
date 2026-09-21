"""Testnet execution: the durable path from a request to a venue order.

The order of operations is the design. Nothing reaches the venue that has not
first been written down, ruled on by the risk engine, and had its execution
parameters confirmed by the venue itself:

    request
      -> durable CREATED              (a crash here leaves a record)
      -> VALIDATING, risk engine      (the authority, unchanged)
      -> leverage bracket read        (the real ceiling, for this notional tier)
      -> leverage set AND verified    (approval is binding, not advisory)
      -> ISOLATED margin set AND verified
      -> durable SUBMITTED            (committed BEFORE the network call)
      -> venue
      -> durable ACK

Two properties are worth stating because they are easy to lose:

**There is no shortcut from the API to the venue.** Every step above is a
committed transaction, and the submission stamp is committed before the request
leaves. A crash in the window leaves ``submitted_at`` set with no
``venue_order_id``, which is exactly the signature phase 8a's recovery reads.

**Nothing here decides.** Sizing, leverage and permission come from the risk
engine; this module refuses when the engine refuses and refuses again when the
venue will not confirm what the engine approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from aetheris.adapters.exchange.binance.testnet_adapter import (
    BinanceTestnetTradingAdapter,
    HedgeModeNotSupportedError,
    MarginModeRefusedError,
)
from aetheris.adapters.exchange.errors import ExchangeError
from aetheris.analysis.indicators.prepare import prepare_candles
from aetheris.analysis.volatility import (
    VolatilityMeasurement,
    VolatilityStatus,
    measure_atr_percent,
)
from aetheris.core.config import Settings
from aetheris.core.errors import AetherisError, RiskRejectionCode
from aetheris.core.freshness import DataStatus, utcnow
from aetheris.core.money import ZERO, floor_to_step
from aetheris.domain.enums import OrderSide, OrderState, OrderType, Timeframe, TradingMode
from aetheris.domain.order import OrderIntent, OrderOrigin, OrderRecord, VenueOrderView
from aetheris.domain.paper import RiskLockState
from aetheris.domain.venue import MarginMode, VenueAccount, VenuePosition
from aetheris.engines.order.engine import OrderLifecycleEngine
from aetheris.engines.order.identity import build_intent_key
from aetheris.engines.risk.engine import evaluate as risk_evaluate
from aetheris.engines.risk.policy import (
    RiskAccountView,
    RiskMarketView,
    RiskPolicy,
    RiskProposal,
    RiskVerdict,
)
from aetheris.services.market_data import MarketDataService

__all__ = [
    "ReconciliationSweep",
    "TestnetExecutionService",
    "TestnetOrderRequest",
    "TestnetOrderResult",
    "TestnetUnavailableError",
    "VenueConnection",
    "VenueSnapshot",
]

#: Identifies the caller to the risk engine. Confers nothing: the engine rules
#: identically whichever origin proposed the order.
TESTNET_ORIGIN = "testnet-manual"

#: ATR(14) needs 15 closed bars; ~60 is ample and costs one call.
RISK_CANDLES = 60
RISK_TIMEFRAME = Timeframe.M15


class TestnetUnavailableError(ExchangeError):
    """Testnet execution was asked for and cannot be served safely."""


@dataclass(frozen=True, slots=True)
class TestnetOrderRequest:
    symbol: str
    side: OrderSide
    margin: Decimal
    requested_leverage: Decimal
    #: Required, not optional. The risk engine derives its own leverage ceiling
    #: from the stop distance, and without one it derives nothing -- which the
    #: constraint chain reports as RISK_MAX_LEVERAGE_UNKNOWN and refuses. An
    #: order with no stop is not something to pick a ceiling for by guessing.
    stop_loss_percent: Decimal
    #: Makes the derived identity stable for a retry. Two different intents
    #: must not share one, or the second would replay the first.
    intent_key: str
    take_profit_percent: Decimal | None = None


class VenueConnection(StrEnum):
    """Whether the venue answered, stated rather than inferred."""

    CONNECTED = "CONNECTED"
    #: Configured, reachable in principle, and not answering right now.
    UNAVAILABLE = "UNAVAILABLE"
    #: Not configured or not enabled. We never had it; this is not an incident.
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class VenueSnapshot:
    """One read of the venue, for display only.

    Deliberately not a trading input. Nothing in the execution path reads this;
    it exists so a screen can show what the venue says without any component
    having to decide what to do when a field is missing.
    """

    connection: VenueConnection
    venue: str
    account: VenueAccount | None = None
    positions: tuple[VenuePosition, ...] = ()
    open_orders: tuple[VenueOrderView, ...] = ()
    margin_mode: MarginMode | None = None
    observed_at: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationSweep:
    """What one pass over the unsettled orders found."""

    checked: int
    resolved: int
    unreachable: int
    #: True when every queried order vanished at once. Resolves nothing.
    venue_reset_suspected: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TestnetOrderResult:
    accepted: bool
    record: OrderRecord | None
    detail: str
    rejection_code: RiskRejectionCode | None = None
    #: True when the request replayed an order that already existed. A replay
    #: is never re-submitted to the venue.
    replayed: bool = False
    checks_performed: tuple[str, ...] = ()


class TestnetExecutionService:
    """Drives the durable lifecycle around the testnet adapter."""

    def __init__(
        self,
        adapter: BinanceTestnetTradingAdapter,
        orders: OrderLifecycleEngine,
        market_data: MarketDataService,
        settings: Settings,
        *,
        account_id: str,
        session_start: datetime,
    ) -> None:
        self._adapter = adapter
        self._orders = orders
        self._market_data = market_data
        self._settings = settings
        self._account_id = account_id
        self._session_start = session_start

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    async def submit(self, request: TestnetOrderRequest) -> TestnetOrderResult:
        now = utcnow()
        if not self._settings.is_mode_enabled(TradingMode.TESTNET):
            return TestnetOrderResult(
                accepted=False,
                record=None,
                detail="Testnet trading is not enabled.",
                rejection_code=RiskRejectionCode.MODE_NOT_ENABLED,
            )

        symbol = request.symbol.upper()

        # 1. One-way mode, checked before anything is written. Hedge mode is
        #    refused rather than adapted to: every order here says
        #    positionSide=BOTH, and in hedge mode that is not merely wrong, it
        #    can open a position against the one it meant to close.
        try:
            await self._adapter.require_one_way_mode()
        except HedgeModeNotSupportedError as exc:
            return TestnetOrderResult(
                accepted=False,
                record=None,
                detail=str(exc),
                rejection_code=RiskRejectionCode.MODE_NOT_ENABLED,
            )

        # 2. Write the intent down before anything else happens. create_or_get
        #    means a retry after a restart replays rather than duplicating.
        intent = OrderIntent(
            account_id=self._account_id,
            symbol=symbol,
            side=request.side,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.00000001"),  # provisional; re-derived after approval
            mode=TradingMode.TESTNET,
            origin=OrderOrigin.MANUAL,
            intent_key=build_intent_key(
                symbol=symbol, side=request.side, purpose="testnet", bucket=request.intent_key
            ),
            created_at=now,
        )
        record, created = await self._orders.create_or_get(intent)
        if not created:
            return TestnetOrderResult(
                accepted=record.state is not OrderState.REJECTED,
                record=record,
                detail=(
                    "This intent already has an order. Returning the persisted record "
                    "rather than placing a second one."
                ),
                replayed=True,
            )

        await self._orders.mark_validating(record.order_id, now=now)

        # 3. The risk engine. The authority, used exactly as the paper path
        #    uses it -- the same function, not a copy of its rules.
        verdict, detail = await self._rule(request, record, now)
        if verdict is None:
            await self._orders.mark_risk_rejected(
                record.order_id,
                code=RiskRejectionCode.STALE_DATA,
                detail=detail,
                now=now,
            )
            return TestnetOrderResult(
                accepted=False,
                record=await self._orders.repository.get(record.order_id),
                detail=detail,
                rejection_code=RiskRejectionCode.STALE_DATA,
            )
        if not verdict.approved:
            await self._orders.mark_risk_rejected(
                record.order_id,
                code=verdict.code or RiskRejectionCode.MODE_NOT_ENABLED,
                detail=verdict.detail,
                now=now,
            )
            return TestnetOrderResult(
                accepted=False,
                record=await self._orders.repository.get(record.order_id),
                detail=verdict.detail,
                rejection_code=verdict.code,
                checks_performed=verdict.checks_performed,
            )

        approved_leverage = verdict.leverage.approved_leverage if verdict.leverage else None
        if approved_leverage is None:
            # Unreachable when the engine approves, and refused anyway: an
            # order sized against an unknown ceiling is the failure the whole
            # leverage chain exists to prevent.
            return await self._refuse(
                record,
                RiskRejectionCode.MAX_LEVERAGE,
                "The risk engine approved without an approved leverage. Refusing.",
                now,
            )

        # 4. Make the venue agree, and verify that it did.
        try:
            applied = await self._adapter.set_leverage(symbol, approved_leverage)
        except ExchangeError as exc:
            return await self._refuse(
                record,
                RiskRejectionCode.MAX_LEVERAGE,
                f"The venue would not set leverage: {exc}",
                now,
            )
        if applied != approved_leverage:
            return await self._refuse(
                record,
                RiskRejectionCode.MAX_LEVERAGE,
                (
                    f"The venue applied {applied}x where the risk engine approved "
                    f"{approved_leverage}x. Refusing: an order placed at a leverage "
                    "risk never approved is not the order that was ruled on."
                ),
                now,
            )

        try:
            await self._adapter.set_margin_mode(symbol, MarginMode.ISOLATED)
            confirmed = await self._adapter.margin_mode(symbol)
        except (MarginModeRefusedError, ExchangeError) as exc:
            return await self._refuse(
                record,
                RiskRejectionCode.MODE_NOT_ENABLED,
                f"ISOLATED margin could not be established: {exc}",
                now,
            )
        if confirmed is not MarginMode.ISOLATED:
            return await self._refuse(
                record,
                RiskRejectionCode.MODE_NOT_ENABLED,
                (
                    f"The venue reports {confirmed.value} margin for {symbol} after the "
                    "change was requested. Refusing rather than trading under a margin "
                    "mode nobody asked for."
                ),
                now,
            )

        # 5. Size the order against the approved leverage and the venue's own
        #    step size, then commit the submission stamp BEFORE the call.
        sized = await self._size(symbol, request.margin, approved_leverage)
        if sized is None:
            return await self._refuse(
                record,
                RiskRejectionCode.STALE_DATA,
                "The order could not be sized against current market data.",
                now,
            )
        quantity, _price = sized

        record = await self._orders.repository.get(record.order_id) or record
        record = await self._orders.repository.update(
            record.model_copy(
                update={
                    "intent": record.intent.model_copy(update={"quantity": quantity}),
                    "venue_leverage": applied,
                    "venue_margin_mode": MarginMode.ISOLATED,
                    "updated_at": now,
                }
            )
        )
        await self._orders.mark_submitting(record.order_id, now=now)
        record = await self._orders.repository.get(record.order_id) or record

        # ------------------ the uncertainty window opens ------------------
        try:
            view = await self._adapter.submit(record)
        except ExchangeError as exc:
            # Never retried, and never assumed to have failed. A timeout before
            # and after the venue received this are indistinguishable from
            # here, so the honest state is UNKNOWN and the answer comes from
            # reconciliation, not from a guess.
            await self._orders.mark_unknown(
                record.order_id,
                reason=f"The submission did not return a usable answer: {exc}",
                now=utcnow(),
            )
            return TestnetOrderResult(
                accepted=False,
                record=await self._orders.repository.get(record.order_id),
                detail=(
                    "The order was sent and its outcome is unknown. It is recorded as "
                    "UNKNOWN and will be reconciled against the venue; no new entry is "
                    "permitted until it is settled."
                ),
            )

        acked = await self._orders.apply_venue_ack(
            record.order_id,
            venue_order_id=view.venue_order_id or "",
            state=view.state,
            now=utcnow(),
        )
        return TestnetOrderResult(
            accepted=True,
            record=acked,
            detail=f"Accepted by the venue in state {view.state.value}.",
            checks_performed=verdict.checks_performed,
        )

    # ------------------------------------------------------------------
    # Read-only venue state, for display
    # ------------------------------------------------------------------

    async def venue_snapshot(self, symbol: str | None = None) -> VenueSnapshot:
        """Everything a screen needs, read from the venue in one pass.

        Failures come back as a snapshot that says so rather than as an
        exception, because a status panel that cannot reach the venue must
        render "unavailable" and not a stack trace -- and must not render the
        last known numbers as though they were current.

        Nothing here is invented. A field the venue did not supply stays
        ``None``, and the UI is responsible for showing that as unavailable
        rather than as zero.
        """
        try:
            account = await self._adapter.account_identity()
        except ExchangeError as exc:
            return VenueSnapshot(
                connection=VenueConnection.UNAVAILABLE,
                venue=self._adapter.venue_name,
                detail=f"The venue could not be reached: {type(exc).__name__}.",
            )

        positions: tuple[VenuePosition, ...] = ()
        orders: tuple[VenueOrderView, ...] = ()
        partial: list[str] = []
        try:
            positions = await self._adapter.positions()
        except ExchangeError:
            partial.append("positions")
        try:
            orders = await self._adapter.open_orders(symbol=symbol)
        except ExchangeError:
            partial.append("open orders")

        margin: MarginMode | None = None
        if symbol:
            try:
                margin = await self._adapter.margin_mode(symbol)
            except ExchangeError:
                partial.append("margin mode")

        return VenueSnapshot(
            connection=VenueConnection.CONNECTED,
            venue=self._adapter.venue_name,
            account=account,
            positions=positions,
            open_orders=orders,
            margin_mode=margin,
            observed_at=utcnow(),
            detail=(
                None if not partial else f"Connected, but {', '.join(partial)} could not be read."
            ),
        )

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel(self, order_id: str) -> TestnetOrderResult:
        """Request cancellation. Losing the race to a fill is not an error.

        A cancel that arrives after the order filled is an ordinary outcome,
        not a failure: the venue's answer is authoritative and the state
        machine already treats CANCEL_REQUESTED -> FILLED as legal.
        """
        record = await self._orders.repository.get(order_id)
        if record is None:
            return TestnetOrderResult(accepted=False, record=None, detail=f"No order {order_id}.")
        if record.is_terminal:
            return TestnetOrderResult(
                accepted=False,
                record=record,
                detail=f"{order_id} is already {record.state.value}.",
            )

        now = utcnow()
        try:
            view = await self._adapter.cancel(
                symbol=record.intent.symbol, client_order_id=record.client_order_id
            )
        except ExchangeError as exc:
            # A cancellation whose outcome is unknown must not be recorded as a
            # cancellation. The order may still be live.
            await self._orders.mark_unknown(
                record.order_id,
                reason=f"The cancellation did not return a usable answer: {exc}",
                now=now,
            )
            return TestnetOrderResult(
                accepted=False,
                record=await self._orders.repository.get(order_id),
                detail=(
                    "The cancellation was sent and its outcome is unknown. The order is "
                    "recorded as UNKNOWN and will be reconciled."
                ),
            )

        resolved, _ = await self._orders.reconcile(
            record.order_id, view, venue_reachable=True, now=now
        )
        return TestnetOrderResult(
            accepted=resolved.state is OrderState.CANCELLED,
            record=resolved,
            detail=f"The venue reports {resolved.state.value}.",
        )

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def reconcile_all(self) -> ReconciliationSweep:
        """Ask the venue about every order whose fate is not settled.

        **Venue-reset protection.** The testnet is reset roughly monthly,
        without notice, and the reset takes every open and executed order with
        it. Afterwards the venue answers "no such order" for all of them --
        which, read one order at a time, looks exactly like evidence. It is
        not: it is one event, and resolving a batch of orders on the strength
        of it would fabricate a terminal state for every position the system
        was carrying.

        So a wholesale disappearance is detected as a *condition* and resolves
        nothing. The orders stay unreconciled, they keep blocking new entries,
        a discrepancy is recorded against each, and a human decides.
        """
        now = utcnow()
        scan = await self._orders.repository.scan_open()
        candidates = [r for r in scan.records if r.reached_venue and not r.is_terminal]

        answers: dict[str, VenueOrderView | None] = {}
        unreachable: list[str] = []
        for record in candidates:
            try:
                answers[record.order_id] = await self._adapter.query(
                    symbol=record.intent.symbol, client_order_id=record.client_order_id
                )
            except ExchangeError:
                unreachable.append(record.order_id)

        vanished = [oid for oid, view in answers.items() if view is None]
        reset_suspected = len(vanished) >= 2 and len(vanished) == len(answers)

        if reset_suspected:
            for order_id in vanished:
                await self._orders.repository.record_unreadable(
                    order_id,
                    detail=(
                        f"The venue reports no such order for all {len(vanished)} orders "
                        "queried in one sweep. That is a venue-reset signature, not "
                        "evidence about any single order, so nothing is resolved. These "
                        "orders remain unreconciled and continue to block new entries "
                        "until a human settles them."
                    ),
                    now=now,
                )
            return ReconciliationSweep(
                checked=len(candidates),
                resolved=0,
                unreachable=len(unreachable),
                venue_reset_suspected=True,
                detail=(
                    f"All {len(vanished)} queried orders vanished from the venue at once. "
                    "Treated as a venue reset and requiring human review; no order was "
                    "given a terminal state."
                ),
            )

        resolved = 0
        for record in candidates:
            if record.order_id in unreachable:
                await self._orders.reconcile(record.order_id, None, venue_reachable=False, now=now)
                continue
            view = answers.get(record.order_id)
            after, _ = await self._orders.reconcile(
                record.order_id,
                view,
                venue_reachable=True,
                now=now,
            )
            if after.is_terminal:
                resolved += 1
        return ReconciliationSweep(
            checked=len(candidates),
            resolved=resolved,
            unreachable=len(unreachable),
            venue_reset_suspected=False,
            detail=f"{len(candidates)} order(s) checked, {resolved} settled by the venue.",
        )

    async def _refuse(
        self, record: OrderRecord, code: RiskRejectionCode, detail: str, now: datetime
    ) -> TestnetOrderResult:
        await self._orders.mark_risk_rejected(record.order_id, code=code, detail=detail, now=now)
        return TestnetOrderResult(
            accepted=False,
            record=await self._orders.repository.get(record.order_id),
            detail=detail,
            rejection_code=code,
        )

    # ------------------------------------------------------------------
    # Risk inputs, read rather than assumed
    # ------------------------------------------------------------------

    async def _rule(
        self, request: TestnetOrderRequest, record: OrderRecord, now: datetime
    ) -> tuple[RiskVerdict | None, str]:
        """Build the risk view from real venue state and rule on the proposal.

        Every input is measured. Where one cannot be measured the order is
        refused rather than ruled on against a placeholder -- a risk view with
        an assumed number in it is a risk decision nobody made.
        """
        symbol = record.intent.symbol
        mark = await self._market_data.get_ticker(symbol)
        if mark.value is None or mark.value.last_price is None:
            return None, f"No usable mark price for {symbol}."

        price = mark.value.last_price
        notional = request.margin * request.requested_leverage

        volatility = await self._measure_volatility(symbol)
        try:
            account = await self._adapter.account_identity()
            bracket = await self._adapter.leverage_bracket(symbol, notional=notional)
            realized = await self._adapter.session_realized_pnl(since=self._session_start)
        except ExchangeError as exc:
            return None, f"Venue account state could not be read: {exc}"

        if account.available_balance is None:
            return None, "The venue did not report an available balance."

        pending = await self._orders.reconciliation_pending()
        risk = self._settings.risk
        view = RiskAccountView(
            balance=account.available_balance,
            available_balance=account.available_balance,
            equity=account.available_balance,
            margin_used=ZERO,
            open_symbols=frozenset(),
            open_position_count=0,
            total_notional=ZERO,
            session_realized_pnl=realized,
            daily_profit_target=risk.daily_profit_target,
            daily_loss_limit=risk.daily_loss_limit,
            lock_state=RiskLockState.NONE,
            mode_enabled=self._settings.is_mode_enabled(TradingMode.TESTNET),
            unreconciled_orders=pending.count,
            unreconciled_detail=pending.detail,
        )
        try:
            listed = await self._market_data.get_symbol(symbol)
        except AetherisError:
            return None, f"{symbol} is not a listed instrument at the venue."
        verdict = risk_evaluate(
            RiskProposal(
                symbol=symbol,
                side=request.side,
                requested_margin=request.margin,
                requested_leverage=request.requested_leverage,
                stop_loss_percent=request.stop_loss_percent,
                take_profit_percent=request.take_profit_percent,
                origin=TESTNET_ORIGIN,
            ),
            account=view,
            market=RiskMarketView(
                symbol=symbol,
                status=mark.status,
                age_seconds=mark.age_seconds,
                last_price=price,
                atr_percent=volatility.atr_percent,
                volatility_status=volatility.status,
                volatility_detail=volatility.detail,
                filters=listed.filters,
                exchange_max_leverage=bracket.max_leverage,
            ),
            policy=RiskPolicy(
                max_leverage=risk.max_leverage,
                max_open_positions=risk.max_open_positions,
                max_position_notional=risk.max_position_notional,
                max_portfolio_exposure=risk.max_portfolio_exposure,
                max_data_age_seconds=risk.max_data_age_seconds,
                entry_cooldown_seconds=float(risk.manual_entry_cooldown_seconds),
                # The same volatility ceiling the paper path applies. One
                # threshold, read from one place; two would drift.
                max_atr_percent=self._settings.autonomous.max_atr_percent,
            ),
            now=now,
        )
        return verdict, verdict.detail

    async def _measure_volatility(self, symbol: str) -> VolatilityMeasurement:
        """Real candles, measured. Never estimated and never reused.

        The same definition the paper path uses, from the same pure function --
        two callers computing ATR slightly differently is the problem ADR 0006
        exists to prevent. What differs here is only where the candles come
        from, and failures come back as a status so the refusal can name the
        actual condition.
        """
        try:
            observation = await self._market_data.get_klines(
                symbol, RISK_TIMEFRAME, limit=RISK_CANDLES
            )
        except AetherisError as exc:
            return VolatilityMeasurement(
                status=VolatilityStatus.CANDLES_UNAVAILABLE,
                detail=(
                    f"Candles for {symbol} could not be fetched ({exc.code.value}), so "
                    "volatility is unknown and is not estimated."
                ),
            )
        if observation.status is DataStatus.STALE:
            return VolatilityMeasurement(
                status=VolatilityStatus.CANDLES_STALE,
                detail=f"Candles for {symbol} are stale.",
            )
        raw = observation.value.candles if observation.value is not None else ()
        prepared = prepare_candles(raw, utcnow())
        if not prepared.ok:
            return VolatilityMeasurement(
                status=VolatilityStatus.CANDLES_UNAVAILABLE,
                detail=f"Candles for {symbol} are unusable: {prepared.detail}",
            )
        return measure_atr_percent(prepared.candles)

    async def _size(
        self, symbol: str, margin: Decimal, leverage: Decimal
    ) -> tuple[Decimal, Decimal] | None:
        """Quantity from notional, truncated to the venue's own step size."""
        mark = await self._market_data.get_ticker(symbol)
        if mark.value is None or mark.value.last_price is None:
            return None
        price = mark.value.last_price
        try:
            listed = await self._market_data.get_symbol(symbol)
        except AetherisError:
            return None
        if price <= ZERO:
            return None
        quantity = floor_to_step(margin * leverage / price, listed.filters.step_size)
        if quantity <= ZERO:
            return None
        return quantity, price
