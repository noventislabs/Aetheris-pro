"""The autonomous paper-trading loop.

A server-side task that proposes orders from strategy output without a human,
and is refused by the risk engine exactly as a human would be. It is the first
thing in this system that acts on its own, so most of what follows is about
making that safe and visible rather than about making it clever.

**It is off.** Three independent conditions arm it: configuration must permit
it, paper mode must be enabled, and somebody must call the arm endpoint. Config
alone never starts it, and the arm does not survive a restart -- a process that
crashed and came back trading unattended, against an account it does not
remember, is the worst outcome available here.

**It proposes; it never decides.** Every entry goes through
``engines.risk.engine.evaluate`` and then, independently, through the phase 6
paper gate. Two gates is not redundancy to be optimised away: it is the same
principle that makes ``resolve_leverage`` re-validate input it was already
handed.

**It records everything.** One decision per symbol per iteration, including the
ones where nothing happened, because "the loop did nothing for six hours" has
to be distinguishable from "the loop was not running" and from "the loop died
quietly".

**It reaches no venue.** The only exchange dependency is the read-only
``MarketDataPort``. There is no ``TradingPort`` implementation to inject, here
or anywhere else in the package.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from aetheris.adapters.exchange.errors import SymbolNotFoundError
from aetheris.analysis.indicators.library import atr as atr_indicator
from aetheris.analysis.indicators.prepare import prepare_candles
from aetheris.analysis.strategies import trend_momentum
from aetheris.analysis.strategies.registry import evaluate_from_candles
from aetheris.core.config import Settings
from aetheris.core.errors import AetherisError, RiskRejectionCode
from aetheris.core.freshness import utcnow
from aetheris.core.logging import get_logger
from aetheris.core.money import ZERO, quantize_usdt
from aetheris.domain.autonomous import (
    AutonomousAction,
    AutonomousDecision,
    AutonomousLoopState,
    AutonomousStatus,
)
from aetheris.domain.enums import OrderSide, PositionSide, Timeframe, TradingMode
from aetheris.domain.leverage import LeverageDecision
from aetheris.domain.market import Candle, Symbol
from aetheris.domain.paper import PaperAccount, PaperExitReason
from aetheris.domain.strategy import StrategyBias, StrategyResult, StrategyStatus
from aetheris.engines.paper.engine import SubmitOrderRequest
from aetheris.services.market_data import MarketDataService
from aetheris.services.paper import PaperTradingService

__all__ = ["AUTONOMOUS_ORIGIN", "AutonomousLoop", "build_client_order_id"]

#: Identifies the caller to the risk authority, which uses it to pick the
#: cooldown that belongs to this cadence. It confers no privilege: the
#: engine rules identically whichever origin proposed the order.
AUTONOMOUS_ORIGIN = "autonomous-loop"

_log = get_logger("autonomous")

_ATR_PERIOD = 14
_HUNDRED = Decimal(100)


def build_client_order_id(
    symbol: str, timeframe: Timeframe, bar_close: datetime, side: OrderSide
) -> str:
    """The deterministic idempotency key.

    ``auto-{symbol}-{timeframe}-{bar_close_ms}-{side}``.

    Duplicate protection falls out of the key rather than needing a flag. A
    retry after a transient failure recomputes the same id and replays the
    original outcome; and because the bar's close time comes from the candle
    series rather than the clock, re-evaluating the same bar -- which happens
    every iteration within a 15-minute candle -- cannot open a second position.

    At most one autonomous entry per symbol per closed bar is therefore
    structural, not a rule someone has to remember to enforce.
    """
    epoch_ms = int(bar_close.timestamp() * 1000)
    return f"auto-{symbol.upper()}-{timeframe.value}-{epoch_ms}-{side.value}"


def _atr_percent(candles: Sequence[Candle]) -> Decimal | None:
    """Measured ATR as a percent of the last close, or ``None``.

    ``None`` when it could not be computed, which the risk engine turns into a
    refusal. It never becomes a zero: a zero would assert the instrument is not
    moving, which is a claim no measurement made.
    """
    if len(candles) < _ATR_PERIOD + 1:
        return None
    series = atr_indicator(candles, period=_ATR_PERIOD)["atr"]
    value = next((v for v in reversed(series) if v is not None), None)
    last_close = candles[-1].close
    if value is None or last_close <= ZERO:
        return None
    return (value / last_close * _HUNDRED).quantize(Decimal("0.0001"))


class AutonomousLoop:
    """Owns the task, the clock and the decision log."""

    def __init__(
        self,
        market_data: MarketDataService,
        paper: PaperTradingService,
        settings: Settings,
    ) -> None:
        self._market_data = market_data
        self._paper = paper
        self._settings = settings
        self._config = settings.autonomous

        self._armed = False
        self._failure_detail: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

        self._decisions: list[AutonomousDecision] = []
        self._sequence = 0
        self._iterations = 0
        self._entries = 0
        self._refusals = 0
        self._closes = 0
        self._last_iteration_at: datetime | None = None
        self._last_duration: float | None = None

        self._consecutive_failures: dict[str, int] = {}
        self._excluded: dict[str, str] = {}
        self._last_entry_at: dict[str, datetime] = {}
        self._total_failure_iterations = 0

    # ------------------------------------------------------------------
    # Arming
    # ------------------------------------------------------------------

    @property
    def permitted_by_config(self) -> bool:
        return self._settings.autonomous_trading_enabled

    @property
    def armed(self) -> bool:
        return self._armed

    def _state(self) -> AutonomousLoopState:
        if self._failure_detail is not None:
            return AutonomousLoopState.FAILED
        if not self.permitted_by_config:
            return AutonomousLoopState.DISABLED_BY_CONFIG
        return AutonomousLoopState.ARMED if self._armed else AutonomousLoopState.DISARMED

    def status(self) -> AutonomousStatus:
        """What the loop is doing, reported honestly including when it is off."""
        return AutonomousStatus(
            state=self._state(),
            enabled=self._armed,
            permitted_by_config=self.permitted_by_config,
            paper_mode_enabled=self._settings.is_mode_enabled(TradingMode.PAPER),
            symbols=tuple(self._config.symbols),
            excluded_symbols=dict(self._excluded),
            timeframe=self._config.timeframe,
            interval_seconds=self._config.interval_seconds,
            iterations=self._iterations,
            decisions_recorded=self._sequence,
            entries=self._entries,
            refusals=self._refusals,
            closes=self._closes,
            last_iteration_at=self._last_iteration_at,
            last_iteration_duration_seconds=self._last_duration,
            failure_detail=self._failure_detail,
            venue_outage=self._total_failure_iterations >= self._config.outage_iterations,
        )

    async def arm(self, *, enabled: bool) -> AutonomousStatus:
        """Arm or disarm. The only way the loop ever starts.

        Refuses to arm when configuration does not permit it or paper mode is
        off. Arming clears a previous failure, because re-arming is a human
        saying they have looked at it.
        """
        if enabled:
            if not self.permitted_by_config:
                raise AetherisError(
                    "Autonomous trading is not permitted by this deployment's "
                    "configuration. Set AETHERIS_AUTONOMOUS_TRADING_ENABLED=true and "
                    "restart; the flag gates arming and never arms on its own."
                )
            if not self._settings.is_mode_enabled(TradingMode.PAPER):
                raise AetherisError("Paper trading is disabled, so there is nothing to automate.")
            self._failure_detail = None

        self._armed = enabled
        _log.info("autonomous_armed" if enabled else "autonomous_disarmed", armed=enabled)
        return self.status()

    # ------------------------------------------------------------------
    # The task
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Create the background task. Does **not** arm it."""
        if self._task is not None or not self.permitted_by_config:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._supervise(), name="aetheris-autonomous-loop")

    async def aclose(self) -> None:
        """Stop the task and wait for it, so shutdown is not racy."""
        self._armed = False
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        # Shutdown must not raise, whatever the task was doing. The supervisor
        # has already recorded any real failure in ``failure_detail``, so there
        # is nothing left here worth propagating into a shutdown path.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _supervise(self) -> None:
        """Run iterations until stopped, and fail loudly rather than silently.

        A loop that restarts itself after an unexplained crash hides the crash.
        This records the exception, disarms, and stops -- re-arming is a human
        deciding they have understood it.
        """
        try:
            while not self._stop.is_set():
                if self._armed:
                    await self.run_once()
                await self._sleep_until_next()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure_detail = f"{type(exc).__name__}: {exc}"
            self._armed = False
            _log.error("autonomous_loop_failed", failure=self._failure_detail)

    async def _sleep_until_next(self) -> None:
        interval = self._config.interval_seconds
        if self._total_failure_iterations >= self._config.outage_iterations:
            # Back off while the venue is unreachable. Not a stop: open
            # positions still need attention when it comes back.
            interval *= self._config.outage_backoff_multiplier
        # A timeout is the normal path: the interval elapsed without anybody
        # asking the loop to stop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=interval)

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------

    async def run_once(self) -> tuple[AutonomousDecision, ...]:
        """Manage open positions, then evaluate each symbol for entry.

        Management first, deliberately: a position that should have closed must
        not still be occupying a slot when the entry check counts open ones.
        """
        started = utcnow()
        produced: list[AutonomousDecision] = []
        universe = self._universe()

        if not universe:
            produced.append(
                self._record(
                    symbol="-",
                    action=AutonomousAction.SKIPPED,
                    detail=(
                        "No symbols are configured, so the loop evaluated nothing. Set "
                        "AETHERIS_AUTO_SYMBOLS to choose what it watches; it does not "
                        "pick instruments on its own."
                    ),
                    now=started,
                )
            )
            self._finish_iteration(started, failures=0, evaluated=0)
            return tuple(produced)

        # 1. Management. Stops, targets, trailing and liquidation all live in
        #    the phase 6 engine; this only calls it.
        account, closed = await self._paper.tick()
        for trade in closed:
            self._closes += 1
            produced.append(
                self._record(
                    symbol=trade.symbol,
                    action=AutonomousAction.CLOSED,
                    detail=(
                        f"{trade.exit_reason.value} at {trade.exit_price} for a net "
                        f"{trade.net_pnl} USDT."
                    ),
                    now=started,
                    exit_reason=trade.exit_reason,
                    position_side=trade.side,
                    trade_id=trade.trade_id,
                    realized_pnl=trade.net_pnl,
                )
            )

        # 2. Per symbol.
        failures = 0
        for symbol in universe:
            try:
                decisions, failed = await self._evaluate_symbol(symbol, account, started)
            except AetherisError as exc:
                failed, decisions = (
                    True,
                    [
                        self._record(
                            symbol=symbol,
                            action=AutonomousAction.SKIPPED,
                            detail=f"{exc.code.value}: {exc}",
                            now=started,
                        )
                    ],
                )
            except Exception as exc:
                failed, decisions = (
                    True,
                    [
                        self._record(
                            symbol=symbol,
                            action=AutonomousAction.SKIPPED,
                            detail=f"Evaluation raised {type(exc).__name__}: {exc}",
                            now=started,
                        )
                    ],
                )
            produced.extend(decisions)
            if failed:
                failures += 1
                self._note_failure(symbol)
            else:
                self._consecutive_failures.pop(symbol, None)
            # Refresh the account between symbols so the second entry of an
            # iteration sees the margin the first one committed.
            account = await self._paper.get_account()

        self._finish_iteration(started, failures=failures, evaluated=len(universe))
        return tuple(produced)

    def _finish_iteration(self, started: datetime, *, failures: int, evaluated: int) -> None:
        self._iterations += 1
        self._last_iteration_at = started
        self._last_duration = (utcnow() - started).total_seconds()
        if evaluated and failures >= evaluated:
            self._total_failure_iterations += 1
        else:
            self._total_failure_iterations = 0

    def _note_failure(self, symbol: str) -> None:
        count = self._consecutive_failures.get(symbol, 0) + 1
        self._consecutive_failures[symbol] = count
        if count >= self._config.max_consecutive_failures:
            self._excluded[symbol] = (
                f"Dropped for this session after {count} consecutive failures. A "
                "delisted or permanently broken instrument should stop costing quota."
            )
            _log.warning("autonomous_symbol_dropped", symbol=symbol, failures=count)

    def _universe(self) -> tuple[str, ...]:
        """Configured symbols, minus any dropped this session, capped."""
        live = [s for s in self._config.symbols if s not in self._excluded]
        return tuple(live[: self._config.max_symbols])

    # ------------------------------------------------------------------
    # One symbol
    # ------------------------------------------------------------------

    async def _evaluate_symbol(
        self, symbol: str, account: PaperAccount, now: datetime
    ) -> tuple[list[AutonomousDecision], bool]:
        """Returns the decisions produced, and whether the symbol failed."""
        observation = await self._market_data.get_klines(
            symbol, self._config.timeframe, limit=self._config.candle_limit
        )
        raw = observation.value.candles if observation.value is not None else ()
        prepared = prepare_candles(raw, now)
        if not prepared.ok or not prepared.candles:
            return [
                self._record(
                    symbol=symbol,
                    action=AutonomousAction.SKIPPED,
                    detail=(
                        f"Candles for {symbol} are unusable: {prepared.detail}. No entry "
                        "is proposed and no price is invented."
                    ),
                    now=now,
                    source=observation.source,
                    data_status=observation.status.value,
                    data_age_seconds=observation.age_seconds,
                )
            ], True

        candles = prepared.candles
        bar_close = candles[-1].close_time
        result = evaluate_from_candles(
            candles,
            symbol=symbol,
            timeframe=self._config.timeframe,
            last_candle_time=bar_close,
        )
        atr_pct = _atr_percent(candles)

        open_position = next((p for p in account.positions if p.symbol == symbol), None)
        if open_position is not None:
            return await self._manage_open(
                symbol, open_position.side, result, now, bar_close
            ), False

        if result.status is not StrategyStatus.READY:
            return [
                self._record(
                    symbol=symbol,
                    action=AutonomousAction.SKIPPED,
                    detail=(
                        f"Analysis could not run: {result.status.value}. "
                        f"{result.detail or ''}".strip()
                    ),
                    now=now,
                    result=result,
                    bar_close=bar_close,
                    atr_percent=atr_pct,
                    source=observation.source,
                    data_status=observation.status.value,
                    data_age_seconds=observation.age_seconds,
                )
            ], True

        if result.bias is None or result.bias is StrategyBias.NEUTRAL:
            return [
                self._record(
                    symbol=symbol,
                    action=AutonomousAction.NO_SIGNAL,
                    detail=(
                        "The rule set reports NEUTRAL: the conditions for neither "
                        "direction were met, or they conflicted. A finding, not a gap."
                    ),
                    now=now,
                    result=result,
                    bar_close=bar_close,
                    atr_percent=atr_pct,
                    source=observation.source,
                    data_status=observation.status.value,
                    data_age_seconds=observation.age_seconds,
                )
            ], False

        return await self._propose_entry(
            symbol=symbol,
            result=result,
            account=account,
            atr_percent=atr_pct,
            bar_close=bar_close,
            now=now,
            source=observation.source,
        ), False

    async def _manage_open(
        self,
        symbol: str,
        side: PositionSide,
        result: StrategyResult,
        now: datetime,
        bar_close: datetime,
    ) -> list[AutonomousDecision]:
        """Close on an opposed bias, otherwise report the position as managed.

        A NEUTRAL reading never closes. Neutral means the conditions no longer
        align, not that they oppose, and closing on it would churn the account
        on every indecisive bar.
        """
        opposed = (side is PositionSide.LONG and result.bias is StrategyBias.SHORT_BIAS) or (
            side is PositionSide.SHORT and result.bias is StrategyBias.LONG_BIAS
        )
        if self._config.close_on_signal_flip and opposed and result.status is StrategyStatus.READY:
            outcome = await self._paper.close_position(symbol, reason=PaperExitReason.SIGNAL_FLIP)
            if outcome.accepted and outcome.trade is not None:
                self._closes += 1
                return [
                    self._record(
                        symbol=symbol,
                        action=AutonomousAction.CLOSED,
                        detail=(
                            f"The rule set now reads {result.bias.value if result.bias else '-'}, "
                            f"which opposes the open {side.value}. Closed at "
                            f"{outcome.trade.exit_price} for a net {outcome.trade.net_pnl} USDT."
                        ),
                        now=now,
                        result=result,
                        bar_close=bar_close,
                        exit_reason=PaperExitReason.SIGNAL_FLIP,
                        position_side=side,
                        trade_id=outcome.trade.trade_id,
                        realized_pnl=outcome.trade.net_pnl,
                    )
                ]
            return [
                self._record(
                    symbol=symbol,
                    action=AutonomousAction.MANAGED,
                    detail=(
                        f"A signal-flip close was attempted and refused: {outcome.detail}. "
                        "The position is left open rather than closed at an unusable price."
                    ),
                    now=now,
                    result=result,
                    bar_close=bar_close,
                    position_side=side,
                    rejection_code=outcome.order.rejection_code,
                    rejection_detail=outcome.order.rejection_detail,
                )
            ]

        return [
            self._record(
                symbol=symbol,
                action=AutonomousAction.MANAGED,
                detail=(
                    f"Position held. Levels were evaluated and none fired; the rule set "
                    f"reads {result.bias.value if result.bias else result.status.value}."
                ),
                now=now,
                result=result,
                bar_close=bar_close,
                position_side=side,
            )
        ]

    async def _propose_entry(
        self,
        *,
        symbol: str,
        result: StrategyResult,
        account: PaperAccount,
        atr_percent: Decimal | None,
        bar_close: datetime,
        now: datetime,
        source: str | None,
    ) -> list[AutonomousDecision]:
        side = OrderSide.BUY if result.bias is StrategyBias.LONG_BIAS else OrderSide.SELL
        client_order_id = build_client_order_id(symbol, self._config.timeframe, bar_close, side)

        margin = quantize_usdt(
            account.available_balance * self._config.position_size_percent / _HUNDRED
        )
        # The loop proposes. It does **not** rule: ADR 0006 made
        # PaperTradingService the single risk authority, so submitting is how
        # the risk engine is reached. Evaluating here as well would be a second
        # verdict, and the whole point of the ADR is that there is one.
        outcome = await self._paper.submit_order(
            SubmitOrderRequest(
                symbol=symbol,
                side=side,
                margin=margin,
                stop_loss_percent=self._config.stop_loss_percent,
                take_profit_percent=self._config.take_profit_percent,
                trailing_stop_percent=self._config.trailing_stop_percent,
                client_order_id=client_order_id,
            ),
            requested_leverage=self._config.requested_leverage,
            origin=AUTONOMOUS_ORIGIN,
        )

        if not outcome.accepted:
            self._refusals += 1
            return [
                self._record(
                    symbol=symbol,
                    action=AutonomousAction.REFUSED,
                    detail=outcome.detail or "Refused.",
                    now=now,
                    result=result,
                    bar_close=bar_close,
                    atr_percent=atr_percent,
                    side=side,
                    client_order_id=client_order_id,
                    rejection_code=outcome.order.rejection_code,
                    rejection_detail=outcome.order.rejection_detail,
                    leverage=outcome.order.leverage,
                    risk_max_leverage=outcome.risk_max_leverage,
                    checks_performed=outcome.checks_performed,
                    proposed_margin=margin,
                    source=source,
                )
            ]

        if outcome.accepted:
            self._entries += 1
            self._last_entry_at[symbol] = now
            return [
                self._record(
                    symbol=symbol,
                    action=AutonomousAction.ENTERED,
                    detail=outcome.detail or "Entered.",
                    now=now,
                    result=result,
                    bar_close=bar_close,
                    atr_percent=atr_percent,
                    side=side,
                    client_order_id=client_order_id,
                    order_id=outcome.order.order_id,
                    position_id=outcome.position.position_id if outcome.position else None,
                    leverage=outcome.order.leverage,
                    risk_max_leverage=outcome.risk_max_leverage,
                    checks_performed=outcome.checks_performed,
                    proposed_margin=outcome.position.margin if outcome.position else margin,
                    source=source,
                )
            ]

    async def _symbol_metadata(self, symbol: str) -> Symbol | None:
        try:
            return await self._market_data.get_symbol(symbol)
        except SymbolNotFoundError:
            return None

    # ------------------------------------------------------------------
    # The decision log
    # ------------------------------------------------------------------

    def decisions(self, limit: int = 100) -> tuple[AutonomousDecision, ...]:
        """Newest first."""
        return tuple(reversed(self._decisions[-limit:]))

    def _record(
        self,
        *,
        symbol: str,
        action: AutonomousAction,
        detail: str,
        now: datetime,
        result: StrategyResult | None = None,
        bar_close: datetime | None = None,
        atr_percent: Decimal | None = None,
        side: OrderSide | None = None,
        position_side: PositionSide | None = None,
        exit_reason: PaperExitReason | None = None,
        client_order_id: str | None = None,
        order_id: str | None = None,
        position_id: str | None = None,
        trade_id: str | None = None,
        rejection_code: RiskRejectionCode | None = None,
        rejection_detail: str | None = None,
        leverage: LeverageDecision | None = None,
        risk_max_leverage: Decimal | None = None,
        checks_performed: tuple[str, ...] = (),
        proposed_margin: Decimal | None = None,
        realized_pnl: Decimal | None = None,
        source: str | None = None,
        data_status: str | None = None,
        data_age_seconds: float | None = None,
    ) -> AutonomousDecision:
        self._sequence += 1
        decision = AutonomousDecision(
            decision_id=f"auto-decision-{self._sequence}",
            sequence=self._sequence,
            decided_at=now,
            symbol=symbol,
            timeframe=self._config.timeframe,
            action=action,
            detail=detail,
            bar_close_time=bar_close,
            strategy=trend_momentum.STRATEGY_KEY if result else None,
            strategy_status=result.status.value if result else None,
            bias=result.bias.value if result and result.bias else None,
            conditions_met=(
                (
                    result.long_conditions_met
                    if side is not OrderSide.SELL
                    else result.short_conditions_met
                )
                if result
                else None
            ),
            conditions_total=result.conditions_total if result else None,
            rejection_code=rejection_code,
            rejection_detail=rejection_detail,
            leverage=leverage,
            risk_max_leverage=risk_max_leverage,
            checks_performed=checks_performed,
            side=side,
            position_side=position_side,
            exit_reason=exit_reason,
            client_order_id=client_order_id,
            order_id=order_id,
            position_id=position_id,
            trade_id=trade_id,
            proposed_margin=proposed_margin,
            realized_pnl=realized_pnl,
            source=source or (result.source if result else None),
            data_status=data_status or (result.data_status if result else None),
            data_age_seconds=(
                data_age_seconds
                if data_age_seconds is not None
                else (result.data_age_seconds if result else None)
            ),
            atr_percent=atr_percent,
        )
        self._decisions.append(decision)
        overflow = len(self._decisions) - self._config.max_decision_log
        if overflow > 0:
            del self._decisions[:overflow]

        _log.info(
            "autonomous_decision",
            symbol=symbol,
            action=action.value,
            rejection_code=rejection_code.value if rejection_code else None,
        )
        return decision
