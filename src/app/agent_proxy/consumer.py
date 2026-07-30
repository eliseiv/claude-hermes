"""Background agent-run consumer — the WORKING task (ADR-067 §6.1, §6.2, §6.4).

Before ADR-067 the only reader of a run's Hermes stream was the client's own ``/events``
subscription, so a run whose client walked away was never billed and stayed ``running`` for ever
(TD-037: 3 prod runs → 0 debits). This task makes the SERVER the single upstream subscriber: it
reads the stream, mirrors every event into Redis for downstream clients (ADR-067 §3), and applies
the domain rules — snapshot, terminal status, billing — through
:meth:`AgentProxyService.process_event`, which is the very code the relay used to run (§2:
"меняется исполнитель, не правила").

Two properties of the Hermes image shape everything here, both measured, neither negotiable:

* **The stream is ONE-SHOT.** A second subscription to the same ``runId`` receives nothing at all —
  no replay, no live events. The artifact says so in one line, and that line's whole content is:
  a curl timeout after 35005 ms **with 0 bytes received**
  (``tests/fixtures/hermes_prod_resubscribe_empty_adr067.curl.txt``). So exactly one subscriber may
  exist, it must be us, and it must be established BEFORE the client is told the run started.
* **A subscription that fails is spent anyway.** A connection dropped BEFORE the response headers
  still consumed the stream, so there is no safe window for a retry: retrying is forbidden
  UNCONDITIONALLY (§6.4.1) — not "usually", and not to be confused with the connect-only retry of
  ``POST /v1/runs``, which ADR-062 keeps because that endpoint is a different, non-consuming
  operation.

  ⚠️ **Provenance, kept apart from the artifact above ON PURPOSE.** The timings behind this bullet —
  an abort at 0.25 s against a ``time_starttransfer`` of 0.17-0.18 s — come from the Q-067-13
  measurement write-up, NOT from that curl file: the file holds a single timeout line and no timings
  whatsoever. They were once written as though the file contained them, which is exactly how a
  description outlives the thing it described. Cite the question for the numbers and the file only
  for what it actually says.

The supervisor half (lease renewal, heartbeat, ``MAX_DURATION``, stall detection) is a separate
task in the same ``TaskGroup``; this module owns the beacon it reads but never renews a lease or
writes a heartbeat itself — liveness must not be self-declared by the task being judged (§6.1).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import socket
import time
import uuid
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.agent_proxy.service import AgentProxyService, is_terminal_event, iter_sse_blocks
from app.agent_proxy.transport import AgentRunEventBus, LeaseAcquisition, LeaseRenewal
from app.config import Settings
from app.hermes_runtime.manager import InstanceEndpoint
from app.observability.metrics import agent_run_consumers_active

logger = logging.getLogger("app.agent_proxy.consumer")

# How the consumer reaches the database: ONE SESSION PER OPERATION, opened when there is work and
# closed immediately after (ADR-067 §6.1.1).
#
# ⚠️ The invariant behind it matters more than the choice: a session is NEVER held across an
# ``await`` on upstream. A run lasts up to two hours while its DB work is sparse (a snapshot flush
# at most every 3s, a heartbeat every 30s, a debit per usage.delta), so a session per RUN would pin
# a pooled connection — one per concurrent run, a number with no cap (Q-067-2) — for the whole run,
# keep a transaction open across network waits and block VACUUM on the hottest tables of the
# contour. It is also the direct lesson of TD-040: holding a lock across a network call produced a
# symptom we spent several iterations mistaking for a wedged instance.
ServiceFactory = Callable[[], AbstractAsyncContextManager[AgentProxyService]]

# Beacon states (ADR-067 §6.1). Deliberately three, not two: `connecting` is NOT exempt from the
# stall rule while `awaiting_upstream` is, and collapsing them would either kill live runs waiting
# on a slow agent or let an inert subscription live for ever.
BEACON_CONNECTING: Final = "connecting"
BEACON_AWAITING_UPSTREAM: Final = "awaiting_upstream"
BEACON_PROCESSING: Final = "processing"

# Audit eventTypes of the §6.4 self-termination procedure. The consumer never records a terminal
# RUN status (it does not know the outcome) — these say why IT stopped, and the reaper finalizes.
EVENT_CONSUMER_FAILED: Final = "agent_run_consumer_failed"
EVENT_CONSUMER_DISCONNECTED: Final = "agent_run_consumer_disconnected"


@dataclass
class ConsumerBeacon:
    """What the working task publishes about itself for the supervisor to judge (ADR-067 §6.1).

    Written here, read there, never the reverse. Two things make it a real liveness signal rather
    than a claim:

    * ``state`` changes on TRANSITIONS, and ``processing`` is set BEFORE entering the handling of an
      event — not after the iteration. An "end of iteration" beacon leaves the most likely hang
      (the handler itself wedged on a DB write) sitting in ``awaiting_upstream``, which the
      supervisor treats as unconditionally alive, so the very failure it exists to catch would be
      the one it never saw.
    * ``bytes_read`` / ``last_published_seq`` are monotonic PROGRESS counters. State alone says "the
      loop turned", not "the stream moved": a fast reconnect loop would look alive indefinitely.
    """

    state: str = BEACON_CONNECTING
    # Monotonic clock: this is compared against wall-clock-independent thresholds and must not move
    # when the system clock is adjusted.
    since: float = field(default_factory=time.monotonic)
    bytes_read: int = 0
    last_published_seq: int = 0
    # Number of state transitions, so the supervisor can tell a stream that is quietly waiting from
    # a loop that is spinning without reading anything (§6.1, last rule).
    transitions: int = 0

    def set_state(self, state: str) -> None:
        """Record a transition. Idempotent: re-declaring the same state does not reset ``since``.

        Resetting ``since`` on every repeat would let a handler that keeps re-entering
        ``processing`` refresh its own deadline for ever — the stall rule measures how long we
        have been in a state, not how recently we said so.
        """
        if self.state == state:
            return
        self.state = state
        self.since = time.monotonic()
        self.transitions += 1

    def note_bytes(self, count: int) -> None:
        """Count bytes as they arrive AND perform the ``connecting`` → ``awaiting_upstream``
        transition on the very first one (ADR-067 §6.4.2).

        ⚠️ The transition belongs HERE, at the first byte, and not at the 2xx response headers.
        ``awaiting_upstream`` is unconditionally alive to the supervisor, so a beacon moved there on
        the headers exempts the run from ``AGENT_RUN_FIRST_BYTE_STALL_SECONDS`` before the guard has
        anything to judge — and an ESTABLISHED-BUT-INERT subscription (the only class the guard was
        kept in v1 for: a second consumer after a lease lapse, or a stream someone else already
        drank) would then hold its lease and heartbeat until ``MAX_DURATION`` — 7200s instead of
        180s — keeping the user's instance out of hibernation all that time. The spin rule does not
        cover it either: with no events there is a single transition against a threshold of four.

        Counting the bytes was never the hard part — the state machine reading them was.
        """
        if count > 0 and self.bytes_read == 0:
            # First byte of this subscription's whole life: the stream is genuinely flowing, so the
            # guard has done its job and waiting is normal from here on. Checked BEFORE the
            # increment — after it, ``bytes_read`` no longer distinguishes the first byte.
            #
            # ⚠️ ``count > 0`` is the whole condition, not a formality: an empty chunk would
            # otherwise leave the beacon claiming the stream moved while ``bytes_read`` (and
            # ``saw_first_byte``) still say it did not. The run would land in ``awaiting_upstream``,
            # which the supervisor holds alive unconditionally, and fall through to the spin rule,
            # which an event-less stream never trips — the guard disabled by the very signal meant
            # to arm it. Today httpx does not hand us empty chunks, and that is exactly why the
            # guard must not be written as if it were OUR invariant: this contour's original prod
            # defect came from trusting an undocumented property of someone else's stream.
            self.set_state(BEACON_AWAITING_UPSTREAM)
        self.bytes_read += count

    @property
    def saw_first_byte(self) -> bool:
        return self.bytes_read > 0


def keepalive_socket_options(settings: Settings) -> list[tuple[int, int, int]]:
    """TCP keep-alive socket options for the upstream subscription (ADR-067 §6.2).

    ⚠️ httpx has NO "enable keep-alive" switch — without passing these explicitly the Linux default
    ``tcp_keepalive_time`` of 7200s applies and the dead-peer detector is fictional: a vanished
    instance would surface later than our own ``MAX_DURATION``, i.e. never in practice.

    ``TCP_KEEPIDLE``/``TCP_KEEPINTVL``/``TCP_KEEPCNT`` are Linux names; each is included only if the
    running platform defines it (macOS spells the first one ``TCP_KEEPALIVE`` and omits the
    rest), so a developer machine degrades to plain ``SO_KEEPALIVE`` instead of failing at
    import. Production is Linux, where all four apply — end-to-end behaviour through ``hermes-net``
    is H7/Q-067-9.
    """
    options: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (
        ("TCP_KEEPIDLE", settings.agent_run_upstream_tcp_keepidle_seconds),
        ("TCP_KEEPINTVL", settings.agent_run_upstream_tcp_keepintvl_seconds),
        ("TCP_KEEPCNT", settings.agent_run_upstream_tcp_keepcnt),
    ):
        option = getattr(socket, name, None)
        if option is not None:
            options.append((socket.IPPROTO_TCP, option, value))
    return options


def build_upstream_client(settings: Settings) -> httpx.AsyncClient:
    """httpx client for the upstream event subscription: keep-alive on, READ TIMEOUT OFF.

    ⚠️ ``read=None`` is mandatory (ADR-067 §6.2). A read timeout times out SILENCE, and silence is
    normal here — an agent may think for many minutes inside one tool call. Bounding it would
    recreate the idle-timeout defect that was retracted in revision 2: killing working runs because
    they were quiet. Death of the peer is detected by TCP keep-alive instead, which distinguishes a
    dead socket from a quiet one; ``connect``/``write``/``pool`` stay bounded because a dead
    instance must still fail fast at setup.
    """
    connect = settings.hermes_sse_connect_timeout_seconds
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=connect, read=None, write=connect, pool=connect),
        transport=httpx.AsyncHTTPTransport(socket_options=keepalive_socket_options(settings)),
    )


class ConsumerSubscriptionError(RuntimeError):
    """The upstream subscription could not be established, or died once established.

    One exception for both cases ON PURPOSE: ADR-067 §6.4.1 gives them the same response (stop, no
    retry, let the reaper finalize), and the measurement behind that rule is precisely that the two
    are indistinguishable from the image's point of view — a connection dropped before the headers
    had already consumed the stream. A type that let callers branch on "did it even start?" would
    invite exactly the retry the measurement ruled out.
    """


@dataclass
class ConsumerOutcome:
    """How the working task ended — the audit input of the §6.4 self-termination procedure."""

    # agent_run_consumer_* eventType, or None when the run reached a terminal event normally.
    audit_event: str | None
    # Terminal domain event seen (run.completed/run.failed/run.paused). False means the stream
    # ended without one, which is what makes the run the reaper's business rather than settled.
    terminal_seen: bool
    events_processed: int


async def run_worker(
    *,
    services: ServiceFactory,
    bus: AgentRunEventBus,
    settings: Settings,
    endpoint: InstanceEndpoint,
    user_id: uuid.UUID,
    run_id: str,
    ctx: ConsumerContext,
) -> ConsumerOutcome:
    """Own the single upstream subscription for one run until it ends. NEVER retries (§6.4.1).

    Per event, in this order: publish to Redis (best-effort, §3.1) and THEN apply the domain rules.
    The order matters — a domain handler that raises must not cost downstream clients the event,
    while a Redis outage must not cost the run its billing. Only the second of those is fatal here;
    publishing already degrades to a logged ``None`` inside the transport.

    Returns how it ended. It deliberately does NOT write a terminal status or finalize billing on an
    abnormal end (§6.4 step 4): the consumer does not know the run's outcome, and guessing would
    produce exactly the false ``failed`` + partial debit that §6.1 warns about. Finalization of an
    abandoned run belongs to the reaper alone, by one path for every cause.
    """
    url = f"{endpoint.base_url}/v1/runs/{run_id}/events"
    async with services() as service:
        run = await service.new_run_processing(user_id=user_id, run_id=run_id)
    ctx.processing = run
    processed = 0
    terminal_seen = False
    # `connecting` from the start and until the first byte: an established-but-inert subscription
    # is indistinguishable from a slow one at the socket level, and only this state is subject to
    # AGENT_RUN_FIRST_BYTE_STALL_SECONDS (§6.4.2).
    beacon = ctx.beacon
    beacon.set_state(BEACON_CONNECTING)
    try:
        async with (
            build_upstream_client(settings) as client,
            client.stream("GET", url, headers=_bearer(endpoint.api_key)) as response,
        ):
            if not 200 <= response.status_code < 300:
                logger.warning(
                    "agent run consumer upstream non-2xx run_id=%s status=%s",
                    run_id,
                    response.status_code,
                )
                raise ConsumerSubscriptionError("hermes events stream failed")
            # The stream is ours from here: headers are in, and by the measurement behind §6.4.1
            # the image considers it consumed even earlier than this. Announced BEFORE the first
            # event so a caller gated on "are we subscribed?" is not also gated on the agent
            # producing something, which can take minutes.
            ctx.handshake.mark_established()
            # The beacon deliberately STAYS in `connecting` here. Headers prove the subscription was
            # accepted, not that the stream carries anything, and those are exactly the two cases
            # §6.4.2 exists to separate: an inert subscription is accepted too. It leaves this state
            # on the first BYTE, inside ConsumerBeacon.note_bytes — until then the run is subject to
            # AGENT_RUN_FIRST_BYTE_STALL_SECONDS.
            async for block, raw in iter_sse_blocks(response, on_bytes=beacon.note_bytes):
                # BEFORE the handling, not after: see ConsumerBeacon.
                beacon.set_state(BEACON_PROCESSING)
                # ctx.epoch, not a captured value: after a Redis restart the supervisor adopts a NEW
                # generation, and events published under the old one would be dropped by the
                # broker's epoch check — invisible here, silent for the client (§3.3.1).
                seq = await bus.publish(run_id, epoch=ctx.epoch, raw=raw)
                if seq is not None:
                    beacon.last_published_seq = seq
                # Session opened AFTER the event arrived and closed before we go back to waiting
                # on upstream — the invariant of ServiceFactory, stated once and applied here.
                async with services() as service:
                    paused = await service.process_event(run, block)
                processed += 1
                if paused is not None:
                    # Pause-at-zero (ADR-064 §3): the synthetic terminal block is ours, so nobody
                    # upstream will ever send it — it only reaches clients if we put it in the ring.
                    paused_seq = await bus.publish(run_id, epoch=ctx.epoch, raw=paused)
                    if paused_seq is not None:
                        beacon.last_published_seq = paused_seq
                    return ConsumerOutcome(
                        audit_event=None, terminal_seen=True, events_processed=processed
                    )
                if is_terminal_event(block):
                    terminal_seen = True
                beacon.set_state(BEACON_AWAITING_UPSTREAM)
    except httpx.HTTPError as exc:
        # Establishment failure and mid-stream death are ONE case (see ConsumerSubscriptionError).
        logger.warning("agent run consumer upstream error run_id=%s", run_id)
        raise ConsumerSubscriptionError("hermes events subscription lost") from exc
    if not terminal_seen:
        # The stream closed without a terminal event: the run's outcome is unknown to us, so we say
        # so and stop. Recording `failed` here would be a guess, and a wrong one is expensive — it
        # is conditional, so the real terminal status could never overwrite it afterwards.
        logger.warning(
            "agent run consumer stream ended with no terminal event run_id=%s events=%d",
            run_id,
            processed,
        )
        return ConsumerOutcome(
            audit_event=EVENT_CONSUMER_DISCONNECTED,
            terminal_seen=False,
            events_processed=processed,
        )
    return ConsumerOutcome(audit_event=None, terminal_seen=True, events_processed=processed)


def _bearer(api_key: str) -> dict[str, str]:
    """Instance bearer header. Never logged (ADR-003 redaction covers `*authorization*`)."""
    return {"Authorization": f"Bearer {api_key}"}


class SubscriptionHandshake:
    """Signals that the upstream subscription is ESTABLISHED — or that it never will be.

    ADR-067 §3 requires the consumer to be subscribed BEFORE the client is told the run started:
    the Hermes stream is one-shot, so whoever subscribes first is the only one who ever gets the
    history, and a client that beat us to it would leave the run unbilled and never finalized
    (TD-037). Waiting for a WORKER-OWNED milestone rather than for the task to merely exist is the
    only way to state that requirement — a started task proves nothing about the socket.

    Both outcomes must be signalled, and that is the whole subtlety: if only success were, then
    every failure — no lease, a refused connection, a non-2xx — would be indistinguishable from a
    slow subscription and the waiter would block for its entire timeout on a run that is already
    doomed. :meth:`mark_failed` is therefore also called from the consumer's finalizer as a
    backstop, so no waiter can outlive the consumer it is waiting for.

    Deliberately NOT an ``asyncio.Future`` carrying the exception: the caller's response is the same
    for every failure (ADR-067 §6.4.1 — return 202 anyway, audit, let the reaper finish the run), so
    a richer type would only offer a distinction the design forbids acting on.
    """

    def __init__(self) -> None:
        self._done = asyncio.Event()
        self._established = False

    def mark_established(self) -> None:
        """Called by the worker once upstream has answered 2xx and the stream is readable."""
        self._established = True
        self._done.set()

    def mark_failed(self) -> None:
        """Called on every path that ends before establishment. Idempotent, never downgrades."""
        self._done.set()

    @property
    def established(self) -> bool:
        return self._established

    async def wait(self, timeout: float) -> bool:  # noqa: ASYNC109
        """True once the subscription is established; False on failure or timeout.

        A timeout is NOT treated as failure of the run — only of our knowledge about it. The caller
        proceeds either way (§6.4.1: the 202 is still returned); the consumer keeps running and the
        inert-subscription guard (§6.4.2) remains the authority on a subscription that never speaks.

        ``ASYNC109`` (prefer ``asyncio.timeout`` at the call site) is suppressed deliberately: the
        convention assumes a timeout should propagate as an error, and here it must NOT. Pushing it
        out would make every call site wrap this in a ``try``/``except TimeoutError`` that swallows
        the exception — the same behaviour, restated once per caller, with one more chance for a
        caller to get it wrong and fail a run that is merely slow to start.
        """
        try:
            async with asyncio.timeout(timeout):
                await self._done.wait()
        except TimeoutError:
            return False
        return self._established


@dataclass
class ConsumerContext:
    """State shared by the working task and its supervisor for one run.

    ``epoch`` is mutable and that is the point: a Redis restart wipes the generation keys, the
    supervisor re-establishes them and stores the NEW generation here, and the worker picks it up on
    its next event. Publishing under the old generation instead would be invisible locally and fatal
    downstream — the broker drops events whose epoch does not match and the client's stream goes
    quiet for the rest of the run (§3.3.1).

    ``processing`` is the domain state of the run, published by the worker so the §6.4 finalizer can
    make the final snapshot flush; it is None until the worker has started.
    """

    epoch: str
    owner: str = ""
    beacon: ConsumerBeacon = field(default_factory=ConsumerBeacon)
    processing: Any = None
    # Set by the worker the moment upstream answers 2xx; awaited by whoever must not report the
    # run as started until we hold the stream (ADR-067 §3, stage-3 wiring).
    handshake: SubscriptionHandshake = field(default_factory=SubscriptionHandshake)


# How many beacon transitions with NO progress make a quiet stream look like a spinning loop
# (ADR-067 §6.1, last rule: "state менялся многократно"). The ADR gives no number. Four is two
# complete processing↔awaiting_upstream cycles: a single cycle is what one ordinary event produces,
# so a smaller value would flag a live run that emitted one event whose publication happened to fail
# (no seq, hence no progress). Together with the requirement that a full STALL window has elapsed,
# it describes a loop that keeps turning while the stream stands still.
_SPIN_TRANSITIONS: Final = 4

# Upper bound on the final snapshot flush of the §6.4 procedure. It exists because the most likely
# reason to be here is a consumer that wedged ON A DB WRITE — flushing again without a bound would
# wedge in the same place and never reach step 2 (drop the lease), which is the step that makes the
# run visible to the reaper. Saving the last few characters of text never outranks that.
_FINAL_FLUSH_TIMEOUT_SECONDS: Final = 5.0

EVENT_CONSUMER_MAX_DURATION: Final = "agent_run_consumer_max_duration"
EVENT_CONSUMER_STALLED: Final = "agent_run_consumer_stalled"
EVENT_CONSUMER_SHUTDOWN: Final = "agent_run_consumer_shutdown"


@dataclass
class _ProgressWindow:
    """Last observation of real progress, for the "loop spins but the stream stands still" rule."""

    bytes_read: int
    published_seq: int
    transitions: int
    at: float

    @classmethod
    def of(cls, beacon: ConsumerBeacon) -> _ProgressWindow:
        return cls(
            bytes_read=beacon.bytes_read,
            published_seq=beacon.last_published_seq,
            transitions=beacon.transitions,
            at=time.monotonic(),
        )

    def advanced(self, beacon: ConsumerBeacon) -> bool:
        """True when the STREAM moved — bytes or a published seq, never a mere state change."""
        return beacon.bytes_read > self.bytes_read or beacon.last_published_seq > self.published_seq


def is_stalled(beacon: ConsumerBeacon, window: _ProgressWindow, settings: Settings) -> bool:
    """The ADR-067 §6.1 liveness rule, verbatim. True = the working task is wedged.

    * ``awaiting_upstream`` is alive UNCONDITIONALLY — waiting on the outside world is normal and
      may last hours (an agent thinking inside one long tool call). Bounding it is exactly the idle
      timeout that was retracted in revision 2 for killing working runs.
    * ``processing`` and ``connecting`` are NOT exempt: legitimate work in our own code does not
      take minutes. ``connecting`` gets the first-byte threshold while no byte has ever arrived
      (§6.4.2 inert subscription), the processing threshold afterwards.
    * The one case a state alone cannot express: ``awaiting_upstream`` while the loop keeps turning
      and NOTHING is read. A fast reconnect loop would otherwise re-declare itself alive on every
      iteration for ever, which is why liveness is tied to observable progress and not to the loop.
    """
    now = time.monotonic()
    if beacon.state in (BEACON_PROCESSING, BEACON_CONNECTING):
        threshold = (
            settings.agent_run_first_byte_stall_seconds
            if beacon.state == BEACON_CONNECTING and not beacon.saw_first_byte
            else settings.agent_run_processing_stall_seconds
        )
        return now - beacon.since >= threshold
    return (
        not window.advanced(beacon)
        and beacon.transitions - window.transitions >= _SPIN_TRANSITIONS
        and now - window.at >= settings.agent_run_processing_stall_seconds
    )


async def run_supervisor(
    *,
    services: ServiceFactory,
    bus: AgentRunEventBus,
    settings: Settings,
    user_id: uuid.UUID,
    run_id: str,
    ctx: ConsumerContext,
    worker: asyncio.Task[ConsumerOutcome],
    started_at: float,
) -> str | None:
    """Renew the lease, stamp liveness, enforce the limits. Returns the §6.4 audit event, if any.

    Everything here is about the WORKING task, judged from outside it. That separation is the whole
    mechanism (ADR-067 §6.1): a task cannot be the witness of its own liveness, and a
    ``MAX_DURATION`` timer living inside a wedged coroutine would never fire — so the limit is
    applied by CANCELLING the worker from here.

    The supervisor never touches upstream or the domain rules, and it never writes a terminal run
    status: when it stops a run it reports WHY it stopped, and the reaper decides the run's outcome
    by one path for every cause (§6.4 step 4).
    """
    window = _ProgressWindow.of(ctx.beacon)
    interval = settings.agent_run_consumer_lease_renew_seconds
    while True:
        # Wait on the WORKER with a timeout rather than sleeping blind: a run that ends between two
        # ticks would otherwise keep its lease and heartbeat for the rest of the interval and spend
        # one more renewal on a finished run. ``asyncio.wait`` neither cancels the worker nor
        # consumes its exception — the TaskGroup still owns both.
        done, _ = await asyncio.wait({worker}, timeout=interval)
        if done:
            # The run ended on its own terms; nothing to supervise and nothing to report.
            return None

        if time.monotonic() - started_at >= settings.agent_run_max_duration_seconds:
            logger.warning("agent run consumer max duration reached run_id=%s", run_id)
            worker.cancel()
            return EVENT_CONSUMER_MAX_DURATION

        if is_stalled(ctx.beacon, window, settings):
            logger.warning(
                "agent run consumer stalled run_id=%s state=%s bytes=%d seq=%d",
                run_id,
                ctx.beacon.state,
                ctx.beacon.bytes_read,
                ctx.beacon.last_published_seq,
            )
            worker.cancel()
            return EVENT_CONSUMER_STALLED
        if window.advanced(ctx.beacon):
            window = _ProgressWindow.of(ctx.beacon)

        renewal = await bus.renew_lease(run_id, ctx.owner)
        if renewal is LeaseRenewal.LOST:
            # The key holds ANOTHER worker's id — evidence of a second owner. Standing down is the
            # only safe answer: two subscribers to a one-shot stream leave BOTH with nothing.
            logger.warning("agent run consumer lease lost run_id=%s", run_id)
            worker.cancel()
            return EVENT_CONSUMER_SHUTDOWN
        if renewal is LeaseRenewal.UNKNOWN:
            # Redis did not answer (ADR-067 §4.1). NOT a reason to cancel the working task: the run
            # is being consumed right now, its stream is one-shot and cannot be re-read, and nobody
            # can have taken the lease from us — an unreachable Redis hands it to no one. We simply
            # do not know, so we keep driving the run and ask again on the next tick; if Redis comes
            # back the next renewal returns REACQUIRED and the generation is re-established below.
            logger.warning("agent run consumer lease renewal unknown run_id=%s", run_id)
        if renewal is LeaseRenewal.REACQUIRED:
            # The lease key had died (Redis restart / FLUSHDB) and we took it back. The generation
            # keys died with it; re-establishing them is the unconditional step below.
            logger.warning("agent run consumer re-acquired its lease run_id=%s", run_id)

        # §3.3.1b measure 1 (TD-045): ensure the generation on EVERY tick, not only after a
        # REACQUIRED. The lease and the ring keys expire on unrelated clocks — the ring's TTL runs
        # from the last EVENT (only the event pipeline refreshes it), while the lease is renewed
        # every 10s regardless — so a run that stays quiet past
        # AGENT_RUN_EVENT_BUFFER_TTL_SECONDS loses `epoch`/`seq` while keeping its lease. Renewal
        # then returns RENEWED, nothing re-creates the generation, and the next event is published
        # under the OLD epoch with `seq` restarting at 1: to an open client session the generations
        # match, the event is discarded as stale, and the stream goes silent for good. Worse, it is
        # not merely a lost event — `id: <epoch>-<seq>` stops being unique within a generation, so
        # the client's cursor becomes permanently ambiguous, which no broker-side safety net can
        # repair. The cost of preventing it is one SET NX + GET per renewal tick: the key exists →
        # no-op; the key is gone → a NEW generation, and the change travels the ordinary §3.3.1
        # path (marker + reset) instead of vanishing.
        fresh = await bus.ensure_epoch(run_id)
        if fresh is not None and fresh != ctx.epoch:
            logger.warning("agent run consumer adopted a new ring generation run_id=%s", run_id)
            ctx.epoch = fresh

        # Heartbeat ONLY while the liveness rule above holds — a stamp from a wedged consumer is
        # the false claim the whole two-task structure exists to prevent.
        try:
            async with services() as service:
                stamped = await service.consumer_heartbeat(user_id=user_id, run_id=run_id)
            if not stamped:
                logger.warning(
                    "agent run consumer heartbeat matched no snapshot row run_id=%s", run_id
                )
        except SQLAlchemyError:
            # A failed heartbeat is not fatal: it only brings the orphan deadline closer, and the
            # reaper is the correct authority for a consumer that cannot talk to the DB.
            logger.warning("agent run consumer heartbeat failed run_id=%s", run_id)


async def run_consumer(
    *,
    services: ServiceFactory,
    bus: AgentRunEventBus,
    settings: Settings,
    endpoint: InstanceEndpoint,
    user_id: uuid.UUID,
    run_id: str,
    handshake: SubscriptionHandshake | None = None,
) -> None:
    """Drive one run end to end: lease → generation → worker + supervisor → §6.4 finalization.

    The two tasks live in ONE ``TaskGroup``, so the death or cancellation of either cancels the
    other (ADR-067 §6.1, mandatory). Without that invariant a dead supervisor with a live worker
    produces the worst outcome the design has: the heartbeat stops, the reaper finalizes the run as
    an orphan — ``failed`` plus a debit computed from an INCOMPLETE cumulative under
    ``idempotency_key=runId`` — and when the real ``run.completed`` arrives, the worker's proper
    finalization is discarded as a duplicate under that same key, while the matching
    ``_mark_terminal('completed')`` is a no-op against the conditional transition. The undercharge
    and the wrong status both become permanent, and nothing reports either.

    Returns quietly ONLY when another worker demonstrably holds the lease: the run has a consumer,
    and a second subscription would consume the one-shot stream and leave both with nothing. An
    unreachable Redis is NOT that case — see below.
    """
    owner = uuid.uuid4().hex
    acquisition = await bus.acquire_lease(run_id, owner)
    if acquisition is LeaseAcquisition.HELD_ELSEWHERE:
        logger.info("agent run consumer skipped, lease held elsewhere run_id=%s", run_id)
        if handshake is not None:
            # Another worker already drives this run — for the caller that is a SUCCESS of the
            # invariant it cares about ("someone holds the stream"), not a failure, but it is not
            # OUR subscription, so it is reported as not established rather than waited out.
            handshake.mark_failed()
        return
    if acquisition is LeaseAcquisition.UNKNOWN:
        # Redis did not answer, so we hold NO lease — and we drive the run anyway (ADR-067 §4.1).
        # Mutual exclusion is not what makes this safe; the architecture is: the client never
        # subscribes to Hermes (§1), a consumer is started only from run()/resume() on a freshly
        # returned run_id, and ConsumerRegistry is idempotent per run_id — so with Redis down the
        # exclusion loses no participant that could exist. What IS lost is named: the live fan-out
        # (ring and pub/sub), for which clients get run.truncated once Redis returns. Billing, the
        # snapshot, the heartbeat and the terminal status all live in Postgres and are unaffected —
        # and the sweep cannot falsely finalize this run, because its heartbeat holds it and the
        # sweep does not run at all while Redis is unreachable (§5, fail-closed).
        logger.warning("agent run consumer proceeding without a lease run_id=%s", run_id)
    epoch = await bus.ensure_epoch(run_id)
    if epoch is None:
        # Redis is unreachable. The run can still be billed and finalized (Postgres holds all of
        # that), it simply has no live fan-out — so we proceed with a generation of our own rather
        # than abandoning the run to the reaper.
        epoch = uuid.uuid4().hex
        logger.warning("agent run consumer starting without a redis generation run_id=%s", run_id)
    ctx = ConsumerContext(epoch=epoch, owner=owner)
    if handshake is not None:
        ctx.handshake = handshake

    started_at = time.monotonic()
    audit_event: str | None = None
    try:
        try:
            # INSIDE the try (TD-043): creating the snapshot row is the consumer's first DB call and
            # it can fail. Outside, its failure skipped §6.4 whole — the lease stayed held (so the
            # reaper would not touch the run for a full ORPHAN_TIMEOUT), no waiter was released,
            # nothing was audited — and it produced exactly the run WITHOUT A SNAPSHOT ROW that §5.2
            # calls a revenue incident. §6.4 covers the consumer's ENTIRE life, from the lease
            # attempt onwards.
            async with services() as service:
                await service.prepare_consumer_snapshot(user_id=user_id, run_id=run_id)
            async with asyncio.TaskGroup() as tg:
                worker = tg.create_task(
                    run_worker(
                        services=services,
                        bus=bus,
                        settings=settings,
                        endpoint=endpoint,
                        user_id=user_id,
                        run_id=run_id,
                        ctx=ctx,
                    )
                )
                supervisor = tg.create_task(
                    run_supervisor(
                        services=services,
                        bus=bus,
                        settings=settings,
                        user_id=user_id,
                        run_id=run_id,
                        ctx=ctx,
                        worker=worker,
                        started_at=started_at,
                    )
                )
            audit_event = supervisor.result()
            outcome = worker.result()
            audit_event = audit_event or outcome.audit_event
        except* ConsumerSubscriptionError:
            # Establishment failure or a lost subscription — one case, no retry (§6.4.1).
            audit_event = EVENT_CONSUMER_FAILED
        except* SQLAlchemyError:
            # A database failure anywhere in the consumer's life, INCLUDING the snapshot row it
            # creates before subscribing (TD-043) — ``except*`` catches the bare exception as
            # readily as a group, so the pre-TaskGroup call above is covered by the same clause.
            # The ending is of the same class as a subscription that never came up: the consumer
            # cannot drive the run, so it says why it stopped and lets the finalizer below run
            # §6.4 — release the lease, free the waiter, audit — instead of skipping it entirely.
            logger.warning("agent run consumer stopped on a database error run_id=%s", run_id)
            audit_event = EVENT_CONSUMER_FAILED
        except* asyncio.CancelledError:
            # The worker was cancelled by the supervisor (limit/stall) or the whole consumer was
            # cancelled at worker shutdown. The supervisor's verdict, when it has one, is more
            # specific.
            audit_event = audit_event or EVENT_CONSUMER_SHUTDOWN
            # We are SWALLOWING a cancellation, so the request to cancel must be retracted as well.
            # Without ``uncancel`` the task returns normally while asyncio still counts a pending
            # cancellation: ``task.cancelled()`` is False (so the registry treats the ending as
            # ordinary) but ``cancelling()`` is not zero, and any TaskGroup that later encloses this
            # coroutine would read that leftover as a cancellation of its own and act on it.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    except Exception:
        # Anything the three clauses above did not name — and the point is only to make sure it is
        # RECORDED. §6.4 step 3 requires the consumer to say why it stopped; without this, an
        # unexpected failure reached the finalizer with ``audit_event is None``, which the finalizer
        # reads as "ended normally": the lease was dropped and the run went to the reaper with no
        # trace of a failure anywhere. Re-raised immediately, so the registry still logs and reports
        # it exactly as before — this clause changes the record, never the propagation.
        #
        # ⚠️ ``Exception``, NOT ``BaseException``: the cancellability invariant of ADR-067 §3.2.1
        # (axis D, D1 (2)) forbids the wider form in a hot-path task: a clause that can see a
        # ``CancelledError`` is one edit away from swallowing it, and a cancellation that can be
        # swallowed is not a cancellation. Cancellation of this coroutine is already handled — and
        # deliberately retracted — by the ``except*`` clause above; ``KeyboardInterrupt`` and
        # ``SystemExit`` intentionally pass through unrecorded, since the process is going away and
        # the run passes to the reaper by the same route as an abrupt restart.
        audit_event = audit_event or EVENT_CONSUMER_FAILED
        raise
    finally:
        await _finalize(
            services=services,
            bus=bus,
            user_id=user_id,
            run_id=run_id,
            ctx=ctx,
            audit_event=audit_event,
        )


async def _finalize(
    *,
    services: ServiceFactory,
    bus: AgentRunEventBus,
    user_id: uuid.UUID,
    run_id: str,
    ctx: ConsumerContext,
    audit_event: str | None,
) -> None:
    """The ADR-067 §6.4 self-termination procedure, in its mandatory order.

    1. final snapshot flush — best-effort AND time-bounded;
    2. **drop the lease — unconditionally, even if step 1 failed or timed out.** This is what makes
       the run visible to the reaper; skipping it on an error would leave a run nobody drives and
       nobody finalizes, which is the ``running``-for-ever defect this ADR exists to remove;
    3. audit why the consumer stopped;
    4. NO terminal status and NO billing finalization — the consumer does not know the run's
       outcome, and a wrong ``failed`` is irreversible (the transition is conditional, so the real
       terminal status could never overwrite it).
    """
    # Backstop: a consumer that is finishing will never establish anything, so release any waiter
    # now. Idempotent — a subscription that DID come up has already marked itself established.
    ctx.handshake.mark_failed()
    if ctx.processing is not None:
        try:
            async with asyncio.timeout(_FINAL_FLUSH_TIMEOUT_SECONDS):
                async with services() as service:
                    await service.flush_run_snapshot(ctx.processing)
        except (TimeoutError, SQLAlchemyError):
            logger.warning("agent run consumer final flush failed run_id=%s", run_id)
    await bus.release_lease(run_id, ctx.owner)
    if audit_event is None:
        return
    try:
        async with services() as service:
            await service.record_consumer_event(
                user_id=user_id, run_id=run_id, event_type=audit_event
            )
    except SQLAlchemyError:
        # The lease is already gone, so the run is the reaper's regardless of this record.
        logger.warning("agent run consumer audit failed run_id=%s event=%s", run_id, audit_event)


class ConsumerRegistry:
    """Application-scoped owner of the live consumer tasks (ADR-067 §6.1.1).

    ⚠️ Fire-and-forget is forbidden, for three independent reasons and any one of them is enough on
    a money path:

    * a task with no strong reference can be garbage-collected mid-flight — asyncio only keeps weak
      references, so a consumer could simply vanish, taking the run's billing with it;
    * ``§6.4`` lists "cancellation at worker shutdown" as a self-termination trigger, and with
      nobody holding the tasks there is NOBODY TO CANCEL them: no final flush, no lease release, no
      audit, and the run waits out ``LEASE_TTL + ORPHAN_TIMEOUT`` instead of passing to the reaper
      at once;
    * an exception inside a detached task disappears silently.

    It lives at APPLICATION level, not inside a request-scoped service: a handler ASKS for a
    consumer to be started, but must not own a task that outlives its request by up to two hours.
    The count of live tasks is also the natural place to observe concurrency (Q-067-2).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, run_id: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Register and start a consumer for ``run_id``. An existing one is left alone.

        Idempotent per run: a duplicate start would race for the lease and, worse, could win it
        from our own live consumer — and a second subscription to a one-shot stream leaves BOTH
        with nothing. The caller's coroutine is closed rather than leaked when it is not needed.
        """
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            coro.close()
            return existing
        task = asyncio.create_task(coro, name=f"agent-run-consumer:{run_id}")
        self._tasks[run_id] = task
        # Published on every change rather than polled: this is one of the two numbers by which the
        # contour's three shared ceilings (DB pool, Redis subscriptions, ring memory) become
        # observable BEFORE one of them is hit — the precondition of Q-067-2 (ADR-067 §6.1.1).
        agent_run_consumers_active.set(self.active)
        # Self-removal keeps the registry from growing with run history; it fires for every ending,
        # including cancellation, so a drained registry really is empty.
        task.add_done_callback(functools.partial(self._forget, run_id))
        return task

    def _forget(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            del self._tasks[run_id]
        agent_run_consumers_active.set(self.active)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # The one place a detached-task exception can still be seen. Never re-raised: one run's
            # failure must not touch the others or the worker.
            logger.warning(
                "agent run consumer task ended with an exception run_id=%s error=%s",
                run_id,
                type(exc).__name__,
            )

    @property
    def active(self) -> int:
        """Live consumer count. Mirrored into ``agent_run_consumers_active`` on every change.

        ⚠️ It used to be a property nobody read, which made it observability in name only: the number
        existed and no operator could see it. Q-067-2 asks for a cap on concurrent runs, and a cap
        cannot be chosen for a quantity that is never measured — so publishing this is the
        precondition of that question, not its answer.
        """
        return sum(1 for t in self._tasks.values() if not t.done())

    async def drain(self, timeout: float) -> int:  # noqa: ASYNC109
        """Cancel every consumer and wait for their §6.4 procedures. Returns how many were drained.

        ⚠️ MUST run BEFORE the DB pool and the Redis client are closed (ADR-067 §6.1.1). In the
        other order §6.4 can neither flush the final snapshot nor release the lease — every orderly
        restart would degrade into the abrupt case, leaving runs to wait out the orphan timeout.

        The budget is a wall clock bound for ALL of them, not per run: the procedures run
        concurrently. Consumers still running when it expires are abandoned — the lease then simply
        expires on its own and the reaper finalizes the run, which is the designed fallback.

        ``ASYNC109`` is suppressed for the same reason as in :meth:`SubscriptionHandshake.wait`:
        the budget must NOT propagate as an error. Shutdown continues either way, and expiry is a
        logged degradation, not a failure to raise into the lifespan.
        """
        tasks = [t for t in self._tasks.values() if not t.done()]
        if not tasks:
            return 0
        logger.info("draining %d agent run consumer(s)", len(tasks))
        for task in tasks:
            task.cancel()
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                "agent run consumer drain timed out with %d still running; their leases will "
                "expire and the reaper will finalize those runs",
                len(pending),
            )
        return len(tasks)


class ConsumerLauncher:
    """Starts a background consumer for a run and reports when its subscription is up.

    The ONE dependency the request path needs in order to satisfy "the consumer is subscribed
    before the client is told the run started" (ADR-067 §3). It exists so ``AgentProxyService`` —
    which is per-request — never has to hold the pieces that outlive a request (the task registry,
    a session factory, the Redis client): a handler ASKS for a consumer, it does not own one.
    """

    def __init__(
        self,
        *,
        registry: ConsumerRegistry,
        services: ServiceFactory,
        bus: AgentRunEventBus,
        settings: Settings,
    ) -> None:
        self._registry = registry
        self._services = services
        self._bus = bus
        self._settings = settings

    async def start_and_wait(
        self, *, user_id: uuid.UUID, run_id: str, endpoint: InstanceEndpoint
    ) -> bool:
        """Start the consumer, wait briefly for its subscription. True if it came up in time.

        The wait is bounded by ``AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS`` and its expiry is NOT an
        error: the handler answers 202 either way (ADR-067 §6.4.1) and the consumer keeps going.
        What the wait buys is OBSERVABILITY — a subscription that never came up is visible now
        instead of after ``AGENT_RUN_ORPHAN_TIMEOUT``. It is deliberately not the first-byte
        threshold: the events stream has no read timeout (§6.2), so a peer that accepts the
        connection and sends nothing would otherwise hold the request open indefinitely.

        A False result is returned, never raised. Every reason for it — no lease, a refused
        connection, a non-2xx, a slow start — leads to the same behaviour by design, so a caller
        able to distinguish them could only be tempted to act on a distinction the ADR forbids.
        """
        handshake = SubscriptionHandshake()
        self._registry.start(
            run_id,
            run_consumer(
                services=self._services,
                bus=self._bus,
                settings=self._settings,
                endpoint=endpoint,
                user_id=user_id,
                run_id=run_id,
                handshake=handshake,
            ),
        )
        established = await handshake.wait(self._settings.agent_run_handshake_timeout_seconds)
        if not established:
            # Not fatal, but never silent: this is the only moment where "nobody took the stream"
            # is cheap to notice. The run still gets its 202; the reaper is its safety net.
            logger.warning(
                "agent run consumer did not report a subscription in time run_id=%s", run_id
            )
        return established


EVENT_ORPHAN_FINALIZED: Final = "agent_run_orphan_finalized"


async def sweep_orphan_runs(
    *, services: ServiceFactory, bus: AgentRunEventBus, settings: Settings
) -> int:
    """Finalize runs whose consumer is gone (ADR-067 §5). Returns how many were finalized.

    A candidate must satisfy ALL THREE conditions; each rules out a different class of false
    positive, and two of them are not enough:

    1. **no live lease** — nobody is driving the run;
    2. **stale heartbeat** — ``COALESCE(consumer_heartbeat_at, created_at)`` older than
       ``AGENT_RUN_ORPHAN_TIMEOUT_SECONDS``. "No lease" alone is insufficient: a Redis restart wipes
       every lease at once;
    3. **Redis has been up longer than the grace** — ``INFO server → uptime_in_seconds``. Without
       it, the sweep immediately after a restart would take every active run for an orphan.

    ⚠️ FAIL-CLOSED throughout, and note this is the OPPOSITE reading of an unknown lease from the
    broker's. There, uncertainty must not close a client stream; here, uncertainty must not finalize
    a run, because the error is IRREVERSIBLE: a debit from an incomplete cumulative under
    ``idempotency_key=runId`` plus a ``failed`` status that the real terminal transition can never
    overwrite (it is conditional). So an unreadable ``INFO`` skips the whole tick, and a lease probe
    that returns ``None`` skips that run. Both readings are correct in their place — the cost of
    being wrong differs.
    """
    uptime = await bus.uptime_seconds()
    if uptime is None or uptime < settings.agent_run_orphan_redis_grace_seconds:
        # Condition 3 unmet or unknowable: no sweep at all this tick (not "sweep anyway").
        return 0
    cap = settings.agent_run_orphan_max_per_tick
    async with services() as service:
        candidates = await service.list_orphan_candidates(
            timeout_seconds=settings.agent_run_orphan_timeout_seconds, limit=cap
        )
    if not candidates:
        return 0
    if len(candidates) >= cap:
        # Hitting the cap means "investigate", not "keep going": a healthy deployment finalizes a
        # handful of orphans, not a capped batch every tick.
        logger.warning("agent run orphan sweep hit its per-tick cap (%d) — investigate", cap)
    finalized = 0
    for row in candidates:
        run_id = str(row["run_id"])
        if await bus.lease_alive(run_id) is not False:
            # Condition 1 unmet, or unknown (None) — see the fail-closed note above.
            continue
        try:
            async with services() as service:
                await service.finalize_orphan_run(
                    user_id=row["user_id"],
                    run_id=run_id,
                    input_tokens=int(row["input_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                    # ADR-067 §5.2: forwarded, not derived from the token counts — zero tokens
                    # cannot say whether anything was ever measured, and that is the whole
                    # distinction between a free run and a run nobody consumed.
                    snapshot_present=bool(row["snapshot_present"]),
                    # §5 step 3: audited as the age the sweep actually decided on, hence taken
                    # from the query's own clock rather than recomputed here.
                    heartbeat_age_seconds=float(row["heartbeat_age_seconds"]),
                )
            finalized += 1
        except SQLAlchemyError:
            # One run's failure must not abort the batch; the next tick retries it (no-op if it
            # was in fact finalized, both writes being idempotent/conditional).
            logger.warning("agent run orphan finalization failed run_id=%s", run_id)
    return finalized
