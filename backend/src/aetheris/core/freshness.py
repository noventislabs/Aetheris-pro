"""Data provenance and freshness.

Rule 38 of the product spec -- never fabricate market data -- is enforced
structurally rather than by convention. Every market value that reaches the API
travels inside an :class:`Observation`, which can hold a value *or* a reason it
has none, never a filler number standing in for one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataStatus(StrEnum):
    OK = "OK"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"

    @property
    def is_usable_for_trading(self) -> bool:
        """Only fresh data may inform an order decision."""
        return self is DataStatus.OK


def utcnow() -> datetime:
    """Timezone-aware UTC now. The only clock the system reads."""
    return datetime.now(UTC)


class Observation[T](BaseModel):
    """A value observed from an external source, with its provenance attached.

    ``value`` is ``None`` whenever ``status`` is not ``OK``. That invariant is
    enforced below, so a consumer that checks the status cannot then read a
    fabricated number, and a consumer that forgets to check gets ``None``
    rather than a plausible-looking lie.
    """

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    status: DataStatus = DataStatus.OK
    source: str = Field(description="Origin of the datum, e.g. 'venue-adapter:rest'")
    event_ts: datetime | None = Field(
        default=None, description="Exchange-side timestamp of the event, when provided"
    )
    received_ts: datetime = Field(default_factory=utcnow)
    detail: str | None = Field(
        default=None, description="Human-readable reason when status is not OK"
    )

    @model_validator(mode="after")
    def _check_value_matches_status(self) -> Self:
        if self.status is DataStatus.OK and self.value is None:
            raise ValueError("Observation with status OK must carry a value")
        if self.status is not DataStatus.OK and self.value is not None:
            raise ValueError(f"Observation with status {self.status} must not carry a value")
        return self

    @property
    def age_seconds(self) -> float | None:
        """Seconds between the exchange event and our receipt of it."""
        if self.event_ts is None:
            return None
        return (self.received_ts - self.event_ts).total_seconds()

    @classmethod
    def ok(
        cls,
        value: T,
        *,
        source: str,
        event_ts: datetime | None = None,
    ) -> Observation[T]:
        return cls(value=value, status=DataStatus.OK, source=source, event_ts=event_ts)

    @classmethod
    def unavailable(cls, *, source: str, detail: str) -> Observation[T]:
        return cls(status=DataStatus.UNAVAILABLE, source=source, detail=detail)

    @classmethod
    def stale(cls, *, source: str, detail: str, event_ts: datetime | None = None) -> Observation[T]:
        return cls(status=DataStatus.STALE, source=source, detail=detail, event_ts=event_ts)

    @classmethod
    def error(cls, *, source: str, detail: str) -> Observation[T]:
        return cls(status=DataStatus.ERROR, source=source, detail=detail)
