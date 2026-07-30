"""Downstream fan-out of the client ``GET /v1/agent/runs/{runId}/events`` (ADR-067 §3.2-§3.4).

Under the broker model the client stream NEVER touches Hermes: the background consumer is the only
upstream subscriber, and this module replays the Redis ring and follows the pub/sub channel. It is
purely reading — no billing, no snapshot upsert, no status write. That is what dissolves the
ADR-066 objection about double-debiting a run with a live client relay: the client path does not
bill at all.

What it must get right, each item paid for by a review round and one of them by a reproduction on a
perfectly healthy run:

**The client's cursor is a CLAIM and may never be the dedup baseline (§3.2.1, TD-046).** Dedup
compares two numbers — the arriving ``seq`` and a baseline — so it is correct only while the
baseline means "what this session actually delivered" and both numbers belong to one sequence. Of
the three possible baselines (client cursor, replay tail, delivered live event) only the cursor
proves nothing: it is unbounded above and, as ``?afterSeq=``, carries no generation. Seeding from it
needed ONE request — ``?afterSeq=500`` against a ring holding ``1..3`` — to produce a permanently
open, EMPTY stream on an undamaged deployment: the replay filtered to nothing, every live event was
suppressed as "already sent" though none had been, and no close rule fires while the lease is
alive. Hence:
``delivered`` starts at zero and grows only from delivery; the cursor is a replay filter, bounded by
the real counter; and a filter we cannot verify against a generation is always announced.

**Generation checks are not only for reconnects.** ``epoch`` is a property of the READING SESSION,
re-checked on every event, whenever the pub/sub subscription is re-established and periodically —
not just when the stream opens. The failure it prevents (§3.3.1): Redis restarts, the consumer
survives and republishes from ``seq`` 1 under a NEW generation, while this stream still remembers
"last delivered = 500" and silently drops everything that follows. None of the close rules fire —
the lease is alive — so the client keeps an OPEN, SILENT stream for the rest of the run, which is
worse than the behaviour the cursor replaced.

**A session that opens WITHOUT a generation must still arm that check (§3.3.1a, TD-044).** Reading
the epoch key can come back empty — the key is gone, or Redis did not answer, which
``transport.current_epoch`` deliberately does not distinguish because the response to both is the
same. Making the check conditional on that one read succeeding meant such a session ran with §3.3.1
disabled for its entire life, i.e. the silent stream above with a rarer entrance — and §4.1 (drive
the run with no lease when Redis is down) turned that entrance from exotic into routine. So the
generation is established in three descending ways: the key, else the epoch of the last ring
element delivered, else ADOPTION of the first generation seen — from an event or from the periodic
check, because a quiet run may emit nothing for hours. Adoption announces ``run.truncated`` when the
client arrived HOLDING a cursor (C5): it believes it has a prefix and we are handing it the tail of
a generation we could not check it against, and a readable key would have told it so (§3.2
step 2) — adoption must not be softer than the ordinary path, or the client's contract comes to
depend on whether Redis answered. ⚠️ An empty cursor buys no silence: C3 applies independently, so a
first delivery starting above the beginning is announced whether a cursor was sent or not — the ring
reads as ``[]`` both when it is empty and when it could not be READ, and "the client received no
prefix" is not the same statement as "no prefix exists".

**A sequence can also break WITHOUT its name changing (§3.3.1b, TD-045).** The ring keys expire on a
TTL refreshed only by events, while the lease is renewed on its own clock, so a quiet run can lose
``seq`` and keep its lease: the counter restarts at 1 under the SAME epoch, generations match, and
the event is dropped as stale. ``INCR`` makes a live sequence monotonic, so a regression is evidence
of a reset rather than a duplicate — which is why this module keeps TWO counters with different
provenance (``delivered`` from delivery, ``arrived`` from channel order) and cannot merge them.

**Closing is now our job.** Before this ADR the stream ended because Hermes ended it. A terminal
event may never appear in Redis at all (the consumer never started, died, the ring TTL expired,
Redis was flushed), so independent rules close the stream (§3.3) — five about the run plus rule 6,
"the reader has finished"; without them the client hangs forever exactly where it used to get a
clean ``200``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final

from redis.exceptions import RedisError

from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.transport import AgentRunEventBus, RingEvent
from app.config import Settings
from app.errors import BadRequestError
from app.observability.metrics import agent_run_event_streams_active

logger = logging.getLogger("app.agent_proxy.broker")

# How the broker reaches ``agent_runs``: a factory of SHORT-LIVED repositories, one per probe. Never
# a repository bound to the request session — see :class:`AgentRunBroker` for what that costs.
RunsFactory = Callable[[], AbstractAsyncContextManager[AgentRunsRepository]]

# Synthetic marker telling the client its replay does NOT start at the beginning of the run
# (ADR-067 §3.4). Mandatory, not advisory: a client that resets its accumulated text on reconnect
# would otherwise replace a complete transcript with a knowingly partial one and be unable to tell.
EVENT_RUN_TRUNCATED: Final = "run.truncated"

# agent_runs statuses that mean the run is over (ADR-067 §3.3 rules 2 and 4).
_TERMINAL_STATUSES: Final = frozenset({"completed", "failed", "cancelled", "paused"})

# Terminal EVENT names on the wire. Duplicated from ``agent_proxy.service`` on purpose: that module
# imports this one, so importing it back would be circular. Kept to the three names, nothing else.
_TERMINAL_EVENTS: Final = frozenset({"run.completed", "run.failed", "run.paused"})


class _EndOfStream:
    """In-band marker for the NORMAL end of the reader's work (ADR-067 §3.2.2).

    It travels through the queue so that it arrives AFTER everything already queued — that ordering
    is the whole point, because the last thing queued is usually the terminal event. It is admitted
    into the slot reserved by :meth:`AgentRunBroker._put_due_replay`/``_offer_live``, which ordinary
    blocks never reach.

    ⚠️ The sentinel is NOT the signal the writer decides on: a queue cannot be peeked, so a writer
    that saw only the abnormal ``Event`` could never tell whether a sentinel was still queued behind
    ordinary blocks. The decision is made on the FLAG set before this is placed; the sentinel then
    marks where to stop draining.
    """


_END_OF_STREAM: Final = _EndOfStream()

_QueueItem = bytes | _EndOfStream

# OUR OWN deadline on each periodic probe the reader makes (run status in Postgres, lease in Redis).
#
# ⚠️ It is deliberately not delegated to the server: `DB_POOL_TIMEOUT` bounds only the wait for a
# connection FROM THE POOL, while `statement_timeout`/`lock_timeout` are set nowhere in this
# deployment (Q-067-20, owner devops) — so a query already holding a connection and waiting on a
# lock has no bound at all today. That is the shape of TD-040 on the launch path: an `await` with no
# deadline, a symptom with no error code, and months of wrong attribution. A timeout turns the
# unbounded block into an error, and the error closes the stream, which is the whole point.
_PERIODIC_PROBE_TIMEOUT_SECONDS: Final = 5.0

# OUR OWN deadline on the reader's open phase (SUBSCRIBE → cursor → LRANGE → the two open-time close
# rules). Same reasoning as above, one phase instead of a loop: it is all Redis and Postgres calls
# with no server-side bound, and a block here produces a stream that never says anything.
_OPEN_PHASE_TIMEOUT_SECONDS: Final = 15.0

# How often the reader re-checks for room in the delivery queue while the client is at its ceiling.
# Short enough not to add noticeable latency to a client only momentarily behind, long enough not to
# spin: this is a wait for a slow network, not a fast path.
_QUEUE_ROOM_POLL_SECONDS: Final = 0.01


@dataclass(frozen=True)
class Cursor:
    """Where a reconnecting client left off. ``seq == 0`` is the EMPTY cursor ("from the start").

    ``epoch`` is None for a cursor that carries no generation — either absent entirely or supplied
    as ``?afterSeq=``, which by contract is only meaningful within the CURRENT generation.
    """

    seq: int = 0
    epoch: str | None = None

    @property
    def empty(self) -> bool:
        return self.seq <= 0


@dataclass
class _Activity:
    """What the pipeline tells the supervisor about the session. One writer per field, no locks.

    Mutable on purpose and safe by construction: a single-threaded event loop, one task writing each
    field, one reading, and both values only ever move forward.
    """

    # Last time the READER queued an event for the client — the input of close rule 5 (idle).
    last_event_at: float
    # ⛔ Last time the WRITER actually handed a portion to the client. The supervisor withholds its
    # verdicts while this is fresher than ``AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS``: a hand-over in
    # progress may not be aborted, whatever anyone knows about the run's outcome.
    #
    # ⚠️ It replaced a condition phrased as "the reader already knows the ending", and the reason is
    # a measured entrance that no enumeration of reader knowledge can cover: a run that is NOT
    # terminal at open, with no terminal event in the ring, whose status is written to Postgres by
    # ANOTHER PROCESS mid-delivery. The reader cannot know, so it cannot say — and the supervisor
    # abandoned the queue with 21 of 41 blocks still owed. Progress is observable without knowing
    # anything about the outcome, which is why it is the right predicate.
    #
    # ⚠️ And it must be a FRESHNESS test, not a latch: an unconditional stand-down disables rules 4
    # and 5 for the rest of the session, and with rule 4 goes the only way ``MAX_DURATION`` becomes
    # visible to a client. "The reader's awaits are all bounded" does not rescue that — bounded
    # awaits give non-blocking, not termination.
    last_delivered_at: float


@dataclass(frozen=True)
class _CursorVerdict:
    """What a client cursor is allowed to do in this session (ADR-067 §3.2.1, TD-046).

    Two fields because the cursor has exactly one legitimate power and one obligation:

    * ``filter_seq`` — how much of the ring we may SKIP because the client says it has it. A filter,
      nothing more. ⛔ It is deliberately NOT called ``seq`` and never seeds the dedup baseline: a
      claim cannot prove delivery, and seeding from it is precisely how one ``?afterSeq=`` request
      produced a permanently open, empty stream on a healthy run.
    * ``announce_gap`` — whether the client must be told its prefix cannot be trusted, i.e. whether
      we are serving it a stream we could not line up against its cursor.
    """

    filter_seq: int
    announce_gap: bool


@dataclass(frozen=True)
class _Replay:
    """What the replay phase actually delivered — the provenance of the dedup baseline (source S2).

    ``max_seq`` is the ONLY value allowed to seed live dedup; every field is 0 when nothing was
    delivered, so a replay filtered down to nothing cannot pretend otherwise.

    ⚠️ ``min_seq``/``max_seq`` are a MINIMUM and a MAXIMUM, not "first and last element". They
    coincide only because the ring's element order matches its ``seq`` order — a guarantee living in
    the write path (``INCR`` and ``RPUSH`` inside one atomic Lua script, §3.1). Naming the
    dependency here is the point: an "optimisation" moving the ring write out of Lua would break
    the baseline silently, and this is the line that says so.

    ``min_seq`` exists because a scalar baseline is an upper MARK and not the set of delivered seqs
    (§3.2.1): an event can arrive below the mark having never been delivered — published while we
    were already subscribed, then pushed out of the ring by head trimming before our ``LRANGE`` read
    it. Below ``min_seq`` is therefore a HOLE, not a proven duplicate.
    """

    blocks: list[bytes]
    first_seq: int
    min_seq: int
    max_seq: int


def parse_cursor(*, last_event_id: str | None, after_seq: str | None) -> Cursor:
    """Resolve the client's cursor from the two sources (ADR-067 §3.2 step 2).

    ``Last-Event-ID`` WINS over ``?afterSeq=`` when both are present — one rule, with no attempt to
    merge two sources. The asymmetry in how they fail is deliberate and not an oversight:

    * an invalid ``?afterSeq=`` is a 400 — the client typed it, so it can fix it;
    * an invalid ``Last-Event-ID`` degrades to the EMPTY cursor — that header is set automatically
      by any standard SSE library, and failing a reconnect over a value the application never chose
      would strand the client with no way to recover.
    """
    if last_event_id:
        epoch, _, raw_seq = last_event_id.rpartition("-")
        if epoch and raw_seq.isdigit():
            return Cursor(seq=int(raw_seq), epoch=epoch)
        # Malformed header → empty cursor (full replay), never an error. See the docstring.
        return Cursor()
    if after_seq is not None:
        if not after_seq.isdigit():
            # isdigit() also rejects "-1" and "1.5": the contract is int >= 0.
            raise BadRequestError("afterSeq must be a non-negative integer")
        return Cursor(seq=int(after_seq))
    return Cursor()


def sse_block(*, epoch: str, seq: int, data: bytes) -> bytes:
    """One SSE block carrying the self-identifying cursor ``id: <epoch>-<seq>`` (ADR-067 §3.1).

    The id is prepended to the event's own bytes, which are relayed verbatim. Additive by design: a
    client dispatching on the JSON ``event`` field ignores it, a standard SSE library stores it and
    returns it as ``Last-Event-ID`` on reconnect — which is what makes reconnects incremental
    without any client change.
    """
    return b"id: " + f"{epoch}-{seq}".encode() + b"\n" + data


def truncation_marker(*, epoch: str, seq: int, run_id: str, from_seq: int) -> bytes:
    """The synthetic ``run.truncated`` block (ADR-067 §3.4)."""
    payload = json.dumps(
        {
            "event": EVENT_RUN_TRUNCATED,
            "run_id": run_id,
            "from_seq": from_seq,
            "reason": "buffer_trimmed",
        }
    )
    return sse_block(epoch=epoch, seq=seq, data=f"data: {payload}\n\n".encode())


class AgentRunBroker:
    """Serves one client stream from the Redis ring + channel. Never writes anything.

    ⛔ ``runs`` is a FACTORY of short-lived repositories, not a repository bound to the request
    session, and the difference is a worker-wide outage rather than a style preference. A client
    stream lives up to ``AGENT_RUN_MAX_DURATION_SECONDS`` (2 h), and its ``StreamingResponse``
    postpones the request-session teardown until the stream closes. A repository on that session
    would mean: the supervisor's status probe every 30 s opens a transaction on it, nothing ever
    commits (the broker writes nothing), and one pooled connection sits ``idle in transaction`` for
    the whole run. With ``DB_POOL_SIZE + DB_MAX_OVERFLOW`` = 15 per worker, ~15 concurrent
    ``/events`` exhaust the pool and EVERY endpoint of that worker then waits out
    ``DB_POOL_TIMEOUT`` and fails — the failure is the worker, not the feature. Second cost: such a
    transaction holds ACCESS SHARE on ``agent_runs`` for two hours, so a routine ``ALTER TABLE``
    queues behind it on ACCESS EXCLUSIVE
    and blocks every reader queued behind THAT.

    This is the same rule the consumer and the orphan sweep already follow — one short session per
    operation (ADR-067 §6.1.1) — which had simply never been extended to this module.
    """

    def __init__(
        self,
        *,
        bus: AgentRunEventBus,
        runs: RunsFactory,
        settings: Settings,
    ) -> None:
        self._bus = bus
        self._runs = runs
        self._settings = settings

    async def stream(self, *, run_id: str, cursor: Cursor) -> AsyncIterator[bytes]:
        """Deliver the session's stream. THREE tasks, mirroring §6.1 (ADR-067 §3.2.2, variant «а»).

        | task           | what it does                                                          |
        |----------------|-----------------------------------------------------------------------|
        | **reader**     | SUBSCRIBE → cursor → LRANGE → §3.2.1 classification → queue           |
        | **writer**     | this coroutine: ``get`` from the queue → hand to the client         |
        | **supervisor** | periodic close rules 4/5 and the idle timeout                         |

        Why the replay belongs to the READER and not here: inside one task ``SUBSCRIBE`` and
        ``LRANGE`` are consecutive statements, so their order holds BY CONSTRUCTION rather than by a
        readiness handshake between tasks — and the replay baseline (``delivered`` and
        ``replay_min_seq``) is complete before the first channel message is classified. With it on
        this side the reader would spend the whole ``LRANGE`` classifying against ``delivered = 0``,
        find every channel copy of every ring element "due", and send the client a SECOND copy of
        the entire ring — up to 5000 duplicates.

        Why the periodic probes belong to the SUPERVISOR: they touch Postgres and Redis, and an
        unbounded block in them must not stop delivery. In the reader a block would stop the channel
        read as well, leaving three live tasks and a silent stream (§3.2.2 forbids it explicitly);
        here it only delays the safety net.
        """
        # ⚠️ Capacity, the reserved sentinel slot, the normal drain and the PRIORITY between the two
        # termination paths are still being decided (TD-048) — three defects were found in that
        # mechanism, and each candidate answer produces different code. What is implemented here is
        # deliberately the DATA-PRESERVING side of every one of those questions:
        #   * bounded depth with a BLOCKING ``put`` — memory per client is capped (the real resource
        #     risk) while the disconnect POLICY stays open. No ``QueueFull`` disconnect is made,
        #     because the depth of a queue the replay does not fit into ten times over cannot yet be
        #     read as "the client is behind";
        #   * end of stream = the reader finished AND the queue is empty, so the terminal event can
        #     never be dropped on the way out;
        #   * the supervisor's verdict likewise drains what is already queued before closing.
        # ⛔ The old burst counter is GONE and must not come back: it counted messages between two
        # idle polls of the channel, so it cut off a fast healthy client for one busy second and
        # never disconnected a genuinely slow one (§3.2.2).
        # Capacity is the ceiling PLUS ONE, and the extra slot is reserved by a RULE rather than by
        # the structure: `asyncio.Queue` knows nothing about "for the sentinel only" and would let
        # any `put_nowait` take the last slot, so regular blocks are admitted only while
        # `qsize() < AGENT_RUN_SUBSCRIBER_QUEUE_MAX`. Without that rule the reserve fails in the ONE
        # state it exists for: at overflow the end-of-stream sentinel would find no room, and the
        # reader would either block (D2) or take the abnormal path — discarding the terminal event
        # already sitting in the queue.
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue(
            maxsize=self._settings.agent_run_subscriber_queue_max + 1
        )
        # Two signals, and their ASYMMETRY is the correctness of the whole termination story:
        #   * `normal_end` — the FLAG. Set by the reader BEFORE it places the sentinel, so a writer
        #     that observes it knows a sentinel is on its way even though queues cannot be peeked.
        #     Everything queued must still be delivered, terminal event included.
        #   * `closing` — the abnormal signal, out of band. The queue is abandoned at once.
        # ⛔ The flag is what carries correctness: with only the abnormal Event, the supervisor's
        # rule 4 would abort the drain of every completed run BY CONSTRUCTION — a normal end means
        # the run IS terminal, so rule 4 is true exactly while the drain is running.
        normal_end = asyncio.Event()
        closing = asyncio.Event()
        activity = _Activity(last_event_at=time.monotonic(), last_delivered_at=time.monotonic())
        pubsub = self._bus.subscribe(run_id)
        # ⛔ PLAIN TASKS, NOT an ``asyncio.TaskGroup`` — do not "restore the symmetry with §6.1"
        # here. A group cannot wrap a ``yield``: on an ordinary client disconnect the ASGI layer
        # calls ``aclose()``, which throws ``GeneratorExit`` at the yield; ``GeneratorExit`` is a
        # ``BaseException``, so ``TaskGroup.__aexit__`` catches it, cancels the children and
        # re-raises it WRAPPED in a ``BaseExceptionGroup``. ``aclose()`` then raises instead of
        # returning, and the async-generator finalisation protocol is broken — on every disconnect,
        # i.e. on the most ordinary path there is. The two properties the group provided are
        # re-established below by hand, both named so a later refactor cannot drop one silently:
        #   (1) a child that FAILS cancels the other and closes the stream (D1 (3)) — the delivery
        #       loop waits on both tasks, treats a failed one as an abnormal end, and ``finally``
        #       cancels its sibling;
        #   (2) a child that finishes NORMALLY cancels nothing — the normal end depends on it (the
        #       reader finishes on purpose, and the supervisor may stand down).
        reader = asyncio.create_task(
            self._read_channel(
                run_id=run_id,
                cursor=cursor,
                pubsub=pubsub,
                queue=queue,
                activity=activity,
                normal_end=normal_end,
                closing=closing,
            ),
            name=f"agent-run-events-reader:{run_id}",
        )
        supervisor = asyncio.create_task(
            self._supervise(run_id=run_id, closing=closing, activity=activity, queue=queue),
            name=f"agent-run-events-supervisor:{run_id}",
        )
        closing_waiter = asyncio.ensure_future(closing.wait())
        normal_waiter = asyncio.ensure_future(normal_end.wait())
        # The drain deadline is counted LOCALLY here, and it is a SECOND budget of the same
        # size as the replay's — not a share of one. A shared counter would let a slow
        # replay spend it all and leave the drain nothing, discarding the terminal event
        # by expiry: the priority of the normal path defeated by the very budget meant to
        # bound it. It would also need a deadline shared BETWEEN tasks, i.e. another
        # synchronisation point. The honest cost of two budgets is named in the ADR: up to
        # 2x the value in the worst case (a slow replay, then a slow drain).
        budget = self._settings.agent_run_subscriber_drain_seconds
        drain_deadline: float | None = None
        # The second half of the Q-067-2 precondition: open client streams were counted NOWHERE, so
        # none of the three ceilings they share (DB pool connections, Redis subscriptions, ring
        # memory) could be seen approaching. Incremented here and decremented in the ``finally``
        # below, which runs on every exit including a client disconnect.
        agent_run_event_streams_active.inc()
        # Held OUTSIDE the loop so the teardown can collect it: a getter cancelled on the last
        # iteration has had no chance to process its cancellation, and a ``Queue.get`` coroutine
        # finalised after the loop is gone reports "Event loop is closed" from the GC — an error
        # nobody can act on, in a path that runs on every disconnect.
        getter: asyncio.Task[_QueueItem] | None = None
        try:
            while True:
                if normal_end.is_set() and drain_deadline is None:
                    drain_deadline = time.monotonic() + budget
                if drain_deadline is None and (
                    closing.is_set()
                    or reader.done()
                    or _task_failed(supervisor, run_id=run_id, role="supervisor")
                ):
                    # ABNORMAL, and checked BEFORE the queue: what is still queued is abandoned
                    # deliberately. Draining it anyway would blur the two paths into one and
                    # lose the only reason for having a flag — on this path the prefix is intact,
                    # the rest is still in the ring, and the client's reconnect replays it. Also
                    # rule 6 (§3.3): a reader that ended without signalling a normal end must
                    # not leave the stream open and silent.
                    _task_failed(reader, run_id=run_id, role="reader")
                    logger.info(
                        "agent run events closing run_id=%s reader_done=%s abnormal=%s",
                        run_id,
                        reader.done(),
                        closing.is_set(),
                    )
                    return
                if not queue.empty():
                    item = queue.get_nowait()
                    if isinstance(item, _EndOfStream):
                        logger.info("agent run events closing: end of stream run_id=%s", run_id)
                        return
                    yield item
                    # Progress, and the supervisor reads it: while portions keep going out, no
                    # verdict of rules 4/5 may cut the hand-over short.
                    activity.last_delivered_at = time.monotonic()
                    if drain_deadline is not None:
                        # Progress resets the drain clock, for the same reason it resets the
                        # reader's: the budget bounds how long we WAIT for a client, not how
                        # long a client may legitimately take to receive what it is owed.
                        drain_deadline = time.monotonic() + budget
                    continue
                # Draining a normal end: the sentinel is already queued behind whatever is
                # left, so the ONLY thing that may end this early is the clock. The reader
                # has finished, so nothing refills the queue and `QueueFull` — the bound
                # protecting every other await here — cannot occur.
                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    logger.warning("agent run events drain budget exhausted run_id=%s", run_id)
                    return
                getter = asyncio.ensure_future(queue.get())
                await asyncio.wait(
                    {getter, reader, supervisor, closing_waiter, normal_waiter},
                    timeout=_QUEUE_ROOM_POLL_SECONDS if drain_deadline else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if getter.done() and not getter.cancelled():
                    item = getter.result()
                    if isinstance(item, _EndOfStream):
                        logger.info("agent run events closing: end of stream run_id=%s", run_id)
                        return
                    yield item
                    activity.last_delivered_at = time.monotonic()
                    continue
                getter.cancel()
                # Round the loop: the top re-reads both signals and drains whatever is
                # queued. A cancelled ``get`` cannot swallow an item — ``asyncio.Queue``
                # removes it only in ``get_nowait`` — and cancellation of a getter happens
                # only here, never on the normal path, which disarms that known trap.
        finally:
            # D1 requirement 2: cancel on EVERY exit — the client disconnecting (GeneratorExit at a
            # yield above) included. Awaited with ``return_exceptions=True`` so this returns rather
            # than raising: it retrieves every result, leaves no task behind for the event loop to
            # complain about at GC time, and guarantees the reader is finished before the
            # subscription under it is closed. Bounded because nothing in this pipeline shields
            # itself or swallows ``CancelledError`` — the invariant of D1 (2), enforced by a
            # static guard test rather than by hope.
            closing_waiter.cancel()
            normal_waiter.cancel()
            reader.cancel()
            supervisor.cancel()
            pending: list[asyncio.Future[Any]] = [
                reader,
                supervisor,
                closing_waiter,
                normal_waiter,
            ]
            if getter is not None:
                getter.cancel()
                pending.append(getter)
            await asyncio.gather(*pending, return_exceptions=True)
            await _close_pubsub(pubsub)
            agent_run_event_streams_active.dec()

    async def _read_channel(
        self,
        *,
        run_id: str,
        cursor: Cursor,
        pubsub: object,
        queue: asyncio.Queue[_QueueItem],
        activity: _Activity,
        normal_end: asyncio.Event,
        closing: asyncio.Event,
    ) -> None:
        """Own the whole read side and signal WHICH of the two ends happened (ADR-067 §3.2.2)."""
        ended_normally = await self._read_until_end(
            run_id=run_id,
            cursor=cursor,
            pubsub=pubsub,
            queue=queue,
            activity=activity,
        )
        if ended_normally:
            # ⛔ ORDER: the flag first, the sentinel second — and keep it, though NOT for the reason
            # first written here. The old argument ("a writer waking in between decides with no
            # observable sign") was withdrawn by the ADR, and is wrong twice over: there is no
            # ``await`` between these two statements, so no writer can run in between at all, and
            # even if one could, it decides on the FLAG — while that is unset it simply keeps
            # draining and waiting, which is what it would do anyway.
            #
            # The real value of this order is defence in depth for the reserved slot. If the
            # reserve were ever broken, this order degrades the failure from "the terminal event
            # is thrown away with the queue" into "the stream closes later than it could": the
            # flag is set, so the writer drains and only its own deadline ends it. Reversed, a
            # lost sentinel would leave the writer with no flag either — the abnormal path,
            # abandoning what is already owed.
            normal_end.set()
            # Always fits: ordinary blocks are admitted only below the ceiling, so the last slot of
            # the ``ceiling + 1`` capacity is free by rule.
            queue.put_nowait(_END_OF_STREAM)
        else:
            closing.set()

    async def _read_until_end(
        self,
        *,
        run_id: str,
        cursor: Cursor,
        pubsub: object,
        queue: asyncio.Queue[_QueueItem],
        activity: _Activity,
    ) -> bool:
        """SUBSCRIBE → replay → classify. True = a NORMAL end (ADR-067 §3.2 steps 1-6).

        Normal means: the run's terminal event has been queued, or the channel ended. All else —
        an unreadable Redis, a deadline, a client too far behind — is abnormal, and the difference
        decides whether the writer hands over what is queued or abandons it.

        ⛔ Between the ``SUBSCRIBE`` and the end of the replay this task must NOT call
        ``get_message`` — not once. That is the mechanism variant «а» rests on, not a matter of
        being quick: incoming messages pile up in the connection buffer with nobody to classify
        them, so the replay baseline is complete before the first of them is looked at. "Optimising"
        this by draining the channel during the ``LRANGE`` silently restores classification against
        ``delivered = 0`` and re-sends the client the whole ring.
        """
        try:
            async with asyncio.timeout(_OPEN_PHASE_TIMEOUT_SECONDS):
                # §3.2 step 1 / TD-047: the REAL ``SUBSCRIBE``, and before the ``LRANGE`` — an event
                # published in between would otherwise reach nobody: it is already in the ring we
                # have finished reading, and the next event, having a higher seq, sails through the
                # dedup rule so nothing reports the hole. Lazy subscription is forbidden here (the
                # actual command must be issued, not deferred to a first poll); the ordering itself
                # is now guaranteed by these being consecutive statements of one task.
                await pubsub.ensure_subscribed()  # type: ignore[attr-defined]
                epoch = await self._bus.current_epoch(run_id)
                verdict = await self._cursor_verdict(run_id=run_id, cursor=cursor, epoch=epoch)
                ring = await self._bus.replay(run_id)
                # Rule 2 (§3.3): the run is ALREADY terminal at open → serve what the ring holds and
                # end. The client takes the rest from /state.
                terminal_at_open = await self._is_terminal(run_id)
                # Rule 3: nobody drives the run AND there is nothing to replay → end at once. For
                # the client this is the old "200 with no events". ``lease_alive`` is tri-state:
                # ``None`` means Redis did not answer, and reading that as "no lease" would close
                # live streams during a blip, so only an explicit ``False`` counts.
                lease = await self._bus.lease_alive(run_id)
        except RedisError:
            logger.warning("agent run events could not open its subscription run_id=%s", run_id)
            return False
        except TimeoutError:
            # Mode D2: the open phase talks to Redis and Postgres, and neither has a server-side
            # statement bound in this deployment (Q-067-20). A deadline of our own turns an
            # unbounded block into an ending stream instead of a silent one.
            logger.warning("agent run events open phase timed out run_id=%s", run_id)
            return False

        if not ring and lease is False and not terminal_at_open:
            logger.info("agent run events closing: no lease and empty ring run_id=%s", run_id)
            # Nothing is owed and nothing is wrong: a NORMAL end, so the writer closes after handing
            # over the (empty) queue rather than treating this as a failure.
            return True

        # §3.3.1a step 1: the epoch key could not be read, so fall back to the generation of the
        # LAST RING ELEMENT — the ring carries its epoch in every element (§3.1), the same fact from
        # another source, and it is the generation the replay tail (the dedup baseline) belongs to.
        # Adopting a later live event's generation instead would silently place already-delivered
        # elements of one generation under the name of another, and the stream would go quiet on the
        # first colliding seq — one entrance closed, a second opened.
        generation = epoch or (ring[-1].epoch if ring else "")
        replay = self._replay_blocks(
            run_id=run_id,
            ring=ring,
            filter_seq=verdict.filter_seq,
            generation=generation,
            gap_announced=verdict.announce_gap,
        )
        # §3.2.1 C6 / TD-046: the marker is born on the COMMON path, not inside the replay. Inside,
        # it could only exist when there was something to replay — and the worst case is the
        # opposite: a cursor above the ring filters the replay to nothing, and the client got
        # neither events nor any indication why. Deferred only when the generation is not known yet
        # (an unarmed session, §3.3.1a): the marker needs an epoch for its own id, and arming
        # supplies it within one event or one periodic tick.
        #
        # ⚠️ The marker carries ``seq = 0``, NOT the cursor's value. A standard SSE library stores
        # the last id it saw and returns it as ``Last-Event-ID``: a marker labelled 500 would hand
        # back the very cursor we refused, so a quiet run would earn a fresh marker (and a fresh
        # ``/state`` fetch) on every reconnect. Zero also keeps the marker out of the sequence
        # entirely (§3.4), which is what guarantees it can never raise ``delivered``/``arrived``.
        pending_gap = verdict.announce_gap
        # ⛔ The REPLAY phase discipline: wait for room, bounded by TIME. Depth is the wrong
        # criterion here and the arithmetic says so — the ring holds ten times what the queue does
        # (AGENT_RUN_EVENT_BUFFER_MAX 5000 against AGENT_RUN_SUBSCRIBER_QUEUE_MAX 500), so a replay
        # cannot fit and must FLOW THROUGH; a depth-triggered disconnect would fire on virtually
        # every reconnect to a full ring. Re-check any future rule against that pair of numbers.
        if pending_gap and generation:
            marker = truncation_marker(
                epoch=generation, seq=0, run_id=run_id, from_seq=replay.first_seq
            )
            if not await self._put_due_replay(queue, marker, run_id=run_id):
                return False
            pending_gap = False
        terminal_replayed = False
        for block in replay.blocks:
            if not await self._put_due_replay(queue, block, run_id=run_id):
                return False
            terminal_replayed = terminal_replayed or _is_terminal_block(block)
        if terminal_at_open or terminal_replayed:
            # The run is over and its terminal event is queued: a NORMAL end. Recognised HERE rather
            # than left to the supervisor, whose rule 4 is true from this moment on and would
            # otherwise abandon the queue together with that very event.
            return True
        # ⛔ Hysteresis before the live discipline — the phase switch is DECOUPLED from the guard.
        # At the boundary the queue is full BY CONSTRUCTION (the replay queued its last block by
        # waiting for room) and channel messages have been accumulating in the connection buffer for
        # the whole replay, so the first live put would meet the ceiling and disconnect a client
        # just served its entire replay within budget. Waiting for HALF the ceiling means the depth
        # guard starts measuring only once the client has PROVEN it drains, and it also stops the
        # thrashing a coincident threshold would cause.
        if not await self._await_live_phase(queue, run_id=run_id):
            return False
        # ⛔ The baseline handed to the live phase is the replay TAIL — what we actually delivered —
        # and never the client's cursor (§3.2.1 axis A / TD-046). Seeding from a claim let one HTTP
        # request with ?afterSeq= above the counter produce a permanently open, empty stream on a
        # perfectly healthy run: every live event was suppressed as "already sent" though none had
        # been. Nothing we did not deliver can be suppressed now.
        return await self._classify_live(
            run_id=run_id,
            pubsub=pubsub,
            queue=queue,
            activity=activity,
            generation=generation,
            delivered=replay.max_seq,
            replay_min_seq=replay.min_seq,
            filter_seq=verdict.filter_seq,
            pending_gap=pending_gap,
        )

    async def _put_due_replay(
        self,
        queue: asyncio.Queue[_QueueItem],
        block: bytes,
        *,
        run_id: str,
    ) -> bool:
        """REPLAY discipline: hand over one block, waiting for room. False = the budget ran out.

        Two rules meet here:

        * **the ceiling is a RULE, not ``maxsize``.** Ordinary blocks are admitted only while
          ``qsize() < AGENT_RUN_SUBSCRIBER_QUEUE_MAX`` although the queue holds one more, so the
          last slot stays free for the end-of-stream sentinel. Leaving that to ``maxsize`` makes the
          reserve fail in the one state it exists for — at overflow, where the sentinel finds no
          room and the terminal event already queued is thrown away with the queue.
        * **the wait is bounded by TIME** (``AGENT_RUN_SUBSCRIBER_DRAIN_SECONDS``), counted
          in this task. Depth cannot end this: the queue is full, the reader is parked in the put,
          and nothing new arrives to trip a guard on. Only a clock can, which is why the budget is a
          requirement and not a refinement (D2 — an unbounded await in the delivery path).

        ⚠️ **The budget bounds ONE WAIT — "no room for this long" — not the phase as a whole, and
        the difference is behavioural rather than cosmetic.** A phase-total deadline disconnects a
        client that drains steadily and simply has more to receive than the budget covers in
        wall-clock terms: with the ring holding ten times the queue that is an ordinary slow-network
        reconnect, and the failure it produced (a client cut off on the LAST block of a replay it
        had taken block by block) is the same "penalise the honest client" defect this section has
        rejected twice already. What must be released by time is a client that has STOPPED taking
        anything — exactly the per-wait reading, since progress resets the clock by construction.
        ⚠️ Consequence stated plainly: the ADR's "at most 2x the value" holds for the case it names
        (a slow replay, then a slow drain) but is NOT a universal cap — a client that resumes just
        before each deadline can be served for longer. The run's own upper bound
        (``AGENT_RUN_MAX_DURATION_SECONDS``, 2 h) dominates either way.
        """
        ceiling = self._settings.agent_run_subscriber_queue_max
        deadline = time.monotonic() + self._settings.agent_run_subscriber_drain_seconds
        while queue.qsize() >= ceiling:
            if time.monotonic() >= deadline:
                logger.warning(
                    "agent run events client took nothing for %.0fs, disconnecting run_id=%s "
                    "queued=%d",
                    self._settings.agent_run_subscriber_drain_seconds,
                    run_id,
                    queue.qsize(),
                )
                return False
            # Polling, deliberately: an Event signalled by the writer would be a new synchronisation
            # point in the hot path — a link the axis-D trigger list would require justifying, with
            # a deadline of its own — while a short sleep adds no primitive and no failure mode.
            await asyncio.sleep(_QUEUE_ROOM_POLL_SECONDS)  # noqa: ASYNC110 - see above
        queue.put_nowait(block)
        return True

    async def _await_live_phase(self, queue: asyncio.Queue[_QueueItem], *, run_id: str) -> bool:
        """Hold the replay discipline until the queue falls to HALF the ceiling. False = budget out.

        The gap is the point (§3.2.2): entering the live discipline at the ceiling would disconnect
        the client on its first live put, because the queue is full at the boundary by construction.
        Half the ceiling guarantees the first live put at least that many free slots — the guard
        begins to measure lag only once the client has demonstrably drained — and it removes the
        thrashing that a coincident entry point and threshold would produce around the limit.

        Bounded by the same clock as the replay, and on the same "per wait" reading: the deadline is
        reset whenever the queue actually shrinks, so what is released by TIME is a client that has
        stopped taking anything — not one that is draining steadily towards the halfway mark.
        """
        half = self._settings.agent_run_subscriber_queue_max // 2
        budget = self._settings.agent_run_subscriber_drain_seconds
        deadline = time.monotonic() + budget
        queued = queue.qsize()
        while queue.qsize() > half:
            if queue.qsize() < queued:
                # Progress: the client is draining, so the clock starts again.
                queued = queue.qsize()
                deadline = time.monotonic() + budget
            elif time.monotonic() >= deadline:
                logger.warning(
                    "agent run events client did not drain to half the queue in %.0fs, "
                    "disconnecting run_id=%s queued=%d",
                    budget,
                    run_id,
                    queue.qsize(),
                )
                return False
            await asyncio.sleep(_QUEUE_ROOM_POLL_SECONDS)  # noqa: ASYNC110 - as in _put_due_replay
        return True

    def _offer_live(self, queue: asyncio.Queue[_QueueItem], block: bytes, *, run_id: str) -> bool:
        """LIVE discipline: hand over one block by DEPTH. False = the client is too far behind.

        Here the depth means exactly what the setting is named after, and only because classifying
        happens BEFORE the queue: overlap duplicates never enter it, and the replay has already
        flowed through, so what is queued is what this client is owed and has not taken. Hitting the
        ceiling is therefore the client's lag, and the answer is the abnormal path — a deliberate
        disconnect with a log naming the real reason.

        ⚠️ No ``run.truncated`` accompanies it, and that is correct rather than an omission
        (§3.2.2): the prefix already delivered is INTACT, the undelivered messages stay in the ring,
        and the client's reconnect replays them from its cursor — or, if the ring was trimmed by
        then, C3 announces the gap. The operator level is mandatory here; the client level is not.
        """
        if queue.qsize() >= self._settings.agent_run_subscriber_queue_max:
            logger.warning(
                "agent run events subscriber is %d blocks behind, disconnecting run_id=%s",
                queue.qsize(),
                run_id,
            )
            return False
        queue.put_nowait(block)
        return True

    async def _supervise(
        self,
        *,
        run_id: str,
        closing: asyncio.Event,
        activity: _Activity,
        queue: asyncio.Queue[_QueueItem],
    ) -> None:
        """Close rules 4 and 5, off the delivery path (ADR-067 §3.2.2).

        This task exists because of WHERE its two probes may block, not because the loop was
        crowded. ``_is_terminal`` is a query against ``agent_runs`` and ``lease_alive`` a call to
        Redis; in the reader a block in either would stop the channel read AND the queue, leaving
        three live tasks and a stream that says nothing — the failure D2 names. Here the same block
        delays only the safety net.

        A probe that times out is therefore logged and RETRIED on the next tick rather than ending
        the stream: the deadline is what keeps this task from wedging, and its cost is a late
        closure, not a lost delivery.
        """
        # ⛔ Its OWN setting, not the lease TTL it used to borrow (TD-050): how long a consumer's
        # claim survives without renewal and how quickly a CLIENT stream notices its run ended are
        # unrelated quantities, and while they shared a knob, tuning the lease silently changed
        # downstream closing latency.
        interval = self._settings.agent_run_subscriber_probe_seconds
        idle_limit = self._settings.agent_run_downstream_idle_timeout_seconds
        while True:
            await asyncio.sleep(interval)
            if self._handover_in_progress(activity, queue):
                # A hand-over is under way, so no verdict may be issued: everything still queued is
                # already owed to this client. Note this SKIPS the tick and does not stand down for
                # good — the moment progress stops for a whole budget, rules 4/5 apply again.
                logger.debug(
                    "agent run events supervisor defers to the hand-over run_id=%s", run_id
                )
                continue
            try:
                async with asyncio.timeout(_PERIODIC_PROBE_TIMEOUT_SECONDS):
                    # Rule 4: the run may reach a terminal status without a terminal EVENT ever
                    # appearing in Redis (the consumer died, the ring expired, Redis was flushed).
                    if await self._is_terminal(run_id):
                        if self._handover_in_progress(activity, queue):
                            # A portion went out WHILE we were in the probe: the verdict is already
                            # stale, and acting on it would discard what is being delivered.
                            continue
                        logger.info("agent run events closing: run is terminal run_id=%s", run_id)
                        closing.set()
                        return
                    # Rule 5: idle AND no live lease. Both are required — a quiet run with a live
                    # consumer is normal and may stay quiet for a long tool call, and treating
                    # silence as death is the mistake that retired the idle timeout of §6.3.
                    idle = time.monotonic() - activity.last_event_at >= idle_limit
                    if idle and await self._bus.lease_alive(run_id) is False:
                        if self._handover_in_progress(activity, queue):
                            continue
                        logger.info(
                            "agent run events closing: idle with no lease run_id=%s", run_id
                        )
                        closing.set()
                        return
            except TimeoutError:
                # Mode D2 in the one place where its cost is acceptable — see the docstring.
                logger.warning("agent run events periodic probe timed out run_id=%s", run_id)

    def _handover_in_progress(self, activity: _Activity, queue: asyncio.Queue[_QueueItem]) -> bool:
        """Is a hand-over under way right now? The supervisor's only stand-down predicate (§3.2.2).

        Evaluated at three points of ONE tick — before the probes and again after each of them —
        because a probe takes time and a verdict formed before it may already be stale when it would
        be acted on.

        ⛔ TWO conjuncts, each with its own measured failure behind it, so neither may be dropped:

        * **something is still owed** (the queue is not empty). Without it the supervisor stays
          muted for a whole budget after the LAST block of a finished hand-over, when nothing is
          owed at all — and rule 5 stops closing an idle stream, because the default budget (120s)
          is far longer than the idle timeout that should have closed it. Measured: an idle
          leaseless stream stayed open past 15s where it used to close in about 2.
        * **and it is moving** (the last hand-over is newer than the budget). Without it a
          client that simply stops reading with a full queue mutes rules 4 and 5 for the rest of the
          session — the unconditional stand-down, which also takes with it the only route by which
          ``AGENT_RUN_MAX_DURATION_SECONDS`` becomes observable to a client.

        ⚠️ It is deliberately NOT phrased as "the reader knows how this ends". That was the previous
        condition, and a measured entrance defeats any such enumeration: a run not terminal at open,
        with no terminal event in the ring, whose status is written by ANOTHER PROCESS while it is
        being delivered — 21 of 41 blocks were abandoned while the reader had no way of knowing any
        of it. Progress is observable without knowing the outcome, which is what makes it right.
        """
        if queue.empty():
            return False
        budget = self._settings.agent_run_subscriber_drain_seconds
        return time.monotonic() - activity.last_delivered_at < budget

    async def _cursor_verdict(
        self, *, run_id: str, cursor: Cursor, epoch: str | None
    ) -> _CursorVerdict:
        """What the client's cursor may be used for, and whether a gap must be announced (§3.2.1).

        The cursor is a CLAIM, never evidence of what we delivered, so its only legitimate role is
        "do not resend what I already have" — a replay filter (``filter_seq``). It is honoured only
        when both halves of the dedup contract hold: a value not above the actual counter (the
        numeric guard of §3.2 step 2, whose absence let a cursor from the future silently eat an
        existing replay) and a generation we can verify it against.

        The two failure kinds collapse into one answer — replay everything and announce the gap —
        with one deliberate exception: ``?afterSeq=`` carries no generation BY FORM, so it can
        never be verified. Rejecting it outright would break a documented debugging parameter;
        honouring it silently would let the client splice two generations. So it is applied as a
        filter and ALWAYS announced. Making it carry a generation was rejected as being
        ``Last-Event-ID`` under another name.
        """
        if cursor.empty:
            # ⚠️ This branch OWNS ``?afterSeq=0``, and that is not a technicality. The contract
            # admits 0 (int >= 0) and §3.2 step 2 makes it the EMPTY cursor, identical to sending
            # nothing — while the pairwise negative of §3.4 requires that an empty cursor against a
            # complete ring produce NO marker. So "applying ?afterSeq always warrants a marker" and
            # that negative are contradictory demands on this one input; the marker is due when the
            # filter
            # actually filters (``afterSeq > 0``), which is also the only case where the client is
            # claiming a prefix we cannot verify.
            return _CursorVerdict(filter_seq=0, announce_gap=False)
        current = await self._bus.current_seq(run_id)
        if cursor.seq > current:
            # From the future: a counter that was reset (or a client inventing a number). Note that
            # an unreadable counter reads as 0 here, which rejects the cursor — the safe direction,
            # since the alternative is filtering a replay against a number we could not check.
            logger.info(
                "agent run events cursor is above the counter run_id=%s cursor=%d current=%d",
                run_id,
                cursor.seq,
                current,
            )
            return _CursorVerdict(filter_seq=0, announce_gap=True)
        if epoch is None or (cursor.epoch is not None and cursor.epoch != epoch):
            logger.info("agent run events cursor from another generation run_id=%s", run_id)
            return _CursorVerdict(filter_seq=0, announce_gap=True)
        if cursor.epoch is None:
            # ?afterSeq= — a filter, and one whose generation is unverifiable by construction.
            return _CursorVerdict(filter_seq=cursor.seq, announce_gap=True)
        return _CursorVerdict(filter_seq=cursor.seq, announce_gap=False)

    def _replay_blocks(
        self,
        *,
        run_id: str,
        ring: list[RingEvent],
        filter_seq: int,
        generation: str,
        gap_announced: bool,
    ) -> _Replay:
        """Ring elements above the filter, plus the seqs that make up the delivery provenance.

        ``first_seq``/``last_seq`` are what was ACTUALLY delivered here (0 when nothing was), and
        ``last_seq`` is the only legitimate seed for the live dedup baseline (§3.2.1 source S2).

        The gap rule is "first delivered seq > filter + 1", and with an empty filter ≡ 0 it reads
        "first seq > 1" — which is why the marker also fires on a FIRST connection to an
        already-trimmed ring. It is skipped when the caller has already announced the same
        discontinuity, so a rejected cursor produces exactly ONE marker rather than two.

        Ring order is assumed to match seq order; the write pipeline guarantees it by doing
        ``INCR`` and ``RPUSH`` inside one atomic script.
        """
        pending = [event for event in ring if event.seq > filter_seq]
        if not pending:
            return _Replay(blocks=[], first_seq=0, min_seq=0, max_seq=0)
        blocks: list[bytes] = []
        if pending[0].seq > filter_seq + 1 and not gap_announced:
            logger.info(
                "agent run events replay starts past the cursor run_id=%s from_seq=%d",
                run_id,
                pending[0].seq,
            )
            blocks.append(
                truncation_marker(
                    epoch=pending[0].epoch or generation,
                    seq=max(filter_seq, 0),
                    run_id=run_id,
                    from_seq=pending[0].seq,
                )
            )
        blocks.extend(
            sse_block(epoch=event.epoch or generation, seq=event.seq, data=event.data)
            for event in pending
        )
        seqs = [event.seq for event in pending]
        return _Replay(
            blocks=blocks, first_seq=pending[0].seq, min_seq=min(seqs), max_seq=max(seqs)
        )

    async def _classify_live(
        self,
        *,
        run_id: str,
        pubsub: object,
        queue: asyncio.Queue[_QueueItem],
        activity: _Activity,
        generation: str,
        delivered: int,
        replay_min_seq: int,
        filter_seq: int,
        pending_gap: bool,
    ) -> bool:
        """Apply §3.2.1 to the live channel and queue ONLY what the client is owed.

        Returns True for a NORMAL end — the terminal event has been queued, or the channel itself
        ended — and False for an abnormal one (a client too far behind, a deadline). The caller
        turns that into flag-plus-sentinel or into the abnormal signal, and the writer's opposite
        treatment of the queue hangs on that difference.

        Classification happens BEFORE the queue, and that is what makes the queue's depth mean "how
        far behind the client is". Queueing everything and filtering at drain time cannot work by
        construction: the queue would be filled by a task that only reads and emptied by one that
        parses, compares and probes databases, so its depth would answer a question about our own
        speed. After TD-047 it would also be the ORDINARY path rather than an edge case — the
        subscription is established before the ``LRANGE``, so channel copies of every replayed event
        always arrive, and a ring of 5000 against a depth of 500 would disconnect on every reconnect
        to a full ring. qa saw exactly that: a client that had received its whole 600-block replay,
        disconnected while the duplicates drained.

        ⚠️ The invariant C1a depends on: ONE reader, ONE consumer, FIFO. ``arrived`` is evidence of a
        counter reset only because arrival order on the channel is monotonic; a second reader of
        this channel, or an unordered hand-off, breaks that silently — false ``run.truncated`` on
        reordered messages, and a real reset possibly unnoticed.
        """
        check_every = self._settings.agent_run_consumer_lease_ttl_seconds
        last_check_at = time.monotonic()
        current_generation = generation
        # The two counters of §3.2.1, and they cannot be merged — that is the whole point:
        # `delivered` is the dedup baseline and its provenance is DELIVERY (replay tail / a live
        # event we queued); `arrived` is the last seq that came in ON THE CHANNEL, whatever we then
        # did with it, and its provenance is channel ORDER. The pair of them is what separates two
        # situations with identical numbers: a normal ring+live overlap duplicate (seq 35 while 40
        # was delivered — arrives in channel order, no regression) from a counter reset (seq 1 while
        # 500 was delivered — a regression of a counter that INCR makes monotonic within a
        # generation, hence evidence of a reset and never a duplicate).
        arrived = 0
        # Has the hole BELOW the replay's lowest delivered seq been announced yet? One marker covers
        # the whole hole; the events inside it are all delivered regardless (see the branch below).
        hole_announced = False

        async for message in _iter_messages(pubsub, run_id=run_id):
            now = time.monotonic()
            if message is not None:
                event = _parse_message(message)
                if event is None:
                    # Operator-level only, and deliberately: we know a message existed but NOT its
                    # seq, so no marker can be built here. The CLIENT is told by C2 on the next
                    # event, whose seq will exceed `delivered + 1` (§3.2.1 — the two levels are
                    # separate and neither replaces the other). The payload is NEVER logged: it is
                    # user content (§3.5).
                    logger.warning("agent run events channel message unparseable run_id=%s", run_id)
                else:
                    # ⚠️ ONE branch per message, mutually exclusive, at most ONE marker — see
                    # §3.3.1b. Written as independent ifs it would emit two markers in a row
                    # (generation change, then "1 <= 500 looks like a reset"), or a marker for
                    # every event 2..500 after a reset. `arrived` is updated in EVERY branch for
                    # the same reason, and `delivered` advances in the shared `if deliver:` block
                    # below — so every branch that delivers advances it, including adoption. Were
                    # that not so, C2 would compare against a baseline frozen at 0 for ever.
                    deliver = True
                    # Whether THIS message has already produced a client notification. One
                    # discontinuity earns one marker: the C3 check at delivery must not add a second
                    # one to a message whose branch already announced (a reset delivers its own
                    # event with `delivered` back at 0, which is precisely C3's trigger shape).
                    announced = False
                    if not current_generation:
                        # §3.3.1a step 2: the session opened with no generation at all (no key
                        # AND an empty ring). It adopts the first one it sees. From here the
                        # generation check is armed — the invariant being that no event reaches
                        # the client while it is not.
                        logger.info("agent run events adopted generation run_id=%s", run_id)
                        current_generation = event.epoch
                        if pending_gap:
                            # The client arrived HOLDING a cursor we could not verify against
                            # any generation, and is about to receive the tail of one. Silence
                            # here would have it splice two generations into one transcript.
                            # With a readable key the same client is told by §3.2 step 2, and
                            # adoption must not be softer than the ordinary path — otherwise the
                            # contract depends on whether Redis happened to answer.
                            pending_gap = False
                            announced = True
                            if not self._offer_live(
                                queue,
                                truncation_marker(
                                    epoch=event.epoch, seq=0, run_id=run_id, from_seq=0
                                ),
                                run_id=run_id,
                            ):
                                return False
                    elif event.epoch != current_generation:
                        # §3.3.1: the generation changed UNDER an open connection. Continuing
                        # with the old baseline is what turns the stream silent — every new event
                        # would be discarded by the dedup rule below. Reset and say so.
                        logger.warning(
                            "agent run events generation changed mid-stream run_id=%s", run_id
                        )
                        current_generation = event.epoch
                        delivered = 0
                        # Same reason as in the reset branch below: the replay window belonged to
                        # the previous numbering and must not be applied to the new one.
                        replay_min_seq = 0
                        announced = True
                        if not self._offer_live(
                            queue,
                            truncation_marker(
                                epoch=event.epoch, seq=0, run_id=run_id, from_seq=event.seq
                            ),
                            run_id=run_id,
                        ):
                            return False
                    elif event.seq <= arrived:
                        # §3.3.1b: the counter went BACKWARDS inside one generation. INCR makes
                        # that impossible for a live sequence, so this is a reset of the counter
                        # (the ring keys expire on their own TTL after a long silence while the
                        # lease lives on) and NOT a duplicate. Treated exactly like a generation
                        # change: announce, drop the baseline, deliver the event. Without it the
                        # generations match, the check is armed, and the event is still lost.
                        logger.warning(
                            "agent run events seq regressed mid-stream run_id=%s seq=%d arrived=%d",
                            run_id,
                            event.seq,
                            arrived,
                        )
                        delivered = 0
                        # The old numbering is gone, so the replay's lower bound describes nothing
                        # any more — keeping it would make the new generation's low seqs look like
                        # holes and earn a second marker for one discontinuity.
                        replay_min_seq = 0
                        announced = True
                        if not self._offer_live(
                            queue,
                            truncation_marker(
                                epoch=event.epoch, seq=0, run_id=run_id, from_seq=event.seq
                            ),
                            run_id=run_id,
                        ):
                            return False
                    elif event.seq > delivered:
                        # Above the mark, so it is new — but "new" is not the same as "next", and
                        # C2 (§3.2.1) is the check for the difference. A jump over `delivered + 1`
                        # means events between the two exist and this session will never serve them:
                        # a channel message we could not parse (we knew it existed but not its seq,
                        # so the log was all we had then — THIS is where the client finally learns),
                        # a message lost on the channel, the LRANGE→SUBSCRIBE window before TD-047,
                        # or a source nobody has enumerated. Nothing checked for this at all before:
                        # `event.seq > delivered` waves every jump through in silence.
                        #
                        # Scoped to `delivered > 0` on purpose: with nothing delivered yet there is
                        # no "previous" to be discontinuous with, and that case belongs to C3 below.
                        if delivered > 0 and event.seq > delivered + 1:
                            announced = True
                            logger.warning(
                                "agent run events skipped a seq run_id=%s seq=%d delivered=%d",
                                run_id,
                                event.seq,
                                delivered,
                            )
                            if not self._offer_live(
                                queue,
                                truncation_marker(
                                    epoch=event.epoch, seq=0, run_id=run_id, from_seq=event.seq
                                ),
                                run_id=run_id,
                            ):
                                return False
                    elif event.seq < replay_min_seq:
                        # §3.2.1 C1 (strict invariant): BELOW the replay's lowest delivered seq. The
                        # baseline is an upper MARK, not the set of delivered seqs, so "under the
                        # mark" does NOT mean "was delivered" — this event was published while we
                        # were already subscribed and then pushed out of the ring by head trimming
                        # before our LRANGE read it, so it fell in a hole we never served. Dropping
                        # it as a "proven duplicate" is the silent loss of a single event, which the
                        # invariant covers as much as the loss of a stream. Announced ONCE for the
                        # whole hole; every event in it is delivered.
                        if not hole_announced:
                            hole_announced = True
                            announced = True
                            logger.warning(
                                "agent run events event below the replay window run_id=%s "
                                "seq=%d replay_min=%d",
                                run_id,
                                event.seq,
                                replay_min_seq,
                            )
                            if not self._offer_live(
                                queue,
                                truncation_marker(
                                    epoch=event.epoch, seq=0, run_id=run_id, from_seq=event.seq
                                ),
                                run_id=run_id,
                            ):
                                return False
                    else:
                        # The ONLY silent drop the contract allows: this seq lies WITHIN the range
                        # the replay actually delivered, and channel order confirms it is the
                        # ring+live overlap rather than a reset.
                        deliver = False
                        logger.debug(
                            "agent run events dropping an overlap duplicate run_id=%s "
                            "seq=%d delivered=%d replay_min=%d",
                            run_id,
                            event.seq,
                            delivered,
                            replay_min_seq,
                        )
                    arrived = event.seq
                    if deliver:
                        # C3 (§3.2.1), and it applies in the LIVE phase too — that is the whole
                        # point of "independent of phase". The first thing this session delivers
                        # starting above the beginning means events before it exist and the client
                        # will never see them. In the replay phase _replay_blocks covers it; here it
                        # covers the case the old rule missed entirely: ``replay()`` returns ``[]``
                        # both for an empty ring and for a ring it could NOT READ (RedisError), so a
                        # session can adopt a generation believing there is no history while the
                        # history exists — and then the first live event arrives at an arbitrary seq
                        # with nothing to notice it by. Neither replay_min_seq (0 after an empty
                        # replay) nor the dedup rule sees it. The old justification ("with an empty
                        # cursor nothing can be called incomplete") substituted "the client got no
                        # prefix" for "no prefix exists"; only the second would license silence.
                        if delivered == 0 and not announced and event.seq > filter_seq + 1:
                            logger.info(
                                "agent run events first delivery starts past the beginning "
                                "run_id=%s seq=%d filter=%d",
                                run_id,
                                event.seq,
                                filter_seq,
                            )
                            if not self._offer_live(
                                queue,
                                truncation_marker(
                                    epoch=event.epoch, seq=0, run_id=run_id, from_seq=event.seq
                                ),
                                run_id=run_id,
                            ):
                                return False
                        # ``max`` because the baseline is an UPPER mark: delivering an event from
                        # the hole below must not lower it, or all above would look new again.
                        delivered = max(delivered, event.seq)
                        activity.last_event_at = now
                        block = sse_block(epoch=event.epoch, seq=event.seq, data=event.data)
                        if not self._offer_live(queue, block, run_id=run_id):
                            return False
                        if _is_terminal_block(block):
                            # Rule 1 (§3.3): the run ended and its terminal event is now queued, so
                            # this is a NORMAL end. Recognised here rather than left to the
                            # supervisor, whose rule 4 turns true at this very moment and would
                            # otherwise abandon the queue together with that event.
                            logger.info(
                                "agent run events reached the terminal event run_id=%s", run_id
                            )
                            return True
                        continue
            # ⚠️ What is left of the periodic tick HERE is only the generation re-read, and it
            # stays because it is CLASSIFICATION, not a close rule: it resets the baseline and emits
            # a marker, so it must run where that state lives. The close rules 4 and 5 moved to the
            # supervisor — a block in a Postgres query or a lease probe must not stop the channel
            # read and the queue at once (§3.2.2).
            #
            # ⛔ It still gets a deadline of our own (mode D2). This one call is Redis, and while
            # ``current_epoch`` turns a RedisError into ``None``, what stays unbounded is a call
            # that neither answers nor fails — and blocking HERE stops delivery, which is why
            # the reader may not hold an unbounded await.
            if now - last_check_at >= check_every:
                last_check_at = now
                try:
                    async with asyncio.timeout(_PERIODIC_PROBE_TIMEOUT_SECONDS):
                        # Re-read on a cadence, not only on reconnect: a restart during a quiet run
                        # would otherwise go unnoticed until the next event, and by then the dedup
                        # rule has already silenced it.
                        fresh = await self._bus.current_epoch(run_id)
                except TimeoutError:
                    logger.warning(
                        "agent run events generation re-read timed out, closing run_id=%s", run_id
                    )
                    # A deadline that fired is an ABNORMAL end (§3.2.2): we no longer know whether
                    # the numbering under us is still the one we are comparing against.
                    return False
                if fresh is not None and not current_generation:
                    # §3.3.1a step 2, SECOND arming point — and not a duplicate of the one on the
                    # event: a quiet run may emit nothing for hours, and a session armed only by
                    # events would spend all that time unarmed, then lose the very event that would
                    # have armed it to the dedup rule.
                    logger.info("agent run events adopted generation while idle run_id=%s", run_id)
                    current_generation = fresh
                    if pending_gap:
                        # Same duty as in the event branch, and it must be here too: for a quiet run
                        # this IS the arming path, so announcing only on the event would leave the
                        # cursor-holding client unwarned for as long as the run is silent.
                        pending_gap = False
                        if not self._offer_live(
                            queue,
                            truncation_marker(epoch=fresh, seq=0, run_id=run_id, from_seq=0),
                            run_id=run_id,
                        ):
                            return False
                elif fresh is not None and current_generation and fresh != current_generation:
                    logger.warning(
                        "agent run events generation changed while idle run_id=%s", run_id
                    )
                    current_generation = fresh
                    delivered = 0
                    # ⚠️ `arrived` resets too: the new generation starts at seq 1, and leaving a
                    # stale 500 here would make its first event look like a regression and earn a
                    # SECOND marker for one discontinuity. Same for the replay window, which
                    # described the numbering that has just been replaced.
                    arrived = 0
                    replay_min_seq = 0
                    # The marker is due here as much as on the event path. Without it this branch
                    # fixed the silence and left a stream repeating id: values the client has seen
                    # already, with no sign of a discontinuity — and for a quiet run this fires
                    # FIRST, before any event, so it is the main path and not an edge case. No event
                    # means no from_seq: 0 reads "the stream restarts".
                    if not self._offer_live(
                        queue,
                        truncation_marker(epoch=fresh, seq=0, run_id=run_id, from_seq=0),
                        run_id=run_id,
                    ):
                        return False
        # The channel iteration ended: no more events can arrive, which §3.2.2 counts as a NORMAL
        # end — everything already queued is still owed to the client and is handed over.
        return True

    async def _is_terminal(self, run_id: str) -> bool:
        """Whether ``agent_runs.status`` says the run is over. False when the row is missing.

        One short session per probe (see the class docstring): the connection is taken, the row read
        and the connection returned, so nothing is held between probes 30 s apart.
        """
        async with self._runs() as runs:
            run = await runs.get(run_id)
        return run is not None and run.status in _TERMINAL_STATUSES


async def _iter_messages(pubsub: object, *, run_id: str) -> AsyncIterator[object | None]:
    """Yield channel messages, or None on a poll tick so the caller can run its timers.

    ⛔ This is the ONLY caller of ``get_message`` in the module, and it must stay that way: it is
    entered only after the replay, from the live phase, and the replay baseline is complete before
    the first message is looked at precisely BECAUSE nothing polls the channel earlier. Messages
    published during the ``LRANGE`` wait in the connection buffer, with nobody to classify them —
    which is the mechanism, not a bet on the replay being quick. A second call site "to drain the
    channel while the replay runs" would classify against ``delivered = 0`` and re-send the client
    every element of the ring, silently (§3.2.2, variant «а»).

    ⛔ The burst counter that used to live here is GONE and must not return (§3.2.2): it counted
    messages between two idle polls of the channel and disconnected the client above a threshold, so
    a fast HEALTHY client was cut off for one busy second while a genuinely slow one was never
    cut off at all — the client's speed did not enter the condition. No threshold value fixes that;
    it is the wrong quantity. When a client is too far behind is the queue depth's job (TD-048),
    and reading the channel is now unconditional.

    The poll timeout keeps this loop from being an unbounded await (mode D2) and is what lets the
    caller's periodic tick run during a silent run.
    """
    while True:
        try:
            message = await pubsub.get_message(  # type: ignore[attr-defined]
                ignore_subscribe_messages=True, timeout=1.0
            )
        except RedisError:
            logger.warning("agent run events subscription failed run_id=%s", run_id)
            return
        yield message


def _task_failed(task: asyncio.Task[None], *, run_id: str, role: str) -> bool:
    """Whether ``task`` ended with an exception — retrieving it, and logging it once.

    Serves D1 (3) now that the pipeline holds plain tasks instead of a ``TaskGroup``: a child that
    dies must close the client stream, and the delivery loop can only act on what it can observe.
    Retrieval is the second half of the job — an unretrieved task exception is reported by
    asyncio at GC time, with no run id and long after anyone could act on it.

    A CANCELLED task is not a failure: cancellation is how this pipeline is torn down normally.
    """
    if not task.done() or task.cancelled():
        return False
    error = task.exception()
    if error is None:
        return False
    logger.warning(
        "agent run events %s failed run_id=%s error=%s", role, run_id, type(error).__name__
    )
    return True


def _is_terminal_block(block: bytes) -> bool:
    """Whether an outgoing SSE block carries a terminal run event (ADR-067 §3.3 rule 1).

    Read from the JSON ``event`` field, where the patched production image puts it (there is
    no ``event:`` header line, ADR-065). Used to recognise a NORMAL end while the terminal event is
    still in the queue — the supervisor's rule 4 becomes true at that same moment, and without this
    it would abandon the queue together with the event.
    """
    marker = block.find(b"data: ")
    if marker < 0:
        return False
    try:
        payload = json.loads(block[marker + len(b"data: ") :])
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("event") in _TERMINAL_EVENTS


def _parse_message(message: object) -> RingEvent | None:
    """Decode a pub/sub payload into a ring event. None when it is not one."""
    if not isinstance(message, dict):
        return None
    from app.agent_proxy.transport import parse_ring_element

    return parse_ring_element(message.get("data"))


async def _close_pubsub(pubsub: object) -> None:
    """Release the subscription; a failure here must never surface to the client."""
    try:
        await pubsub.unsubscribe()  # type: ignore[attr-defined]
        await pubsub.aclose()  # type: ignore[attr-defined]
    except (RedisError, RuntimeError):
        logger.warning("agent run events pubsub close failed")
