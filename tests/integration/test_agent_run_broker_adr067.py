"""Integration: downstream fan-out of the client ``/events`` stream (ADR-067 §3.2-§3.4, stage 4).

Real Redis (own logical DB per test) and a real ``agent_runs`` row, because every rule here is a
decision made from BOTH stores at once: what the ring holds, whether a lease is alive, and what the
lifecycle status says. Faking either side would only assert the author's model of it.

The one that earned a review round of its own is the mid-stream generation change. Redis restarts,
the consumer survives and republishes from ``seq`` 1 under a NEW generation, while an already-open
reader still remembers "delivered = 500" and silently discards everything that follows. No close
rule fires — the lease is alive — so the client keeps an open, SILENT stream for the rest of the
run: strictly worse than the hang the cursor replaced, and invisible in any reconnect-based test.
It is therefore tested on a LIVE stream, never by reconnecting.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.broker import EVENT_RUN_TRUNCATED, AgentRunBroker, Cursor
from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.transport import AgentRunEventBus, url_with_db
from app.config import Settings
from tests.conftest import seed_user


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def broker_factory(
    redis_url: str, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Any]:
    """A broker on a fresh logical DB, wired to the real runs repository."""
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(_session: AsyncSession, **overrides: Any) -> tuple[Any, AgentRunEventBus]:
        db = counter["n"]
        counter["n"] += 1
        base: dict[str, Any] = {
            "REDIS_URL": redis_url,
            "AGENT_RUN_REDIS_DB": db,
            "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": 60,
            # Small so the closing rules are reachable inside a test.
            "AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS": 1,
            # The supervisor's period, split out of the lease TTL. Short so a tick lands
            # inside the scenes that need one; at the 10s default they would get none.
            "AGENT_RUN_SUBSCRIBER_PROBE_SECONDS": 0.5,
            "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS": 2,
            "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS": 1,
            "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS": 2,
        }
        base.update(overrides)
        settings = Settings(**base)  # type: ignore[arg-type]
        client = redis_asyncio.from_url(
            url_with_db(redis_url, db), decode_responses=True, socket_timeout=5
        )
        clients.append(client)
        bus = AgentRunEventBus(client, settings)

        # ⚠️ A REAL new session per probe — opened here, closed here. The broker probes
        # `agent_runs` for the whole life of an SSE stream (up to two hours), so a session bound to
        # the request would hold a pooled connection, and an ACCESS SHARE lock, for that entire
        # time: about fifteen concurrent streams exhausted a worker's pool, after which EVERY
        # endpoint of that worker failed and not just this feature.
        #
        # ⛔ It must NOT close over the test's own `_session`. An earlier version of this fixture
        # did exactly that while its comment claimed the opposite, so every supervisor probe ran on
        # the connection the test itself was using — and asyncpg answers a second concurrent
        # operation on one connection with `InterfaceError: another operation is in progress`.
        # It stayed hidden
        # while the probe period was borrowed from the 30s lease TTL; at 0.5s the overlap became
        # ordinary. The knob made it observable, it did not create it. A fixture whose comment and
        # code disagree is the same defect class this contour keeps finding in production code.
        @asynccontextmanager
        async def _runs() -> AsyncIterator[AgentRunsRepository]:
            async with db_sessionmaker() as probe_session:
                yield AgentRunsRepository(probe_session)

        broker = AgentRunBroker(bus=bus, runs=_runs, settings=settings)
        return broker, bus

    yield _make

    for client in clients:
        try:
            await client.flushdb()
            await client.aclose()
        except RedisError:  # pragma: no cover - teardown best effort
            pass


async def _seed_run(
    maker: async_sessionmaker[AsyncSession], run_id: str, *, status: str = "running"
) -> uuid.UUID:
    async with maker() as session:
        user_id = await seed_user(session, subscription="active", balance=100)
        await session.execute(
            text(
                "INSERT INTO agent_runs (run_id, user_id, session_id, status, model) "
                "VALUES (:r, :u, 'sess-1', CAST(:st AS agent_run_status), 'm')"
            ),
            {"r": run_id, "u": str(user_id), "st": status},
        )
        await session.commit()
    return user_id


def _payloads(blocks: list[bytes]) -> list[dict[str, Any] | None]:
    """Decoded body per block; ``None`` for ordinary events, whose payload is raw upstream bytes.

    Only the synthetic markers the broker generates are JSON — a relayed event is whatever the
    image sent, which the ring carries verbatim and this helper must not assume anything about.
    """
    out: list[dict[str, Any] | None] = []
    for block in blocks:
        body = block.split(b"data: ", 1)[1]
        try:
            parsed = json.loads(body.decode())
        except (UnicodeDecodeError, ValueError):
            out.append(None)
            continue
        out.append(parsed if isinstance(parsed, dict) else None)
    return out


def _ids(blocks: list[bytes]) -> list[str]:
    return [b.split(b"\n", 1)[0].removeprefix(b"id: ").decode() for b in blocks]


async def _drain(
    stream: AsyncIterator[bytes], *, limit: int = 50, deadline: float = 10.0
) -> list[bytes]:
    """Collect up to ``limit`` blocks; stop when the stream closes."""
    collected: list[bytes] = []

    async def _run() -> None:
        async for block in stream:
            collected.append(block)
            if len(collected) >= limit:
                return

    await asyncio.wait_for(_run(), timeout=deadline)
    return collected


# ==================================================================================================
# Truncation: the gap must be announced, including on a FIRST connection.
# ==================================================================================================
@pytest.mark.asyncio
async def test_run_truncated_fires_on_a_first_connection_to_a_trimmed_ring(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The marker is due whenever the first servable seq is beyond ``cursor + 1``.

    With the empty cursor that reads "first seq > 1", which is exactly why it also fires for a
    client connecting for the FIRST time to a ring that has already been trimmed. Restricting the
    rule to reconnects would have missed the case it exists for: a client that resets its
    accumulated text on connect would otherwise replace a complete transcript with a knowingly
    partial one and have no way to tell.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session, AGENT_RUN_EVENT_BUFFER_MAX=3)
        epoch = await bus.ensure_epoch(run_id)
        for i in range(1, 7):
            await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode())

        blocks = await _drain(broker.stream(run_id=run_id, cursor=Cursor()))

    payloads = _payloads(blocks)
    first = payloads[0]
    assert (
        first is not None and first["event"] == EVENT_RUN_TRUNCATED
    ), "a first-connection gap went unannounced"
    assert first["from_seq"] == 4
    assert _ids(blocks)[1:] == [f"{epoch}-{n}" for n in (4, 5, 6)]


@pytest.mark.asyncio
async def test_no_truncation_marker_when_the_ring_is_complete(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Paired negative — a marker on every connection would train clients to ignore it."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        epoch = await bus.ensure_epoch(run_id)
        for i in range(1, 4):
            await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode())

        blocks = await _drain(broker.stream(run_id=run_id, cursor=Cursor()))

    assert all(EVENT_RUN_TRUNCATED.encode() not in b for b in blocks)
    assert _ids(blocks) == [f"{epoch}-{n}" for n in (1, 2, 3)]


@pytest.mark.asyncio
async def test_a_reconnect_resumes_incrementally(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The cursor must actually save the client from re-reading what it already has."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        epoch = await bus.ensure_epoch(run_id)
        for i in range(1, 6):
            await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode())

        blocks = await _drain(broker.stream(run_id=run_id, cursor=Cursor(seq=3, epoch=epoch)))

    assert _ids(blocks) == [f"{epoch}-4", f"{epoch}-5"]


@pytest.mark.asyncio
async def test_a_cursor_from_another_generation_replays_everything(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """A stale generation is not an error — it is a full replay plus an explicit gap marker.

    ⚠️ The assertion below used to contradict this very sentence: it required the ids to be
    exactly the three ring elements, i.e. NO marker. It was written when the marker could only
    be born inside
    ``_replay_blocks``' gap rule, which does not fire here (the emptied filter is 0 and the ring
    starts at 1). C5 (§3.2.1) is the reason it is due anyway — the client's claim to a prefix of
    ``a-dead-generation`` is rejected, and a rejected claim must be answered, not silently replaced.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        epoch = await bus.ensure_epoch(run_id)
        for i in range(1, 4):
            await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode())

        blocks = await _drain(
            broker.stream(run_id=run_id, cursor=Cursor(seq=2, epoch="a-dead-generation"))
        )

    assert _ids(blocks) == [f"{epoch}-{n}" for n in (0, 1, 2, 3)], (
        "the stale cursor was trusted, or its rejection was not announced (C5) — the leading "
        "`-0` is the marker, whose seq is 0 by C6 so a reconnect does not re-present the claim"
    )


# ==================================================================================================
# THE critical one: the generation changes UNDER an open connection.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_generation_change_under_an_open_stream_is_announced_and_resets_dedup(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Redis restarted mid-run: the consumer republishes from seq 1 under a NEW generation.

    Without the mid-stream check the reader keeps "delivered = 500", every new event fails the
    ``seq > last_seq`` dedup, and the client holds an OPEN, SILENT stream to the end of the run — no
    close rule fires because the lease is alive. Driven on a LIVE stream on purpose: a
    reconnect-based test exercises ``_validate_cursor`` instead and would pass with the mid-stream
    check deleted.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        old_epoch = await bus.ensure_epoch(run_id)
        await bus.acquire_lease(run_id, "consumer-1")
        for i in range(1, 4):
            await bus.publish(run_id, epoch=old_epoch, raw=f"data: old-{i}\n\n".encode())

        collected: list[bytes] = []
        stream = broker.stream(run_id=run_id, cursor=Cursor())

        async def _reader() -> None:
            async for block in stream:
                collected.append(block)
                if len(collected) >= 5:
                    return

        reader = asyncio.create_task(_reader())
        await asyncio.sleep(0.3)  # let the replay drain and the subscription settle

        # Redis "restart": the generation key is gone and the counter restarts from 1.
        await bus._redis.delete(f"agent:run:{run_id}:epoch", f"agent:run:{run_id}:seq")
        new_epoch = await bus.ensure_epoch(run_id)
        assert new_epoch != old_epoch
        await bus.publish(run_id, epoch=new_epoch, raw=b"data: new-1\n\n")

        await asyncio.wait_for(reader, timeout=10)

    payloads = _payloads(collected)
    assert any(
        p.get("event") == EVENT_RUN_TRUNCATED for p in payloads[3:]
    ), "the generation change was not announced — the stream would have gone silent"
    assert collected[-1].endswith(
        b"data: new-1\n\n"
    ), "the post-restart event was swallowed by dedup against the OLD generation's seq"
    assert _ids(collected)[-1] == f"{new_epoch}-1"


# ==================================================================================================
# The closing rules (§3.3).
# ==================================================================================================
@pytest.mark.asyncio
async def test_rule_2_a_terminal_run_serves_the_ring_and_closes(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """A finished run must cost no subscription at all — the client takes the rest from /state."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        epoch = await bus.ensure_epoch(run_id)
        await bus.acquire_lease(run_id, "consumer-1")  # a live lease must not keep it open
        await bus.publish(run_id, epoch=epoch, raw=b"data: only\n\n")

        blocks = await asyncio.wait_for(
            _drain(broker.stream(run_id=run_id, cursor=Cursor())), timeout=5
        )

    assert len(blocks) == 1, "a terminal run kept the stream open"


@pytest.mark.asyncio
async def test_rule_3_no_lease_and_an_empty_ring_closes_immediately(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The equivalent of the old "200 with no events" — nobody is driving this run."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, _bus = broker_factory(session)
        blocks = await asyncio.wait_for(
            _drain(broker.stream(run_id=run_id, cursor=Cursor())), timeout=5
        )

    assert blocks == []


@pytest.mark.asyncio
async def test_rule_3_does_not_fire_when_redis_cannot_answer_about_the_lease(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """``None`` is not ``False``: an UNKNOWN lease must never close a client stream.

    Rule 3 is isolated deliberately. An entirely dead Redis also kills the pub/sub subscription, so
    an end-to-end "unplug Redis" test would end the stream for a different reason and pass even if
    rule 3 treated ``None`` as ``False``. Only ``lease_alive`` is overridden — the ring, the epoch
    and the channel stay real — so what is asserted is the rule itself.

    The stakes: conflating the two would disconnect every subscriber at once during a Redis blip,
    the same mass-failure shape the orphan grace exists to avoid on the other side of the system.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await bus.ensure_epoch(run_id)

        async def _unknown(_run_id: str) -> bool | None:
            return None

        bus.lease_alive = _unknown  # type: ignore[method-assign]

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        with pytest.raises(TimeoutError):
            # Empty ring + UNKNOWN lease. Rule 3 fires only on an explicit False, so this must keep
            # waiting rather than return at once.
            await asyncio.wait_for(_drain(stream, limit=99, deadline=30), timeout=4)


@pytest.mark.asyncio
async def test_rule_3_does_fire_on_a_definite_absence_of_a_lease(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Paired positive: the same code path DOES close when the answer is a definite ``False``."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await bus.ensure_epoch(run_id)
        assert await bus.lease_alive(run_id) is False

        blocks = await asyncio.wait_for(
            _drain(broker.stream(run_id=run_id, cursor=Cursor())), timeout=5
        )
    assert blocks == []


@pytest.mark.asyncio
async def test_rule_5_idle_with_no_lease_closes(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Idle AND leaseless. Both are required — a quiet run with a live consumer is normal, and a
    long tool call is exactly that."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        epoch = await bus.ensure_epoch(run_id)
        await bus.publish(run_id, epoch=epoch, raw=b"data: one\n\n")  # non-empty ring, no lease

        blocks = await asyncio.wait_for(
            _drain(broker.stream(run_id=run_id, cursor=Cursor())), timeout=15
        )

    assert _ids(blocks) == [f"{epoch}-1"], "the stream must serve the ring, then close on idle"


@pytest.mark.asyncio
async def test_a_quiet_run_with_a_live_lease_is_not_closed(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The protection for a long tool call: silence with a live consumer is NOT a dead stream.

    This is the false-positive side of rule 5, and it matters more than the rule itself — closing a
    working run's stream is a visible product failure, while a lingering dead stream is not.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session, AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS=30)
        epoch = await bus.ensure_epoch(run_id)
        await bus.acquire_lease(run_id, "consumer-1")
        await bus.publish(run_id, epoch=epoch, raw=b"data: one\n\n")

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        with pytest.raises(TimeoutError):
            # Idle far beyond AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS=1, but the lease is alive.
            await asyncio.wait_for(_drain(stream, limit=99, deadline=30), timeout=4)
