"""Integration: axis C of the cursor contract — every loss is ANNOUNCED (ADR-067 §3.2.1, C1-C3).

WHY THIS MODULE IS SEPARATE. §3.2.1 derives correctness as ``A ∧ B ∧ C``: the baseline's provenance
(A), the comparability of the two numbers (B), and the ANNOUNCEMENT of any break in either (C). Two
of the four silent-stream entrances found in this contour had a perfectly correct comparison and
failed on C alone — the replay marker died together with an empty result set, and the periodic
generation branch reset the baseline without saying anything. Axis A and B are covered by the
neighbouring modules; this one is about C, and it is a different question, not a finer version of
the same one.

⚠️ TWO LEVELS, and one does not substitute for the other (§3.2.1). ``run.truncated`` is the CLIENT
notification; a ``warning`` in the log is OPERATOR observability. A test that accepts a log line as
evidence of C would pass on an implementation the client cannot survive: the log does not reach it.
Every assertion here is on the delivered SSE stream.

⚠️ "EXACTLY ONE MARKER" IS PER CHANNEL MESSAGE, NEVER PER SCENARIO. One scenario legitimately
produces several — the ring trimming that opens a hole also raises the first replayed ``seq``, so C3
fires on the replay and C1 fires later on the message that arrives inside the hole. An assertion of
"one marker per scenario" would declare the correct implementation defective, which is why the
counting below is always scoped to the message that caused it.

THE SEAM USED THROUGHOUT. ``stream()`` subscribes and only then reads the ring (§3.2 step 1,
TD-047), and both happen inside the generator before it yields anything, so no test can interleave
traffic between them from outside. ``bus.replay`` is therefore wrapped: the wrapper publishes while
the subscription is already live and then delegates. That is not a mock of the behaviour under
test — it is the only way to put real traffic in a window the design deliberately made narrow.
"""

from __future__ import annotations

import asyncio
import contextlib
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

_EPOCH = "gen-cccccccc"


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
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(_session: AsyncSession, **overrides: Any) -> tuple[Any, AgentRunEventBus, Any]:
        db = counter["n"]
        counter["n"] += 1
        base: dict[str, Any] = {
            "REDIS_URL": redis_url,
            "AGENT_RUN_REDIS_DB": db,
            "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": 60,
            "AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS": 1,
            # The supervisor's period, split out of the lease TTL. Short so a tick lands
            # inside the scenes that need one; at the 10s default they would get none.
            "AGENT_RUN_SUBSCRIBER_PROBE_SECONDS": 0.5,
            "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS": 30,
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
        return broker, bus, settings

    yield _make

    for client in clients:
        try:
            await client.flushdb()
            await client.aclose()
        except RedisError:  # pragma: no cover - teardown best effort
            pass


async def _seed_run(maker: async_sessionmaker[AsyncSession], run_id: str) -> uuid.UUID:
    async with maker() as session:
        user_id = await seed_user(session, subscription="active", balance=100)
        await session.execute(
            text(
                "INSERT INTO agent_runs (run_id, user_id, session_id, status, model) "
                "VALUES (:r, :u, 'sess-1', 'running', 'm')"
            ),
            {"r": run_id, "u": str(user_id)},
        )
        await session.commit()
    return user_id


async def _hold_lease(bus: AgentRunEventBus, run_id: str) -> None:
    """A live lease, so no closing rule fires while the scenario plays out."""
    await bus._redis.set(f"agent:run:{run_id}:lease", "consumer-1", ex=120)


def _is_marker(block: bytes) -> bool:
    body = block.split(b"data: ", 1)[1]
    try:
        parsed = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("event") == EVENT_RUN_TRUNCATED


def _payload(block: bytes) -> bytes:
    return block.split(b"data: ", 1)[1]


def _shape(blocks: list[bytes]) -> list[str]:
    """Readable rendering: ``MARK`` for a marker, the raw body otherwise. Used in messages."""
    return ["MARK" if _is_marker(b) else _payload(b).strip().decode() for b in blocks]


async def _collect(
    stream: AsyncIterator[bytes], *, want: int, deadline: float = 20.0
) -> list[bytes]:
    collected: list[bytes] = []
    closed = False

    async def _run() -> None:
        nonlocal closed
        async for block in stream:
            collected.append(block)
            if len(collected) >= want:
                return
        closed = True

    try:
        await asyncio.wait_for(_run(), timeout=deadline)
    except TimeoutError:
        raise AssertionError(
            f"delivered {len(collected)} of {want} blocks in {deadline}s: {_shape(collected)}. "
            "A stream that drops live events never closes either (the lease is alive), so this is "
            "silence, not slowness."
        ) from None
    assert not closed, f"the stream closed after {_shape(collected)}; it must stay open here"
    return collected


def _publishing_replay(bus: AgentRunEventBus, plan: list[bytes]) -> Any:
    """Wrap ``bus.replay`` so ``plan`` is published while the subscription is ALREADY live.

    The window between ``SUBSCRIBE`` and ``LRANGE`` is where C1's scenario lives, and after TD-047
    it is genuinely narrow — which is the point of the fix and the reason it cannot be hit by
    scheduling from a test. Publishing from inside the replay call puts real events in exactly that
    window, on the real channel, with real ring trimming.
    """
    original = bus.replay

    async def _replay(run_id: str) -> Any:
        for raw in plan:
            await bus.publish(run_id, epoch=_EPOCH, raw=raw)
        return await original(run_id)

    return _replay


# ==================================================================================================
# C1 — a hole BELOW the replay's lowest delivered seq is a break, not a proven duplicate.
# ==================================================================================================
@pytest.mark.asyncio
async def test_an_event_evicted_before_the_replay_read_it_is_delivered_and_announced(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """``replay_min_seq``: the baseline is an upper MARK, not the set of delivered ``seq``.

    Sequence, all of it real: the session subscribes; five events are published; the ring ceiling of
    three pushes ``seq`` 1-2 out of the head; ``LRANGE`` then reads a ring that STARTS at 3. So 1-2
    were never delivered, yet they sit *below* the baseline — and a rule that drops everything under
    the mark as "already delivered" loses them in silence.

    ⚠️ This is the assertion that makes the invariant checkable at all. "We only drop what was
    delivered" is unfalsifiable against a scalar baseline, because the scalar cannot distinguish
    "below the mark" from "delivered".

    ⚠️ TWO markers here and both are correct — one per causing MESSAGE, never one per scenario:
    C3 on the replay (it starts at 3, past the beginning) and C1 on the first message inside the
    hole. The hole itself earns exactly ONE, covering both events in it.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session, AGENT_RUN_EVENT_BUFFER_MAX=3)
        await _hold_lease(bus, run_id)
        bus.replay = _publishing_replay(  # type: ignore[method-assign]
            bus, [f"data: e-{i}\n\n".encode() for i in range(1, 6)]
        )

        # 1 replay marker (C3) + 3 replayed + 1 hole marker (C1) + 2 events from the hole.
        blocks = await _collect(broker.stream(run_id=run_id, cursor=Cursor()), want=7)

    bodies = [_payload(b) for b in blocks if not _is_marker(b)]
    assert bodies == [
        b"e-3\n\n",
        b"e-4\n\n",
        b"e-5\n\n",
        b"e-1\n\n",
        b"e-2\n\n",
    ], f"the events evicted before the replay read them were lost: {_shape(blocks)}"
    assert _is_marker(blocks[0]), "the replay starting past the beginning was not announced (C3)"
    # The hole gets ONE marker for both of its events, and it precedes the first of them.
    hole_start = bodies.index(b"e-1\n\n") + 1  # +1 for the leading C3 marker
    assert _is_marker(
        blocks[hole_start]
    ), f"the event below the replay window arrived without a marker: {_shape(blocks)}"
    assert sum(1 for b in blocks if _is_marker(b)) == 2, (
        f"expected exactly two markers — C3 for the replay and ONE for the whole hole — got "
        f"{_shape(blocks)}. A marker per event in the hole would be the naive implementation."
    )


# ==================================================================================================
# C2 — a jump FORWARD is a break. Nothing looked forward before this measure existed.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_skipped_seq_in_the_live_phase_is_announced_and_the_event_delivered(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """C2: ``delivered > 0`` and ``event.seq > delivered + 1`` ⇒ marker, then deliver.

    The gap is produced the way the design says it can occur without anyone enumerating a cause: a
    channel message with a ``seq`` that skips numbers. Its real sources are an unparseable channel
    message (whose ``seq`` we never learn, so the log is all that is available at the moment of
    detection — the CLIENT learns here, on the next event), a message lost on the channel, and the
    pre-TD-047 ``LRANGE``→``SUBSCRIBE`` window.

    ⚠️ The old rule ``event.seq > delivered`` waves every jump through in silence: it answers "is
    this new?", and C2 asks "is this NEXT?".
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        bus.replay = _publishing_replay(bus, [b"data: e-1\n\n"])  # type: ignore[method-assign]

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=3))
        await asyncio.sleep(0.5)  # replay delivers e-1; delivered == 1

        # Publish straight onto the channel with seq 5: numbers 2-4 exist for this session's
        # purposes and it will never serve them.
        channel = AgentRunEventBus.channel(run_id)
        await bus._redis.publish(
            channel, json.dumps({"epoch": _EPOCH, "seq": 5, "data": "data: e-5\n\n"})
        )

        blocks = await collector

    assert _payload(blocks[0]) == b"e-1\n\n", f"the replay did not run: {_shape(blocks)}"
    assert _is_marker(
        blocks[1]
    ), f"a jump from delivered=1 to seq=5 was waved through in silence: {_shape(blocks)}"
    assert _payload(blocks[2]) == b"e-5\n\n", "the event after the gap was not delivered"


@pytest.mark.asyncio
async def test_a_continuous_live_stream_never_produces_a_marker(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The paired negative for C2, and the more expensive direction to get wrong.

    A marker tells the client to discard what it has and refetch. Emitting one on an unbroken stream
    would make every ordinary run pay that cost, and a signal that fires when nothing is wrong stops
    being handled — which is exactly the argument §3.2.1 uses to keep the empty-cursor case silent.

    Also covers the ``delivered == 0`` boundary: the FIRST event of the session must not be judged
    against a "previous" that does not exist. That case belongs to C3, on its own terms.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=4))
        await asyncio.sleep(0.4)
        # Perfectly continuous from seq 1: nothing was ever skipped, nothing was ever delivered
        # before, so neither C2 nor C3 has anything to announce.
        for i in range(1, 5):
            await bus.publish(run_id, epoch=_EPOCH, raw=f"data: e-{i}\n\n".encode())

        blocks = await collector

    assert _shape(blocks) == ["e-1", "e-2", "e-3", "e-4"], (
        f"an unbroken stream from seq 1 produced a marker: {_shape(blocks)} — clients would be "
        "told to refetch on every healthy run and would learn to ignore the signal"
    )


# ==================================================================================================
# C3 — the first thing delivered in a session starting past the beginning.
# ==================================================================================================
@pytest.mark.asyncio
async def test_an_unread_ring_does_not_hide_the_start_of_the_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """C3 in the LIVE phase — the case the phase-scoped rule missed entirely.

    ``replay()`` returns ``[]`` for an empty ring AND for a ring it could not READ (``RedisError``
    is swallowed there). The two are opposite facts: in one there is no history, in the other the
    history exists and we are blind to it. A session that adopts a generation on the second and then
    delivers ``seq = 50`` as its first event hands the client the middle of a run with no sign that
    1-49 ever existed.

    ⚠️ Fails on the rule "an empty cursor warrants no marker": that reasoning substituted "the
    client received no prefix" for "no prefix exists", and only the second licenses silence.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # The ring is NOT empty — it holds 49 events — but the read fails, exactly as a RedisError
        # in replay() would surface to the broker.
        for i in range(1, 50):
            await bus.publish(run_id, epoch=_EPOCH, raw=f"data: old-{i}\n\n".encode())

        async def _unreadable_replay(_run_id: str) -> Any:
            return []

        bus.replay = _unreadable_replay  # type: ignore[method-assign]
        assert await bus.current_epoch(run_id) is None, "no key: the session must adopt"

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=2))
        await asyncio.sleep(0.4)
        await bus.publish(run_id, epoch=_EPOCH, raw=b"data: live-50\n\n")

        blocks = await collector

    assert _is_marker(blocks[0]), (
        f"the first delivered event was seq 50 and the client was not told that 1-49 exist and "
        f"will never arrive: {_shape(blocks)}"
    )
    assert _payload(blocks[1]) == b"live-50\n\n", "the event itself was not delivered"


@pytest.mark.asyncio
async def test_a_first_connection_to_a_complete_ring_gets_no_marker(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The paired negative for C3: nothing is missing, so nothing is announced.

    Distinguishes C3 from "always mark on an empty cursor". The filter is 0 and the first delivered
    ``seq`` is 1, i.e. exactly ``filter + 1`` — continuous, and the client holds the whole run.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        for i in range(1, 4):
            await bus.publish(run_id, epoch=_EPOCH, raw=f"data: e-{i}\n\n".encode())

        blocks = await _collect(broker.stream(run_id=run_id, cursor=Cursor()), want=3)

    assert _shape(blocks) == [
        "e-1",
        "e-2",
        "e-3",
    ], f"a complete ring from seq 1 was announced as truncated: {_shape(blocks)}"


# ==================================================================================================
# The `arrived` / `delivered` discriminator — observable now that the overlap actually exists.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_ring_and_live_overlap_is_silent_and_delivered_once(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The ordinary overlap: an event in the ring AND on the channel — one delivery, no marker.

    ⚠️ THIS is the test that catches ``arrived`` being replaced by ``delivered`` in the regression
    check. Both discriminators answer "did the counter go backwards?", and they differ only where an
    event arrives whose ``seq`` is at or below the baseline: the overlap. Compared against
    ``delivered`` (an upper mark that the replay already advanced), every replayed event coming back
    on the channel looks like a counter reset, so a HEALTHY stream opens with a spurious
    ``run.truncated`` and a baseline reset. Compared against ``arrived`` — the previous seq seen on
    the CHANNEL — it does not, because a duplicate reaching us from two different sources never
    breaks the channel's own monotonicity.

    ⚠️ Was inert until TD-047: with ``LRANGE`` before ``SUBSCRIBE`` (and a lazy subscribe on top) the
    overlap did not exist, so both discriminators passed. It is exercised for real here — the
    published events land while the subscription is live and also sit in the ring.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        bus.replay = _publishing_replay(  # type: ignore[method-assign]
            bus, [f"data: e-{i}\n\n".encode() for i in range(1, 4)]
        )

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=4))
        await asyncio.sleep(0.6)  # let all three channel copies be consumed and dropped
        # A genuinely new event, so the collector finishes on content rather than on a timeout.
        await bus.publish(run_id, epoch=_EPOCH, raw=b"data: e-4\n\n")

        blocks = await collector

    assert _shape(blocks) == ["e-1", "e-2", "e-3", "e-4"], (
        f"the ring+live overlap was not handled silently: {_shape(blocks)}. A marker here means "
        "the regression check compares against `delivered` instead of `arrived`, so every healthy "
        "stream opens by telling the client its transcript is broken."
    )


# ==================================================================================================
# Backpressure — a DISCRIMINATOR, not a requirement. Pins today's behaviour, asserts no policy.
#
# ⚠️ READ THIS BEFORE CHANGING THE TEST BELOW. It deliberately does NOT assert that the right client
# gets disconnected, because the disconnection policy is architect's to decide and has not been
# decided. What it asserts is the DIFFERENCE — or rather its absence: fed the identical burst,
# a fast consumer and a slow consumer are treated identically today.
#
# The substitution it exposes (found by review, confirmed here by reading `_iter_messages`): the
# counter `pending` increments once per non-empty `get_message` and is reset to 0 by EVERY empty
# poll. That measures how many messages arrived back-to-back — a property of the PUBLISHER's
# burstiness — and not how far behind the subscriber has fallen, which is what a backpressure
# threshold is supposed to bound. Two mirror-image consequences follow, and both are wrong:
#
#   * a fast, healthy client is disconnected whenever the consumer happens to emit more than
#     AGENT_RUN_SUBSCRIBER_QUEUE_MAX events in one uninterrupted burst, however promptly the client
#     consumes them;
#   * a genuinely slow client is never disconnected as long as idle gaps keep arriving between
#     bursts, because each gap zeroes the counter no matter how far behind it is.
#
# A same-outcome result below is therefore EVIDENCE OF THE DEFECT, not a passing behaviour check.
# Once a policy exists, this test is replaced by an asserting pair (fast survives / slow dropped).
# ==================================================================================================
_QUEUE_MAX = 50
_BURST = 600


async def _run_twin(
    broker: Any, bus: AgentRunEventBus, run_id: str, *, per_block_delay: float
) -> tuple[int, bool]:
    """Open a stream on an EMPTY ring, then publish the burst LIVE and consume at a chosen pace.

    ⚠️ The burst must arrive AFTER the stream is open, and an earlier version of this test got that
    wrong: it published first, so the events came through the REPLAY. The replay is not subject to
    the backpressure counter at all, and the slow twin then spent the whole budget inside it and
    never reached the live phase — so the twins differed because one never ran the code under
    test. A difference produced by a confound is worse than no measurement, because it looks like a
    result. Both twins now meet the identical input on the identical path: one uninterrupted run of
    ``_BURST`` messages on the channel.

    Returns (blocks received, whether the broker ended the stream). Both outcomes are legitimate
    results — the run stays ``running`` with a live lease throughout, so only the backpressure rule
    can end the iteration.
    """
    received = 0
    closed = False
    stream = broker.stream(run_id=run_id, cursor=Cursor())

    async def _consume() -> None:
        nonlocal received, closed
        async for _block in stream:
            received += 1
            if per_block_delay:
                await asyncio.sleep(per_block_delay)
        closed = True

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.4)  # the subscription is live and the (empty) replay is done
    for i in range(1, _BURST + 1):
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: e-{i}\n\n".encode())

    # Neither outcome may fail the test: the stream ending means this subscriber was dropped, the
    # timeout means it was not. Forcing either would decide the question the twins exist to answer.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(consumer, timeout=10.0)
    if not consumer.done():
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    return received, closed


@pytest.mark.asyncio
async def test_backpressure_disconnects_the_slow_consumer_and_spares_the_fast_one(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """REQUIREMENT (flipped after TD-048): the pace of the consumer decides, and nothing else.

    Twins on one and the same burst of 600 events with a threshold of 50 — the only difference
    between them is how fast they read. The threshold is declared as QUEUE DEPTH, so the fast twin
    must survive and the slow one must be dropped.

    ⚠️ This test was committed first as a PIN of the opposite behaviour (assert the two outcomes
    COINCIDE), because the old counter measured messages between two idle polls of the channel — the
    size of a burst, in which the client's pace plays no part at all. §3.2.2 prescribes a two-stage
    fixation and this is the second stage: the assertion is now the requirement.

    ⚠️ RED until the live-phase depth disconnect lands (TD-048): today nothing disconnects a
    subscriber at all, so the slow twin survives too.
    """
    fast_run = f"run_{uuid.uuid4().hex[:8]}"
    slow_run = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, fast_run)
    await _seed_run(db_sessionmaker, slow_run)

    async with db_sessionmaker() as session:
        # A run each, so neither twin inherits the other's ring and both start from an empty one.
        broker, bus, _settings = broker_factory(
            session,
            AGENT_RUN_SUBSCRIBER_QUEUE_MAX=_QUEUE_MAX,
            AGENT_RUN_EVENT_BUFFER_MAX=_BURST * 2,
        )
        await _hold_lease(bus, fast_run)
        await _hold_lease(bus, slow_run)

        fast_received, fast_closed = await _run_twin(broker, bus, fast_run, per_block_delay=0.0)
        slow_received, slow_closed = await _run_twin(broker, bus, slow_run, per_block_delay=0.02)

    outcome = (
        f"fast: closed={fast_closed} after {fast_received} blocks | "
        f"slow: closed={slow_closed} after {slow_received} blocks"
    )
    # Anti-confound: both twins must have reached the live phase, or the comparison is not a
    # measurement (see _run_twin — an earlier version differed only because one twin never ran it).
    assert fast_received > 0 and slow_received > 0, f"a twin never received anything — {outcome}"
    assert not fast_closed, (
        f"the FAST twin was disconnected — {outcome}. It kept up throughout, so the threshold is "
        "still reading something other than its lag: a healthy client is being punished for the "
        "consumer's burstiness."
    )
    assert slow_closed, (
        f"the SLOW twin was NOT disconnected — {outcome}. The protection does not fire on its own "
        "target input: depth is what the setting names, and this client is demonstrably behind."
    )
