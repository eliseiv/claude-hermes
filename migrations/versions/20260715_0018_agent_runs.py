"""agent_runs: incremental agent-run billing + pause/resume — ADR-064 §6 (03-data-model.md §24)

Expand-only (new enum ``agent_run_status`` + table ``agent_runs`` + 3 indexes + 2 CHECKs). One row
per Hermes run (``run_id`` TEXT PK, NOT a UUID); ``user_id`` FK ``users`` ON DELETE CASCADE. The
self-FK ``continued_from_run_id`` (``ON DELETE SET NULL``) forms the resume continuation chain
(child → parent; root = NULL). ``cumulative_credits_spent``/``last_billed_step`` are a denormalised
mirror of the ledger (source of truth is ``ledger_transactions``). The table is populated ONLY on
the incremental-billing path (``AGENT_INCREMENTAL_BILLING_ENABLED``, ADR-064 §5); the post-hoc
ADR-047 path never writes here.

``users``/other tables are NOT touched.

Chain: 0001 -> ... -> 0017 -> 0018 (single head). down_revision is the FULL revision id of 0017
(``0017_hermes_provisioning``), NOT the short ``0017`` — the short form would break the Alembic
chain. The revision id ``0018_agent_runs`` is kept ≤ 32 chars (``alembic_version.version_num`` is
VARCHAR(32) by default and ``migrations/env.py`` does not raise ``version_table_column_length``); a
longer id truncates on the ``UPDATE alembic_version`` at upgrade time
(asyncpg StringDataRightTruncationError).

Revision ID: 0018_agent_runs
Revises: 0017_hermes_provisioning
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_agent_runs"
down_revision: str | None = "0017_hermes_provisioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_ENUM = postgresql.ENUM(
    "running",
    "paused",
    "resumed",
    "completed",
    "failed",
    "cancelled",
    name="agent_run_status",
    create_type=False,
)


def upgrade() -> None:
    # 24. agent_runs — agent-run lifecycle + resume chain (ADR-064 §6). run_id TEXT PK (Hermes
    # string, not UUID); user_id FK users ON DELETE CASCADE. session_id is the stable continuation
    # key. continued_from_run_id is a self-FK (ON DELETE SET NULL) forming the resume chain.
    _STATUS_ENUM.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            _STATUS_ENUM,
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column(
            "cumulative_credits_spent",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_billed_step",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        # Self-FK: continuation chain (child → parent). Root = NULL.
        sa.Column(
            "continued_from_run_id",
            sa.Text(),
            sa.ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("cumulative_credits_spent >= 0", name="ck_agent_runs_cumulative_nonneg"),
        sa.CheckConstraint("last_billed_step >= 0", name="ck_agent_runs_last_step_nonneg"),
    )
    op.create_index("ix_agent_runs_user_status", "agent_runs", ["user_id", "status"])
    op.create_index("ix_agent_runs_session", "agent_runs", ["session_id"])
    op.create_index("ix_agent_runs_continued_from", "agent_runs", ["continued_from_run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_continued_from", table_name="agent_runs")
    op.drop_index("ix_agent_runs_session", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_status", table_name="agent_runs")
    op.drop_table("agent_runs")
    _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
