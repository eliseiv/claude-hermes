"""Integration: alembic migration 0018 (agent_runs + agent_run_status enum, ADR-064 §6).

ISOLATED throwaway Postgres container (mirrors test_migration_0017) so CREATE TYPE / CREATE TABLE
cannot disturb the shared session container. Applies BOTH directions on a REAL DB (the CU rule:
offline ``--sql`` is not enough — it never runs ``UPDATE alembic_version`` and cannot catch a
runtime ``StringDataRightTruncationError`` when a revision id exceeds the ``version_num
VARCHAR(32)`` limit). Verifies:
- single migration head; 0018 is on the chain (down_revision = the FULL 0017 revision id);
- the revision id ``0018_agent_runs`` is ≤ 32 chars (fits ``alembic_version.version_num``);
- upgrade CREATES the enum ``agent_run_status`` with EXACTLY its 6 values, the ``agent_runs`` table,
  its 3 indexes and 2 CHECK constraints; a bad status is rejected by the enum, and each CHECK
  rejects a negative value;
- downgrade DROPS the table + the enum; re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under ``asyncio.run``, which
cannot nest inside a running test loop (mirrors test_migration_0017 + the conftest _migrated
fixture).
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

_PREV_REV = "0017_hermes_provisioning"
_THIS_REV = "0018_agent_runs"
_TABLE = "agent_runs"
_ENUM = "agent_run_status"
_ENUM_VALUES = ("running", "paused", "resumed", "completed", "failed", "cancelled")
_INDEXES = ("ix_agent_runs_user_status", "ix_agent_runs_session", "ix_agent_runs_continued_from")
_CHECKS = ("ck_agent_runs_cumulative_nonneg", "ck_agent_runs_last_step_nonneg")


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


def _enum_values(url: str) -> list[str] | None:
    async def _read(conn: Any) -> list[str] | None:
        rows = await conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :name ORDER BY e.enumsortorder"
            ),
            {"name": _ENUM},
        )
        labels = [r.enumlabel for r in rows]
        return labels or None

    return asyncio.run(_run_async(url, _read))


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


def _check_constraints(url: str) -> set[str]:
    async def _read(conn: Any) -> set[str]:
        rows = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class r ON r.oid = c.conrelid "
                "WHERE r.relname = :t AND c.contype = 'c'"
            ),
            {"t": _TABLE},
        )
        return {r.conname for r in rows}

    return asyncio.run(_run_async(url, _read))


def _reset_to_prev(cfg: Any, url: str) -> None:
    from alembic import command

    async def _drop_all(conn: Any) -> None:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    asyncio.run(_run_async(url, _drop_all))
    command.upgrade(cfg, _PREV_REV)


# --------------------------- single head + revision-id length ---------------------------
def test_0018_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    # The invariant is NO FORK (a single head), NOT "0018 is the tip" — a future migration (0019+)
    # will legitimately move the tip forward. Assert 0018 is on the linear chain from the single
    # head back to base, so this test stays green as later migrations are added.
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert _THIS_REV in ancestry  # 0018 is on the chain
    assert _PREV_REV in ancestry  # 0017 is its ancestor (chain intact, full id — not short "0017")


def test_0018_revision_id_fits_version_num_column() -> None:
    # alembic_version.version_num is VARCHAR(32) by default (env.py does not raise
    # version_table_column_length). A longer revision id truncates on the UPDATE alembic_version at
    # upgrade time (asyncpg StringDataRightTruncationError). Guard the id length statically.
    assert len(_THIS_REV) <= 32, f"revision id {_THIS_REV!r} exceeds version_num VARCHAR(32)"


# --------------------------- upgrade creates enum + table + indexes + checks ------------------
def test_0018_upgrade_creates_enum_table_indexes_checks(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    # At 0017: no agent_runs table, no agent_run_status enum.
    assert _TABLE not in _tables(isolated_pg)
    assert _enum_values(isolated_pg) is None

    # Real upgrade (runs UPDATE alembic_version — proves the id fits VARCHAR(32) at runtime).
    command.upgrade(cfg, _THIS_REV)

    # Enum with EXACTLY the 6 declared values in order.
    assert _enum_values(isolated_pg) == list(_ENUM_VALUES)
    # Table + all 3 indexes + both CHECK constraints.
    assert _TABLE in _tables(isolated_pg)
    assert _indexes(isolated_pg).issuperset(set(_INDEXES))
    assert _check_constraints(isolated_pg) == set(_CHECKS)


def test_0018_enum_and_checks_are_enforced(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)

    uid = uuid.uuid4()

    async def _seed_user(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )

    asyncio.run(_run_async(isolated_pg, _seed_user))

    # A valid row inserts fine (all 6 enum values accepted).
    async def _insert_valid(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO agent_runs (run_id, user_id, session_id, status) "
                "VALUES (:r, :u, :s, CAST(:st AS agent_run_status))"
            ),
            {"r": "run_ok", "u": str(uid), "s": "sess", "st": "paused"},
        )

    asyncio.run(_run_async(isolated_pg, _insert_valid))

    # A bogus status is rejected by the enum type.
    with pytest.raises(Exception):  # noqa: B017 - asyncpg DataError / InvalidTextRepresentation
        asyncio.run(
            _run_async(
                isolated_pg,
                lambda conn: conn.execute(
                    text(
                        "INSERT INTO agent_runs (run_id, user_id, session_id, status) "
                        "VALUES (:r, :u, :s, CAST(:st AS agent_run_status))"
                    ),
                    {"r": "run_bad", "u": str(uid), "s": "sess", "st": "nonsense"},
                ),
            )
        )

    # Each CHECK rejects a negative value.
    with pytest.raises(Exception):  # noqa: B017 - CHECK violation (cumulative_credits_spent >= 0)
        asyncio.run(
            _run_async(
                isolated_pg,
                lambda conn: conn.execute(
                    text(
                        "INSERT INTO agent_runs (run_id, user_id, session_id, "
                        "cumulative_credits_spent) VALUES (:r, :u, :s, -1)"
                    ),
                    {"r": "run_neg_c", "u": str(uid), "s": "sess"},
                ),
            )
        )
    with pytest.raises(Exception):  # noqa: B017 - CHECK violation (last_billed_step >= 0)
        asyncio.run(
            _run_async(
                isolated_pg,
                lambda conn: conn.execute(
                    text(
                        "INSERT INTO agent_runs (run_id, user_id, session_id, last_billed_step) "
                        "VALUES (:r, :u, :s, -1)"
                    ),
                    {"r": "run_neg_s", "u": str(uid), "s": "sess"},
                ),
            )
        )


# --------------------------- downgrade drops table + enum / re-up clean ---------------------------
def test_0018_downgrade_drops_table_enum_and_reupgrade_clean(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    assert _TABLE in _tables(isolated_pg)
    assert _enum_values(isolated_pg) == list(_ENUM_VALUES)

    # Real downgrade (updates alembic_version back to 0017).
    command.downgrade(cfg, _PREV_REV)
    assert _TABLE not in _tables(isolated_pg)
    assert _enum_values(isolated_pg) is None  # enum dropped too

    # Re-upgrade is clean (idempotent create path, checkfirst=True on the enum).
    command.upgrade(cfg, _THIS_REV)
    assert _TABLE in _tables(isolated_pg)
    assert _enum_values(isolated_pg) == list(_ENUM_VALUES)
