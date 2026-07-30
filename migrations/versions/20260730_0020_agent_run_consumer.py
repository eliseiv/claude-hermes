"""agent-run background consumer: heartbeat column + active-runs index — ADR-067 §4/§5

Expand-only, two objects, nothing existing is touched:

1. ``agent_run_snapshots.consumer_heartbeat_at TIMESTAMPTZ NULL`` — liveness of the background
   consumer (ADR-067 §4). A SEPARATE column is mandatory: both ``agent_run_snapshots.updated_at``
   and ``agent_runs.updated_at`` are the CLIENT's staleness detector (``/state.updatedAt``,
   ADR-066 §5), while a heartbeat is written even when nothing about the run changed — moving
   either of them on a heartbeat would tell the client the state advanced when it did not. The
   column is never returned to the client and is not part of the ``/state`` contract.
   ⚠️ The ``first_byte_at`` column of the original design is CANCELLED together with the
   instance-unwedging mechanism (ADR-067 §5.1): its premise was disproved — the row lock was what
   wedged, not the instance (TD-039 re-attributed, TD-040 fixed).

2. ``ix_agent_runs_active`` — PARTIAL index on ``agent_runs (created_at) WHERE status IN
   ('running','resumed')``, serving the orphan sweep (ADR-067 §5).

Why the index lives on ``agent_runs`` and NOT on the heartbeat column (03-data-model.md §25):
a ``… ON agent_run_snapshots (consumer_heartbeat_at) WHERE consumer_heartbeat_at IS NOT NULL``
index does NOT self-clean — its predicate stays true for every row the consumer ever touched, so it
would grow with the entire run history (exactly the property for which ADR-066 §7 rejected a full
``updated_at`` index) and would amplify writes on a column written every 30s per active run. The
status predicate here self-cleans instead: a terminal run leaves the index forever. Leading column
is ``created_at``, NOT ``updated_at`` — candidate age is computed as
``COALESCE(snapshot.consumer_heartbeat_at, agent_runs.created_at)`` and ``updated_at`` does not
take part in the sweep at all; the index is a compact partial SET of active runs that also yields a
stable oldest-first order under the ``AGENT_RUN_ORPHAN_MAX_PER_TICK`` cap. The index count of
``agent_run_snapshots`` therefore stays TWO (ADR-066 §7 invariant preserved).

Chain: 0001 -> ... -> 0019 -> 0020 (single head). ``down_revision`` is the FULL revision id of 0019.
The revision id is kept ≤ 32 chars (``alembic_version.version_num`` is VARCHAR(32) and
``migrations/env.py`` sets no ``version_table_column_length``; a longer id truncates on the
``UPDATE alembic_version`` — asyncpg StringDataRightTruncationError).

Revision ID: 0020_agent_run_consumer
Revises: 0019_agent_run_snapshots
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_agent_run_consumer"
down_revision: str | None = "0019_agent_run_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADR-067 §4. NULLable with no server_default: existing rows (runs that predate the consumer)
    # legitimately have no heartbeat, and the sweep reads that as "never seen a consumer" via
    # COALESCE(consumer_heartbeat_at, agent_runs.created_at) — a default of now() would instead
    # make every historical run look freshly alive.
    op.add_column(
        "agent_run_snapshots",
        sa.Column("consumer_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    # ADR-067 §5 orphan sweep working set. The WHERE must stay identical to the sweep predicate for
    # the planner to use the index.
    op.create_index(
        "ix_agent_runs_active",
        "agent_runs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('running', 'resumed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_active", table_name="agent_runs")
    op.drop_column("agent_run_snapshots", "consumer_heartbeat_at")
