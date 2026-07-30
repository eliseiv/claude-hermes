"""Unit: every phase of ``agent_run_launch_upstream_timeout_total`` actually lights up (Q-067-17).

WHY THIS MODULE EXISTS. ADR-067 §5.1 was withdrawn from v1 — the "wedged instance" premise was
disproved — but explicitly **under a tripwire**: if ``502 upstream_timeout`` ever clusters on one
user again, the premise returns and §5.1 is resurrected. This counter IS that tripwire, and the ADR
is blunt that it must be *built, not declared*.

A counter that exists, scrapes cleanly, and never increments is indistinguishable from no tripwire
at all — it is worse, because it looks like coverage. Exposure at ``/metrics`` was already asserted
(the sweep module); what was never asserted is that any real failure REACHES it. So every test here
drives a genuine launch/resume path to a genuine timeout and reads the counter afterwards. A dead
phase is a code defect, not a coverage gap.

Three deliberate NON-emissions are pinned alongside, because each is a design decision that a later
"let's count everything" refactor would quietly reverse:

* a REFUSED connection is an answer, not muteness — it must not enter a counter whose whole purpose
  is to find silent instances;
* ``connect`` cannot be reported by the readiness probe (it runs with ``refine=False``), so a
  readiness timeout stays ``readiness`` whatever its cause;
* the passthrough routes (``/stop``, ``/approval``) are not instrumented at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.agent_proxy import service as service_mod
from app.config import Settings
from app.errors import UpstreamError, UpstreamTimeoutError
from app.hermes_runtime.manager import InstanceEndpoint
from app.observability.metrics import agent_run_launch_upstream_timeout_total
from tests.unit.test_agent_proxy_service import (
    FakeManager,
    FakeRunsRepo,
    _Decision,
    _make_service,
    _patch_policy,
)

# The complete label set of the counter, mirroring _PHASE_* in service.py. Every one of these must
# be produced by a test below; `test_every_declared_phase_is_reachable` enforces exactly that.
_PHASES = ("connect", "readiness", "launch", "hydrate", "budget")

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
        "HERMES_LAUNCH_RETRY_ATTEMPTS": 1,
        "HERMES_LAUNCH_RETRY_BACKOFF_SECONDS": 0.05,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _count(phase: str) -> float:
    """Current value of the counter for one phase. Read before and after; never reset.

    Prometheus counters are process-global and other modules in the same session touch them, so
    every assertion here is a DELTA. Asserting an absolute value would make this module depend on
    test execution order.
    """
    return agent_run_launch_upstream_timeout_total.labels(phase=phase)._value.get()


def _all_counts() -> dict[str, float]:
    return {phase: _count(phase) for phase in _PHASES}


def _delta(before: dict[str, float]) -> dict[str, float]:
    """Which phases moved, and by how much. The assertions read this rather than one phase.

    Checking only the expected phase would pass on an implementation that increments *every* label
    — and a counter that fires on all five at once carries no more information than one that never
    fires, which is the failure mode this module is about.
    """
    after = _all_counts()
    return {
        phase: after[phase] - before[phase] for phase in _PHASES if after[phase] > before[phase]
    }


# --------------------------------------------------------------------------------------------
# Real sockets, for the cases where the behaviour under test IS socket behaviour.
# --------------------------------------------------------------------------------------------
class _Server:
    def __init__(self, server: asyncio.AbstractServer, port: int) -> None:
        self._server = server
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self.base_url = f"http://127.0.0.1:{port}"

    async def aclose(self) -> None:
        for writer in self._writers:
            with contextlib.suppress(Exception):
                writer.close()
        for task in self._tasks:
            task.cancel()
        self._server.close()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    def _track(self, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)


async def _silent_server() -> _Server:
    """Accepts the connection, reads the request, and then says NOTHING — a mute instance.

    This is the shape the tripwire is looking for in production, so it is reproduced with a real
    socket rather than a raised exception: the phase is derived from what httpx actually observed.
    """
    holder: dict[str, _Server] = {}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        holder["s"]._track(writer)
        with contextlib.suppress(Exception):
            await reader.read(65536)
            await asyncio.sleep(3600)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    holder["s"] = _Server(server, server.sockets[0].getsockname()[1])
    return holder["s"]


async def _refused_base_url() -> str:
    """A port bound and immediately released: connecting is REFUSED, not silent."""
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return f"http://127.0.0.1:{port}"


class _ConnectTimeoutClient:
    """An httpx double whose every request dies in the CONNECT phase.

    A hanging connect cannot be produced portably (it needs a dropped SYN, not a refusal), and the
    property under test is the PHASE ATTRIBUTION, not the syscall — ``__cause__`` being a
    ``ConnectTimeout`` is exactly what ``_timeout_phase`` reads.
    """

    attempts = 0

    def __init__(self, *_args: Any, timeout: httpx.Timeout, **_kwargs: Any) -> None:
        self._timeout = timeout

    async def __aenter__(self) -> _ConnectTimeoutClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        type(self).attempts += 1
        await asyncio.sleep(float(self._timeout.connect or 0))
        raise httpx.ConnectTimeout("simulated blackhole")

    async def get(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        type(self).attempts += 1
        await asyncio.sleep(float(self._timeout.connect or 0))
        raise httpx.ConnectTimeout("simulated blackhole")


def _svc_for(base_url: str, settings: Settings, **kwargs: Any) -> Any:
    manager = FakeManager(endpoint=InstanceEndpoint(base_url=base_url, api_key="k"))
    svc, _mgr, _wal, _aud = _make_service(settings=settings, manager=manager, **kwargs)
    return svc


async def _run(svc: Any) -> Any:
    return await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)


@pytest.fixture(autouse=True)
def _allow_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_policy(monkeypatch, _Decision(allow=True))


# ==================================================================================================
# The five phases. Each drives a real path to a real timeout.
# ==================================================================================================
async def test_readiness_phase_is_recorded_when_the_instance_never_becomes_ready() -> None:
    """``ensure_running`` timing out is the readiness gate, and it is reported unrefined.

    The manager raises its own verdict with no httpx cause to read, which is exactly why this probe
    runs with ``refine=False``: guessing a phase from an absent cause would file readiness failures
    under ``budget`` and blunt the tripwire.
    """
    settings = _settings()

    class _NeverReady(FakeManager):
        async def ensure_running(self, user_id: uuid.UUID, *, deadline: float | None = None) -> Any:
            raise UpstreamTimeoutError("ready gate expired")

    svc, _m, _w, _a = _make_service(settings=settings, manager=_NeverReady())
    before = _all_counts()
    with pytest.raises(UpstreamTimeoutError):
        await _run(svc)

    assert _delta(before) == {"readiness": 1.0}


async def test_launch_phase_is_recorded_when_the_instance_accepts_then_goes_mute() -> None:
    """A read-phase timeout on ``POST /v1/runs``: accepted the connection, then said nothing.

    THE canonical shape of the symptom the tripwire hunts — the instance is reachable but mute.
    """
    server = await _silent_server()
    before = _all_counts()
    try:
        with pytest.raises(UpstreamTimeoutError):
            await _run(_svc_for(server.base_url, _settings()))
    finally:
        await server.aclose()

    assert _delta(before) == {"launch": 1.0}


async def test_connect_phase_is_recorded_when_the_connect_itself_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect-phase timeout is filed as ``connect``, not as the caller's nominal phase.

    The distinction is the tripwire's whole content: "never accepted the connection" and "accepted
    and then said nothing" are different instance faults, and collapsing them into one label would
    leave the operator with a number that cannot answer the question it was raised for.
    """
    monkeypatch.setattr(service_mod.httpx, "AsyncClient", _ConnectTimeoutClient)
    _ConnectTimeoutClient.attempts = 0
    before = _all_counts()

    with pytest.raises(UpstreamTimeoutError):
        await _run(_svc_for("http://wedged.invalid", _settings()))

    assert _ConnectTimeoutClient.attempts >= 1, "no request was attempted at all"
    assert _delta(before) == {"connect": 1.0}


async def test_budget_phase_is_recorded_when_our_own_deadline_expires_first() -> None:
    """The budget ceiling is OUR property, not the instance's, and must not read as muteness.

    ``ensure_running`` burns the whole budget, so ``_remaining`` refuses to start the launch attempt
    and raises with no httpx cause at all. Filing that as ``launch`` would have the tripwire count
    our own deadline as evidence against the user's instance.
    """
    settings = _settings()

    class _SlowManager(FakeManager):
        async def ensure_running(self, user_id: uuid.UUID, *, deadline: float | None = None) -> Any:
            await asyncio.sleep(_BUDGET + 0.5)
            return self.endpoint

    svc, _m, _w, _a = _make_service(settings=settings, manager=_SlowManager())
    before = _all_counts()
    started = time.monotonic()
    with pytest.raises(UpstreamTimeoutError):
        await _run(svc)
    elapsed = time.monotonic() - started

    assert _delta(before) == {"budget": 1.0}
    assert elapsed >= _BUDGET, "the budget did not actually expire; the scenario was not exercised"


async def test_hydrate_phase_is_recorded_when_the_transcript_fetch_goes_mute() -> None:
    """Resume walks the same path with one extra leg, and that leg gets its own label.

    ``GET /api/sessions/{id}/messages`` is the only phase unique to resume; without a label of its
    own a mute instance during hydrate would be filed as ``launch`` and point the operator at the
    wrong call.
    """
    resumable = SimpleNamespace(run_id="run_x", user_id=None, status="paused")

    class _ResumeRepo(FakeRunsRepo):
        async def get(self, run_id: str) -> Any:
            return resumable

        async def cas_resume(self, run_id: str) -> Any:
            return SimpleNamespace(run_id=run_id, session_id="sess-1", model=None)

        async def revert_cas(self, run_id: str) -> None:
            self.mark_status_calls.append((run_id, "reverted"))

    server = await _silent_server()
    settings = _settings()
    uid = uuid.uuid4()
    resumable.user_id = uid
    repo = _ResumeRepo()
    svc = _svc_for(server.base_url, settings, runs=repo)
    before = _all_counts()
    try:
        with pytest.raises(UpstreamTimeoutError):
            await svc.resume(user_id=uid, run_id="run_x", message=None)
    finally:
        await server.aclose()

    assert _delta(before) == {"hydrate": 1.0}
    assert repo.mark_status_calls, "the CAS was not reverted — resume did not reach the launch leg"


async def test_every_declared_phase_is_reachable() -> None:
    """No phase of the enum may be dead: a dead phase is a hole in the tripwire, not in coverage.

    The four tests above plus the hydrate one produce all five labels. This test states the
    completeness claim explicitly so that ADDING a phase to ``_PHASE_*`` without a path that emits
    it fails here — the counter is a safety net, and a net with a hole is not a smaller net.
    """
    observed = {phase for phase in _PHASES if _count(phase) > 0}
    assert observed == set(_PHASES), (
        f"phases never emitted by any test in this module: {sorted(set(_PHASES) - observed)} — "
        "each is a label the tripwire declares but nothing can ever produce"
    )


# ==================================================================================================
# The three deliberate NON-emissions.
# ==================================================================================================
async def test_a_refused_connection_is_not_counted_at_all() -> None:
    """A refusal is an ANSWER. The tripwire hunts MUTENESS, and counting refusals would drown it.

    ``ConnectError`` maps to ``UpstreamError``, not ``UpstreamTimeoutError``, so the probe's
    ``except`` never sees it. A "let's also count connection errors" change would make the counter
    rise on ordinary restarts and cold starts, and the §5.1 resurrection criterion — clustering on
    one user — would start firing on noise.
    """
    base_url = await _refused_base_url()
    before = _all_counts()

    with pytest.raises(UpstreamError) as excinfo:
        await _run(_svc_for(base_url, _settings()))

    assert not isinstance(excinfo.value, UpstreamTimeoutError)
    assert _delta(before) == {}, "a refused connection moved the muteness tripwire"


async def test_a_connect_timeout_inside_the_readiness_gate_stays_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connect`` is unreachable through the readiness probe BY CONSTRUCTION (``refine=False``).

    The manager's verdict is not derived from a single httpx call, so a cause found on it would not
    describe the gate as a whole. The probe therefore never refines it — asserted here rather than
    left as a comment, because ``refine=False`` is one keyword away from being "cleaned up".
    """
    settings = _settings()

    class _ConnectFailingGate(FakeManager):
        async def ensure_running(self, user_id: uuid.UUID, *, deadline: float | None = None) -> Any:
            exc = UpstreamTimeoutError("ready gate expired")
            # A cause that WOULD refine to `connect` if the readiness probe ever refined.
            raise exc from httpx.ConnectTimeout("connect blackhole")

    svc, _m, _w, _a = _make_service(settings=settings, manager=_ConnectFailingGate())
    before = _all_counts()
    with pytest.raises(UpstreamTimeoutError):
        await _run(svc)

    assert _delta(before) == {"readiness": 1.0}, (
        "the readiness gate reported a refined phase — a gate timeout would be filed as an "
        "instance connect fault it does not evidence"
    )


@pytest.mark.parametrize("route", ["stop", "approval"])
async def test_the_passthrough_routes_are_not_instrumented(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/stop`` and ``/approval`` feed no phase — stated as a fact, not assumed.

    They are not part of the launch path, so a timeout there is not evidence about a run STARTING,
    which is what §5.1's premise was about. The gap is deliberate; pinning it means a later
    decision to instrument them has to be made on purpose and given its own label, rather than
    silently polluting an enum the tripwire's thresholds are calibrated against.
    """
    monkeypatch.setattr(service_mod.httpx, "AsyncClient", _ConnectTimeoutClient)
    _ConnectTimeoutClient.attempts = 0
    uid = uuid.uuid4()
    repo = FakeRunsRepo()
    repo.owner_user_id = uid  # own the run, so RBAC cannot short-circuit before the passthrough
    svc = _svc_for("http://wedged.invalid", _settings(), runs=repo)
    before = _all_counts()

    with pytest.raises((UpstreamError, UpstreamTimeoutError)):
        if route == "stop":
            await svc.stop(user_id=uid, run_id="run_x")
        else:
            await svc.approval(user_id=uid, run_id="run_x", body={"approved": True})

    # Without this the test is vacuous: a route that 404s on RBAC also increments nothing.
    assert (
        _ConnectTimeoutClient.attempts >= 1
    ), f"/{route} never reached the instance, so 'it recorded no phase' proves nothing"
    assert _delta(before) == {}, f"/{route} fed the launch-path tripwire"
