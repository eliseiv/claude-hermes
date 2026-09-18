"""Integration: alembic migration 0021 (cloudpayments_webhook_events, ADR-068).

Isolated throwaway Postgres so CREATE TABLE cannot disturb the shared session container.
Verifies:
- single migration head; 0021 is on the chain (down_revision = the full 0020 id);
- the revision id fits ``alembic_version.version_num`` VARCHAR(32);
- upgrade creates the table + user_id index; downgrade drops them; re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_PREV_REV = "0020_agent_run_consumer"
_THIS_REV = "0021_cp_webhook_events"
_TABLE = "cloudpayments_webhook_events"
_INDEX = "ix_cloudpayments_webhook_events_user_id"


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


def _tables(url: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                names = await conn.run_sync(lambda sc: inspect(sc).get_table_names())
                return set(names)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _indexes(url: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                idx = await conn.run_sync(lambda sc: inspect(sc).get_indexes(_TABLE))
                return {i["name"] for i in idx if i.get("name")}
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


def test_0021_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert _THIS_REV in ancestry
    assert _PREV_REV in ancestry


def test_0021_revision_id_fits_version_num_column() -> None:
    assert len(_THIS_REV) <= 32, f"revision id {_THIS_REV!r} exceeds version_num VARCHAR(32)"


def test_0021_upgrade_creates_table_and_index(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    assert _TABLE not in _tables(isolated_pg)

    command.upgrade(cfg, _THIS_REV)
    assert _TABLE in _tables(isolated_pg)
    assert _INDEX in _indexes(isolated_pg)


def test_0021_downgrade_drops_table_reupgrade_clean(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    command.upgrade(cfg, _THIS_REV)
    command.downgrade(cfg, _PREV_REV)
    assert _TABLE not in _tables(isolated_pg)
    command.upgrade(cfg, _THIS_REV)
    assert _TABLE in _tables(isolated_pg)
