"""Logging configuration must actually reach the loggers that use it.

These tests exist because of a real defect: ``get_logger`` used to call
``.bind()``, which resolves structlog's configuration immediately. Module-level
loggers are created at import time -- before ``configure_logging`` runs -- so
they captured structlog's defaults permanently and silently lost the JSON
renderer, UTC timestamps, level filtering and request-context injection. The
symptom was local-time timestamps and unfiltered debug output in production
paths.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import structlog

from aetheris.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def _isolate_structlog() -> Iterator[None]:
    """structlog configuration is process-global; keep it from leaking."""
    structlog.reset_defaults()
    clear_request_context()
    yield
    structlog.reset_defaults()
    clear_request_context()


def read_json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr().out.strip()
    assert captured, "expected a log line on stdout"
    line: dict[str, object] = json.loads(captured.splitlines()[-1])
    return line


def test_logger_created_before_configuration_still_honours_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The regression guard: import-time loggers must not freeze the defaults."""
    logger = get_logger("probe")  # as a module-level logger would be
    configure_logging(debug=False, level="INFO")

    logger.info("configured_event", detail="value")

    payload = read_json_line(capsys)
    assert payload["event"] == "configured_event"
    assert payload["component"] == "probe"
    assert payload["detail"] == "value"


def test_timestamps_are_utc(capsys: pytest.CaptureFixture[str]) -> None:
    logger = get_logger("probe")
    configure_logging(debug=False, level="INFO")
    logger.info("timestamped")

    timestamp = read_json_line(capsys)["timestamp"]
    assert isinstance(timestamp, str)
    # A 'Z' suffix is the whole point: local time in a trading log makes two
    # venues' timestamps incomparable.
    assert timestamp.endswith("Z")


def test_level_filter_is_applied(capsys: pytest.CaptureFixture[str]) -> None:
    logger = get_logger("probe")
    configure_logging(debug=False, level="INFO")

    logger.debug("should_be_filtered")
    assert capsys.readouterr().out.strip() == ""

    logger.warning("should_appear")
    assert "should_appear" in capsys.readouterr().out


def test_request_context_is_injected(capsys: pytest.CaptureFixture[str]) -> None:
    logger = get_logger("probe")
    configure_logging(debug=False, level="INFO")

    bind_request_context(request_id="req-1", correlation_id="corr-1")
    logger.info("in_request")

    payload = read_json_line(capsys)
    assert payload["request_id"] == "req-1"
    assert payload["correlation_id"] == "corr-1"


def test_no_request_context_outside_a_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = get_logger("probe")
    configure_logging(debug=False, level="INFO")

    logger.info("background_event")

    payload = read_json_line(capsys)
    assert "request_id" not in payload
    assert "correlation_id" not in payload


def test_secrets_are_not_rendered_by_accident(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SecretStr must never print its value, even if someone logs it."""
    from pydantic import SecretStr

    logger = get_logger("probe")
    configure_logging(debug=False, level="INFO")
    logger.info("credential_event", api_key=SecretStr("super-secret-value"))

    output = capsys.readouterr().out
    assert "super-secret-value" not in output
