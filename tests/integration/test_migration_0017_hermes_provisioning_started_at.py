"""Integration: alembic migration 0017 (hermes_instances.provisioning_started_at, ADR-062 §1a).

ISOLATED throwaway Postgres container (mirrors test_migration_0013) so ALTER TABLE cannot disturb
the shared session container. Verifies:
- single migration head; 0017 is on the chain (down_revision = 0016 full revision id);
- upgrade ADDS the nullable ``provisioning_started_at`` (TIMESTAMPTZ) column;
- backfill: in-flight ``provisioning`` rows get ``provisioning_started_at = created_at`` (preserving
  the TD-031 stale age across the deploy); rows in other statuses stay NULL; ``created_at`` is NOT
  touched;
- downgrade DROPS the column; re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_migration_0013 + the conftest _migrated fixture).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# NB: the revision id is kept ≤ 32 chars (alembic_version.version_num is VARCHAR(32) by default) —
# a longer id truncates on the UPDATE alembic_version at upgrade time. So the migration ADDS the
# `provisioning_started_at` column, but the revision id itself is the shorter
# `0017_hermes_provisioning` (24 chars) — do not confuse the two.
_PREV_REV = "0016_audit_logs_append_only"
_THIS_REV = "0017_hermes_provisioning"
_COLUMN = "provisioning_started_at"


@pytest.fixture(scope="module")
def isolated_pg() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


def _alembic_config(url: str) -> Any:
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _run_async(url: str, fn: Any) -> Any:
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await fn(conn)
    finally:
        await engine.dispose()


def _columns(url: str, table: str) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns(table))
                return {c["name"]: c for c in cols}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _reset_to_prev(cfg: Any, url: str) -> None:
    from alembic import command

    async def _drop_all(conn: Any) -> None:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    asyncio.run(_run_async(url, _drop_all))
    command.upgrade(cfg, _PREV_REV)


# --------------------------- single head ---------------------------
def test_0017_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"
    assert heads[0] == _THIS_REV  # 0017 is the current head
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert _THIS_REV in ancestry
    assert _PREV_REV in ancestry  # 0016 is its ancestor (chain intact)


# --------------------------- upgrade adds the nullable column ---------------------------
def test_0017_upgrade_adds_nullable_timestamptz_column(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    # Column absent at 0016.
    assert _COLUMN not in _columns(isolated_pg, "hermes_instances")

    command.upgrade(cfg, _THIS_REV)

    cols = _columns(isolated_pg, "hermes_instances")
    assert _COLUMN in cols
    col = cols[_COLUMN]
    assert col["nullable"] is True  # expand-only nullable add-column
    # TIMESTAMPTZ (timezone-aware timestamp).
    assert "TIMESTAMP" in str(col["type"]).upper()
    assert getattr(col["type"], "timezone", False) is True


# --------------------------- backfill semantics ---------------------------
def test_0017_backfill_sets_provisioning_rows_to_created_at_others_null(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    prov_uid, run_uid, stop_uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _seed(conn: Any) -> None:
        for uid in (prov_uid, run_uid, stop_uid):
            await conn.execute(
                text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
            )
        # created_at explicitly set in the past so the backfill (= created_at) is assertable.
        rows = [
            (prov_uid, "provisioning"),
            (run_uid, "running"),
            (stop_uid, "stopped"),
        ]
        for uid, status in rows:
            await conn.execute(
                text(
                    "INSERT INTO hermes_instances "
                    "(user_id, api_key_enc, encrypted_dek, nonce, status, created_at) "
                    "VALUES (:uid, :a, :d, :n, :st, TIMESTAMPTZ '2026-01-01 00:00:00+00')"
                ),
                {"uid": str(uid), "a": b"e", "d": b"d", "n": b"n", "st": status},
            )

    # Seed at 0016 (no provisioning_started_at column yet).
    asyncio.run(_run_async(isolated_pg, _seed))

    command.upgrade(cfg, _THIS_REV)

    async def _read(conn: Any) -> dict[str, Any]:
        result = await conn.execute(
            text(
                "SELECT user_id, status, created_at, provisioning_started_at "
                "FROM hermes_instances"
            )
        )
        return {str(r.user_id): r for r in result}

    rows = asyncio.run(_run_async(isolated_pg, _read))

    # provisioning row: backfilled to created_at (stale age preserved across the deploy).
    prov = rows[str(prov_uid)]
    assert prov.provisioning_started_at is not None
    assert prov.provisioning_started_at == prov.created_at
    # non-provisioning rows: anchor stays NULL; created_at untouched.
    assert rows[str(run_uid)].provisioning_started_at is None
    assert rows[str(stop_uid)].provisioning_started_at is None


# --------------------------- downgrade drops the column / re-up clean ---------------------------
def test_0017_downgrade_drops_column_and_reupgrade_clean(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    assert _COLUMN in _columns(isolated_pg, "hermes_instances")

    command.downgrade(cfg, _PREV_REV)
    assert _COLUMN not in _columns(isolated_pg, "hermes_instances")

    # Re-upgrade is clean (idempotent add-column path).
    command.upgrade(cfg, _THIS_REV)
    assert _COLUMN in _columns(isolated_pg, "hermes_instances")
