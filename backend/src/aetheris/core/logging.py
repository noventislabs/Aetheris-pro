"""Structured logging (spec section 34).

Logs are events with fields, not sentences. Every record carries a timestamp,
severity, component and -- inside a request -- the request and correlation IDs,
so a trading decision can be traced from HTTP entry through risk evaluation to
order submission.

Secrets are never logged. ``SecretStr`` renders as ``**********`` and raw
credentials must not be placed in event fields in the first place.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def bind_request_context(*, request_id: str, correlation_id: str) -> None:
    _request_id.set(request_id)
    _correlation_id.set(correlation_id)


def clear_request_context() -> None:
    _request_id.set(None)
    _correlation_id.set(None)


def current_request_id() -> str | None:
    return _request_id.get()


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def _inject_request_context(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Attach the ambient request/correlation IDs to every event."""
    if (request_id := _request_id.get()) is not None:
        event_dict.setdefault("request_id", request_id)
    if (correlation_id := _correlation_id.get()) is not None:
        event_dict.setdefault("correlation_id", correlation_id)
    return event_dict


def configure_logging(*, debug: bool = False, level: str = "INFO") -> None:
    """Configure structlog once at process start.

    Development gets the human-readable console renderer; everything else gets
    JSON, because production logs are parsed by machines before people read
    them.
    """
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=False)
        if debug
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_request_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to a component name, e.g. ``risk.engine``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger().bind(component=component)
    return logger
