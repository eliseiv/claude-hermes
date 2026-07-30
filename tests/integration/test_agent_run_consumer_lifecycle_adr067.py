"""Integration: the consumer's lifecycle against a REAL upstream socket (ADR-067 §6.1/§6.4).

Covers the properties that can only be established as FACTS, never by reading the code:

* a database session is never held across an ``await`` on upstream — asserted by the PEAK number of
  simultaneously open sessions, because counting opens would pass on the implementation the
  invariant forbids;
* the lease is released even when the final flush HANGS — the case that motivated the 5s bound, and
  the one where skipping the release leaves a run nobody drives and nobody finalizes;
* ``bytes_read`` grows on a PARTIAL block, which requires a single SSE event to arrive in two socket
  writes and therefore a real server;
* the beacon reaches ``processing`` from INSIDE the handler, not after the iteration.

The upstream is a real local HTTP server (``tests/support/agent_run_harness.py``) rather than a
transport double: every one of these is a property of bytes on a socket.
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
    BEACON_AWAITING_UPSTREAM,
    BEACON_CONNECTING,
    BEACON_PROCESSING,
    EVENT_CONSUMER_STALLED,
    ConsumerContext,
    SubscriptionHandshake,
    _ProgressWindow,
    is_stalled,
    run_consumer,
    run_worker,
)
from app.agent_proxy.transport import AgentRunEventBus, LeaseAcquisition, url_with_db
from app.deps import get_agent_proxy_service_for
from app.hermes_runtime.manager import InstanceEndpoint
from tests.conftest import seed_user
from tests.support.agent_run_harness import (
    FakeUpstream,
    SessionCounter,
    UpstreamScript,
    await_consumer,
    consumer_settings,
    counting_services,
    sse_chunks_split_first_event,
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
    """A consumer environment: own Redis DB, real service factory, session counter."""
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(**overrides: Any) -> tuple[Any, AgentRunEventBus, Any, SessionCounter]:
        db = counter["n"]
        counter["n"] += 1
        settings = consumer_settings(redis_url=redis_url, redis_db=db, **overrides)
        client = redis_asyncio.from_url(
            url_with_db(redis_url, db), decode_responses=True, socket_timeout=5
        )
        clients.append(client)
        bus = AgentRunEventBus(client, settings)

        @asynccontextmanager
        async def raw_services() -> AsyncIterator[Any]:
            async with db_sessionmaker() as session:
                yield get_agent_proxy_service_for(session)

        sessions = SessionCounter()
        return counting_services(raw_services, sessions), bus, settings, sessions

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
_COMPLETED = (
    '{"event": "run.completed", "run_id": "r", "usage": {"input_tokens": 10, "output_tokens": 5}}'
)


# ==================================================================================================
# (6) A session is never held across an await on upstream.
# ==================================================================================================
@pytest.mark.asyncio
async def test_no_session_is_held_across_an_await_on_upstream(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Asserted by the PEAK of simultaneously open sessions, not by how many were opened.

    A run lasts up to two hours while its DB work is sparse, so a session per RUN would pin a pooled
    connection — one per concurrent run, a number with no cap — hold a transaction open across
    network waits and block VACUUM on the hottest tables. Counting opens cannot express that: the
    forbidden implementation opens exactly one session and would look ideal.

    The upstream deliberately dawdles between events, so a session held across the wait would still
    be open while nothing is happening.
    """
    services, bus, settings, sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    script = UpstreamScript(chunks=_events(_DELTA, _DELTA, _COMPLETED), delay=0.15)
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
            timeout=30,
        )

    assert sessions.opened >= 3, "the consumer did no per-operation DB work at all"
    assert sessions.peak_concurrent == 1, (
        f"{sessions.peak_concurrent} sessions were open at once — a session is being held across "
        "an upstream await"
    )
    assert sessions.live == 0, "a session outlived the consumer"


# ==================================================================================================
# (2б) The lease is released even when the final flush hangs.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_lease_is_released_even_when_the_final_flush_hangs(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """THE case the 5s bound exists for.

    Step 2 of §6.4 is what makes an abandoned run visible to the reaper. If a wedged step 1 could
    skip it, the run would keep a lease nobody renews and nobody owns: the sweep's condition 1 is
    never satisfied, so the run is never finalized and sits ``running`` for ever — the exact defect
    ADR-067 exists to remove, reintroduced through its own cleanup path.
    """
    services, bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    hang_started = asyncio.Event()

    @asynccontextmanager
    async def hanging_services() -> AsyncIterator[Any]:
        async with db_sessionmaker() as session:
            service = get_agent_proxy_service_for(session)

            async def _hang(_run: Any) -> None:
                hang_started.set()
                await asyncio.sleep(3600)

            service.flush_run_snapshot = _hang  # type: ignore[method-assign]
            yield service

    script = UpstreamScript(chunks=_events(_DELTA, _COMPLETED))
    async with FakeUpstream(script) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        await asyncio.wait_for(
            run_consumer(
                services=hanging_services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
            ),
            timeout=60,
        )

    assert hang_started.is_set(), "the final flush was never attempted — the test proved nothing"
    assert (
        await bus.lease_alive(run_id) is False
    ), "the lease survived a hung final flush; the run would never become visible to the reaper"


@pytest.mark.asyncio
async def test_the_lease_is_released_on_an_upstream_failure(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """The same guarantee on the other abnormal exit: a subscription that never established."""
    services, bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(UpstreamScript(status=500)) as upstream:
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
            timeout=30,
        )

    assert await bus.lease_alive(run_id) is False


@pytest.mark.asyncio
async def test_a_run_whose_lease_is_held_elsewhere_is_skipped(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Two consumers on a ONE-SHOT stream would leave both with nothing.

    The second must not even open the subscription — asserted on the fake upstream's request log,
    which is the only place that distinguishes "declined to start" from "started and gave up".
    """
    services, bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)
    assert await bus.acquire_lease(run_id, "someone-else") is LeaseAcquisition.ACQUIRED

    async with FakeUpstream(UpstreamScript(chunks=_events(_COMPLETED))) as upstream:
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
            timeout=30,
        )
        assert upstream.requests == [], "a second consumer consumed the one-shot stream"

    assert await bus.lease_alive(run_id) is True, "the interloper released someone else's lease"


# ==================================================================================================
# (2а) Byte accounting on a PARTIAL block, and the beacon's position.
#
# ⚠️ THE GAP THIS BLOCK WAS WRITTEN TO CLOSE. `test_bytes_read_grows_on_a_partial_block` (below)
# asserted that the byte counter grows, and the unit module asserted that `is_stalled` honours
# AGENT_RUN_FIRST_BYTE_STALL_SECONDS in the `connecting` state. Both were true, both were green —
# and the guard was DEAD, because nothing tested the thing that JOINS them: which state the beacon
# is actually in between the 2xx and the first byte. The beacon was moved to `awaiting_upstream` on
# the response headers, and that state is unconditionally alive to the supervisor, so the threshold
# was never reached by any real subscription. The unit tests set `beacon.state` by assignment, so
# they could not have caught it: they construct the state the guard needs instead of observing the
# state the worker produces.
#
# Hence the two tests below assert the TRANSITION, on a real socket, from outside the beacon:
# properties of neighbouring components being individually correct guarantees nothing about the
# mechanism they are supposed to form.
# ==================================================================================================
@pytest.mark.asyncio
async def test_an_established_but_silent_subscription_stays_connecting_and_is_stalled(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """(а) Headers in, not one byte after them ⇒ beacon still ``connecting``, and the guard fires.

    This is the ENTIRE class §6.4.2 was kept in v1 for: a subscription the image accepted but has
    already drained for somebody else. It is indistinguishable from a slow one at the socket level,
    which is why the only usable discriminator is "has a byte ever arrived", and why the beacon may
    not leave ``connecting`` merely because the response line did.

    Both halves are asserted, because either alone is satisfiable by a broken implementation: the
    STATE (a beacon in ``awaiting_upstream`` is unconditionally alive, so the threshold would never
    be consulted) and the VERDICT of ``is_stalled`` against real settings.

    ⚠️ Fails on the implementation that transitioned right after ``mark_established()``: there the
    state reads ``awaiting_upstream`` and ``is_stalled`` returns False for ever — the run would hold
    its lease and heartbeat for ``MAX_DURATION`` (7200s) instead of 180s.
    """
    services, bus, settings, _sessions = env(AGENT_RUN_FIRST_BYTE_STALL_SECONDS=1)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    # Headers and then nothing at all — an accepted, inert subscription.
    script = UpstreamScript(chunks=[], hold_open=True)
    async with FakeUpstream(script) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        ctx = ConsumerContext(epoch="e", owner="o")
        worker = asyncio.create_task(
            run_worker(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                ctx=ctx,
            )
        )
        try:
            assert await ctx.handshake.wait(timeout=20), "the subscription never came up"
            state_at_handshake = ctx.beacon.state
            bytes_at_handshake = ctx.beacon.bytes_read
            # Past the (shrunk) first-byte threshold, still with nothing on the wire.
            await asyncio.sleep(settings.agent_run_first_byte_stall_seconds + 0.5)
            verdict = is_stalled(ctx.beacon, _ProgressWindow.of(ctx.beacon), settings)
            state_after = ctx.beacon.state
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    assert bytes_at_handshake == 0, "the upstream sent a byte; this is no longer the inert case"
    assert state_at_handshake == BEACON_CONNECTING, (
        f"the beacon moved to {state_at_handshake!r} on the response HEADERS. That state is "
        "unconditionally alive to the supervisor, so AGENT_RUN_FIRST_BYTE_STALL_SECONDS is never "
        "consulted and the inert-subscription guard is dead code"
    )
    assert state_after == BEACON_CONNECTING, "the beacon left `connecting` without a single byte"
    assert verdict is True, (
        "is_stalled did not fire on a subscription that has never delivered a byte — the run would "
        "hold its lease and heartbeat until MAX_DURATION"
    )


@pytest.mark.asyncio
async def test_the_first_raw_chunk_moves_the_beacon_out_of_connecting(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """(б) The transition happens on the first BYTE — before any complete event is parsed.

    The chunk is deliberately half an SSE block, so at the moment of the assertion nothing has been
    handed to the domain layer at all. Tying the transition to a parsed EVENT instead would leave a
    subscription that is demonstrably flowing under the first-byte guard, and the guard would cancel
    it — a working run killed, its one-shot stream spent, which is the more expensive of the two
    possible mistakes here.

    Paired negative, and the critical half: once a byte HAS arrived, silence no longer counts. A
    stream quiet for far longer than the first-byte threshold must NOT be stalled — that would be
    the idle timeout retracted in revision 2, resurrected through the guard's back door.
    """
    services, bus, settings, _sessions = env(AGENT_RUN_FIRST_BYTE_STALL_SECONDS=1)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    head, tail = sse_chunks_split_first_event(_DELTA, at=20)
    # Only the HEAD is ever sent; the tail would complete the block, and completing it is precisely
    # what must NOT be required for the transition.
    script = UpstreamScript(chunks=[head], hold_open=True)

    async with FakeUpstream(script) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        ctx = ConsumerContext(epoch="e", owner="o")
        worker = asyncio.create_task(
            run_worker(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                ctx=ctx,
            )
        )
        try:
            assert await ctx.handshake.wait(timeout=20), "the subscription never came up"
            for _ in range(200):
                if ctx.beacon.bytes_read:
                    break
                await asyncio.sleep(0.05)
            state_on_first_byte = ctx.beacon.state
            bytes_on_first_byte = ctx.beacon.bytes_read
            # Now stay silent for well past the first-byte threshold.
            await asyncio.sleep(settings.agent_run_first_byte_stall_seconds + 0.5)
            verdict = is_stalled(ctx.beacon, _ProgressWindow.of(ctx.beacon), settings)
            state_after_silence = ctx.beacon.state
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    assert 0 < bytes_on_first_byte <= len(head), "no partial chunk was observed at all"
    assert state_on_first_byte == BEACON_AWAITING_UPSTREAM, (
        f"the beacon was {state_on_first_byte!r} after a partial chunk had already arrived — the "
        "transition is tied to a PARSED EVENT, so the guard would cancel a flowing subscription"
    )
    assert state_after_silence == BEACON_AWAITING_UPSTREAM
    assert verdict is False, (
        "a stream that delivered a byte and then went quiet was declared stalled — this is the "
        "retracted idle timeout coming back through the first-byte guard"
    )


@pytest.mark.asyncio
async def test_the_supervisor_cancels_an_inert_subscription_end_to_end(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """The same guard through the whole machine: §6.4.2 → §6.4, lease dropped, audit written.

    The two tests above pin the beacon and the verdict; this one pins that the verdict is acted on.
    A run whose subscription never speaks must be released to the reaper in
    ``AGENT_RUN_FIRST_BYTE_STALL_SECONDS``, not held to ``MAX_DURATION`` — the difference is
    180 seconds against two hours of a lease, a heartbeat, and an instance kept out of hibernation.

    The consumer is polled rather than awaited under ``wait_for`` (see
    ``tests/support/agent_run_harness.await_consumer``).
    """
    services, bus, settings, _sessions = env(AGENT_RUN_FIRST_BYTE_STALL_SECONDS=1)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(UpstreamScript(chunks=[], hold_open=True)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        consumer = asyncio.create_task(
            run_consumer(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
            )
        )
        # Generous next to the 1s threshold, nowhere near MAX_DURATION.
        stopped = await await_consumer(consumer, budget=30)

    assert stopped, (
        "the consumer kept an inert subscription alive past the first-byte threshold — it would "
        "hold its lease and heartbeat until MAX_DURATION"
    )
    assert await bus.lease_alive(run_id) is False, "the stalled consumer kept its lease"
    async with db_sessionmaker() as session:
        events = [
            r.event_type
            for r in (
                await session.execute(
                    text("SELECT event_type FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
                )
            ).all()
        ]
    assert EVENT_CONSUMER_STALLED in events, f"no stall audit was written; got {events}"


@pytest.mark.asyncio
async def test_bytes_read_grows_on_a_partial_block(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """The first event arrives in TWO socket writes; the first half must already count.

    If byte accounting were tied to parsed EVENTS rather than to bytes received, a subscription that
    delivered half an event and then went quiet would look like it had read nothing — and the
    inert-subscription guard would cancel a subscription that is demonstrably alive.
    """
    services, bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    head, tail = sse_chunks_split_first_event(_DELTA, at=20)
    script = UpstreamScript(chunks=[head, tail], delay=0.4, hold_open=True)

    async with FakeUpstream(script) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        ctx = ConsumerContext(epoch="e", owner="o")
        worker = asyncio.create_task(
            run_worker(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                ctx=ctx,
            )
        )
        # After the FIRST chunk only — no complete event has been parsed yet.
        await asyncio.sleep(0.25)
        partial_bytes = ctx.beacon.bytes_read
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert partial_bytes >= len(head), (
        f"only {partial_bytes} bytes counted after a {len(head)}-byte partial block — byte "
        "accounting is tied to parsed events, not to the socket"
    )
    assert ctx.beacon.saw_first_byte is True


@pytest.mark.asyncio
async def test_the_beacon_reaches_processing_from_inside_the_handler(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """``processing`` must be set BEFORE the handler runs, not after the iteration.

    An "end of iteration" beacon leaves the most likely hang — the handler itself wedged on a DB
    write — sitting in ``awaiting_upstream``, which the supervisor treats as unconditionally alive.
    The very failure the state exists to catch would be the one it never saw. Observed from INSIDE a
    handler that blocks, which is the only vantage point that can tell the two apart.
    """
    services, bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    observed: list[str] = []
    in_handler = asyncio.Event()
    release = asyncio.Event()
    ctx = ConsumerContext(epoch="e", owner="o")

    @asynccontextmanager
    async def observing_services() -> AsyncIterator[Any]:
        async with db_sessionmaker() as session:
            service = get_agent_proxy_service_for(session)
            original = service.process_event

            async def _observed(run: Any, block: Any) -> Any:
                observed.append(ctx.beacon.state)
                in_handler.set()
                await release.wait()
                return await original(run, block)

            service.process_event = _observed  # type: ignore[method-assign]
            yield service

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        worker = asyncio.create_task(
            run_worker(
                services=observing_services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                ctx=ctx,
            )
        )
        await asyncio.wait_for(in_handler.wait(), timeout=20)
        state_while_wedged = ctx.beacon.state
        release.set()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert observed and observed[0] == BEACON_PROCESSING, (
        f"the handler ran while the beacon said {observed[0]!r} — a wedged handler would be "
        "invisible to the supervisor"
    )
    assert state_while_wedged == BEACON_PROCESSING


# ==================================================================================================
# (2б) The limits are applied FROM OUTSIDE, and the two tasks live or die together.
#
# This is the entire reason the consumer is two tasks instead of one. A task cannot witness its own
# liveness, and a MAX_DURATION timer living inside a wedged coroutine would never fire. The failure
# the pairing prevents is the most expensive one in the design: a dead supervisor beside a live
# worker stops the heartbeat, the reaper finalizes the run as an orphan — ``failed`` plus a debit
# from an INCOMPLETE cumulative under ``idempotency_key=runId`` — and when the real
# ``run.completed`` finally arrives, the correct debit is discarded as a duplicate under that same
# key while ``_mark_terminal('completed')`` is a no-op against the conditional transition. The
# undercharge and the wrong status both become permanent, and nothing reports either.
#
# The timers here are set to ~1s rather than mocked: the property under test is that an EXTERNAL
# actor cancels a worker that cannot cancel itself, and substituting the clock would remove the very
# scheduling the test is about. One second is a bound, not a wait.
# ==================================================================================================
@pytest.mark.asyncio
async def test_max_duration_cancels_a_worker_that_cannot_cancel_itself(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """The upstream holds the connection open for an hour; only an outside actor can end this.

    The worker is parked in ``async for`` awaiting bytes that never come, so if the consumer returns
    at all, the cancellation came from the supervisor. That is the claim — not merely that the run
    ended, but that it ended by an authority OTHER than the code that is stuck.
    """
    services, bus, settings, _sessions = env(
        AGENT_RUN_MAX_DURATION_SECONDS=1, AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=1
    )
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    script = UpstreamScript(chunks=_events(_DELTA), hold_open=True)
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
            timeout=30,
        )

    # §6.4: the consumer says WHY it stopped and releases the lease; it never writes a terminal
    # status — it does not know the run's outcome, and guessing is the irreversible mistake.
    async with db_sessionmaker() as session:
        events = [
            r[0]
            for r in (
                await session.execute(
                    text("SELECT event_type FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
                )
            ).all()
        ]
        status = (
            await session.execute(
                text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()

    assert "agent_run_consumer_max_duration" in events, events
    assert await bus.lease_alive(run_id) is False
    assert str(status) == "running", (
        "the consumer wrote a terminal status — it does not know the outcome, and a wrong "
        "`failed` can never be overwritten"
    )


@pytest.mark.asyncio
async def test_a_dying_supervisor_takes_the_worker_with_it(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """THE invariant of the TaskGroup, in the direction that costs money.

    A supervisor that dies beside a live worker is the worst state the design has: nobody stamps the
    heartbeat, so the reaper finalizes a RUNNING run as an orphan. The worker must therefore be
    cancelled with it — asserted against an upstream that would otherwise keep the worker alive for
    an hour.

    The supervisor is killed through a heartbeat that raises something it does NOT handle: it
    tolerates ``SQLAlchemyError`` on purpose (a failed heartbeat only brings the orphan deadline
    closer), so a different exception is what makes it die.
    """
    services, bus, settings, _sessions = env(AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=1)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    @asynccontextmanager
    async def exploding_heartbeat() -> AsyncIterator[Any]:
        async with db_sessionmaker() as session:
            service = get_agent_proxy_service_for(session)

            async def _boom(**_kwargs: Any) -> bool:
                raise RuntimeError("supervisor died")

            service.consumer_heartbeat = _boom  # type: ignore[method-assign]
            yield service

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        with pytest.raises(BaseExceptionGroup):
            await asyncio.wait_for(
                run_consumer(
                    services=exploding_heartbeat,
                    bus=bus,
                    settings=settings,
                    endpoint=endpoint,
                    user_id=uid,
                    run_id=run_id,
                ),
                timeout=30,
            )

    # The worker did not outlive its supervisor, and §6.4 still ran: the lease is gone, so the run
    # passes to the reaper at once instead of waiting out LEASE_TTL + ORPHAN_TIMEOUT.
    assert await bus.lease_alive(run_id) is False, (
        "the lease survived a dead supervisor — the run would be driven by nobody and finalized "
        "by nobody"
    )


@pytest.mark.asyncio
async def test_a_dying_worker_takes_the_supervisor_with_it(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """The mirror direction: a supervisor left renewing a lease for a worker that is gone would
    keep the run looking alive to the sweep while nothing consumes it."""
    services, bus, settings, _sessions = env(
        AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=1, AGENT_RUN_MAX_DURATION_SECONDS=7200
    )
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    async with FakeUpstream(UpstreamScript(status=500)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        # Returns promptly: had the supervisor survived, its loop would still be renewing.
        await asyncio.wait_for(
            run_consumer(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
            ),
            timeout=15,
        )

    assert await bus.lease_alive(run_id) is False


# ==================================================================================================
# (2а) Order within one event: the ring first, the domain rules second.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_failing_domain_handler_does_not_cost_downstream_clients_the_event(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """Publish BEFORE applying the domain rules — the event is already in the ring when they run.

    The two failure directions are deliberately not symmetric. A domain handler that raises must not
    cost downstream clients an event they could otherwise have seen; a Redis outage must not cost
    the run its billing. Only the second is fatal, which is exactly why the publish comes first and
    degrades to a logged ``None`` while the handler is allowed to propagate.
    """
    services, bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    @asynccontextmanager
    async def failing_domain() -> AsyncIterator[Any]:
        async with db_sessionmaker() as session:
            service = get_agent_proxy_service_for(session)

            async def _boom(_run: Any, _block: Any) -> Any:
                raise RuntimeError("domain handler failed")

            service.process_event = _boom  # type: ignore[method-assign]
            yield service

    async with FakeUpstream(UpstreamScript(chunks=_events(_DELTA), hold_open=True)) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        ctx = ConsumerContext(epoch=await bus.ensure_epoch(run_id) or "e", owner="o")
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                run_worker(
                    services=failing_domain,
                    bus=bus,
                    settings=settings,
                    endpoint=endpoint,
                    user_id=uid,
                    run_id=run_id,
                    ctx=ctx,
                ),
                timeout=20,
            )

    replayed = await bus.replay(run_id)
    assert len(replayed) == 1, (
        "the event never reached the ring — a failing domain handler cost downstream clients an "
        "event, which is the order this test exists to pin"
    )
    assert replayed[0].data == _events(_DELTA)[0]


# ==================================================================================================
# ADR-067 §4.1 — the three-valued lease. THE PAIR: leaving on EVIDENCE vs leaving on IGNORANCE.
#
# These two tests only mean something together, and that is not a stylistic preference. A single
# test showing "the consumer subscribed" is satisfied by an implementation that never stands down
# at all; a single test showing "the consumer did not subscribe" is satisfied by the pre-§4.1 code
# that stood down on everything. What §4.1 actually decided is the DISTINCTION — an unreachable
# Redis is not evidence of a second owner, a foreign key value is — so the distinction is what has
# to be observed, on the one witness that cannot be faked: the upstream's request log.
# ==================================================================================================
@pytest.mark.asyncio
async def test_an_unreachable_redis_still_gets_the_run_consumed_billed_and_finalized(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """§4.1 positive half: ``RedisError`` on the lease ⇒ the run is DRIVEN, as with Redis up.

    Before §4.1 ``acquire_lease`` returned a bare ``bool`` and folded "someone else holds it" into
    "Redis did not answer". The consumer read that ``False`` as "this run already has a consumer"
    and returned without subscribing — so a run that started during a Redis outage was never
    consumed, never billed, never finalized, and the sweep later stamped it ``failed`` with
    ``owed = 0``. That is TD-037 itself, reproduced by the very code written to remove it.

    Everything asserted here lives in POSTGRES, which the outage does not touch: the snapshot row,
    the debit under ``idempotency_key=runId`` and the terminal status. What genuinely degrades is
    the Redis fan-out, and the test states that too — ``publish`` returns ``None`` throughout and no
    ring exists — so "we proceed" is not quietly read as "nothing was lost".

    ⚠️ Fails on the pre-§4.1 implementation with ``upstream.requests == []``.
    """
    _services, _bus, settings, _sessions = env()
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)

    # Port 1 on loopback: a connection refused on every call, i.e. RedisError on every operation.
    dead = redis_asyncio.from_url("redis://127.0.0.1:1/9", socket_connect_timeout=1)
    dead_bus = AgentRunEventBus(dead, settings)
    assert (
        await dead_bus.acquire_lease(run_id, "probe") is LeaseAcquisition.UNKNOWN
    ), "the outage must surface as UNKNOWN, not HELD_ELSEWHERE — otherwise this test proves nothing"

    @asynccontextmanager
    async def real_services() -> AsyncIterator[Any]:
        async with db_sessionmaker() as session:
            yield get_agent_proxy_service_for(session)

    usage = (
        '{"event": "usage.delta", "run_id": "r", "step_index": 1, '
        '"cumulative_input_tokens": 2000, "cumulative_output_tokens": 1000}'
    )
    # Terminal usage in the wire shape of tests/fixtures/hermes_prod_completed_run_adr067.sse
    # (nested `usage` with `input_tokens`/`output_tokens`/`total_tokens`), with the token counts of
    # the delta above so the credited amount is legible: 2 + 5 = 7.
    completed = (
        '{"event": "run.completed", "run_id": "r", "timestamp": 1785321309.57, "output": "DONE.", '
        '"usage": {"input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000}}'
    )
    async with FakeUpstream(UpstreamScript(chunks=_events(usage, completed))) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        await asyncio.wait_for(
            run_consumer(
                services=real_services,
                bus=dead_bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
            ),
            timeout=60,
        )
        # The decisive observation, and the one an outage cannot fabricate: we DID subscribe, and to
        # this run's events path.
        assert len(upstream.requests) == 1, "the consumer stood down on an unreachable Redis"
        assert f"/v1/runs/{run_id}/events".encode() in upstream.requests[0]

    # The fan-out really is gone — the cost of proceeding, asserted rather than assumed.
    assert await dead_bus.publish(run_id, epoch="e", raw=b"data: x\n\n") is None
    assert await dead_bus.replay(run_id) == []
    await dead.aclose()

    async with db_sessionmaker() as session:
        status = (
            await session.execute(
                text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()
        snapshots = (
            await session.execute(
                text("SELECT count(*) FROM agent_run_snapshots WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()
        debits = (
            await session.execute(
                text(
                    "SELECT idempotency_key, amount FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit' ORDER BY id"
                ),
                {"u": str(uid)},
            )
        ).all()

    assert str(status) == "completed", "the run was consumed but never finalized"
    assert snapshots == 1, "no snapshot row — the run would be a §5.2 no_snapshot revenue incident"
    # 2000 in + 1000 out at 1.0/5.0 per 1k = 2 + 5 = 7, under the BARE runId key.
    assert [(r.idempotency_key, r.amount) for r in debits] == [
        (run_id, 7)
    ], "the run went unbilled during the outage"


@pytest.mark.asyncio
async def test_a_foreign_lease_stops_the_consumer_before_it_touches_upstream(
    db_sessionmaker: async_sessionmaker[AsyncSession], env: Any
) -> None:
    """§4.1 negative half: ``SET NX`` failing WITHOUT an error ⇒ no upstream request at all.

    The paired opposite of the test above, on the same witness. A foreign uuid in the key is direct
    evidence of a second owner, and the Hermes stream is one-shot — two subscribers leave BOTH with
    nothing — so this is the one case where standing down is right. Keeping it asserted is what
    stops the §4.1 relaxation from being widened from "Redis did not answer" to "the lease was not
    taken", which is the shape the ADR forbids explicitly.

    The handshake outcome is asserted here too: the caller must be released immediately with
    "not established" rather than waiting out AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS, since no
    subscription of ours will ever come up.
    """
    services, bus, settings, _sessions = env(AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS=30.0)
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    uid = await _seed_run(db_sessionmaker, run_id)
    assert await bus.acquire_lease(run_id, "another-worker") is LeaseAcquisition.ACQUIRED
    assert (
        await bus.acquire_lease(run_id, "us") is LeaseAcquisition.HELD_ELSEWHERE
    ), "the setup did not produce the foreign-lease case this test is about"

    handshake = SubscriptionHandshake()
    async with FakeUpstream(UpstreamScript(chunks=_events(_COMPLETED))) as upstream:
        endpoint = InstanceEndpoint(base_url=upstream.base_url, api_key="k")
        started = time.monotonic()
        await asyncio.wait_for(
            run_consumer(
                services=services,
                bus=bus,
                settings=settings,
                endpoint=endpoint,
                user_id=uid,
                run_id=run_id,
                handshake=handshake,
            ),
            timeout=30,
        )
        assert upstream.requests == [], "a second consumer consumed the one-shot stream"

    assert not handshake.established, "the handshake reported a subscription we never opened"
    assert await handshake.wait(timeout=0.1) is False, "the waiter was not released"
    assert time.monotonic() - started < 10, (
        "the caller was made to wait out the handshake timeout instead of being told at once "
        "that this consumer will never subscribe"
    )
    assert await bus.lease_alive(run_id) is True, "the interloper released someone else's lease"

    async with db_sessionmaker() as session:
        snapshots = (
            await session.execute(
                text("SELECT count(*) FROM agent_run_snapshots WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()
    assert snapshots == 0, "a consumer that declined to run still made its first DB write"
