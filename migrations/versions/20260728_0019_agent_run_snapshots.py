"""agent_run_snapshots: agent-run state snapshot — ADR-066 §2 (03-data-model.md §25)

Expand-only (one new table ``agent_run_snapshots`` + 2 indexes + 2 CHECKs). Nothing existing is
touched: ``agent_runs`` keeps its schema and the enum ``agent_run_status`` is NOT extended
(``waiting_approval`` is a DERIVED client status computed on read, ADR-066 §4).

1:1 to ``agent_runs``: ``run_id`` is the PK and at the same time an FK to ``agent_runs.run_id``
``ON DELETE CASCADE`` (a snapshot never outlives its lifecycle row). ``user_id`` is duplicated for a
direct RBAC scope and for ``ON DELETE CASCADE`` from ``users`` (deleting a user wipes the stored
model text). ``result_text``/``pending_approval`` carry user-facing content and are cleared by the
reaper retention sweep after ``AGENT_RUN_SNAPSHOT_TTL_DAYS`` (the row itself is kept so
``GET /v1/agent/runs/{runId}/state`` keeps returning status/usage/updatedAt).

Two indexes, deliberately: the owner scope, and a PARTIAL index serving the retention sweep. The
sweep index is partial rather than a plain ``(updated_at)`` one because the table is the hot-write
side of the contour (an upsert every few seconds per active run, every one of them touching
``updated_at``), so a second full index on that column would cost write amplification on every
flush while serving a job that matches zero rows in the steady state.

Chain: 0001 -> ... -> 0018 -> 0019 (single head). down_revision is the FULL revision id of 0018
(``0018_agent_runs``). The revision id ``0019_agent_run_snapshots`` is kept ≤ 32 chars
(``alembic_version.version_num`` is VARCHAR(32) by default and ``migrations/env.py`` does not raise
``version_table_column_length``); a longer id truncates on the ``UPDATE alembic_version`` at upgrade
time (asyncpg StringDataRightTruncationError).

Revision ID: 0019_agent_run_snapshots
Revises: 0018_agent_runs
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_agent_run_snapshots"
down_revision: str | None = "0018_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 25. agent_run_snapshots — UX state snapshot of an agent run (ADR-066 §2). Written as a side
    # effect of the SSE relay, read by GET /v1/agent/runs/{runId}/state. Status is NOT duplicated
    # here (agent_runs.status stays the single source of truth).
    op.create_table(
        "agent_run_snapshots",
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Concatenated message.delta text (head-preserving truncation in the writer).
        sa.Column("result_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("last_tool", sa.Text(), nullable=True),
        # {"tool": ..., "preview": ...} while the run waits for an approval answer.
        sa.Column("pending_approval", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("input_tokens >= 0", name="ck_agent_run_snapshots_input_nonneg"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_agent_run_snapshots_output_nonneg"),
    )
    op.create_index("ix_agent_run_snapshots_user", "agent_run_snapshots", ["user_id"])
    # Retention sweep (ADR-066 §7) — PARTIAL on the sweep predicate, not a plain (updated_at)
    # index. The sweep runs every HERMES_REAPER_INTERVAL_SECONDS (300 s) and in the steady state
    # matches ZERO rows, because the idempotency guard excludes everything it already cleared. A
    # plain index would still make every tick walk the whole tail of history older than the TTL to
    # discard it; the partial index contains ONLY rows that still hold content, so a swept-clean
    # history costs O(0) per tick and the index itself shrinks as rows are cleared. The WHERE must
    # stay byte-identical to the sweep predicate for the planner to use it.
    op.create_index(
        "ix_agent_run_snapshots_sweep",
        "agent_run_snapshots",
        ["updated_at"],
        postgresql_where=sa.text("result_text <> '' OR pending_approval IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_snapshots_sweep", table_name="agent_run_snapshots")
    op.drop_index("ix_agent_run_snapshots_user", table_name="agent_run_snapshots")
    op.drop_table("agent_run_snapshots")
