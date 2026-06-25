"""Integration: alembic migration 0013 (hermes_instances table, ADR-046 §3, follow_up #11).

ISOLATED throwaway Postgres container (mirrors test_migration_0010) so CREATE/DROP TABLE cannot
disturb the shared session container. Verifies:
- single migration head; 0013 is on the chain;
- upgrade creates hermes_instances with the expected columns + status enum + index;
- the FK user_id → users(id) is ON DELETE CASCADE (deleting a user removes its instance row);
- downgrade drops the table (and enum) cleanly; re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run, which cannot
nest inside a running test loop (mirrors test_migration_0010 + the conftest _migrated fixture).
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

_PREV_REV = "0012_auth_identities"
_THIS_REV = "0013_hermes_instances"

_EXPECTED_COLUMNS = {
    "user_id",
    "container_id",
    "endpoint",
    "api_key_enc",
    "encrypted_dek",
    "nonce",
    "status",
    "port",
    "last_active_at",
    "created_at",
}


@pytest.fixture(scope="module")
def isolated_pg() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


def _alembic_config(url: str):
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


def _has_table(url: str, table: str) -> bool:
    async def _run() -> bool:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(lambda sc: inspect(sc).has_table(table))
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _indexes(url: str, table: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                idx = await conn.run_sync(lambda sc: inspect(sc).get_indexes(table))
                return {i["name"] for i in idx}
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
def test_0013_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert _THIS_REV in ancestry


# --------------------------- upgrade creates table/columns/index ---------------------------
def test_0013_upgrade_creates_table(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    assert not _has_table(isolated_pg, "hermes_instances")

    command.upgrade(cfg, _THIS_REV)

    assert _has_table(isolated_pg, "hermes_instances")
    cols = _columns(isolated_pg, "hermes_instances")
    assert _EXPECTED_COLUMNS.issubset(set(cols))
    # Encrypted material is NOT NULL.
    for col in ("api_key_enc", "encrypted_dek", "nonce"):
        assert cols[col]["nullable"] is False
    # The reaper index exists.
    assert "ix_hermes_instances_status_active" in _indexes(isolated_pg, "hermes_instances")


def test_0013_status_enum_constrains_values(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)

    uid = uuid.uuid4()

    async def _seed_bad(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )
        await conn.execute(
            text(
                "INSERT INTO hermes_instances "
                "(user_id, api_key_enc, encrypted_dek, nonce, status) "
                "VALUES (:uid, :a, :d, :n, 'bogus')"
            ),
            {"uid": str(uid), "a": b"x", "d": b"y", "n": b"z"},
        )

    with pytest.raises(Exception):  # invalid enum value rejected by the DB
        asyncio.run(_run_async(isolated_pg, _seed_bad))


# --------------------------- FK ON DELETE CASCADE (follow_up #11) ---------------------------
def test_0013_fk_on_delete_cascade(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    uid = uuid.uuid4()

    async def _seed(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )
        await conn.execute(
            text(
                "INSERT INTO hermes_instances "
                "(user_id, api_key_enc, encrypted_dek, nonce, status) "
                "VALUES (:uid, :a, :d, :n, 'running')"
            ),
            {"uid": str(uid), "a": b"enc", "d": b"dek", "n": b"non"},
        )

    asyncio.run(_run_async(isolated_pg, _seed))

    async def _delete_user(conn: Any) -> Any:
        await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(uid)})
        return await conn.scalar(
            text("SELECT count(*) FROM hermes_instances WHERE user_id = :uid"), {"uid": str(uid)}
        )

    remaining = asyncio.run(_run_async(isolated_pg, _delete_user))
    assert remaining == 0  # CASCADE removed the instance row


# --------------------------- downgrade drops table / re-up clean ---------------------------
def test_0013_downgrade_drops_table_and_reupgrade_clean(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    assert _has_table(isolated_pg, "hermes_instances")

    command.downgrade(cfg, _PREV_REV)
    assert not _has_table(isolated_pg, "hermes_instances")

    # Re-upgrade is clean (enum was dropped on downgrade, recreated on upgrade).
    command.upgrade(cfg, _THIS_REV)
    assert _has_table(isolated_pg, "hermes_instances")
