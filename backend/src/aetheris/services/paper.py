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
from decimal import Decimal

from aetheris.adapters.exchange.errors import SymbolNotFoundError
from aetheris.analysis.leverage import resolve_leverage
from aetheris.core.config import Settings
from aetheris.core.errors import AetherisError
from aetheris.core.freshness import DataStatus, Observation, utcnow
from aetheris.domain.enums import TradingMode
from aetheris.domain.leverage import (
    LEVERAGE_MAX,
    LEVERAGE_MIN,
    LeverageDecision,
    LeverageRequest,
)
from aetheris.domain.market import Symbol, Ticker
from aetheris.domain.paper import (
    PaperAccount,
    PaperExitReason,
    PaperOrderResult,
    PaperTrade,
    ReconciliationReport,
)
from aetheris.engines.paper.engine import (
    MarkPrice,
    PaperEngine,
    SubmitOrderRequest,
)
from aetheris.services.market_data import MarketDataService

__all__ = ["PaperTradingService"]


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

    @property
    def engine(self) -> PaperEngine:
        return self._engine

    @property
    def paper_enabled(self) -> bool:
        return self._settings.is_mode_enabled(TradingMode.PAPER)

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

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
            return self._engine.tick(now=utcnow(), marks=await self._open_marks())

    async def submit_order(
        self,
        request: SubmitOrderRequest,
        *,
        requested_leverage: Decimal,
        risk_verdict_detail: str | None = None,
    ) -> PaperOrderResult:
        """Resolve the leverage chain, then submit to the engine's risk gate."""
        async with self._write_lock:
            return await self._submit_locked(
                request,
                requested_leverage=requested_leverage,
                risk_verdict_detail=risk_verdict_detail,
            )

    async def _submit_locked(
        self,
        request: SubmitOrderRequest,
        *,
        requested_leverage: Decimal,
        risk_verdict_detail: str | None = None,
    ) -> PaperOrderResult:
        """The body of ``submit_order``, run under the write lock.

        Split out so the lock is acquired once at the boundary rather than
        being threaded through every early return.
        """
        symbol = request.symbol.upper()
        now = utcnow()
        marks = await self._open_marks()

        # Metadata before price: an unlisted symbol has no ticker to fetch, and
        # letting that surface as a bare 404 would give the order path a second
        # failure shape for callers to handle.
        listed, problem = await self._metadata(symbol)
        if listed is None:
            # Modelled as an unusable-price refusal so the caller gets the same
            # shape as every other rejection rather than a special case.
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
                leverage=self._resolve_leverage(requested_leverage, exchange_max=None),
                paper_enabled=self.paper_enabled,
                marks=marks,
            )

        # Read from venue metadata rather than assumed. None today, and
        # honestly so: leverage brackets are served only from an authenticated
        # endpoint and this build holds no credentials. The moment a venue does
        # publish one, the chain starts using it with no change here.
        exchange_max = Decimal(listed.max_leverage) if listed.max_leverage is not None else None
        decision = self._resolve_leverage(requested_leverage, exchange_max=exchange_max)
        mark = marks.get(symbol) or await self._mark(symbol)
        return self._engine.submit_order(
            request,
            now=now,
            mark=mark,
            filters=listed.filters,
            leverage=decision,
            paper_enabled=self.paper_enabled,
            marks=marks,
            risk_verdict_detail=risk_verdict_detail,
        )

    async def close_position(
        self, symbol: str, *, reason: PaperExitReason = PaperExitReason.MANUAL_CLOSE
    ) -> PaperOrderResult:
        async with self._write_lock:
            marks = await self._open_marks()
            key = symbol.upper()
            mark = marks.get(key) or await self._mark(key)
            return self._engine.close_position(
                key, now=utcnow(), mark=mark, reason=reason, marks=marks
            )

    async def reset(self, *, starting_balance: Decimal | None = None) -> PaperAccount:
        """Discard the account.

        Async, and holding the write lock, although the engine call itself is
        synchronous: resetting underneath an in-flight submission would let a
        fill land in an account that no longer exists.
        """
        async with self._write_lock:
            return self._engine.reset(now=utcnow(), starting_balance=starting_balance)

    async def set_emergency_stop(self, *, engaged: bool, reason: str) -> PaperAccount:
        """Block or unblock new entries.

        Takes the write lock so the halt cannot be observed half-applied by a
        submission that is already past its own check.
        """
        async with self._write_lock:
            return self._engine.set_emergency_stop(engaged=engaged, reason=reason, now=utcnow())

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_leverage(requested: Decimal, *, exchange_max: Decimal | None) -> LeverageDecision:
        """Run the Phase 4 chain, unchanged, for a paper order.

        Paper does not get a softer chain than live would. The risk engine is
        still absent (phase 7) and the venue ceiling is still unknown, so
        anything above the domain minimum fails closed here exactly as it does
        on the analysis endpoint -- which is the point: the refusal is
        exercised against real requests before it ever guards real money.
        """
        clamped = max(LEVERAGE_MIN, min(requested, LEVERAGE_MAX))
        candidate = LeverageRequest(
            requested_leverage=clamped,
            basis=(
                "Caller-supplied leverage request for a paper position. A request, "
                "never an authorisation: the constraint chain rules on it, and the "
                "risk engine has final authority."
            ),
            inputs={"requested_leverage": clamped},
        )
        return resolve_leverage(
            candidate,
            exchange_max_leverage=exchange_max,
            risk_max_leverage=None,
            risk_engine_available=False,
        )
