"""Integration: the orphan sweep (ADR-067 §5, stage 5) — real Postgres, real Redis.

WHY THIS MODULE GETS THE MOST CARE OF THE ADR-067 SET. Every other stage can be wrong and be fixed
by a retry; this one is IRREVERSIBLE in both of its writes:

* the debit goes out under ``idempotency_key=runId`` — the same key the consumer's own finalization
  would use — computed from whatever incomplete cumulative the snapshot happened to hold. Once it
  lands, the real finalization is a no-op, so an early sweep does not merely charge wrongly, it
  PERMANENTLY replaces the correct charge with a smaller one;
* the status becomes ``failed`` through a CONDITIONAL update, so the genuine terminal transition
  afterwards updates zero rows. The run is recorded as failed for ever, having actually succeeded.

Hence every test here is about NOT firing. The three conditions are tested by removing one at a
time — the assertion that matters is that any single missing condition is enough to spare the run,
because that is what "all three" actually means and what a plausible refactor would erode.

The sweep also reads an unknown lease the OPPOSITE way to the broker: there ``None`` must not close
a client stream, here ``None`` must not finalize a run. Both are fail-closed with respect to their
own cost, and the pair is asserted explicitly so a future "let's make lease_alive consistent" change
has to confront the asymmetry rather than discover it.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from httpx import AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.consumer import sweep_orphan_runs
from app.agent_proxy.transport import AgentRunEventBus, LeaseAcquisition, url_with_db
from app.config import Settings
from app.deps import get_agent_proxy_service_for
from tests.conftest import seed_user

_GRACE = 2
_ORPHAN_TIMEOUT = 60


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def sweep_env(
    redis_url: str, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Any]:
    """A sweep wired to a real DB and its own Redis logical DB."""
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(**overrides: Any) -> tuple[Any, AgentRunEventBus, Settings]:
        db = counter["n"]
        counter["n"] += 1
        base: dict[str, Any] = {
            "REDIS_URL": redis_url,
            "AGENT_RUN_REDIS_DB": db,
            "AGENT_RUN_ORPHAN_TIMEOUT_SECONDS": _ORPHAN_TIMEOUT,
            "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS": _GRACE,
            "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS": 30,
            "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS": 1,
            "AGENT_RUN_ORPHAN_MAX_PER_TICK": 20,
        }
        base.update(overrides)
        settings = Settings(**base)  # type: ignore[arg-type]
        client = redis_asyncio.from_url(
            url_with_db(redis_url, db), decode_responses=True, socket_timeout=5
        )
        clients.append(client)
        bus = AgentRunEventBus(client, settings)

        @asynccontextmanager
        async def services() -> AsyncIterator[Any]:
            async with db_sessionmaker() as session:
                yield get_agent_proxy_service_for(session)

        return services, bus, settings

    yield _make

    for client in clients:
        try:
            await client.flushdb()
            await client.aclose()
        except RedisError:  # pragma: no cover - teardown best effort
            pass


async def _seed_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    status: str = "running",
    age_seconds: float = 0.0,
    balance: int = 1000,
    heartbeat_age_seconds: float | None = None,
    snapshot: bool = True,
    input_tokens: int = 2000,
    output_tokens: int = 1000,
    updated_at_age_seconds: float | None = None,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert an agent_runs row (+ optional snapshot) with fully controlled timestamps."""
    now = datetime.datetime.now(datetime.UTC)
    async with maker() as session:
        uid = user_id or await seed_user(session, subscription="active", balance=balance)
        await session.execute(
            text(
                "INSERT INTO agent_runs (run_id, user_id, session_id, status, model, created_at) "
                "VALUES (:r, :u, 'sess-1', CAST(:st AS agent_run_status), 'm', :created)"
            ),
            {
                "r": run_id,
                "u": str(uid),
                "st": status,
                "created": now - datetime.timedelta(seconds=age_seconds),
            },
        )
        if snapshot:
            heartbeat = (
                None
                if heartbeat_age_seconds is None
                else now - datetime.timedelta(seconds=heartbeat_age_seconds)
            )
            updated = (
                now
                if updated_at_age_seconds is None
                else now - datetime.timedelta(seconds=updated_at_age_seconds)
            )
            await session.execute(
                text(
                    "INSERT INTO agent_run_snapshots (run_id, user_id, result_text, input_tokens, "
                    "output_tokens, consumer_heartbeat_at, updated_at) "
                    "VALUES (:r, :u, 'partial', :i, :o, :hb, :upd)"
                ),
                {
                    "r": run_id,
                    "u": str(uid),
                    "i": input_tokens,
                    "o": output_tokens,
                    "hb": heartbeat,
                    "upd": updated,
                },
            )
        await session.commit()
    return uid


async def _status(maker: async_sessionmaker[AsyncSession], run_id: str) -> str:
    async with maker() as session:
        return str(
            (
                await session.execute(
                    text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id}
                )
            ).scalar_one()
        )


async def _debits(maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> list[tuple]:
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT idempotency_key, amount FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit' ORDER BY created_at, id"
                ),
                {"u": str(user_id)},
            )
        ).all()
    return [(r.idempotency_key, r.amount) for r in rows]


async def _wait_for_grace(bus: AgentRunEventBus) -> None:
    """Let the Redis container's reported uptime exceed the configured grace."""
    import asyncio

    for _ in range(60):
        uptime = await bus.uptime_seconds()
        if uptime is not None and uptime >= _GRACE:
            return
        await asyncio.sleep(0.5)
    raise AssertionError("redis uptime never exceeded the grace period")  # pragma: no cover


# ==================================================================================================
# The happy path — so the negatives below cannot pass by never sweeping anything.
# ==================================================================================================
@pytest.mark.asyncio
async def test_all_three_conditions_met_finalizes_the_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """Stale heartbeat + no lease + Redis up past the grace → charge and mark failed.

    2000 in + 1000 out at 1.0/5.0 per 1k = 2 + 5 = 7 credits, under the BARE runId key — the same
    key the consumer's own finalization uses, which is what makes the two mutually idempotent.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    assert await _status(db_sessionmaker, run_id) == "failed"
    assert await _debits(db_sessionmaker, uid) == [(run_id, 7)]


# ==================================================================================================
# THE core of the module: each condition ALONE is enough to spare the run.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_live_lease_alone_spares_the_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """Condition 1 removed: heartbeat is stale and Redis is up, but somebody holds the lease.

    A working consumer whose heartbeat writes are merely slow or contended must never be finalized —
    that is the case where the sweep charges a live run from a partial cumulative and then locks it
    into ``failed``.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)
    assert await bus.acquire_lease(run_id, "a-live-consumer") is LeaseAcquisition.ACQUIRED

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert await _status(db_sessionmaker, run_id) == "running"
    assert await _debits(db_sessionmaker, uid) == []


@pytest.mark.asyncio
async def test_a_fresh_heartbeat_alone_spares_the_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """Condition 2 removed: no lease at all, Redis is up, but the consumer beat recently.

    "No lease" on its own is emphatically not enough — a Redis restart wipes every lease at once,
    and this is the shape of the mass finalization that would follow.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=1)
    await _wait_for_grace(bus)
    assert await bus.lease_alive(run_id) is False

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert await _status(db_sessionmaker, run_id) == "running"
    assert await _debits(db_sessionmaker, uid) == []


@pytest.mark.asyncio
async def test_an_unmet_redis_grace_skips_the_entire_tick(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """Condition 3 removed: a run that satisfies conditions 1 and 2 is still spared.

    Immediately after a Redis restart every lease is gone, so without this the very first tick would
    take every active run for an orphan and finalize the lot.
    """
    services, bus, settings = sweep_env(AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS=86_400)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert await _status(db_sessionmaker, run_id) == "running"
    assert await _debits(db_sessionmaker, uid) == []


@pytest.mark.asyncio
async def test_an_unreadable_redis_info_skips_the_entire_tick(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any, redis_url: str
) -> None:
    """``INFO`` unavailable ⇒ NO sweep at all, rather than "sweep anyway".

    Uptime unknown means the grace cannot be evaluated, and the grace is the only thing standing
    between a Redis restart and a mass finalization. Note the direction: unknown here disables the
    mechanism, whereas on the broker's side unknown keeps the stream open. Both are fail-closed with
    respect to their own cost.
    """
    settings = Settings(  # type: ignore[call-arg]
        REDIS_URL=redis_url,
        AGENT_RUN_REDIS_DB=9,
        AGENT_RUN_ORPHAN_TIMEOUT_SECONDS=_ORPHAN_TIMEOUT,
        AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS=_GRACE,
        # The grace must stay above one renew period (config invariant), so the renew knob has to
        # come down with it — the defaults would reject this combination at construction.
        AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=1,
        AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS=30,
    )
    dead = redis_asyncio.from_url("redis://127.0.0.1:1/9", socket_connect_timeout=1)
    bus = AgentRunEventBus(dead, settings)
    assert await bus.uptime_seconds() is None

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)

    called: list[str] = []

    @asynccontextmanager
    async def services() -> AsyncIterator[Any]:
        called.append("opened")  # pragma: no cover - must never happen
        async with db_sessionmaker() as session:
            yield get_agent_proxy_service_for(session)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert called == [], "the tick queried the database despite an unknowable uptime"
    assert await _status(db_sessionmaker, run_id) == "running"
    assert await _debits(db_sessionmaker, uid) == []
    await dead.aclose()


@pytest.mark.asyncio
async def test_an_unknown_lease_does_not_finalize(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """``lease_alive`` → ``None`` skips THAT run — ``is not False``, not ``if not``.

    This is the asymmetry with the broker stated as a test: there ``None`` must not close a client
    stream, here ``None`` must not finalize a run. A refactor that "unified" the two readings would
    break exactly one of them, and this is the side where being wrong cannot be undone.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)

    async def _unknown(_run_id: str) -> bool | None:
        return None

    bus.lease_alive = _unknown  # type: ignore[method-assign]

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert await _status(db_sessionmaker, run_id) == "running"
    assert await _debits(db_sessionmaker, uid) == []


# ==================================================================================================
# Candidate selection: what makes a run OLD, and what must not.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_run_without_a_snapshot_is_aged_by_created_at(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """The consumer never started at all: no snapshot row, so no heartbeat to go stale.

    Without the ``COALESCE(..., created_at)`` fallback such a run could never become a candidate and
    would sit ``running`` for ever — which is the exact class TD-037 reported.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, snapshot=False, age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1
    assert await _status(db_sessionmaker, run_id) == "failed"
    # No snapshot ⇒ no observed usage ⇒ nothing to charge. The status still has to be recorded.
    assert await _debits(db_sessionmaker, uid) == []


@pytest.mark.asyncio
async def test_a_fresh_updated_at_does_not_protect_a_stale_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """REGRESSION on the age formula: ``updated_at`` must take NO part in selection.

    It moves on billing and status writes, neither of which is evidence that anybody is still
    CONSUMING the run — a run can be billed by a finalization while its consumer is long dead. If
    the formula ever used ``updated_at``, an orphan would keep looking fresh and never be finalized,
    which is the failure this whole stage exists to prevent.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(
        db_sessionmaker,
        run_id,
        heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30,
        updated_at_age_seconds=0,  # touched a moment ago
    )
    await _wait_for_grace(bus)

    assert (
        await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1
    ), "a fresh updated_at hid a stale run from the sweep — updated_at is in the age formula"
    assert await _status(db_sessionmaker, run_id) == "failed"
    assert await _debits(db_sessionmaker, uid) == [(run_id, 7)]


@pytest.mark.asyncio
async def test_a_stale_heartbeat_beats_a_recent_created_at(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """COALESCE order: when a heartbeat EXISTS it is authoritative, even for a young run."""
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(
        db_sessionmaker, run_id, age_seconds=0, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30
    )
    await _wait_for_grace(bus)
    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1


@pytest.mark.asyncio
async def test_a_terminal_run_is_never_a_candidate(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """Only ``running``/``resumed`` are in the working set — the partial index predicate."""
    services, bus, settings = sweep_env()
    await _wait_for_grace(bus)
    for status in ("completed", "failed", "cancelled", "paused"):
        run_id = f"run_{status}_{uuid.uuid4().hex[:6]}"
        await _seed_run(
            db_sessionmaker,
            run_id,
            status=status,
            heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30,
        )
    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0


# ==================================================================================================
# Idempotency: a second tick must cost nothing.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_second_tick_is_a_no_op(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """Both writes are idempotent/conditional, so a repeated tick must not double-charge.

    After the first tick the run is ``failed`` and therefore out of the working set entirely; the
    debit key would also refuse a second charge. Both layers are asserted, because either alone
    would let the other rot unnoticed.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1
    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert await _debits(db_sessionmaker, uid) == [(run_id, 7)]
    assert await _status(db_sessionmaker, run_id) == "failed"


@pytest.mark.asyncio
async def test_the_sweep_cannot_overwrite_a_run_that_finished_meanwhile(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """The conditional status write, from the other direction.

    A run that reached ``completed`` between candidate selection and finalization must keep that
    status. An unconditional UPDATE here would rewrite a successful run as ``failed`` for ever.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)

    # The age is taken from the candidate query, not invented: `heartbeat_age_seconds` has no
    # default on purpose, because a placeholder would put a number in the audit trail that nothing
    # ever measured. This call is a direct one, so it must do what the sweep does — ask.
    async with db_sessionmaker() as session:
        candidates = await get_agent_proxy_service_for(session).list_orphan_candidates(
            timeout_seconds=_ORPHAN_TIMEOUT, limit=20
        )
    row = next(c for c in candidates if str(c["run_id"]) == run_id)
    measured_age = float(row["heartbeat_age_seconds"])
    assert measured_age >= _ORPHAN_TIMEOUT, "the candidate query did not report a stale age"

    async with db_sessionmaker() as session:
        service = get_agent_proxy_service_for(session)
        # The run finishes normally while the sweep is between its two steps.
        async with db_sessionmaker() as other:
            await other.execute(
                text("UPDATE agent_runs SET status='completed' WHERE run_id=:r"),
                {"r": run_id},
            )
            await other.commit()
        await service.finalize_orphan_run(
            user_id=uid,
            run_id=run_id,
            input_tokens=2000,
            output_tokens=1000,
            snapshot_present=True,
            heartbeat_age_seconds=measured_age,
        )

    assert (
        await _status(db_sessionmaker, run_id) == "completed"
    ), "the sweep overwrote a run that had already finished successfully"


@pytest.mark.asyncio
async def test_the_per_tick_cap_bounds_the_batch(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """A pathological backlog must not be finalized in one go — oldest first, capped."""
    services, bus, settings = sweep_env(AGENT_RUN_ORPHAN_MAX_PER_TICK=2)
    await _wait_for_grace(bus)
    for i in range(5):
        await _seed_run(
            db_sessionmaker,
            f"run_cap_{i}_{uuid.uuid4().hex[:6]}",
            heartbeat_age_seconds=_ORPHAN_TIMEOUT + 100 - i,
        )

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 2


# ==================================================================================================
# The heartbeat's SHAPE (stage 2б). It is the sweep's only input about liveness, so what it does
# — and does not — touch decides whether the sweep can be trusted at all.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_heartbeat_moves_one_column_and_leaves_updated_at_alone(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """``consumer_heartbeat_at = now()`` and NOTHING else.

    ``updated_at`` is the CLIENT's staleness detector (``/state.updatedAt``), while a heartbeat is
    written every 30s whether or not anything about the run changed. Moving both would tell the
    client the state advanced when it did not — an upsert refreshing ``updated_at`` unconditionally
    is the natural implementation and the wrong one here, which is why the repository has a bespoke
    single-column UPDATE rather than reusing the snapshot upsert.
    """
    from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=300, updated_at_age_seconds=300)

    async with db_sessionmaker() as session:
        before = (
            await session.execute(
                text(
                    "SELECT consumer_heartbeat_at, updated_at, result_text, input_tokens, "
                    "output_tokens FROM agent_run_snapshots WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one()

    async with db_sessionmaker() as session:
        rows = await AgentRunSnapshotsRepository(session).touch_consumer_heartbeat(run_id)
        await session.commit()
    assert rows == 1

    async with db_sessionmaker() as session:
        after = (
            await session.execute(
                text(
                    "SELECT consumer_heartbeat_at, updated_at, result_text, input_tokens, "
                    "output_tokens FROM agent_run_snapshots WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one()

    assert after.consumer_heartbeat_at > before.consumer_heartbeat_at, "the heartbeat did not move"
    assert (
        after.updated_at == before.updated_at
    ), "the heartbeat moved updated_at — the client would read a state change that never happened"
    # Nothing else may be disturbed either: this must not be an upsert in disguise.
    assert (after.result_text, after.input_tokens, after.output_tokens) == (
        before.result_text,
        before.input_tokens,
        before.output_tokens,
    )


@pytest.mark.asyncio
async def test_the_heartbeat_never_inserts_a_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No snapshot row ⇒ nothing to stamp, and the sweep ages that run by ``created_at`` instead.

    An INSERT here would give every consumer-less run a fresh heartbeat and hide it from the sweep
    for ever — the precise failure the ``created_at`` fallback exists to catch.
    """
    from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, snapshot=False)

    async with db_sessionmaker() as session:
        rows = await AgentRunSnapshotsRepository(session).touch_consumer_heartbeat(run_id)
        await session.commit()
    assert rows == 0

    async with db_sessionmaker() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM agent_run_snapshots WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()
    assert count == 0, "the heartbeat created a snapshot row"


@pytest.mark.asyncio
async def test_a_beating_consumer_keeps_its_run_out_of_the_sweep(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """End to end between the two halves: the heartbeat is what actually saves a live run.

    Both sides are exercised for real — the repository writes the column, the sweep reads it — so a
    change to either that silently stopped agreeing with the other would surface here rather than in
    production, where the symptom is a working run charged and marked failed.
    """
    from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository

    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)

    # The consumer beats once, just in time.
    async with db_sessionmaker() as session:
        await AgentRunSnapshotsRepository(session).touch_consumer_heartbeat(run_id)
        await session.commit()

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 0
    assert await _status(db_sessionmaker, run_id) == "running"
    assert await _debits(db_sessionmaker, uid) == []


# ==================================================================================================
# TD-042 — the sweep charges the REMAINDER (ADR-067 §5 step 1). The money path.
# ==================================================================================================
async def _step_debit(
    maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, key: str, amount: int
) -> None:
    """A per-step incremental debit exactly as ADR-064 §1 writes one: key ``runId:<step>``."""
    async with maker() as session:
        wallet = get_agent_proxy_service_for(session)._wallet
        await wallet.consume(
            user_id=user_id,
            amount=amount,
            idempotency_key=key,
            meta={"source": "agent_run", "incremental": True},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_the_sweep_charges_only_the_remainder_of_an_incrementally_billed_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """TD-042: with incremental billing ON, the run must not be billed a SECOND time in full.

    The idempotency key protects nothing here, and that is the whole defect. Incremental billing
    debits under ``runId:<step>`` keys, so the bare ``runId`` key the sweep uses is still FREE when
    it arrives — a full-amount debit sails through on top of every step already paid, and the run is
    charged twice over. The flag defaulting to false was the condition for the defect to stay
    latent, never a mitigation.

    Arithmetic, all of it visible: the snapshot's cumulative 2000 in / 1000 out is 2 + 5 = 7 credits
    observed; steps 1 and 2 already took 3 + 2 = 5; the remainder is 2.

    ⚠️ On the pre-TD-042 implementation the last assertion reads ``(run_id, 7)`` — the run pays 12
    for 7 credits of work.
    """
    services, bus, settings = sweep_env(AGENT_INCREMENTAL_BILLING_ENABLED=True)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _step_debit(db_sessionmaker, uid, f"{run_id}:1", 3)
    await _step_debit(db_sessionmaker, uid, f"{run_id}:2", 2)
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    assert await _status(db_sessionmaker, run_id) == "failed"
    assert await _debits(db_sessionmaker, uid) == [
        (f"{run_id}:1", 3),
        (f"{run_id}:2", 2),
        (run_id, 2),
    ], "the sweep re-billed steps that had already been paid for"


@pytest.mark.asyncio
async def test_a_fully_paid_run_is_finalized_without_any_further_debit(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """The boundary of the same rule: steps already cover the observed usage ⇒ remainder 0.

    ``max(observed - charged, 0)`` must clamp rather than write a zero or negative debit — the
    latter would raise out of ``consume`` and cost the run its terminal status, turning an
    over-billing bug into a permanently ``running`` run.
    """
    services, bus, settings = sweep_env(AGENT_INCREMENTAL_BILLING_ENABLED=True)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _step_debit(db_sessionmaker, uid, f"{run_id}:1", 9)  # more than the observed 7
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    assert await _status(db_sessionmaker, run_id) == "failed", "the clamp cost the run its status"
    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:1", 9)], "a spurious extra debit"


@pytest.mark.asyncio
async def test_a_similarly_named_run_does_not_absorb_this_ones_charges(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """The subtraction reads the LEDGER by key prefix, so neighbouring run ids must not bleed in.

    ``charged_for_run`` matches ``runId`` OR ``runId:%``. A run whose id is a prefix of another's
    would otherwise see the neighbour's steps as its own and under-charge — silently, since the
    result is a smaller debit rather than an error.
    """
    services, bus, settings = sweep_env(AGENT_INCREMENTAL_BILLING_ENABLED=True)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    neighbour = f"{run_id}x"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _step_debit(db_sessionmaker, uid, f"{neighbour}:1", 5)
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    assert await _debits(db_sessionmaker, uid) == [
        (f"{neighbour}:1", 5),
        (run_id, 7),
    ], "the neighbour's per-step debit was counted as this run's, under-charging it"


# ==================================================================================================
# ADR-067 §5.2 — three grounds for a zero, told apart in the audit AND in the metric.
# ==================================================================================================
async def _audit_payload(maker: async_sessionmaker[AsyncSession], run_id: str) -> dict:
    async with maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT payload FROM audit_logs WHERE event_type='agent_run_orphan_finalized' "
                    "AND payload->>'runId' = :r"
                ),
                {"r": run_id},
            )
        ).scalar_one()
    return dict(row)


def _basis_count(basis: str) -> float:
    from app.observability.metrics import agent_run_orphan_finalized_total

    return agent_run_orphan_finalized_total.labels(basis=basis)._value.get()


@pytest.mark.asyncio
async def test_the_audit_records_what_the_run_was_worth_not_only_what_this_write_took(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """``billed=0`` under ``basis="snapshot"`` has two causes, and the payload must separate them.

    Since the remainder rule (TD-042) landed, ``billed`` is what THIS write took, not what the run
    was worth. So a run fully paid step by step and a run whose snapshot writes were LOST both
    surface as ``billed=0, billingBasis="snapshot"`` — and ``billingBasis``, which §5.2 introduced
    precisely to stop a zero from being ambiguous, no longer separates them on its own. Only
    ``observed`` and ``alreadyCharged`` do.

    Both are exercised side by side here, because the claim is comparative: it is not "the fields
    are present" but "the two cases differ in them". A payload carrying only ``billed`` makes the
    two rows byte-identical.
    """
    services, bus, settings = sweep_env(AGENT_INCREMENTAL_BILLING_ENABLED=True)
    paid = f"run_{uuid.uuid4().hex[:8]}"
    lost = f"run_{uuid.uuid4().hex[:8]}"
    # Fully pre-billed: the snapshot observed 7 credits, the steps already took all 7.
    paid_uid = await _seed_run(db_sessionmaker, paid, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _step_debit(db_sessionmaker, paid_uid, f"{paid}:1", 7)
    # Snapshot writes LOST: the ledger proves 7 credits of usage happened, but the snapshot's
    # cumulative barely moved. The snapshot is flushed immediately BEFORE each per-step debit
    # (ADR-064 §1), so its cumulative can only ever run AHEAD of the ledger — a ledger ahead of the
    # snapshot is the invariant breach the service itself logs, i.e. exactly "writes were lost".
    lost_uid = await _seed_run(
        db_sessionmaker,
        lost,
        heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30,
        input_tokens=1,
        output_tokens=0,
    )
    await _step_debit(db_sessionmaker, lost_uid, f"{lost}:1", 7)
    await _wait_for_grace(bus)

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 2

    paid_payload = await _audit_payload(db_sessionmaker, paid)
    lost_payload = await _audit_payload(db_sessionmaker, lost)

    # The premise: on `billed` + `billingBasis` alone the two rows are indistinguishable.
    assert (paid_payload["billed"], paid_payload["billingBasis"]) == (0, "snapshot")
    assert (lost_payload["billed"], lost_payload["billingBasis"]) == (0, "snapshot")

    # And the fields that do tell them apart.
    assert (paid_payload["observed"], paid_payload["alreadyCharged"]) == (
        7,
        7,
    ), "a fully pre-billed run must show that its 7 credits were observed AND already collected"
    assert (lost_payload["observed"], lost_payload["alreadyCharged"]) == (1, 7), (
        "a run whose snapshot writes were lost must show the ledger AHEAD of the observation — the "
        "shape that says 'we measured almost nothing of what demonstrably happened'"
    )
    assert paid_payload["usage"] == {"input_tokens": 2000, "output_tokens": 1000}
    assert lost_payload["usage"] == {"input_tokens": 1, "output_tokens": 0}
    assert paid_payload["heartbeatAgeSeconds"] >= _ORPHAN_TIMEOUT


@pytest.mark.asyncio
async def test_the_audited_usage_counts_survive_adr049_redaction(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any
) -> None:
    """``usage.input_tokens``/``output_tokens`` must reach the audit row as INTEGERS, not REDACTED.

    The generic denylist redacts anything whose key contains ``token``, and these keys do. They
    survive only through the ADR-049 exact-match carve-out ``_USAGE_COUNT_ALLOWLIST``; if the audit
    payload ever names them differently — ``tokensIn``, ``inputTokenCount`` — the carve-out stops
    matching and the sweep's only record of what it measured becomes the string ``REDACTED``, which
    is precisely the reconstruction §5.2 needs and cannot get back.

    Asserted through ``redact()`` itself as well as on the stored row, so a change to the allowlist
    fails here rather than silently in production audit.
    """
    from app.observability.redaction import redact

    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)
    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    payload = await _audit_payload(db_sessionmaker, run_id)
    assert payload["usage"] == {"input_tokens": 2000, "output_tokens": 1000}

    survived = redact(payload)
    assert survived["usage"] == {"input_tokens": 2000, "output_tokens": 1000}, (
        f"the usage counts did not survive redaction: {survived['usage']!r} — the audit trail of "
        "what the sweep measured would be unreadable"
    )
    assert survived["observed"] == payload["observed"]
    assert survived["alreadyCharged"] == payload["alreadyCharged"]


@pytest.mark.parametrize(
    ("snapshot", "input_tokens", "output_tokens", "basis", "billed"),
    [
        pytest.param(True, 2000, 1000, "snapshot", 7, id="observed-non-zero"),
        pytest.param(True, 0, 0, "zero_usage", 0, id="observed-zero"),
        pytest.param(False, 0, 0, "no_snapshot", 0, id="never-observed"),
    ],
)
@pytest.mark.asyncio
async def test_each_billing_basis_is_reported_in_the_audit_and_the_metric(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sweep_env: Any,
    snapshot: bool,
    input_tokens: int,
    output_tokens: int,
    basis: str,
    billed: int,
) -> None:
    """The two zeros are NOT the same event, and §5.2 exists because they used to look identical.

    ``zero_usage`` means the run really was watched and really produced nothing; ``no_snapshot``
    means the consumer never made its first DB call, so nothing about the run was ever observed —
    a revenue incident whose whole point is that it must not average into the healthy case. With a
    plain ``COALESCE(...,0)`` both arrive as "billed 0" and a free run nobody consumed is
    indistinguishable from a legitimate zero.

    The status is asserted for all three: ``failed`` is unconditional, because an upper bound on a
    run's life outranks the precision of its label (leaving it ``running`` is TD-037 coming back).
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(
        db_sessionmaker,
        run_id,
        heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30 if snapshot else None,
        age_seconds=_ORPHAN_TIMEOUT + 30,
        snapshot=snapshot,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    assert await _debits(db_sessionmaker, uid) == [], "the run starts with a clean ledger"
    await _wait_for_grace(bus)
    before = _basis_count(basis)
    others = {b: _basis_count(b) for b in ("snapshot", "zero_usage", "no_snapshot") if b != basis}

    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    assert await _status(db_sessionmaker, run_id) == "failed"
    payload = await _audit_payload(db_sessionmaker, run_id)
    assert payload["billingBasis"] == basis, "the sweep reported the wrong ground for its charge"
    assert payload["billed"] == billed
    assert _basis_count(basis) == before + 1, "the counter did not move for this basis"
    for other, count in others.items():
        assert _basis_count(other) == count, f"the finalization also counted as {other}"


@pytest.mark.asyncio
async def test_the_orphan_counters_are_exposed_without_any_unbounded_label(
    db_sessionmaker: async_sessionmaker[AsyncSession], sweep_env: Any, client: AsyncClient
) -> None:
    """Both ADR-067 counters reach ``GET /metrics``, and neither carries a ``userId``.

    ``metrics.py`` states the convention as "bounded enum labels only (no user-content)", and
    ADR-067 §5.1/§5.2 lean on it twice: a ``userId`` label would give both unbounded cardinality
    and user identifiers in a scrape endpoint. Identifiers belong in the audit row and the log
    event, which the sweep writes anyway — asserted here so the split is not merely documented.
    """
    services, bus, settings = sweep_env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id, heartbeat_age_seconds=_ORPHAN_TIMEOUT + 30)
    await _wait_for_grace(bus)
    assert await sweep_orphan_runs(services=services, bus=bus, settings=settings) == 1

    # Through the real endpoint, not render_metrics(): what the ADR promises an operator is the
    # SCRAPE, and a counter that exists in the registry but never reaches /metrics is not a signal.
    response = await client.get("/metrics")
    assert response.status_code == 200
    text_body = response.content.decode()

    assert 'agent_run_orphan_finalized_total{basis="snapshot"}' in text_body
    assert "agent_run_launch_upstream_timeout_total" in text_body
    exposed = [
        line
        for line in text_body.splitlines()
        if line.startswith(
            ("agent_run_orphan_finalized_total", "agent_run_launch_upstream_timeout")
        )
    ]
    assert exposed, "neither ADR-067 counter is exposed at all"
    for line in exposed:
        assert "userId" not in line and "user_id" not in line, f"unbounded label leaked: {line}"
        assert "runId" not in line and "run_id" not in line, f"unbounded label leaked: {line}"
        assert str(uid) not in line and run_id not in line, f"an identifier leaked: {line}"
