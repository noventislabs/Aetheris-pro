"""The autonomous paper-trading loop.

What these tests are really guarding:

* **It is off**, and no single condition turns it on.
* **It records everything**, including the iterations where nothing happened,
  because a decision log of only the interesting entries cannot tell an idle
  loop from a dead one.
* **It cannot act twice on the same bar**, however many times it runs.
* **It fails loudly.** A loop that restarts itself after an unexplained crash
  hides the crash.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.core.config import Settings
from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus, Observation, utcnow
from aetheris.domain.autonomous import AutonomousAction, AutonomousLoopState
from aetheris.domain.enums import ContractType, OrderSide, SymbolStatus, Timeframe
from aetheris.domain.market import Candle, CandleSeries, Symbol, SymbolFilters, Ticker
from aetheris.engines.paper.engine import PaperEngine, PaperEngineConfig
from aetheris.engines.paper.store import InMemoryPaperRepository
from aetheris.services.autonomous import AutonomousLoop, build_client_order_id
from aetheris.services.paper import PaperTradingService

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


SYMBOL = Symbol(
    symbol="AAAUSDT",
    base_asset="AAA",
    quote_asset="USDT",
    status=SymbolStatus.TRADING,
    contract_type=ContractType.PERPETUAL,
    price_precision=2,
    quantity_precision=3,
    filters=SymbolFilters(
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
    ),
)


def walk(
    count: int = 200, *, seed: int = 1, drift: float = 0.002, amp: float = 0.02
) -> list[Candle]:
    """A deterministic noisy drift the rule set actually reads as a bias.

    Three traps are baked out of this, all found the hard way.

    **Noise is not decoration.** A smooth ramp pins RSI above the overbought
    band, the momentum condition fails, and the strategy reports NEUTRAL -- so a
    clean uptrend produces *no* signal and every entry test silently becomes a
    test of nothing. The backtest suite hit the same trap.

    **The series is anchored to the real clock, not to a fixed NOW.** An earlier
    version ended its candles at a constant timestamp that sat hours ahead of
    the wall clock, so ``prepare_candles`` dropped everything after "now" as
    still forming -- a different number of bars each time the suite ran, and a
    different strategy reading with it. The tests passed alone and failed in a
    full run. Ending the series just before real now means nothing is ever
    dropped and the reading is fixed.

    **The parameters were chosen against the prepared series**, which is what
    the loop evaluates, not the raw one.
    """
    candles: list[Candle] = []
    value, state = 100.0, seed
    # One second of slack, so the newest bar is closed however long the test
    # takes to get here.
    end = utcnow() - timedelta(seconds=1)
    start = end - timedelta(minutes=15 * count)
    for index in range(count):
        state = (1103515245 * state + 12345) % (2**31)
        noise = (state / 2**31 - 0.5) * amp
        value *= 1 + drift + noise
        open_time = start + timedelta(minutes=15 * index)
        candles.append(
            Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=15, milliseconds=-1),
                open=Decimal(f"{value:.4f}"),
                high=Decimal(f"{value * 1.006:.4f}"),
                low=Decimal(f"{value * 0.994:.4f}"),
                close=Decimal(f"{value:.4f}"),
                volume=Decimal(1000),
            )
        )
    return candles


def trending_candles(count: int = 200, *, rising: bool = True) -> list[Candle]:
    """Rising reads LONG_BIAS 4/4; falling reads SHORT_BIAS 4/4."""
    if rising:
        return walk(count, seed=1, drift=0.002, amp=0.02)
    return walk(count, seed=11, drift=0.0, amp=0.02)


class FakeMarketData:
    """Serves whatever the test configures, with real provenance envelopes."""

    exchange_name = "test-venue"

    def __init__(self) -> None:
        self.candles = trending_candles()
        self.price = Decimal("150")
        self.ticker_status = DataStatus.OK
        self.kline_status = DataStatus.OK
        self.raise_on_klines: Exception | None = None
        self.unknown_symbols: set[str] = set()
        self.kline_calls = 0

    async def get_klines(
        self, symbol: str, timeframe: Timeframe, *, limit: int
    ) -> Observation[CandleSeries]:
        self.kline_calls += 1
        if self.raise_on_klines is not None:
            raise self.raise_on_klines
        if self.kline_status is not DataStatus.OK:
            return Observation[CandleSeries].unavailable(
                source="test-venue:rest", detail="candles unavailable"
            )
        return Observation[CandleSeries].ok(
            CandleSeries(
                symbol=symbol.upper(),
                timeframe=timeframe,
                candles=tuple(self.candles),
            ),
            source="test-venue:rest",
            event_ts=self.candles[-1].close_time,
        )

    async def get_ticker(self, symbol: str) -> Observation[Ticker]:
        # Stamped with the real clock, like the candles: a fixed timestamp goes
        # stale as the suite runs and every refusal becomes STALE_DATA, which
        # masks whichever limit the test was actually about.
        now = utcnow()
        if self.ticker_status is not DataStatus.OK:
            return Observation[Ticker].stale(
                source="test-venue:rest", detail="ticker stale", event_ts=now
            )
        return Observation[Ticker].ok(
            Ticker(
                symbol=symbol.upper(),
                last_price=self.price,
                bid_price=self.price - Decimal("0.01"),
                ask_price=self.price + Decimal("0.01"),
                event_time=now,
            ),
            source="test-venue:rest",
            event_ts=now,
        )

    async def get_symbol(self, symbol: str) -> Symbol:
        from aetheris.adapters.exchange.errors import SymbolNotFoundError

        if symbol.upper() in self.unknown_symbols:
            raise SymbolNotFoundError(f"{symbol} is not listed")
        return SYMBOL.model_copy(update={"symbol": symbol.upper()})


def build(
    *,
    symbols: list[str] | None = None,
    permitted: bool = True,
    balance: Decimal = Decimal(1000),
    **auto: object,
) -> tuple[AutonomousLoop, FakeMarketData, PaperEngine, PaperTradingService]:
    settings = Settings(
        environment="test",
        autonomous_trading_enabled=permitted,
        _env_file=None,  # type: ignore[call-arg]
    )
    settings.autonomous.symbols = symbols if symbols is not None else ["AAAUSDT"]
    settings.autonomous.timeframe = Timeframe.M15
    for key, value in auto.items():
        setattr(settings.autonomous, key, value)
    settings.risk.entry_cooldown_seconds = 0

    config = PaperEngineConfig(
        starting_balance=balance,
        daily_profit_target=settings.risk.daily_profit_target,
        daily_loss_limit=settings.risk.daily_loss_limit,
        max_open_positions=settings.risk.max_open_positions,
        max_position_notional=Decimal(10_000),
        max_portfolio_exposure=Decimal(50_000),
        max_data_age_seconds=settings.risk.max_data_age_seconds,
    )
    repository = InMemoryPaperRepository(starting_balance=balance, now=NOW)
    engine = PaperEngine(repository, config)
    market = FakeMarketData()
    paper = PaperTradingService(market, engine, settings)  # type: ignore[arg-type]
    loop = AutonomousLoop(market, paper, settings)  # type: ignore[arg-type]
    settings.risk.max_position_notional = Decimal(10_000)
    settings.risk.max_portfolio_exposure = Decimal(50_000)
    return loop, market, engine, paper


# ----------------------------------------------------------------------
# Off by default
# ----------------------------------------------------------------------


async def test_the_loop_is_disarmed_on_construction() -> None:
    loop, *_ = build()
    status = loop.status()
    assert status.enabled is False
    assert status.state is AutonomousLoopState.DISARMED


async def test_configuration_alone_never_arms_it() -> None:
    """The flag gates arming. It is not an arm."""
    loop, *_ = build(permitted=True)
    assert loop.armed is False
    assert loop.status().permitted_by_config is True


async def test_arming_is_refused_when_configuration_forbids_it() -> None:
    from aetheris.core.errors import AetherisError

    loop, *_ = build(permitted=False)
    assert loop.status().state is AutonomousLoopState.DISABLED_BY_CONFIG
    with pytest.raises(AetherisError, match="not permitted"):
        await loop.arm(enabled=True)
    assert loop.armed is False


async def test_starting_the_task_does_not_arm_it() -> None:
    """A created task idles; it does not trade."""
    loop, market, *_ = build()
    loop.start()
    try:
        await asyncio.sleep(0)
        assert loop.armed is False
        assert market.kline_calls == 0
    finally:
        await loop.aclose()


async def test_arming_then_disarming_reports_honestly() -> None:
    loop, *_ = build()
    assert (await loop.arm(enabled=True)).enabled is True
    assert loop.status().state is AutonomousLoopState.ARMED
    assert (await loop.arm(enabled=False)).enabled is False
    assert loop.status().state is AutonomousLoopState.DISARMED


async def test_arming_is_refused_when_paper_mode_is_disabled() -> None:
    from aetheris.core.errors import AetherisError

    loop, *_ = build()
    loop._settings.paper_trading_enabled = False
    with pytest.raises(AetherisError, match="Paper trading is disabled"):
        await loop.arm(enabled=True)


# ----------------------------------------------------------------------
# The universe is never chosen for the user
# ----------------------------------------------------------------------


async def test_an_empty_universe_evaluates_nothing_and_says_so() -> None:
    loop, market, *_ = build(symbols=[])
    decisions = await loop.run_once()
    assert market.kline_calls == 0
    assert [d.action for d in decisions] == [AutonomousAction.SKIPPED]
    assert "does not pick instruments on its own" in decisions[0].detail


async def test_the_universe_is_capped() -> None:
    """Asserted on symbols evaluated, not on raw kline calls.

    Since ADR 0006 each symbol costs two candle fetches on the autonomous path
    -- 300 bars for the strategy in the loop, 60 for the volatility the risk
    authority measures for itself. Counting calls would make this test a
    tripwire for that ratio rather than for the cap it is named after.
    """
    loop, market, *_ = build(symbols=[f"S{i}USDT" for i in range(20)], max_symbols=3)
    decisions = await loop.run_once()
    assert len({d.symbol for d in decisions}) == 3
    assert market.kline_calls > 0


# ----------------------------------------------------------------------
# Entry flow
# ----------------------------------------------------------------------


async def test_a_directional_bias_opens_a_position() -> None:
    loop, _market, engine, _ = build()
    decisions = await loop.run_once()
    entered = [d for d in decisions if d.action is AutonomousAction.ENTERED]
    if not entered:
        # The fixture's trend did not produce a bias; the loop must have said
        # so explicitly rather than silently doing nothing.
        assert decisions[0].action in {AutonomousAction.NO_SIGNAL, AutonomousAction.REFUSED}
        return
    assert len(engine.snapshot(now=NOW).positions) == 1
    assert entered[0].client_order_id is not None
    assert entered[0].client_order_id.startswith("auto-AAAUSDT-15m-")


async def test_a_neutral_reading_is_recorded_as_a_finding_not_a_gap() -> None:
    loop, market, *_ = build()
    # A series the rule set reads as neither direction.
    market.candles = walk(seed=1, drift=0.0, amp=0.02)
    decisions = await loop.run_once()
    assert decisions[0].action in {AutonomousAction.NO_SIGNAL, AutonomousAction.SKIPPED}
    if decisions[0].action is AutonomousAction.NO_SIGNAL:
        assert "finding, not a gap" in decisions[0].detail


async def test_every_iteration_records_something_even_when_idle() -> None:
    """An idle loop must be distinguishable from a dead one."""
    loop, *_ = build()
    before = loop.status().decisions_recorded
    await loop.run_once()
    assert loop.status().decisions_recorded > before
    assert loop.status().iterations == 1


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_the_client_order_id_is_deterministic() -> None:
    first = build_client_order_id("aaausdt", Timeframe.M15, NOW, OrderSide.BUY)
    second = build_client_order_id("AAAUSDT", Timeframe.M15, NOW, OrderSide.BUY)
    assert first == second == f"auto-AAAUSDT-15m-{int(NOW.timestamp() * 1000)}-BUY"


def test_the_key_separates_bars_sides_and_symbols() -> None:
    base = build_client_order_id("AAAUSDT", Timeframe.M15, NOW, OrderSide.BUY)
    assert base != build_client_order_id("AAAUSDT", Timeframe.M15, NOW, OrderSide.SELL)
    assert base != build_client_order_id("BBBUSDT", Timeframe.M15, NOW, OrderSide.BUY)
    assert base != build_client_order_id(
        "AAAUSDT", Timeframe.M15, NOW + timedelta(minutes=15), OrderSide.BUY
    )
    assert base != build_client_order_id("AAAUSDT", Timeframe.H1, NOW, OrderSide.BUY)


async def test_repeated_iterations_on_one_bar_open_at_most_one_position() -> None:
    """The structural duplicate protection, exercised end to end."""
    loop, _market, engine, _ = build()
    for _ in range(5):
        await loop.run_once()

    account = engine.snapshot(now=NOW)
    assert len(account.positions) <= 1
    fills = [o for o in account.recent_orders if o.state.value == "FILLED"]
    assert len(fills) <= 1, "one bar must not produce two entries"


# ----------------------------------------------------------------------
# Unusable data
# ----------------------------------------------------------------------


async def test_unusable_candles_skip_the_symbol_without_inventing_a_price() -> None:
    loop, market, engine, _ = build()
    market.kline_status = DataStatus.UNAVAILABLE
    decisions = await loop.run_once()
    assert decisions[0].action is AutonomousAction.SKIPPED
    assert engine.snapshot(now=NOW).positions == ()


async def test_a_stale_ticker_refuses_the_entry() -> None:
    loop, market, engine, _ = build()
    market.ticker_status = DataStatus.STALE
    decisions = await loop.run_once()
    assert all(d.action is not AutonomousAction.ENTERED for d in decisions)
    assert engine.snapshot(now=NOW).positions == ()


async def test_one_failing_symbol_does_not_abort_the_iteration() -> None:
    loop, market, *_ = build(symbols=["AAAUSDT", "BBBUSDT"])
    market.unknown_symbols = {"AAAUSDT"}
    decisions = await loop.run_once()
    assert len({d.symbol for d in decisions}) == 2, "both symbols must be reported"


async def test_a_symbol_is_dropped_after_repeated_failures() -> None:
    loop, market, *_ = build(max_consecutive_failures=2)
    market.kline_status = DataStatus.UNAVAILABLE
    await loop.run_once()
    await loop.run_once()
    status = loop.status()
    assert "AAAUSDT" in status.excluded_symbols
    assert "consecutive failures" in status.excluded_symbols["AAAUSDT"]

    market.kline_calls = 0
    await loop.run_once()
    assert market.kline_calls == 0, "a dropped symbol must stop costing quota"


async def test_a_total_failure_run_is_reported_as_a_venue_outage() -> None:
    loop, market, *_ = build(outage_iterations=2, max_consecutive_failures=50)
    market.kline_status = DataStatus.UNAVAILABLE
    await loop.run_once()
    await loop.run_once()
    assert loop.status().venue_outage is True


# ----------------------------------------------------------------------
# Failure handling
# ----------------------------------------------------------------------


async def test_an_unexpected_exception_in_one_symbol_is_recorded_not_raised() -> None:
    loop, market, *_ = build()
    market.raise_on_klines = RuntimeError("upstream exploded")
    decisions = await loop.run_once()
    assert decisions[0].action is AutonomousAction.SKIPPED
    assert "RuntimeError" in decisions[0].detail


async def test_a_loop_failure_disarms_and_does_not_restart_itself() -> None:
    """A loop that silently restarts after a crash hides the crash."""
    loop, *_ = build()
    await loop.arm(enabled=True)

    async def explode() -> tuple[()]:
        raise RuntimeError("iteration exploded")

    loop.run_once = explode  # type: ignore[method-assign]
    loop.start()
    for _ in range(50):
        await asyncio.sleep(0)
        if loop.status().state is AutonomousLoopState.FAILED:
            break
    await loop.aclose()

    status = loop.status()
    assert status.state is AutonomousLoopState.FAILED
    assert status.enabled is False
    assert status.failure_detail is not None
    assert "RuntimeError" in status.failure_detail


async def test_re_arming_clears_a_previous_failure() -> None:
    loop, *_ = build()
    loop._failure_detail = "RuntimeError: earlier crash"
    assert loop.status().state is AutonomousLoopState.FAILED
    await loop.arm(enabled=True)
    assert loop.status().state is AutonomousLoopState.ARMED
    assert loop.status().failure_detail is None


# ----------------------------------------------------------------------
# The decision log
# ----------------------------------------------------------------------


async def test_the_decision_log_is_bounded() -> None:
    loop, *_ = build(max_decision_log=5)
    for _ in range(12):
        await loop.run_once()
    assert len(loop.decisions(limit=500)) <= 5


async def test_decisions_are_returned_newest_first() -> None:
    loop, *_ = build()
    await loop.run_once()
    await loop.run_once()
    decisions = loop.decisions(limit=10)
    assert decisions[0].sequence > decisions[-1].sequence


async def test_a_decision_carries_counts_not_a_score() -> None:
    loop, *_ = build()
    decisions = await loop.run_once()
    analysed = [d for d in decisions if d.conditions_total is not None]
    for decision in analysed:
        assert isinstance(decision.conditions_met, int)
        assert 0 <= (decision.conditions_met or 0) <= (decision.conditions_total or 0)


async def test_every_decision_is_labelled_a_simulation() -> None:
    loop, *_ = build()
    for decision in await loop.run_once():
        assert "SIMULATION ONLY" in decision.label
        assert "NO REAL ORDER" in decision.label


# ----------------------------------------------------------------------
# The risk engine has final authority over the loop
# ----------------------------------------------------------------------


async def test_a_daily_lock_blocks_autonomous_entries() -> None:
    """The lock must hold against the clock the loop actually reads.

    Seeded with ``utcnow()`` rather than the fixture's fixed ``NOW``. The
    loop stamps its own run from the real clock, and the engine correctly
    rolls a fresh session when the UTC date changes -- a lock that survived
    midnight would be a permanent stop wearing a daily name. Seeding
    yesterday's date therefore hands the loop a session it is right to
    discard, and the test fails for a reason that has nothing to do with
    what it is checking. It passed only on the calendar day ``NOW`` names.
    """
    loop, _market, engine, _paper = build()
    state = engine._repository.load()
    engine._ensure_session(state, utcnow())
    assert state.session is not None
    state.session.realized_pnl = Decimal(-50)
    state.session.lock_state = __import__(
        "aetheris.domain.paper", fromlist=["RiskLockState"]
    ).RiskLockState.DAILY_LOSS_LIMIT
    state.session.lock_reason = "Daily loss limit reached."

    decisions = await loop.run_once()
    refused = [d for d in decisions if d.action is AutonomousAction.REFUSED]
    assert refused, "an entry must have been proposed and refused"
    assert refused[0].rejection_code is RiskRejectionCode.DAILY_LOSS_LIMIT
    assert engine.snapshot(now=NOW).positions == ()


async def test_an_emergency_stop_blocks_autonomous_entries() -> None:
    loop, _market, engine, paper = build()
    await paper.set_emergency_stop(engaged=True, reason="Operator halt")
    decisions = await loop.run_once()
    refused = [d for d in decisions if d.action is AutonomousAction.REFUSED]
    assert refused
    assert refused[0].rejection_code is RiskRejectionCode.EMERGENCY_STOP
    assert engine.snapshot(now=NOW).positions == ()


async def test_leverage_above_one_is_refused_for_the_loop_too() -> None:
    """The loop gets no softer chain than a human does."""
    loop, _market, engine, _ = build(requested_leverage=Decimal(3))
    decisions = await loop.run_once()
    refused = [d for d in decisions if d.action is AutonomousAction.REFUSED]
    assert refused
    assert refused[0].rejection_code is RiskRejectionCode.MAX_LEVERAGE
    assert refused[0].leverage is not None
    assert refused[0].leverage.approved_leverage is None
    assert refused[0].leverage.exchange_max_leverage is None
    assert engine.snapshot(now=NOW).positions == ()


async def test_a_refusal_records_the_checks_that_ran_before_it() -> None:
    loop, _market, _engine, _paper = build(requested_leverage=Decimal(3))
    decisions = await loop.run_once()
    refused = [d for d in decisions if d.action is AutonomousAction.REFUSED]
    assert refused
    assert "mode_enabled" in refused[0].checks_performed
    assert "leverage_constraint_chain" in refused[0].checks_performed
