"""Opt-in tests against the **real** Binance futures testnet.

These are the tests the controlled-transport suite cannot replace. A fake venue
proves what this system does; only the real one proves that the request shape,
the signature and the parameter names are the ones Binance actually accepts. A
signing bug looks identical to a correct implementation until a real server
rejects it.

**They never run without credentials**, and credentials are never stored in the
repository or required by CI. Skipping is reported as skipping: nothing here
passes by default, and a green suite on a machine without keys is not evidence
that testnet execution works.

To run them::

    AETHERIS_TESTNET_API_KEY=...  in backend/.env
    AETHERIS_TESTNET_API_SECRET=...
    AETHERIS_TESTNET_REAL_TESTS=1
    pytest tests/integration/test_testnet_live_optin.py

The opt-in flag is separate from the credentials on purpose. Having keys
configured for the application should not silently start placing orders from a
test run.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

import pytest
import pytest_asyncio
from tests.integration.test_persistence import ENV_FILE

from aetheris.adapters.exchange.binance.testnet_adapter import BinanceTestnetTradingAdapter
from aetheris.adapters.exchange.errors import ExchangeError
from aetheris.core.config import TESTNET_ALLOWED_HOST, Settings
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.order import OrderIntent, OrderOrigin, OrderRecord
from aetheris.domain.venue import MarginMode, PositionMode
from aetheris.engines.order.identity import build_intent_key, client_order_id

pytestmark = pytest.mark.database

OPT_IN = "AETHERIS_TESTNET_REAL_TESTS"
#: Deliberately tiny and deliberately far from the market, so an order placed
#: by a test cannot fill. Cancelled immediately afterwards regardless.
PROBE_SYMBOL = "BTCUSDT"


def _settings() -> Settings:
    if os.environ.get(OPT_IN) != "1":
        pytest.skip(f"real testnet tests are opt-in; set {OPT_IN}=1 to run them")
    if not ENV_FILE.exists():
        pytest.skip("no backend/.env")
    settings = Settings(_env_file=str(ENV_FILE))  # type: ignore[call-arg]
    if not settings.testnet.credentials_present:
        pytest.skip("AETHERIS_TESTNET_API_KEY / _API_SECRET are not configured")
    return settings


@pytest.fixture(scope="module")
def settings() -> Settings:
    return _settings()


@pytest_asyncio.fixture
async def adapter(settings: Settings) -> AsyncIterator[BinanceTestnetTradingAdapter]:
    client = BinanceTestnetTradingAdapter(settings.testnet)
    try:
        await client.sync_clock()
        yield client
    finally:
        await client.aclose()


# ----------------------------------------------------------------------
# Authentication and identity
# ----------------------------------------------------------------------


async def test_the_signature_is_accepted_by_the_real_venue(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The one thing no fake can establish.

    A wrong parameter order, a missing recvWindow or a signature computed over
    a re-encoded dictionary all look correct locally and are refused here.

    Asserted on the balance rather than on ``can_trade``: a balance can only
    have come from an accepted signature, whereas the permission flag lives on
    a different endpoint version and says nothing about whether we
    authenticated. Conflating the two is what made this test fail for a reason
    that had nothing to do with signing.
    """
    account = await adapter.account_identity()
    assert account.available_balance is not None
    assert account.account_id


async def test_the_trading_permission_is_read_not_assumed(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The permission must come from the venue, in either direction.

    ``None`` would mean we could not find out, and this build refuses on
    anything that is not ``True`` -- so a real answer is what makes the account
    usable at all.
    """
    account = await adapter.account_identity()
    assert account.can_trade is not None, "permission unknown; the venue was not readable"
    assert account.can_trade is True, "this Demo key cannot trade futures"


async def test_margin_mode_is_readable_for_a_symbol_with_no_position(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The flat-account case, against the real venue.

    The pre-trade check runs before the first order on a symbol, which is
    exactly when the account holds no position in it.
    """
    mode = await adapter.margin_mode(PROBE_SYMBOL)
    assert mode in (MarginMode.ISOLATED, MarginMode.CROSSED)


async def test_the_adapter_is_talking_to_the_testnet_and_not_production(
    settings: Settings,
) -> None:
    assert TESTNET_ALLOWED_HOST in settings.testnet.rest_base_url
    assert "fapi.binance.com" not in settings.testnet.rest_base_url.replace(
        TESTNET_ALLOWED_HOST, ""
    )


async def test_the_venue_account_is_in_one_way_mode(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """If this fails, the account is in hedge mode and 8b refuses to trade it."""
    assert await adapter.position_mode() is PositionMode.ONE_WAY


# ----------------------------------------------------------------------
# Leverage and margin, read from the real venue
# ----------------------------------------------------------------------


async def test_real_leverage_brackets_are_tiered_and_finite(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The ceiling is whatever the venue says. Never 500, never assumed."""
    small = await adapter.leverage_bracket(PROBE_SYMBOL, notional=Decimal(100))
    large = await adapter.leverage_bracket(PROBE_SYMBOL, notional=Decimal(5_000_000))

    assert small.max_leverage > 0
    assert large.max_leverage > 0
    # Tiering is the property that matters: a bigger position must not be
    # allowed more leverage than a smaller one.
    assert large.max_leverage <= small.max_leverage


async def test_setting_leverage_is_echoed_back_by_the_real_venue(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """Set-then-verify, against the server that actually applies it."""
    applied = await adapter.set_leverage(PROBE_SYMBOL, Decimal(2))
    assert applied == Decimal(2)


async def test_isolated_margin_can_be_established_and_read_back(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """Includes the -4046 "no need to change" path, which is the goal state."""
    await adapter.set_margin_mode(PROBE_SYMBOL, MarginMode.ISOLATED)
    assert await adapter.margin_mode(PROBE_SYMBOL) is MarginMode.ISOLATED
    # Idempotent: asking again must not be an error.
    await adapter.set_margin_mode(PROBE_SYMBOL, MarginMode.ISOLATED)


async def test_session_realized_pnl_can_be_read(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The daily loss limit is judged against this. It must be readable."""
    from aetheris.core.freshness import utcnow

    value = await adapter.session_realized_pnl(since=utcnow())
    assert isinstance(value, Decimal)


# ----------------------------------------------------------------------
# A real order, placed and cancelled
# ----------------------------------------------------------------------


async def test_querying_an_order_that_does_not_exist_returns_none(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """``-2013`` from the real venue, mapped to ``None`` rather than raising.

    ``None`` means "the venue has no such order", never "it never existed":
    only the caller knows whether it was sent, and only that makes absence
    meaningful.
    """
    absent = client_order_id(
        account_id=str(uuid.uuid4()),
        intent_key=build_intent_key(
            symbol=PROBE_SYMBOL,
            side=OrderSide.BUY,
            purpose="testnet",
            bucket=f"absent-{uuid.uuid4().hex[:8]}",
        ),
    )
    assert await adapter.query(symbol=PROBE_SYMBOL, client_order_id=absent) is None


async def test_open_orders_reads_from_the_endpoint_that_actually_reads(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """``/fapi/v1/openOrders``, not ``/fapi/v1/allOpenOrders``.

    The published endpoint table lists the latter as a readable GET. It is
    DELETE-only -- it cancels everything -- and a GET returns ``-5000``. This
    test is what stops that documentation error from being reintroduced.
    """
    orders = await adapter.open_orders(symbol=PROBE_SYMBOL)
    assert isinstance(orders, tuple)


# ----------------------------------------------------------------------
# A real order, placed and cancelled
# ----------------------------------------------------------------------


async def test_a_real_order_is_placed_queried_and_cancelled(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The full lifecycle against Binance, with a deliberately unfillable order.

    A BUY limit priced far below the market cannot match, so the order rests on
    the book and is cancelled before anything can happen to it. The point is
    not to trade: it is to prove that the request shape Binance accepts is the
    one this adapter sends, which is the single thing a controlled transport
    can never establish.

    Cleanup runs in ``finally`` and is unconditional -- an assertion failure
    must not leave a resting order behind -- and the last assertions check that
    nothing of ours remains and nothing filled, so a leak fails the test rather
    than escaping it.
    """
    # Public endpoints, unsigned: the venue's own filters decide what a valid
    # order looks like, rather than this test guessing at tick and step sizes.
    info = await adapter._request("GET", "/fapi/v1/exchangeInfo", signed=False)
    entry = next(s for s in info["symbols"] if s["symbol"] == PROBE_SYMBOL)
    filters = {f["filterType"]: f for f in entry["filters"]}
    tick = Decimal(filters["PRICE_FILTER"]["tickSize"])
    step = Decimal(filters["LOT_SIZE"]["stepSize"])
    min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
    min_notional = Decimal(filters.get("MIN_NOTIONAL", {}).get("notional", "5"))

    ticker = await adapter._request(
        "GET", "/fapi/v1/ticker/price", {"symbol": PROBE_SYMBOL}, signed=False
    )
    market = Decimal(str(ticker["price"]))

    # Half the market price: far enough that it cannot fill, and still on the
    # venue's tick grid.
    price = (market / 2 / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    needed = min_notional * Decimal("1.2") / price
    quantity = max(min_qty, (needed / step).to_integral_value(rounding=ROUND_UP) * step)

    now = datetime.now(UTC)
    bucket = "real-" + uuid.uuid4().hex[:10]
    intent = OrderIntent(
        account_id=str(uuid.uuid4()),
        symbol=PROBE_SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        mode=TradingMode.TESTNET,
        origin=OrderOrigin.MANUAL,
        intent_key=build_intent_key(
            symbol=PROBE_SYMBOL, side=OrderSide.BUY, purpose="testnet", bucket=bucket
        ),
        created_at=now,
    )
    record = OrderRecord(
        order_id="order-" + uuid.uuid4().hex,
        client_order_id=client_order_id(account_id=intent.account_id, intent_key=intent.intent_key),
        intent=intent,
        state=OrderState.SUBMITTED,
        created_at=now,
        updated_at=now,
        submitted_at=now,
    )

    try:
        placed = await adapter.submit(record)

        # 1. The venue accepted it and echoed OUR identity back.
        assert placed.client_order_id == record.client_order_id
        assert placed.venue_order_id is not None
        assert placed.state is OrderState.ACCEPTED
        assert placed.filled_quantity == Decimal(0)

        # 2. Readable by the identity we derived, not by the venue's id --
        #    which is exactly what recovery after a restart does.
        queried = await adapter.query(symbol=PROBE_SYMBOL, client_order_id=record.client_order_id)
        assert queried is not None
        assert queried.venue_order_id == placed.venue_order_id
        assert queried.state is OrderState.ACCEPTED

        # 3. Visible as an open order.
        listed = await adapter.open_orders(symbol=PROBE_SYMBOL)
        assert any(o.client_order_id == record.client_order_id for o in listed)

        # 4. Cancellation accepted, and the venue reports it cancelled.
        cancelled = await adapter.cancel(
            symbol=PROBE_SYMBOL, client_order_id=record.client_order_id
        )
        assert cancelled.state is OrderState.CANCELLED
        assert cancelled.filled_quantity == Decimal(0)
    finally:
        # Unconditional: a failed assertion above must not leave an order on
        # the book. Already-cancelled is the normal outcome here.
        with contextlib.suppress(ExchangeError):
            await adapter.cancel(symbol=PROBE_SYMBOL, client_order_id=record.client_order_id)

    # 5. Nothing of ours is left behind, and nothing filled.
    remaining = await adapter.open_orders(symbol=PROBE_SYMBOL)
    assert not any(o.client_order_id == record.client_order_id for o in remaining)

    settled = await adapter.query(symbol=PROBE_SYMBOL, client_order_id=record.client_order_id)
    assert settled is not None
    assert settled.state is OrderState.CANCELLED
    assert settled.filled_quantity == Decimal(0)


async def test_no_position_is_left_in_the_probe_symbol(
    adapter: BinanceTestnetTradingAdapter,
) -> None:
    """The test order could not fill. This proves it did not."""
    risk = await adapter._request("GET", "/fapi/v2/positionRisk", {"symbol": PROBE_SYMBOL})
    for entry in risk if isinstance(risk, list) else [risk]:
        if str(entry.get("symbol", "")).upper() == PROBE_SYMBOL:
            amount = Decimal(str(entry.get("positionAmt", "0")))
            assert amount == 0, "a position exists in the probe symbol; an order filled"
