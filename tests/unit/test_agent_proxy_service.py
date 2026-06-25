"""Unit tests for AgentProxyService: launch, SSE relay + billing, approval/stop (ADR-045/047).

The Hermes instance is mocked at the HTTP boundary (respx) and ``HermesInstanceManager`` /
``WalletService`` / ``AuditService`` are faked, so the proxy logic is isolated from Docker, the
DB and the ledger (06-testing-strategy.md §Политика моков; agent-proxy/09-testing.md §Unit).

Covers follow_up_for_qa #1-3, #5-10:
- run launch: policy blocked (200, no wake, no debit, no Hermes call), allowed (ensure_running +
  proxy POST /v1/runs with Bearer + body mapping), upstream failures → 502.
- SSE relay: events forwarded verbatim; run.completed → one debit (idempotency_key=runId),
  zero usage → no debit, run.failed → no debit, duplicate/replayed run.completed → one debit;
  billing failure (InsufficientCreditsError) does not break the relay; meta shape.
- approval/stop passthrough with correct runId + Bearer; non-2xx / transport → 502.
- security: the instance Bearer / api_key never appears in relayed bytes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent_proxy.service import AgentProxyService
from app.audit.service import EVENT_BILLING_DEBIT, EVENT_BILLING_DEBIT_INSUFFICIENT
from app.config import Settings
from app.errors import InsufficientCreditsError, UpstreamError
from app.hermes_runtime.manager import InstanceEndpoint
from app.policy.engine import BlockReason
from app.wallet.service import ConsumeResult

# --- Constants / fixtures -------------------------------------------------------------------

_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"  # the decrypted API_SERVER_KEY.


@pytest.fixture
def settings() -> Settings:
    # Real Settings with defaults (CREDITS_PER_1K_INPUT=1.0, OUTPUT=5.0; short timeouts).
    return Settings()  # type: ignore[call-arg]


class FakeManager:
    """Stand-in for HermesInstanceManager. Records ensure_running calls; returns a fixed ep."""

    def __init__(self, *, endpoint: InstanceEndpoint | None = None) -> None:
        self.endpoint = endpoint or InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)
        self.ensure_running_calls: list[uuid.UUID] = []

    async def ensure_running(self, user_id: uuid.UUID) -> InstanceEndpoint:
        self.ensure_running_calls.append(user_id)
        return self.endpoint


class FakeWallet:
    """Stand-in for WalletService.consume. Records each call; scriptable replay / exception."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_exc: Exception | None = None
        self._replay_keys: set[str] = set()
        # ADR-047 §6: on InsufficientCreditsError the relay records a billing_debit_insufficient
        # audit event whose payload carries the CURRENT balance — so the proxy now calls
        # WalletService.current_balance(user_id). Mirror that on the double (scriptable).
        self.current_balance_value = 0
        self.current_balance_calls: list[uuid.UUID] = []
        # ADR-051 §4: the run debt-gate calls WalletService.current_debt(user_id) before
        # ensure_running (AGENT_DEBT_RECONCILE_ENABLED default true). Default 0 → not blocked by
        # debt; scriptable so a debt-gate test can drive a positive value.
        self.current_debt_value = 0
        self.current_debt_calls: list[uuid.UUID] = []

    async def current_balance(self, user_id: uuid.UUID) -> int:
        self.current_balance_calls.append(user_id)
        return self.current_balance_value

    async def current_debt(self, user_id: uuid.UUID) -> int:
        self.current_debt_calls.append(user_id)
        return self.current_debt_value

    async def consume(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
        session_id: uuid.UUID | None = None,
    ) -> ConsumeResult:
        self.calls.append(
            {
                "user_id": user_id,
                "amount": amount,
                "idempotency_key": idempotency_key,
                "meta": meta,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        replay = idempotency_key in self._replay_keys
        self._replay_keys.add(idempotency_key)
        return ConsumeResult(new_balance=100, ledger_tx_id=uuid.uuid4(), idempotent_replay=replay)


class FakeAudit:
    """Stand-in for AuditService.record. Records events (used to assert no-secrets / phases)."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


class FakePolicyState:
    """Minimal PolicyState double; only the attributes evaluate() reads matter."""


class FakeSession:
    """Stand-in for AsyncSession used by _bill_completed (ADR-047 §6, ADR-051 §2.1).

    ``_bill_completed`` runs INSIDE the StreamingResponse body generator and must persist the
    billing state with its OWN ``await self._session.commit()`` (success + insufficient branches),
    rolling back on a generic billing failure. The unit tests stub the wallet/audit at the service
    boundary, so this double only needs to count commit/rollback calls (no real DB) — letting the
    tests both run (no AttributeError on ``object()``) AND assert the streaming-context commit
    actually happened (masking-regression guard: the test verifies the persistence call, not merely
    tolerates it).
    """

    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _make_service(
    *,
    settings: Settings,
    manager: FakeManager | None = None,
    wallet: FakeWallet | None = None,
    audit: FakeAudit | None = None,
    session: FakeSession | None = None,
) -> tuple[AgentProxyService, FakeManager, FakeWallet, FakeAudit]:
    mgr = manager or FakeManager()
    wal = wallet or FakeWallet()
    aud = audit or FakeAudit()
    svc = AgentProxyService(
        session=session or FakeSession(),  # type: ignore[arg-type]  # commit/rollback are stubbed.
        manager=mgr,  # type: ignore[arg-type]
        wallet=wal,  # type: ignore[arg-type]
        audit=aud,  # type: ignore[arg-type]
        settings=settings,
    )
    return svc, mgr, wal, aud


def _patch_policy(monkeypatch: pytest.MonkeyPatch, decision: Any) -> None:
    """Patch the loader + evaluate used inside service.run so no DB/policy I/O happens."""
    import app.agent_proxy.service as service_mod

    async def _fake_load_policy_state(_session: Any, _user_id: uuid.UUID) -> Any:
        return FakePolicyState()

    def _fake_evaluate(_state: Any, _mode: Any) -> Any:
        return decision

    monkeypatch.setattr(service_mod, "load_policy_state", _fake_load_policy_state)
    monkeypatch.setattr(service_mod, "evaluate", _fake_evaluate)


class _Decision:
    def __init__(self, allow: bool, block_reason: BlockReason | None = None) -> None:
        self.allow = allow
        self.block_reason = block_reason


def _sse(name: str, data_json: str) -> bytes:
    return f"event: {name}\ndata: {data_json}\n\n".encode()


class _ListHandler(logging.Handler):
    """Collects formatted records emitted on the logger it is attached to."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _capture_service_logs:
    """Capture WARNING+ logs from ``app.agent_proxy.service`` directly on its logger.

    Hermetic against cross-test logging pollution (mirrors the established pattern in
    ``test_anthropic_upstream_error_logging.py``): a prior integration test that calls
    ``create_app()`` → ``configure_logging()`` (which runs ``root.handlers.clear()``) combined with
    pytest's logging plugin leaves the ``app.agent_proxy.service`` logger with ``disabled=True``,
    which would silently drop the captured records (order-dependent flake — caplog stays empty when
    this module runs after the openapi documentation module). We attach our own handler to the
    NAMED logger AND force-enable it for the duration of the block, restoring the flag/level after. In
    production the logger is always enabled.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("app.agent_proxy.service")
        self._handler = _ListHandler()
        self._prev_level = self._logger.level
        self._prev_disabled = self._logger.disabled

    def __enter__(self) -> _ListHandler:
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.WARNING)
        self._logger.disabled = False
        return self._handler

    def __exit__(self, *_exc: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)
        self._logger.disabled = self._prev_disabled

    @property
    def text(self) -> str:
        return "\n".join(self._handler.messages)


# ============================================================================
# 1. POST /v1/agent/run — policy blocked
# ============================================================================
# The achievable credits-branch reasons for the agent path (02-api-contracts.md §Достижимый набор;
# service._AGENT_BLOCK_REASONS). `subscription_required` is byok-only and NOT reachable here.
@pytest.mark.parametrize(
    "reason",
    [
        BlockReason.trial_used,
        BlockReason.subscription_expired,
        BlockReason.credits_empty,
    ],
)
@respx.mock
async def test_run_policy_blocked_no_wake_no_debit_no_hermes(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    reason: BlockReason,
) -> None:
    _patch_policy(monkeypatch, _Decision(allow=False, block_reason=reason))
    svc, mgr, wal, _aud = _make_service(settings=settings)
    # respx with no routes registered: any HTTP call would raise → proves Hermes was NOT called.
    with _capture_service_logs() as logs:
        result = await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)
    assert result.blocked is True
    assert result.block_reason == reason.value
    assert result.run_id is None
    # ensure_running must NOT be called (container not woken) and no debit happened.
    assert mgr.ensure_running_calls == []
    assert wal.calls == []
    assert respx.calls.call_count == 0
    # All three are achievable reasons → the defensive branch must NOT log them as unexpected.
    assert not any("unexpected reason" in m for m in logs.messages)


@respx.mock
async def test_run_blocked_trial_used_is_legitimate_no_unexpected_log(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Legitimate trial_used (subscription=none, trial spent) → 200 blocked, blockReason=trial_used,
    # WITHOUT the defensive "unexpected reason" warning (02-api-contracts.md §Достижимый набор:
    # trial_used is in _AGENT_BLOCK_REASONS after the docs↔code sync).
    _patch_policy(monkeypatch, _Decision(allow=False, block_reason=BlockReason.trial_used))
    svc, mgr, wal, _aud = _make_service(settings=settings)
    with _capture_service_logs() as logs:
        result = await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)
    assert result.blocked is True
    assert result.block_reason == "trial_used"
    assert result.run_id is None
    assert mgr.ensure_running_calls == []
    assert wal.calls == []
    assert respx.calls.call_count == 0
    assert not any("unexpected reason" in m for m in logs.messages)


@respx.mock
async def test_run_blocked_truly_unexpected_reason_is_logged(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard the defensive branch itself: a reason OUTSIDE _AGENT_BLOCK_REASONS (e.g. an
    # unreachable byok reason leaking through) IS still logged as unexpected. Uses
    # subscription_required, which is byok-only and must never occur on the agent credits path.
    _patch_policy(
        monkeypatch, _Decision(allow=False, block_reason=BlockReason.subscription_required)
    )
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    with _capture_service_logs() as logs:
        result = await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)
    assert result.blocked is True
    assert result.block_reason == "subscription_required"
    assert any("unexpected reason" in m for m in logs.messages)


# ============================================================================
# 2. POST /v1/agent/run — allowed: ensure_running + proxy POST /v1/runs + body mapping + Bearer
# ============================================================================
@respx.mock
async def test_run_allowed_proxies_with_bearer_and_body_mapping(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_policy(monkeypatch, _Decision(allow=True))
    svc, mgr, _wal, _aud = _make_service(settings=settings)
    route = respx.post(f"{_BASE_URL}/v1/runs").mock(
        return_value=httpx.Response(202, json={"run_id": "run_abc", "status": "running"})
    )
    uid = uuid.uuid4()
    result = await svc.run(
        user_id=uid, message="build me a site", session_id="sess-1", model="claude-x"
    )
    assert result.blocked is False
    assert result.run_id == "run_abc"
    assert result.status == "running"
    assert mgr.ensure_running_calls == [uid]
    # Verify the proxied request: Bearer header + mapped body.
    assert route.called
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {_API_KEY}"
    import json as _json

    sent = _json.loads(req.content)
    assert sent == {"input": "build me a site", "session_id": "sess-1", "model": "claude-x"}


@respx.mock
async def test_run_allowed_omits_optional_fields(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # sessionId/model None → not sent (only `input`); status normalized to queued when missing.
    _patch_policy(monkeypatch, _Decision(allow=True))
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    route = respx.post(f"{_BASE_URL}/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "run_xyz"})
    )
    result = await svc.run(user_id=uuid.uuid4(), message="hello", session_id=None, model=None)
    assert result.run_id == "run_xyz"
    assert result.status == "queued"  # normalized from missing/unknown status.
    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"input": "hello"}


# ============================================================================
# 3. POST /v1/agent/run — upstream failures → 502 (UpstreamError), never 200 blocked
# ============================================================================
@respx.mock
async def test_run_transport_error_is_502(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_policy(monkeypatch, _Decision(allow=True))
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    respx.post(f"{_BASE_URL}/v1/runs").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(UpstreamError):
        await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)


@respx.mock
async def test_run_non_2xx_is_502(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_policy(monkeypatch, _Decision(allow=True))
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    respx.post(f"{_BASE_URL}/v1/runs").mock(return_value=httpx.Response(500, json={"e": "boom"}))
    with pytest.raises(UpstreamError):
        await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)


@respx.mock
async def test_run_missing_run_id_is_502(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_policy(monkeypatch, _Decision(allow=True))
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    respx.post(f"{_BASE_URL}/v1/runs").mock(
        return_value=httpx.Response(202, json={"status": "queued"})  # no run_id
    )
    with pytest.raises(UpstreamError):
        await svc.run(user_id=uuid.uuid4(), message="hi", session_id=None, model=None)


# ============================================================================
# 5-7. SSE relay + billing
# ============================================================================
async def _collect(stream: Any) -> bytes:
    out = b""
    async for chunk in stream:
        out += chunk
    return out


def _events_route(body: bytes, run_id: str = "run_1", status: int = 200) -> Any:
    return respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(status, content=body)
    )


@respx.mock
async def test_sse_relays_events_verbatim_and_bills_on_completed(
    settings: Settings,
) -> None:
    sess = FakeSession()
    svc, _mgr, wal, _aud = _make_service(settings=settings, session=sess)
    body = (
        _sse("run.running", '{"run_id":"run_1"}')
        + _sse("message.delta", '{"text":"hel"}')
        + _sse("tool.started", '{"tool":"files.write"}')
        + _sse("tool.completed", '{"tool":"files.write"}')
        + _sse("approval.request", '{"tool":"shell"}')
        + _sse("run.completed", '{"usage":{"input_tokens":2000,"output_tokens":1000},"model":"m"}')
    )
    _events_route(body)
    relayed = await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    # All original event blocks pass through unchanged.
    assert b"event: run.running" in relayed
    assert b"event: message.delta" in relayed
    assert b"event: tool.started" in relayed
    assert b"event: approval.request" in relayed
    assert b"event: run.completed" in relayed
    assert relayed == body
    # Exactly one debit: amount = ceil(2000/1000*1.0 + 1000/1000*5.0) = ceil(7.0) = 7.
    assert len(wal.calls) == 1
    call = wal.calls[0]
    assert call["amount"] == 7
    assert call["idempotency_key"] == "run_1"
    assert call["meta"]["source"] == "agent_run"
    assert call["meta"]["runId"] == "run_1"
    assert call["meta"]["model"] == "m"
    assert call["meta"]["usage"] == {
        "input_tokens": 2000,
        "output_tokens": 1000,
        "total_tokens": 0,
    }
    # ADR-047 §6: _bill_completed persists the debit with its OWN streaming-context commit (it runs
    # after the request session teardown), and never rolls it back on the success path.
    assert sess.commit_calls == 1
    assert sess.rollback_calls == 0


@respx.mock
async def test_sse_keepalive_comment_relayed(settings: Settings) -> None:
    svc, _mgr, wal, _aud = _make_service(settings=settings)
    body = b": keepalive\n\n" + _sse("run.running", '{"run_id":"run_1"}')
    _events_route(body)
    relayed = await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert b": keepalive" in relayed
    assert wal.calls == []  # no run.completed → no debit.


@respx.mock
async def test_sse_zero_usage_no_debit(settings: Settings) -> None:
    svc, _mgr, wal, _aud = _make_service(settings=settings)
    body = _sse("run.completed", '{"usage":{"input_tokens":0,"output_tokens":0}}')
    _events_route(body)
    await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    # amount == 0 → consume is NOT called.
    assert wal.calls == []


@respx.mock
async def test_sse_run_failed_no_debit(settings: Settings) -> None:
    svc, _mgr, wal, _aud = _make_service(settings=settings)
    body = _sse("run.failed", '{"error":"boom"}')
    _events_route(body)
    relayed = await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert b"event: run.failed" in relayed
    assert wal.calls == []


@respx.mock
async def test_sse_duplicate_completed_debits_once_in_stream(settings: Settings) -> None:
    # Two run.completed blocks in one stream → only the first triggers a debit (billed flag).
    sess = FakeSession()
    svc, _mgr, wal, _aud = _make_service(settings=settings, session=sess)
    completed = _sse("run.completed", '{"usage":{"input_tokens":1000,"output_tokens":0}}')
    _events_route(completed + completed)
    await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert len(wal.calls) == 1
    # Only the first run.completed bills AND commits; the `billed` flag suppresses the second.
    assert sess.commit_calls == 1


@respx.mock
async def test_sse_restream_same_run_id_idempotent(settings: Settings) -> None:
    # Re-subscribing to the same runId: the wallet idempotency by runId yields one effective debit.
    sess = FakeSession()
    svc, _mgr, wal, _aud = _make_service(settings=settings, session=sess)
    completed = _sse("run.completed", '{"usage":{"input_tokens":1000,"output_tokens":0}}')
    uid = uuid.uuid4()
    _events_route(completed)
    await _collect(svc.stream_events(user_id=uid, run_id="run_1"))
    await _collect(svc.stream_events(user_id=uid, run_id="run_1"))
    # consume called twice (once per stream) but with the SAME idempotency_key → one ledger debit.
    assert len(wal.calls) == 2
    assert {c["idempotency_key"] for c in wal.calls} == {"run_1"}
    assert wal.calls[1]["amount"] == wal.calls[0]["amount"]
    # Each subscription commits its own streaming-context state; the second is a harmless no-op
    # commit on the ON-CONFLICT replay (ADR-047 §4) — never a rollback.
    assert sess.commit_calls == 2
    assert sess.rollback_calls == 0


# ============================================================================
# 8. Billing failure does not break the stream; meta shape on insufficient credits
# ============================================================================
@respx.mock
async def test_sse_billing_failure_does_not_break_relay(settings: Settings) -> None:
    # ADR-047 §6: InsufficientCreditsError on run.completed must NOT break the relay and must be
    # recorded as a billing_debit_insufficient audit event (NOT a ledger row) carrying the current
    # balance; no secrets in the payload.
    sess = FakeSession()
    svc, _mgr, wal, aud = _make_service(settings=settings, session=sess)
    wal.raise_exc = InsufficientCreditsError("insufficient_credits")
    wal.current_balance_value = 3  # balance recorded in the insufficient audit payload.
    uid = uuid.uuid4()
    body = _sse("message.delta", '{"text":"hi"}') + _sse(
        "run.completed",
        '{"usage":{"input_tokens":1000,"output_tokens":0,"total_tokens":1000},"model":"m"}',
    )
    _events_route(body)
    # The relay must complete without raising even though consume() raised.
    relayed = await _collect(svc.stream_events(user_id=uid, run_id="run_1"))
    assert relayed == body
    assert len(wal.calls) == 1
    meta = wal.calls[0]["meta"]
    assert meta["source"] == "agent_run"
    assert meta["runId"] == "run_1"
    assert meta["model"] == "m"
    assert "usage" in meta
    # The relay queried the current balance for the audit payload.
    assert wal.current_balance_calls == [uid]
    # Exactly one billing_debit_insufficient audit event with the expected payload (no debit audit).
    ins = [e for e in aud.events if e.event_type == EVENT_BILLING_DEBIT_INSUFFICIENT]
    assert len(ins) == 1
    payload = ins[0].payload
    assert payload["runId"] == "run_1"
    assert payload["amount"] == 1  # ceil(1000/1000*1 + 0) = 1.
    assert payload["balance"] == 3
    assert payload["model"] == "m"
    assert payload["usage"] == {"input_tokens": 1000, "output_tokens": 0, "total_tokens": 1000}
    # No successful billing_debit audit emitted on the insufficient path.
    assert not [e for e in aud.events if e.event_type == EVENT_BILLING_DEBIT]
    # No secrets / instance bearer in the relayed bytes or audit payload.
    assert _API_KEY.encode() not in relayed
    assert _API_KEY not in str(payload)
    # ADR-047 §6: the insufficient branch persists the billing_debit_insufficient audit with its OWN
    # streaming-context commit (not a rollback) so the uncharged delta is not lost on teardown.
    assert sess.commit_calls == 1
    assert sess.rollback_calls == 0


@respx.mock
async def test_sse_events_stream_non_2xx_is_502(settings: Settings) -> None:
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    _events_route(b"", status=500)
    with pytest.raises(UpstreamError):
        await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))


# ============================================================================
# 9. approval / stop passthrough
# ============================================================================
@respx.mock
async def test_approval_passthrough_with_bearer(settings: Settings) -> None:
    svc, mgr, _wal, _aud = _make_service(settings=settings)
    route = respx.post(f"{_BASE_URL}/v1/runs/run_1/approval").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    uid = uuid.uuid4()
    out = await svc.approval(user_id=uid, run_id="run_1", body={"choice": "once"})
    assert out == {"ok": True}
    assert mgr.ensure_running_calls == [uid]
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {_API_KEY}"
    import json as _json

    assert _json.loads(req.content) == {"choice": "once"}


@respx.mock
async def test_stop_passthrough_with_bearer(settings: Settings) -> None:
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    route = respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(
        return_value=httpx.Response(200, json={"stopped": True})
    )
    out = await svc.stop(user_id=uuid.uuid4(), run_id="run_1")
    assert out == {"stopped": True}
    assert route.calls.last.request.headers["authorization"] == f"Bearer {_API_KEY}"


@respx.mock
async def test_approval_non_2xx_is_502(settings: Settings) -> None:
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    respx.post(f"{_BASE_URL}/v1/runs/run_1/approval").mock(
        return_value=httpx.Response(503, json={"e": "x"})
    )
    with pytest.raises(UpstreamError):
        await svc.approval(user_id=uuid.uuid4(), run_id="run_1", body={"choice": "deny"})


@respx.mock
async def test_stop_transport_error_is_502(settings: Settings) -> None:
    svc, _mgr, _wal, _aud = _make_service(settings=settings)
    respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(UpstreamError):
        await svc.stop(user_id=uuid.uuid4(), run_id="run_1")


# ============================================================================
# 10. Security: the instance Bearer / api_key never leaks into the relayed client bytes
# ============================================================================
@respx.mock
async def test_api_key_never_in_relayed_bytes(settings: Settings) -> None:
    svc, _mgr, _wal, _aud = _make_service(settings=settings, session=FakeSession())
    body = _sse("run.running", '{"run_id":"run_1"}') + _sse(
        "run.completed", '{"usage":{"input_tokens":1000,"output_tokens":0},"model":"m"}'
    )
    _events_route(body)
    relayed = await _collect(svc.stream_events(user_id=uuid.uuid4(), run_id="run_1"))
    assert _API_KEY.encode() not in relayed
    assert b"Authorization" not in relayed
    assert b"Bearer" not in relayed
