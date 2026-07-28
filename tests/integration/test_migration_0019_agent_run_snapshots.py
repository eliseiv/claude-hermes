"""Integration: alembic migration 0019 (agent_run_snapshots, ADR-066 §2 / 03-data-model.md §25).

ISOLATED throwaway Postgres container (mirrors test_migration_0018) so CREATE TABLE cannot disturb
the shared session container. Applies BOTH directions on a REAL DB — offline ``--sql`` is not
enough: it never runs ``UPDATE alembic_version`` and cannot catch a runtime
``StringDataRightTruncationError`` when a revision id exceeds ``version_num VARCHAR(32)``.

Verifies (agent-proxy/09-testing.md §Миграция 0019):
- a single migration head; 0019 is on the linear chain and 0018 is its ancestor (full revision id);
- the revision id fits ``alembic_version.version_num``;
- upgrade creates the table + BOTH indexes + BOTH CHECK constraints, and the CHECKs are enforced;
- the enum ``agent_run_status`` is NOT modified (``waiting_approval`` is derived on READ, ADR-066
  §4 — a DB enum migration would have been the expensive alternative);
- the FK CASCADEs from ``agent_runs`` and from ``users`` both work;
- downgrade drops the table (leaving 0018's objects intact) and re-upgrade is clean.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under ``asyncio.run``, which
cannot nest inside a running test loop (mirrors test_migration_0018).
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

_PREV_REV = "0018_agent_runs"
_THIS_REV = "0019_agent_run_snapshots"
_TABLE = "agent_run_snapshots"
_ENUM = "agent_run_status"
# ADR-066 §2: the enum is NOT extended by this migration — exactly the 6 values of 0018.
_ENUM_VALUES = ("running", "paused", "resumed", "completed", "failed", "cancelled")
# Exactly two secondary indexes: the owner scope, and a PARTIAL index serving the retention sweep.
# The sweep index is partial rather than a plain (updated_at) one because the table is upserted
# every few seconds per active run — a second FULL index on the hot column would amplify every
# write, while the partial one holds ONLY rows that still carry content (so a swept-clean history
# costs nothing per reaper tick and the index shrinks as rows are cleared).
_INDEXES = ("ix_agent_run_snapshots_user", "ix_agent_run_snapshots_sweep")
_SWEEP_INDEX = "ix_agent_run_snapshots_sweep"
# The partial predicate MUST stay identical to the sweep statement's guard for the planner to use
# the index (snapshots_repo.sweep_expired).
_SWEEP_PREDICATE_FRAGMENTS = ("result_text", "<>", "pending_approval", "IS NOT NULL")
# A plain full index on updated_at must NOT exist: it was REPLACED by the partial one, not added to.
_REPLACED_INDEX = "ix_agent_run_snapshots_updated_at"
_CHECKS = ("ck_agent_run_snapshots_input_nonneg", "ck_agent_run_snapshots_output_nonneg")


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


def _index_defs(url: str) -> dict[str, str]:
    """name -> the full ``CREATE INDEX`` text as Postgres reconstructs it (carries the WHERE)."""

    async def _read(conn: Any) -> dict[str, str]:
        rows = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
            {"t": _TABLE},
        )
        return {r.indexname: r.indexdef for r in rows}

    return asyncio.run(_run_async(url, _read))


def _check_constraints(url: str) -> set[str]:
    async def _read(conn: Any) -> set[str]:
        rows = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
                "WHERE r.relname = :t AND c.contype = 'c'"
            ),
            {"t": _TABLE},
        )
        return {r.conname for r in rows}

    return asyncio.run(_run_async(url, _read))


def _enum_values(url: str) -> list[str] | None:
    async def _read(conn: Any) -> list[str] | None:
        rows = await conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :name ORDER BY e.enumsortorder"
            ),
            {"name": _ENUM},
        )
        labels = [r.enumlabel for r in rows]
        return labels or None

    return asyncio.run(_run_async(url, _read))


def _reset_to_prev(cfg: Any, url: str) -> None:
    from alembic import command

    async def _drop_all(conn: Any) -> None:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    asyncio.run(_run_async(url, _drop_all))
    command.upgrade(cfg, _PREV_REV)


def _seed_user_and_run(url: str, uid: uuid.UUID, run_id: str = "run_1") -> None:
    async def _seed(conn: Any) -> None:
        await conn.execute(
            text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
        )
        await conn.execute(
            text("INSERT INTO agent_runs (run_id, user_id, session_id) VALUES (:r, :u, 'sess')"),
            {"r": run_id, "u": str(uid)},
        )

    asyncio.run(_run_async(url, _seed))


def _insert_snapshot(url: str, uid: uuid.UUID, run_id: str = "run_1", **cols: Any) -> None:
    async def _insert(conn: Any) -> None:
        await conn.execute(
            text(
                "INSERT INTO agent_run_snapshots (run_id, user_id, result_text, input_tokens, "
                "output_tokens) VALUES (:r, :u, :txt, :i, :o)"
            ),
            {
                "r": run_id,
                "u": str(uid),
                "txt": cols.get("result_text", "t"),
                "i": cols.get("input_tokens", 0),
                "o": cols.get("output_tokens", 0),
            },
        )

    asyncio.run(_run_async(url, _insert))


def _snapshot_count(url: str) -> int:
    async def _read(conn: Any) -> int:
        row = await conn.execute(text(f"SELECT count(*) AS c FROM {_TABLE}"))
        return int(row.one().c)

    return asyncio.run(_run_async(url, _read))


# --------------------------- single head + revision-id length ---------------------------
def test_0019_single_head_and_chain() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    # The invariant is NO FORK, not "0019 is the tip" — a later migration may move the tip on.
    assert len(heads) == 1, f"expected a single migration head (no fork), got {heads}"
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert _THIS_REV in ancestry
    assert _PREV_REV in ancestry  # chain intact via the FULL 0018 id (not a short "0018")
    assert script.get_revision(_THIS_REV).down_revision == _PREV_REV


def test_0019_revision_id_fits_version_num_column() -> None:
    # alembic_version.version_num is VARCHAR(32) (env.py does not raise
    # version_table_column_length): a longer id truncates at upgrade time (asyncpg
    # StringDataRightTruncationError).
    assert len(_THIS_REV) <= 32, f"revision id {_THIS_REV!r} exceeds version_num VARCHAR(32)"


# --------------------------- upgrade creates table + indexes + checks ---------------------------
def test_0019_upgrade_creates_table_indexes_checks(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    assert _TABLE not in _tables(isolated_pg)
    # 0018 objects are already there (the chain is intact).
    assert "agent_runs" in _tables(isolated_pg)

    command.upgrade(cfg, _THIS_REV)  # real upgrade (also runs UPDATE alembic_version)

    assert _TABLE in _tables(isolated_pg)
    assert _indexes(isolated_pg).issuperset(set(_INDEXES))
    assert _check_constraints(isolated_pg) == set(_CHECKS)

    # Exactly TWO secondary indexes (plus the PK): the sweep index REPLACED the plain
    # (updated_at) one — an extra full index on the hot column would amplify every upsert.
    defs = _index_defs(isolated_pg)
    secondary = {n for n in defs if not n.endswith("_pkey")}
    assert secondary == set(_INDEXES), secondary
    assert _REPLACED_INDEX not in defs, "the plain updated_at index must be REPLACED, not kept"


def test_0019_sweep_index_is_partial_on_the_sweep_predicate(isolated_pg: str) -> None:
    # The index must be PARTIAL and its predicate must match the sweep statement's content guard —
    # a plain index would make every reaper tick walk the whole tail of history older than the TTL
    # only to discard it, and would cost write amplification on a table upserted every few seconds.
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    indexdef = _index_defs(isolated_pg)[_SWEEP_INDEX]
    assert " WHERE " in indexdef, f"sweep index is not partial: {indexdef}"
    assert "updated_at" in indexdef
    for fragment in _SWEEP_PREDICATE_FRAGMENTS:
        assert fragment in indexdef, f"{fragment!r} missing from the partial predicate: {indexdef}"
    # The owner index stays a plain one (no predicate) — it serves arbitrary owner lookups.
    assert " WHERE " not in _index_defs(isolated_pg)["ix_agent_run_snapshots_user"]


def test_0019_sweep_index_predicate_matches_the_repository_statement(isolated_pg: str) -> None:
    # Lock-step guard: the partial predicate and the sweep's own guard must not drift apart, or the
    # planner silently stops using the index (the sweep keeps working, just slower and unnoticed).
    import inspect as _inspect

    from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository

    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)

    source = _inspect.getsource(AgentRunSnapshotsRepository.sweep_expired)
    assert "result_text <> '' OR pending_approval IS NOT NULL" in source, (
        "the sweep guard changed — update the partial index predicate in migration 0019 and the "
        "ORM Index(postgresql_where=...) in models/tables.py together with it"
    )
    indexdef = _index_defs(isolated_pg)[_SWEEP_INDEX]
    # Postgres normalises the predicate (quoting/parens), so compare on the semantic tokens.
    normalised = indexdef.replace("(", " ").replace(")", " ").replace("'", "")
    assert "result_text <>" in normalised.replace("::text", "")
    assert "pending_approval IS NOT NULL" in normalised


def test_0019_does_not_change_the_agent_run_status_enum(isolated_pg: str) -> None:
    # ADR-066 §2/§4: `waiting_approval` is DERIVED on read; the enum must stay exactly as 0018 left
    # it (a DB enum migration is more expensive than a mapping).
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    before = _enum_values(isolated_pg)
    command.upgrade(cfg, _THIS_REV)
    after = _enum_values(isolated_pg)
    assert before == list(_ENUM_VALUES)
    assert after == before
    assert "waiting_approval" not in (after or [])


def test_0019_checks_are_enforced(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    uid = uuid.uuid4()
    _seed_user_and_run(isolated_pg, uid)

    # A valid row inserts fine (defaults fill result_text/tokens/updated_at).
    _insert_snapshot(isolated_pg, uid)
    assert _snapshot_count(isolated_pg) == 1

    for column in ("input_tokens", "output_tokens"):
        with pytest.raises(Exception):  # noqa: B017 - CHECK violation (>= 0)
            asyncio.run(
                _run_async(
                    isolated_pg,
                    lambda conn, c=column: conn.execute(  # type: ignore[misc]
                        text(
                            "INSERT INTO agent_run_snapshots (run_id, user_id, "
                            f"{c}) VALUES ('run_1', :u, -1)"
                        ),
                        {"u": str(uid)},
                    ),
                )
            )


def test_0019_run_id_is_pk_and_fk_to_agent_runs(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    uid = uuid.uuid4()
    _seed_user_and_run(isolated_pg, uid)
    _insert_snapshot(isolated_pg, uid)

    # 1:1 — a second snapshot for the same run violates the PK.
    with pytest.raises(Exception):  # noqa: B017 - PK violation
        _insert_snapshot(isolated_pg, uid)
    # A snapshot for an unknown run violates the FK to agent_runs.
    with pytest.raises(Exception):  # noqa: B017 - FK violation
        _insert_snapshot(isolated_pg, uid, run_id="run_unknown")


def test_0019_fk_cascade_from_agent_runs_and_users(isolated_pg: str) -> None:
    # ADR-066 §2: the snapshot never outlives its lifecycle row, and deleting a user wipes the
    # stored model text (the mitigation for keeping user content at rest).
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    uid = uuid.uuid4()
    _seed_user_and_run(isolated_pg, uid, run_id="run_a")
    _insert_snapshot(isolated_pg, uid, run_id="run_a")
    assert _snapshot_count(isolated_pg) == 1

    # CASCADE from agent_runs.
    asyncio.run(
        _run_async(
            isolated_pg,
            lambda conn: conn.execute(text("DELETE FROM agent_runs WHERE run_id='run_a'")),
        )
    )
    assert _snapshot_count(isolated_pg) == 0

    # CASCADE from users.
    _seed_user_and_run(isolated_pg, uid2 := uuid.uuid4(), run_id="run_b")
    _insert_snapshot(isolated_pg, uid2, run_id="run_b")
    assert _snapshot_count(isolated_pg) == 1
    asyncio.run(
        _run_async(
            isolated_pg,
            lambda conn: conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": str(uid2)}),
        )
    )
    assert _snapshot_count(isolated_pg) == 0


# --------------------------- downgrade / re-upgrade ---------------------------
def test_0019_downgrade_drops_table_keeps_0018_and_reupgrade_clean(isolated_pg: str) -> None:
    cfg = _alembic_config(isolated_pg)
    _reset_to_prev(cfg, isolated_pg)
    from alembic import command

    command.upgrade(cfg, _THIS_REV)
    assert _TABLE in _tables(isolated_pg)

    command.downgrade(cfg, _PREV_REV)  # real downgrade (updates alembic_version back to 0018)
    assert _TABLE not in _tables(isolated_pg)
    # Expand-only: 0018's objects are untouched by the rollback.
    assert "agent_runs" in _tables(isolated_pg)
    assert _enum_values(isolated_pg) == list(_ENUM_VALUES)

    command.upgrade(cfg, _THIS_REV)
    assert _TABLE in _tables(isolated_pg)
    assert _indexes(isolated_pg).issuperset(set(_INDEXES))
