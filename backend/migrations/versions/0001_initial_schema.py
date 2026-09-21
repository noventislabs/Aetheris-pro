"""Initial schema: users, accounts, orders, order_fills, order_discrepancies.

Revision ID: 0001
Revises:
Create Date: 2026-09-21

Money is NUMERIC(24,8) and time is timestamptz throughout; see
adapters/persistence/models.py for why neither is negotiable.

Fully reversible. An order history that cannot migrate is an order history that
eventually gets dropped, so every migration in this project either reverses or
says in its docstring that it cannot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(24, 8)
UUID = PgUUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(120)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "accounts",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "owner_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("starting_balance", MONEY, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("owner_id", "mode", name="uq_accounts_owner_mode"),
        sa.CheckConstraint("mode IN ('PAPER','TESTNET','LIVE')", name="ck_accounts_mode"),
    )
    op.create_index("ix_accounts_owner_id", "accounts", ["owner_id"])

    op.create_table(
        "orders",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "account_id", UUID, sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.Column("venue_order_id", sa.String(64)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("intent_key", sa.Text, nullable=False),
        sa.Column("reduce_only", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("price", MONEY),
        sa.Column("filled_quantity", MONEY, nullable=False, server_default=sa.text("0")),
        sa.Column("average_fill_price", MONEY),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        # Written in the same transaction as state=SUBMITTED and committed
        # before the network call; the gap to venue_order_id IS the window.
        sa.Column("submitted_at", TS),
        sa.Column("terminal_at", TS),
        sa.Column("rejection_code", sa.String(64)),
        sa.Column("rejection_detail", sa.Text),
        sa.Column("venue_rejection", sa.Text),
        sa.Column(
            "reconciliation_attempts", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_reconciled_at", TS),
        sa.Column("reconciliation_detail", sa.Text),
        sa.Column("submission_attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("next_retry_at", TS),
        sa.Column("resolved_by_operator", sa.String(120)),
        sa.Column("operator_reason", sa.Text),
        # The idempotency guarantee, moved out of application code so it holds
        # when a recovery pass and a live request race.
        sa.UniqueConstraint("account_id", "client_order_id", name="uq_orders_account_client_id"),
    )
    op.create_index("ix_orders_account_state", "orders", ["account_id", "state"])
    op.create_index("ix_orders_venue_order_id", "orders", ["venue_order_id"])
    op.create_index("ix_orders_owner", "orders", ["owner_id"])

    op.create_table(
        "order_fills",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", UUID, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fill_id", sa.String(64), nullable=False),
        sa.Column("venue_trade_id", sa.String(64)),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("fee", MONEY, nullable=False, server_default=sa.text("0")),
        sa.Column("filled_at", TS, nullable=False),
        # A replayed venue message must not double-count a fill.
        sa.UniqueConstraint("order_id", "venue_trade_id", name="uq_order_fills_venue_trade"),
        sa.UniqueConstraint("order_id", "fill_id", name="uq_order_fills_fill_id"),
    )
    op.create_index("ix_order_fills_order", "order_fills", ["order_id"])

    op.create_table(
        "order_discrepancies",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", UUID, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("local_state", sa.String(24), nullable=False),
        sa.Column("venue_state", sa.String(24)),
    )
    op.create_index("ix_order_discrepancies_order", "order_discrepancies", ["order_id"])


def downgrade() -> None:
    op.drop_table("order_discrepancies")
    op.drop_table("order_fills")
    op.drop_table("orders")
    op.drop_table("accounts")
    op.drop_table("users")
