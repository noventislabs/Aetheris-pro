"""Durable paper account state.

Paper balances, positions, the order log, trade history and the daily session
lived in process memory and were lost on every restart. That was the build's
oldest disclosed limitation, repeated on every account response. A database
is now configured, so the limitation is a choice rather than a constraint.

## One document, not a dozen tables

Order records get typed columns because they are *evidence*: a venue may
contradict them, a recovery pass queries them by client id, and a discrepancy
has to be provable afterwards. Paper state is none of that. It is simulation
state this process owns outright, replaced wholesale by ``POST /paper/reset``,
and never reconciled against an outside authority.

So it is one row per account, replaced atomically, carrying its schema version
as a column. The version is what makes the shortcut safe: a snapshot written
by a different build is refused rather than half-applied, and an account whose
balance restored while its open positions silently vanished never happens.

Money inside the document crosses as strings. A JSON number is a float to
almost every reader, and a paper balance that drifts by a cent per restart is
the kind of bug found months later and never explained.

## Tenancy

The table joins the same row-level security every other tenant table already
has, with the same policy shape and the same runtime role. It is added to the
policy set rather than left out of it: a new table that quietly sits outside
RLS is how tenant isolation stops being true.

Downgrade drops the table. That loses paper history, which is acceptable in a
way losing order history would not be -- paper state is reproducible by
trading again, and the engine falls back to in-memory and says so.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "paper_state_snapshots"
RUNTIME_ROLE = "aetheris_app"
OWNER_GUC = "aetheris.owner_id"


def _scope() -> str:
    # NULLIF guards the cast: an empty GUC raises rather than matching rows.
    return f"owner_id = NULLIF(current_setting('{OWNER_GUC}', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("owner_id", "account_id", name="uq_paper_snapshot_owner_account"),
    )
    # Names match what the model's ``index=True`` generates, so ``alembic
    # check`` stays clean rather than reporting perpetual drift.
    op.create_index("ix_paper_state_snapshots_owner_id", TABLE, ["owner_id"])
    op.create_index("ix_paper_state_snapshots_account_id", TABLE, ["account_id"])

    # The same isolation every other tenant table carries. FORCE as well as
    # ENABLE, because ENABLE alone does not apply to the table's owner.
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        f"FOR ALL TO {RUNTIME_ROLE} "
        f"USING ({_scope()}) WITH CHECK ({_scope()})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON {TABLE} FROM {RUNTIME_ROLE}")
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE}")
    op.drop_index("ix_paper_state_snapshots_account_id", table_name=TABLE)
    op.drop_index("ix_paper_state_snapshots_owner_id", table_name=TABLE)
    op.drop_table(TABLE)
