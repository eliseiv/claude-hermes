"""Shared harness for the ADR-067 background consumer: fake upstream, session counter, lifespan.

Extracted rather than kept local because three different stages need the same three things, and
each of them is the kind of scaffolding that is subtly wrong the second time it is written:

* :class:`FakeUpstream` — a REAL local HTTP server speaking SSE, whose response can be sliced into
  arbitrary chunks and held open. Chunk control is not a luxury: the property "``bytes_read`` grows
  on a PARTIAL block" is only observable if a single SSE event can be split across two writes, and
  no mock of ``httpx`` can honestly stand in for that (the consumer's byte accounting hangs off
  ``aiter_bytes``, i.e. off the socket).
* :class:`SessionCounter` — counts sessions that are OPEN AT THE SAME TIME, which is the only way
  to state "a session is never held across an await on upstream". Counting opens would pass on the
  very implementation the invariant forbids.
* :func:`consumer_settings` — one place where the ADR-067 knobs are shrunk to test scale while
  still satisfying the config invariants (renew < ttl, heartbeat < orphan, grace > renew), which
  are easy to violate accidentally when overriding one knob at a time.
* :func:`await_consumer` — the ONLY valid way to assert that a consumer stopped by itself.
  ``asyncio.wait_for`` cannot express it, and silently passes when it does not hold; the reason is
  in that function's docstring and is worth reading before writing any consumer-lifecycle test.

Everything here is inert unless a test uses it; nothing touches global state.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings


# ==================================================================================================
# Fake upstream: a real socket that speaks SSE on OUR schedule.
# ==================================================================================================
@dataclass
class UpstreamScript:
    """What the fake upstream should do for one request.

    ``chunks`` are written verbatim, in order, with ``delay`` seconds between them — so an SSE block
    may be split across several entries, which is the whole point. ``hold_open`` keeps the response
    unfinished afterwards, modelling a run that is still thinking.
    """

    chunks: Sequence[bytes] = ()
    delay: float = 0.0
    status: int = 200
    hold_open: bool = False
    # Delay before the response STATUS line — models a slow instance without stalling the socket.
    initial_delay: float = 0.0


class FakeUpstream:
    """A local HTTP server standing in for a Hermes instance's ``/v1/runs/{id}/events``.

    Deliberately a real server rather than a transport double: the consumer's liveness accounting
    (``bytes_read``), its TCP keep-alive options and its streaming timeouts are all properties of an
    actual socket, and a double would assert the author's model of them instead.
    """

    def __init__(self, script: UpstreamScript) -> None:
        self._script = script
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self.base_url = ""
        self.requests: list[bytes] = []

    async def __aenter__(self) -> FakeUpstream:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Cancel live handlers instead of waiting for them.

        ``Server.wait_closed()`` blocks until every handler returns, and these deliberately never
        do (``hold_open`` models a run in progress) — awaiting it would hang the suite, which is
        exactly how an earlier module in this project deadlocked.
        """
        for writer in self._writers:
            with contextlib.suppress(Exception):
                writer.close()
        for task in self._tasks:
            task.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(request)
            script = self._script
            if script.initial_delay:
                await asyncio.sleep(script.initial_delay)
            writer.write(
                f"HTTP/1.1 {script.status} OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n"
                "Transfer-Encoding: chunked\r\n\r\n".encode()
            )
            await writer.drain()
            for chunk in script.chunks:
                writer.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
                if script.delay:
                    await asyncio.sleep(script.delay)
            if script.hold_open:
                await asyncio.sleep(3600)
            else:
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (
            asyncio.CancelledError,
            ConnectionResetError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
        ):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def sse_chunks_split_first_event(text: str, at: int) -> list[bytes]:
    """One SSE event split into two chunks at byte ``at`` — the partial-block case.

    A block that arrives in two socket reads is the only way to observe that byte accounting is tied
    to BYTES RECEIVED rather than to events parsed. If it were tied to events, a stream that
    delivered half an event and then stopped would look like it had read nothing at all, and the
    inert-subscription guard would fire on a subscription that is demonstrably alive.
    """
    raw = f"data: {text}\n\n".encode()
    assert 0 < at < len(raw), "the split must fall strictly inside the event"
    return [raw[:at], raw[at:]]


# ==================================================================================================
# Waiting for a consumer to stop — and why `asyncio.wait_for` MUST NOT be used for it.
# ==================================================================================================
async def await_consumer(consumer: asyncio.Task[None], *, budget: float) -> bool:
    """Poll a ``run_consumer`` task for completion. True if it stopped within ``budget`` seconds.

    ⚠️ **Never use ``asyncio.wait_for`` to assert that a consumer stopped on its own.** It is not a
    style preference, it silently voids the assertion:

    ``run_consumer`` catches ``CancelledError`` (``except*``) and does NOT re-raise it. That is
    deliberate: the §6.4 procedure must still run when a worker is cancelled at shutdown. So the
    ``wait_for`` cancels the task, the task then finishes *normally*, and — because §6.4 runs on
    that path too — it writes the very ``agent_run_consumer_*`` audit record the test was looking
    for. The ``TimeoutError`` never surfaces and the test passes on an implementation that never
    stopped at all.

    This was found the hard way: a test asserting "a foreign lease owner stops the run" passed with
    the supervisor's entire ``LOST`` branch disabled. Polling for ``done()`` and never cancelling
    inside the measurement is the only oracle that distinguishes "it stopped" from "we stopped it".

    The caller keeps ownership: on a False result the task is still running and must be cancelled
    in the caller's cleanup, since cancelling here would recreate the very ambiguity above.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if consumer.done():
            await consumer  # surface whatever it raised, rather than an opaque False
            return True
        await asyncio.sleep(0.05)
    return False


# ==================================================================================================
# Session accounting.
# ==================================================================================================
@dataclass
class SessionCounter:
    """Tracks CONCURRENTLY open sessions, not the number of opens.

    The invariant under test is "a session is never held across an ``await`` on upstream". Counting
    opens cannot express it — a consumer holding one session for a whole run opens exactly one and
    would look perfect. The peak of simultaneously-open sessions is what distinguishes the two.
    """

    opened: int = 0
    closed: int = 0
    peak_concurrent: int = 0
    _live: int = field(default=0, repr=False)

    def enter(self) -> None:
        self.opened += 1
        self._live += 1
        self.peak_concurrent = max(self.peak_concurrent, self._live)

    def exit(self) -> None:
        self.closed += 1
        self._live -= 1

    @property
    def live(self) -> int:
        return self._live


def counting_services(factory: Callable[[], Any], counter: SessionCounter) -> Callable[[], Any]:
    """Wrap a ``ServiceFactory`` so every session it hands out is counted while open."""

    @contextlib.asynccontextmanager
    async def _services() -> AsyncIterator[Any]:
        counter.enter()
        try:
            async with factory() as service:
                yield service
        finally:
            counter.exit()

    return _services


# ==================================================================================================
# Settings at test scale, satisfying every ADR-067 config invariant.
# ==================================================================================================
def consumer_settings(*, redis_url: str, redis_db: int, **overrides: Any) -> Settings:
    """ADR-067 knobs shrunk for tests, with the interdependent ones kept mutually valid.

    The validator rejects several combinations that look individually reasonable (renew >= ttl,
    heartbeat >= orphan, grace <= renew). Overriding one knob per test and rediscovering that each
    time is how a test ends up asserting a ``ValidationError`` it never meant to provoke.
    """
    base: dict[str, Any] = {
        "REDIS_URL": redis_url,
        "AGENT_RUN_REDIS_DB": redis_db,
        "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS": 30,
        "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS": 1,
        "AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS": 1,
        "AGENT_RUN_ORPHAN_TIMEOUT_SECONDS": 900,
        "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS": 2,
        "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS": 60,
        "AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS": 5.0,
        "AGENT_RUN_SHUTDOWN_DRAIN_SECONDS": 5.0,
        "AGENT_RUN_PROCESSING_STALL_SECONDS": 120,
        "AGENT_RUN_FIRST_BYTE_STALL_SECONDS": 180,
        "AGENT_RUN_MAX_DURATION_SECONDS": 7200,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
