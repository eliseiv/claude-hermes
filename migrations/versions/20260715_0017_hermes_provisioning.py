"""hermes_instances.provisioning_started_at — ADR-062 §1a (wake stale-anchor)

Expand-only: adds the nullable ``hermes_instances.provisioning_started_at`` column (TIMESTAMPTZ).
It records the start of the CURRENT provisioning attempt (cold-start ``create_provisioning`` OR
wake ``mark_provisioning``) and becomes the anchor for ``_is_stale_provisioning`` (ADR-062 §1a),
replacing the immutable ``created_at``. For a woken instance ``created_at`` is the ORIGINAL
provision time (hours/days ago), so anchoring stale on it would falsely flag a live wake-wait and
let a concurrent ``ensure_running`` replay-remove its container. A dedicated column keeps
``created_at`` immutable and makes the "live wait ≤ ready < stale" invariant true.

Backfill: existing in-flight ``provisioning`` rows get ``provisioning_started_at = created_at`` so
the current TD-031 stale semantics are preserved across the deploy (an in-flight row keeps its
existing age anchor). Rows in other statuses stay NULL (only a `provisioning` row is stale-checked;
the manager anchors with a ``created_at`` fallback defensively). ``created_at`` is NOT touched.

Chain: 0001 -> ... -> 0016 -> 0017 (single head). down_revision is the FULL revision id of 0016
(``0016_audit_logs_append_only``), NOT the short ``0016`` — the short form would break the chain.
The revision id is kept ≤ 32 chars (``alembic_version.version_num`` is VARCHAR(32) by default and
``migrations/env.py`` does not raise ``version_table_column_length``); a longer id truncates on the
``UPDATE alembic_version`` at upgrade time (asyncpg StringDataRightTruncationError).

Revision ID: 0017_hermes_provisioning
Revises: 0016_audit_logs_append_only
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_hermes_provisioning"
down_revision: str | None = "0016_audit_logs_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADR-062 §1a: nullable anchor for the CURRENT provisioning attempt (expand-only add-column).
    op.add_column(
        "hermes_instances",
        sa.Column("provisioning_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill in-flight `provisioning` rows so the existing TD-031 stale age is preserved across
    # the deploy (an in-flight cold-start keeps its created_at anchor). Other statuses stay NULL.
    op.execute(
        "UPDATE hermes_instances "
        "SET provisioning_started_at = created_at "
        "WHERE status = 'provisioning'"
    )


def downgrade() -> None:
    op.drop_column("hermes_instances", "provisioning_started_at")
