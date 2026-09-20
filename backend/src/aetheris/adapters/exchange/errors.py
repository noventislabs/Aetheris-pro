"""Exchange fault vocabulary.

Upstream failures are separated by what the caller should *do* about them, not
by what went wrong technically. Rate limiting means back off; unavailable means
retry later; invalid response means our parser or their contract is wrong and a
human should look. Collapsing these into one "exchange error" would make the
scanner's retry policy unknowable.
"""

from __future__ import annotations

from typing import Any

from aetheris.core.errors import AetherisError, ErrorCode, NotFoundError


class ExchangeError(AetherisError):
    """Base class for upstream exchange faults."""

    code = ErrorCode.UPSTREAM_UNAVAILABLE
    status_code = 502
    message = "Exchange request failed"


class ExchangeTimeoutError(ExchangeError):
    code = ErrorCode.EXCHANGE_TIMEOUT
    status_code = 504
    message = "Exchange request timed out"


class ExchangeRateLimitedError(ExchangeError):
    """The venue asked us to slow down.

    ``retry_after_seconds`` is surfaced so callers can honour the venue's own
    guidance instead of guessing. Ignoring a 429 risks an IP ban, which would
    take market data down for every mode at once.
    """

    code = ErrorCode.EXCHANGE_RATE_LIMITED
    status_code = 429
    message = "Exchange rate limit reached"

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if retry_after_seconds is not None:
            merged["retry_after_seconds"] = retry_after_seconds
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message, details=merged or None)


class ExchangeUnavailableError(ExchangeError):
    code = ErrorCode.EXCHANGE_UNAVAILABLE
    status_code = 503
    message = "Exchange is unavailable"


class ExchangeInvalidResponseError(ExchangeError):
    """The venue answered, but not with something we can trust.

    Raised for malformed JSON, missing required fields, unparseable numbers and
    candles that fail validation. Deliberately *not* retried: repeating a
    request that produced nonsense produces nonsense again, and the safe
    outcome is to report no data rather than to keep hammering.
    """

    code = ErrorCode.EXCHANGE_INVALID_RESPONSE
    status_code = 502
    message = "Exchange returned an unusable response"


class SymbolNotFoundError(NotFoundError):
    """The requested symbol is not in the discovered universe."""

    message = "Symbol not found in the discovered universe"
