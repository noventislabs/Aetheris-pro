"""API contract tests for the phase 0 surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aetheris import __version__


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


def test_system_status_reports_spec_risk_defaults(client: TestClient) -> None:
    defaults = client.get("/api/v1/system/status").json()["risk_defaults"]
    assert defaults["paper_starting_balance"] == "100"
    assert defaults["daily_profit_target"] == "20"
    assert defaults["daily_loss_limit"] == "-10"
    assert defaults["max_leverage"] == "3"


def test_capabilities_endpoint_lists_planned_work(client: TestClient) -> None:
    capabilities = client.get("/api/v1/system/capabilities").json()["capabilities"]
    by_key = {c["key"]: c for c in capabilities}
    assert by_key["order.engine"]["status"] == "PLANNED"
    assert by_key["core.money"]["status"] == "AVAILABLE"


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
