"""Testnet execution: venue identity, provenance and the uniqueness the venue implies.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21

Additive and fully reversible. Phase 1 already provisioned the columns a venue
path needs -- ``venue_order_id``, ``submission_attempts``, ``next_retry_at``,
``reconciliation_*`` -- so what is added here is what phase 1 could not have
known: who we are at the venue, what the venue literally said, and the
execution parameters that were verified before an order was allowed to leave.

Nothing here stores a credential. ``credential_ref`` holds a *name* that
resolves to an environment variable at runtime, and a CHECK constraint confines
it to a shape no key or secret could occupy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(24, 8)


def upgrade() -> None:
    # --- accounts: which venue account, under which named credential --------
    op.add_column("accounts", sa.Column("venue_account_id", sa.String(64), nullable=True))
    op.add_column("accounts", sa.Column("credential_ref", sa.String(40), nullable=True))
    op.create_check_constraint(
        "ck_accounts_credential_ref_is_a_name",
        "accounts",
        "credential_ref IS NULL OR credential_ref ~ '^[a-z0-9_]{1,40}$'",
    )

    # --- orders: provenance and the parameters verified before submission ---
    # What the venue literally said, before mapping. EXPIRED_IN_MATCH maps to
    # EXPIRED in the domain; without this column that distinction would be lost
    # at the only point anyone could have read it.
    op.add_column("orders", sa.Column("venue_status_raw", sa.String(32), nullable=True))
    # Separates "asked, nothing had changed" from "never asked".
    op.add_column("orders", sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True))
    # The leverage the venue confirmed it had applied, read back before the
    # order was sent. Recorded so a fill can be audited against the ceiling
    # that was actually in force rather than the one that was requested.
    op.add_column("orders", sa.Column("venue_leverage", MONEY, nullable=True))
    op.add_column("orders", sa.Column("venue_margin_mode", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_orders_venue_margin_mode",
        "orders",
        "venue_margin_mode IS NULL OR venue_margin_mode IN ('ISOLATED','CROSSED')",
    )

    # One venue order belongs to at most one local order. Previously indexed
    # for lookup but not constrained, which left room for two local records to
    # claim the same venue order after a confused reconciliation.
    op.create_unique_constraint(
        "uq_orders_account_venue_order_id", "orders", ["account_id", "venue_order_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_orders_account_venue_order_id", "orders", type_="unique")
    op.drop_constraint("ck_orders_venue_margin_mode", "orders", type_="check")
    op.drop_column("orders", "venue_margin_mode")
    op.drop_column("orders", "venue_leverage")
    op.drop_column("orders", "last_polled_at")
    op.drop_column("orders", "venue_status_raw")
    op.drop_constraint("ck_accounts_credential_ref_is_a_name", "accounts", type_="check")
    op.drop_column("accounts", "credential_ref")
    op.drop_column("accounts", "venue_account_id")
