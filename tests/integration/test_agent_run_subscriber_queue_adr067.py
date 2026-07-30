"""Integration: the subscriber queue — depth, phases, budgets, termination (ADR-067 §3.2.2, TD-048).

⚠️ WRITTEN BEFORE THE IMPLEMENTATION, ON PURPOSE. Three consecutive review rounds introduced a fresh
defect into this very mechanism while it existed only as prose, and at least two of them (an
unsatisfiable priority rule, a false disconnect at the phase boundary) would have been caught by the
first executable test. So the order is inverted here: the tests state the specification, and the
implementation is checked by running them rather than by reading it.

WHAT THAT MEANS FOR THE COLOUR OF THIS FILE. It is expected to be RED in part. Each test says in its
docstring which of three states it is in, because the distinction matters more than the count:

* **RED now, by design** — the mechanism does not exist yet (no deadline on the replay wait, no
  depth disconnect in the live phase). These are the executable form of the requirement.
* **GREEN now, discriminating later** — today's deliberately data-preserving implementation already
  produces the right OUTCOME (it drains before closing, it never disconnects), so the test cannot
  fail today. It exists to catch the named mutation once the mechanism lands; until then its
  neutralisation probe is NOT RUNNABLE, and the report says so rather than implying coverage.
* **GREEN now, discriminating now** — the invariant is already implemented and its mutation already
  reddens the test. Only the replay-ordering invariant is in this state.

⚠️ ``await_consumer``-style polling, never ``asyncio.wait_for``, wherever the claim is "the stream
ended by itself": a generator abandoned by ``wait_for`` looks exactly like one that closed on its
own. That confusion already produced one falsely-green test in this contour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.broker import AgentRunBroker, Cursor
from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.transport import AgentRunEventBus, url_with_db
from app.config import Settings
from tests.conftest import seed_user

_EPOCH = "gen-queue01"
# Test-scale knobs. The ARITHMETIC that matters is the RATIO the ADR insists on re-checking: the
# ring
# holds an order of magnitude more than the queue (5000 against 500 in production), so the replay
# cannot fit and must flow THROUGH the queue. Scaled down, not flattened.
_QUEUE_MAX = 10
_RING_MAX = 100
_DRAIN_SECONDS = 3.0
# The supervisor ticks on this knob, so a drain that must outlive a tick needs it short. It cannot
# be 1: the config invariant requires LEASE_RENEW < LEASE_TTL, and renew has its own floor of 1.
_SUPERVISOR_TICK = 2


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


class CountingPubSub:
    """Wraps a real subscription and counts ``get_message`` calls. Delegates everything else.

    Exists for ONE assertion — that the reader does not touch the channel between ``SUBSCRIBE`` and
    the end of the replay (§3.2.2). That is a statement about the ORDER OF OPERATIONS inside one
    task, and no observation of the delivered stream can distinguish it from a lucky schedule: the
    duplicate delivery it prevents depends on timing. So the mechanism is instrumented directly,
    which the ADR asks for in as many words ("ассерт на устройство, а не на исход").
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.get_message_calls = 0
        self.subscribed_at: int | None = None

    async def ensure_subscribed(self) -> None:
        await self._inner.ensure_subscribed()
        # Recorded as a count, not a timestamp: the claim is "no calls in between", and a counter
        # cannot be wrong about that the way a clock comparison can.
        self.subscribed_at = self.get_message_calls

    async def get_message(self, **kwargs: Any) -> Any:
        self.get_message_calls += 1
        return await self._inner.get_message(**kwargs)

    async def unsubscribe(self) -> None:
        await self._inner.unsubscribe()

    async def aclose(self) -> None:
        await self._inner.aclose()


@pytest.fixture
async def broker_factory(redis_url: str) -> AsyncIterator[Any]:
    clients: list[redis_asyncio.Redis] = []
    counter = {"n": 2}

    def _make(session: AsyncSession, **overrides: Any) -> tuple[Any, AgentRunEventBus, Settings]:
        db = counter["n"]
        counter["n"] += 1
        base: dict[str, Any] = {
            "REDIS_URL": redis_url,
            "AGENT_RUN_REDIS_DB": db,
            "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": 60,
            "AGENT_RUN_EVENT_BUFFER_MAX": _RING_MAX,
            "AGENT_RUN_SUBSCRIBER_QUEUE_MAX": _QUEUE_MAX,
            "AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS": _DRAIN_SECONDS,
            "AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS": 1,
            "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS": _SUPERVISOR_TICK,
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

        # ⚠️ A FACTORY of short-lived repositories, never one bound to this session. The broker
        # probes `agent_runs` for the whole life of an SSE stream (up to two hours), so a
        # request-scoped session here would hold a pooled connection — and an ACCESS SHARE lock —
        # for that entire time. About fifteen concurrent streams exhausted a worker's pool, after
        # which EVERY endpoint of that worker failed, not just this feature.
        @asynccontextmanager
        async def _runs() -> AsyncIterator[AgentRunsRepository]:
            yield AgentRunsRepository(session)

        broker = AgentRunBroker(bus=bus, runs=_runs, settings=settings)
        return broker, bus, settings

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


async def _set_status(maker: async_sessionmaker[AsyncSession], run_id: str, status: str) -> None:
    async with maker() as session:
        await session.execute(
            text("UPDATE agent_runs SET status = CAST(:st AS agent_run_status) WHERE run_id = :r"),
            {"r": run_id, "st": status},
        )
        await session.commit()


async def _hold_lease(bus: AgentRunEventBus, run_id: str) -> None:
    await bus._redis.set(f"agent:run:{run_id}:lease", "consumer-1", ex=300)


async def _drop_lease(bus: AgentRunEventBus, run_id: str) -> None:
    await bus._redis.delete(f"agent:run:{run_id}:lease")


_COMPLETED = json.dumps({"event": "run.completed", "run_id": "r", "usage": {}})


def _has_completed(blocks: list[bytes]) -> bool:
    return any(b'"run.completed"' in b for b in blocks)


def _is_marker(block: bytes) -> bool:
    """Whether this block is the synthetic ``run.truncated`` the broker generates."""
    body = block.split(b"data: ", 1)[1]
    try:
        parsed = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("event") == "run.truncated"


def _shape(blocks: list[bytes]) -> list[str]:
    """``MARK`` for a marker, the raw body otherwise — for readable failure messages only."""
    return [
        "MARK" if _is_marker(b) else b.split(b"data: ", 1)[1].strip().decode(errors="replace")
        for b in blocks
    ]


class Reading:
    """A client consuming a stream at a chosen pace, observed from outside.

    ``ended`` is the only honest way to say "the broker closed the stream": it is set by the
    ``async for`` running out, never by us cancelling. Nothing here uses ``asyncio.wait_for`` on the
    consumption task for exactly that reason.
    """

    def __init__(
        self,
        stream: AsyncIterator[bytes],
        *,
        delay: float,
        stall_after: int | None = None,
        stall_for: float = 0.0,
    ) -> None:
        self._stream = stream
        self._delay = delay
        # A ONE-SHOT pause, not a per-block delay. A repeated delay would make the measured hold a
        # function of how long WE slept times the number of blocks — the first version of the
        # replay-deadline test measured 528s that way, nearly all of it its own sleeping.
        self._stall_after = stall_after
        self._stall_for = stall_for
        self.blocks: list[bytes] = []
        self.ended = False
        self.error: BaseException | None = None
        self.task: asyncio.Task[None] | None = None

    def start(self) -> Reading:
        self.task = asyncio.create_task(self._consume())
        return self

    async def _consume(self) -> None:
        try:
            async for block in self._stream:
                self.blocks.append(block)
                if self._stall_after is not None and len(self.blocks) == self._stall_after:
                    await asyncio.sleep(self._stall_for)
                elif self._delay:
                    await asyncio.sleep(self._delay)
            self.ended = True
        except BaseException as exc:  # noqa: BLE001 - recorded, re-raised by the caller if wanted
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.error = exc

    async def wait_until_ended(self, *, budget: float) -> bool:
        """Poll for a self-inflicted end. True if the stream ended on its own within ``budget``."""
        deadline = time.monotonic() + budget
        assert self.task is not None
        while time.monotonic() < deadline:
            if self.task.done():
                if self.error is not None:
                    raise self.error
                return self.ended
            await asyncio.sleep(0.05)
        return False

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def _publish_burst(bus: AgentRunEventBus, run_id: str, count: int) -> None:
    for i in range(1, count + 1):
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: e-{i}\n\n".encode())


# ==================================================================================================
# (5) THE REPLAY INVARIANT — the only assertion here that discriminates TODAY.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_reader_does_not_touch_the_channel_until_the_replay_is_done(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """§3.2.2 variant (a): no ``get_message`` between SUBSCRIBE and the end of LRANGE.

    ⚠️ STATE: green now, discriminating now. The mechanism exists; its mutation reddens this test.

    Why it must be asserted on the MECHANISM. The consequence of breaking it — the reader classifies
    channel copies with ``delivered = 0`` and hands the client a second copy of the whole ring — is
    a RACE. An outcome-only test passes whenever the schedule happens to be kind, and this contour
    has already shipped one invariant that was "satisfied by the absence of a mechanism".

    Two assertions, and the second is the one an optimisation would break: the channel is not read
    before the replay finished (count unchanged across ``LRANGE``), and a message published DURING
    the replay is nevertheless delivered exactly once — classified afterwards, against a baseline
    that is by then complete.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 5)

        probe: dict[str, CountingPubSub] = {}
        inner_subscribe = bus.subscribe

        def _subscribe(rid: str) -> Any:
            wrapped = CountingPubSub(inner_subscribe(rid))
            probe["pubsub"] = wrapped
            return wrapped

        bus.subscribe = _subscribe  # type: ignore[method-assign]

        window: dict[str, int] = {}
        inner_replay = bus.replay

        async def _replay(rid: str) -> Any:
            # A real event lands on the channel WHILE the replay is running — the situation the
            # invariant is about. It must be classified after the baseline exists, not during.
            await bus.publish(rid, epoch=_EPOCH, raw=b"data: during-replay\n\n")
            ring = await inner_replay(rid)
            # ⚠️ The window is measured from SUBSCRIBE to the END OF THE REPLAY, not from the entry
            # of this wrapper. Measuring only "inside bus.replay" left the obvious optimisation —
            # a poll placed just BEFORE the replay call — outside the window entirely, and the probe
            # for this test passed with the invariant broken. The window has to be the one the
            # invariant names.
            window["at_replay_end"] = probe["pubsub"].get_message_calls
            return ring

        bus.replay = _replay  # type: ignore[method-assign]

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
        # 5 replayed + the one published during the replay.
        for _ in range(200):
            if len(reading.blocks) >= 6:
                break
            await asyncio.sleep(0.05)
        await reading.stop()

    assert probe["pubsub"].subscribed_at == 0, (
        "the channel was polled before SUBSCRIBE was even issued — the subscription is LAZY, which "
        "TD-047 forbids: events published before the first get_message are lost outright"
    )
    subscribed_at = probe["pubsub"].subscribed_at
    assert window["at_replay_end"] == subscribed_at, (
        f"the reader polled the channel {window['at_replay_end'] - (subscribed_at or 0)} time(s) "
        "between SUBSCRIBE and the end of the replay. Then classification runs with delivered=0 "
        "and "
        "replay_min_seq=0, every channel copy of a ring element looks due, and the client receives "
        "the entire ring TWICE."
    )
    bodies = [b.split(b"data: ", 1)[1] for b in reading.blocks]
    assert (
        bodies.count(b"during-replay\n\n") == 1
    ), f"the mid-replay event was not delivered exactly once: {bodies}"
    assert len(bodies) == len(set(bodies)), f"the client received duplicates: {bodies}"


# ==================================================================================================
# (1) NORMAL vs ABNORMAL TERMINATION — the flag is set BEFORE the sentinel.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_normal_end_drains_the_queue_and_delivers_the_terminal_event(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(1a) Flag set ⇒ the writer drains to the sentinel, and ``run.completed`` reaches the client.

    ⚠️ STATE: green now, discriminating later. Today's writer drains the queue before honouring any
    close signal, so the outcome is already right; the mutations this guards ("trust only the
    abnormal Event", "sentinel before the flag") do not exist yet and CANNOT be probed until the
    flag does. Reported as such — not as coverage.

    The client is deliberately slower than the reader so that the queue still holds items when the
    reader finishes. A writer that closed on the reader's completion instead of on an empty queue
    would drop exactly the tail that carries the terminal event, and close rule 1 would silently
    degrade into "the client should have refetched from /state".
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 8)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())
        # The run really is over — this is what makes the end NORMAL rather than a disconnect.
        await _set_status(db_sessionmaker, run_id, "completed")

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.05).start()
        ended = await reading.wait_until_ended(budget=25.0)
        await reading.stop()

    assert ended, "the stream never ended by itself on a terminal run"
    assert _has_completed(reading.blocks), (
        f"the terminal event was dropped on the way out — {len(reading.blocks)} blocks delivered. "
        "The client is then told the stream is over without being told "
        "the run completed, and close "
        "rule 1 degrades into 'go and refetch /state'."
    )
    assert reading.blocks[-1].endswith(
        f"data: {_COMPLETED}\n\n".encode()
    ), "the terminal event arrived but not LAST — the drain is not ordered"


@pytest.mark.asyncio
async def test_an_abnormal_end_abandons_the_queue_without_waiting_for_a_sentinel(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(1b) No flag ⇒ the queue is abandoned at once; the writer must not wait for a sentinel.

    ⚠️ STATE: green now, discriminating later — the mutation ("the writer always drains to the
    sentinel") needs the sentinel to exist. Its failure mode is a HANG, which is why the assertion
    is on a self-inflicted end within a budget and not on the blocks received.

    The reader is killed mid-flight by making its first Redis read raise. In a real crash no
    sentinel is ever placed and none ever will be, so a writer that waits for one waits for ever —
    on a request that is still holding a connection.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 3)

        async def _boom(_run_id: str) -> Any:
            raise RedisError("the reader dies here")

        bus.replay = _boom  # type: ignore[method-assign]

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
        # Generously more than any legitimate handover, far less than a hang.
        ended = await reading.wait_until_ended(budget=15.0)
        await reading.stop()

    assert ended or reading.error is not None, (
        "the reader died and the stream neither ended nor raised — it is "
        "open, empty and permanent. "
        "No member of A/B/C fires (no events arrive at all) and close rules 4/5 need a terminal "
        "status and a dead lease, neither of which exists."
    )


# ==================================================================================================
# (4) THE RESERVED SLOT — a rule, not a property of ``maxsize``.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_terminal_event_survives_a_queue_filled_to_the_ceiling(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(4) The queue sits at its ceiling and the normal end-of-stream signal still fits.

    ⚠️ STATE: green now; the mutation ``qsize() < maxsize`` instead of ``< QUEUE_MAX`` IS applicable
    today, so this one can be probed — but what it protects (the sentinel) does not exist yet, so a
    green result proves the RULE holds, not that the sentinel is placeable.

    The client is slow enough that the ring's blocks pile up to the ceiling while the reader is
    still working; the run is terminal, so the end is normal. Losing the terminal event here is the
    exact price of letting ordinary traffic occupy the last slot.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # Comfortably more than the queue depth, so the ceiling is genuinely reached.
        await _publish_burst(bus, run_id, _QUEUE_MAX * 3)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())
        await _set_status(db_sessionmaker, run_id, "completed")

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.03).start()
        ended = await reading.wait_until_ended(budget=30.0)
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert _has_completed(reading.blocks), (
        f"the terminal event was lost with the queue at its ceiling "
        f"({len(reading.blocks)} blocks). "
        "An ordinary block took the slot the end-of-stream signal needs, so the signal became "
        "unavailable in exactly the state it was reserved for."
    )


# ==================================================================================================
# (2) HYSTERESIS — the phase switch is decoupled from the depth guard.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_client_that_honestly_drained_a_full_replay_is_not_disconnected(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(2) A reconnect to a FULL ring must not end in a disconnect at the phase boundary.

    ⚠️ STATE: green now, discriminating later. The live-phase depth disconnect does not exist yet, so
    nothing can drop this client today; the mutation ("switch phase at the end of the replay")
    likewise cannot be applied. This is the executable form of the requirement, not evidence.

    ⚠️ WHY THE CLIENT MUST BE SLOWER THAN LOCAL DECODING. At the boundary the queue is full BY
    CONSTRUCTION, and only a slow client reproduces that: the replay places blocks while WAITING for
    room, so the last put leaves the queue against the ceiling, and channel messages have been
    accumulating in the connection buffer for the whole replay (that is the mechanism of the
    invariant above), so the reader has something to place immediately. A fast client empties the
    queue and the state under test never occurs — the test would pass without exercising anything.

    Hysteresis is what separates the two: live discipline begins only once the queue has fallen to
    half the ceiling, so the first live put is guaranteed at least half the depth in free slots —
    the client has DEMONSTRATED it drains before depth starts being read as lag.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # A full ring: an order of magnitude more than the queue holds, as in production.
        await _publish_burst(bus, run_id, _RING_MAX)

        # ⚠️ GENUINELY NEW events must follow the replay, or this test cannot exercise the boundary
        # at all. Every channel copy of a REPLAYED event is a duplicate, and duplicates are
        # classified away BEFORE the queue by design — so a ring-only scenario reaches the live
        # phase with nothing to put, and the first live put never happens. The probe for the
        # hysteresis mutation stayed green for exactly that reason until these were added. They are
        # published after LRANGE has read the ring, so their seq is above the replayed tail.
        inner_replay = bus.replay

        async def _replay(rid: str) -> Any:
            ring = await inner_replay(rid)
            for i in range(1, 6):
                await bus.publish(rid, epoch=_EPOCH, raw=f"data: live-{i}\n\n".encode())
            return ring

        bus.replay = _replay  # type: ignore[method-assign]

        expected = _RING_MAX + 5
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.01).start()
        for _ in range(2000):
            if len(reading.blocks) >= expected:
                break
            await asyncio.sleep(0.02)
        received = len(reading.blocks)
        ended_early = reading.task is not None and reading.task.done()
        await reading.stop()

    assert not ended_early or received >= expected, (
        f"the client was disconnected after {received} of {expected} blocks. It kept draining "
        "throughout, so this is the phase boundary firing the depth guard on a queue that is full "
        "by construction — the normal outcome of a SUCCESSFUL replay becomes a disconnect."
    )
    assert received >= expected, (
        f"only {received} of {expected} blocks arrived; the replay must FLOW THROUGH the queue "
        f"(ring {_RING_MAX} against depth {_QUEUE_MAX}), not fit into it, and the five live events "
        "after it must cross the phase boundary"
    )


# ==================================================================================================
# (3) TWO BUDGETS, ONE PER PHASE — and the replay wait is bounded at all. RED TODAY.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_client_that_stops_reading_during_the_replay_is_released_by_time(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(3a) The replay wait has a DEADLINE — ``AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS``.

    ⚠️ STATE: **RED now, by design.** ``_put_due`` polls for room with no bound at all (stated
    openly in its docstring as an open D2 point), so a client that stops reading holds the reader —
    and the request — indefinitely. Nothing but cancellation ends it today.

    The client reads a few blocks and then stops. Depth cannot save this: the queue is full, the
    reader is blocked inside the put, and there is no further arrival to trip a guard on. Only a
    clock can end it, which is why the budget is a requirement and not a refinement.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, _RING_MAX)

        # Reads enough to fill the queue, then goes silent ONCE for longer than the budget. In a
        # real deployment this is a phone that lost its network without closing the connection.
        started = time.monotonic()
        reading = Reading(
            broker.stream(run_id=run_id, cursor=Cursor()),
            delay=0.0,
            stall_after=_QUEUE_MAX + 2,
            stall_for=_DRAIN_SECONDS * 2,
        ).start()
        # Generous: the budget, the stall that outlasts it, and slack for scheduling.
        stalled_client_ended = await reading.wait_until_ended(budget=_DRAIN_SECONDS * 2 + 5.0)
        elapsed = time.monotonic() - started
        received = len(reading.blocks)
        await reading.stop()

    assert stalled_client_ended, (
        f"a client that stopped reading held the stream open past its budget ({elapsed:.1f}s, "
        f"{received} blocks). The reader is parked inside its put with no deadline, so nothing "
        "ends "
        "this: not depth (the queue is full and nothing new arrives), not the close rules (the run "
        "is live and the lease is alive). It resumed and was served in full instead."
    )
    assert elapsed <= _DRAIN_SECONDS * 2 + 5.0, (
        f"the stream was held {elapsed:.1f}s against a budget of {_DRAIN_SECONDS}s per phase; the "
        "ADR bounds the worst case at 2x the value (slow replay, then slow drain)"
    )


@pytest.mark.asyncio
async def test_the_drain_gets_its_own_budget_and_not_the_replays_leftovers(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(3b) Two deadlines from one value, each counted LOCALLY in its own task.

    ⚠️ STATE: green now, discriminating later — and the label was corrected after running it. With
    NO budget implemented at all, nothing can expire, so the outcome is already right; the mutation
    ("one counter for both phases") needs a counter to exist before it can be applied.

    The scenario is the one the ADR names as fatal for a single counter: a replay slow enough to
    have spent most of a shared budget, followed by a normal drain. With one counter the drain
    inherits nothing and the terminal event is discarded by expiry — the normal path's priority
    defeated by the very budget meant to bound it. With two, the drain starts its own clock and
    the terminal event arrives.
    Also pins the honest upper bound: the ADR accepts up to ``2 x`` the value for "slow replay, then
    slow drain", so the assertion is against that and not against one budget.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, _RING_MAX)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())
        await _set_status(db_sessionmaker, run_id, "completed")

        # Slow enough that the replay alone eats most of one budget: 100 blocks x 25ms = 2.5s
        # against a 3s budget.
        started = time.monotonic()
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.025).start()
        ended = await reading.wait_until_ended(budget=_DRAIN_SECONDS * 4)
        elapsed = time.monotonic() - started
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert _has_completed(reading.blocks), (
        f"the terminal event was lost after a slow replay ({len(reading.blocks)} blocks in "
        f"{elapsed:.1f}s). A single budget shared by both phases leaves the drain with nothing, so "
        "the priority of the normal path is cancelled by the clock rather than by a decision."
    )
    assert (
        elapsed <= _DRAIN_SECONDS * 2 + 2.0
    ), f"held for {elapsed:.1f}s — the ADR's honest ceiling is 2x{_DRAIN_SECONDS}s"


# ==================================================================================================
# (6) THE SUPERVISOR MUST NOT CUT A NORMAL DRAIN SHORT.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_supervisor_does_not_abort_a_normal_drain(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """(6) Close rules 4 and 5 are true DURING the drain, and the client still receives everything.

    ⚠️ STATE: green now, discriminating later — the flag the correctness rests on does not exist yet.

    ⚠️ THE OVERLAP IS GUARANTEED, NOT RARE, and that is why this test is not a corner case: a normal
    end means by definition that the run is terminal, so rule 4 is true precisely while the drain is
    running, and rule 5 follows immediately after (the consumer releases its lease on termination).
    The drain therefore MUST outlast a supervisor tick for the test to exercise anything — hence the
    slow client and the one-second tick.

    Roles are not symmetric here, and the ADR is explicit: the flag carries the correctness, while
    cancelling the supervisor only narrows the race and stops needless DB traffic. So the mutation
    "remove the supervisor cancellation, keep the flag" must leave this test GREEN — asserting
    otherwise would give a bookkeeping detail the weight of an invariant.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, _QUEUE_MAX * 4)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())

        # Both rules become true while the drain is in progress: terminal status (rule 4) and,
        # right after, no lease (rule 5).
        await _set_status(db_sessionmaker, run_id, "completed")
        await _drop_lease(bus, run_id)

        # 40 blocks x 0.1s = ~4s of draining against a 1s supervisor tick.
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.1).start()
        ended = await reading.wait_until_ended(budget=30.0)
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert _has_completed(reading.blocks), (
        f"the supervisor's verdict cut the drain short — "
        f"{len(reading.blocks)} blocks delivered and "
        "the terminal event was not among them. Rule 4 is true throughout a normal drain BY "
        "DEFINITION, so a supervisor that closes on it discards the tail of every completed run."
    )
    assert len(reading.blocks) >= _QUEUE_MAX * 4, (
        f"only {len(reading.blocks)} of {_QUEUE_MAX * 4 + 1} blocks arrived — the drain was "
        "truncated even though every one of them was already owed to the client"
    )


# ==================================================================================================
# THE ENTRANCE FOUND BY RUNNING, NOT BY READING: a run already TERMINAL when the stream opens.
#
# Both architect and reviewer concluded from the text that the normal-end FLAG was necessary AND
# sufficient, and that cancelling the supervisor added nothing to correctness. That conclusion is
# false, and only a run can show why: the flag is set by the reader when it FINISHES, while close
# rule 4 ("the run is terminal") is true from the very first supervisor tick. For a run terminal at
# open the two orderings cannot be reconciled — there is no moment at which the flag exists and the
# supervisor has not yet had its say. The supervisor set the abnormal signal in the middle of the
# replay, the writer abandoned the queue by design, and the client got 20 of 41 blocks.
#
# What closes it is a ONE-WAY state ("this ending will be normal") published by the reader as soon
# as it knows, i.e. BEFORE it finishes — not the flag, which is necessarily later.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_run_terminal_at_open_still_delivers_its_whole_replay(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The flag is NOT sufficient: rule 4 is true before the flag can possibly exist.

    A client reconnecting to a run that has already finished is the ordinary case, not a corner
    one — it is exactly what §3.4 tells clients to do. The replay must therefore outlast a
    supervisor tick for this test to exercise anything, which is what the slow client and the short
    tick are for.

    ⚠️ Not reachable by reading the spec: the text says the flag carries the correctness, and it does
    for a run that finishes WHILE being watched. This is the other order of events, and no amount of
    care about the flag helps, because the flag is by construction later than the supervisor's first
    verdict.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    # Terminal BEFORE anyone subscribes — the whole point.
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 40)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())

        # 41 blocks x 0.1s = ~4s of delivery against a 2s supervisor tick, so rule 4 is evaluated
        # at least twice while the hand-over is still in progress.
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.1).start()
        ended = await reading.wait_until_ended(budget=40.0)
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert len(reading.blocks) == 41, (
        f"only {len(reading.blocks)} of 41 blocks reached the client. The supervisor's rule-4 "
        "verdict landed mid-replay and the writer abandoned the queue — for a run terminal at open "
        "the flag cannot prevent this, because the flag is set when the reader FINISHES and rule 4 "
        "is true from the first tick."
    )
    assert _has_completed(reading.blocks), "the terminal event was among the blocks discarded"


@pytest.mark.asyncio
async def test_the_drain_budget_bounds_one_wait_and_not_the_whole_phase(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The budget answers "no progress for this long", never "this phase has run too long".

    ⚠️ Found by MEASUREMENT, and the symptom is deliberately narrow: with a phase-total deadline the
    client loses exactly ONE block — the last, terminal one — after receiving forty. 41 blocks at
    0.1s is 4.1s of honest, steady draining against a 3s budget, so a phase clock expires on the
    final hand-over while a per-wait clock never starts counting at all.

    That is the same "penalise the honest client" defect this section has already rejected twice: a
    client on a slow network receiving a full ring legitimately takes longer than any budget meant
    to bound WAITING. Progress must reset the clock, which makes the reading "per wait" rather than
    "per phase" — and the ADR's "at most 2x the value" then holds only for the case it names, not as
    a universal cap. Stated here because the test would otherwise look like it contradicts the ADR.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 40)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())
        await _set_status(db_sessionmaker, run_id, "completed")

        # Steady progress, never a pause: 41 x 0.1s = 4.1s against a 3s budget.
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.1).start()
        ended = await reading.wait_until_ended(budget=40.0)
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert _has_completed(reading.blocks), (
        f"the client took {len(reading.blocks)} blocks at a steady pace and lost the terminal one. "
        "The budget was counted from the start of the phase, so it expired on a client that never "
        "once stopped taking what it was owed."
    )
    assert len(reading.blocks) == 41, f"expected all 41 blocks, got {len(reading.blocks)}"


@pytest.mark.asyncio
async def test_a_client_disconnecting_tears_the_pipeline_down_without_hanging(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """D1 requirement 2: ``aclose()`` returns, and no task or subscription is left behind.

    The generator's ``finally`` cancels the reader, the supervisor and the pending waiters. Two
    distinct failure modes are separated here, because they need different fixes:

    * **a HANG** — a reader that swallowed ``CancelledError`` without re-raising makes the
      cancellation a no-op, the ``TaskGroup`` waits for a task that never ends, and ``aclose()``
      never returns while holding the connection;
    * **a RAISE** — ``aclose()`` throws ``GeneratorExit`` at the ``yield``; that exception is a
      ``BaseException``, so ``TaskGroup.__aexit__`` catches it, cancels the children and re-raises
      it WRAPPED in a ``BaseExceptionGroup``. ``aclose()`` then does not return — it
      raises — and the
      async-generator finalisation protocol is broken for every ordinary client disconnect.

    Both are asserted, and the second is reproducible in twenty lines with no project code at all
    (an async generator that yields from inside ``async with asyncio.TaskGroup()``), so it is a
    property of the STRUCTURE and not of anything this contour does.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id)

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, _RING_MAX)

        stream = broker.stream(run_id=run_id, cursor=Cursor())
        taken = 0
        async for _block in stream:
            taken += 1
            if taken >= 3:
                break  # the client walks away mid-stream, exactly as a closed connection does

        closed = False
        raised: BaseException | None = None
        try:
            # Must RETURN: not hang (the swallowed-cancellation mutation) and not raise (the
            # TaskGroup-inside-a-generator defect).
            #
            # ⚠️ ``asyncio.timeout``, NOT ``asyncio.wait_for``. ``wait_for`` runs ``aclose()`` in a
            # SEPARATE task, and that task boundary changes where ``GeneratorExit`` is delivered:
            # with it the observation became order-dependent — this test passed alone and failed
            # under random ordering in the full gate, the worst kind of detector. The
            # timeout context cancels the CURRENT task instead, so ``aclose()`` is awaited directly,
            # exactly as an ASGI server awaits it, and the result is deterministic.
            async with asyncio.timeout(10.0):
                await stream.aclose()
            closed = True
        except TimeoutError:
            pass
        except BaseException as exc:  # noqa: BLE001 - classified below, never swallowed silently
            raised = exc
        await asyncio.sleep(0.2)
        after = [t for t in asyncio.all_tasks() if "agent-run-events" in (t.get_name() or "")]

    assert taken == 3
    assert not after, f"tasks leaked after the client disconnected: {[t.get_name() for t in after]}"
    assert raised is None, (
        f"aclose() raised {type(raised).__name__} instead of returning: {raised!r}. "
        "GeneratorExit is a BaseException, so a TaskGroup wrapping the yield catches it and "
        "re-raises it inside a "
        "BaseExceptionGroup — every ordinary client disconnect now ends in an exception out of the "
        "teardown path."
    )
    assert closed, "aclose() never returned — cancellation is not propagating to the children"


def test_no_agent_proxy_code_defeats_cancellation() -> None:
    """Static guard for D1(2): cancellation must propagate through every task in the contour.

    ⚠️ A STATIC test on purpose, and it is the only one of its kind here. The behaviours it forbids
    are individually reasonable-looking and each one silently disables a cancellation path that the
    whole shutdown design (the generator's ``finally``, ``ConsumerRegistry.drain``, §6.4) rests on.
    Their failure mode is a hang in teardown, which is expensive to provoke per call site and cheap
    to forbid by inspection — backend found one real violation this way, introduced by an earlier
    change of its own, and a manual sweep does not catch the next one.

    ``contextlib.suppress(Exception)`` and ``except Exception`` are NOT flagged: ``CancelledError``
    derives from ``BaseException``, so they let it through. Only the constructs that actually catch
    it are.
    """
    import re
    from pathlib import Path

    forbidden = {
        "asyncio.shield(": "shield detaches the child from the caller's cancellation",
        "except BaseException": "catches CancelledError; use `except Exception` instead",
        "suppress(BaseException": "suppresses CancelledError",
    }
    offences: list[str] = []
    directory = Path(__file__).resolve().parents[2] / "src" / "app" / "agent_proxy"
    for path in sorted(directory.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            for needle, why in forbidden.items():
                if needle in code:
                    offences.append(f"{path.name}:{number} {needle!r} — {why}")
            if re.match(r"\s*except\s*:", code):
                offences.append(f"{path.name}:{number} bare `except:` — catches CancelledError")
            # `except CancelledError` is legitimate only when it re-raises. Checked by looking for a
            # `raise` in the handler body, which for this codebase is always the next few lines.
            if re.search(r"except\s+(asyncio\.)?CancelledError", code):
                lines = path.read_text(encoding="utf-8").splitlines()
                body = lines[number : number + 6]
                if not any(re.match(r"\s*raise\b", b) for b in body):
                    offences.append(
                        f"{path.name}:{number} `except CancelledError` without a re-raise — "
                        "the task survives cancellation and its awaiter hangs"
                    )

    assert not offences, (
        "cancellation-defeating constructs in the agent-proxy contour:\n" + "\n".join(offences)
    )


# ==================================================================================================
# THE STAND-DOWN CONDITION IS THE WRITER'S PROGRESS — not what the reader has managed to learn.
#
# ⚠️ WRITTEN BEFORE THE IMPLEMENTATION. Today the stand-down is gated on `activity.settled` ("the
# reader knows the ending"), and §3.2.2 replaces that with "the last chunk handed to the client is
# no older than AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS". Two independent reasons, and the second is
# what the tests below are for:
#
#   1. an UNCONDITIONAL stand-down disables close rules 4/5 for ever — and rule 4 is the only thing
#      through which a client stream observes AGENT_RUN_MAX_DURATION_SECONDS at all. The old
#      justification ("the reader will close it anyway, all its awaits are bounded") is false:
#      bounded awaits give non-blocking, not termination — `get_message` returns None on timeout and
#      the loop goes round again;
#   2. a list of "what the reader has learned" is INCOMPLETE BY CONSTRUCTION, because terminality is
#      produced OUTSIDE the reader: the consumer writes it to Postgres, and the supervisor sees it
#      there before the terminal event travels the channel. Enumerating the knowledge states of one
#      actor cannot settle a question another actor decides.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_run_that_turns_terminal_DURING_delivery_still_delivers_everything(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The entrance no enumeration of reader knowledge can cover — terminality arrives elsewhere.

    ⚠️ STATE: **RED now, by design.** The run is not terminal at open, the ring holds no terminal
    event, so nothing can set `settled`; the supervisor's rule 4 becomes true mid-delivery and the
    writer abandons the queue with everything still owed in it.

    Setup mirrors the reachable production case: a client reconnects to a long ring on a slow link
    while the run is still going, and the consumer finishes the run a couple of seconds later.
    There is no moment at which the reader could have known — the fact is written to Postgres by
    another process.

    ⚠️ The mutation "stand down on a list of reader knowledge states" must redden this. It is the
    reason the condition is the writer's PROGRESS: while the client is being handed what it is owed,
    no verdict may be issued, whatever anyone knows about the outcome.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # 40 blocks and NO terminal event in the ring. ⚠️ The terminal event MUST NOT be published
        # here: putting it in the ring makes the reader observe the ending during its replay, which
        # is the case the neighbouring test covers and NOT this one. An earlier version of this test
        # published it anyway — the comment said one thing and the code did another, and the test
        # passed while exercising nothing.
        await _publish_burst(bus, run_id, 40)

        # 41 x 0.1s = ~4s of delivery against a 2s supervisor tick.
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.1).start()

        async def _finish_the_run() -> None:
            # The consumer completes the run while the hand-over is in progress: it publishes the
            # terminal event to the CHANNEL (so it arrives live, long after the replay baseline was
            # taken) and drops its lease — rules 4 AND 5 both become true mid-delivery, and the
            # reader had no way to know any of it in advance.
            # ⚠️ The two events are separated ON PURPOSE, and an earlier version that did both at
            # 2.0s was racy: the first supervisor tick also falls at 2.0s, so whether it saw the run
            # as terminal was a coin toss, and the test settled on the benign side.
            #
            # Terminality becomes visible to the SUPERVISOR at 1.0s — before its first tick at 2.0s.
            await asyncio.sleep(1.0)
            await _set_status(db_sessionmaker, run_id, "completed")
            await _drop_lease(bus, run_id)
            # The terminal EVENT reaches the channel much later, so the reader cannot possibly know
            # the ending when the supervisor passes its verdict.
            await asyncio.sleep(2.5)
            await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())

        finisher = asyncio.create_task(_finish_the_run())
        ended = await reading.wait_until_ended(budget=40.0)
        await asyncio.gather(finisher, return_exceptions=True)
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert len(reading.blocks) == 41, (
        f"only {len(reading.blocks)} of 41 blocks reached the client. The run turned terminal "
        "while the writer was still handing over what it was owed, and the supervisor's verdict "
        "discarded the rest. No state the READER could hold would have prevented this — "
        "terminality is written "
        "by the consumer, outside the reader entirely."
    )
    assert _has_completed(reading.blocks), "the terminal event was among the discarded blocks"


@pytest.mark.asyncio
async def test_a_writer_that_stops_making_progress_lets_the_close_rules_return(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The stand-down is BOUNDED by the same clock as the budgets, so termination is never disabled.

    ⚠️ STATE: green now, discriminating later. Today the supervisor never stands down in this
    scenario (`settled` is never set), so rule 4 closes the stream for a different reason. The
    mutation this exists for — an UNCONDITIONAL stand-down — makes it hang, which is precisely the
    failure the previous decision would have introduced.

    The shape matters: the writer FIRST makes progress (so a progress-gated supervisor does stand
    down), and only then the client goes silent. With the stand-down unbounded, the writer waits on
    the queue, the supervisor has withdrawn, and the reader keeps looping on a channel that returns
    None — three live tasks and nobody left to close the stream. That is also how
    ``AGENT_RUN_MAX_DURATION_SECONDS`` stops being observable to a client, since rule 4 is its only
    route to one.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # A handful of blocks and NO terminal event: the reader stays alive in the live phase.
        await _publish_burst(bus, run_id, 5)

        reading = Reading(
            broker.stream(run_id=run_id, cursor=Cursor()),
            delay=0.0,
            # Progress happens first, then the client goes quiet for longer than the budget.
            stall_after=3,
            stall_for=_DRAIN_SECONDS * 2,
        ).start()

        async def _finish_the_run() -> None:
            await asyncio.sleep(1.0)
            await _set_status(db_sessionmaker, run_id, "completed")
            await _drop_lease(bus, run_id)

        finisher = asyncio.create_task(_finish_the_run())
        # The budget, plus a supervisor tick to notice, plus slack.
        ended = await reading.wait_until_ended(budget=_DRAIN_SECONDS * 2 + _SUPERVISOR_TICK + 8.0)
        await asyncio.gather(finisher, return_exceptions=True)
        await reading.stop()

    assert ended, (
        f"the stream stayed open after the writer stopped making progress ({len(reading.blocks)} "
        "blocks). An unbounded stand-down disables close rules 4/5 permanently: the writer waits "
        "on "
        "the queue, the supervisor has withdrawn, and the reader loops on a channel that yields "
        "None — nothing is left that can end the session, and MAX_DURATION becomes unobservable."
    )


# ==================================================================================================
# P4 — the NORMAL completion of one link must NOT cancel the others.
#
# P3 and P4 came free with ``TaskGroup`` and are lost SILENTLY when the construction is replaced
# (which it is being, to fix P1). Their consequences differ: without P3 orphaned tasks remain, and
# without P4 a reader that finished normally tears down the drain and the client loses the terminal
# event. So P4 needs an assertion of its own, phrased against behaviour rather than construction.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_readers_normal_finish_does_not_tear_down_the_drain(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """P4: the reader ends long before the writer, and the writer must keep going to the end.

    The gap is made large on purpose — a ring of 40 against a queue of 10 and a client at 0.1s/block
    means the reader has placed everything and finished while roughly three quarters of the blocks
    are still owed. If a link's normal return cancelled its siblings, the client would lose that
    remainder and, with it, ``run.completed``.

    ⚠️ Distinct from the flag test next door: that one is about how the WRITER distinguishes the two
    endings; this one is about the LIFETIME rule of the group holding the two tasks. A construction
    can satisfy either and violate the other, and the replacement for ``TaskGroup`` must be checked
    for both.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 40)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.1).start()
        ended = await reading.wait_until_ended(budget=40.0)
        await reading.stop()

    assert ended, "the stream never ended by itself"
    assert len(reading.blocks) == 41, (
        f"the reader finished and the writer stopped with it — {len(reading.blocks)} of 41 blocks "
        "delivered. A link's NORMAL return must not cancel its siblings; only an exception may."
    )
    assert _has_completed(reading.blocks), "the terminal event was lost with the cancelled drain"


# ==================================================================================================
# THE TWO SCENES THE EXISTING TESTS NEVER BUILT — both properties are computed at moments no
# previous test reached, which is exactly why their mutations were inert.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_sentinel_fits_when_the_queue_is_full_at_the_moment_it_is_placed(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The reserved slot, observed AT THE INSTANT the sentinel is queued — not merely nearby.

    ⚠️ WHY THE EARLIER TESTS COULD NOT CATCH THE ONE-SLOT MUTATION. They all had a client that kept
    reading, so by the time the reader reached the sentinel the client had taken a block
    and room had
    appeared. The rule is computed exactly once — on the ``put_nowait`` of the sentinel against a
    queue at its ceiling — and no scene reached that instant. A rule can be present on every code
    path and still be untested if nothing ever exercises the state it decides.

    The scene: ``QUEUE_MAX + 1`` blocks in the ring, the terminal event LAST, and a client
    that stops
    reading entirely right after its first block. While it is silent the reader fills the queue to
    the brim and then places the sentinel.

    ⚠️ THE CONSEQUENCE IS A LATE CLOSE, NOT A LOST EVENT — corrected by measurement. The ADR, and my
    own first version of this test, expected the mutation to cost the terminal event. It does not,
    and the reason is another requirement of the same section: the flag is set BEFORE the sentinel.
    So when ``put_nowait`` of the sentinel raises ``QueueFull``, ``normal_end`` is already set, the
    writer still takes the drain path and hands over everything — the sentinel's absence only means
    the drain ends on its BUDGET instead of on the sentinel. Instrumented: ``qsize=11 maxsize=11``
    at the put, ``QueueFull`` raised, and the client still received 12 of 12 including
    ``run.completed``, 3.0s later than the baseline — exactly one
    ``AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS``.

    So the assertion is on the CLOSE LATENCY, the property that actually differs. The two
    requirements interact: mandating the flag before the sentinel demotes the reserve from
    "protects the terminal event" to "closes the stream promptly and does not fail the reader" — and
    the reserve is worth keeping on that ground alone, since a dying reader means a logged failure
    and a timeout close on every completed run whose client stalls.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # ⚠️ THE ARITHMETIC IS THE TEST. The client takes exactly ONE block and stops, so with the
        # reserve gone (ceiling = QUEUE_MAX + 1) the reader can place all the remaining blocks and
        # the LAST of them brings the queue to `maxsize` — which is the only state in which
        # `put_nowait` of the sentinel raises. One block fewer and the reader parks before the
        # sentinel; one more and it parks with blocks still to place. An earlier version used
        # QUEUE_MAX + 1 elements and the mutation stayed inert for exactly that reason.
        await _publish_burst(bus, run_id, _QUEUE_MAX + 1)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())
        expected = _QUEUE_MAX + 2

        stall_for = 2.0
        started = time.monotonic()
        reading = Reading(
            broker.stream(run_id=run_id, cursor=Cursor()),
            delay=0.0,
            # Silent from the very first block, long enough for the reader to fill the queue and
            # reach the sentinel while there is demonstrably no room to spare.
            stall_after=1,
            stall_for=stall_for,
        ).start()
        ended = await reading.wait_until_ended(budget=40.0)
        elapsed = time.monotonic() - started
        await reading.stop()

    assert ended, "the stream never ended by itself"
    # THE discriminating assertion: the stream closes ON THE SENTINEL, promptly after the last
    # block — not a whole drain budget later, which is what losing the sentinel costs.
    assert elapsed < stall_for + _DRAIN_SECONDS * 0.8, (
        f"the stream took {elapsed:.1f}s to close against a stall of {stall_for}s — a full drain "
        f"budget ({_DRAIN_SECONDS}s) of delay, i.e. the end-of-stream sentinel never arrived: "
        "ordinary blocks took the slot reserved for it, put_nowait raised QueueFull, the reader "
        "died, and the stream had to time out instead of ending cleanly."
    )
    assert len(reading.blocks) == expected, (
        f"{len(reading.blocks)} of {expected} blocks delivered. With no slot reserved, ordinary "
        "blocks fill the queue to maxsize and the end-of-stream sentinel cannot be placed at all — "
        "the reader fails and the abnormal path discards the terminal event already queued."
    )
    assert _has_completed(reading.blocks), "the terminal event was lost with the sentinel"


@pytest.mark.asyncio
async def test_a_client_silent_FOREVER_does_not_mute_the_close_rules(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """The freshness conjunct, observed where it is computed: a stall that never ends.

    ⚠️ WHY THE EARLIER TESTS COULD NOT CATCH ITS REMOVAL. Every stall in this module was one-shot —
    the client resumed, the queue drained, and the stand-down lifted by itself, so "there is
    something to hand over" alone was enough to explain every green result. The freshness
    conjunct is
    the ONLY thing that lifts a stand-down that would otherwise never lift, and that requires a
    client which stops for good while the queue is still non-empty.

    Without it the two close rules are muted for the rest of the session: the queue stays non-empty
    for ever, so the first conjunct holds for ever. And rule 4 is the only route by which
    ``AGENT_RUN_MAX_DURATION_SECONDS`` becomes observable to a client, so the loss is not merely a
    late close — it is the upper bound on a session disappearing.

    Deliberately NOT a normal end: no terminal event in the ring, so the reader stays alive in the
    live phase and ``drain_deadline`` is never armed. Otherwise the drain budget would close the
    stream on its own and mask the conjunct entirely.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        # ⚠️ FEWER blocks than the queue holds, on purpose. With more, the READER parks in its own
        # bounded wait for room and IT closes the stream when that budget expires — masking the
        # conjunct entirely (the first version of this test used QUEUE_MAX * 3 and the mutation
        # stayed inert). With five, the reader places everything, enters the live phase and waits on
        # a quiet channel, so the only thing that can end the session is the supervisor.
        await _publish_burst(bus, run_id, 5)

        reading = Reading(
            broker.stream(run_id=run_id, cursor=Cursor()),
            delay=0.0,
            stall_after=1,
            # Silent for well past the budget; the client "resumes" only to observe the verdict.
            stall_for=_DRAIN_SECONDS * 3 + _SUPERVISOR_TICK * 2,
        ).start()

        async def _finish_the_run() -> None:
            # Rules 4 and 5 both become true while the client is silent.
            await asyncio.sleep(1.0)
            await _set_status(db_sessionmaker, run_id, "completed")
            await _drop_lease(bus, run_id)

        finisher = asyncio.create_task(_finish_the_run())
        ended = await reading.wait_until_ended(
            budget=_DRAIN_SECONDS * 3 + _SUPERVISOR_TICK * 3 + 10.0
        )
        await asyncio.gather(finisher, return_exceptions=True)
        await reading.stop()

    assert ended, (
        f"the stream stayed open after the client went silent for good ({len(reading.blocks)} "
        "blocks taken). Without the freshness conjunct the queue is non-empty for ever, so the "
        "stand-down never lifts, close rules 4/5 are muted for the rest of the session, and "
        "MAX_DURATION stops being observable to a client at all."
    )
    # ⚠️ THIS is the assertion that discriminates, and it took a second look to find. While the
    # client is silent nothing about the stream is observable — the writer IS the generator and
    # cannot return unless someone pulls it. What IS observable is WHEN the verdict was passed: if
    # the supervisor un-muted during the silence and closed the session, the writer takes the
    # abnormal path on the very next pull and hands over NOTHING further; the untaken blocks stay in
    # the ring for a reconnect (§3.2.2 — the prefix is intact). With the conjunct removed the
    # supervisor never un-mutes, so on resume the client is simply served the rest.
    assert len(reading.blocks) == 1, (
        f"the client took {len(reading.blocks)} blocks: it was served the remainder on resume, "
        "which means no close verdict had been passed while it was silent — the stand-down was "
        "never lifted and rules 4/5 stayed muted."
    )


@pytest.mark.asyncio
async def test_a_supervisor_that_dies_during_a_normal_drain_does_not_strand_the_client(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """P3's ONE deliberate exception, checked for the thing that would make it a defect.

    P3 says an exception in any link cancels the others. The implementation exempts one case: a
    supervisor that fails DURING a normal drain is ignored, because the drain must reach
    the sentinel
    and is bounded by its own budget. The exemption is only defensible if the drain still ENDS, so
    that is what is asserted here — both halves: the client receives everything it is
    owed, including
    the terminal event, AND the stream closes rather than hanging on a dead supervisor.

    ⚠️ The supervisor is failed through its own probe (``_is_terminal`` raising), which is where a
    real failure would come from — a DB error mid-drain — rather than by cancelling the task from
    outside, which would test asyncio instead of the design.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="completed")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 30)
        await bus.publish(run_id, epoch=_EPOCH, raw=f"data: {_COMPLETED}\n\n".encode())
        expected = 31

        calls = {"n": 0}
        inner_terminal = broker._is_terminal

        async def _failing_terminal(rid: str) -> bool:
            calls["n"] += 1
            if calls["n"] > 1:
                # A database error in the supervisor's probe, arriving mid-drain.
                raise RedisError("supervisor probe failed")
            return await inner_terminal(rid)

        broker._is_terminal = _failing_terminal  # type: ignore[method-assign]

        # 31 x 0.1s = ~3s of draining, so the supervisor ticks (2s) and fails inside it.
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.1).start()
        ended = await reading.wait_until_ended(budget=40.0)
        await reading.stop()

    assert ended, (
        "the stream did not end after the supervisor failed mid-drain. Ignoring its exception is "
        "only defensible while the drain still terminates on its own — otherwise the exemption "
        "turns a dead link into a stranded client."
    )
    assert len(reading.blocks) == expected, (
        f"{len(reading.blocks)} of {expected} blocks delivered: the supervisor's failure cut the "
        "drain short, which is precisely what the exemption exists to prevent"
    )
    assert _has_completed(reading.blocks), "the terminal event was lost"


# ==================================================================================================
# M4a — the FIRST conjunct: a stand-down needs something to hand over at all.
# ==================================================================================================
@pytest.mark.asyncio
async def test_an_idle_leaseless_stream_with_an_empty_queue_closes_on_the_idle_timeout(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Rule 5 must fire on its own timescale, not a drain budget later.

    ⚠️ The two conjuncts fail in OPPOSITE directions, and this is the one nothing covered. Removing
    the freshness half mutes rules 4/5 for ever (a client that stops with a full queue);
    removing THIS
    half mutes them for a whole budget every time a hand-over merely finishes — the queue is empty,
    nothing is owed, and the last delivery is by definition recent. In production that is 120s of an
    idle leaseless stream held open where the idle timeout says 2.

    ⚠️ The consequence is a LATE CLOSE, so the assertion is on latency. The budget is deliberately
    stretched to 20s here while the idle timeout stays at 1s: with both near 3s the correct and the
    broken behaviour are indistinguishable inside the noise of a test, which is how a latency defect
    hides.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        # A budget far larger than the idle timeout, so the two timescales cannot be confused.
        broker, bus, _settings = broker_factory(session, AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS=20.0)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 3)

        started = time.monotonic()
        # Reads everything immediately, so the queue is empty and `last_delivered_at` is fresh —
        # exactly the state in which the first conjunct is the only thing lifting the stand-down.
        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()

        async def _abandon_the_run() -> None:
            # No consumer any more: rule 5 (idle AND no live lease) becomes true. The run itself is
            # NOT terminal, so rule 4 cannot close this stream — only rule 5 can.
            await asyncio.sleep(0.5)
            await _drop_lease(bus, run_id)

        abandoner = asyncio.create_task(_abandon_the_run())
        ended = await reading.wait_until_ended(budget=30.0)
        elapsed = time.monotonic() - started
        await asyncio.gather(abandoner, return_exceptions=True)
        await reading.stop()

    assert ended, "the idle leaseless stream never closed at all"
    assert len(reading.blocks) == 3, f"expected the 3 queued blocks, got {len(reading.blocks)}"
    # One idle timeout plus at most a couple of supervisor ticks — nowhere near the 20s budget.
    assert elapsed < 10.0, (
        f"the stream stayed open {elapsed:.1f}s after going idle without a lease. The queue was "
        "empty and nothing was owed, so the stand-down had no business holding "
        "rule 5 back — it did "
        "so for the freshness window (20s here, 120s in production) instead of "
        "the idle timeout (1s)."
    )


# ==================================================================================================
# Deferred coverage (#14): hysteresis thrashing, D2 on the probes, C4, the unparseable message.
# ==================================================================================================
@pytest.mark.asyncio
async def test_a_client_hovering_at_the_limit_is_not_disconnected_on_every_burst(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Thrashing: repeated approaches to the ceiling must not each cost a disconnect.

    The hysteresis gap exists for two reasons and the tests so far covered only the first (a full
    replay must not end in a disconnect at the boundary). The second is this: a client that keeps
    oscillating around the limit — the ordinary shape of a mobile link — would, with the entry point
    and the threshold coinciding, be dropped on every burst rather than once.

    The scene drives several bursts through the live phase with a client slow enough that each burst
    pushes the queue back up towards the ceiling. Correct behaviour is one uninterrupted stream.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")
    bursts, per_burst = 4, _QUEUE_MAX - 2

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, _QUEUE_MAX)
        expected = _QUEUE_MAX + bursts * per_burst

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.02).start()

        async def _bursty_producer() -> None:
            # Each burst is nearly a full queue, arriving faster than the client drains, so the
            # depth repeatedly climbs back towards the limit and falls away again.
            for _ in range(bursts):
                await asyncio.sleep(0.35)
                for i in range(per_burst):
                    await bus.publish(run_id, epoch=_EPOCH, raw=f"data: b-{i}\n\n".encode())

        producer = asyncio.create_task(_bursty_producer())
        for _ in range(2000):
            if len(reading.blocks) >= expected:
                break
            await asyncio.sleep(0.02)
        await asyncio.gather(producer, return_exceptions=True)
        received = len(reading.blocks)
        dropped = reading.task is not None and reading.task.done()
        await reading.stop()

    assert not dropped or received >= expected, (
        f"the client was disconnected after {received} of {expected} blocks "
        f"while oscillating around "
        "the limit — the depth guard is firing on every burst instead of on a client that is "
        "genuinely behind, which is the thrashing the hysteresis gap exists to remove."
    )
    assert received >= expected, f"only {received} of {expected} blocks arrived"


@pytest.mark.asyncio
async def test_the_periodic_branch_announces_a_generation_change_with_no_event_at_all(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """C4 in the branch that owns it: a QUIET run whose generation changes under an open stream.

    The event branch cannot cover this — it fires ON an event, and here there is none. For a quiet
    run the periodic re-read is the ONLY path that notices, so the marker is due from it:
    without one
    the stream would resume under a new numbering, repeating ``id:`` values the client has already
    seen, with no sign of a discontinuity.

    Asserted with NO event published after the change, which is what distinguishes this from the
    event-path test in the arming module.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        first = await bus.ensure_epoch(run_id)
        assert first is not None
        await _publish_burst(bus, run_id, 3)

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
        for _ in range(200):
            if len(reading.blocks) >= 3:
                break
            await asyncio.sleep(0.05)
        assert len(reading.blocks) == 3, "the replay did not arrive; the session has no generation"

        # The generation changes and NOTHING is published. Only the periodic re-read can see it.
        await bus._redis.set(f"agent:run:{run_id}:epoch", "gen-brandnew", ex=60)

        for _ in range(400):
            if len(reading.blocks) >= 4:
                break
            await asyncio.sleep(0.05)
        blocks = list(reading.blocks)
        await reading.stop()

    assert len(blocks) >= 4, (
        "no marker arrived for a generation change on a quiet run. The event branch cannot fire — "
        "there is no event — so without the periodic branch announcing it the client silently "
        "continues under a numbering that restarted."
    )
    assert _is_marker(blocks[3]), f"the 4th block is not run.truncated: {_shape(blocks)}"


@pytest.mark.asyncio
async def test_an_unparseable_channel_message_is_logged_and_announced_on_the_next_event(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    broker_factory: Any,
) -> None:
    """Both levels for one failure, and neither substitutes for the other (§3.2.1).

    At the moment of detection we know a message existed but NOT its ``seq`` — it did not parse — so
    an exact marker is impossible and a ``warning`` is all that is available. The CLIENT learns from
    the next event, whose number is above ``delivered + 1``: that is C2, and without C2 this failure
    would have no client notification at all, which would make the definition of axis C false.

    ⚠️ The log assertion is a SECURITY one as much as an observability one: the payload is user
    content (§3.5) and must never appear in a log line. Asserted positively (the run id is
    there) and
    negatively (the body is not).
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")
    secret = "USER-CONTENT-THAT-MUST-NOT-BE-LOGGED"

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 2)

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
        for _ in range(200):
            if len(reading.blocks) >= 2:
                break
            await asyncio.sleep(0.05)

        channel = AgentRunEventBus.channel(run_id)
        # ⚠️ THE LOGGER MUST BE RE-ENABLED FIRST, and the reason is worth knowing (Q-067-21).
        # ``migrations/env.py`` calls ``fileConfig(...)``, whose default is
        # ``disable_existing_loggers=True``, and ``tests/conftest.py`` runs alembic IN-PROCESS at
        # session setup — so every app logger imported before that point comes out with
        # ``disabled = True``. A disabled logger short-circuits inside ``Logger.handle``, so NO
        # handler anywhere sees the record: not ``caplog``, not one attached to this logger, not one
        # on the root. That is why two earlier attempts captured nothing while the code path was
        # demonstrably running, and it applies to every in-process log assertion in this suite.
        # Production is unaffected: nothing runs migrations in the API process.
        broker_logger = logging.getLogger("app.agent_proxy.broker")
        captured: list[str] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = _Collect(level=logging.NOTSET)
        was_disabled = broker_logger.disabled
        previous_level = broker_logger.level
        broker_logger.disabled = False
        broker_logger.setLevel(logging.WARNING)
        broker_logger.addHandler(handler)
        try:
            # A message that cannot be parsed — its seq is unknowable, so no marker can name it.
            await bus._redis.publish(channel, f"{{not json at all {secret}")
            await asyncio.sleep(0.4)
            # The next real event skips a number, standing for the one we could not read.
            await bus._redis.publish(
                channel, json.dumps({"epoch": _EPOCH, "seq": 4, "data": "data: after-gap\n\n"})
            )
            for _ in range(200):
                if len(reading.blocks) >= 4:
                    break
                await asyncio.sleep(0.05)
        finally:
            broker_logger.removeHandler(handler)
            broker_logger.setLevel(previous_level)
            broker_logger.disabled = was_disabled
        blocks = list(reading.blocks)
        await reading.stop()

    assert len(blocks) >= 4, f"the event after the gap never arrived: {_shape(blocks)}"
    assert _is_marker(blocks[2]), (
        f"the forward gap left by the unparseable message was not announced: {_shape(blocks)}. "
        "Then this failure has no client notification at all and axis C is not what it claims."
    )
    assert blocks[3].endswith(b"data: after-gap\n\n")

    # The payload must never reach the client either — the marker carries no user content.
    assert secret.encode() not in blocks[2], "user content leaked into the truncation marker"

    # The OPERATOR half: a trace exists, it names the run, and it does NOT carry the payload.
    unparseable = [m for m in captured if "unparseable" in m]
    assert unparseable, f"the unparseable message left no operator trace at all; got {captured}"
    for message in unparseable:
        assert run_id in message, f"the warning does not say which run: {message!r}"
        assert secret not in message, f"USER CONTENT LEAKED INTO THE LOG: {message!r}"


@pytest.mark.asyncio
async def test_a_supervisor_exception_outside_a_drain_closes_the_stream(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """P3 on the supervisor path, in the case that is NOT exempt.

    The exemption covers a supervisor that fails during a normal drain (there the drain must reach
    the sentinel and is bounded by its own budget). Outside a drain the general rule applies: a link
    that dies of an exception must close the client stream, or the session is left with a missing
    safety net and no one to notice.

    Distinct from the drain test next door precisely by the absence of a normal end: no
    terminal event
    in the ring, the run stays running, so ``drain_deadline`` is never armed.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 3)

        calls = {"n": 0}
        inner = broker._is_terminal

        async def _failing_terminal(rid: str) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                return await inner(rid)
            raise RuntimeError("supervisor probe exploded")

        broker._is_terminal = _failing_terminal  # type: ignore[method-assign]

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
        ended = await reading.wait_until_ended(budget=30.0)
        await reading.stop()

    assert calls["n"] >= 2, "the supervisor never reached its probe"
    assert ended, (
        "the supervisor died of an exception outside any drain and the stream stayed open. P3: a "
        "link that fails must close the session — otherwise the safety net is "
        "gone and nothing says "
        "so, which on this construction is no longer prevented by the structure."
    )


@pytest.mark.asyncio
async def test_a_blocked_supervisor_probe_does_not_stop_delivery(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Mode D2: the periodic probes sit OFF the delivery path.

    The probes touch Postgres and Redis, and neither has a statement timeout in this deployment —
    the exact shape of TD-040, an unbounded await on a network call. §3.2.2 puts them in the
    supervisor for that reason: there a block delays only the safety net, while in the reader it
    would stop the channel read AND the queue, leaving three live tasks and a silent stream.

    ⚠️ The scene must let the supervisor actually REACH a probe. While it stands down it never calls
    one, so a steady hand-over keeps it away from the code under test — an earlier version used a
    terminal run with 31 blocks and never reached a single probe (``calls == 1``). Here the client
    drains everything, the queue empties, the stand-down lifts, and the probe is entered and
    blocked. Delivery of NEW events is then asserted while it is demonstrably stuck.
    """
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    await _seed_run(db_sessionmaker, run_id, status="running")

    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)
        await _hold_lease(bus, run_id)
        await _publish_burst(bus, run_id, 3)

        blocked = asyncio.Event()
        calls = {"n": 0}
        inner = broker._is_terminal

        async def _blocking_terminal(rid: str) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                # The reader's own check at open must still work, or nothing gets going.
                return await inner(rid)
            blocked.set()
            await asyncio.sleep(30)
            return await inner(rid)

        broker._is_terminal = _blocking_terminal  # type: ignore[method-assign]

        reading = Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
        await asyncio.wait_for(blocked.wait(), timeout=25.0)
        before = len(reading.blocks)

        for i in range(4):
            await bus.publish(run_id, epoch=_EPOCH, raw=f"data: during-{i}\n\n".encode())
        for _ in range(400):
            if len(reading.blocks) >= before + 4:
                break
            await asyncio.sleep(0.05)
        during = len(reading.blocks)
        await reading.stop()

    assert calls["n"] >= 2, "the supervisor never reached its probe; the block was not exercised"
    assert during >= before + 4, (
        f"only {during - before} of 4 events arrived while a supervisor probe was stuck. Delivery "
        "must not depend on the probes at all — that dependency is mode D2, and it produces a "
        "silent stream with every task formally alive."
    )


# ==================================================================================================
# THE POOL: open streams must not accumulate database connections.
#
# The defect this pins was found in final review and is the worst kind in this contour, because the
# blast radius is not the feature: the broker held the REQUEST-SCOPED session for the whole life
# of an SSE stream — up to two hours. About fifteen concurrent `/events` exhausted a worker's pool,
# and from then on EVERY endpoint of that worker waited out `DB_POOL_TIMEOUT` and failed. A second
# cost is quieter: an open transaction holds ACCESS SHARE on `agent_runs` for those two hours, so
# a routine `ALTER TABLE` queues on ACCESS EXCLUSIVE and blocks every reader queued behind it.
# ==================================================================================================
@dataclass
class _SessionLifetimes:
    """How long each repository session stays OPEN. The discriminator is duration, not count.

    ⚠️ A count of concurrently-open sessions is NOT enough here, and the first version of this test
    failed on exactly that: six streams started together tick together, so their probes fire in the
    same instant and six short-lived sessions are legitimately open at once. "Six held for the
    life of the stream" and "six opened for a millisecond each" are indistinguishable by peak.

    Duration separates them by construction: a probe session lives milliseconds, a request-scoped
    one lives as long as the SSE response. So the assertion is on the LONGEST session observed.

    ⚠️ Sessions rather than ``pool.checkedout()``: the test engine uses ``NullPool``, which keeps
    no checkout count at all. With ``NullPool`` each session opens its own real connection, so a
    session held open IS a connection held open — the quantity the defect was about.
    """

    durations: list[float] = field(default_factory=list)
    opened: int = 0

    @asynccontextmanager
    async def track(self) -> AsyncIterator[None]:
        self.opened += 1
        started = time.monotonic()
        try:
            yield
        finally:
            self.durations.append(time.monotonic() - started)

    @property
    def longest(self) -> float:
        return max(self.durations, default=0.0)


@pytest.mark.asyncio
async def test_open_streams_do_not_accumulate_pool_connections(
    db_sessionmaker: async_sessionmaker[AsyncSession], broker_factory: Any
) -> None:
    """Six concurrent streams must not hold six connections. Asserted on the PEAK, not a total.

    The ``runs`` factory here is the PRODUCTION shape — a fresh session per probe, opened and closed
    around it — which is what makes the measurement meaningful: with a repository bound to one
    long-lived session the count would be pinned at one per stream and the test would be measuring
    the fixture instead of the broker.

    Streams are left open and idle (a live lease, no terminal event, nothing to deliver), which is
    precisely the state a real client sits in between events and the state in which the
    old code held
    its connection.
    """
    streams = 6
    run_ids = [f"run_{uuid.uuid4().hex[:8]}" for _ in range(streams)]
    for run_id in run_ids:
        await _seed_run(db_sessionmaker, run_id, status="running")

    sessions = _SessionLifetimes()
    readings: list[Reading] = []
    async with db_sessionmaker() as session:
        broker, bus, _settings = broker_factory(session)

        # Override the fixture's factory with the production one: a real session per probe.
        @asynccontextmanager
        async def _short_lived() -> AsyncIterator[AgentRunsRepository]:
            async with sessions.track(), db_sessionmaker() as fresh:
                yield AgentRunsRepository(fresh)

        broker._runs = _short_lived  # type: ignore[assignment]

        try:
            for run_id in run_ids:
                await _hold_lease(bus, run_id)
                await _publish_burst(bus, run_id, 2)
                readings.append(
                    Reading(broker.stream(run_id=run_id, cursor=Cursor()), delay=0.0).start()
                )

            # Let every stream open, drain its two blocks and settle into the idle state.
            for _ in range(200):
                if all(len(r.blocks) >= 2 for r in readings):
                    break
                await asyncio.sleep(0.05)
            delivered = [len(r.blocks) for r in readings]
            assert all(n >= 2 for n in delivered), f"streams did not all deliver: {delivered}"

            # Idle across more than one supervisor tick, so every stream runs probes DURING the
            # window and a session held open by any of them is recorded in the peak.
            await asyncio.sleep(_SUPERVISOR_TICK * 2 + 0.5)
        finally:
            for reading in readings:
                await reading.stop()

    assert sessions.opened >= streams, (
        f"only {sessions.opened} sessions were ever opened for {streams} streams — the broker is "
        "not using the factory, so this measures nothing"
    )
    # THE assertion. The streams stayed idle for several seconds; a session that lived anywhere
    # near that long was held for the response rather than for a probe.
    assert sessions.longest < 1.0, (
        f"the longest repository session stayed open {sessions.longest:.1f}s "
        f"while the streams were "
        f"idle for ~{_SUPERVISOR_TICK * 2 + 0.5:.1f}s. A session that lives as long as the SSE "
        "response means ~15 concurrent streams exhaust the worker's pool, after "
        "which EVERY endpoint "
        "on that worker fails — and an ACCESS SHARE lock on agent_runs is held for the whole two "
        "hours a stream may last."
    )
