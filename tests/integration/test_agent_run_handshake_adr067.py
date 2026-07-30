"""Integration: the subscription handshake and the shutdown drain (ADR-067 §3/§6.1.1, stage 3).

The handshake exists because the Hermes stream is ONE-SHOT: whoever subscribes first is the only
one who ever gets the run's history, so the consumer must be up before the client is told the run
started. Waiting for a WORKER-OWNED milestone rather than for the task to merely exist is the only
way to state that — a started task proves nothing about a socket.

The subtlety, and the reason both outcomes are tested: if only SUCCESS were signalled, every
failure — no lease, a refused connection, a non-2xx — would be indistinguishable from a slow
subscription, and the request handler would block for the whole timeout on a run that is already
doomed. So the tests below are mostly about the paths that must NOT leave a waiter hanging.

The drain is tested for the property that its ORDER exists to guarantee: §6.4 runs while the pool is
still open. Asserting it after the fact is the only honest way — once the pool is closed the
procedure cannot flush or release anything, and the evidence is precisely that it did.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.consumer import (
    ConsumerLauncher,
    ConsumerRegistry,
    SubscriptionHandshake,
    run_consumer,
)
from app.agent_proxy.transport import AgentRunEventBus, LeaseAcquisition, url_with_db
from app.deps import get_agent_proxy_service_for
from app.hermes_runtime.manager import InstanceEndpoint
from tests.conftest import seed_user
from tests.support.agent_run_harness import (
    FakeUpstream,
    UpstreamScript,
    consumer_settings,
)

_DELTA = '{"event": "message.delta", "run_id": "r", "delta": "hi"}'
_COMPLETED = (
    '{"event": "run.completed", "run_id": "r", "usage": {"input_tokens": 10, "output_tokens": 5}}'
)


def _events(*payloads: str) -> list[bytes]:
    return [f"data: {p}\n\n".encode() for p in payloads]


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
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(**overrides: Any) -> tuple[Any, AgentRunEventBus, Any]:
        db = counter["n"]
        counter["n"] += 1
        settings = consumer_settings(redis_url=redis_url, redis_db=db, **overrides)
        client = redis_asyncio.from_url(
            url_with_db(redis_url, db), decode_responses=True, socket_timeout=5
        )
        clients.append(client)

        @asynccontextmanager
        async def services() -> AsyncIterator[Any]:
            async with db_sessionmaker() as session:
                yield get_agent_proxy_service_for(session)

        return services, AgentRunEventBus(client, settings), settings

    yield _make

    for client in clients:
        try:
            await client.flushdb()
            await client.aclose()
        except RedisError:  # pragma: no cover - teardown best effort
            pass


async def _seed_run(maker: async_sessionmaker[AsyncSession], run_id: str) -> uuid.UUID:
    async with maker() as session:
        uid = await seed_user(session, subscription="active", balance=10_000)
        await session.execute(
            text(
                "INSERT INTO agent_runs (run_id, user_id, session_id, status, model) "
                "VALUES (:r, :u, 'sess-1', 'running', 'm')"
            ),
            {"r": run_id, "u": str(uid)},
        )
        await session.commit()
    return uid


# ==================================================================================================
# The handshake primitive: both outcomes must be reachable, and neither may block for ever.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_handshake_signals_success() -> None:
    handshake = SubscriptionHandshake()
    handshake.mark_established()
    assert await handshake.wait(0.1) is True
    assert handshake.established is True


@pytest.mark.asyncio
async def test_the_handshake_signals_failure_without_waiting_out_the_timeout() -> None:
    """A doomed run must not hold a request open for the whole handshake budget.

    This is the half that is easy to omit — signalling only success still "works" in the happy
    path, and the cost shows up as latency on exactly the runs that are already failing.
    """
    handshake = SubscriptionHandshake()
    handshake.mark_failed()
    started = time.monotonic()
    assert await handshake.wait(10.0) is False
    assert time.monotonic() - started < 1.0, "a failed handshake waited out its timeout"


@pytest.mark.asyncio
async def test_the_handshake_never_downgrades_an_established_subscription() -> None:
    """``mark_failed`` is also a backstop from the finalizer, so it must be idempotent AND safe."""
    handshake = SubscriptionHandshake()
    handshake.mark_established()
    handshake.mark_failed()
    handshake.mark_failed()
    assert await handshake.wait(0.1) is True


@pytest.mark.asyncio
async def test_an_expired_handshake_returns_false_rather_than_raising() -> None:
    """Expiry is a fact about our KNOWLEDGE, not about the run — the handler still answers 202."""
    handshake = SubscriptionHandshake()
    started = time.monotonic()
    assert await handshake.wait(0.2) is False
    assert 0.15 <= time.monotonic() - started < 3.0


# ==================================================================================================
# End to end: no completion path of a consumer may leave a waiter blocked.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_live_subscription_reports_established(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """The happy path, so the negatives below cannot pass by never establishing anything."""
    services, bus, settings = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        registry = ConsumerRegistry()
        launcher = ConsumerLauncher(
            registry=registry, services=services, bus=bus, settings=settings
        )
        established = await launcher.start_and_wait(
            user_id=uid,
            run_id=run_id,
            endpoint=InstanceEndpoint(base_url=upstream.base_url, api_key="k"),
        )
        assert established is True
        assert registry.active == 1
        await registry.drain(timeout=settings.agent_run_shutdown_drain_seconds)


@pytest.mark.parametrize(
    ("script", "why"),
    [
        (UpstreamScript(status=500), "a non-2xx upstream"),
        (UpstreamScript(chunks=[], hold_open=False), "a stream that closes at once"),
    ],
    ids=["non_2xx", "immediate_close"],
)
@pytest.mark.asyncio
async def test_a_doomed_consumer_releases_its_waiter(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    env: Any,
    script: UpstreamScript,
    why: str,
) -> None:
    """Every ending must release the waiter — that is what the finalizer's backstop is for.

    Without it the handler would block for the entire handshake budget on a run that is already
    over, turning a fast failure into a slow one on exactly the requests that are already going
    wrong.
    """
    services, bus, settings = env(AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS=10.0)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(script) as upstream:
        registry = ConsumerRegistry()
        launcher = ConsumerLauncher(
            registry=registry, services=services, bus=bus, settings=settings
        )
        started = time.monotonic()
        await launcher.start_and_wait(
            user_id=uid,
            run_id=run_id,
            endpoint=InstanceEndpoint(base_url=upstream.base_url, api_key="k"),
        )
        elapsed = time.monotonic() - started
        await registry.drain(timeout=settings.agent_run_shutdown_drain_seconds)

    assert elapsed < 8.0, f"{why}: the waiter blocked for {elapsed:.1f}s of a 10s budget"


@pytest.mark.asyncio
async def test_a_run_whose_lease_is_taken_releases_its_waiter_at_once(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Somebody else drives this run: an invariant SUCCESS, but not OUR subscription.

    Reported as "not established" rather than waited out — the caller's behaviour is the same for
    every non-establishment, and blocking here would penalise the one case where the system is
    working exactly as designed.
    """
    services, bus, settings = env(AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS=10.0)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)
    assert await bus.acquire_lease(run_id, "another-worker") is LeaseAcquisition.ACQUIRED

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA))) as upstream:
        registry = ConsumerRegistry()
        launcher = ConsumerLauncher(
            registry=registry, services=services, bus=bus, settings=settings
        )
        started = time.monotonic()
        established = await launcher.start_and_wait(
            user_id=uid,
            run_id=run_id,
            endpoint=InstanceEndpoint(base_url=upstream.base_url, api_key="k"),
        )
        elapsed = time.monotonic() - started
        assert upstream.requests == [], "the interloper opened the one-shot stream anyway"

    assert established is False
    assert elapsed < 8.0, "the waiter blocked although the outcome was known immediately"


@pytest.mark.asyncio
async def test_an_expired_handshake_does_not_stop_the_consumer(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Expiry costs OBSERVABILITY, never the run: the consumer keeps going and the client gets 202.

    The whole point of bounding the wait is that the events stream has no read timeout, so a peer
    that accepts the connection and says nothing would otherwise hold the request open indefinitely.
    """
    services, bus, settings = env(AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS=0.2)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    # Headers arrive only after the handshake budget has already expired.
    script = UpstreamScript(chunks=_events(_DELTA), initial_delay=1.5, hold_open=True)
    async with FakeUpstream(script) as upstream:
        registry = ConsumerRegistry()
        launcher = ConsumerLauncher(
            registry=registry, services=services, bus=bus, settings=settings
        )
        established = await launcher.start_and_wait(
            user_id=uid,
            run_id=run_id,
            endpoint=InstanceEndpoint(base_url=upstream.base_url, api_key="k"),
        )
        assert established is False, "the budget did not expire — the test proved nothing"
        assert registry.active == 1, "an expired handshake killed the consumer"
        await asyncio.sleep(2.0)
        assert await bus.lease_alive(run_id) is True, "the consumer stopped after its waiter left"
        await registry.drain(timeout=settings.agent_run_shutdown_drain_seconds)


# ==================================================================================================
# The registry and the drain (§6.1.1).
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_duplicate_start_is_ignored(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Idempotent per run: a second start could win the lease from our OWN live consumer, and a
    second subscription to a one-shot stream leaves both with nothing."""
    services, bus, settings = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        registry = ConsumerRegistry()

        def _coro() -> Any:
            return run_consumer(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
            )

        first = registry.start(run_id, _coro())
        second = registry.start(run_id, _coro())
        assert first is second, "a duplicate consumer was started for the same run"
        assert registry.active == 1
        await registry.drain(timeout=settings.agent_run_shutdown_drain_seconds)


@pytest.mark.asyncio
async def test_the_drain_lets_the_shutdown_procedure_finish_while_the_pool_is_open(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """(8) THE ordering guarantee, asserted by its observable consequence.

    §6.4 must run BEFORE the DB pool and the Redis client are closed. In the other order it could
    neither flush the final snapshot nor release the lease, so every orderly restart would degrade
    into the abrupt case and leave runs waiting out the orphan timeout. The evidence that the order
    held is that the lease is gone and the audit row is there — neither of which is reachable once
    the pool is closed.
    """
    services, bus, settings = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        registry = ConsumerRegistry()
        launcher = ConsumerLauncher(
            registry=registry, services=services, bus=bus, settings=settings
        )
        assert await launcher.start_and_wait(
            user_id=uid,
            run_id=run_id,
            endpoint=InstanceEndpoint(base_url=upstream.base_url, api_key="k"),
        )
        assert await bus.lease_alive(run_id) is True

        drained = await registry.drain(timeout=settings.agent_run_shutdown_drain_seconds)

    assert drained == 1
    assert registry.active == 0, "a drained registry must be empty"
    assert (
        await bus.lease_alive(run_id) is False
    ), "the lease survived an ORDERLY shutdown — §6.4 did not get to run before teardown"

    async with db_sessionmaker() as session:
        audited = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_logs "
                    "WHERE user_id = :u AND event_type LIKE 'agent_run_consumer_%'"
                ),
                {"u": str(uid)},
            )
        ).scalar_one()
    assert audited >= 1, "the consumer stopped without recording why — the pool was already closed"


@pytest.mark.asyncio
async def test_draining_an_empty_registry_is_a_no_op(env: Any) -> None:
    _services, _bus, settings = env()
    registry = ConsumerRegistry()
    assert await registry.drain(timeout=settings.agent_run_shutdown_drain_seconds) == 0


# ==================================================================================================
# (7) The lifecycle row must be COMMITTED before the client is told the run started.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_agent_runs_row_is_visible_from_another_session_before_the_202(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Visibility from a SECOND session is the only honest way to state "already committed".

    Reading it back through the writing session proves nothing — an uncommitted row is visible to
    its own transaction. The property matters because everything downstream keys off that row: the
    consumer's snapshot preparation, the orphan sweep's working set, and ``/state``. A 202 handed
    out before the row is durable means a client can ask about a run the database does not admit
    exists.

    Driven through ``new_run_processing`` + ``prepare_consumer_snapshot`` — the two writes the
    launch path performs before the handler returns — and observed from an independent session.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    # A snapshot row is created EAGERLY (§6.1) rather than on the first flush: a run whose consumer
    # dies immediately would otherwise have no heartbeat to go stale, and the sweep would age it by
    # created_at instead — which is a fallback, not the intended path.
    async with db_sessionmaker() as writer:
        service = get_agent_proxy_service_for(writer)
        await service.prepare_consumer_snapshot(user_id=uid, run_id=run_id)

    async with db_sessionmaker() as observer:
        visible = (
            await observer.execute(
                text("SELECT count(*) FROM agent_run_snapshots WHERE run_id = :r"),
                {"r": run_id},
            )
        ).scalar_one()
        run_visible = (
            await observer.execute(
                text("SELECT status FROM agent_runs WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()

    assert visible == 1, (
        "the snapshot row is not visible from another session — it was never committed, so a "
        "consumer that dies at once would leave the sweep with no heartbeat to age"
    )
    assert str(run_visible) == "running"
