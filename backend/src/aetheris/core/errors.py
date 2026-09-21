"""Machine-readable error and rejection vocabulary.

Every refusal the system issues carries a stable code. Operators grep for these
in audit logs, the frontend switches on them, and tests assert on them, so they
are part of the public contract -- add codes freely, never repurpose one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class RiskRejectionCode(StrEnum):
    """Reasons the risk engine may refuse a proposed order.

    The risk engine has final authority (spec section 5): strategies, AI,
    Falcon, the frontend and API clients can all propose, and any of them can
    be refused with one of these.
    """

    DAILY_LOSS_LIMIT = "RISK_REJECTED_DAILY_LOSS_LIMIT"
    DAILY_PROFIT_TARGET = "RISK_REJECTED_DAILY_PROFIT_TARGET"
    MAX_LEVERAGE = "RISK_REJECTED_MAX_LEVERAGE"
    POSITION_SIZE = "RISK_REJECTED_POSITION_SIZE"
    SYMBOL_LIMIT = "RISK_REJECTED_SYMBOL_LIMIT"
    STRATEGY_LIMIT = "RISK_REJECTED_STRATEGY_LIMIT"
    MAX_OPEN_POSITIONS = "RISK_REJECTED_MAX_OPEN_POSITIONS"
    PORTFOLIO_EXPOSURE = "RISK_REJECTED_PORTFOLIO_EXPOSURE"
    STALE_DATA = "RISK_REJECTED_STALE_DATA"
    #: Enough real history to measure volatility does not exist yet -- a
    #: newly listed instrument, typically. Distinct from STALE_DATA because
    #: the price may be perfectly fresh; what is missing is the past, and no
    #: amount of waiting for a newer tick supplies it.
    INSUFFICIENT_HISTORY = "RISK_REJECTED_INSUFFICIENT_HISTORY"
    ABNORMAL_VOLATILITY = "RISK_REJECTED_ABNORMAL_VOLATILITY"
    INSUFFICIENT_BALANCE = "RISK_REJECTED_INSUFFICIENT_BALANCE"
    MIN_NOTIONAL = "RISK_REJECTED_MIN_NOTIONAL"
    EXCHANGE_PRECISION = "RISK_REJECTED_EXCHANGE_PRECISION"
    COOLDOWN = "RISK_REJECTED_COOLDOWN"
    EMERGENCY_STOP = "RISK_REJECTED_EMERGENCY_STOP"
    MODE_NOT_ENABLED = "RISK_REJECTED_MODE_NOT_ENABLED"
    RECONCILIATION_PENDING = "RISK_REJECTED_RECONCILIATION_PENDING"


class ErrorCode(StrEnum):
    """Transport-level error codes returned by the API."""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # Exchange connectivity faults. Distinct codes because the caller's
    # correct response differs: back off, retry later, or report a bug.
    EXCHANGE_RATE_LIMITED = "EXCHANGE_RATE_LIMITED"
    EXCHANGE_UNAVAILABLE = "EXCHANGE_UNAVAILABLE"
    EXCHANGE_TIMEOUT = "EXCHANGE_TIMEOUT"
    EXCHANGE_INVALID_RESPONSE = "EXCHANGE_INVALID_RESPONSE"


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The single error envelope every failing endpoint returns."""

    error: ErrorBody


class AetherisError(Exception):
    """Base class for errors that map onto an HTTP response.

    Carrying the status code on the exception keeps the mapping next to the
    error definition instead of in a translation table that drifts.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status_code: int = 500
    message: str = "Internal error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)

    def to_response(self, request_id: str | None = None) -> ErrorResponse:
        return ErrorResponse(
            error=ErrorBody(
                code=self.code,
                message=self.message,
                details=self.details,
                request_id=request_id,
            )
        )


class NotFoundError(AetherisError):
    code = ErrorCode.NOT_FOUND
    status_code = 404
    message = "Resource not found"


class ValidationFailedError(AetherisError):
    code = ErrorCode.VALIDATION_FAILED
    status_code = 422
    message = "Request validation failed"


class UnauthenticatedError(AetherisError):
    code = ErrorCode.UNAUTHENTICATED
    status_code = 401
    message = "Authentication required"


class ForbiddenError(AetherisError):
    code = ErrorCode.FORBIDDEN
    status_code = 403
    message = "Not permitted"


class NotImplementedYetError(AetherisError):
    """Explicit 'this is planned, not built' -- never a silent fake response.

    Spec section 38: unimplemented capability must say so rather than return a
    plausible placeholder.
    """

    code = ErrorCode.NOT_IMPLEMENTED
    status_code = 501
    message = "Capability is not implemented yet"


class UpstreamUnavailableError(AetherisError):
    code = ErrorCode.UPSTREAM_UNAVAILABLE
    status_code = 503
    message = "Upstream service unavailable"
