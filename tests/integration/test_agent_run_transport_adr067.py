"""Integration: the Redis ring / pub-sub / lease transport (ADR-067 §3, stage 1б).

WHY A REAL REDIS. Every property below is a property of what Redis actually did — an atomic Lua
pipeline, a TTL that was applied, a key that does or does not exist, a logical database that is or
is not shared. A double would only replay the author's belief about those things, and the single
most valuable test here (``test_the_agent_run_db_is_really_isolated``) is a regression against a
belief that was FALSE: ``redis.from_url(url, db=N)`` silently discards ``db`` when the URL carries a
path, so a settings validator that checks the CONFIG would have passed while the contour shared a
database with rate limiting and idempotency.

The container is module-scoped and each test gets its own logical DB, so the suite-wide
``REDIS_URL`` (deliberately pointed at a closed port in ``tests/conftest.py`` so rate limiting fails
open) is left completely alone. Nothing here changes global state.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from app.agent_proxy.transport import AgentRunEventBus, LeaseAcquisition, url_with_db
from app.config import Settings

_TTL = 60


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    """A throwaway Redis, isolated from the rest of the suite."""
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


def _settings(redis_url: str, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "REDIS_URL": redis_url,
        "AGENT_RUN_REDIS_DB": 1,
        "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": _TTL,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
async def bus_factory(redis_url: str) -> AsyncIterator[Any]:
    """Build a bus on a FRESH logical DB per test, and flush it afterwards.

    Per-test isolation matters more than usual here: several tests assert on key EXISTENCE, so a
    leftover key from a neighbour would not fail loudly, it would make an assertion pass for the
    wrong reason.
    """
    clients: list[redis_asyncio.Redis] = []
    used_dbs: list[int] = []
    counter = {"n": 2}

    def _make(**overrides: Any) -> tuple[AgentRunEventBus, redis_asyncio.Redis, Settings]:
        db = counter["n"]
        counter["n"] += 1
        used_dbs.append(db)
        settings = _settings(redis_url, AGENT_RUN_REDIS_DB=db, **overrides)
        client = redis_asyncio.from_url(
            url_with_db(redis_url, db), decode_responses=True, socket_timeout=5
        )
        clients.append(client)
        return AgentRunEventBus(client, settings), client, settings

    yield _make

    for client in clients:
        try:
            await client.flushdb()
            await client.aclose()
        except RedisError:  # pragma: no cover - teardown best effort
            pass


def _elements(raw: list[str]) -> list[dict[str, Any]]:
    return [json.loads(item) for item in raw]


# ==================================================================================================
# The regression that justifies the whole module: the logical DB must be REALLY separate.
# ==================================================================================================
async def test_the_agent_run_db_is_really_isolated(redis_url: str) -> None:
    """A key written by the agent-run contour must NOT be visible from the main DB.

    Asserted by VISIBILITY, not by reading back a setting. ``from_url(url, db=1)`` on a URL ending
    in ``/0`` silently keeps db 0 — redis-py parses the URL and then lets it overwrite the explicit
    kwarg — so a test that checked ``settings.agent_run_redis_db == 1`` would have passed while both
    contours shared one database and a ``FLUSHDB`` of either took the other with it.
    """
    settings = _settings(redis_url, AGENT_RUN_REDIS_DB=1)
    assert redis_url.endswith(
        "/0"
    ), "the URL must carry a path for this regression to mean anything"

    agent_client = redis_asyncio.from_url(
        url_with_db(settings.redis_url, settings.agent_run_redis_db), decode_responses=True
    )
    main_client = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        await agent_client.set("isolation-probe", "agent-contour")
        assert await agent_client.get("isolation-probe") == "agent-contour"
        assert (
            await main_client.get("isolation-probe") is None
        ), "the agent-run key is visible from the main DB — the contours share a database"
        # And the reverse direction, so the test cannot pass by writing nowhere at all.
        await main_client.set("isolation-probe-main", "rate-limiting")
        assert await agent_client.get("isolation-probe-main") is None
    finally:
        await agent_client.flushdb()
        await main_client.flushdb()
        await agent_client.aclose()
        await main_client.aclose()


def test_url_with_db_rewrites_every_url_form() -> None:
    """The pure half of the same regression, including the form redis-py gets wrong."""
    assert url_with_db("redis://h:6379/0", 1) == "redis://h:6379/1"
    assert url_with_db("redis://h:6379", 1) == "redis://h:6379/1"
    assert url_with_db("rediss://h:6379/0", 2) == "rediss://h:6379/2"
    # A unix socket carries the DB in the query string, not the path.
    assert "db=1" in url_with_db("unix:///var/run/redis.sock", 1)
    assert "db=2" in url_with_db("unix:///var/run/redis.sock?db=9", 2)
    assert "db=9" not in url_with_db("unix:///var/run/redis.sock?db=9", 2)
    # An unknown scheme is returned untouched — the only honest answer for a form we do not know.
    assert url_with_db("memory://x", 3) == "memory://x"


def test_redis_py_really_does_discard_the_db_kwarg(redis_url: str) -> None:
    """The upstream behaviour this helper exists for, pinned against the real library.

    If redis-py ever fixes it, this test fails and ``url_with_db`` can be reconsidered deliberately
    instead of surviving as folklore.
    """
    client = redis_asyncio.from_url(redis_url, db=7)
    assert (
        client.connection_pool.connection_kwargs["db"] == 0
    ), "redis-py now honours the db= kwarg over the URL path — url_with_db may be redundant"


# ==================================================================================================
# The event pipeline: epoch, trimming, TTL.
# ==================================================================================================
async def test_every_ring_element_and_every_published_message_carries_the_epoch(
    bus_factory: Any,
) -> None:
    """A subscriber must be able to tell the generation from the event alone.

    Not "the epoch is stored somewhere" — in EVERY element and EVERY message. A live subscriber
    never reads the epoch key; all it ever sees is what arrives on the channel, so an epoch missing
    from the message body is an epoch the subscriber cannot act on.
    """
    bus, client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)
    assert epoch is not None

    received: list[dict[str, Any]] = []
    pubsub = client.pubsub()
    await pubsub.subscribe(AgentRunEventBus.channel(run_id))

    async def _collect() -> None:
        while len(received) < 3:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
            if message is not None:
                received.append(json.loads(message["data"]))

    collector = asyncio.create_task(_collect())
    await asyncio.sleep(0.1)
    for i in range(3):
        assert await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode()) == i + 1
    await asyncio.wait_for(collector, timeout=10)
    await pubsub.unsubscribe()
    await pubsub.aclose()

    stored = _elements(await client.lrange(f"agent:run:{run_id}:events", 0, -1))
    assert [e["epoch"] for e in stored] == [epoch] * 3, "a ring element without its generation"
    assert [e["seq"] for e in stored] == [1, 2, 3]
    assert [m["epoch"] for m in received] == [epoch] * 3, "a channel message without its generation"
    assert [m["seq"] for m in received] == [1, 2, 3]


async def test_the_event_pipeline_never_creates_the_epoch_key(bus_factory: Any) -> None:
    """ "The epoch key is missing" must keep meaning "the generation is gone".

    Both the stale-cursor rule (§3.2) and downstream close rule 3 (§3.3) are built on that meaning,
    so an event that silently recreated the key would not break a test — it would quietly destroy
    the definition those two rules rest on.
    """
    bus, client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch_key = f"agent:run:{run_id}:epoch"

    seq = await bus.publish(run_id, epoch="orphaned-generation", raw=b"data: x\n\n")
    assert seq == 1, "the event itself must still be stored"
    assert await client.exists(epoch_key) == 0, "publishing recreated the epoch key"

    # ... and once ensure_epoch HAS created it, publishing refreshes rather than replaces it.
    created = await bus.ensure_epoch(run_id)
    await bus.publish(run_id, epoch=created, raw=b"data: y\n\n")
    assert await client.get(epoch_key) == created


async def test_expire_is_reapplied_to_every_key_on_every_event(bus_factory: Any) -> None:
    """The ring must not evaporate mid-run: each event refreshes the TTL of ALL of the run's keys.

    Asserted by driving the TTL down between events — if any key were expired only once, at
    creation, its remaining TTL would keep falling instead of resetting.
    """
    bus, client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)
    await bus.publish(run_id, epoch=epoch, raw=b"data: 1\n\n")

    keys = [f"agent:run:{run_id}:{s}" for s in ("events", "seq", "bytes", "epoch")]
    for key in keys:
        await client.expire(key, 5)
        assert await client.ttl(key) <= 5

    await bus.publish(run_id, epoch=epoch, raw=b"data: 2\n\n")
    for key in keys:
        ttl = await client.ttl(key)
        assert ttl > 5, f"{key} was not refreshed by the event pipeline (ttl={ttl})"


async def test_the_event_count_ceiling_trims_from_the_head(bus_factory: Any) -> None:
    """Oldest-first eviction: the newest events, which a live client awaits, must survive."""
    bus, client, _settings_obj = bus_factory(AGENT_RUN_EVENT_BUFFER_MAX=5)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)
    for i in range(1, 9):
        await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode())

    stored = _elements(await client.lrange(f"agent:run:{run_id}:events", 0, -1))
    assert len(stored) == 5
    assert [e["seq"] for e in stored] == [4, 5, 6, 7, 8], "trimmed from the wrong end"


async def test_the_byte_ceiling_trims_from_the_head(bus_factory: Any) -> None:
    """The second ceiling, independent of the first: a few large events must also be bounded."""
    bus, client, _settings_obj = bus_factory(
        AGENT_RUN_EVENT_BUFFER_MAX=1000, AGENT_RUN_EVENT_BUFFER_MAX_BYTES=2000
    )
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)
    payload = b"x" * 400
    for _ in range(10):
        await bus.publish(run_id, epoch=epoch, raw=payload)

    stored = _elements(await client.lrange(f"agent:run:{run_id}:events", 0, -1))
    assert 0 < len(stored) < 10, f"the byte ceiling did not bind: {len(stored)} elements"
    seqs = [e["seq"] for e in stored]
    assert seqs == sorted(seqs) and seqs[-1] == 10, "the newest event must survive"


async def test_one_oversized_event_does_not_empty_the_ring(bus_factory: Any) -> None:
    """A single event larger than the WHOLE ceiling must still be served.

    Otherwise the client sees neither the event nor any reason for its absence — the ring would be
    empty, and an empty ring is indistinguishable from a run that has not started. The gap is not
    silent either way: the broker emits ``run.truncated`` when the first seq it can serve is beyond
    the cursor.
    """
    bus, client, _settings_obj = bus_factory(AGENT_RUN_EVENT_BUFFER_MAX_BYTES=500)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)

    seq = await bus.publish(run_id, epoch=epoch, raw=b"y" * 5000)
    assert seq == 1

    stored = _elements(await client.lrange(f"agent:run:{run_id}:events", 0, -1))
    assert len(stored) == 1, "the oversized event was dropped, leaving an unexplainable empty ring"
    assert stored[0]["seq"] == 1


async def test_invalid_utf8_round_trips_byte_for_byte(bus_factory: Any) -> None:
    """The ring relays BYTES. An event the image sent must come back exactly as it was sent.

    Upstream payloads are not ours to normalise — the whole ADR-065 defect began with an assumption
    about payload shape — so a lossy encode/decode here would corrupt a stream we are only meant to
    carry.
    """
    bus, _client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)

    payloads = [
        b"data: \xff\xfe invalid utf-8\n\n",
        "data: кириллица\n\n".encode(),
        b"data: \x00\x01\x02 control bytes\n\n",
        b"data: " + bytes(range(256)) + b"\n\n",
    ]
    for raw in payloads:
        await bus.publish(run_id, epoch=epoch, raw=raw)

    replayed = await bus.replay(run_id)
    assert [event.data for event in replayed] == payloads, "the ring is not byte-transparent"


async def test_replay_returns_the_whole_ring_oldest_first(bus_factory: Any) -> None:
    """The broker needs the FIRST available seq to decide whether a truncation marker is due, which
    is impossible if the read filtered by cursor."""
    bus, _client, _settings_obj = bus_factory(AGENT_RUN_EVENT_BUFFER_MAX=3)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)
    for i in range(1, 6):
        await bus.publish(run_id, epoch=epoch, raw=f"data: {i}\n\n".encode())

    replayed = await bus.replay(run_id)
    assert [e.seq for e in replayed] == [3, 4, 5]
    assert await bus.current_seq(run_id) == 5


async def test_a_corrupt_element_does_not_abort_an_otherwise_serviceable_replay(
    bus_factory: Any,
) -> None:
    """One unparseable element must cost that element, not the whole reconnect."""
    bus, client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    epoch = await bus.ensure_epoch(run_id)
    await bus.publish(run_id, epoch=epoch, raw=b"data: a\n\n")
    await client.rpush(f"agent:run:{run_id}:events", "{not json at all")
    await bus.publish(run_id, epoch=epoch, raw=b"data: b\n\n")

    replayed = await bus.replay(run_id)
    assert [e.data for e in replayed] == [b"data: a\n\n", b"data: b\n\n"]


# ==================================================================================================
# Degradation: a Redis failure costs the live stream and nothing else.
# ==================================================================================================
async def test_a_redis_failure_never_raises_into_the_consumer(redis_url: str) -> None:
    """Money, status and /state live in Postgres; the ring is a convenience.

    A raised exception here would cost the BILLING of a whole run to save a live stream, which is
    the wrong trade in every direction. Every write path must degrade to ``None``/``False``.
    """
    settings = _settings(redis_url)
    dead = redis_asyncio.from_url("redis://127.0.0.1:1/1", socket_connect_timeout=1)
    bus = AgentRunEventBus(dead, settings)
    run_id = "run_dead_redis"

    assert await bus.ensure_epoch(run_id) is None
    assert await bus.current_epoch(run_id) is None
    assert await bus.publish(run_id, epoch="e", raw=b"data: x\n\n") is None
    assert await bus.current_seq(run_id) == 0
    assert await bus.replay(run_id) == []
    # UNKNOWN, not HELD_ELSEWHERE (ADR-067 §4.1): "Redis did not answer" and "somebody else holds
    # it" are DIFFERENT outcomes, and the consumer drives the run on the first and stands down on
    # the second. A bool here is precisely the defect §4.1 was written to remove.
    assert await bus.acquire_lease(run_id, "owner") is LeaseAcquisition.UNKNOWN
    # `None` means UNKNOWN and must stay distinct from False (= "definitely not held"): the orphan
    # sweep is allowed to finalize on False and must never finalize on None.
    assert await bus.lease_alive(run_id) is None
    assert await bus.uptime_seconds() is None
    await dead.aclose()


async def test_lease_alive_distinguishes_unknown_from_absent(bus_factory: Any) -> None:
    """``None`` (Redis unreachable) and ``False`` (no lease) drive OPPOSITE decisions in the sweep,
    so conflating them would finalize live runs during a Redis outage."""
    bus, _client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    assert await bus.lease_alive(run_id) is False, "no lease must be a definite False"
    assert await bus.acquire_lease(run_id, "owner-1") is LeaseAcquisition.ACQUIRED
    assert await bus.lease_alive(run_id) is True
    assert (
        await bus.acquire_lease(run_id, "owner-2") is LeaseAcquisition.HELD_ELSEWHERE
    ), "a held lease is not re-acquirable"


async def test_release_only_removes_a_lease_we_still_own(bus_factory: Any) -> None:
    """Compare-and-delete: a worker whose lease already lapsed and was taken over must not be able
    to delete the NEW owner's lease."""
    bus, _client, _settings_obj = bus_factory()
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    assert await bus.acquire_lease(run_id, "owner-1") is LeaseAcquisition.ACQUIRED
    await bus.release_lease(run_id, "someone-else")
    assert await bus.lease_alive(run_id) is True, "a foreign release removed the lease"

    await bus.release_lease(run_id, "owner-1")
    assert await bus.lease_alive(run_id) is False
