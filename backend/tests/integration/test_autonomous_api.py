"""HTTP contract for the autonomous endpoints.

The headline assertion is the boring one: **arriving at a fresh application,
autonomy is off**. Everything else here is about making sure that stays true
and that the loop's state is reported honestly, including when it is doing
nothing.
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


def per_symbol(build: Callable[[str], dict[str, object]]) -> RouteHandler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=build(request.url.params.get("symbol", "BTCUSDT")))

    return handler


def routes(price: str = "100.00") -> RoutingHandler:
    spread = Decimal(price) * Decimal("0.0001")
    return RoutingHandler(
        {
            endpoints.EXCHANGE_INFO: json_route(
                payloads.exchange_info(
                    [
                        payloads.symbol_entry(symbol=s, base_asset=s[:-4], min_notional="5")
                        for s in UNIVERSE
                    ]
                )
            ),
            endpoints.TICKER_24H: per_symbol(
                lambda s: payloads.ticker_24h(symbol=s, last_price=price)
            ),
            endpoints.BOOK_TICKER: per_symbol(
                lambda s: payloads.book_ticker(
                    symbol=s,
                    bid=str(Decimal(price) - spread),
                    ask=str(Decimal(price) + spread),
                )
            ),
            endpoints.KLINES: json_route(payloads.klines(count=200, interval_seconds=900)),
        }
    )


def make_client(handler: RoutingHandler, **overrides: object) -> Iterator[TestClient]:
    settings = Settings(environment="test", _env_file=None, **overrides)  # type: ignore[call-arg]
    settings.binance.max_retries = 0
    settings.binance.backoff_seconds = 0.0
    with TestClient(create_app(settings, exchange_transport=handler.transport())) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from make_client(routes(), autonomous_trading_enabled=True)


@pytest.fixture
def forbidden_client() -> Iterator[TestClient]:
    yield from make_client(routes(), autonomous_trading_enabled=False)


# ----------------------------------------------------------------------
# Off by default
# ----------------------------------------------------------------------


def test_autonomy_is_off_on_a_fresh_application(client: TestClient) -> None:
    """The single most important assertion in this file."""
    body = client.get("/api/v1/paper/autonomous").json()
    assert body["enabled"] is False
    assert body["state"] == "DISARMED"


def test_configuration_alone_does_not_arm_it(client: TestClient) -> None:
    body = client.get("/api/v1/paper/autonomous").json()
    assert body["permitted_by_config"] is True, "the flag is set"
    assert body["enabled"] is False, "and it is still off"


def test_the_default_deployment_cannot_arm_at_all(forbidden_client: TestClient) -> None:
    status = forbidden_client.get("/api/v1/paper/autonomous").json()
    assert status["state"] == "DISABLED_BY_CONFIG"
    response = forbidden_client.post("/api/v1/paper/autonomous", json={"enabled": True})
    assert response.status_code >= 400
    assert forbidden_client.get("/api/v1/paper/autonomous").json()["enabled"] is False


def test_the_universe_is_empty_unless_configured(client: TestClient) -> None:
    """It never picks instruments on its own."""
    assert client.get("/api/v1/paper/autonomous").json()["symbols"] == []


def test_arming_and_disarming_round_trip(client: TestClient) -> None:
    armed = client.post("/api/v1/paper/autonomous", json={"enabled": True}).json()
    assert armed["enabled"] is True
    assert armed["state"] == "ARMED"

    disarmed = client.post("/api/v1/paper/autonomous", json={"enabled": False}).json()
    assert disarmed["enabled"] is False
    assert disarmed["state"] == "DISARMED"


def test_a_new_application_starts_disarmed_however_the_last_one_was_left(
    client: TestClient,
) -> None:
    """Restart semantics, asserted at the HTTP surface.

    A process that crashed and came back trading unattended, against an account
    it does not remember, is the worst outcome available here.
    """
    client.post("/api/v1/paper/autonomous", json={"enabled": True})
    assert client.get("/api/v1/paper/autonomous").json()["enabled"] is True

    for fresh in make_client(routes(), autonomous_trading_enabled=True):
        assert fresh.get("/api/v1/paper/autonomous").json()["enabled"] is False


# ----------------------------------------------------------------------
# Disclosure
# ----------------------------------------------------------------------


def test_the_status_says_it_is_a_simulation(client: TestClient) -> None:
    disclaimer = client.get("/api/v1/paper/autonomous").json()["disclaimer"].lower()
    assert "no order is sent to any exchange" in disclaimer
    assert "no api credential exists" in disclaimer
    assert "final authority" in disclaimer


def test_the_decision_log_is_empty_and_says_so(client: TestClient) -> None:
    body = client.get("/api/v1/paper/autonomous/decisions").json()
    assert body["count"] == 0
    assert body["decisions"] == []
    assert "lost on restart" in body["detail"]


def test_capabilities_report_the_loop_and_the_authority(client: TestClient) -> None:
    by_key = {c["key"]: c for c in client.get("/api/v1/system/capabilities").json()["capabilities"]}
    assert by_key["paper.autonomous"]["status"] == "AVAILABLE"
    assert "OFF by default" in by_key["paper.autonomous"]["detail"]
    assert by_key["risk.engine"]["status"] == "AVAILABLE"
    # Building the authority did not unlock leverage, and must not claim to.
    assert "above 1x still fails closed" in by_key["risk.engine"]["detail"]
    assert by_key["execution.testnet"]["status"] == "PLANNED"
    assert by_key["execution.live"]["status"] == "PLANNED"


# ----------------------------------------------------------------------
# The route surface is unchanged in shape
# ----------------------------------------------------------------------


def test_the_autonomous_routes_do_not_widen_the_write_surface(client: TestClient) -> None:
    """ADR 0003's invariant needs no amendment for phase 7."""
    assert_route_surface(client)


def test_no_autonomous_endpoint_names_a_credential_surface(client: TestClient) -> None:
    schema = client.get("/openapi.json").text.lower()
    for token in ("api_key", "apikey", "api-secret", "withdraw", "x-mbx-apikey"):
        assert token not in schema


@pytest.mark.parametrize("verb", ["put", "patch", "delete"])
def test_unsupported_verbs_are_refused(client: TestClient, verb: str) -> None:
    assert getattr(client, verb)("/api/v1/paper/autonomous").status_code == 405


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-field"),
        pytest.param({"enabled": "yes please"}, id="wrong-type"),
        pytest.param({"enabled": True, "extra": 1}, id="unknown-field"),
    ],
)
def test_malformed_arm_requests_are_refused_at_the_boundary(client: TestClient, body: dict) -> None:
    assert client.post("/api/v1/paper/autonomous", json=body).status_code == 422


def test_the_decision_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/api/v1/paper/autonomous/decisions?limit=0").status_code == 422
    assert client.get("/api/v1/paper/autonomous/decisions?limit=9999").status_code == 422
    assert client.get("/api/v1/paper/autonomous/decisions?limit=10").status_code == 200
