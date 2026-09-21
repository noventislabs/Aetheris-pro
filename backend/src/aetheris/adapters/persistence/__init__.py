"""PostgreSQL persistence.

This package is the only place in the codebase that knows a database exists.
The ports it satisfies live in ``aetheris.engines`` and name no vendor, which
is what the architecture tests enforce: ``aetheris.adapters`` is forbidden to
the pure layers, so persistence reaches them through an ABC or not at all.
"""

from __future__ import annotations

from aetheris.adapters.persistence.engine import (
    OWNER_GUC,
    DatabaseUnavailableError,
    build_engine,
    check_connectivity,
    normalise_dsn,
    session_scope,
)

__all__ = [
    "OWNER_GUC",
    "DatabaseUnavailableError",
    "build_engine",
    "check_connectivity",
    "normalise_dsn",
    "session_scope",
]
