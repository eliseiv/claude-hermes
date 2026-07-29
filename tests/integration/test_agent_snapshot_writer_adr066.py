"""Integration: relay snapshot writer, lifecycle statuses and retention sweep (ADR-066).

Real PostgreSQL (testcontainers) with the REAL repositories, wallet and audit — only the Hermes
instance boundary is mocked (respx), per 06-testing-strategy.md §Политика моков. This is where the
SQL that carries the ADR-066 invariants is actually exercised: the PER-COLUMN replay-guard of the
``ON CONFLICT`` upsert, the conditional status transitions, the owner-scoped writes and the
idempotent retention sweep.

Regression cases required by agent-proxy/09-testing.md (each MUST fail on the implementation it
guards against):
1. **Replay window (per-column guard).** A re-subscription replays Hermes' buffer FROM THE START, so
   an incoming short prefix must not shorten ``result_text`` — while ``approval.request`` in that
   same window still sets ``pending_approval`` and ``last_tool``/``updated_at`` still move. Fails on
   a row-level ``ON CONFLICT … DO UPDATE … WHERE`` (which gates the row as a whole).
2. **Sweep idempotency.** A second pass over an already-cleared run reports ``rowcount = 0`` and
   ``updated_at`` never moves. Fails without the ``AND (result_text <> '' OR pending_approval IS NOT
   NULL)`` guard, and fails on any implementation that touches ``updated_at``.
3. **Terminal events after a stop.** A late ``run.completed``/``run.failed`` from the still-open
   relay must not overwrite the recorded ``cancelled``. Fails on an unconditional status UPDATE.
4. **Pause-at-zero never passes through ``cancelled``.** The internal interrupt records no status,
   so the run is ``paused`` and ``POST /resume`` works instead of answering 409.

Plus the backend-reviewer cases: a failing ``wallet.consume`` must not lose the ``completed``
status; ``POST /stop`` with a FOREIGN runId (Hermes answers 2xx for unknown runs) must update 0
rows; a throttled flush must not resurrect an answered approval; and with the billing flag OFF the
lifecycle row is still created and the tokens come from ``run.completed{usage}``.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.service import AgentProxyService
from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository
from app.audit.service import AuditService
from app.config import Settings
from app.errors import RunNotResumableError
from app.wallet.service import WalletService
from tests.conftest import seed_user

_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"


class _FakeManager:
    """Stand-in for HermesInstanceManager: a fixed endpoint, no Docker."""

    def __init__(self) -> None:
        from app.hermes_runtime.manager import InstanceEndpoint

        self.endpoint = InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)
        self.ensure_running_calls: list[uuid.UUID] = []

    async def ensure_running(self, user_id: uuid.UUID) -> Any:
        self.ensure_running_calls.append(user_id)
        return self.endpoint


def _settings(**overrides: Any) -> Settings:
    # Flush every event by default: the throttle is a unit-level concern, here we want each event's
    # SQL effect observable.
    base: dict[str, Any] = {"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _proxy(session: AsyncSession, **overrides: Any) -> AgentProxyService:
    audit = AuditService(session)
    return AgentProxyService(
        session=session,
        manager=_FakeManager(),  # type: ignore[arg-type]
        wallet=WalletService(session, audit),
        audit=audit,
        settings=_settings(**overrides),
        runs=AgentRunsRepository(session),
        snapshots=AgentRunSnapshotsRepository(session),
    )


def _sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _delta(text_piece: str) -> bytes:
    """A ``message.delta`` in the shape the PRODUCTION image actually emits (ADR-065).

    Bare-string ``delta``, no SSE ``event:`` header line — verified against the raw prod capture in
    ``tests/fixtures/hermes_prod_run_adr065.sse``. Replaces the invented ``{"text": …}`` helper that
    let every writer test below pass while prod ``resultText`` was identically empty (ADR-066).
    """
    payload = {"event": "message.delta", "run_id": "run_1", "delta": text_piece}
    return f"data: {json.dumps(payload)}\n\n".encode()


def _completed(input_tokens: int, output_tokens: int) -> bytes:
    return _sse(
        "run.completed",
        {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            "model": "m",
        },
    )


def _events_route(body: bytes, run_id: str) -> Any:
    return respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=body)
    )


def _stop_route(run_id: str) -> Any:
    return respx.post(f"{_BASE_URL}/v1/runs/{run_id}/stop").mock(
        return_value=httpx.Response(200, json={"stopped": True})
    )


async def _collect(stream: Any) -> bytes:
    out = b""
    async for chunk in stream:
        out += chunk
    return out


# --- DB helpers -------------------------------------------------------------------------------
async def _seed_run(
    m: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    balance: int = 1000,
    status: str = "running",
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Seed a subscribed user (+wallet) and the root ``agent_runs`` lifecycle row."""
    async with m() as s:
        uid = user_id or await seed_user(s, subscription="active", balance=balance)
        await AgentRunsRepository(s).create_running(run_id, uid, "sess-1", "m", status=status)
        await s.commit()
    return uid


async def _run_status(m: async_sessionmaker[AsyncSession], run_id: str) -> tuple[str, str | None]:
    async with m() as s:
        row = (
            await s.execute(
                text("SELECT status, paused_reason FROM agent_runs WHERE run_id=:r"), {"r": run_id}
            )
        ).one()
    return str(row.status), row.paused_reason


async def _snapshot(m: async_sessionmaker[AsyncSession], run_id: str) -> Any:
    async with m() as s:
        return (
            await s.execute(
                text(
                    "SELECT result_text, last_tool, pending_approval, input_tokens, "
                    "output_tokens, updated_at FROM agent_run_snapshots WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one_or_none()


# ============================================================================
# REGRESS 1 — replay window: the guard is PER-COLUMN, never row-level (MAJOR)
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_replay_window_keeps_text_but_still_writes_approval_tool_and_updated_at(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = "run_replay"
    uid = await _seed_run(db_sessionmaker, run_id)

    # --- Consumer #1: a long answer + a tool call. ------------------------------------------
    long_text = "A" * 400
    _events_route(_delta(long_text) + _sse("tool.started", {"tool": "files.write"}), run_id)
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    first = await _snapshot(db_sessionmaker, run_id)
    assert first is not None
    assert first.result_text == long_text
    assert first.last_tool == "files.write"
    assert first.pending_approval is None

    # --- Consumer #2: Hermes REPLAYS its buffer from the start → a SHORT prefix arrives first,
    # then a different tool and an approval request. -----------------------------------------
    respx.reset()
    _events_route(
        _delta("AA")
        + _sse("tool.started", {"tool": "shell"})
        + _sse("approval.request", {"tool": "shell", "preview": "rm -rf /"}),
        run_id,
    )
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    second = await _snapshot(db_sessionmaker, run_id)
    assert second is not None

    # result_text is MONOTONIC: the short replay prefix must not shorten the stored answer.
    assert second.result_text == long_text, "replay prefix overwrote the fuller text"
    # ... but the columns that are NOT monotonic by nature were written INSIDE the replay window.
    # A row-level `DO UPDATE … WHERE` would have frozen all three and the client would never see
    # waiting_approval (this assertion is the regression guard).
    assert second.pending_approval == {"tool": "shell", "preview": "rm -rf /"}
    assert second.last_tool == "shell"
    assert second.updated_at > first.updated_at, "updated_at (staleness detector) froze"

    # End to end: /state derives waiting_approval from the freshly written approval.
    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=uid, run_id=run_id)
    assert view.status == "waiting_approval"
    assert view.result_text == long_text


async def _upsert(
    m: async_sessionmaker[AsyncSession],
    run_id: str,
    uid: uuid.UUID,
    text_value: str,
    *,
    last_tool: str | None = None,
    pending_approval: dict[str, Any] | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Any:
    """One snapshot upsert through the REAL repository; returns the SnapshotUpsertResult."""
    async with m() as s:
        written = await AgentRunSnapshotsRepository(s).upsert(
            run_id=run_id,
            user_id=uid,
            result_text=text_value,
            last_tool=last_tool,
            pending_approval=pending_approval,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await s.commit()
    return written


@pytest.mark.asyncio
async def test_longer_but_non_continuing_text_is_refused(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # THE regression case for the tightened guard (09-testing.md): the incoming text is LONGER than
    # the stored one but does NOT continue it. A length-only guard (`char_length(EXCLUDED) >=
    # char_length(t)`) would happily overwrite a full answer with a fragment that is missing its
    # beginning — which is exactly what a "new events only" (no replay-from-start) Hermes would feed
    # a reconnecting relay. `left(EXCLUDED, char_length(t)) = t` makes the write freeze instead.
    # THIS TEST MUST FAIL on a length-only implementation.
    run_id = "run_diverge"
    uid = await _seed_run(db_sessionmaker, run_id)
    stored = "Полный ответ модели с самого начала."
    await _upsert(db_sessionmaker, run_id, uid, stored, last_tool="files.write")

    # Longer (by a wide margin) but starts elsewhere — a mid-stream fragment, not a continuation.
    divergent = "…продолжение без начала, зато сильно длиннее исходного текста ответа."
    assert len(divergent) > len(stored)
    written = await _upsert(db_sessionmaker, run_id, uid, divergent, last_tool="shell")

    snap = await _snapshot(db_sessionmaker, run_id)
    assert snap.result_text == stored, "a longer non-continuing text replaced the stored answer"
    # The refusal is OBSERVABLE to the caller (it drives the one-shot DEBUG latch in the relay).
    assert written.applied is True  # the row WAS written...
    assert written.stored_text_length == len(stored)  # ... but the text did not advance
    assert written.stored_text_length < len(divergent)
    # Per-column, as always: the non-monotonic columns still moved.
    assert snap.last_tool == "shell"


@pytest.mark.asyncio
async def test_honest_continuation_is_accepted(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The paired POSITIVE: the guard must not be so strict that it freezes the normal path. A relay
    # that keeps accumulating submits stored + more, so the prefix matches and the text advances.
    run_id = "run_continue"
    uid = await _seed_run(db_sessionmaker, run_id)
    head = "Начало ответа"
    await _upsert(db_sessionmaker, run_id, uid, head)

    grown = head + " и его честное продолжение"
    written = await _upsert(db_sessionmaker, run_id, uid, grown)
    assert written.applied is True
    assert written.stored_text_length == len(grown)
    assert (await _snapshot(db_sessionmaker, run_id)).result_text == grown

    # An IDENTICAL re-submit (same length, trivially its own prefix) is accepted and is a no-op.
    same = await _upsert(db_sessionmaker, run_id, uid, grown)
    assert same.stored_text_length == len(grown)
    assert (await _snapshot(db_sessionmaker, run_id)).result_text == grown

    # A strict PREFIX of the stored text (the classic replay-from-start case) is still refused.
    shorter = await _upsert(db_sessionmaker, run_id, uid, head)
    assert shorter.stored_text_length == len(grown)
    assert (await _snapshot(db_sessionmaker, run_id)).result_text == grown


@respx.mock
@pytest.mark.asyncio
async def test_relay_text_freezes_when_it_stops_continuing_the_stored_value(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # End-to-end shape of the same rule: a second consumer whose stream carries a DIFFERENT answer
    # (not a replay of the first) cannot clobber the stored one, however long it grows — while the
    # rest of the snapshot keeps tracking the live relay.
    run_id = "run_freeze"
    uid = await _seed_run(db_sessionmaker, run_id)
    _events_route(_delta("ОРИГИНАЛ-") + _delta("полный-ответ"), run_id)
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    original = (await _snapshot(db_sessionmaker, run_id)).result_text
    assert original == "ОРИГИНАЛ-полный-ответ"

    respx.reset()
    _events_route(
        _delta("ДРУГОЙ-текст-который-заведомо-длиннее-оригинального-ответа")
        + _sse("tool.started", {"tool": "shell"}),
        run_id,
    )
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    snap = await _snapshot(db_sessionmaker, run_id)
    assert snap.result_text == original, "a diverging longer stream overwrote the stored answer"
    assert snap.last_tool == "shell"  # non-monotonic columns still track the live relay


# ============================================================================
# Tenancy guard — a run_id colliding across tenants must never leak or clobber (Q-066-2)
# ============================================================================
@pytest.mark.asyncio
async def test_upsert_from_a_foreign_tenant_is_refused_and_leaves_the_owner_untouched(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Hermes run ids are generated per instance and their GLOBAL uniqueness is unconfirmed
    # (Q-064-4), so `WHERE t.user_id = EXCLUDED.user_id` fails CLOSED: a foreign writer updates
    # nothing rather than overwriting another user's snapshot.
    run_id = "run_tenant"
    owner = await _seed_run(db_sessionmaker, run_id)
    async with db_sessionmaker() as s:
        stranger = await seed_user(s, subscription="active", balance=10)

    await _upsert(
        db_sessionmaker,
        run_id,
        owner,
        "текст владельца",
        last_tool="files.write",
        input_tokens=100,
        output_tokens=50,
    )
    before = await _snapshot(db_sessionmaker, run_id)

    written = await _upsert(
        db_sessionmaker,
        run_id,
        stranger,
        "текст чужака, который длиннее и продолжать ничего не обязан",
        last_tool="shell",
        pending_approval={"tool": "shell", "preview": "p"},
        input_tokens=999999,
        output_tokens=999999,
    )
    assert written.applied is False, "a foreign tenant's upsert was applied"
    assert written.stored_text_length == 0

    after = await _snapshot(db_sessionmaker, run_id)
    # NOTHING was written — not even the columns that are otherwise updated unconditionally.
    assert after.result_text == before.result_text
    assert after.last_tool == before.last_tool
    assert after.pending_approval is None
    assert (after.input_tokens, after.output_tokens) == (100, 50)
    assert after.updated_at == before.updated_at


@pytest.mark.asyncio
async def test_get_from_a_foreign_tenant_returns_none(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = "run_tenant_read"
    owner = await _seed_run(db_sessionmaker, run_id)
    async with db_sessionmaker() as s:
        stranger = await seed_user(s, subscription="active", balance=10)
    await _upsert(db_sessionmaker, run_id, owner, "приватный текст владельца")

    async with db_sessionmaker() as s:
        repo = AgentRunSnapshotsRepository(s)
        assert await repo.get(run_id, stranger) is None
        owned = await repo.get(run_id, owner)
    assert owned is not None
    assert owned.result_text == "приватный текст владельца"


@pytest.mark.asyncio
async def test_get_state_degrades_to_defaults_on_a_cross_tenant_snapshot(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The exact leak this guards: two users hold a run row under the SAME Hermes run_id (a collision
    # is possible because ids are per-instance). The lifecycle row makes the read pass RBAC, so
    # without the owner-scoped snapshot read the second user would be served the FIRST user's text.
    # It must degrade to the documented empty-snapshot defaults instead.
    run_id = "run_collision"
    victim = await _seed_run(db_sessionmaker, run_id)
    await _upsert(
        db_sessionmaker,
        run_id,
        victim,
        "СЕКРЕТНЫЙ текст жертвы",
        last_tool="files.read",
        pending_approval={"tool": "shell", "preview": "секретный предпросмотр"},
        input_tokens=777,
        output_tokens=333,
    )

    # A second tenant whose own agent_runs row carries the very same run_id. The PK on agent_runs is
    # the run_id, so the collision is expressed by re-pointing the row's owner — the shape /state
    # sees is "my lifecycle row + someone else's snapshot".
    async with db_sessionmaker() as s:
        other = await seed_user(s, subscription="active", balance=10)
        await s.execute(
            text("UPDATE agent_runs SET user_id = :u WHERE run_id = :r"),
            {"u": str(other), "r": run_id},
        )
        await s.commit()

    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=other, run_id=run_id)
    # Documented defaults — and, decisively, NOT the victim's content.
    assert view.result_text == ""
    assert view.last_tool is None
    assert view.pending_approval is None
    assert (view.input_tokens, view.output_tokens) == (0, 0)
    assert view.status == "running"  # derived without the foreign pending_approval
    assert "СЕКРЕТНЫЙ" not in view.result_text

    # The victim's row itself is intact (the read is scoped, not destructive).
    assert (await _snapshot(db_sessionmaker, run_id)).result_text == "СЕКРЕТНЫЙ текст жертвы"


@respx.mock
@pytest.mark.asyncio
async def test_relay_of_a_colliding_run_does_not_clobber_the_other_tenants_snapshot(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The same collision from the WRITE side: the second tenant's relay streams events for a run_id
    # whose snapshot belongs to someone else. Every flush is refused; the relay must keep working.
    run_id = "run_collision_w"
    victim = await _seed_run(db_sessionmaker, run_id)
    await _upsert(db_sessionmaker, run_id, victim, "текст жертвы", input_tokens=42)
    before = await _snapshot(db_sessionmaker, run_id)

    async with db_sessionmaker() as s:
        other = await seed_user(s, subscription="active", balance=1000)
        await s.execute(
            text("UPDATE agent_runs SET user_id = :u WHERE run_id = :r"),
            {"u": str(other), "r": run_id},
        )
        await s.commit()

    _events_route(_delta("чужой поток") + _completed(2000, 1000), run_id)
    async with db_sessionmaker() as s:
        relayed = await _collect(_proxy(s).stream_events(user_id=other, run_id=run_id))
    assert b"run.completed" in relayed  # the relay is not broken by the refusals

    after = await _snapshot(db_sessionmaker, run_id)
    assert after.result_text == before.result_text
    assert after.input_tokens == before.input_tokens
    assert after.updated_at == before.updated_at


@respx.mock
@pytest.mark.asyncio
async def test_replay_window_does_not_lower_token_counters(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # GREATEST on both counters: a replay that starts from a smaller cumulative must not regress.
    run_id = "run_tokens"
    uid = await _seed_run(db_sessionmaker, run_id)
    _events_route(_delta("x") + _completed(9000, 4000), run_id)
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    assert (await _snapshot(db_sessionmaker, run_id)).input_tokens == 9000

    respx.reset()
    _events_route(_delta("x") + _completed(10, 5), run_id)
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    snap = await _snapshot(db_sessionmaker, run_id)
    assert (snap.input_tokens, snap.output_tokens) == (9000, 4000)


# ============================================================================
# REGRESS 2 — retention sweep is idempotent and never moves updated_at (MAJOR)
# ============================================================================
@pytest.mark.asyncio
async def test_sweep_is_idempotent_and_never_moves_updated_at(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_run(db_sessionmaker, "run_done", status="completed")
    async with db_sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO agent_run_snapshots (run_id, user_id, result_text, pending_approval, "
                "input_tokens, output_tokens, updated_at) VALUES "
                "('run_done', :u, 'секретный ответ', CAST('{\"tool\":\"shell\"}' AS JSONB), "
                "500, 100, now() - make_interval(days => 30))"
            ),
            {"u": str(uid)},
        )
        await s.commit()
    before = await _snapshot(db_sessionmaker, "run_done")

    # First pass clears the CONTENT of the expired terminal run.
    async with db_sessionmaker() as s:
        first_rowcount = await AgentRunSnapshotsRepository(s).sweep_expired(14)
        await s.commit()
    assert first_rowcount == 1
    after_first = await _snapshot(db_sessionmaker, "run_done")
    assert after_first.result_text == ""
    assert after_first.pending_approval is None
    # Non-content columns survive: /state keeps answering "the run happened, here is its outcome".
    assert (after_first.input_tokens, after_first.output_tokens) == (500, 100)
    # ADR-066 §7 invariant 2: clearing content is NOT a state write.
    assert after_first.updated_at == before.updated_at

    # Second pass must be a NO-OP (guard `AND (result_text <> '' OR pending_approval IS NOT NULL)`).
    # Without it every reaper tick would rewrite the whole terminal history forever (MVCC/WAL churn
    # growing with the history).
    async with db_sessionmaker() as s:
        second_rowcount = await AgentRunSnapshotsRepository(s).sweep_expired(14)
        await s.commit()
    assert second_rowcount == 0, "sweep is not idempotent (missing guard)"
    after_second = await _snapshot(db_sessionmaker, "run_done")
    assert after_second.updated_at == before.updated_at


@pytest.mark.asyncio
async def test_sweep_never_touches_active_runs_at_any_age(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Active runs (running/resumed) are excluded from the sweep at ANY age (ADR-066 §7).
    uid = await _seed_run(db_sessionmaker, "run_active", status="running")
    async with db_sessionmaker() as s:
        await AgentRunsRepository(s).create_running("run_res", uid, "sess-2", "m", status="running")
        await s.execute(text("UPDATE agent_runs SET status='resumed' WHERE run_id='run_res'"))
        for rid in ("run_active", "run_res"):
            await s.execute(
                text(
                    "INSERT INTO agent_run_snapshots (run_id, user_id, result_text, updated_at) "
                    "VALUES (:r, :u, 'живой текст', now() - make_interval(days => 400))"
                ),
                {"r": rid, "u": str(uid)},
            )
        await s.commit()

    async with db_sessionmaker() as s:
        rowcount = await AgentRunSnapshotsRepository(s).sweep_expired(14)
        await s.commit()
    assert rowcount == 0
    for rid in ("run_active", "run_res"):
        assert (await _snapshot(db_sessionmaker, rid)).result_text == "живой текст"


@pytest.mark.asyncio
async def test_reaper_tick_runs_the_sweep(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-066 §7: the sweep is wired into the EXISTING reaper tick, not a new loop. Assert that
    # wiring (the SQL itself is covered above); the instance-hibernation half is stubbed out.
    from app.hermes_runtime import reaper as reaper_mod
    from app.hermes_runtime.manager import HermesInstanceManager

    uid = await _seed_run(db_sessionmaker, "run_reaped", status="failed")
    async with db_sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO agent_run_snapshots (run_id, user_id, result_text, updated_at) "
                "VALUES ('run_reaped', :u, 'старое', now() - make_interval(days => 60))"
            ),
            {"u": str(uid)},
        )
        await s.commit()

    # noqa ASYNC109: this is a signature-compatible stub of HermesInstanceManager.stop_idle, whose
    # `timeout` is an idle THRESHOLD in seconds (ADR-046 §5), not an asyncio cancellation budget.
    async def _noop_stop_idle(self: Any, timeout: int) -> None:  # noqa: ASYNC109
        return None

    monkeypatch.setattr(HermesInstanceManager, "stop_idle", _noop_stop_idle)

    async def _session_scope() -> Any:
        async with db_sessionmaker() as s:
            yield s
            await s.commit()

    monkeypatch.setattr(reaper_mod, "session_scope", _session_scope)
    await reaper_mod._run_one_tick(_settings())

    assert (await _snapshot(db_sessionmaker, "run_reaped")).result_text == ""


# ============================================================================
# REGRESS 3 — terminal events arriving after a stop must not overwrite `cancelled` (MAJOR)
# ============================================================================
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("late_event", ["run.completed", "run.failed"])
async def test_late_terminal_event_does_not_overwrite_cancelled(
    db_sessionmaker: async_sessionmaker[AsyncSession], late_event: str
) -> None:
    # `stop → stopped` is eventually consistent: Hermes keeps flushing buffered events into the
    # still-open relay. An unconditional status UPDATE would show `completed` for a run the user
    # stopped themselves — and the history would lose the cancellation.
    run_id = "run_stopped"
    uid = await _seed_run(db_sessionmaker, run_id)
    _stop_route(run_id)
    async with db_sessionmaker() as s:
        await _proxy(s).stop(user_id=uid, run_id=run_id)
        await s.commit()
    assert (await _run_status(db_sessionmaker, run_id))[0] == "cancelled"

    body = _completed(2000, 1000) if late_event == "run.completed" else _sse("run.failed", {"e": 1})
    _events_route(_delta("хвост") + body, run_id)
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))

    # The FIRST terminal status wins (WHERE status IN ('running','resumed')).
    assert (await _run_status(db_sessionmaker, run_id))[0] == "cancelled"
    async with db_sessionmaker() as s:
        assert (await _proxy(s).get_state(user_id=uid, run_id=run_id)).status == "stopped"
    # The snapshot content still updates — only the STATUS is frozen (they are separate concerns).
    assert (await _snapshot(db_sessionmaker, run_id)).result_text == "хвост"


@respx.mock
@pytest.mark.asyncio
async def test_stop_does_not_overwrite_an_already_completed_run(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The mirror case: stopping a finished run must not rewrite its terminal status either.
    run_id = "run_finished"
    uid = await _seed_run(db_sessionmaker, run_id, status="completed")
    _stop_route(run_id)
    async with db_sessionmaker() as s:
        await _proxy(s).stop(user_id=uid, run_id=run_id)
        await s.commit()
    assert (await _run_status(db_sessionmaker, run_id))[0] == "completed"


# ============================================================================
# REGRESS 4 — pause-at-zero never passes through `cancelled` (MAJOR)
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_pause_at_zero_is_paused_and_stays_resumable(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = "run_pause"
    uid = await _seed_run(db_sessionmaker, run_id, balance=1)
    _stop_route(run_id)  # the INTERNAL interrupt uses the same transport as the client /stop
    _events_route(
        _sse(
            "usage.delta",
            {
                "step_index": 0,
                "input_tokens": 5000,
                "output_tokens": 5000,
                "cumulative_input_tokens": 5000,
                "cumulative_output_tokens": 5000,
                "model": "m",
            },
        ),
        run_id,
    )
    async with db_sessionmaker() as s:
        relayed = await _collect(
            _proxy(s, **{"AGENT_INCREMENTAL_BILLING_ENABLED": True}).stream_events(
                user_id=uid, run_id=run_id
            )
        )
    assert b"run.paused" in relayed

    # `paused` + reason — NOT `cancelled`: the client must be offered a top-up, not a "stopped" run.
    status, reason = await _run_status(db_sessionmaker, run_id)
    assert status == "paused"
    assert reason == "credits_exhausted"
    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=uid, run_id=run_id)
    assert view.status == "paused"
    assert view.block_reason == "credits_exhausted"

    # The decisive consequence: POST /resume must NOT answer 409 run_not_resumable (the guard
    # requires paused/resumed — a transient `cancelled` would have broken exactly this).
    async with db_sessionmaker() as s:
        await WalletService(s, AuditService(s)).grant(
            user_id=uid, amount=500, idempotency_key=f"topup:{uid}", meta={}, reason="topup"
        )
        await s.commit()
    respx.reset()
    respx.get(f"{_BASE_URL}/api/sessions/sess-1/messages").mock(
        return_value=httpx.Response(200, json={"data": [{"role": "user", "content": "hi"}]})
    )
    respx.post(f"{_BASE_URL}/v1/runs").mock(
        return_value=httpx.Response(202, json={"run_id": "run_child", "status": "running"})
    )
    async with db_sessionmaker() as s:
        result = await _proxy(s, **{"AGENT_INCREMENTAL_BILLING_ENABLED": True}).resume(
            user_id=uid, run_id=run_id, message=None
        )
    assert result.blocked is False
    assert result.run_id == "run_child"
    assert result.continued_from == run_id


@respx.mock
@pytest.mark.asyncio
async def test_cancelled_run_is_not_resumable(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Counter-proof for the case above: had pause-at-zero recorded `cancelled`, resume WOULD 409.
    # This test pins that consequence so the previous test's value cannot silently evaporate.
    run_id = "run_cancelled"
    uid = await _seed_run(db_sessionmaker, run_id)
    _stop_route(run_id)
    async with db_sessionmaker() as s:
        await _proxy(s).stop(user_id=uid, run_id=run_id)
        await s.commit()
    async with db_sessionmaker() as s:
        with pytest.raises(RunNotResumableError):
            await _proxy(s).resume(user_id=uid, run_id=run_id, message=None)


# ============================================================================
# backend-reviewer: `completed` survives a failing wallet.consume
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_completed_status_recorded_even_when_wallet_consume_raises(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The status is written by the run.completed HANDLER, before billing. Inside _bill_completed it
    # would be skipped by the generic rollback branch — leaving the run 'running' forever and making
    # /state lie indefinitely.
    run_id = "run_billing_boom"
    uid = await _seed_run(db_sessionmaker, run_id)

    async def _boom(self: Any, **kwargs: Any) -> Any:
        raise RuntimeError("wallet exploded")

    monkeypatch.setattr(WalletService, "consume", _boom)
    _events_route(_delta("готово") + _completed(2000, 1000), run_id)
    async with db_sessionmaker() as s:
        relayed = await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    assert b"run.completed" in relayed  # the relay was not broken

    assert (await _run_status(db_sessionmaker, run_id))[0] == "completed"
    async with db_sessionmaker() as s:
        assert (await _proxy(s).get_state(user_id=uid, run_id=run_id)).status == "completed"
    # No debit was recorded (consume never succeeded) — the status write is independent of money.
    async with db_sessionmaker() as s:
        debits = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
    assert debits == 0


# ============================================================================
# backend-reviewer: POST /stop with a FOREIGN runId updates 0 rows (owner-scoped UPDATE)
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_stop_with_foreign_run_id_updates_no_rows(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Hermes answers 2xx for an unknown/foreign run (idempotent-stop semantics), and run_id comes
    # straight from the request path — so without `AND user_id = :uid` in the UPDATE itself, user A
    # could cancel user B's run. RBAC must be a property of the STATEMENT, not only of a preceding
    # check. A foreign id simply updates 0 rows; no 403 is surfaced.
    victim_run = "run_victim"
    victim = await _seed_run(db_sessionmaker, victim_run)
    async with db_sessionmaker() as s:
        attacker = await seed_user(s, subscription="active", balance=100)
    assert attacker != victim

    _stop_route(victim_run)
    async with db_sessionmaker() as s:
        out = await _proxy(s).stop(user_id=attacker, run_id=victim_run)
        await s.commit()
    assert out == {"stopped": True}  # the passthrough result is still returned (no 403)

    # The victim's run is untouched, and the victim still reads `running` from /state.
    assert (await _run_status(db_sessionmaker, victim_run))[0] == "running"
    async with db_sessionmaker() as s:
        assert (await _proxy(s).get_state(user_id=victim, run_id=victim_run)).status == "running"


@respx.mock
@pytest.mark.asyncio
async def test_mark_stopped_rowcount_is_zero_for_a_foreign_owner(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Direct statement-level assertion of the same invariant (rowcount, not just the end state).
    uid = await _seed_run(db_sessionmaker, "run_scoped")
    async with db_sessionmaker() as s:
        assert await AgentRunsRepository(s).mark_stopped("run_scoped", uuid.uuid4()) == 0
        assert await AgentRunsRepository(s).mark_stopped("run_scoped", uid) == 1
        await s.commit()


# ============================================================================
# backend-reviewer: a throttled flush must not resurrect an ANSWERED approval
# ============================================================================
@pytest.mark.asyncio
async def test_throttled_upsert_does_not_resurrect_a_cleared_approval(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The client may answer POST …/approval out of band (another request, another session). The
    # relay still holds its cached {tool, preview}; a throttled message.delta flush carries
    # assert_pending_approval=False so the stored NULL is preserved. Asserted at the SQL level
    # because this is a CASE inside the ON CONFLICT clause.
    run_id = "run_approval"
    uid = await _seed_run(db_sessionmaker, run_id)
    async with db_sessionmaker() as s:
        repo = AgentRunSnapshotsRepository(s)
        # 1. approval.request (immediate flush, asserts the approval).
        await repo.upsert(
            run_id=run_id,
            user_id=uid,
            result_text="abc",
            last_tool=None,
            pending_approval={"tool": "shell", "preview": "p"},
            input_tokens=0,
            output_tokens=0,
            assert_pending_approval=True,
        )
        await s.commit()
    assert (await _snapshot(db_sessionmaker, run_id)).pending_approval == {
        "tool": "shell",
        "preview": "p",
    }

    # 2. The user answers → POST …/approval clears it (owner-scoped, moves updated_at).
    async with db_sessionmaker() as s:
        assert await AgentRunSnapshotsRepository(s).clear_pending_approval(run_id, uid) == 1
        await s.commit()
    assert (await _snapshot(db_sessionmaker, run_id)).pending_approval is None

    # 3. A THROTTLED text flush from the still-running relay, whose cached belief is stale.
    async with db_sessionmaker() as s:
        await AgentRunSnapshotsRepository(s).upsert(
            run_id=run_id,
            user_id=uid,
            result_text="abcdef",
            last_tool=None,
            pending_approval={"tool": "shell", "preview": "p"},  # stale relay belief
            input_tokens=0,
            output_tokens=0,
            assert_pending_approval=False,
        )
        await s.commit()
    snap = await _snapshot(db_sessionmaker, run_id)
    assert snap.pending_approval is None, "a throttled flush resurrected an answered approval"
    assert snap.result_text == "abcdef"  # the text it WAS responsible for did land
    async with db_sessionmaker() as s:
        assert (await _proxy(s).get_state(user_id=uid, run_id=run_id)).status == "running"


@pytest.mark.asyncio
async def test_clear_pending_approval_is_owner_scoped(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # ADR-066 §6: the clear runs on the /approval path where run_id comes from the request path, so
    # it is owner-scoped — a foreign user_id must update 0 rows and leave the victim's snapshot
    # (its pending approval AND its updated_at) untouched.
    run_id = "run_owned_approval"
    uid = await _seed_run(db_sessionmaker, run_id)
    async with db_sessionmaker() as s:
        await AgentRunSnapshotsRepository(s).upsert(
            run_id=run_id,
            user_id=uid,
            result_text="t",
            last_tool=None,
            pending_approval={"tool": "shell", "preview": "p"},
            input_tokens=0,
            output_tokens=0,
            assert_pending_approval=True,
        )
        await s.commit()
    before = await _snapshot(db_sessionmaker, run_id)

    async with db_sessionmaker() as s:
        assert (
            await AgentRunSnapshotsRepository(s).clear_pending_approval(run_id, uuid.uuid4()) == 0
        )
        await s.commit()
    after = await _snapshot(db_sessionmaker, run_id)
    assert after.pending_approval == {"tool": "shell", "preview": "p"}
    assert after.updated_at == before.updated_at

    # A second clear by the real owner is idempotent: the row is already NULL → 0 rows.
    async with db_sessionmaker() as s:
        repo = AgentRunSnapshotsRepository(s)
        assert await repo.clear_pending_approval(run_id, uid) == 1
        assert await repo.clear_pending_approval(run_id, uid) == 0
        await s.commit()


@pytest.mark.asyncio
async def test_clear_pending_approval_on_a_snapshotless_run_is_a_noop(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # It never INSERTs: a run whose relay has not flushed anything has nothing to clear.
    uid = await _seed_run(db_sessionmaker, "run_nosnap")
    async with db_sessionmaker() as s:
        assert await AgentRunSnapshotsRepository(s).clear_pending_approval("run_nosnap", uid) == 0
        await s.commit()
    assert await _snapshot(db_sessionmaker, "run_nosnap") is None


# ============================================================================
# backend-reviewer: billing flag OFF — the lifecycle row exists, tokens come from run.completed
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_flag_off_creates_lifecycle_row_and_fills_tokens_from_completed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # ADR-066 §3: agent_runs is now an UNCONDITIONAL lifecycle record. On the default configuration
    # (agent_incremental_billing_enabled=false) there used to be no agent_runs row at all — so
    # neither /state nor any run history existed. There are no usage.delta events in this mode, so
    # the snapshot tokens must come from run.completed{usage}.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=1000)

    respx.post(f"{_BASE_URL}/v1/runs").mock(
        return_value=httpx.Response(202, json={"run_id": "run_off", "status": "running"})
    )
    async with db_sessionmaker() as s:
        launched = await _proxy(s, **{"AGENT_INCREMENTAL_BILLING_ENABLED": False}).run(
            user_id=uid, message="hi", session_id="sess-off", model=None
        )
        await s.commit()
    assert launched.run_id == "run_off"
    # The lifecycle row exists with the flag OFF.
    assert (await _run_status(db_sessionmaker, "run_off"))[0] == "running"

    respx.reset()
    _events_route(
        _delta("ответ ")
        + _sse("tool.started", {"tool": "files.read"})
        + _delta("готов")
        + _completed(3000, 2000),
        "run_off",
    )
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, **{"AGENT_INCREMENTAL_BILLING_ENABLED": False}).stream_events(
                user_id=uid, run_id="run_off"
            )
        )

    assert (await _run_status(db_sessionmaker, "run_off"))[0] == "completed"
    snap = await _snapshot(db_sessionmaker, "run_off")
    assert (snap.input_tokens, snap.output_tokens) == (3000, 2000)
    assert snap.result_text == "ответ готов"
    assert snap.last_tool == "files.read"

    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=uid, run_id="run_off")
    assert view.status == "completed"
    assert view.session_id == "sess-off"
    assert (view.input_tokens, view.output_tokens) == (3000, 2000)


# ============================================================================
# Truncation at the DB boundary (head-preserving) + the FK/CASCADE contract
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_result_text_is_capped_head_preserving_end_to_end(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id = "run_big"
    uid = await _seed_run(db_sessionmaker, run_id)
    head = "H" * 32
    _events_route(_delta(head) + _delta("T" * 500) + _completed(10, 10), run_id)
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, **{"AGENT_STATE_RESULT_TEXT_MAX_CHARS": 32}).stream_events(
                user_id=uid, run_id=run_id
            )
        )
    stored = (await _snapshot(db_sessionmaker, run_id)).result_text
    assert stored == head  # the HEAD survived; the tail was dropped, never the other way round
    assert "T" not in stored


@pytest.mark.asyncio
async def test_snapshot_cascades_from_agent_runs_and_users(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The snapshot never outlives its lifecycle row, and deleting a user wipes the stored model text
    # (ADR-066 §2 — the mitigation for having user-facing content at rest).
    uid = await _seed_run(db_sessionmaker, "run_cascade_a")
    await _seed_run(db_sessionmaker, "run_cascade_b", user_id=uid)
    async with db_sessionmaker() as s:
        for rid in ("run_cascade_a", "run_cascade_b"):
            await AgentRunSnapshotsRepository(s).upsert(
                run_id=rid,
                user_id=uid,
                result_text="user content",
                last_tool=None,
                pending_approval=None,
                input_tokens=0,
                output_tokens=0,
            )
        await s.commit()

    async with db_sessionmaker() as s:
        await s.execute(text("DELETE FROM agent_runs WHERE run_id='run_cascade_a'"))
        await s.commit()
    assert await _snapshot(db_sessionmaker, "run_cascade_a") is None
    assert await _snapshot(db_sessionmaker, "run_cascade_b") is not None

    async with db_sessionmaker() as s:
        await s.execute(text("DELETE FROM users WHERE id=:u"), {"u": str(uid)})
        await s.commit()
    assert await _snapshot(db_sessionmaker, "run_cascade_b") is None


@respx.mock
@pytest.mark.asyncio
async def test_snapshot_write_for_a_run_without_lifecycle_row_never_breaks_the_relay(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A run started before agent_runs became unconditional has no parent row: every upsert hits the
    # FK. The relay must survive it (rolled back + logged, no user content) and still bill.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=1000)
    _events_route(_delta("orphan") + _completed(2000, 1000), "run_orphan")
    async with db_sessionmaker() as s:
        relayed = await _collect(_proxy(s).stream_events(user_id=uid, run_id="run_orphan"))
    assert b"run.completed" in relayed
    assert await _snapshot(db_sessionmaker, "run_orphan") is None
    async with db_sessionmaker() as s:
        debits = await s.scalar(
            text(
                "SELECT count(*) FROM ledger_transactions "
                "WHERE user_id=:u AND type='debit' AND idempotency_key='run_orphan'"
            ),
            {"u": str(uid)},
        )
    assert debits == 1


@pytest.mark.asyncio
async def test_state_updated_at_is_timezone_aware_utc(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # updatedAt is the client staleness detector; a naive timestamp would be unusable on iOS.
    uid = await _seed_run(db_sessionmaker, "run_tz")
    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=uid, run_id="run_tz")
    assert view.updated_at.tzinfo is not None
    assert view.updated_at.utcoffset() == datetime.timedelta(0)
