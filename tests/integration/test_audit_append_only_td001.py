"""Integration: durable append-only audit_logs (TD-001 / ADR-053, migration 0016). Real PostgreSQL.

ADR-053 is defense-in-depth: (1) least-privilege runtime role app_rw (REVOKE UPDATE/DELETE/TRUNCATE,
GRANT INSERT/SELECT) and (2) a role-agnostic BEFORE UPDATE/DELETE trigger ``audit_logs_no_mutate()``
that raises for ANY role (incl. the owner). These tests exercise the trigger (role-agnostic) against
the migrated testcontainer:
- a normal INSERT works (append path intact);
- a SELECT works (read path intact);
- an UPDATE raises the trigger exception;
- a DELETE raises the trigger exception.

NB on the role REVOKE (ADR-053 §1): app_rw / app_migrate are provisioned by the devops init script
``docker/postgres/init/01-roles.sh`` (docker-compose / e2e) — they do NOT exist in the single-role
testcontainer, and migration 0016 guards the GRANT/REVOKE behind a pg_roles existence check (no-op
here). So the *role-based* permission-denied is exercised in the docker/e2e environment with the
roles present; the *trigger* (the role-agnostic belt) is the durable guarantee asserted here and is
what protects even a privileged/owner connection.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import seed_user


async def _insert_audit(
    s: AsyncSession, uid: uuid.UUID, event_type: str = "policy_decision"
) -> str:
    row_id = str(uuid.uuid4())
    await s.execute(
        text(
            "INSERT INTO audit_logs (id, user_id, event_type, payload) "
            "VALUES (:id, :u, :e, CAST(:payload AS JSONB))"
        ),
        {"id": row_id, "u": str(uid), "e": event_type, "payload": '{"k": 1}'},
    )
    return row_id


# ============================================================================
# INSERT + SELECT (append path intact)
# ============================================================================
@pytest.mark.asyncio
async def test_audit_insert_and_select_work(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        row_id = await _insert_audit(s, uid)
        await s.commit()
    async with db_sessionmaker() as s:
        got = await s.scalar(text("SELECT event_type FROM audit_logs WHERE id=:id"), {"id": row_id})
    assert got == "policy_decision"


# ============================================================================
# UPDATE → trigger raises (append-only)
# ============================================================================
@pytest.mark.asyncio
async def test_audit_update_blocked_by_trigger(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        row_id = await _insert_audit(s, uid)
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(DBAPIError) as ei:
            await s.execute(
                text("UPDATE audit_logs SET event_type='tampered' WHERE id=:id"), {"id": row_id}
            )
        await s.rollback()
    # The trigger message carries the ADR marker (audit_logs_no_mutate raises with TG_OP).
    assert "append-only" in str(ei.value).lower() or "forbidden" in str(ei.value).lower()

    # The row is unchanged (UPDATE rolled back / never applied).
    async with db_sessionmaker() as s:
        got = await s.scalar(text("SELECT event_type FROM audit_logs WHERE id=:id"), {"id": row_id})
    assert got == "policy_decision"


# ============================================================================
# DELETE → trigger raises (append-only)
# ============================================================================
@pytest.mark.asyncio
async def test_audit_delete_blocked_by_trigger(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        row_id = await _insert_audit(s, uid)
        await s.commit()

    async with db_sessionmaker() as s:
        with pytest.raises(DBAPIError) as ei:
            await s.execute(text("DELETE FROM audit_logs WHERE id=:id"), {"id": row_id})
        await s.rollback()
    assert "append-only" in str(ei.value).lower() or "forbidden" in str(ei.value).lower()

    # The row still exists (DELETE blocked).
    async with db_sessionmaker() as s:
        cnt = await s.scalar(text("SELECT count(*) FROM audit_logs WHERE id=:id"), {"id": row_id})
    assert int(cnt) == 1


# ============================================================================
# The append-only triggers exist in the migrated schema (migration 0016)
# ============================================================================
@pytest.mark.asyncio
async def test_append_only_triggers_present(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        rows = await s.scalars(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'audit_logs'::regclass AND NOT tgisinternal"
            )
        )
        names = set(rows)
    assert "trg_audit_logs_no_update" in names
    assert "trg_audit_logs_no_delete" in names
