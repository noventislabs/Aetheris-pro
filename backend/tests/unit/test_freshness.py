"""An Observation may hold a value or a reason -- never a fabricated stand-in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aetheris.core.freshness import DataStatus, Observation


def test_ok_observation_carries_value_and_source() -> None:
    obs = Observation[str].ok("42000.5", source="binance-futures:rest")
    assert obs.value == "42000.5"
    assert obs.status is DataStatus.OK
    assert obs.status.is_usable_for_trading


def test_unavailable_observation_has_no_value() -> None:
    obs = Observation[str].unavailable(source="binance-futures:rest", detail="endpoint 503")
    assert obs.value is None
    assert obs.detail == "endpoint 503"
    assert not obs.status.is_usable_for_trading


def test_ok_without_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must carry a value"):
        Observation[str](status=DataStatus.OK, source="test")


def test_non_ok_with_value_is_rejected() -> None:
    # This is the fabrication guard: you cannot mark data STALE and still ship
    # a number that a careless caller would read as current.
    with pytest.raises(ValidationError, match="must not carry a value"):
        Observation[str](status=DataStatus.STALE, value="42000", source="test")


def test_age_is_measured_from_the_exchange_timestamp() -> None:
    event = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    obs = Observation[str](
        value="1", source="test", event_ts=event, received_ts=event + timedelta(seconds=3)
    )
    assert obs.age_seconds == 3.0


def test_age_is_unknown_without_an_exchange_timestamp() -> None:
    assert Observation[str].ok("1", source="test").age_seconds is None


def test_observation_is_immutable() -> None:
    obs = Observation[str].ok("1", source="test")
    with pytest.raises(ValidationError):
        obs.value = "2"  # type: ignore[misc]
