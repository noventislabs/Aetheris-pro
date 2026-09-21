"""Uniform error responses.

Every failure leaves the API in the same envelope shape, so the frontend has
exactly one error contract to handle and operators have one shape to grep.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from aetheris.core.errors import AetherisError, ErrorBody, ErrorCode, ErrorResponse
from aetheris.core.logging import get_logger

_log = get_logger("api.errors")

_STATUS_TO_CODE = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    501: ErrorCode.NOT_IMPLEMENTED,
    503: ErrorCode.UPSTREAM_UNAVAILABLE,
}


def _safe_errors(errors: object) -> list[dict[str, Any]]:
    """Reduce pydantic error records to JSON-safe, non-echoing fields.

    Two reasons to filter rather than pass through: pydantic puts the original
    exception object in ``ctx``, which no JSON encoder can serialise, and
    ``input`` echoes the caller's value straight back into the response body.
    Type, location and message are what a caller needs to fix the request.
    """
    safe: list[dict[str, Any]] = []
    if not isinstance(errors, list):
        return safe
    for error in errors:
        if not isinstance(error, dict):
            continue
        safe.append(
            {
                "type": str(error.get("type", "")),
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
            }
        )
    return safe


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value is not None else None


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AetherisError)
    async def _handle_aetheris_error(request: Request, exc: AetherisError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response(_request_id(request)).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(
                code=ErrorCode.VALIDATION_FAILED,
                message="Request validation failed",
                details={"errors": _safe_errors(exc.errors())},
                request_id=_request_id(request),
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(PydanticValidationError)
    async def _handle_model_validation_error(
        request: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        """Bad input is a 422, wherever the rejection happened.

        FastAPI converts field-level failures itself, but a model-level
        validator on a dependency-bound parameter model (``macd_fast`` must be
        below ``macd_slow``) raises pydantic's own error, which would otherwise
        surface as a 500 and read as our bug rather than the caller's.
        """
        body = ErrorResponse(
            error=ErrorBody(
                code=ErrorCode.VALIDATION_FAILED,
                message="Request validation failed",
                details={"errors": _safe_errors(exc.errors(include_url=False))},
                request_id=_request_id(request),
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        body = ErrorResponse(
            error=ErrorBody(code=code, message=str(exc.detail), request_id=_request_id(request))
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The message is deliberately generic: internal detail is for the log,
        # not for the client.
        _log.exception("unhandled_exception", path=request.url.path)
        body = ErrorResponse(
            error=ErrorBody(
                code=ErrorCode.INTERNAL_ERROR,
                message="Internal error",
                request_id=_request_id(request),
            )
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))
