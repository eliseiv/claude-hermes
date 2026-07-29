"""Unit: agent-run state snapshot — relay writer, lifecycle statuses, mapping (ADR-066).

Isolated from Docker/DB/ledger: the Hermes instance is mocked at the HTTP boundary (respx) and the
collaborators (manager / wallet / audit / runs repo / snapshots repo / session) are the fakes of
``tests/unit/test_agent_proxy_service.py`` (agent-proxy/09-testing.md §Unit; 06-testing-strategy.md
§Политика моков — only the EXTERNAL boundary is mocked, the DB-facing SQL of the repositories is
exercised for real in ``tests/integration/test_agent_snapshot_writer_adr066.py``).

Covers agent-proxy/09-testing.md §"Снапшот состояния прогона" / Unit:
- pure ``map_client_status`` — every row of the ADR-066 §4 table (7 outcomes; ``queued`` not
  emitted in v1);
- head-preserving truncation of ``result_text`` at the ``AGENT_STATE_RESULT_TEXT_MAX_CHARS``
  boundary (exactly the cap / cap+1);
- ``message.delta`` throttling vs the IMMEDIATE flushes (``approval.request``, ``tool.*``,
  terminal events) and the ``assert_pending_approval`` semantics of a throttled flush;
- ``tool.*`` → ``last_tool`` + approval cleared; ``POST …/approval`` (2xx) → approval cleared;
- lifecycle statuses: ``run.failed``→``failed``, ``run.completed``→``completed`` recorded BEFORE
  billing and independently of its outcome, ``POST …/stop``→``mark_stopped(run_id, user_id)``,
  and pause-at-zero NEVER routing through ``mark_stopped``;
- with the billing flag OFF the snapshot token counters come from ``run.completed{usage}``;
- a snapshot write failure never breaks the relay;
- the one-shot LATCHES of ``_log_write_anomaly``: each silent refusal mode of the upsert (tenancy
  rejection → WARNING, frozen ``result_text`` → DEBUG) is reported ONCE per relay, and the lines
  carry only ids and lengths — never user content.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy.exc import SQLAlchemyError

from app.agent_proxy.service import AgentProxyService, map_client_status
from app.config import Settings
from tests.unit.test_agent_proxy_service import (
    _API_KEY,
    _BASE_URL,
    FakeAudit,
    FakeManager,
    FakeRunsRepo,
    FakeSession,
    FakeSnapshotsRepo,
    FakeWallet,
    _capture_service_logs,
    _collect,
    _sse,
)


def _settings(**overrides: Any) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


class _capture_at_level:
    """Capture records of ``app.agent_proxy.service`` at an arbitrary level (records, not text).

    ``_capture_service_logs`` is pinned to WARNING; the frozen-text anomaly is logged at DEBUG, so
    the latch tests need a level-parameterised twin. Same hermetic handling as the original: attach
    to the NAMED logger and force-enable it, because a prior integration test that ran
    ``create_app()`` → ``configure_logging()`` (which clears root handlers) can leave this logger
    ``disabled=True`` and silently drop everything (order-dependent flake).
    """

    def __init__(self, level: int = logging.DEBUG) -> None:
        self._logger = logging.getLogger("app.agent_proxy.service")
        self._level = level
        self.records: list[logging.LogRecord] = []
        self._prev_level = self._logger.level
        self._prev_disabled = self._logger.disabled
        self._prev_propagate = self._logger.propagate

    def __enter__(self) -> _capture_at_level:
        outer = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                outer.records.append(record)

        self._handler = _Handler(level=self._level)
        self._logger.addHandler(self._handler)
        self._logger.setLevel(self._level)
        self._logger.disabled = False
        # Keep the captured records out of the root handlers (pytest's live log) — irrelevant noise.
        self._logger.propagate = False
        return self

    def __exit__(self, *_exc: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)
        self._logger.disabled = self._prev_disabled
        self._logger.propagate = self._prev_propagate

    def messages(self, level: int | None = None) -> list[str]:
        return [r.getMessage() for r in self.records if level is None or r.levelno == level]

    def matching(self, needle: str) -> list[str]:
        return [m for m in self.messages() if needle in m]


class _Wiring:
    """The full collaborator set of one service instance (so a test can assert on any of them)."""

    def __init__(self, svc: AgentProxyService, **parts: Any) -> None:
        self.svc = svc
        self.manager: FakeManager = parts["manager"]
        self.wallet: FakeWallet = parts["wallet"]
        self.audit: FakeAudit = parts["audit"]
        self.session: FakeSession = parts["session"]
        self.runs: FakeRunsRepo = parts["runs"]
        self.snapshots: FakeSnapshotsRepo = parts["snapshots"]


def _wire(
    settings: Settings | None = None,
    *,
    wallet: FakeWallet | None = None,
    snapshots: FakeSnapshotsRepo | None = None,
    runs: FakeRunsRepo | None = None,
) -> _Wiring:
    parts: dict[str, Any] = {
        "manager": FakeManager(),
        "wallet": wallet or FakeWallet(),
        "audit": FakeAudit(),
        "session": FakeSession(),
        "runs": runs or FakeRunsRepo(),
        "snapshots": snapshots or FakeSnapshotsRepo(),
    }
    parts["wallet"].session = parts["session"]
    parts["wallet"].runs = parts["runs"]
    svc = AgentProxyService(
        session=parts["session"],  # type: ignore[arg-type]
        manager=parts["manager"],  # type: ignore[arg-type]
        wallet=parts["wallet"],  # type: ignore[arg-type]
        audit=parts["audit"],  # type: ignore[arg-type]
        settings=settings or _settings(),
        runs=parts["runs"],  # type: ignore[arg-type]
        snapshots=parts["snapshots"],  # type: ignore[arg-type]
    )
    return _Wiring(svc, **parts)


def _events_route(body: bytes, run_id: str = "run_1", status: int = 200) -> Any:
    return respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(status, content=body)
    )


def _delta(text: str) -> bytes:
    """A ``message.delta`` in the shape the PRODUCTION image actually emits (ADR-065).

    Bare-string ``delta``, no SSE ``event:`` header line — verified against the raw prod capture in
    ``tests/fixtures/hermes_prod_run_adr065.sse`` (see
    ``tests/unit/test_agent_sse_delta_contract_adr065.py``). It deliberately replaces the previous
    ``{"text": …}`` helper, which was invented alongside the parser: with that shape every writer
    test below stayed green while ``resultText`` was identically empty on prod (ADR-066 defect).
    The nested ``{"delta": {"text": …}}`` build is covered by the contract module's shape matrix.
    """
    payload = {"event": "message.delta", "run_id": "run_1", "delta": text}
    return f"data: {json.dumps(payload)}\n\n".encode()


def _completed(input_tokens: int, output_tokens: int) -> bytes:
    return _sse(
        "run.completed",
        json.dumps(
            {
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "model": "m",
            }
        ),
    )


# ============================================================================
# D. Pure mapping server → client (ADR-066 §4) — every row of the table.
# ============================================================================
@pytest.mark.parametrize(
    ("db_status", "pending", "expected"),
    [
        # running/resumed WITHOUT a pending approval → running. `resumed` is the PARENT row after a
        # resume (work continues in the child), so it maps to `running` as well.
        ("running", False, "running"),
        ("resumed", False, "running"),
        # ... WITH a pending approval → the DERIVED waiting_approval (never a DB enum value).
        ("running", True, "waiting_approval"),
        ("resumed", True, "waiting_approval"),
        # Terminal statuses pass through; `cancelled` is renamed to the client vocabulary.
        ("paused", False, "paused"),
        ("completed", False, "completed"),
        ("failed", False, "failed"),
        ("cancelled", False, "stopped"),
    ],
)
def test_map_client_status_covers_every_adr066_row(
    db_status: str, pending: bool, expected: str
) -> None:
    assert map_client_status(db_status, has_pending_approval=pending) == expected


@pytest.mark.parametrize("db_status", ["paused", "completed", "failed", "cancelled"])
def test_map_client_status_terminal_ignores_pending_approval(db_status: str) -> None:
    # waiting_approval is derived ONLY from running/resumed; a stale pending_approval left on a
    # terminal run must never resurrect it (the run awaits nothing any more).
    assert map_client_status(db_status, has_pending_approval=True) == map_client_status(
        db_status, has_pending_approval=False
    )


def test_map_client_status_queued_is_never_emitted_in_v1() -> None:
    # `queued` is a forward-compat member of the Literal only: no agent_runs.status maps to it.
    every_db_status = ["running", "resumed", "paused", "completed", "failed", "cancelled"]
    produced = {
        map_client_status(s, has_pending_approval=p) for s in every_db_status for p in (True, False)
    }
    assert "queued" not in produced


def test_map_client_status_unknown_future_enum_degrades_to_running() -> None:
    # A value added to the DB enum by a future migration must not leak an off-contract status.
    assert map_client_status("some_future_status", has_pending_approval=False) == "running"


# ============================================================================
# D. Head-preserving truncation at the AGENT_STATE_RESULT_TEXT_MAX_CHARS boundary.
# ============================================================================
@respx.mock
async def test_result_text_truncation_is_head_preserving_at_boundary() -> None:
    # Cap = 10, flush interval 0 so EVERY delta is flushed (the throttle is asserted separately).
    w = _wire(
        _settings(
            **{
                "AGENT_STATE_RESULT_TEXT_MAX_CHARS": 10,
                "AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0,
            }
        )
    )
    # 9 chars (under the cap) → 10 chars (EXACTLY the cap) → 11 chars (cap + 1, must truncate).
    _events_route(_delta("012345678") + _delta("9") + _delta("X"))
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    texts = [u["result_text"] for u in w.snapshots.upserts]
    assert texts == ["012345678", "0123456789", "0123456789"]
    # cap+1 kept the HEAD, not the tail: tail-preserving would have produced "123456789X".
    assert texts[-1].startswith("012345678")
    assert not texts[-1].endswith("X")


@respx.mock
async def test_truncated_text_length_freezes_so_replay_guard_keeps_accepting() -> None:
    # ADR-066 §6.3: once the cap is reached the stored length is CONSTANT, so the `>=` length
    # comparison of the per-column replay-guard keeps letting later writes through (the first N
    # chars are identical by construction). Assert the invariant the guard relies on.
    w = _wire(
        _settings(
            **{"AGENT_STATE_RESULT_TEXT_MAX_CHARS": 8, "AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}
        )
    )
    _events_route(_delta("abcdefghij") + _delta("klmnop") + _delta("qrst"))
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    texts = [u["result_text"] for u in w.snapshots.upserts]
    assert {len(t) for t in texts} == {8}
    assert set(texts) == {"abcdefgh"}  # stable prefix, never re-cut from a different offset


# ============================================================================
# Writer: message.delta throttling vs the immediate flushes (ADR-066 §6.1).
# ============================================================================
@respx.mock
async def test_message_delta_series_within_interval_writes_once() -> None:
    # Default interval 3.0s: the FIRST delta flushes (last_flush_at == 0.0), the rest of the burst
    # is throttled into that single write.
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 3.0}))
    _events_route(_delta("a") + _delta("b") + _delta("c") + _delta("d"))
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert len(w.snapshots.upserts) == 1, w.snapshots.upserts
    assert w.snapshots.upserts[0]["result_text"] == "a"


@respx.mock
async def test_approval_request_and_terminal_bypass_the_throttle() -> None:
    # Same 3s window as above, but approval.request and run.completed are IMMEDIATE: a delayed
    # write would leave the client unaware that the run is waiting for its answer.
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 3.0}))
    body = (
        _delta("a")  # flush #1 (first delta)
        + _delta("b")  # throttled
        + _sse("approval.request", '{"tool":"shell","preview":"rm -rf /"}')  # flush #2 immediate
        + _delta("c")  # throttled
        + _completed(1000, 0)  # flush #3 immediate
    )
    _events_route(body)
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert len(w.snapshots.upserts) == 3
    # The approval flush carries the pending payload and asserts it; the terminal one clears it.
    assert w.snapshots.upserts[1]["pending_approval"] == {"tool": "shell", "preview": "rm -rf /"}
    assert w.snapshots.upserts[1]["assert_pending_approval"] is True
    assert w.snapshots.upserts[2]["pending_approval"] is None
    assert w.snapshots.upserts[2]["assert_pending_approval"] is True
    # Text accumulated across the throttled deltas is present on the terminal flush.
    assert w.snapshots.upserts[2]["result_text"] == "abc"


@respx.mock
async def test_throttled_delta_flush_does_not_assert_pending_approval() -> None:
    # C3 (backend-reviewer): the client may answer POST …/approval OUT OF BAND, so a throttled text
    # flush — which knows nothing about the approval state — must NOT re-assert the relay's cached
    # {tool, preview}. It writes with assert_pending_approval=False, leaving the stored NULL alone.
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}))
    body = _sse("approval.request", '{"tool":"shell","preview":"p"}') + _delta("a") + _delta("b")
    _events_route(body)
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    approval_flush, *delta_flushes = w.snapshots.upserts
    assert approval_flush["assert_pending_approval"] is True
    assert delta_flushes, "the delta flushes must reach the repository"
    for flush in delta_flushes:
        assert flush["assert_pending_approval"] is False, flush


@respx.mock
async def test_tool_events_set_last_tool_and_clear_pending_approval() -> None:
    w = _wire()
    body = (
        _sse("approval.request", '{"tool":"shell","preview":"p"}')
        + _sse("tool.started", '{"tool":"files.write"}')
        + _sse("tool.completed", '{"tool":"files.write"}')
    )
    _events_route(body)
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    approval, started, completed = w.snapshots.upserts
    assert approval["pending_approval"] == {"tool": "shell", "preview": "p"}
    # The agent moved on → the approval is resolved, and it is ASSERTED (authoritative event).
    assert started["last_tool"] == "files.write"
    assert started["pending_approval"] is None
    assert started["assert_pending_approval"] is True
    assert completed["pending_approval"] is None


@respx.mock
async def test_approval_passthrough_clears_pending_approval_owner_scoped() -> None:
    # Third clearing point (ADR-066 §6): without it the derived waiting_approval would stick after
    # the user already answered. Owner-scoped (run_id, user_id).
    w = _wire()
    respx.post(f"{_BASE_URL}/v1/runs/run_1/approval").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    uid = uuid.uuid4()
    await w.svc.approval(user_id=uid, run_id="run_1", body={"choice": "once"})
    assert w.snapshots.clear_calls == [("run_1", uid)]


@respx.mock
async def test_approval_non_2xx_does_not_clear_pending_approval() -> None:
    # The clear happens only AFTER a successful passthrough: a failed answer leaves the run waiting.
    w = _wire()
    respx.post(f"{_BASE_URL}/v1/runs/run_1/approval").mock(return_value=httpx.Response(503))
    with pytest.raises(Exception):  # noqa: B017 - UpstreamError (502 surface)
        await w.svc.approval(user_id=uuid.uuid4(), run_id="run_1", body={"choice": "once"})
    assert w.snapshots.clear_calls == []


@respx.mock
async def test_approval_preview_is_capped_by_the_result_text_knob() -> None:
    # The preview is user content stored in JSONB and must not be unbounded; it reuses the
    # result-text cap (no separate knob).
    w = _wire(_settings(**{"AGENT_STATE_RESULT_TEXT_MAX_CHARS": 5}))
    _events_route(_sse("approval.request", json.dumps({"tool": "shell", "preview": "x" * 50})))
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert w.snapshots.upserts[0]["pending_approval"] == {"tool": "shell", "preview": "xxxxx"}


# ============================================================================
# Lifecycle statuses in agent_runs (ADR-066 §3) — written from the relay, always conditional.
# ============================================================================
@respx.mock
async def test_run_failed_records_failed_status_and_final_flush() -> None:
    # Before ADR-066 a crashed run stayed 'running' forever. No debit on run.failed (ADR-047 §4).
    w = _wire()
    _events_route(_delta("partial") + _sse("run.failed", '{"error":"boom"}'))
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert w.runs.mark_status_calls == [("run_1", "failed")]
    assert w.wallet.calls == []
    assert w.snapshots.upserts[-1]["result_text"] == "partial"
    assert w.snapshots.upserts[-1]["pending_approval"] is None


@respx.mock
async def test_run_completed_records_status_before_billing() -> None:
    # ADR-066 §3: the lifecycle status is recorded in the run.completed HANDLER, before the debit —
    # so it can never be skipped by a billing failure. Asserted by ordering, not by mere presence.
    w = _wire()
    _events_route(_completed(2000, 1000))
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert w.runs.mark_status_calls == [("run_1", "completed")]
    assert len(w.wallet.calls) == 1
    # The wallet double snapshots the runs-repo state it observed: the status was already written.
    assert w.wallet.marks_at_consume == [[("run_1", "completed")]]


@respx.mock
async def test_run_completed_records_status_even_when_billing_raises() -> None:
    # C1 (backend-reviewer): an unexpected wallet failure (RuntimeError → the generic rollback
    # branch of _bill_completed) used to leave the run 'running' forever and make /state lie
    # indefinitely. The status must be persisted regardless, and the relay must not break.
    wallet = FakeWallet()
    wallet.raise_exc = RuntimeError("wallet exploded")
    w = _wire(wallet=wallet)
    body = _completed(2000, 1000)
    _events_route(body)
    with _capture_service_logs() as logs:
        relayed = await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert relayed == body  # the stream completed normally
    assert w.runs.mark_status_calls == [("run_1", "completed")]
    assert any("billing failed" in m for m in logs.messages)
    # The generic branch rolled back the dirty billing state; the status commit already happened.
    assert w.session.rollback_calls == 1


@respx.mock
async def test_client_stop_marks_cancelled_owner_scoped_after_passthrough() -> None:
    # ADR-066 §3: the CLIENT stop path records `cancelled` — with the user_id in the UPDATE itself
    # (Hermes answers 2xx for unknown/foreign runs, idempotent-stop semantics).
    w = _wire()
    route = respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(
        return_value=httpx.Response(200, json={"stopped": True})
    )
    uid = uuid.uuid4()
    out = await w.svc.stop(user_id=uid, run_id="run_1")
    assert out == {"stopped": True}
    assert route.called
    assert w.runs.mark_stopped_calls == [("run_1", uid)]
    # mark_status is NOT the vehicle for cancelled (call-site invariant, ADR-066 §3).
    assert w.runs.mark_status_calls == []


@respx.mock
async def test_stop_non_2xx_does_not_mark_cancelled() -> None:
    # The status flip happens only AFTER a successful passthrough.
    w = _wire()
    respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(return_value=httpx.Response(503))
    with pytest.raises(Exception):  # noqa: B017 - UpstreamError (502 surface)
        await w.svc.stop(user_id=uuid.uuid4(), run_id="run_1")
    assert w.runs.mark_stopped_calls == []


@respx.mock
async def test_pause_at_zero_never_routes_through_mark_stopped() -> None:
    # B4 / ADR-066 §3 (MAJOR): pause-at-zero interrupts Hermes with the SAME POST …/stop transport,
    # but through _interrupt_run — which records NO status. Marking `cancelled` there would make a
    # credits-exhausted run transiently read as `stopped` (no top-up offer) and answer 409
    # run_not_resumable to POST /resume inside that window.
    wallet = FakeWallet()
    wallet.current_balance_value = 0  # nothing left to charge → depleted
    w = _wire(_settings(**{"AGENT_INCREMENTAL_BILLING_ENABLED": True}), wallet=wallet)
    stop_route = respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(
        return_value=httpx.Response(200, json={"stopped": True})
    )
    body = _sse(
        "usage.delta",
        json.dumps(
            {
                "step_index": 0,
                "cumulative_input_tokens": 5000,
                "cumulative_output_tokens": 5000,
                "model": "m",
            }
        ),
    )
    _events_route(body)
    relayed = await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))

    # The Hermes interrupt DID happen (the run is really stopped upstream)...
    assert stop_route.called
    # ... but the observable status sequence is running → paused, with NO `cancelled` in between.
    assert w.runs.mark_stopped_calls == []
    assert w.runs.mark_status_calls == []
    assert w.runs.mark_paused_calls == [("run_1", "credits_exhausted")]
    # A synthetic terminal run.paused closes the stream (no run.completed follows).
    assert b'"event": "run.paused"' in relayed
    assert b"run.completed" not in relayed
    # Nothing is awaited from the client any more.
    assert w.snapshots.upserts[-1]["pending_approval"] is None


# ============================================================================
# C4. Billing flag OFF — the writer still runs; tokens come from run.completed{usage}.
# ============================================================================
@respx.mock
async def test_flag_off_snapshot_tokens_come_from_run_completed_usage() -> None:
    # ADR-066 §6: with agent_incremental_billing_enabled=false there are NO usage.delta events, so
    # the terminal usage payload is the only token source. Text/tools/approval are written as usual.
    w = _wire(_settings(**{"AGENT_INCREMENTAL_BILLING_ENABLED": False}))
    body = _delta("hello ") + _sse("tool.started", '{"tool":"files.read"}') + _completed(2000, 1000)
    _events_route(body)
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    # The ledger seed is NOT read with the flag OFF (charged stays 0 → post-hoc ADR-047 billing).
    assert w.wallet.charged_for_run_calls == []
    final = w.snapshots.upserts[-1]
    assert final["input_tokens"] == 2000
    assert final["output_tokens"] == 1000
    assert final["result_text"] == "hello "
    assert final["last_tool"] == "files.read"
    assert w.runs.mark_status_calls == [("run_1", "completed")]


@respx.mock
async def test_snapshot_token_counters_are_monotonic_across_events() -> None:
    # usage.delta anchors are recorded even with billing OFF (a patched image may emit them), and a
    # LOWER terminal usage payload must never pull the counters back down (max()).
    w = _wire(
        _settings(
            **{
                "AGENT_INCREMENTAL_BILLING_ENABLED": False,
                "AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0,
            }
        )
    )
    body = (
        _sse(
            "usage.delta",
            json.dumps({"cumulative_input_tokens": 9000, "cumulative_output_tokens": 4000}),
        )
        + _delta("x")
        + _completed(10, 5)  # a smaller/partial terminal payload
    )
    _events_route(body)
    await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    final = w.snapshots.upserts[-1]
    assert final["input_tokens"] == 9000
    assert final["output_tokens"] == 4000


# ============================================================================
# Robustness: a snapshot persistence failure must never break the relay (ADR-066 §6).
# ============================================================================
@respx.mock
async def test_snapshot_write_failure_does_not_break_relay_and_logs_no_user_content() -> None:
    class _FailingSnapshots(FakeSnapshotsRepo):
        async def upsert(self, **kwargs: Any) -> None:  # type: ignore[override]
            self.upserts.append(kwargs)
            raise SQLAlchemyError("agent_runs parent row missing (FK)")

    snapshots = _FailingSnapshots()
    w = _wire(snapshots=snapshots)
    secret_text = "SUPER-PRIVATE-MODEL-OUTPUT"
    body = _delta(secret_text) + _completed(1000, 0)
    _events_route(body)
    with _capture_service_logs() as logs:
        relayed = await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert relayed == body  # relay intact
    assert any("snapshot write failed" in m for m in logs.messages)
    # ADR-066 §5: result_text / the approval preview are user content and must NEVER be logged.
    logged = "\n".join(logs.messages)
    assert secret_text not in logged
    assert _API_KEY not in logged
    # Each failed write is rolled back so the dirty state is not carried into the rest of the
    # stream; billing on the terminal event still proceeds.
    assert w.session.rollback_calls == len(snapshots.upserts)
    assert len(w.wallet.calls) == 1


# ============================================================================
# One-shot anomaly latches (_log_write_anomaly) — one line per relay, no user content
# ============================================================================
@respx.mock
async def test_tenancy_rejection_is_warned_exactly_once_per_relay() -> None:
    # A run_id colliding across tenants (Q-066-2) makes EVERY flush of this relay a no-op. The
    # condition persists for the whole run, so without the latch a long run would emit a WARNING
    # every few seconds; with it, one line carries the same information.
    snapshots = FakeSnapshotsRepo()
    snapshots.force_tenancy_skip = True
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}), snapshots=snapshots)
    secret = "ПРИВАТНЫЙ-ТЕКСТ-МОДЕЛИ"
    body = (
        _delta(secret)
        + _sse("tool.started", '{"tool":"files.write"}')
        + _delta("ещё")
        + _sse("approval.request", '{"tool":"shell","preview":"ПРЕДПРОСМОТР-СЕКРЕТ"}')
        + _completed(1000, 500)
    )
    _events_route(body)
    with _capture_at_level(logging.DEBUG) as logs:
        relayed = await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))

    # Several flushes were attempted and all were refused...
    assert len(snapshots.upserts) >= 4
    # ... yet exactly ONE warning was emitted (the latch), not one per flush.
    warnings = logs.matching("snapshot upsert skipped (tenancy)")
    assert len(warnings) == 1, warnings
    assert logs.records[0].levelno == logging.WARNING
    assert "run_1" in warnings[0]
    # The relay itself is unaffected — a snapshot that cannot be written is not a client-facing
    # failure (the run keeps streaming and billing).
    assert relayed == body
    assert len(w.wallet.calls) == 1
    # ADR-066 §5: ids and lengths only — never result_text nor the approval preview.
    logged = "\n".join(logs.messages())
    assert secret not in logged
    assert "ПРЕДПРОСМОТР-СЕКРЕТ" not in logged
    assert _API_KEY not in logged


@respx.mock
async def test_frozen_result_text_is_logged_at_debug_exactly_once_per_relay() -> None:
    # The second silent refusal: the row IS written but result_text does not advance, because the
    # incoming text does not continue the stored one (Q-066-1). DEBUG, not WARNING — the run keeps
    # working and the snapshot simply freezes at its fullest known text.
    snapshots = FakeSnapshotsRepo()
    uid = uuid.uuid4()
    # Pre-seed a stored value this relay's text will never continue (a diverging earlier consumer).
    snapshots.stored["run_1"] = {"user_id": uid, "result_text": "ЧУЖОЕ"}
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}), snapshots=snapshots)
    body = _delta("свой-длинный-текст") + _delta("-ещё-длиннее") + _completed(1000, 0)
    _events_route(body)
    with _capture_at_level(logging.DEBUG) as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    frozen = logs.matching("result_text frozen")
    assert len(frozen) == 1, frozen
    assert logs.records[0].levelno == logging.DEBUG
    # Only ids and LENGTHS travel into the log line.
    assert "stored=5" in frozen[0]  # len("ЧУЖОЕ")
    assert "submitted=" in frozen[0]
    assert "свой-длинный-текст" not in frozen[0]
    # No WARNING: a frozen text is not an operational failure.
    assert logs.messages(logging.WARNING) == []
    # The stored value really was preserved (the fake mirrors the SQL prefix guard).
    assert snapshots.stored["run_1"]["result_text"] == "ЧУЖОЕ"


@respx.mock
async def test_no_anomaly_logging_on_the_normal_path() -> None:
    # Belt-and-braces for the latches: an ordinary relay (text honestly continuing, single tenant)
    # must produce NEITHER line — otherwise the signals would be noise and get ignored in prod.
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}))
    _events_route(_delta("а") + _delta("б") + _delta("в") + _completed(1000, 0))
    with _capture_at_level(logging.DEBUG) as logs:
        await _collect(w.svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert logs.matching("tenancy") == []
    assert logs.matching("frozen") == []


@respx.mock
async def test_latches_are_per_relay_not_per_process() -> None:
    # The latch lives in _RelayState (one stream_events call), so a NEW subscription reports the
    # condition again — a persistent collision must stay visible across reconnects, just not spam
    # within one stream.
    snapshots = FakeSnapshotsRepo()
    snapshots.force_tenancy_skip = True
    w = _wire(_settings(**{"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}), snapshots=snapshots)
    uid = uuid.uuid4()
    body = _delta("a") + _delta("b") + _completed(1000, 0)
    with _capture_at_level(logging.DEBUG) as logs:
        _events_route(body)
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
        assert len(logs.matching("snapshot upsert skipped (tenancy)")) == 1
        respx.reset()
        _events_route(body)
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert len(logs.matching("snapshot upsert skipped (tenancy)")) == 2
