"""Integration: alembic migration 0020 (ADR-067 §4/§5) — heartbeat column + active-runs index.

ISOLATED throwaway Postgres container (mirrors test_migration_0018/0019) so DDL cannot disturb the
shared session container. Both directions run against a REAL database: offline ``--sql`` never
executes ``UPDATE alembic_version`` and so cannot catch a revision id that overflows
``version_num VARCHAR(32)``.

The two properties worth more than "the objects exist":

* ``consumer_heartbeat_at`` is NULLable WITH NO SERVER DEFAULT. A ``DEFAULT now()`` would make every
  run that predates the consumer look freshly alive to the orphan sweep, which reads candidate age
  as ``COALESCE(consumer_heartbeat_at, agent_runs.created_at)``. The absence of a default is the
  feature.
* ``agent_run_snapshots`` still has exactly TWO non-PK indexes. ADR-066 §7 rejected a full index on
  a column written every few seconds per active run; putting the sweep index on the heartbeat
  column would have re-introduced exactly that, and a partial index on
  ``consumer_heartbeat_at IS NOT NULL`` does not self-clean — its predicate stays true for every row
  the consumer ever touched. The index went on ``agent_runs`` with a STATUS predicate instead,
  which does self-clean: a terminal run leaves the index for ever.

SYNC tests (no pytest-asyncio): alembic's env.py drives migrations under ``asyncio.run``, which
cannot nest inside a running test loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_PREV_REV = "0019_agent_run_snapshots"
_THIS_REV = "0020_agent_run_consumer"
_SNAPSHOTS = "agent_run_snapshots"
_RUNS = "agent_runs"
_COLUMN = "consumer_heartbeat_at"
_ACTIVE_INDEX = "ix_agent_runs_active"
# ADR-066 §7 invariant, preserved by 0020: the snapshot table keeps exactly these two.
_SNAPSHOT_INDEXES = {"ix_agent_run_snapshots_user", "ix_agent_run_snapshots_sweep"}
# The partial predicate must stay identical to the sweep's own guard or the planner ignores it.
_ACTIVE_PREDICATE_FRAGMENTS = ("running", "resumed", "status")


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


def _index_defs(url: str, table: str) -> dict[str, str]:
    async def _read(conn: Any) -> dict[str, str]:
        rows = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
            {"t": table},
        )
        return {r.indexname: r.indexdef for r in rows}

    return asyncio.run(_run_async(url, _read))


def _non_pk_indexes(url: str, table: str) -> set[str]:
    """Index names on ``table`` excluding the primary-key index."""
    return {name for name in _index_defs(url, table) if not name.endswith("_pkey")}


def _column_default(url: str, table: str, column: str) -> Any:
    async def _read(conn: Any) -> Any:
        row = await conn.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
        return row.one_or_none()

    return asyncio.run(_run_async(url, _read))


def _upgrade(cfg: Any, revision: str) -> None:
    from alembic import command

    command.upgrade(cfg, revision)


def _downgrade(cfg: Any, revision: str) -> None:
    from alembic import command

    command.downgrade(cfg, revision)


@pytest.fixture(scope="module")
def migrated(isolated_pg: str) -> Iterator[tuple[Any, str]]:
    cfg = _alembic_config(isolated_pg)
    _upgrade(cfg, _THIS_REV)
    yield cfg, isolated_pg


def test_single_head_and_linear_chain() -> None:
    """0020 must be the ONLY head and must sit directly on 0019.

    A second head is not a style problem: alembic refuses to upgrade at all, so a deploy fails after
    the image is already rolled out.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = list(script.get_heads())
    assert heads == [_THIS_REV], f"expected a single head {_THIS_REV}, got {heads}"

    revision = script.get_revision(_THIS_REV)
    assert revision.down_revision == _PREV_REV, "0020 must chain directly onto the full 0019 id"
    assert len(_THIS_REV) <= 32, "revision id must fit alembic_version.version_num VARCHAR(32)"


def test_upgrade_adds_the_heartbeat_column_nullable_without_a_default(
    migrated: tuple[Any, str],
) -> None:
    """THE property: no server default.

    With ``DEFAULT now()`` every pre-consumer run would read as freshly alive to the sweep, which
    ages candidates by ``COALESCE(consumer_heartbeat_at, agent_runs.created_at)``.
    """
    _cfg, url = migrated
    columns = _columns(url, _SNAPSHOTS)
    assert _COLUMN in columns, "0020 did not add the heartbeat column"
    assert columns[_COLUMN]["nullable"] is True

    row = _column_default(url, _SNAPSHOTS, _COLUMN)
    assert row is not None
    assert row.column_default is None, f"a server default defeats the sweep's COALESCE: {row!r}"
    assert row.is_nullable == "YES"


def test_the_heartbeat_column_is_timezone_aware(migrated: tuple[Any, str]) -> None:
    """A naive timestamp would compare wrongly against ``now()`` in the sweep predicate."""
    _cfg, url = migrated
    column = _columns(url, _SNAPSHOTS)[_COLUMN]
    assert getattr(column["type"], "timezone", False) is True


def test_upgrade_creates_the_partial_active_runs_index(migrated: tuple[Any, str]) -> None:
    """The sweep's working set: partial on STATUS (self-cleaning), leading column ``created_at``."""
    _cfg, url = migrated
    defs = _index_defs(url, _RUNS)
    assert _ACTIVE_INDEX in defs, f"{_ACTIVE_INDEX} missing; have {sorted(defs)}"
    definition = defs[_ACTIVE_INDEX].lower()
    assert "where" in definition, "the index must be PARTIAL — a full one would not self-clean"
    for fragment in _ACTIVE_PREDICATE_FRAGMENTS:
        assert fragment in definition, f"{fragment!r} missing from {definition!r}"
    assert "created_at" in definition, "candidate age is ordered by created_at, not updated_at"
    assert "updated_at" not in definition, "updated_at takes no part in the sweep"


def test_the_snapshot_table_still_has_exactly_two_non_pk_indexes(
    migrated: tuple[Any, str],
) -> None:
    """ADR-066 §7 preserved: 0020 must NOT have indexed the hot, every-30s-written column.

    A partial index on ``consumer_heartbeat_at IS NOT NULL`` would never shrink — its predicate
    stays true for every row a consumer ever touched — so it would grow with the entire run history
    while amplifying the writes of the busiest column in the contour.
    """
    _cfg, url = migrated
    assert _non_pk_indexes(url, _SNAPSHOTS) == _SNAPSHOT_INDEXES


def test_downgrade_removes_both_objects_and_re_upgrade_is_clean(
    migrated: tuple[Any, str],
) -> None:
    """A migration that cannot be rolled back and re-applied is a one-way door on a live deploy."""
    cfg, url = migrated

    _downgrade(cfg, _PREV_REV)
    assert _COLUMN not in _columns(url, _SNAPSHOTS), "downgrade left the column behind"
    assert _ACTIVE_INDEX not in _index_defs(url, _RUNS), "downgrade left the index behind"
    # 0019's own objects must survive its successor's rollback untouched.
    assert _non_pk_indexes(url, _SNAPSHOTS) == _SNAPSHOT_INDEXES

    _upgrade(cfg, _THIS_REV)
    assert _COLUMN in _columns(url, _SNAPSHOTS)
    assert _ACTIVE_INDEX in _index_defs(url, _RUNS)
    assert _non_pk_indexes(url, _SNAPSHOTS) == _SNAPSHOT_INDEXES
