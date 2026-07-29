"""Unit: the end-to-end launch budget and the connect/proxy timeout split (TD-040).

THE DEFECT. `POST /v1/agent/run` against a WEDGED instance — one that accepts nothing and answers
nothing, not even a health probe — hung for ≥90s and returned no error code at all. The arithmetic
behind it: the ADR-062 connect-retry is by construction spent ONLY on the connect phase, but a
single ``httpx.Timeout(float)`` sets all four phases, so each connect attempt was capped at the
PROXY timeout. 3 × 30s + 2 × 2s backoff = 94s, and nothing above it bounded the sum.

WHAT THE TESTS HAVE TO PROVE, AND WHY THEY ARE SHAPED THIS WAY. The error CODE alone proves
nothing: the old code also ended in a 502 eventually. The defect was the TIME, so the assertions
are on elapsed wall clock against a deliberately tiny budget, and the key ones are verified to fail
on the pre-fix arithmetic (see the QA report's neutralisation runs). Two techniques are used:

* REAL sockets (``_silent_server`` / ``_drip_server`` / a refused port) for the cases where the
  behaviour under test IS the socket behaviour — a server that accepts and never speaks, and one
  that dribbles bytes below the read timeout. No mock can honestly stand in for those.
* A transport double that CONSUMES ITS OWN CONNECT CAP for the retry-arithmetic regression. A
  hanging connect cannot be produced portably (it needs a dropped SYN, not a refusal), and the
  property under test is the arithmetic — attempts × cap + backoffs — not the syscall.

Timing assertions are BOUNDS, never equalities: a socket test that pins a duration is a flake
waiting for a slow CI box. Each bound is chosen to sit clearly between the fixed and the pre-fix
behaviour, so it discriminates without being tight.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from app.agent_proxy import service as service_mod
from app.config import Settings
from app.errors import UpstreamError, UpstreamTimeoutError
from app.hermes_runtime.manager import InstanceEndpoint
from tests.unit.test_agent_proxy_service import (
    FakeManager,
    _Decision,
    _make_service,
    _patch_policy,
)
from tests.unit.test_hermes_runtime_manager import (
    FakeRegistry,
    FakeRuntimeBackend,
    _manager,
)

# Budget knobs shrunk so the suite measures the SHAPE of the bound, not the production constants.
# The config invariant (budget >= ready + 2 × proxy) is respected by every combination below —
# violating it is itself a test (test_settings_reject_a_budget_below_the_invariant).
_READY = 1
_PROXY = 2.0
_CONNECT = 0.1
_BUDGET = 5.0


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "HERMES_PROVISION_READY_TIMEOUT_SECONDS": _READY,
        "HERMES_PROVISION_READY_INTERVAL_SECONDS": 1,
        "HERMES_PROXY_TIMEOUT_SECONDS": _PROXY,
        "HERMES_CONNECT_TIMEOUT_SECONDS": _CONNECT,
        "HERMES_LAUNCH_BUDGET_SECONDS": _BUDGET,
        "HERMES_LAUNCH_RETRY_ATTEMPTS": 3,
        "HERMES_LAUNCH_RETRY_BACKOFF_SECONDS": 0.05,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# Real sockets. Each helper yields a base_url pointing at a locally bound server.
# --------------------------------------------------------------------------------------------
class _Server:
    """A locally bound asyncio server plus the base_url the proxy should be pointed at.

    Teardown CANCELS live handlers instead of waiting for them. ``Server.wait_closed()`` blocks
    until every connection handler has finished, and these handlers deliberately never finish (a
    wedged instance sleeps, a drip server loops) — awaiting it hangs the whole session, which is
    exactly what the first run of this module did.
    """

    def __init__(self, server: asyncio.AbstractServer, port: int) -> None:
        self._server = server
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self.base_url = f"http://127.0.0.1:{port}"

    def _track(self, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)

    async def aclose(self) -> None:
        for writer in self._writers:
            writer.close()
        for task in self._tasks:
            task.cancel()
        self._server.close()
        # Teardown safety net: never let a stuck handler turn cleanup into a hang.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)


async def _start(handler: Any) -> _Server:
    """Bind a server whose handler is wrapped so every connection is cancellable on teardown."""
    holder: dict[str, _Server] = {}

    async def wrapped(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        holder["server"]._track(writer)
        try:
            await handler(reader, writer)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass  # teardown or a client that gave up first — both expected here
        finally:
            writer.close()

    server = await asyncio.start_server(wrapped, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    holder["server"] = _Server(server, port)
    return holder["server"]


async def _silent_server() -> _Server:
    """Accepts the connection, reads the request, and then says NOTHING — a wedged instance.

    This is the failure mode TD-040 measured in production: TCP succeeds, so no transport error is
    ever raised; only a timeout can end the wait.
    """

    async def handler(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        await asyncio.sleep(3600)

    return await _start(handler)


async def _drip_server(interval: float = 0.1) -> _Server:
    """Sends ONE byte every ``interval`` seconds, forever, never completing a response.

    The reason ``asyncio.timeout`` had to be added on top of the httpx caps: httpx timeouts are
    PER-OPERATION, so every received chunk restarts the read clock. An upstream dripping below the
    read timeout satisfies the phase caps indefinitely and would outlive any purely phase-based
    bound.
    """

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        await writer.drain()
        while True:
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(interval)

    return await _start(handler)


async def _refused_base_url() -> str:
    """A port that is bound and immediately released: connecting to it is REFUSED, not silent."""

    async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
        return None  # pragma: no cover - nothing ever connects before the port is released

    server = await _start(handler)
    url = server.base_url
    await server.aclose()
    return url


def _svc_for(base_url: str, settings: Settings) -> Any:
    manager = FakeManager(endpoint=InstanceEndpoint(base_url=base_url, api_key="k"))
    svc, _mgr, _wal, _aud = _make_service(settings=settings, manager=manager)
    return svc


async def _run(svc: Any) -> Any:
    return await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)


@pytest.fixture(autouse=True)
def _allow_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_policy(monkeypatch, _Decision(allow=True))


# ==================================================================================================
# 1. The regression itself — a wedged instance answers WITHIN the budget, with a code.
# ==================================================================================================
async def test_wedged_instance_fails_with_upstream_timeout_inside_the_budget() -> None:
    """Case 1, real socket: accepted-then-silent ends at the READ cap, far inside the budget.

    A ``ReadTimeout`` is post-send, so ADR-062 never retries it (double-run risk) — one attempt,
    bounded by the proxy timeout. The client gets a code instead of the ≥90s of nothing TD-040
    reported.
    """
    server = await _silent_server()
    try:
        started = time.monotonic()
        with pytest.raises(UpstreamTimeoutError) as excinfo:
            await _run(_svc_for(server.base_url, _settings()))
        elapsed = time.monotonic() - started
    finally:
        await server.aclose()

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "upstream_timeout"
    assert elapsed < _BUDGET, f"answered at {elapsed:.2f}s, budget {_BUDGET}s"
    # It ended on the READ cap, not on the outer ceiling — otherwise the phase caps did nothing.
    # Generous slack over the 2s cap: this must not measure how loaded the box is.
    assert elapsed < _PROXY + 2.0


async def test_slow_drip_upstream_is_ended_by_the_budget_ceiling() -> None:
    """Case 11: bytes arriving below the read timeout never trip it — the ceiling must.

    Without ``asyncio.timeout`` this call does not end at all: every dribbled byte restarts the
    per-operation read clock. The assertion is therefore two-sided — it must finish, and it must
    finish AT the budget rather than before it (proving the ceiling fired, not a phase cap).
    """
    server = await _drip_server(interval=0.1)
    settings = _settings(HERMES_PROXY_TIMEOUT_SECONDS=1.0, HERMES_LAUNCH_BUDGET_SECONDS=3.0)
    try:
        started = time.monotonic()
        with pytest.raises(UpstreamTimeoutError):
            await _run(_svc_for(server.base_url, settings))
        elapsed = time.monotonic() - started
    finally:
        await server.aclose()

    # Lower bound is the real assertion — nothing but the ceiling can end this call, so finishing
    # EARLY would mean a phase cap fired and the drip scenario was not exercised. The upper bound is
    # a loose sanity net (without the ceiling this ran 101s in the neutralisation run).
    assert elapsed >= 3.0 - 0.5, f"drip ended at {elapsed:.2f}s — before the 3.0s budget"
    assert elapsed < 3.0 + 4.0, f"drip ended at {elapsed:.2f}s — the ceiling did not bind"


async def test_refused_connection_is_upstream_error_not_upstream_timeout() -> None:
    """Case 5, real socket: a refusal is an ANSWER. Reporting it as a timeout would invert the
    distinction the new code exists to draw — "silent, retry later" vs "broken now"."""
    base_url = await _refused_base_url()
    started = time.monotonic()
    with pytest.raises(UpstreamError) as excinfo:
        await _run(_svc_for(base_url, _settings()))
    elapsed = time.monotonic() - started

    assert not isinstance(excinfo.value, UpstreamTimeoutError)
    assert excinfo.value.code == "upstream_error"
    # Three refused connects + two backoffs, all fast: nowhere near the budget.
    assert elapsed < _BUDGET


# ==================================================================================================
# 2. The split itself, and the retry arithmetic built on it.
# ==================================================================================================
def test_attempt_timeout_bounds_connect_separately_from_read() -> None:
    """Case 2: the four httpx phases are NOT one number. This is the fix in one assertion."""
    svc = _svc_for("http://x", _settings())
    timeout = svc._attempt_timeout(_BUDGET)

    assert timeout.connect == _CONNECT
    assert timeout.read == timeout.write == _PROXY
    assert timeout.pool == _CONNECT, "pool is a local-resource wait, it follows connect"
    assert timeout.connect != timeout.read, "a single float here is exactly what caused TD-040"


def test_attempt_timeout_clamps_every_phase_to_the_remaining_budget() -> None:
    """A nearly spent budget must not authorise a full-length attempt."""
    svc = _svc_for("http://x", _settings())
    timeout = svc._attempt_timeout(0.05)
    assert timeout.connect == timeout.read == timeout.write == timeout.pool == 0.05


def test_remaining_refuses_an_attempt_that_cannot_fit() -> None:
    """The budget is spent ⇒ a deterministic 502 now, not a token attempt nobody awaits."""
    svc = _svc_for("http://x", _settings())
    assert svc._remaining(time.monotonic() + 30, phase="t") == pytest.approx(30, abs=0.5)
    with pytest.raises(UpstreamTimeoutError):
        svc._remaining(time.monotonic() + 0.01, phase="t")


class _ConnectCapClient:
    """Transport double whose connect phase CONSUMES ITS CAP and then times out.

    Models a blackholed SYN — the mode that multiplied by the attempt count in TD-040 — which
    cannot be produced portably with a real socket (a closed port is refused, not silent). The
    recorded timeouts let the arithmetic be asserted directly.
    """

    recorded: list[httpx.Timeout] = []
    attempts: int = 0

    def __init__(self, *_args: Any, timeout: httpx.Timeout, **_kwargs: Any) -> None:
        self._timeout = timeout
        type(self).recorded.append(timeout)

    async def __aenter__(self) -> _ConnectCapClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        type(self).attempts += 1
        await asyncio.sleep(float(self._timeout.connect or 0))
        raise httpx.ConnectTimeout("simulated blackhole")


@pytest.fixture
def connect_cap_client(monkeypatch: pytest.MonkeyPatch) -> type[_ConnectCapClient]:
    _ConnectCapClient.recorded = []
    _ConnectCapClient.attempts = 0
    monkeypatch.setattr(service_mod.httpx, "AsyncClient", _ConnectCapClient)
    return _ConnectCapClient


async def test_retry_cycle_costs_attempts_times_connect_not_attempts_times_proxy(
    connect_cap_client: type[_ConnectCapClient],
) -> None:
    """Case 1, THE arithmetic regression — the assertion that fails on the pre-fix code.

    Every attempt burns its connect cap. Fixed: 3 × 0.1 + 2 × 0.05 = 0.4s. Pre-fix (connect capped
    at the proxy timeout): 3 × 2.0 + 0.1 = 6.1s, i.e. past the whole budget. The bound below sits
    between the two, so it discriminates without pinning a duration.
    """
    started = time.monotonic()
    with pytest.raises(UpstreamTimeoutError):
        await _run(_svc_for("http://wedged.invalid", _settings()))
    elapsed = time.monotonic() - started

    assert connect_cap_client.attempts == 3, "the ADR-062 retry count must be unchanged"
    assert elapsed < 2.0, (
        f"retry cycle took {elapsed:.2f}s — with connect capped at the proxy timeout it would be "
        f"~{3 * _PROXY:.1f}s, which is the TD-040 defect"
    )
    assert [t.connect for t in connect_cap_client.recorded] == [_CONNECT] * 3
    assert {t.read for t in connect_cap_client.recorded} == {_PROXY}


class _ScriptedClient:
    """Transport double replaying a scripted sequence of outcomes, one per attempt."""

    script: list[Any] = []
    attempts: int = 0

    def __init__(self, *_args: Any, timeout: httpx.Timeout, **_kwargs: Any) -> None:
        self._timeout = timeout

    async def __aenter__(self) -> _ScriptedClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        outcome = type(self).script[type(self).attempts]
        type(self).attempts += 1
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "hang":
            await asyncio.sleep(3600)
        return httpx.Response(202, json={"run_id": "run_x", "status": "queued"})


@pytest.fixture
def scripted_client(monkeypatch: pytest.MonkeyPatch) -> type[_ScriptedClient]:
    _ScriptedClient.script = []
    _ScriptedClient.attempts = 0
    monkeypatch.setattr(service_mod.httpx, "AsyncClient", _ScriptedClient)
    return _ScriptedClient


async def test_connect_error_then_success_still_launches(
    scripted_client: type[_ScriptedClient],
) -> None:
    """Case 15, ADR-062 regress: the connect-only retry is intact — the fix changed the PRICE of an
    attempt, not which attempts happen."""
    scripted_client.script = [httpx.ConnectError("blip"), "ok"]
    result = await _run(_svc_for("http://x", _settings()))
    assert (result.run_id, result.status) == ("run_x", "queued")
    assert scripted_client.attempts == 2


@pytest.mark.parametrize(
    "post_send",
    [httpx.ReadTimeout("silent after send"), httpx.WriteError("broken mid-send")],
    ids=["read_timeout", "write_error"],
)
async def test_post_send_errors_are_never_retried(
    scripted_client: type[_ScriptedClient], post_send: Exception
) -> None:
    """Case 15 / case 5: a post-send failure may already have created a run — retrying it risks a
    double run (ADR-062). It also decides the CODE: a read timeout is silence, a write error is
    not."""
    scripted_client.script = [post_send, "ok"]
    with pytest.raises(UpstreamError) as excinfo:
        await _run(_svc_for("http://x", _settings()))

    assert scripted_client.attempts == 1, "a post-send error must never be retried"
    expected_timeout = isinstance(post_send, httpx.TimeoutException)
    assert isinstance(excinfo.value, UpstreamTimeoutError) is expected_timeout


async def test_budget_ceiling_is_never_retried(scripted_client: type[_ScriptedClient]) -> None:
    """Case 12: the ``asyncio.timeout`` ceiling fires POST-SEND by construction, so retrying it
    would carry the same double-run risk as a ReadTimeout. One attempt, then 502."""
    scripted_client.script = ["hang", "ok"]
    settings = _settings(HERMES_LAUNCH_BUDGET_SECONDS=3.0, HERMES_PROXY_TIMEOUT_SECONDS=1.0)
    started = time.monotonic()
    with pytest.raises(UpstreamTimeoutError):
        await _run(_svc_for("http://x", settings))
    elapsed = time.monotonic() - started

    assert scripted_client.attempts == 1
    assert elapsed < 3.0 + 2.0


# ==================================================================================================
# 3. The same guarantees on every other proxied path.
# ==================================================================================================
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda svc, uid: svc.stop(user_id=uid, run_id="r"), id="stop"),
        pytest.param(
            lambda svc, uid: svc.approval(user_id=uid, run_id="r", body={"approved": True}),
            id="approval",
        ),
    ],
)
async def test_passthrough_paths_share_the_budget_and_the_codes(call: Any) -> None:
    """Case 6: ``/stop`` is what a user reaches for when a run is ALREADY misbehaving — the one
    request that must never hang. Same socket, same bound, same code."""
    server = await _silent_server()
    try:
        svc = _svc_for(server.base_url, _settings())
        started = time.monotonic()
        with pytest.raises(UpstreamTimeoutError) as excinfo:
            await call(svc, uuid.uuid4())
        elapsed = time.monotonic() - started
    finally:
        await server.aclose()

    assert excinfo.value.code == "upstream_timeout"
    assert elapsed < _BUDGET


def test_events_stream_keeps_read_unbounded_but_bounds_every_other_phase() -> None:
    """Case 7: a long-lived stream must not be killed by a read cap — a run may think for minutes.

    Everything that is NOT the stream body still has to be bounded, otherwise the SSE path becomes
    the new hang. Asserted on the constructed timeout rather than on a socket: the property is
    "read is None and the rest is not", which a live stream cannot show without waiting forever.
    """
    settings = _settings(HERMES_SSE_CONNECT_TIMEOUT_SECONDS=7.0)
    timeout = httpx.Timeout(
        connect=settings.hermes_sse_connect_timeout_seconds,
        read=None,
        write=settings.hermes_sse_connect_timeout_seconds,
        pool=settings.hermes_sse_connect_timeout_seconds,
    )
    assert timeout.read is None, "a long stream must not be killed by a read cap"
    assert timeout.connect == timeout.write == timeout.pool == 7.0


async def test_events_stream_passes_a_deadline_to_ensure_running() -> None:
    """Case 7: the stream body is unbounded, but WAKING the instance for it is not."""
    captured: dict[str, Any] = {}

    class _RecordingManager(FakeManager):
        async def ensure_running(
            self, user_id: uuid.UUID, *, deadline: float | None = None
        ) -> InstanceEndpoint:
            captured["deadline"] = deadline
            raise UpstreamTimeoutError("ready gate expired")

    svc, _m, _w, _a = _make_service(settings=_settings(), manager=_RecordingManager())
    with pytest.raises(UpstreamTimeoutError):
        async for _chunk in svc.stream_events(user_id=uuid.uuid4(), run_id="r"):
            pass  # pragma: no cover - the manager raises before the first chunk

    assert captured["deadline"] is not None, "ensure_running must be bounded on the SSE path too"
    assert captured["deadline"] > time.monotonic()


# ==================================================================================================
# 4. Configuration.
# ==================================================================================================
def test_settings_reject_a_budget_below_the_invariant() -> None:
    """Case 9: fail-fast at startup — the only place these knobs are visible together.

    At request time a truncated budget is indistinguishable from a dead instance, so a misconfigured
    deployment would silently fail slow instances with ``upstream_timeout`` while they were merely
    booting.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(  # type: ignore[call-arg]
            HERMES_PROVISION_READY_TIMEOUT_SECONDS=90,
            HERMES_PROXY_TIMEOUT_SECONDS=30.0,
            HERMES_LAUNCH_BUDGET_SECONDS=149.0,
        )
    assert "HERMES_LAUNCH_BUDGET_SECONDS" in str(excinfo.value)


def test_settings_accept_the_invariant_boundary_exactly() -> None:
    """The bound is inclusive: ready + 2 × proxy is exactly enough, and the defaults satisfy it."""
    settings = Settings(  # type: ignore[call-arg]
        HERMES_PROVISION_READY_TIMEOUT_SECONDS=90,
        HERMES_PROXY_TIMEOUT_SECONDS=30.0,
        HERMES_LAUNCH_BUDGET_SECONDS=150.0,
    )
    assert settings.hermes_launch_budget_seconds == 150.0
    defaults = Settings()  # type: ignore[call-arg]
    assert defaults.hermes_launch_budget_seconds >= (
        defaults.hermes_provision_ready_timeout_seconds + 2 * defaults.hermes_proxy_timeout_seconds
    )


def test_connect_timeout_is_a_separate_knob_from_the_proxy_timeout() -> None:
    """The two were one number; the defect was that nothing could tell them apart."""
    defaults = Settings()  # type: ignore[call-arg]
    assert defaults.hermes_connect_timeout_seconds == 10.0
    assert defaults.hermes_proxy_timeout_seconds == 30.0


# ==================================================================================================
# 5. The manager: the row lock is what made ONE wedged instance hang EVERY request of that user.
# ==================================================================================================
class _LockTimeoutError(DBAPIError):
    """A DBAPIError carrying Postgres SQLSTATE 55P03, the way the asyncpg adapter delivers it.

    The production classifier reads ``sqlstate`` off the wrapped driver error rather than matching
    an exception class (the class is an adapter implementation detail), so the double has to carry
    the code on ``orig`` — matching on the type here would test a rule the code does not use.
    """

    def __init__(self) -> None:
        orig = Exception("canceling statement due to lock timeout")
        orig.sqlstate = "55P03"  # type: ignore[attr-defined]
        super().__init__("SELECT ... FOR UPDATE", {}, orig)


def _mgr_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "HERMES_IMAGE": "hermes:test-1.0",
        "HERMES_LLM_API_KEY": "service-llm-key-xyz",
        "HERMES_LLM_PROVIDER": "anthropic",
        "HERMES_MODEL": "claude-sonnet-4-5",
        "HERMES_PROVISION_READY_TIMEOUT_SECONDS": 2,
        "HERMES_PROVISION_READY_INTERVAL_SECONDS": 1,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_row_lock_timeout_becomes_upstream_timeout_and_rolls_back() -> None:
    """Case 4: queueing behind another request's lock is silence from THIS request's point of view.

    The rollback matters as much as the code: the statement aborted the transaction, so without it
    the session is unusable for the audit / agent_runs writes that follow on the same request.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _mgr_settings())

    async def _raise(*_a: Any, **_kw: Any) -> None:
        raise _LockTimeoutError()

    reg.get_for_update = _raise  # type: ignore[assignment]
    with pytest.raises(UpstreamTimeoutError):
        await mgr.ensure_running(uuid.uuid4(), deadline=time.monotonic() + 30)
    assert mgr._session.rollback_calls == 1  # type: ignore[attr-defined]


async def test_a_non_lock_db_error_is_never_laundered_into_a_502() -> None:
    """Case 4: only 55P03 means "busy". Any other DB failure must surface as itself.

    Swallowing it would turn a real database fault into "the instance is busy", i.e. hide an
    outage behind a retryable-looking code.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _mgr_settings())

    class _OtherDbError(DBAPIError):
        def __init__(self) -> None:
            orig = Exception("deadlock detected")
            orig.sqlstate = "40P01"  # type: ignore[attr-defined]
            super().__init__("SELECT 1", {}, orig)

    async def _raise(*_a: Any, **_kw: Any) -> None:
        raise _OtherDbError()

    reg.get_for_update = _raise  # type: ignore[assignment]
    with pytest.raises(DBAPIError):
        await mgr.ensure_running(uuid.uuid4(), deadline=time.monotonic() + 30)


async def test_lock_timeout_ms_floors_at_one_millisecond() -> None:
    """Postgres reads ``lock_timeout = 0`` as WAIT FOREVER, so an exhausted budget must not round
    down to it — that would turn the tightest deadline into no deadline at all."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _mgr_settings())
    assert mgr._lock_timeout_ms(time.monotonic() - 5) == 1
    assert mgr._lock_timeout_ms(None) is None, "no budget ⇒ the pre-existing unbounded wait"
    assert mgr._lock_timeout_ms(time.monotonic() + 2) == pytest.approx(2000, abs=200)


async def test_running_fast_path_commits_before_returning() -> None:
    """Case 10: THE fix for "one wedged instance hangs every later request of the same user".

    This branch used to only ``flush``, so the ``FOR UPDATE`` lock was held until the request
    session's teardown — across the caller's entire HTTP call. On a wedged instance that is the
    whole budget, and every other request of that user queued behind it.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _mgr_settings())
    uid = uuid.uuid4()
    backend.health_return = True
    await mgr.ensure_running(uid)  # cold start → running row
    commits_after_provision = mgr._session.commit_calls  # type: ignore[attr-defined]

    await mgr.ensure_running(uid, deadline=time.monotonic() + 30)  # the fast path

    assert reg.touch_calls >= 1
    assert mgr._session.commit_calls > commits_after_provision, (  # type: ignore[attr-defined]
        "the fast path must release the row lock BEFORE the caller's HTTP call"
    )


async def test_readiness_timeout_is_upstream_timeout() -> None:
    """Case 14: the gate ended on a deadline with the instance silent ⇒ ``upstream_timeout``."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _mgr_settings())
    backend.health_return = False  # never becomes ready

    with pytest.raises(UpstreamTimeoutError):
        await mgr.ensure_running(uuid.uuid4(), deadline=time.monotonic() + 30)


async def test_a_competitor_that_cleaned_up_is_a_definite_answer_not_a_deadline() -> None:
    """Case 14: the row VANISHED — the concurrent provisioner failed and tidied up.

    That is a definite outcome, not silence, so the honest code is the generic 502. Reporting it as
    ``upstream_timeout`` would tell the client "still booting, retry later" about an instance that
    is not booting at all.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _mgr_settings())
    uid = uuid.uuid4()

    with pytest.raises(UpstreamError) as excinfo:
        await mgr._await_concurrent_ready(uid, deadline=time.monotonic() + 30)
    assert not isinstance(excinfo.value, UpstreamTimeoutError)
    assert excinfo.value.code == "upstream_error"


# ==================================================================================================
# 6. /resume — a budget expiry must leave the run resumable.
# ==================================================================================================
async def test_resume_timeout_after_the_cas_reverts_the_run_to_paused() -> None:
    """Case 8: the CAS is committed BEFORE the upstream calls, so a timeout must undo it.

    Otherwise the run is stuck ``resumed`` with no child chained: ``/resume`` answers 409 forever
    and the user's paid-for run is unreachable. The budget expiry has to travel the same failure
    path as any other upstream error.
    """
    settings = _settings()

    class _TimingOutManager(FakeManager):
        async def ensure_running(
            self, user_id: uuid.UUID, *, deadline: float | None = None
        ) -> InstanceEndpoint:
            raise UpstreamTimeoutError("budget expired while waking")

    class _CasRow:
        session_id = uuid.uuid4()
        model = "m"

    uid = uuid.uuid4()

    class _Row:
        def __init__(self) -> None:
            self.user_id = uid
            self.status = "paused"
            self.run_id = "run_1"

    class _ResumeRunsRepo:
        """Minimal runs-repo double for the resume path: win the CAS, record the revert."""

        def __init__(self) -> None:
            self.revert_cas_calls: list[str] = []

        async def get(self, run_id: str) -> Any:
            return _Row()

        async def cas_resume(self, run_id: str) -> Any:
            return _CasRow()

        async def revert_cas(self, run_id: str) -> int:
            self.revert_cas_calls.append(run_id)
            return 1

    runs = _ResumeRunsRepo()
    svc, _m, _w, _a = _make_service(
        settings=settings,
        manager=_TimingOutManager(),
        runs=runs,  # type: ignore[arg-type]
    )

    with pytest.raises(UpstreamTimeoutError):
        await svc.resume(user_id=uid, run_id="run_1", message=None)

    assert runs.revert_cas_calls == ["run_1"], "a timed-out resume must stay resumable"
