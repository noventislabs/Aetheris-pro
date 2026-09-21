"""Connection handling, and the tenant boundary every query passes through.

Three things here are load-bearing.

**The URL never reaches a log, a message or a traceback.** It carries a
password. ``DatabaseUnavailableError`` is raised with a description of what
failed, never with the DSN, and every call site that touches the driver wraps
it -- because an asyncpg exception string can otherwise carry the host.

**A connection is not a tenant.** ``session_scope`` takes an owner and sets a
transaction-local GUC that the row-level security policies read. Sessions are
pooled and reused, so the setting has to be transaction-local (``set_config``
with ``is_local=true``); a session-level ``SET`` would leak one user's identity
into the next request that borrowed the same connection.

**Fail closed.** The policies compare ``owner_id`` against the GUC, and
``current_setting(..., true)`` returns NULL when it was never set. NULL never
equals anything, so a query issued outside ``session_scope`` sees zero rows
rather than everything. That is the behaviour a forgotten ``WHERE`` clause
should have.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aetheris.core.config import Settings

__all__ = [
    "OWNER_GUC",
    "DatabaseUnavailableError",
    "build_engine",
    "build_session_factory",
    "check_connectivity",
    "normalise_dsn",
    "session_scope",
]

#: The setting the RLS policies read. Namespaced so it cannot collide with a
#: PostgreSQL parameter, and named once here so a migration and a query cannot
#: drift apart.
OWNER_GUC: Final = "aetheris.owner_id"

_DRIVER: Final = "postgresql+asyncpg"


class DatabaseUnavailableError(RuntimeError):
    """The database could not be reached or a statement failed.

    Carries a description, never a connection string. ADR 0002 made the
    database a remote service, which makes a dropped connection an operational
    state to report rather than an exception to let escape -- the same
    discipline already applied to venue data.
    """


def normalise_dsn(raw: str) -> tuple[str, dict[str, Any]]:
    """Return a SQLAlchemy URL and the connect arguments that go with it.

    Two translations, both of which exist because the connection string a
    provider hands you is written for libpq and this driver is not libpq:

    - ``postgresql://`` becomes ``postgresql+asyncpg://``.
    - ``sslmode`` is a libpq spelling that asyncpg does not accept as a query
      parameter. It is removed from the URL and applied as ``ssl``, so a string
      copied verbatim from a dashboard works rather than failing on a parameter
      nobody looks at twice.

    A hosted database is reached over the public internet, so TLS is the
    default here even when the URL asks for nothing.

    **On which TLS mode is the default, and what it does not protect against.**
    The default is ``require``: the connection is encrypted, but the server's
    certificate is not verified against a public CA. That is what libpq's
    ``sslmode=require`` means and what every client using a managed provider's
    copy-paste string gets, because these providers terminate TLS with their
    own certificate authority rather than a publicly-trusted one -- verifying
    against the system trust store fails outright. Encryption without
    verification stops passive interception; it does not stop an active
    machine-in-the-middle. Set ``sslmode=verify-full`` in the URL together with
    ``AETHERIS_DATABASE_SSL_ROOT_CERT`` pointing at the provider's CA file to
    close that gap. The weaker default is a deliberate, stated choice rather
    than an accident, and it is never silently downgraded further: ``disable``
    has to be asked for by name.
    """
    parts = urlsplit(raw)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.pop("sslmode", None)

    scheme = parts.scheme.partition("+")[0]
    if scheme != "postgresql":
        raise DatabaseUnavailableError(
            f"expected a postgresql:// URL, got scheme {scheme!r}; "
            "PostgreSQL is required by ADR 0002 and is not substitutable"
        )

    rebuilt = urlunsplit(
        (
            _DRIVER,
            parts.netloc,
            parts.path,
            "&".join(f"{k}={v}" for k, v in query.items()),
            "",
        )
    )
    return rebuilt, {"sslmode": sslmode or "require"}


def _ssl_argument(sslmode: str, root_cert: str | None) -> Any:
    """Translate a libpq ``sslmode`` into what asyncpg wants for ``ssl``."""
    if sslmode == "disable":
        return False
    if sslmode in ("verify-ca", "verify-full"):
        if root_cert is None:
            raise DatabaseUnavailableError(
                f"sslmode={sslmode} requires AETHERIS_DATABASE_SSL_ROOT_CERT to name the "
                "certificate authority to verify against; refusing to fall back to an "
                "unverified connection"
            )
        context = ssl.create_default_context(cafile=root_cert)
        context.check_hostname = sslmode == "verify-full"
        context.verify_mode = ssl.CERT_REQUIRED
        return context
    # prefer / allow / require: encrypted, certificate not verified.
    return "require"


def build_engine(settings: Settings, *, migration: bool = False) -> AsyncEngine:
    """Build the pool for one of the two roles.

    ``migration=True`` selects the schema owner, which exists to run Alembic
    and nothing else. Everything serving a request uses the default, which is
    the restricted role -- the one row-level security actually applies to.
    """
    secret = settings.database_migration_url if migration else settings.database_url
    if secret is None:
        which = "database_migration_url" if migration else "database_url"
        raise DatabaseUnavailableError(f"{which} is not configured")

    url, options = normalise_dsn(secret.get_secret_value())
    connect_args: dict[str, Any] = {
        "ssl": _ssl_argument(str(options["sslmode"]), settings.database_ssl_root_cert),
        "timeout": settings.database_connect_timeout_seconds,
    }
    connect_args["server_settings"] = {
        # Applied per connection so a lock wait cannot sit until the server's
        # own ceiling while holding a row reconciliation needs.
        "statement_timeout": str(int(settings.database_statement_timeout_seconds * 1000)),
        "application_name": "aetheris-migrations" if migration else "aetheris",
    }

    if migration:
        # Migrations are a single short-lived connection. Pooling them only
        # creates connections the runtime pool then cannot have.
        from sqlalchemy.pool import NullPool

        return create_async_engine(url, connect_args=connect_args, poolclass=NullPool)

    return create_async_engine(
        url,
        connect_args=connect_args,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        # A hosted database closes idle connections without telling us. Without
        # pre-ping the first query after that closure fails instead of
        # transparently reconnecting.
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession], owner_id: str
) -> AsyncIterator[AsyncSession]:
    """One transaction, scoped to one owner, committed or rolled back as a unit.

    The GUC is set inside the transaction and dies with it. That is not a
    detail: connections are pooled, so a setting that outlived its transaction
    would hand the next borrower the previous tenant's identity.

    The owner is passed as a bind parameter. ``SET LOCAL`` cannot take one --
    it takes a literal, which would mean interpolating an identifier into SQL
    -- so ``set_config`` is used instead, which can.
    """
    session = factory()
    try:
        async with session.begin():
            await session.execute(
                text("SELECT set_config(:guc, :owner, true)"),
                {"guc": OWNER_GUC, "owner": owner_id},
            )
            yield session
    except IntegrityError:
        # A constraint violation is the database answering the question, not
        # failing to. Idempotency in particular arrives this way: a duplicate
        # client order id is *confirmation the order exists*, and flattening it
        # into "unavailable" would turn a correct refusal into a false outage.
        raise
    except SQLAlchemyError as exc:
        raise DatabaseUnavailableError(f"transaction failed: {type(exc).__name__}") from None
    finally:
        await session.close()


async def check_connectivity(engine: AsyncEngine) -> tuple[bool, str]:
    """Answer whether the database is reachable, without raising.

    Readiness is a report, not a request that may fail. The detail returned
    here is rendered into a health response, so it names the failure class and
    never the connection.
    """
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SHOW server_version"))
            database = await conn.scalar(text("SELECT current_database()"))
        return True, f"Connected to PostgreSQL {version} (database {database})."
    except SQLAlchemyError as exc:
        return False, f"Database unreachable: {type(exc).__name__}."
    except OSError as exc:
        return False, f"Database unreachable: {type(exc).__name__}."
