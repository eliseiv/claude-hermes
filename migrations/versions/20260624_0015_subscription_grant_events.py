"""subscription_grant_events: durable idempotency anchor — ADR-052 (03-data-model.md §23)

Expand-only: adds the ``subscription_grant_events`` table — a durable idempotency anchor for
``POST /v1/admin/subscription/grant`` (``SubscriptionService.admin_grant``) that lives OUTSIDE the
ledger, so a strict 409 on "same idempotencyKey, different payload" is reachable for BOTH
``grantCredits`` paths (incl. ``grantCredits=false``, where no ledger row exists). Closes TD-030.

Pattern mirrors ``adapty_webhook_events`` (migration 0008): one transaction with
``INSERT ... ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING ...``. ``payload_hash`` is
sha256 of the canonical payload (plan ‖ ISO8601 expiresAt ‖ grantCredits) — the payload-conflict
source of truth (covers the full subscription payload, not only the ledger ``amount``).

Chain: 0001 -> ... -> 0014 -> 0015 (single head). down_revision is the FULL revision id of 0014
(``0014_wallets_debt``), NOT the short ``0014``.

Revision ID: 0015_subscription_grant_events
Revises: 0014_wallets_debt
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0015_subscription_grant_events"
down_revision: str | None = "0014_wallets_debt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 23. subscription_grant_events — durable idempotency anchor for admin subscription-grant
    # (ADR-052 §1). UNIQUE (user_id, idempotency_key) is the dedup point for BOTH grantCredits
    # paths. ledger_tx_id is the credit-tx id when grantCredits=true (nullable otherwise).
    op.create_table(
        "subscription_grant_events",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        # sha256(plan ‖ ISO8601 expiresAt ‖ grantCredits) — payload-conflict source of truth.
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_credits", sa.Boolean(), nullable=False),
        # id of the credit-tx at grantCredits=true (nullable).
        sa.Column("ledger_tx_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ux_subscription_grant_idempotency",
        "subscription_grant_events",
        ["user_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_subscription_grant_idempotency", table_name="subscription_grant_events")
    op.drop_table("subscription_grant_events")
