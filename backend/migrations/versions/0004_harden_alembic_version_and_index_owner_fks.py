"""Close the alembic_version exposure, and index the two owner foreign keys.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22

Two unrelated-looking changes, shipped together because one is a security gap
and the other is the cosmetic half of the same audit; releasing the cosmetic
half alone would have been the wrong priority.

**1. ``alembic_version`` was reachable with the public anon key.**

Supabase applies ``GRANT ALL ON ALL TABLES IN SCHEMA public TO anon,
authenticated`` to tables created in ``public``. The five Aetheris trading
tables absorbed that harmlessly -- row-level security is enabled and FORCEd on
every one of them, and the policies are scoped to ``aetheris_app``, so ``anon``
gets a grant it can never exercise and reads zero rows.

``alembic_version`` is the exception. Alembic creates it, so it inherited the
same grant, and it has no RLS to stop anyone using it. Verified against the
live database by ``SET ROLE anon``: every trading table returned 0 rows, and
``alembic_version`` returned its row. The anon key is *designed* to be public --
it ships in browsers -- so anyone holding it could have read, altered or
truncated the migration pointer. No trading data was at risk; migration
integrity was. An emptied ``alembic_version`` makes Alembic believe the
database is unmigrated and attempt ``0001`` against tables that already exist.

The fix is a revoke, not RLS. RLS on the version table would have to be
navigated by every future migration, and the table needs exactly one reader:
the owner that runs Alembic.

**2. Two foreign keys had no supporting index.**

``order_fills.owner_id`` and ``order_discrepancies.owner_id`` both reference
``users``, and PostgreSQL does not index the referencing side automatically.
Every ``ON DELETE CASCADE`` from ``users`` therefore scans them. This costs
nothing today -- both tables are empty -- and it is not a fix for a problem
anyone is currently having. It is the shape of a problem that only appears
once there is data, at which point the fix is no longer cheap.

**On the downgrade.** It restores the ``anon``/``authenticated`` grants, which
means downgrading past this revision deliberately re-opens the exposure above.
That is the honest reading of "reversible": a downgrade returns the database to
the state revision 0003 described, and a migration that quietly declined to
reverse half of itself would leave the chain in a state no revision describes.
The re-exposure is called out here so that anyone running the downgrade knows
what it costs.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The roles whose grants are being withdrawn. ``service_role`` is deliberately
#: NOT in this list: it also holds full privileges, but that key is a
#: server-side secret by design, whereas the anon key is published to browsers.
#: Narrowing it further is a separate decision, not a drive-by change.
PUBLIC_ROLES = ("anon", "authenticated")

VERSION_TABLE = "alembic_version"

#: Exact names, read from the live catalogue rather than assumed. Index names
#: follow the convention already in use (``ix_orders_owner``).
OWNER_FK_INDEXES = (
    ("ix_order_fills_owner", "order_fills", "owner_id"),
    ("ix_order_discrepancies_owner", "order_discrepancies", "owner_id"),
)


def upgrade() -> None:
    # The version table is Alembic's, and Alembic connects as the owner. The
    # owner's privileges come from ownership, not from a grant, so revoking
    # these takes nothing away from the migration path.
    for role in PUBLIC_ROLES:
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {VERSION_TABLE} FROM {role}")

    for name, table, column in OWNER_FK_INDEXES:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _column in OWNER_FK_INDEXES:
        op.drop_index(name, table_name=table)

    # Restores the pre-0004 state, exposure included. See the module docstring.
    for role in PUBLIC_ROLES:
        op.execute(f"GRANT ALL PRIVILEGES ON TABLE {VERSION_TABLE} TO {role}")
