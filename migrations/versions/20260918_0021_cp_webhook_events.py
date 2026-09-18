"""cloudpayments_webhook_events — RU (broadapps/CloudPayments) webhook dedup journal (ADR-068)

Creates the ``cloudpayments_webhook_events`` table: the single deduplication point for RU
payment callbacks. ``transaction_id`` is the PRIMARY KEY (holds the broadapps ``payment_id``).

Expand-only: only CREATE TABLE + CREATE INDEX. Chain: 0020 -> 0021.

Revision ID: 0021_cp_webhook_events
Revises: 0020_agent_run_consumer
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_cp_webhook_events"
down_revision: str | None = "0020_agent_run_consumer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloudpayments_webhook_events",
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transaction_id"),
    )
    op.create_index(
        "ix_cloudpayments_webhook_events_user_id",
        "cloudpayments_webhook_events",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cloudpayments_webhook_events_user_id",
        table_name="cloudpayments_webhook_events",
    )
    op.drop_table("cloudpayments_webhook_events")
