"""Integration: the three-valued lease in FLIGHT, and §6.4's reach over the whole consumer life.

ADR-067 §4.1 (revision 2026-07-31) changed two mappings of a ``RedisError`` at once and said so
explicitly: fixing only ``acquire_lease`` would have been *worse than useless*. The supervisor
cancels the working task on ``LOST``, so a run that did subscribe during a Redis outage would have
been killed at its first renewal tick — after its one-shot stream had already been spent. Hence the
renewal side needs its own module, and needs the same PAIR discipline as the acquisition side:

* ``RedisError`` on renewal ⇒ the run survives (ignorance is not evidence);
* a FOREIGN owner on renewal ⇒ the run stops (a foreign uuid IS evidence).

A single test proves neither. One alone is satisfied by "never stand down", the other by "always
stand down", and the pre-§4.1 code passed the second while failing every run of the first.

The third case, ``REACQUIRED``, is not about survival but about the GENERATION: continuing under an
epoch Redis no longer knows makes the broker drop every remaining event of the run, so the client
gets a stream that is open and silent to the end. The epoch must therefore be observed to change,
not merely the consumer to keep running.

Finally, TD-043: ``prepare_consumer_snapshot`` is the consumer's first DB call and used to sit
OUTSIDE the ``try/finally``, so its failure skipped §6.4 whole.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.consumer import (
    EVENT_CONSUMER_FAILED,
    EVENT_CONSUMER_SHUTDOWN,
    ConsumerContext,
    ConsumerOutcome,
    SubscriptionHandshake,
    run_consumer,
    run_supervisor,
)
from app.agent_proxy.transport import AgentRunEventBus, LeaseAcquisition, url_with_db
from app.deps import get_agent_proxy_service_for
from app.hermes_runtime.manager import InstanceEndpoint
from tests.conftest import seed_user
from tests.support.agent_run_harness import (
    FakeUpstream,
    UpstreamScript,
    await_consumer,
    consumer_settings,
)


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def env(
    redis_url: str, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Any]:
    """A consumer environment on its own Redis logical DB, with a raw client for tampering."""
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(**overrides: Any) -> tuple[Any, AgentRunEventBus, Any, redis_asyncio.Redis]:
        db = counter["n"]
        counter["n"] += 1
        settings = consumer_settings(redis_url=redis_url, redis_db=db, **overrides)
        client = redis_asyncio.from_url(
            url_with_db(redis_url, db), decode_responses=True, socket_timeout=5
        )
        clients.append(client)
        bus = AgentRunEventBus(client, settings)

        @asynccontextmanager
        async def services() -> AsyncIterator[Any]:
            async with db_sessionmaker() as session:
                yield get_agent_proxy_service_for(session)

        return services, bus, settings, client

    yield _make

    for client in clients:
        try:
            await client.flushdb()
            await client.aclose()
        except RedisError:  # pragma: no cover - teardown best effort
            pass


async def _seed_run(
    maker: async_sessionmaker[AsyncSession], run_id: str, *, balance: int = 10_000
) -> uuid.UUID:
    async with maker() as session:
        uid = await seed_user(session, subscription="active", balance=balance)
        await session.execute(
            text(
                "INSERT INTO agent_runs (run_id, user_id, session_id, status, model) "
                "VALUES (:r, :u, 'sess-1', 'running', 'm')"
            ),
            {"r": run_id, "u": str(uid)},
        )
        await session.commit()
    return uid


def _events(*payloads: str) -> list[bytes]:
    return [f"data: {p}\n\n".encode() for p in payloads]


_DELTA = '{"event": "message.delta", "run_id": "r", "delta": "hi"}'
# Wire shape of tests/fixtures/hermes_prod_completed_run_adr067.sse.
_COMPLETED = (
    '{"event": "run.completed", "run_id": "r", "timestamp": 1785321309.57, "output": "DONE.", '
    '"usage": {"input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000}}'
)


async def _audit_types(maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> list[str]:
    async with maker() as session:
        rows = (
            await session.execute(
                text("SELECT event_type FROM audit_logs WHERE user_id=:u ORDER BY id"),
                {"u": str(user_id)},
            )
        ).all()
    return [r.event_type for r in rows]


async def _run_state(
    maker: async_sessionmaker[AsyncSession], run_id: str, user_id: uuid.UUID
) -> tuple[str, list[tuple[str, int]]]:
    async with maker() as session:
        status = (
            await session.execute(
                text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()
        debits = (
            await session.execute(
                text(
                    "SELECT idempotency_key, amount FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit' ORDER BY id"
                ),
                {"u": str(user_id)},
            )
        ).all()
    return str(status), [(r.idempotency_key, r.amount) for r in debits]


# ==================================================================================================
# §4.1 renewal — THE PAIR.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_redis_error_on_renewal_does_not_kill_a_run_in_flight(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Positive half: Redis dies mid-run ⇒ the run keeps going and finishes normally.

    The upstream is deliberately slower than several renewal periods, so the run cannot finish
    before the supervisor has ticked repeatedly on an unanswerable Redis. On the pre-§4.1 mapping
    (``RedisError`` → ``LOST``) the FIRST of those ticks cancels the working task, and the run ends
    with no terminal status and no debit — having already consumed the one-shot stream, which is
    strictly worse than never having subscribed.

    Only the renewal path is broken, not the whole bus: this is the "Redis went away after we
    started" case, which is the one the pre-fix mapping punished hardest.
    """
    services, bus, settings, _client = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    renewals = {"n": 0}

    async def _dead_renew(*_args: Any, **_kwargs: Any) -> Any:
        renewals["n"] += 1
        raise RedisError("connection reset")

    # Break the Lua call underneath renew_lease, so the REAL RedisError → UNKNOWN mapping in
    # transport.py is the thing under test rather than a stubbed return value.
    bus._renew = _dead_renew  # type: ignore[method-assign]

    renew = settings.agent_run_consumer_lease_renew_seconds
    script = UpstreamScript(chunks=_events(_DELTA, _DELTA, _DELTA, _COMPLETED), delay=renew * 1.2)
    async with FakeUpstream(script) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        await asyncio.wait_for(
            run_consumer(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
            ),
            timeout=60,
        )

    assert renewals["n"] >= 2, (
        "the run finished before the supervisor could tick on a broken Redis — the test would "
        "pass on the pre-fix mapping too"
    )
    status, debits = await _run_state(db_sessionmaker, run_id, uid)
    assert status == "completed", "an unanswerable Redis cancelled a run that was being consumed"
    assert debits == [(run_id, 7)], "the run lost its billing to a renewal it could not confirm"


@pytest.mark.asyncio
async def test_a_foreign_owner_on_renewal_stops_the_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Negative half: a foreign uuid in the lease key ⇒ the working task IS cancelled.

    The regression this guards is the relaxation of §4.1 being widened from "Redis did not answer"
    to "the renewal did not succeed". A foreign owner is direct evidence of a second consumer, and
    two subscribers to a one-shot stream leave both with nothing — so this outcome, unlike
    ``UNKNOWN``, must still end the run through §6.4 with its shutdown audit.

    ⚠️ Completion is POLLED via ``await_consumer``, never awaited under ``asyncio.wait_for`` — the
    reason is in that helper's docstring, and an earlier version of this very test is the reason it
    has one: with ``wait_for`` it passed while the supervisor's entire ``LOST`` branch was disabled.
    """
    services, bus, settings, client = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    # hold_open: the run is mid-flight and would never end on its own.
    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        handshake = SubscriptionHandshake()
        consumer = asyncio.create_task(
            run_consumer(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                handshake=handshake,
            )
        )
        try:
            assert await handshake.wait(timeout=20), "the consumer never subscribed"

            # A second owner appears in the key — evidence, not ignorance.
            await client.set(f"agent:run:{run_id}:lease", "a-different-worker", px=60_000)

            # A handful of renewal ticks is generous; the FIRST one already sees the foreign owner.
            # A handful of renewal ticks is generous; the FIRST one already sees the foreign owner.
            budget = settings.agent_run_consumer_lease_renew_seconds * 6
            assert await await_consumer(consumer, budget=budget), (
                "the consumer kept driving a run whose lease a second owner demonstrably holds — "
                "two subscribers to a one-shot stream leave both with nothing"
            )
        finally:
            if not consumer.done():  # pragma: no cover - only on the failing implementation
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

    assert EVENT_CONSUMER_SHUTDOWN in await _audit_types(db_sessionmaker, uid)
    status, debits = await _run_state(db_sessionmaker, run_id, uid)
    assert status == "running", "the consumer guessed a terminal status it could not know"
    assert debits == [], "a cancelled consumer finalized billing it never observed"
    assert (
        await client.get(f"agent:run:{run_id}:lease") == "a-different-worker"
    ), "the departing consumer deleted the rightful owner's lease"


# ==================================================================================================
# §4 REACQUIRED — survival is not enough; the GENERATION has to move.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_wiped_lease_is_retaken_under_a_new_generation(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Redis comes back empty ⇒ ``REACQUIRED``: the key is OURS again and the epoch is new.

    Driven through ``run_supervisor`` directly because the epoch it must replace lives in the
    ``ConsumerContext``, and an end-to-end run gives no handle on that object. Both halves are
    asserted: a supervisor that re-took the lease but kept the old epoch would look healthy here
    while every remaining event of the run was silently dropped by the broker's epoch check —
    exactly the defect the epoch exists to prevent.
    """
    services, bus, settings, client = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)
    async with db_sessionmaker() as session:
        await get_agent_proxy_service_for(session).prepare_consumer_snapshot(
            user_id=uid, run_id=run_id
        )

    owner = uuid.uuid4().hex
    assert await bus.acquire_lease(run_id, owner) is LeaseAcquisition.ACQUIRED
    old_epoch = await bus.ensure_epoch(run_id)
    assert old_epoch is not None
    ctx = ConsumerContext(epoch=old_epoch, owner=owner)

    async def _idle() -> ConsumerOutcome:
        await asyncio.sleep(3600)
        return ConsumerOutcome(audit_event=None, terminal_seen=True, events_processed=0)

    worker = asyncio.create_task(_idle())
    supervisor = asyncio.create_task(
        run_supervisor(
            services=services,
            bus=bus,
            settings=settings,
            user_id=uid,
            run_id=run_id,
            ctx=ctx,
            worker=worker,
            # time.monotonic, not loop.time: the supervisor compares against the former, and the
            # two clocks share no origin — a mismatched start would fire MAX_DURATION at once.
            started_at=time.monotonic(),
        )
    )
    try:
        # "Redis restarted": lease, epoch and seq are all gone, as FLUSHDB would leave them.
        await client.delete(
            f"agent:run:{run_id}:lease", f"agent:run:{run_id}:epoch", f"agent:run:{run_id}:seq"
        )
        deadline = settings.agent_run_consumer_lease_renew_seconds * 6
        for _ in range(int(deadline * 10)):
            await asyncio.sleep(0.1)
            if ctx.epoch != old_epoch:
                break
    finally:
        supervisor.cancel()
        worker.cancel()
        await asyncio.gather(supervisor, worker, return_exceptions=True)

    assert (
        await client.get(f"agent:run:{run_id}:lease") == owner
    ), "the lease was not re-taken — a Redis restart would strand the run without a beacon"
    assert ctx.epoch != old_epoch, (
        "the consumer kept publishing under a generation Redis no longer knows; the broker would "
        "drop every remaining event and the client's stream would go silent to the end of the run"
    )
    assert await bus.current_epoch(run_id) == ctx.epoch, "the adopted epoch is not the stored one"


# ==================================================================================================
# TD-043 — §6.4 covers the consumer's ENTIRE life, starting at its first DB call.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_failing_snapshot_row_still_runs_the_whole_shutdown_procedure(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """``prepare_consumer_snapshot`` raises ⇒ lease released, waiter freed, audit written.

    Before TD-043 this call sat OUTSIDE the ``try/finally``, so its failure skipped §6.4 entirely:
    the lease stayed held (the sweep's condition 1 unsatisfied, hence no finalization for a full
    ORPHAN_TIMEOUT), the caller waited out the whole handshake timeout, nothing was audited — and
    the run was left with no snapshot row at all, which is precisely the ``no_snapshot`` revenue
    incident of §5.2.

    The upstream is asserted untouched as well: a consumer that cannot even record its own snapshot
    must not spend the run's one-shot stream on the way out.
    """
    _services, bus, settings, client = env(AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS=30.0)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    @asynccontextmanager
    async def failing_prepare() -> AsyncIterator[Any]:
        async with db_sessionmaker() as session:
            service = get_agent_proxy_service_for(session)

            async def _boom(**_kwargs: Any) -> None:
                raise SQLAlchemyError("snapshot row could not be created")

            service.prepare_consumer_snapshot = _boom  # type: ignore[method-assign]
            yield service

    handshake = SubscriptionHandshake()
    async with FakeUpstream(UpstreamScript(chunks=_events(_COMPLETED))) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            run_consumer(
                services=failing_prepare,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                handshake=handshake,
            ),
            # Well under AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS: the pre-TD-043 code raised out of
            # run_consumer instead, so the bound also pins that we return rather than blow up.
            timeout=20,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert upstream.requests == [], "the one-shot stream was spent by a consumer that gave up"

    assert not handshake.established
    assert await handshake.wait(timeout=0.1) is False, "the waiter was never released"
    assert elapsed < 20, "the caller was made to wait out the handshake timeout"
    assert (
        await bus.lease_alive(run_id) is False
    ), "the lease survived — the sweep's condition 1 would never hold and the run would sit running"
    assert await client.exists(f"agent:run:{run_id}:lease") == 0
    assert EVENT_CONSUMER_FAILED in await _audit_types(
        db_sessionmaker, uid
    ), "nothing was audited: a consumer that failed before subscribing left no trace at all"
