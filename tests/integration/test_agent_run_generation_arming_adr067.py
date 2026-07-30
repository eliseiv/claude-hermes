"""Integration: a client session must ARM the generation check, however it opened (§3.3.1a, TD-044).

THE DEFECT. ``broker.stream`` took the session's generation as ``epoch or ""`` and put BOTH §3.3.1
checks behind ``if current_generation``. A session that opened while ``current_epoch`` returned
``None`` — the key absent, or Redis not answering, which the transport deliberately does not
distinguish — therefore ran with §3.3.1 disabled for its entire life. Let the generation change
afterwards and ``seq`` restarts at 1 against an advanced cursor: the dedup rule drops every event,
no close rule fires (the lease is alive), and the client holds an open, permanently SILENT SSE
stream. No status code, no log line, no metric — observable only as "the answer never arrives".

WHY IT BECAME URGENT rather than exotic: §4.1 decided that a consumer keeps driving its run when
Redis is unreachable, which turned "the client connected while there was no generation key" into a
SUPPORTED path. The fix's entrance was widened by our own previous decision.

HOW THESE TESTS ARE FRAMED. The ADR asks for an INVARIANT — "no event is delivered to the client
while the generation check is unarmed" — rather than assertions about which line of code runs. Its
observable form is :func:`assert_no_silent_generation_change`: across the delivered stream, the
epoch in ``id: <epoch>-<seq>`` may never change from block to block without a ``run.truncated``
between them. Adoption is exempt by construction, because a session that adopts has delivered
nothing yet — there is no earlier block to change away from. Every scenario below is checked against
it in addition to its own specific assertions.

FOUR sub-cases (§3.3.1a as revised 2026-07-31): (а) no key, non-empty ring → the generation comes
from the last ring element; (б) no key, empty ring, EMPTY cursor → silent adoption, no marker;
(в) the same for a QUIET run, armed by the periodic check; (г) no key, empty ring, NON-EMPTY cursor
→ adoption plus a MANDATORY ``run.truncated``. The marker rule is conditional on the incoming
cursor and on nothing else: a client holding a prefix is told that the tail it is about to receive
could not be checked against it, and a client holding nothing is not told anything, because there
the marker would carry no information at all.

⚠️ The whole module runs on LIVE streams, never on reconnects: a reconnect exercises
``_validate_cursor`` and would stay green with the mid-stream arming deleted entirely.
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

_EPOCH_A = "gen-aaaaaaaa"
_EPOCH_B = "gen-bbbbbbbb"


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
    # Redis ships with 16 logical DBs (0-15); 0 is the one in REDIS_URL, which the
    # config validator forbids reusing. This module owns its own container.
    counter = {"n": 2}

    def _make(_session: AsyncSession, **overrides: Any) -> tuple[Any, AgentRunEventBus]:
        db = counter["n"]
        counter["n"] += 1
        base: dict[str, Any] = {
            "REDIS_URL": redis_url,
            "AGENT_RUN_REDIS_DB": db,
            "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": 60,
            # The idle rule needs a LIVE lease to be harmless; every test here holds one, so a
            # short idle limit only makes an accidental close fail fast instead of hanging.
            "AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS": 1,
            # Doubles as the period of the §3.3 rule-4 check, which is where the SECOND arming
            # point lives. Small so a quiet run reaches it inside a test.
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
    """Keep a live lease for the whole test WITHOUT lengthening the rule-4 check period.

    ``acquire_lease`` sets the key to ``AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS``, and that same knob
    is the period of the periodic check where the second arming point lives. Raising it to outlive
    the test would push the periodic check out of reach; writing the key directly separates the two,
    so a lease expiring mid-test can never be mistaken for the behaviour under test.
    """
    await bus._redis.set(f"agent:run:{run_id}:lease", "consumer-1", ex=120)


def _ids(blocks: list[bytes]) -> list[str]:
    return [b.split(b"\n", 1)[0].removeprefix(b"id: ").decode() for b in blocks]


def _epochs(blocks: list[bytes]) -> list[str]:
    return [i.rpartition("-")[0] for i in _ids(blocks)]


def _is_truncation(block: bytes) -> bool:
    body = block.split(b"data: ", 1)[1]
    try:
        parsed = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("event") == EVENT_RUN_TRUNCATED


def assert_no_silent_generation_change(blocks: list[bytes]) -> None:
    """THE INVARIANT, in its observable form (ADR-067 §3.3.1a).

    "No event is delivered while the generation check is unarmed" cannot be read off the broker's
    internals from outside, and asserting on its internals would pin the implementation rather than
    the rule. What an unarmed session produces is always the same visible symptom, though: two
    consecutive blocks from DIFFERENT generations with no ``run.truncated`` between them — the
    client is silently told to continue a numbering that restarted, which is the silent stream.

    Adoption does not violate this and cannot: a session adopts only when it has delivered nothing,
    so there is no preceding block whose generation could differ.
    """
    previous: str | None = None
    for index, block in enumerate(blocks):
        epoch = _ids([block])[0].rpartition("-")[0]
        if _is_truncation(block):
            # The marker itself announces the change; it carries the NEW generation.
            previous = epoch
            continue
        if previous is not None and epoch != previous:
            raise AssertionError(
                f"block {index} switched generation {previous!r} → {epoch!r} with no "
                f"run.truncated between them — the session was serving events with its generation "
                f"check unarmed, which is the silent stream TD-044 is about"
            )
        previous = epoch
    return None


async def _collect_until(
    stream: AsyncIterator[bytes],
    *,
    match: Any,
    what: str,
    deadline: float = 20.0,
) -> list[bytes]:
    """Collect until a block satisfying ``match`` arrives. Everything seen is returned.

    Used where the CLAIM is "this event reaches the client at all", independent of how many
    synthetic blocks legitimately precede it. Counting blocks instead would couple such a test to
    the marker policy of §3.3.1a, which is a different rule with its own test — and a test that
    fails when an unrelated rule changes is a test nobody can read.
    """
    collected: list[bytes] = []

    async def _run() -> None:
        async for block in stream:
            collected.append(block)
            if match(block):
                return

    try:
        await asyncio.wait_for(_run(), timeout=deadline)
    except TimeoutError:
        raise AssertionError(
            f"{what} never reached the client in {deadline}s — got {len(collected)} block(s): "
            f"{_ids(collected)}. A session that discards live events never closes either (the "
            f"lease is alive), so this is the silent stream, not a slow one."
        ) from None
    return collected


async def _collect(
    stream: AsyncIterator[bytes], *, want: int, deadline: float = 20.0
) -> list[bytes]:
    """Collect ``want`` blocks from a LIVE stream, or fail with what did arrive.

    Deliberately not ``_drain``: every scenario here fails by DELIVERING NOTHING, so the timeout
    message has to carry the partial result — "silent stream" and "test hung" look identical
    otherwise, and telling them apart is the whole point of the module.
    """
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
            f"the stream delivered {len(collected)} of {want} blocks in {deadline}s and then went "
            f"quiet — ids so far: {_ids(collected)}. A session whose generation check never armed "
            f"discards every event of the new generation and never closes (the lease is alive)."
        ) from None
    # A stream that CLOSES short is not a pass with fewer blocks: every scenario here needs the
    # stream open to the end, and swallowing an early close would let an unrelated close rule make
    # the test vacuous.
    assert not closed, (
        f"the stream closed after {len(collected)} of {want} blocks — ids: {_ids(collected)}. "
        "Some closing rule fired; this scenario needs the stream open throughout."
    )
    return collected


# ==================================================================================================
# (а) No epoch key, NON-EMPTY ring → the generation comes from the last ring element.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_session_without_the_key_takes_its_generation_from_the_ring(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Step 1 of §3.3.1a: the ring carries the epoch in every element, so the key is not the only
    source of the same fact.

    ⚠️ The ring fallback is not interchangeable with adoption, and this test is what separates them.
    A session that skipped the fallback and adopted the first LIVE event's generation would file the
    three already-delivered elements of generation A under generation B, keep ``delivered = 3`` as
    the baseline, and drop B's ``seq`` 1 — closing one entrance to the silent stream by opening a
    second. So the assertion is not merely "events arrive": it is that the change from A to B is
    ANNOUNCED, which only a session that knew it was on A can do.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await _hold_lease(bus, run_id)
        # NO ensure_epoch: publish creates the ring but never the epoch key (§3.1 — it only
        # REFRESHES it), so this is exactly the state the defect needs.
        for i in range(1, 4):
            await bus.publish(run_id, epoch=_EPOCH_A, raw=f"data: a-{i}\n\n".encode())
        assert await bus.current_epoch(run_id) is None, "the epoch key must be absent for this test"

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=5))
        await asyncio.sleep(0.4)  # let the replay drain and the subscription settle

        # Redis "restarted": the counter is gone, the consumer republishes under a NEW generation
        # from seq 1 — the exact collision the dedup rule turns into silence.
        await bus._redis.delete(f"agent:run:{run_id}:seq")
        await bus.publish(run_id, epoch=_EPOCH_B, raw=b"data: b-1\n\n")

        blocks = await collector

    assert_no_silent_generation_change(blocks)
    assert (
        _epochs(blocks)[:3] == [_EPOCH_A] * 3
    ), "the replay was not served under its own generation"
    assert any(_is_truncation(b) for b in blocks[3:]), (
        "the generation change was not announced — the session never knew it was on generation A, "
        "so it had nothing to compare the new event against"
    )
    assert blocks[-1].endswith(b"data: b-1\n\n"), "the post-restart event never reached the client"


# ==================================================================================================
# (б) No epoch key, EMPTY ring → adoption from a live EVENT, silently.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_session_with_no_key_and_no_ring_adopts_the_first_event_generation(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Step 2 of §3.3.1a, event branch — and the PAIRED NEGATIVE on the marker.

    Two claims, and the second is as important as the first:

    * adoption ARMS the check — proved by a later generation change being announced, which an
      unarmed session cannot do;
    * adoption on an EMPTY cursor is SILENT — no ``run.truncated``. Not because the marker would
      cost the client its text (it would not: §3.4 has the client refetch from ``GET …/state``, so
      the marker is recoverable and costs one request), but because it would carry NO INFORMATION:
      telling a client that its prefix is incomplete when it holds no prefix is noise, and a noisy
      signal stops being handled. Where the information exists — a non-empty cursor — the marker is
      mandatory, which is the neighbouring sub-case (г).
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        # A live lease: without it rule 3 (no lease + empty ring) closes the stream at once and the
        # scenario is never reached.
        await _hold_lease(bus, run_id)
        assert await bus.current_epoch(run_id) is None
        assert await bus.replay(run_id) == []

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=3))
        await asyncio.sleep(0.4)

        # First event ever seen by this session: adopted, and adopted quietly.
        await bus.publish(run_id, epoch=_EPOCH_A, raw=b"data: a-1\n\n")
        await asyncio.sleep(0.4)
        # Generation changes afterwards; seq restarts at 1 against a session that has delivered 1.
        await bus._redis.delete(f"agent:run:{run_id}:seq")
        await bus.publish(run_id, epoch=_EPOCH_B, raw=b"data: b-1\n\n")

        blocks = await collector

    assert_no_silent_generation_change(blocks)
    # The paired negative: nothing before the generation actually changed may be a marker.
    assert not _is_truncation(blocks[0]), (
        "adoption emitted run.truncated — a client obeying it would replace its complete text with "
        "a truncated one, on a session that had been promised nothing"
    )
    assert blocks[0].endswith(b"data: a-1\n\n")
    assert _epochs(blocks)[0] == _EPOCH_A
    assert any(_is_truncation(b) for b in blocks[1:]), (
        "the later generation change was NOT announced — adoption did not arm the check, so the "
        "session is exactly as unarmed as before the fix"
    )
    assert blocks[-1].endswith(b"data: b-1\n\n"), "the new generation's event was discarded"


# ==================================================================================================
# (г) No key, empty ring, NON-EMPTY cursor → adoption AND a mandatory run.truncated.
# ==================================================================================================
@pytest.mark.asyncio
async def test_adoption_against_a_non_empty_cursor_must_announce_the_gap(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """§3.3.1a as revised: the marker is due iff the client arrived holding a cursor.

    The client believes it holds a prefix of the stream. We are about to hand it the tail of a
    generation we could NOT check that prefix against — the key was unreadable, so the cursor was
    emptied unverified. Saying nothing makes it splice two generations together silently.

    ⚠️ The decisive argument is CONSISTENCY, not caution: had the epoch key been readable, this very
    client would have received ``run.truncated`` from ``_validate_cursor`` by §3.2 step 2. Adoption
    must not be *softer* than the ordinary path, or the client's contract comes to depend on whether
    Redis happened to answer — which is precisely the kind of invisible, environment-dependent
    behaviour this whole section exists to remove.

    ⚠️ Its absence is not "one missing block": it is a client that concatenates the tail of
    generation B onto its prefix of generation A and shows the user a coherent-looking answer that
    never existed.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await _hold_lease(bus, run_id)
        assert await bus.current_epoch(run_id) is None
        assert await bus.replay(run_id) == []

        # The client arrives holding a prefix — the one thing that makes the marker informative.
        stream = broker.stream(run_id=run_id, cursor=Cursor(seq=17, epoch=_EPOCH_A))
        collector = asyncio.create_task(
            _collect_until(
                stream,
                match=lambda b: b.endswith(b"data: b-1\n\n"),
                what="the first event of the adopted generation",
            )
        )
        await asyncio.sleep(0.4)
        await bus.publish(run_id, epoch=_EPOCH_B, raw=b"data: b-1\n\n")

        blocks = await collector

    assert blocks[-1].endswith(b"data: b-1\n\n")
    assert _is_truncation(blocks[0]), (
        "adoption served a client holding a cursor without announcing the gap — it will splice the "
        "tail of one generation onto its prefix of another. On a readable epoch key the same "
        "client would have been told (§3.2 step 2); the contract must not depend on Redis answering"
    )


# ==================================================================================================
# (в) A QUIET run → adoption from the PERIODIC check, with no event at all.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_quiet_session_adopts_its_generation_from_the_periodic_check(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Step 2, periodic branch — NOT a duplicate of the event branch, and this is why.

    A quiet run may emit nothing for hours (one long tool call). A session armed only by events
    spends all that time unarmed, and then the very event that would have armed it is the one the
    dedup rule discards — arming on the event cannot save the event it arrives with. So the periodic
    check must arm too.

    The scenario contains NO event before the arming: the epoch key simply appears while the stream
    is idle. The proof that arming happened is that the SUBSEQUENT generation change is announced —
    an unarmed session would have adopted that event silently and emitted no marker.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await _hold_lease(bus, run_id)
        assert await bus.current_epoch(run_id) is None
        assert await bus.replay(run_id) == []

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        collector = asyncio.create_task(_collect(stream, want=2))

        # Redis comes back: the consumer re-establishes the key. No event accompanies it — that is
        # the whole point of the branch.
        await asyncio.sleep(0.3)
        await bus._redis.set(f"agent:run:{run_id}:epoch", _EPOCH_A, ex=60)
        # Past one full period of the rule-4 check (AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS).
        await asyncio.sleep(3.0)

        # Now the generation changes again and events start under B from seq 1.
        await bus._redis.set(f"agent:run:{run_id}:epoch", _EPOCH_B, ex=60)
        await bus.publish(run_id, epoch=_EPOCH_B, raw=b"data: b-1\n\n")

        blocks = await collector

    assert_no_silent_generation_change(blocks)
    assert any(_is_truncation(b) for b in blocks), (
        "the change away from the generation adopted while idle was not announced — the periodic "
        "check never armed the session, so a quiet run stays unarmed for its whole life"
    )
    assert blocks[-1].endswith(b"data: b-1\n\n"), "the new generation's event was discarded"


# ==================================================================================================
# THE PREMISE the silent adoption rests on — and nothing in the code states it.
#
# Silent adoption (no run.truncated, no cursor reset) is safe ONLY because an unarmed session has
# `last_seq == 0`. The chain: unarmed ⇔ the key was not read AND the ring was empty at open;
# `_validate_cursor` empties any non-empty cursor when the key was not read; an empty ring delivers
# nothing. Not one line expresses that dependency, so a later change to `_validate_cursor` — "do not
# lose the client's position over a Redis blip" is an entirely reasonable-looking patch — restores
# the silent stream at once, and every §3.3.1a test above stays GREEN, because they all open with an
# empty cursor and so never touch the branch that would break.
#
# These two tests fail on precisely that patch, and only on it.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_session_that_could_not_read_the_key_always_opens_at_cursor_zero(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The premise directly: no readable key ⇒ the client's cursor is discarded AND announced.

    Trusting a cursor whose generation cannot be checked is trusting a number against an unknown
    numbering, so the cursor is emptied and the whole ring is replayed.

    ⚠️ The marker is due too, and an earlier version of this test asserted its ABSENCE — wrongly.
    That reasoning measured continuity FORWARD (the emptied filter is 0, the ring starts at 1, no
    gap) which is C3's question. The gap here is BACKWARD, and it is C5's: the client holds text for
    ``seq`` 1..2 of a generation nobody can confirm, and we hand it 1..3. Told nothing, it appends
    and shows a DOUBLED prefix. C5 also makes this consistent with the readable-key path, where the
    identical client is told by §3.2 step 2 — otherwise the contract would depend on whether Redis
    answered.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await _hold_lease(bus, run_id)
        for i in range(1, 4):
            await bus.publish(run_id, epoch=_EPOCH_A, raw=f"data: a-{i}\n\n".encode())
        assert await bus.current_epoch(run_id) is None

        # A client reconnecting mid-ring, with a cursor whose generation nothing can confirm.
        stream = broker.stream(run_id=run_id, cursor=Cursor(seq=2, epoch=_EPOCH_A))
        blocks = await _collect(stream, want=4)

    # C5: the unconfirmable claim is announced, once, ahead of the replay it invalidates.
    assert _is_truncation(blocks[0]), (
        "the client's unverifiable prefix claim was not answered — it will append the replay to "
        "the text it already holds and double its prefix"
    )
    assert sum(1 for b in blocks if _is_truncation(b)) == 1, "one discontinuity, one marker"
    # C6: the marker carries seq=0, so a library storing it as Last-Event-ID reconnects with an
    # EMPTY cursor rather than re-presenting the very claim that was just rejected.
    assert _ids(blocks)[0].endswith("-0"), "the marker's seq would re-admit the rejected cursor"
    # The other half of the original claim, unchanged and still the point: a full replay.
    assert [b.split(b"data: ", 1)[1] for b in blocks[1:]] == [
        b"a-1\n\n",
        b"a-2\n\n",
        b"a-3\n\n",
    ], (
        "the cursor survived a generation that could not be read — the session now carries a "
        "non-zero baseline it cannot justify, and silent adoption is no longer safe"
    )


@pytest.mark.asyncio
async def test_an_unarmed_session_with_a_high_cursor_still_receives_the_first_event(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The CONSEQUENCE of the premise — the silent stream itself, one patch away.

    Empty ring, unreadable key, and a client holding a high cursor. If that cursor were kept, the
    session would open unarmed with ``last_seq = 500``, adopt the first event's generation SILENTLY
    (correctly, by §3.3.1a) and then discard it as ``seq 1 <= 500`` — no marker, no close rule (the
    lease is alive), an open stream that never speaks again. Exactly TD-044, re-entered through the
    door the fix does not guard.

    This is why the pair matters: the tests above prove the check gets armed, and this one proves
    that arming is still *sufficient*. Neither implies the other.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus = broker_factory(session)
        await _hold_lease(bus, run_id)
        assert await bus.current_epoch(run_id) is None
        assert await bus.replay(run_id) == []

        stream = broker.stream(run_id=run_id, cursor=Cursor(seq=500, epoch=_EPOCH_A))
        collector = asyncio.create_task(
            _collect_until(
                stream,
                match=lambda b: b.endswith(b"data: b-1\n\n"),
                what="the first event of the adopted generation",
            )
        )
        await asyncio.sleep(0.4)
        await bus.publish(run_id, epoch=_EPOCH_B, raw=b"data: b-1\n\n")

        blocks = await collector

    # Deliberately silent about whether a run.truncated precedes the event: that is §3.3.1a's
    # marker policy, tested next door. The claim here is only that the event is not SWALLOWED.
    assert any(b.endswith(b"data: b-1\n\n") for b in blocks), (
        "the first event of an adopted generation was discarded against a cursor the session had "
        "no way to validate — the silent stream is back"
    )
