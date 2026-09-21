"""Paper trading orchestration.

The only place the paper engine meets the network. It fetches prices, unwraps
them from their provenance envelope, asks the venue for the symbol's real
filters, runs the leverage chain, and hands all of it to the engine. Every
decision that follows is the engine's.

**It still places no order.** The exchange dependency is the read-only
``MarketDataPort`` -- the same one the terminal and the backtester use. There
is no trading port implementation to inject, in this service or anywhere else.

**Serialising writes (phase 7).** Until the autonomous loop existed there was
exactly one writer: the HTTP request. The loop is a second, and every mutating
path here does a read-modify-write *across an await* -- it fetches prices, then
mutates state. Two operations can therefore interleave at the await point, and
the second acts on a view taken before the first mutated.

``PaperEngine``'s methods are synchronous, so state cannot be corrupted: an
engine call is atomic within the event loop. The hazard is subtler. Two
concurrent submissions for the same symbol can *both* pass the
"is this symbol already open?" check before either opens anything, and the
account ends up with a position it refused to allow.

``_write_lock`` closes that window by covering the whole fetch-then-mutate
sequence rather than just the mutation. Reads do not take it: a snapshot one
moment out of date is not a correctness problem, and blocking reads behind
writes would make the terminal stutter whenever the loop is working.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Final

from aetheris.adapters.exchange.errors import SymbolNotFoundError
from aetheris.adapters.persistence.paper import PostgresPaperSnapshotStore
from aetheris.analysis.indicators.prepare import prepare_candles
from aetheris.analysis.volatility import (
    VolatilityMeasurement,
    VolatilityStatus,
    measure_atr_percent,
)
from aetheris.core.config import Settings
from aetheris.core.errors import AetherisError
from aetheris.core.freshness import DataStatus, Observation, utcnow
from aetheris.core.money import ZERO, quantize_usdt
from aetheris.domain.enums import Timeframe, TradingMode
from aetheris.domain.leverage import (
    LeverageDecision,
    LeverageOutcome,
    LeverageReason,
)
from aetheris.domain.market import Symbol, Ticker
from aetheris.domain.paper import (
    Durability,
    PaperAccount,
    PaperExitReason,
    PaperOrderResult,
    PaperTrade,
    ReconciliationReport,
)
from aetheris.engines.order.engine import OrderLifecycleEngine, ReconciliationPending
from aetheris.engines.paper.engine import (
    MarkPrice,
    PaperEngine,
    SubmitOrderRequest,
)
from aetheris.engines.risk.engine import evaluate as risk_evaluate
from aetheris.engines.risk.policy import (
    RiskAccountView,
    RiskMarketView,
    RiskPolicy,
    RiskProposal,
)
from aetheris.services.market_data import MarketDataService

__all__ = ["MANUAL_RISK_CANDLES", "PaperTradingService"]

#: Candles fetched to measure volatility for one order. ATR(14) needs 15;
#: 60 leaves room for bars dropped as still forming without spending venue
#: quota on history nothing reads (ADR 0006 §L).
MANUAL_RISK_CANDLES: Final = 60
MANUAL_RISK_TIMEFRAME: Final = Timeframe.M15


def mark_from_observation(symbol: str, observation: Observation[Ticker]) -> MarkPrice:
    """Unwrap a ticker into the engine's price input, provenance intact.

    The envelope's rule -- a value exists only when the status is OK -- is what
    makes this safe: there is no branch where a price survives a non-OK status,
    so the engine cannot be handed a number the venue did not send.
    """
    ticker = observation.value
    return MarkPrice(
        symbol=symbol,
        status=observation.status,
        source=observation.source,
        last_price=ticker.last_price if ticker is not None else None,
        bid_price=ticker.bid_price if ticker is not None else None,
        ask_price=ticker.ask_price if ticker is not None else None,
        age_seconds=observation.age_seconds,
        detail=observation.detail,
    )


class PaperTradingService:
    """Wires market data and the paper engine together."""

    def __init__(
        self,
        market_data: MarketDataService,
        engine: PaperEngine,
        settings: Settings,
    ) -> None:
        self._market_data = market_data
        self._engine = engine
        self._settings = settings
        #: Covers each fetch-then-mutate sequence. See the module docstring for
        #: the interleaving this prevents.
        self._write_lock = asyncio.Lock()
        #: Per-symbol entry times, for the cooldown. In memory like every
        #: other piece of paper state; a restart forgets it, which is the
        #: permissive direction and is disclosed rather than hidden.
        self._last_entry_at: dict[str, datetime] = {}
        #: The durable order store, when one is configured. Bound after
        #: construction because the pool is opened during startup, while this
        #: service is built before it. ``None`` means no durable order records
        #: exist at all -- not that they exist and were not consulted.
        self._orders: OrderLifecycleEngine | None = None
        #: The durable paper snapshot store, when one is configured. Bound
        #: after construction for the same reason the order engine is.
        #: ``None`` means paper state is in memory only, which the account
        #: response reports rather than leaving the reader to assume.
        self._snapshots: PostgresPaperSnapshotStore | None = None

    def bind_order_engine(self, engine: OrderLifecycleEngine | None) -> None:
        """Attach the durable order store once it is open."""
        self._orders = engine

    def bind_snapshot_store(self, store: PostgresPaperSnapshotStore | None) -> None:
        """Attach the durable paper store once the pool is open.

        Also points the engine's repository at the store for its durability
        answer. Without that the account response would keep reporting
        IN_MEMORY while state was in fact being written down -- understating
        the guarantee, which is the safe direction but still wrong.
        """
        self._snapshots = store
        probe = getattr(self._engine.repository, "set_durability_probe", None)
        if probe is not None:
            probe(None if store is None else (lambda: store.durability))

    @property
    def durability(self) -> Durability:
        """What the account response should claim about surviving a restart.

        Delegates to the store rather than to configuration, because a
        store whose last write failed is not durable whatever the config
        says, and claiming otherwise is the one lie this must not tell.
        """
        if self._snapshots is None:
            return Durability.IN_MEMORY
        return self._snapshots.durability

    async def _persist(self) -> None:
        """Write the engine's state back, if there is somewhere to write it.

        Always called with the write lock held, which is what makes it
        safe: the lock already covers fetch-then-mutate on every path, so
        a snapshot taken here cannot catch a half-applied mutation.

        Never raises. The mutation has already happened and is correct in
        memory; failing the caller's order because a write failed would
        report a loss that did not occur. The store degrades to IN_MEMORY
        and the account response stops promising durability.
        """
        if self._snapshots is None:
            return
        await self._snapshots.save(self._engine.state())

    async def restore(self) -> bool:
        """Seed the engine from the stored snapshot. Returns whether it did.

        Called once at startup, before the service serves anything. A
        false return is the normal first-run answer and is not a failure.
        """
        if self._snapshots is None:
            return False
        async with self._write_lock:
            stored = await self._snapshots.load()
            if stored is None:
                return False
            self._engine.restore(stored)
            return True

    @property
    def engine(self) -> PaperEngine:
        return self._engine

    @property
    def paper_enabled(self) -> bool:
        return self._settings.is_mode_enabled(TradingMode.PAPER)

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    async def _reconciliation_pending(self) -> ReconciliationPending:
        """How many durable orders forbid a new entry.

        Three outcomes, and the third is the one that matters:

        - No durable store configured: no order records exist anywhere, so
          nothing can be unreconciled. Zero is a fact here, not an assumption.
        - The store answers: use its count.
        - The store is configured and cannot be read: **fail closed.** A store
          that exists and will not answer may be holding an order that reached
          a venue, and treating an unanswerable question as "nothing pending"
          is precisely the silent failure phase 8a exists to prevent.
        """
        if self._orders is None:
            return ReconciliationPending(0, "No durable order store is configured.")
        try:
            return await self._orders.reconciliation_pending()
        except Exception:  # any failure here must block, not pass
            return ReconciliationPending(
                1,
                "The durable order store could not be read, so whether an order is "
                "awaiting reconciliation is unknown. No new entry is permitted until "
                "it can be checked.",
            )

    async def _mark(self, symbol: str) -> MarkPrice:
        """Fetch one price, turning an upstream failure into an unusable mark.

        The adapter raises when the venue is unreachable or answers with an
        error, which is right for a read endpoint -- a ticker request should
        surface a 503. On the order path it is not: the caller asked to open a
        position, and the useful answer is a refusal carrying the reason, with
        the attempt recorded in the account history, rather than an error page
        that leaves no trace of what was tried.

        Nothing is hidden by this. The venue's own message travels into the
        refusal detail, and the order is still refused.
        """
        try:
            observation = await self._market_data.get_ticker(symbol)
        except SymbolNotFoundError:
            raise
        except AetherisError as exc:
            return MarkPrice(
                symbol=symbol.upper(),
                status=DataStatus.ERROR,
                source=f"{self._market_data.exchange_name}:rest",
                detail=f"{exc.code.value}: {exc}",
            )
        return mark_from_observation(symbol.upper(), observation)

    async def _open_marks(self) -> dict[str, MarkPrice]:
        """One ticker per open position, and none for anything else.

        Bounded by the position ceiling rather than by the size of the
        universe, so a refresh costs a handful of calls at most.
        """
        marks: dict[str, MarkPrice] = {}
        for symbol in self._engine.open_symbols():
            marks[symbol] = await self._mark(symbol)
        return marks

    async def _metadata(self, symbol: str) -> tuple[Symbol | None, str | None]:
        """The venue's published record for this symbol, or why there is none.

        One lookup serves both the order filters and the leverage ceiling, so a
        submission costs a single metadata read. Paper trading an instrument
        the venue does not list would mean simulating against constraints that
        do not exist, so an unlisted symbol is a refusal.
        """
        try:
            return await self._market_data.get_symbol(symbol), None
        except SymbolNotFoundError as exc:
            return None, str(exc)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_account(self) -> PaperAccount:
        """Mark the account to live prices. Changes nothing."""
        return self._engine.snapshot(now=utcnow(), marks=await self._open_marks())

    def reconcile(self) -> ReconciliationReport:
        return self._engine.reconcile(now=utcnow())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def tick(self) -> tuple[PaperAccount, tuple[PaperTrade, ...]]:
        """Run one position-management pass against fresh prices."""
        async with self._write_lock:
            outcome = self._engine.tick(now=utcnow(), marks=await self._open_marks())
            await self._persist()
            return outcome

    async def submit_order(
        self,
        request: SubmitOrderRequest,
        *,
        requested_leverage: Decimal,
        origin: str = "manual",
    ) -> PaperOrderResult:
        """Rule on the order, then submit what was approved.

        **The single risk authority for every paper order** (ADR 0006). Manual
        and autonomous callers arrive here, and both are ruled on by
        ``engines.risk.evaluate`` before anything reaches the engine. There is
        no second leverage resolution and no path around this.
        """
        async with self._write_lock:
            result = await self._submit_locked(
                request, requested_leverage=requested_leverage, origin=origin
            )
            await self._persist()
            return result

    async def _submit_locked(
        self,
        request: SubmitOrderRequest,
        *,
        requested_leverage: Decimal,
        origin: str,
    ) -> PaperOrderResult:
        """The body of ``submit_order``, run under the write lock."""
        symbol = request.symbol.upper()
        now = utcnow()
        marks = await self._open_marks()

        # Idempotency before risk. A retry after a dropped response must return
        # what happened the first time; re-ruling it would hand the caller a
        # different answer to the one already applied -- the first attempt
        # opened a position, the second reports a cooldown refusal for it.
        if request.client_order_id is not None:
            replayed = self._engine.replay(request.client_order_id, now=now, marks=marks)
            if replayed is not None:
                return replayed

        # Metadata before price: an unlisted symbol has no ticker to fetch, and
        # letting that surface as a bare 404 would give the order path a second
        # failure shape for callers to handle.
        listed, problem = await self._metadata(symbol)
        if listed is None:
            return self._engine.submit_order(
                request,
                now=now,
                mark=MarkPrice(
                    symbol=symbol,
                    status=DataStatus.UNAVAILABLE,
                    source=f"{self._market_data.exchange_name}:rest",
                    detail=(
                        f"{problem}. A paper order needs the venue's published filters "
                        "for the symbol, so an unlisted instrument cannot be simulated."
                    ),
                ),
                filters=None,
                leverage=_unresolvable_leverage(
                    "The symbol is not listed, so no venue ceiling exists to rule against."
                ),
                paper_enabled=self.paper_enabled,
                marks=marks,
            )

        mark = marks.get(symbol) or await self._mark(symbol)
        volatility = await self._measure_volatility(symbol, now)
        account = self._engine.snapshot(now=now, marks=marks)
        pending = await self._reconciliation_pending()

        # THE RISK AUTHORITY. Every order, whatever proposed it.
        verdict = risk_evaluate(
            RiskProposal(
                symbol=symbol,
                side=request.side,
                requested_margin=self._proposed_margin(request, mark),
                requested_leverage=requested_leverage,
                stop_loss_percent=request.stop_loss_percent,
                take_profit_percent=request.take_profit_percent,
                trailing_stop_percent=request.trailing_stop_percent,
                origin=origin,
            ),
            account=self._account_view(account, symbol, pending),
            market=RiskMarketView(
                symbol=symbol,
                status=mark.status,
                age_seconds=mark.age_seconds,
                last_price=mark.last_price,
                atr_percent=volatility.atr_percent,
                volatility_status=volatility.status,
                volatility_detail=volatility.detail,
                filters=listed.filters,
                # Read from venue metadata, never assumed. None today: leverage
                # brackets are served only from an authenticated endpoint.
                exchange_max_leverage=(
                    Decimal(listed.max_leverage) if listed.max_leverage is not None else None
                ),
            ),
            policy=self._policy(origin),
            now=now,
        )

        if not verdict.approved:
            if verdict.code is None:  # pragma: no cover - RiskVerdict forbids it
                raise ValueError("a refusing verdict must carry a rejection code")
            return self._engine.refuse(
                request,
                now=now,
                code=verdict.code,
                detail=verdict.detail,
                leverage=verdict.leverage,
                marks=marks,
                checks_performed=verdict.checks_performed,
                risk_max_leverage=verdict.risk_max_leverage,
            )

        # Submit exactly what was approved -- the approved margin and the
        # approved leverage, not what was asked for. The engine's own gate then
        # checks again, independently.
        outcome = self._engine.submit_order(
            replace(request, margin=verdict.approved_margin, quantity=None),
            now=now,
            mark=mark,
            filters=listed.filters,
            leverage=verdict.leverage,
            paper_enabled=self.paper_enabled,
            marks=marks,
            risk_verdict_detail=verdict.detail,
            checks_performed=verdict.checks_performed,
            risk_max_leverage=verdict.risk_max_leverage,
        )
        if outcome.accepted:
            self._last_entry_at[symbol] = now
        return outcome

    def _proposed_margin(self, request: SubmitOrderRequest, mark: MarkPrice) -> Decimal:
        """What the caller is asking to commit, expressed as margin.

        The risk engine rules on margin, so a quantity-denominated request is
        converted here. An unusable price leaves it at zero, which the engine
        refuses for data quality before the size is ever considered.
        """
        if request.margin is not None:
            return request.margin
        if request.quantity is not None and mark.last_price is not None:
            return quantize_usdt(request.quantity * mark.last_price)
        return ZERO

    async def _measure_volatility(self, symbol: str, now: datetime) -> VolatilityMeasurement:
        """Fetch real candles and measure ATR. Never estimated, never reused.

        ~60 bars, not the 300 the autonomous loop takes for its strategy:
        ATR(14) needs 15, and fetching five times that for one number is venue
        quota spent on nothing. Failures come back as a status rather than an
        exception so the refusal can name the actual condition.
        """
        try:
            observation = await self._market_data.get_klines(
                symbol, MANUAL_RISK_TIMEFRAME, limit=MANUAL_RISK_CANDLES
            )
        except AetherisError as exc:
            return VolatilityMeasurement(
                status=VolatilityStatus.CANDLES_UNAVAILABLE,
                detail=(
                    f"Candles for {symbol} could not be fetched ({exc.code.value}), so "
                    "volatility is unknown. The price may be fresh; the history is not "
                    "available, and it is not estimated."
                ),
            )
        if observation.status is DataStatus.STALE:
            return VolatilityMeasurement(
                status=VolatilityStatus.CANDLES_STALE,
                detail=(
                    f"Candles for {symbol} are stale, so the volatility they describe is "
                    "not the present one."
                ),
            )
        raw = observation.value.candles if observation.value is not None else ()
        prepared = prepare_candles(raw, now)
        if not prepared.ok:
            return VolatilityMeasurement(
                status=VolatilityStatus.CANDLES_UNAVAILABLE,
                detail=f"Candles for {symbol} are unusable: {prepared.detail}",
            )
        return measure_atr_percent(prepared.candles)

    def _policy(self, origin: str) -> RiskPolicy:
        """The envelope, with the cooldown that belongs to this caller.

        A human is not a loop on 15-minute bars (ADR 0006 §N.3), so the two
        cadences are configured separately rather than sharing one number that
        cannot suit both.
        """
        risk = self._settings.risk
        cooldown = (
            risk.entry_cooldown_seconds
            if origin == "autonomous-loop"
            else risk.manual_entry_cooldown_seconds
        )
        return RiskPolicy(
            max_open_positions=risk.max_open_positions,
            max_position_notional=risk.max_position_notional,
            max_portfolio_exposure=risk.max_portfolio_exposure,
            max_leverage=risk.max_leverage,
            max_data_age_seconds=risk.max_data_age_seconds,
            entry_cooldown_seconds=float(cooldown),
            max_atr_percent=self._settings.autonomous.max_atr_percent,
        )

    def _account_view(
        self, account: PaperAccount, symbol: str, pending: ReconciliationPending
    ) -> RiskAccountView:
        return RiskAccountView(
            balance=account.balance,
            available_balance=account.available_balance,
            equity=account.equity,
            margin_used=account.margin_used,
            open_symbols=frozenset(p.symbol for p in account.positions),
            open_position_count=len(account.positions),
            total_notional=sum((p.notional for p in account.positions), ZERO),
            session_realized_pnl=account.session.realized_pnl,
            daily_profit_target=account.session.profit_target,
            daily_loss_limit=account.session.loss_limit,
            lock_state=account.session.lock_state,
            lock_reason=account.session.lock_reason,
            emergency_stopped=self._engine.emergency_stopped,
            emergency_reason=self._engine.emergency_reason,
            mode_enabled=self.paper_enabled,
            # Read from the durable order store rather than assumed. It was
            # hardcoded to zero while no such store existed, which left the
            # risk engine's RECONCILIATION_PENDING check unreachable -- a
            # safety rule that could never fire.
            unreconciled_orders=pending.count,
            unreconciled_detail=pending.detail,
            last_entry_at=self._last_entry_at.get(symbol),
        )

    async def close_position(
        self, symbol: str, *, reason: PaperExitReason = PaperExitReason.MANUAL_CLOSE
    ) -> PaperOrderResult:
        async with self._write_lock:
            marks = await self._open_marks()
            key = symbol.upper()
            mark = marks.get(key) or await self._mark(key)
            result = self._engine.close_position(
                key, now=utcnow(), mark=mark, reason=reason, marks=marks
            )
            await self._persist()
            return result

    async def reset(self, *, starting_balance: Decimal | None = None) -> PaperAccount:
        """Discard the account.

        Async, and holding the write lock, although the engine call itself is
        synchronous: resetting underneath an in-flight submission would let a
        fill land in an account that no longer exists.
        """
        async with self._write_lock:
            account = self._engine.reset(now=utcnow(), starting_balance=starting_balance)
            await self._persist()
            return account

    async def set_emergency_stop(self, *, engaged: bool, reason: str) -> PaperAccount:
        """Block or unblock new entries.

        Takes the write lock so the halt cannot be observed half-applied by a
        submission that is already past its own check.
        """
        async with self._write_lock:
            account = self._engine.set_emergency_stop(engaged=engaged, reason=reason, now=utcnow())
            await self._persist()
            return account


def _unresolvable_leverage(detail: str) -> LeverageDecision:
    """A chain that could not be reached. Reported rather than omitted.

    Used only where the symbol itself is unusable, so there was never a ceiling
    to rule against. A reader sees that the chain was not reached instead of
    wondering whether it silently passed.
    """
    return LeverageDecision(
        outcome=LeverageOutcome.REJECTED,
        reason=LeverageReason.INSUFFICIENT_DATA,
        detail=detail,
    )
