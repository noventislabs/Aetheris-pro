"""HTTP contract for the paper trading endpoints.

These are the first write routes in the system, so a fair amount of what is
asserted here is about the *shape* of that change: writes exist only under
``/paper``, every refusal comes back as a structured result rather than an
error page, and every response keeps saying what paper trading is and is not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.invariants import assert_route_surface
from tests.fixtures.transport import RouteHandler, RoutingHandler, json_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.core.config import Settings
from aetheris.main import create_app

UNIVERSE = ["BTCUSDT", "ETHUSDT"]


def exchange_info(min_notional: str = "5") -> dict[str, object]:
    """A universe whose minimum notional a 100 USDT account can actually meet."""
    return payloads.exchange_info(
        [
            payloads.symbol_entry(symbol="BTCUSDT", base_asset="BTC", min_notional=min_notional),
            payloads.symbol_entry(symbol="ETHUSDT", base_asset="ETH", min_notional=min_notional),
        ]
    )


def make_client(handler: RoutingHandler, **overrides: object) -> Iterator[TestClient]:
    settings = Settings(environment="test", _env_file=None, **overrides)  # type: ignore[call-arg]
    settings.binance.max_retries = 0
    settings.binance.backoff_seconds = 0.0
    # The adapter caches tickers for a few seconds, which is right in
    # production and wrong here: these tests move the market between calls and
    # need the engine to see the move.
    settings.binance.ticker_ttl_seconds = 0.001
    with TestClient(create_app(settings, exchange_transport=handler.transport())) as client:
        yield client


def per_symbol(build: Callable[[str], dict[str, object]]) -> RouteHandler:
    """Serve the single-symbol shape the adapter asks for.

    The paper engine fetches one symbol at a time, and Binance answers a
    symbol-scoped request with an object rather than the whole-market array.
    Reflecting that here is what keeps these tests exercising the real parsing
    path instead of a shape the venue never sends.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params.get("symbol", "BTCUSDT")
        return httpx.Response(200, json=build(symbol))

    return handler


class PriceBook:
    """A market a test can move.

    The engine only ever sees prices it was given, so moving the market is the
    whole of what it takes to drive a stop or a target -- no clock to advance
    and no time to wait.
    """

    def __init__(self, price: str = "100.00", age_seconds: float = 1.0) -> None:
        self.price = Decimal(price)
        self.age_seconds = age_seconds

    def move(self, price: str) -> None:
        self.price = Decimal(price)

    @property
    def bid(self) -> Decimal:
        return self.price * Decimal("0.9999")

    @property
    def ask(self) -> Decimal:
        return self.price * Decimal("1.0001")


def routes(book: PriceBook | None = None, *, min_notional: str = "5") -> RoutingHandler:
    market = book or PriceBook()
    return RoutingHandler(
        {
            endpoints.EXCHANGE_INFO: json_route(exchange_info(min_notional)),
            endpoints.TICKER_24H: per_symbol(
                lambda symbol: payloads.ticker_24h(
                    symbol=symbol,
                    last_price=str(market.price),
                    age_seconds=market.age_seconds,
                )
            ),
            endpoints.BOOK_TICKER: per_symbol(
                lambda symbol: payloads.book_ticker(
                    symbol=symbol, bid=str(market.bid), ask=str(market.ask)
                )
            ),
            # ADR 0006: every manual order now measures volatility from real
            # candles before the risk engine will rule on it. Sixty bars, which
            # is what the service asks for.
            endpoints.KLINES: json_route(payloads.klines(count=60, interval_seconds=900)),
        }
    )


@pytest.fixture
def book() -> PriceBook:
    return PriceBook()


@pytest.fixture
def handler(book: PriceBook) -> RoutingHandler:
    return routes(book)


@pytest.fixture
def client(handler: RoutingHandler) -> Iterator[TestClient]:
    yield from make_client(handler)


def submit(client: TestClient, **body: object) -> dict:
    payload: dict[str, object] = {"symbol": "BTCUSDT", "side": "BUY", "margin": "20"}
    payload.update(body)
    response = client.post("/api/v1/paper/orders", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------------
# Disclosure
# ----------------------------------------------------------------------


def test_method_publishes_the_fill_model_and_its_gaps(client: TestClient) -> None:
    body = client.get("/api/v1/paper/method").json()
    assert body["label"] == "PAPER / SIMULATION ONLY / NO REAL ORDER"
    joined = " ".join(body["assumptions"]).lower()
    assert "simulation only" in joined
    assert "no order is sent to any exchange" in joined
    gaps = " ".join(body["not_modelled"]).lower()
    assert "funding" in gaps
    assert "partial fills" in gaps
    assert "poll-driven" in gaps


def test_method_distinguishes_paper_from_testnet_and_live(client: TestClient) -> None:
    """The three are routinely conflated, and only one of them is harmless."""
    disclaimer = client.get("/api/v1/paper/method").json()["disclaimer"].lower()
    assert "no order is sent to any exchange" in disclaimer
    assert "not testnet trading" in disclaimer
    assert "not live trading" in disclaimer


def test_method_publishes_the_refusal_vocabulary(client: TestClient) -> None:
    codes = client.get("/api/v1/paper/method").json()["rejection_codes"]
    assert "RISK_REJECTED_DAILY_LOSS_LIMIT" in codes
    assert "RISK_REJECTED_STALE_DATA" in codes
    assert all(code.startswith("RISK_REJECTED_") for code in codes)


def test_the_account_carries_its_durability_warning(client: TestClient) -> None:
    body = client.get("/api/v1/paper/account").json()
    assert body["durability"] == "IN_MEMORY"
    assert "RESETS ON RESTART" in body["durability_notice"]
    assert body["label"] == "PAPER / SIMULATION ONLY / NO REAL ORDER"
    assert body["mode"] == "PAPER"


def test_capabilities_report_paper_as_built_and_execution_as_not(client: TestClient) -> None:
    by_key = {c["key"]: c for c in client.get("/api/v1/system/capabilities").json()["capabilities"]}
    assert by_key["paper.engine"]["status"] == "AVAILABLE"
    assert "NO real order" in by_key["paper.engine"]["detail"]
    assert by_key["paper.persistence"]["status"] == "PARTIAL"
    # Phase 7 built the loop and the authority that refuses it; neither
    # reaches a venue, and real execution is still unclaimed.
    assert by_key["paper.autonomous"]["status"] == "AVAILABLE"
    assert by_key["risk.engine"]["status"] == "AVAILABLE"
    assert by_key["execution.testnet"]["status"] == "PLANNED"
    assert by_key["execution.live"]["status"] == "PLANNED"
    # PARTIAL since phase 8a: records are durable in PostgreSQL and a recovery
    # pass runs at startup. Still not AVAILABLE -- nothing submits anywhere.
    assert by_key["order.persistence"]["status"] == "PARTIAL"
    assert by_key["execution.testnet"]["status"] == "PLANNED"


# ----------------------------------------------------------------------
# The route surface
# ----------------------------------------------------------------------


def test_writes_exist_only_under_the_paper_namespace(client: TestClient) -> None:
    assert_route_surface(client)


def test_no_endpoint_names_a_credential_or_withdrawal_surface(client: TestClient) -> None:
    schema = client.get("/openapi.json").text.lower()
    for token in ("api_key", "apikey", "api-secret", "withdraw", "x-mbx-apikey"):
        assert token not in schema, f"the published schema mentions {token}"


@pytest.mark.parametrize("verb", ["put", "patch", "delete"])
def test_unsupported_verbs_are_refused(client: TestClient, verb: str) -> None:
    response = getattr(client, verb)("/api/v1/paper/account")
    assert response.status_code == 405


# ----------------------------------------------------------------------
# The full pipeline over HTTP
# ----------------------------------------------------------------------


def test_a_paper_order_opens_a_position(client: TestClient) -> None:
    body = submit(client)
    assert body["accepted"] is True
    assert body["order"]["state"] == "FILLED"
    assert body["position"]["symbol"] == "BTCUSDT"
    assert body["account"]["margin_used"] != "0"
    assert "Simulation only" in body["detail"]


def test_the_fill_records_the_price_source_it_used(client: TestClient) -> None:
    fill = submit(client)["order"]["fills"][0]
    assert fill["price_source"].startswith("binance-futures-usdm:rest")
    assert fill["price_age_seconds"] is not None


def test_a_position_opened_then_closed_reconciles_the_balance(client: TestClient) -> None:
    submit(client)
    closed = client.post("/api/v1/paper/positions/BTCUSDT/close").json()
    assert closed["accepted"] is True
    assert closed["trade"]["exit_reason"] == "MANUAL_CLOSE"
    account = closed["account"]
    assert account["positions"] == []
    assert Decimal(account["balance"]) == Decimal(account["starting_balance"]) + Decimal(
        account["realized_pnl"]
    )


def test_a_tick_closes_a_position_that_reached_its_target(
    client: TestClient, book: PriceBook
) -> None:
    submit(client, take_profit_percent="2")
    assert client.post("/api/v1/paper/tick").json()["closed_trades"] == []

    book.move("120.00")
    body = client.post("/api/v1/paper/tick").json()
    assert len(body["closed_trades"]) == 1
    assert body["closed_trades"][0]["exit_reason"] == "TAKE_PROFIT"
    assert Decimal(body["closed_trades"][0]["net_pnl"]) > 0
    assert "1 position(s) closed" in body["detail"]


def test_a_tick_closes_a_position_that_hit_its_stop(client: TestClient, book: PriceBook) -> None:
    submit(client, stop_loss_percent="2")
    book.move("90.00")
    body = client.post("/api/v1/paper/tick").json()
    assert body["closed_trades"][0]["exit_reason"] == "STOP_LOSS"
    assert Decimal(body["closed_trades"][0]["net_pnl"]) < 0


def test_a_tick_with_nothing_to_do_says_so(client: TestClient) -> None:
    body = client.post("/api/v1/paper/tick").json()
    assert body["closed_trades"] == []
    assert "No position reached" in body["detail"]


def test_reading_the_account_never_closes_a_position(client: TestClient) -> None:
    """A browser refreshing a tab must not be able to realise a loss."""
    submit(client, stop_loss_percent="0.0001", take_profit_percent="0.0001")
    for _ in range(3):
        account = client.get("/api/v1/paper/account").json()
        assert len(account["positions"]) == 1


# ----------------------------------------------------------------------
# Refusals come back as structured results
# ----------------------------------------------------------------------


def test_leverage_above_the_domain_minimum_is_refused_with_its_reason(
    client: TestClient,
) -> None:
    body = submit(client, leverage="10")
    assert body["accepted"] is False
    assert body["order"]["rejection_code"] == "RISK_REJECTED_MAX_LEVERAGE"
    chain = body["order"]["leverage"]
    assert chain["requested_leverage"] == "10"
    assert chain["approved_leverage"] is None
    assert chain["exchange_max_leverage"] is None, "never guessed from the 1-500 range"
    assert chain["reason"] == "RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN"


def test_one_times_is_approved_at_the_domain_minimum(client: TestClient) -> None:
    chain = submit(client, leverage="1")["order"]["leverage"]
    assert chain["approved_leverage"] == "1"
    assert chain["reason"] == "LEVERAGE_APPROVED_AT_DOMAIN_MINIMUM"
    assert "unlevered" in chain["detail"]


def test_an_unlisted_symbol_is_refused(client: TestClient) -> None:
    body = submit(client, symbol="NOTREALUSDT")
    assert body["accepted"] is False
    assert body["order"]["rejection_code"] == "RISK_REJECTED_STALE_DATA"


def test_a_notional_below_the_venue_minimum_is_refused() -> None:
    """Binance publishes a 100 USDT minimum for some perpetuals.

    A 100 USDT paper account at 1x cannot meet that once the fee is paid. The
    engine refuses and says why rather than opening a position the venue would
    have rejected.
    """
    handler = routes(min_notional="100")
    for client in make_client(handler):
        body = submit(client, margin="20")
        assert body["accepted"] is False
        assert body["order"]["rejection_code"] == "RISK_REJECTED_MIN_NOTIONAL"
        assert "100" in body["detail"]


def test_a_refused_order_still_appears_in_the_account_history(client: TestClient) -> None:
    submit(client, leverage="10")
    account = client.get("/api/v1/paper/account").json()
    assert account["recent_orders"][0]["state"] == "REJECTED"
    assert account["recent_orders"][0]["rejection_code"] == "RISK_REJECTED_MAX_LEVERAGE"
    assert account["session"]["orders_rejected"] == 1


def test_a_second_position_in_the_same_symbol_is_refused(client: TestClient) -> None:
    submit(client)
    body = submit(client)
    assert body["order"]["rejection_code"] == "RISK_REJECTED_SYMBOL_LIMIT"


def test_closing_nothing_is_refused_rather_than_erroring(client: TestClient) -> None:
    response = client.post("/api/v1/paper/positions/BTCUSDT/close")
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_market_data_failure_refuses_the_order_rather_than_inventing_a_price() -> None:
    """The venue is unreachable, so there is no price -- and none is invented."""
    handler = routes()
    handler.routes[endpoints.TICKER_24H] = json_route({"code": -1121}, status_code=400)
    handler.routes[endpoints.BOOK_TICKER] = json_route({"code": -1121}, status_code=400)
    for client in make_client(handler):
        body = submit(client)
        assert body["accepted"] is False
        assert body["order"]["rejection_code"] == "RISK_REJECTED_STALE_DATA"
        assert body["account"]["positions"] == []


def test_stale_market_data_refuses_the_order() -> None:
    handler = routes(PriceBook(age_seconds=6000.0))
    for client in make_client(handler):
        body = submit(client)
        assert body["accepted"] is False
        assert body["order"]["rejection_code"] == "RISK_REJECTED_STALE_DATA"


def test_paper_disabled_refuses_every_order() -> None:
    handler = routes()
    for client in make_client(handler, paper_trading_enabled=False):
        body = submit(client)
        assert body["accepted"] is False
        assert body["order"]["rejection_code"] == "RISK_REJECTED_MODE_NOT_ENABLED"


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"leverage": "501"}, id="leverage-above-domain"),
        pytest.param({"leverage": "0"}, id="leverage-below-domain"),
        pytest.param({"margin": "-5"}, id="negative-margin"),
        pytest.param({"side": "SIDEWAYS"}, id="unknown-side"),
        pytest.param({"symbol": "../../etc/passwd"}, id="path-traversal"),
        pytest.param({"stop_loss_percent": "150"}, id="impossible-stop"),
        pytest.param({"unexpected": "field"}, id="unknown-field"),
    ],
)
def test_malformed_requests_are_refused_at_the_boundary(client: TestClient, body: dict) -> None:
    response = client.post(
        "/api/v1/paper/orders", json={"symbol": "BTCUSDT", "side": "BUY", "margin": "20", **body}
    )
    assert response.status_code == 422


def test_an_order_with_no_size_is_refused(client: TestClient) -> None:
    response = client.post("/api/v1/paper/orders", json={"symbol": "BTCUSDT", "side": "BUY"})
    assert response.status_code == 200
    assert response.json()["order"]["rejection_code"] == "RISK_REJECTED_POSITION_SIZE"


# ----------------------------------------------------------------------
# Idempotency, reset, emergency stop, reconciliation
# ----------------------------------------------------------------------


def test_resubmitting_a_client_order_id_does_not_open_a_second_position(
    client: TestClient,
) -> None:
    first = submit(client, client_order_id="retry-1")
    second = submit(client, client_order_id="retry-1")
    assert second["order"]["idempotent_replay"] is True
    assert second["order"]["order_id"] == first["order"]["order_id"]
    assert len(second["account"]["positions"]) == 1


def test_the_emergency_stop_blocks_entries_without_closing_anything(
    client: TestClient,
) -> None:
    submit(client)
    stopped = client.post(
        "/api/v1/paper/emergency-stop", json={"engaged": True, "reason": "Operator halt"}
    ).json()
    assert len(stopped["positions"]) == 1

    refused = submit(client, symbol="ETHUSDT")
    assert refused["order"]["rejection_code"] == "RISK_REJECTED_EMERGENCY_STOP"

    client.post("/api/v1/paper/emergency-stop", json={"engaged": False})
    assert submit(client, symbol="ETHUSDT")["accepted"] is True


def test_reset_clears_the_account(client: TestClient) -> None:
    submit(client)
    body = client.post("/api/v1/paper/reset", json={}).json()
    assert body["positions"] == []
    assert body["balance"] == body["starting_balance"]
    assert body["realized_pnl"] == "0"


def test_reset_accepts_a_new_starting_balance(client: TestClient) -> None:
    body = client.post("/api/v1/paper/reset", json={"starting_balance": "500"}).json()
    assert body["starting_balance"] == "500"


def test_reconciliation_reports_not_applicable(client: TestClient) -> None:
    body = client.get("/api/v1/paper/reconciliation").json()
    assert body["status"] == "NOT_APPLICABLE"
    assert "no external authority" in body["detail"].lower()


def test_autonomous_trading_is_reported_off(client: TestClient) -> None:
    assert client.get("/api/v1/paper/account").json()["autonomous_enabled"] is False
