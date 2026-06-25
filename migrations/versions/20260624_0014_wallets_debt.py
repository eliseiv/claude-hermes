"""wallets.debt: agent-run debt reconciliation — ADR-051 (03-data-model.md §3)

Expand-only: adds the ``wallets.debt`` column (accumulated uncharged delta of agent runs, in
credits) with a non-negative CHECK. The column is created INDEPENDENTLY of the runtime feature
flag ``AGENT_DEBT_RECONCILE_ENABLED`` (ADR-051 §5): the schema always has it; the flag only gates
whether the code writes/reads it. ``NOT NULL DEFAULT 0`` backfills existing wallets to zero debt.

``debt`` is a SEPARATE aggregate on ``wallets`` (like ``balance``), NOT a ``ledger_transactions``
row — the ledger stays clean and the invariant ``balance == Σ(credit) − Σ(debit)`` is preserved
(ADR-051 §1). ``balance`` is never driven negative; the uncharged delta accrues in ``debt``.

Chain: 0001 -> ... -> 0013 -> 0014 (single head). down_revision is the FULL revision id of 0013
(``0013_hermes_instances``), NOT the short ``0013`` — the short form would break the Alembic chain.

Revision ID: 0014_wallets_debt
Revises: 0013_hermes_instances
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BIGINT

revision: str = "0014_wallets_debt"
down_revision: str | None = "0013_hermes_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADR-051 §1: accumulated uncharged agent-run delta (credits). server_default '0' backfills
    # existing rows; the CHECK enforces non-negativity (debt never goes below 0 — clawback caps
    # repaid at min(grant, debt)). Created regardless of AGENT_DEBT_RECONCILE_ENABLED.
    op.add_column(
        "wallets",
        sa.Column(
            "debt",
            BIGINT(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint("ck_wallets_debt_nonneg", "wallets", "debt >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_wallets_debt_nonneg", "wallets", type_="check")
    op.drop_column("wallets", "debt")
