"""Alembic environment.

Migrations run as the **schema owner**, never as the runtime role. That is the
whole point of the two-role design: the privilege needed to create a table is
not also present while serving a request. The owner is read from
``AETHERIS_DATABASE_MIGRATION_URL``; if only the runtime URL is configured this
refuses to run rather than quietly migrating as whoever it can log in as.

The URL is never written to the config object Alembic logs, and never appears
in an error raised from here.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from aetheris.adapters.persistence.engine import build_engine
from aetheris.adapters.persistence.models import Base
from aetheris.core.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _settings() -> Settings:
    settings = Settings()
    if settings.database_migration_url is None:
        raise SystemExit(
            "AETHERIS_DATABASE_MIGRATION_URL is not set. Migrations run as the schema "
            "owner; the runtime role deliberately cannot create tables. Set the "
            "migration URL in backend/.env and try again."
        )
    return settings


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Refused.

    Offline mode emits SQL against a URL it never connects to, which would mean
    putting the connection string on a command line -- where it lands in shell
    history and process listings. The migrations here also need to inspect the
    server (the runtime role must exist before privileges are granted to it),
    which offline mode cannot do.
    """
    raise SystemExit(
        "Offline migrations are not supported: the connection string must not appear "
        "on a command line, and these migrations inspect the server."
    )


async def _run_async() -> None:
    engine = build_engine(_settings(), migration=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(lambda sync_conn: _configure(sync_conn))
            await connection.run_sync(lambda _: context.run_migrations())
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
