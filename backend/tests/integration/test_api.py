"""API contract tests for the phase 0 surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aetheris import __version__
from aetheris.core.config import Settings, get_settings
from aetheris.main import create_app


def test_liveness(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "version": __version__}


def test_readiness_reports_unbuilt_components_honestly(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    components = {c["name"]: c["status"] for c in body["components"]}
    assert components["api"] == "OK"
    # Nothing here pretends a database or exchange is connected.
    assert components["database"] == "NOT_CONFIGURED"
    assert components["exchange"] == "NOT_CONFIGURED"


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Correlation-ID"] == response.headers["X-Request-ID"]


def test_inbound_correlation_id_is_preserved(client: TestClient) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Correlation-ID": "trace-abc-123"})
    assert response.headers["X-Correlation-ID"] == "trace-abc-123"
    assert response.headers["X-Request-ID"] != "trace-abc-123"


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/api/v1/health/live").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


def test_system_status_exposes_mode_posture(client: TestClient) -> None:
    body = client.get("/api/v1/system/status").json()
    assert body["autonomous_trading_enabled"] is False
    assert body["default_mode"] == "ANALYSIS"
    modes = {m["mode"]: m for m in body["modes"]}
    assert modes["LIVE"]["enabled"] is False
    assert modes["TESTNET"]["enabled"] is False
    assert modes["PAPER"]["enabled"] is True
    assert modes["LIVE"]["risks_real_funds"] is True
    assert modes["PAPER"]["places_real_orders"] is False


def test_system_status_describes_this_app_not_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status endpoint must answer for the app it is mounted on.

    ``get_settings()`` is process-wide and re-reads the environment, so an
    operator with ``AETHERIS_TESTNET_TRADING_ENABLED=true`` in their shell or
    ``.env`` made this route announce TESTNET as enabled -- on an application
    that had been constructed with testnet switched off. The endpoint and the
    application it describes disagreed, which is worse than either answer:
    anything reading it to decide whether execution is available would have
    been told yes by a service that could not execute.

    The environment here is set **hostile on purpose**. It says TESTNET is on;
    the app says otherwise; the app must win.
    """
    monkeypatch.setenv("AETHERIS_TESTNET_TRADING_ENABLED", "true")
    monkeypatch.setenv("AETHERIS_TESTNET_API_KEY", "not-a-real-key")
    monkeypatch.setenv("AETHERIS_TESTNET_API_SECRET", "not-a-real-secret")
    get_settings.cache_clear()

    # Built explicitly with testnet disabled, whatever the environment says.
    app_settings = Settings(
        environment="test",
        debug=False,
        testnet_trading_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert app_settings.testnet_trading_enabled is False

    with TestClient(create_app(app_settings)) as scoped:
        modes = {m["mode"]: m for m in scoped.get("/api/v1/system/status").json()["modes"]}

    assert modes["TESTNET"]["enabled"] is False, (
        "the endpoint reported the process environment rather than the settings "
        "the application was built with"
    )
    # And the environment really was hostile, so the assertion above means
    # something: process-global settings would have said True here.
    assert get_settings().testnet_trading_enabled is True
    get_settings.cache_clear()


def test_system_status_still_reflects_an_app_that_does_enable_a_mode() -> None:
    """The mirror image, so the fix cannot be "always report disabled"."""
    enabled = Settings(
        environment="test",
        debug=False,
        paper_trading_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    with TestClient(create_app(enabled)) as scoped:
        modes = {m["mode"]: m for m in scoped.get("/api/v1/system/status").json()["modes"]}
    assert modes["PAPER"]["enabled"] is False
    assert modes["ANALYSIS"]["enabled"] is True


def test_system_status_reports_spec_risk_defaults(client: TestClient) -> None:
    defaults = client.get("/api/v1/system/status").json()["risk_defaults"]
    assert defaults["paper_starting_balance"] == "100"
    assert defaults["daily_profit_target"] == "20"
    assert defaults["daily_loss_limit"] == "-10"
    assert defaults["max_leverage"] == "3"


def test_capabilities_endpoint_lists_planned_work(client: TestClient) -> None:
    capabilities = client.get("/api/v1/system/capabilities").json()["capabilities"]
    by_key = {c["key"]: c for c in capabilities}
    # PARTIAL since phase 8a: records are durable in PostgreSQL and a recovery
    # pass runs at startup. Still not AVAILABLE -- the retry columns the venue
    # path needs have no writer.
    assert by_key["order.persistence"]["status"] == "PARTIAL"
    # PARTIAL since phase 8b: signed testnet execution exists. Still not
    # AVAILABLE, and execution.live is still PLANNED and unimplemented.
    assert by_key["execution.testnet"]["status"] == "PARTIAL"
    assert by_key["execution.live"]["status"] == "PLANNED"
    assert by_key["core.money"]["status"] == "AVAILABLE"

    # The database served PLANNED with "No database is configured in this
    # build" for two commits after migrations 0001-0004 were running. Asserted
    # here as well as in the unit tests because this is the copy clients read.
    database = by_key["persistence.database"]
    assert database["status"] == "PARTIAL"
    assert "No database is configured" not in database["detail"]
    assert "PostgreSQL" in database["detail"]
    assert "paper account state" in database["detail"]

    # Same drift, other direction: the port had an implementation while the
    # registry still told clients it had none.
    abstraction = by_key["exchange.abstraction"]
    assert "implemented by nothing" not in abstraction["detail"]
    assert "BinanceTestnetTradingAdapter" in abstraction["detail"]
    assert "NO adapter reports LIVE" in abstraction["detail"]

    # Whatever else moves, nothing may announce phase 9 or a live venue.
    assert by_key["ai.analysis"]["status"] == "PLANNED"
    assert by_key["falcon.command_center"]["status"] == "PLANNED"


def test_unknown_route_returns_the_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"]


def test_openapi_document_builds(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Aetheris Pro"
