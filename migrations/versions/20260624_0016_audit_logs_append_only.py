"""audit_logs durable append-only — ADR-053 (03-data-model.md §9, closes TD-001)

Defense-in-depth, runs under the migration role ``app_migrate`` (DATABASE_URL_MIGRATE), which keeps
full privileges for DDL/rollbacks while the runtime role ``app_rw`` is narrowed:

1. Least-privilege runtime grants (ADR-053 §1): ``REVOKE UPDATE, DELETE, TRUNCATE`` and
   ``GRANT INSERT, SELECT`` on ``audit_logs`` FROM/TO ``app_rw``. The normal application path can
   only append + read; it can never mutate or delete an audit record.
2. BEFORE UPDATE/DELETE trigger (ADR-053 §2): ``audit_logs_no_mutate()`` raises an exception for
   ANY role (incl. the owner on an accidental op) — a cheap insurance over REVOKE. INSERT/SELECT
   are untouched, so the normal audit INSERT from ``app_rw`` keeps working.

Role-scoped GRANT/REVOKE are guarded by a ``pg_roles`` existence check so the migration is a no-op
on single-role environments (local dev where only ``postgres`` exists and ``app_rw`` was never
created by the devops init script ``docker/postgres/init/01-roles.sh``). The trigger (role-agnostic)
is always applied. Intentional erasure (e.g. GDPR) is an out-of-band ``DISABLE TRIGGER`` under a
privileged role (ADR-053 §2) — NOT the application path.

Chain: 0001 -> ... -> 0015 -> 0016 (single head). down_revision is the FULL revision id of 0015
(``0015_subscription_grant_events``), NOT the short ``0015``.

Revision ID: 0016_audit_logs_append_only
Revises: 0015_subscription_grant_events
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_audit_logs_append_only"
down_revision: str | None = "0015_subscription_grant_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Apply the runtime-role grants only when app_rw exists (devops init-script created it). On a
# single-role dev DB the block is a guarded no-op (avoids "role app_rw does not exist" failure).
_REVOKE_GRANT_APP_RW = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM app_rw;
        GRANT INSERT, SELECT ON audit_logs TO app_rw;
    END IF;
END
$$;
"""

# Restore the broader grant profile on downgrade (only if app_rw exists). Does not lose data —
# only removes the hardening.
_RESTORE_GRANT_APP_RW = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON audit_logs TO app_rw;
    END IF;
END
$$;
"""

_CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_logs_no_mutate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only (ADR-053): % is forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # (1) Least-privilege runtime grants on audit_logs for app_rw (ADR-053 §1).
    op.execute(_REVOKE_GRANT_APP_RW)
    # (2) BEFORE UPDATE/DELETE trigger — role-agnostic defense-in-depth (ADR-053 §2).
    op.execute(_CREATE_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_audit_logs_no_update "
        "BEFORE UPDATE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_no_mutate();"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_logs_no_delete "
        "BEFORE DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_no_mutate();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_delete ON audit_logs;")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_update ON audit_logs;")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_no_mutate();")
    # Restore the broader grant profile (only removes the hardening; no data loss).
    op.execute(_RESTORE_GRANT_APP_RW)
