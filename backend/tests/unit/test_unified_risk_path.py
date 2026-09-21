"""ADR 0006 — one risk authority for every paper order.

The property under test is not "manual orders are checked" but something
stronger: **manual and autonomous orders reach the same authority and receive
the same verdict**, and the order that fills is the one that verdict approved.

Before this, `engines/risk/` had exactly one caller and both paths re-resolved
leverage through a resolver told the risk engine did not exist. That was
conservative -- it could only ever return 1x -- and would have diverged the
moment a venue ceiling became knowable, which is precisely what phase 8c does.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from datetime import timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.volatility import (
    MIN_CANDLES_FOR_ATR,
    VolatilityStatus,
    measure_atr_percent,
)
from aetheris.core.config import Settings
from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import Observation, utcnow
from aetheris.domain.enums import (
    ContractType,
    OrderSide,
    SymbolStatus,
    Timeframe,
)
from aetheris.domain.market import Candle, CandleSeries, Symbol, SymbolFilters, Ticker
from aetheris.engines.order.engine import ReconciliationPending
from aetheris.engines.paper.engine import PaperEngine, PaperEngineConfig, SubmitOrderRequest
from aetheris.engines.paper.store import InMemoryPaperRepository
from aetheris.services.autonomous import AUTONOMOUS_ORIGIN
from aetheris.services.paper import MANUAL_RISK_CANDLES, PaperTradingService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


SYMBOL = Symbol(
    symbol="TESTUSDT",
    base_asset="TEST",
    quote_asset="USDT",
    status=SymbolStatus.TRADING,
    contract_type=ContractType.PERPETUAL,
    price_precision=2,
    quantity_precision=3,
    filters=SymbolFilters(
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal(5),
    ),
)


def candles(count: int, *, volatile: bool = False, age_minutes: int = 0) -> tuple[Candle, ...]:
    """Real-shaped bars ending just before now, so none is still forming."""
    end = utcnow() - timedelta(minutes=age_minutes) - timedelta(seconds=1)
    out = []
    for i in range(count):
        open_time = end - timedelta(minutes=15 * (count - i))
        base = Decimal(100)
        spread = Decimal(40) if volatile else Decimal("0.4")
        out.append(
            Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=15, milliseconds=-1),
                open=base,
                high=base + spread,
                low=base - spread,
                close=base,
                volume=Decimal(1000),
            )
        )
    return tuple(out)


class Market:
    """A market whose candle and ticker behaviour the test dictates."""

    exchange_name = "test-venue"

    def __init__(self) -> None:
        self.candles = candles(60)
        self.kline_status = "OK"
        self.ticker_ok = True
        self.raise_on_klines: Exception | None = None
        self.kline_calls = 0
        self.kline_limits: list[int] = []

    async def get_ticker(self, symbol: str) -> Observation[Ticker]:
        now = utcnow()
        if not self.ticker_ok:
            return Observation[Ticker].stale(source="t:rest", detail="stale", event_ts=now)
        return Observation[Ticker].ok(
            Ticker(
                symbol=symbol.upper(),
                last_price=Decimal(100),
                bid_price=Decimal("99.99"),
                ask_price=Decimal("100.01"),
                event_time=now,
            ),
            source="t:rest",
            event_ts=now,
        )

    async def get_symbol(self, symbol: str) -> Symbol:
        return SYMBOL.model_copy(update={"symbol": symbol.upper()})

    async def get_klines(
        self, symbol: str, timeframe: Timeframe, *, limit: int
    ) -> Observation[CandleSeries]:
        self.kline_calls += 1
        self.kline_limits.append(limit)
        if self.raise_on_klines is not None:
            raise self.raise_on_klines
        if self.kline_status == "UNAVAILABLE":
            return Observation[CandleSeries].unavailable(source="t:rest", detail="gone")
        if self.kline_status == "STALE":
            return Observation[CandleSeries].stale(source="t:rest", detail="old", event_ts=utcnow())
        return Observation[CandleSeries].ok(
            CandleSeries(symbol=symbol.upper(), timeframe=timeframe, candles=self.candles),
            source="t:rest",
            event_ts=self.candles[-1].close_time,
        )


def build(**overrides: object) -> tuple[PaperTradingService, Market, PaperEngine]:
    settings = Settings(environment="test", _env_file=None)  # type: ignore[call-arg]
    for key, value in overrides.items():
        setattr(settings.risk, key, value)
    config = PaperEngineConfig(
        starting_balance=Decimal(1000),
        daily_profit_target=settings.risk.daily_profit_target,
        daily_loss_limit=settings.risk.daily_loss_limit,
        max_open_positions=settings.risk.max_open_positions,
        max_position_notional=Decimal(10_000),
        max_portfolio_exposure=Decimal(50_000),
        max_data_age_seconds=settings.risk.max_data_age_seconds,
    )
    settings.risk.max_position_notional = Decimal(10_000)
    settings.risk.max_portfolio_exposure = Decimal(50_000)
    engine = PaperEngine(
        InMemoryPaperRepository(starting_balance=Decimal(1000), now=utcnow()), config
    )
    market = Market()
    return PaperTradingService(market, engine, settings), market, engine  # type: ignore[arg-type]


def order(**kw: object) -> SubmitOrderRequest:
    base: dict[str, object] = {
        "symbol": "TESTUSDT",
        "side": OrderSide.BUY,
        "margin": Decimal(50),
        "stop_loss_percent": Decimal(2),
    }
    base.update(kw)
    return SubmitOrderRequest(**base)  # type: ignore[arg-type]


def code(result) -> str:
    return "ACCEPTED" if result.accepted else result.order.rejection_code.value


# ----------------------------------------------------------------------
# There is exactly one authority, and one leverage decision
# ----------------------------------------------------------------------


def test_no_second_leverage_resolver_exists_anywhere_in_the_order_path() -> None:
    """The invariant ADR 0006 exists to establish, asserted at source.

    ``risk_engine_available=False`` was the bypass: a resolver told the risk
    engine did not exist, deciding leverage for orders that had just been ruled
    on by it.
    """
    root = pathlib.Path(inspect.getfile(PaperTradingService)).parent.parent
    for relative in ("services/paper.py", "services/autonomous.py", "engines/paper/engine.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "risk_engine_available=False" not in source, relative
        assert "_resolve_leverage" not in source, relative


def test_the_paper_service_is_the_only_caller_of_the_risk_authority() -> None:
    root = pathlib.Path(inspect.getfile(PaperTradingService)).parent.parent
    loop_source = (root / "services/autonomous.py").read_text(encoding="utf-8")
    assert "risk_evaluate" not in loop_source, (
        "the loop must submit, not rule -- a second evaluation is a second verdict"
    )
    service_source = (root / "services/paper.py").read_text(encoding="utf-8")
    assert "risk_evaluate(" in service_source


async def test_manual_and_autonomous_receive_identical_verdicts() -> None:
    """The same order, from either caller, is ruled on the same way."""
    manual_service, _, _ = build()
    auto_service, _, _ = build()

    manual = await manual_service.submit_order(order(), requested_leverage=Decimal(1))
    auto = await auto_service.submit_order(
        order(), requested_leverage=Decimal(1), origin=AUTONOMOUS_ORIGIN
    )
    assert code(manual) == code(auto) == "ACCEPTED"
    assert manual.order.leverage is not None and auto.order.leverage is not None
    assert manual.order.leverage.approved_leverage == auto.order.leverage.approved_leverage
    assert manual.checks_performed == auto.checks_performed


async def test_the_filled_leverage_equals_the_risk_approved_leverage() -> None:
    service, _, engine = build()
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.accepted
    assert result.order.leverage is not None
    approved = result.order.leverage.approved_leverage
    assert approved == Decimal(1)
    position = engine.snapshot(now=utcnow()).positions[0]
    assert position.approved_leverage == approved, "the fill used the approved verdict"


# ----------------------------------------------------------------------
# Leverage stays fail-closed above the domain minimum
# ----------------------------------------------------------------------


async def test_one_times_is_accepted_when_every_other_check_passes() -> None:
    service, _, _ = build()
    assert (await service.submit_order(order(), requested_leverage=Decimal(1))).accepted


@pytest.mark.parametrize("leverage", ["1.5", "2", "3", "125", "500"])
async def test_above_one_times_stays_fail_closed_while_the_venue_ceiling_is_unknown(
    leverage: str,
) -> None:
    service, _, _ = build()
    result = await service.submit_order(order(), requested_leverage=Decimal(leverage))
    assert result.order.rejection_code is RiskRejectionCode.MAX_LEVERAGE
    assert result.order.leverage is not None
    assert result.order.leverage.approved_leverage is None
    assert result.order.leverage.exchange_max_leverage is None


# ----------------------------------------------------------------------
# Volatility must be measured from real history
# ----------------------------------------------------------------------


def test_fewer_than_fifteen_candles_is_insufficient_history_not_a_guess() -> None:
    measurement = measure_atr_percent(candles(MIN_CANDLES_FOR_ATR - 1))
    assert measurement.status is VolatilityStatus.INSUFFICIENT_HISTORY
    assert measurement.atr_percent is None


def test_enough_candles_produce_a_measurement() -> None:
    measurement = measure_atr_percent(candles(MIN_CANDLES_FOR_ATR))
    assert measurement.status is VolatilityStatus.MEASURED
    assert measurement.atr_percent is not None


def test_no_candles_reads_as_unavailable_not_insufficient() -> None:
    """Distinct conditions get distinct answers, so refusals can say which."""
    assert measure_atr_percent(()).status is VolatilityStatus.CANDLES_UNAVAILABLE


async def test_a_newly_listed_symbol_is_refused_with_its_own_code() -> None:
    """ADR 0006 §N.1 -- refuse, never fabricate or substitute an ATR."""
    service, market, _ = build()
    market.candles = candles(8)
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.order.rejection_code is RiskRejectionCode.INSUFFICIENT_HISTORY
    assert "ATR(14) needs 15" in (result.order.rejection_detail or "")


async def test_unavailable_candles_are_refused_and_distinguishable() -> None:
    service, market, _ = build()
    market.kline_status = "UNAVAILABLE"
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.order.rejection_code is RiskRejectionCode.STALE_DATA
    assert "unusable" in (result.order.rejection_detail or "").lower()


async def test_a_venue_error_fetching_candles_is_refused_not_raised() -> None:
    from aetheris.core.errors import UpstreamUnavailableError

    service, market, _ = build()
    market.raise_on_klines = UpstreamUnavailableError("venue down")
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.order.rejection_code is RiskRejectionCode.STALE_DATA
    assert "not estimated" in (result.order.rejection_detail or "")


async def test_stale_candles_are_refused() -> None:
    service, market, _ = build()
    market.kline_status = "STALE"
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.order.rejection_code is RiskRejectionCode.STALE_DATA


async def test_a_stale_ticker_is_refused_even_with_good_candles() -> None:
    service, market, _ = build()
    market.ticker_ok = False
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.order.rejection_code is RiskRejectionCode.STALE_DATA


async def test_abnormal_volatility_is_refused() -> None:
    service, market, _ = build()
    market.candles = candles(60, volatile=True)
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.order.rejection_code is RiskRejectionCode.ABNORMAL_VOLATILITY


async def test_the_manual_path_asks_for_sixty_candles_not_three_hundred() -> None:
    """ATR(14) needs 15; fetching 300 spends venue quota on nothing."""
    service, market, _ = build()
    await service.submit_order(order(), requested_leverage=Decimal(1))
    assert market.kline_limits == [MANUAL_RISK_CANDLES]
    assert MANUAL_RISK_CANDLES == 60


# ----------------------------------------------------------------------
# The three newly active checks
# ----------------------------------------------------------------------


async def test_the_manual_cooldown_refuses_a_rapid_re_entry() -> None:
    service, _, _engine = build()
    first = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert first.accepted
    await service.close_position("TESTUSDT")

    again = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert again.order.rejection_code is RiskRejectionCode.COOLDOWN
    assert "60s" in (again.order.rejection_detail or "")


async def test_the_manual_cooldown_is_sixty_seconds_not_the_loop_s_three_hundred() -> None:
    """ADR 0006 §N.3 -- a human is not a loop on 15-minute bars."""
    settings = Settings(environment="test", _env_file=None)  # type: ignore[call-arg]
    assert settings.risk.manual_entry_cooldown_seconds == 60
    assert settings.risk.entry_cooldown_seconds == 300


async def test_the_autonomous_cooldown_is_unchanged() -> None:
    service, _, _ = build()
    manual = service._policy("manual")
    autonomous = service._policy(AUTONOMOUS_ORIGIN)
    assert manual.entry_cooldown_seconds == 60.0
    assert autonomous.entry_cooldown_seconds == 300.0


async def test_holding_a_position_reports_the_symbol_limit_not_the_cooldown() -> None:
    """Position facts outrank pacing.

    ``last_entry_at`` is stamped on entry, so holding a position always implies
    a recent one. Checking pacing first would report "wait 60s" for the whole
    cooldown window when the real answer is "you already hold this".
    """
    service, _, _ = build()
    assert (await service.submit_order(order(), requested_leverage=Decimal(1))).accepted
    again = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert again.order.rejection_code is RiskRejectionCode.SYMBOL_LIMIT


async def test_insufficient_balance_is_refused() -> None:
    service, _, _ = build()
    result = await service.submit_order(
        order(margin=Decimal(50_000)), requested_leverage=Decimal(1)
    )
    assert result.order.rejection_code is RiskRejectionCode.INSUFFICIENT_BALANCE


async def test_no_durable_order_store_means_no_orders_can_be_pending() -> None:
    """Zero here is a fact, not an assumption.

    With no store configured there are no order records anywhere, so nothing
    can be awaiting reconciliation. This is the one case where zero is safe.
    """
    service, _, _ = build()
    pending = await service._reconciliation_pending()
    assert pending.count == 0
    view = service._account_view(service.engine.snapshot(now=utcnow()), "TESTUSDT", pending)
    assert view.unreconciled_orders == 0


async def test_the_real_pending_count_reaches_the_risk_engine_and_refuses() -> None:
    """The check that could never fire, firing.

    ``unreconciled_orders`` was hardcoded to zero, which made the risk engine's
    RECONCILIATION_PENDING branch unreachable -- a safety rule that read
    correctly and could not trigger. This binds a store that reports a pending
    order and asserts the refusal comes back with its code.
    """

    class PendingStore:
        async def reconciliation_pending(self) -> ReconciliationPending:
            return ReconciliationPending(2, "two orders are awaiting reconciliation")

    service, _, _ = build()
    service.bind_order_engine(PendingStore())  # type: ignore[arg-type]

    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert not result.accepted
    assert result.order.rejection_code is RiskRejectionCode.RECONCILIATION_PENDING
    assert "awaiting reconciliation" in (result.order.rejection_detail or "")


async def test_an_unreadable_order_store_fails_closed_rather_than_returning_zero() -> None:
    """A store that will not answer is not a store with nothing in it.

    The failure mode this guards is the quiet one: an exception swallowed into
    a default of zero would let trading continue precisely when the system has
    lost track of what it has outstanding.
    """

    class BrokenStore:
        async def reconciliation_pending(self) -> ReconciliationPending:
            raise RuntimeError("connection reset")

    service, _, _ = build()
    service.bind_order_engine(BrokenStore())  # type: ignore[arg-type]

    pending = await service._reconciliation_pending()
    assert pending.count >= 1
    assert "could not be read" in pending.detail

    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert not result.accepted
    assert result.order.rejection_code is RiskRejectionCode.RECONCILIATION_PENDING


# ----------------------------------------------------------------------
# checks_performed
# ----------------------------------------------------------------------


async def test_an_accepted_order_reports_the_checks_that_ran() -> None:
    service, _, _ = build()
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert "mode_enabled" in result.checks_performed
    assert "measured_volatility" in result.checks_performed
    assert "leverage_constraint_chain" in result.checks_performed
    assert result.checks_performed[-1] == "approved"


async def test_a_refused_order_reports_the_checks_that_ran_before_it() -> None:
    service, market, _ = build()
    market.candles = candles(8)
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert "mode_enabled" in result.checks_performed
    assert result.checks_performed[-1] == "measured_volatility"
    assert "approved" not in result.checks_performed


async def test_checks_performed_names_checks_only_and_leaks_nothing() -> None:
    """Names, not thresholds, balances or internals (ADR 0006 §N.2)."""
    service, _, _ = build()
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    for name in result.checks_performed:
        assert name.replace("_", "").isalnum()
        assert not any(ch.isdigit() for ch in name)


# ----------------------------------------------------------------------
# Idempotency survives the new authority
# ----------------------------------------------------------------------


async def test_a_replay_is_not_re_ruled_by_the_risk_engine() -> None:
    """A retry must return what happened, not a fresh verdict.

    Without this, the first attempt opens a position and the retry reports a
    cooldown refusal for it -- a different answer to the one already applied,
    which is the exact failure idempotency exists to prevent.
    """
    service, _, engine = build()
    first = await service.submit_order(
        order(client_order_id="retry-1"), requested_leverage=Decimal(1)
    )
    assert first.accepted

    second = await service.submit_order(
        order(client_order_id="retry-1"), requested_leverage=Decimal(1)
    )
    assert second.accepted
    assert second.order.idempotent_replay is True
    assert second.order.order_id == first.order.order_id
    assert len(engine.snapshot(now=utcnow()).positions) == 1


async def test_a_replayed_refusal_replays_the_refusal() -> None:
    service, _, _ = build()
    first = await service.submit_order(order(client_order_id="dup"), requested_leverage=Decimal(3))
    assert first.order.rejection_code is RiskRejectionCode.MAX_LEVERAGE

    second = await service.submit_order(order(client_order_id="dup"), requested_leverage=Decimal(1))
    assert second.order.idempotent_replay is True
    assert second.order.rejection_code is RiskRejectionCode.MAX_LEVERAGE


# ----------------------------------------------------------------------
# Two gates, still
# ----------------------------------------------------------------------


def test_the_phase_six_gate_is_still_called() -> None:
    """Neither gate trusts the other."""
    source = pathlib.Path(inspect.getfile(PaperEngine)).read_text(encoding="utf-8")
    assert "check_entry(" in source, "the engine's own gate must still be called"
    assert "check_authority(" in source


async def test_an_order_approved_by_risk_still_passes_the_phase_six_gate() -> None:
    service, _, _engine = build()
    result = await service.submit_order(order(), requested_leverage=Decimal(1))
    assert result.accepted


async def test_concurrent_submissions_still_cannot_both_open_one_symbol() -> None:
    service, _, engine = build()
    first = asyncio.create_task(
        service.submit_order(order(client_order_id="a"), requested_leverage=Decimal(1))
    )
    second = asyncio.create_task(
        service.submit_order(order(client_order_id="b"), requested_leverage=Decimal(1))
    )
    results = await asyncio.gather(first, second)
    assert sum(1 for r in results if r.accepted) == 1
    assert len(engine.snapshot(now=utcnow()).positions) == 1
