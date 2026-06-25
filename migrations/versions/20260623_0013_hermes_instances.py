"""hermes_instances: per-user Hermes runtime registry — ADR-046 §3 (03-data-model.md §22)

Expand-only (new enum + table + index). Adds the ``hermes_instances`` table: one row per user
(``user_id`` PK, FK ``users`` ON DELETE CASCADE) holding the container metadata and the
envelope-encrypted ``API_SERVER_KEY`` (``api_key_enc``/``encrypted_dek``/``nonce``, ADR-003 —
plaintext is never stored). ``ix_hermes_instances_status_active`` serves the hibernation reaper
(``stop_idle``: ``status='running' AND last_active_at < threshold``).

``users``/other tables are NOT touched. The host volume (HERMES_HOME) lives outside the DB.

Chain: 0001 -> ... -> 0012 -> 0013 (single head). down_revision is the FULL revision id of 0012
(``0012_auth_identities``), NOT the short ``0012`` — the short form would break the Alembic chain.

Revision ID: 0013_hermes_instances
Revises: 0012_auth_identities
Create Date: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_hermes_instances"
down_revision: str | None = "0012_auth_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_ENUM = postgresql.ENUM(
    "provisioning",
    "running",
    "stopped",
    name="hermes_instance_status",
    create_type=False,
)


def upgrade() -> None:
    # 22. hermes_instances — per-user Hermes runtime registry (ADR-046 §3). One row per user
    # (user_id PK ⇒ exactly one instance). The API_SERVER_KEY is stored ONLY envelope-encrypted
    # (api_key_enc/encrypted_dek/nonce, ADR-003); the host port is not published (addressing by
    # `endpoint` DNS name in the control-plane docker network).
    _STATUS_ENUM.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "hermes_instances",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # NULL in `provisioning` until the container is created.
        sa.Column("container_id", sa.Text(), nullable=True),
        # DNS name:port in the docker network, e.g. 'hermes-user-<id>:8642'.
        sa.Column("endpoint", sa.Text(), nullable=True),
        # Envelope-encrypted API_SERVER_KEY (ADR-003): plaintext never stored.
        sa.Column("api_key_enc", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            _STATUS_ENUM,
            nullable=False,
            server_default=sa.text("'provisioning'"),
        ),
        # nullable: host port is NOT published (reserved for alternative RuntimeBackends).
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_hermes_instances_status_active",
        "hermes_instances",
        ["status", "last_active_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_hermes_instances_status_active", table_name="hermes_instances")
    op.drop_table("hermes_instances")
    _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
