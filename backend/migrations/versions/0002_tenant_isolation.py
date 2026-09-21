"""Tenant isolation: row-level security, forced, plus the runtime role's grants.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21

This migration refuses to run rather than produce isolation that only looks
like isolation. Two preconditions are checked against the live server:

1. The runtime role must exist. Its password is a secret and therefore cannot
   live in a migration, so the role is provisioned out of band.
2. The runtime role must **not** hold BYPASSRLS. On a managed PostgreSQL the
   default superuser-adjacent role usually does, and row-level security is
   simply not applied to such a role -- every policy below would be inert while
   appearing, in ``\\d+``, to be in force. That failure is silent, which is why
   it is checked here and not left to a reviewer.

``FORCE ROW LEVEL SECURITY`` is applied as well as ``ENABLE``, because ENABLE
alone exempts the table owner, and the owner is who migrations run as.

Policies compare against a transaction-local GUC rather than a database user,
so one pooled connection can serve many tenants without any of them being able
to see another. ``current_setting(..., true)`` yields NULL when unset and NULL
equals nothing, so a query issued outside a tenant scope returns zero rows.
That is the correct behaviour for a forgotten filter.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "aetheris_app"
OWNER_GUC = "aetheris.owner_id"

#: Every table carrying user data. ``users`` scopes on its own primary key;
#: the rest carry a denormalised ``owner_id`` so each policy is a column
#: comparison rather than a subquery executed per row.
TENANT_TABLES = ("accounts", "orders", "order_fills", "order_discrepancies")
ALL_TABLES = ("users", *TENANT_TABLES)

#: The preconditions, as a template rather than an f-string.
#:
#: A role name cannot be a bind parameter -- PostgreSQL wants an identifier
#: here, not a value -- so the name is substituted textually. It is a module
#: constant that never comes from a request or a row, and writing it as a
#: placeholder substitution rather than an f-string says so at the point where
#: a reader would otherwise have to check.
_GUARD_TEMPLATE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__ROLE__') THEN
        RAISE EXCEPTION
            'role % does not exist. Tenant isolation requires a runtime role '
            'separate from the schema owner, because the owner bypasses row-level '
            'security. Create it first (LOGIN, NOSUPERUSER, NOCREATEDB, '
            'NOCREATEROLE, NOBYPASSRLS) and point AETHERIS_DATABASE_URL at it.',
            '__ROLE__';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__ROLE__' AND rolbypassrls) THEN
        RAISE EXCEPTION
            'role % holds BYPASSRLS. Row-level security is not applied to such a '
            'role, so the policies this migration creates would be inert. Remove '
            'the attribute (ALTER ROLE % NOBYPASSRLS) before migrating.',
            '__ROLE__', '__ROLE__';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__ROLE__'
               AND (rolsuper OR rolcreaterole)) THEN
        RAISE EXCEPTION
            'role % holds SUPERUSER or CREATEROLE. The runtime role must not be '
            'able to grant itself out of its own policies.', '__ROLE__';
    END IF;
END
$$;
"""

_GUARD = _GUARD_TEMPLATE.replace("__ROLE__", RUNTIME_ROLE)


def _scope(table: str) -> str:
    column = "id" if table == "users" else "owner_id"
    # NULLIF guards the cast: an empty GUC would raise rather than fail closed.
    return f"{column} = NULLIF(current_setting('{OWNER_GUC}', true), '')::uuid"


def upgrade() -> None:
    op.execute(_GUARD)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")

    for table in ALL_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"FOR ALL TO {RUNTIME_ROLE} "
            f"USING ({_scope(table)}) WITH CHECK ({_scope(table)})"
        )

    # The runtime role reads and writes rows. It does not own tables, so it
    # cannot disable a policy, drop one, or alter a column type.
    for table in ALL_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RUNTIME_ROLE}")

    # Discrepancies are evidence: append-only, enforced by privilege rather
    # than by remembering not to write the statement.
    op.execute(f"REVOKE UPDATE, DELETE ON order_discrepancies FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT UPDATE, DELETE ON order_discrepancies TO {RUNTIME_ROLE}")
    for table in ALL_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {RUNTIME_ROLE}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {RUNTIME_ROLE}")
