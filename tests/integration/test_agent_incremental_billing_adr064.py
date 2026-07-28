"""Integration: incremental agent-run billing + pause-at-zero (ADR-064 §1-§4, §6). Real PostgreSQL.

Flag ON (``AGENT_INCREMENTAL_BILLING_ENABLED``) unless a test says otherwise. The Hermes instance is
mocked at the HTTP boundary (respx) — no Docker; ``HermesInstanceManager`` is a fake endpoint. But
``WalletService`` / ``AuditService`` / ``AgentRunsRepository`` run against the REAL container DB, so
the ledger / wallet / agent_runs state and the money invariant ``balance == Σ(credit) − Σ(debit)``
are observable and the CTE self-clamp (``_consume_incremental_clamp``, ``FOR UPDATE``) executes on
real Postgres.

Covers ADR-064:
- §1 telescoping: Σ per-step charge == usage_to_credits(final) EXACTLY (no per-step-ceil inflation);
- §2 per-step idempotency (duplicate usage.delta same step → one debit) + finalization remainder
  (bare run_id key, separate keyspace from run_id:step);
- §4 no-debt self-clamp routing (meta.incremental → clamp, never raises) + regress (non-incremental
  → savepoint raise); ledger invariant under a balance<amount race (LEAST clamp);
- §3 pause-at-zero: charge==0 / partial → stop + synthetic run.paused, agent_runs.status=paused,
  wallets.debt==0 (netBalance==balance), no run.completed;
- §6 seed-from-ledger reconnect (charged restored, run_id LIKE-metachar ESCAPE, no re-bill);
- flag OFF: no per-step billing, single full run.completed debit, no agent_runs row, resume 404.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.billing import usage_to_credits
from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.service import AgentProxyService
from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository
from app.audit.service import AuditService
from app.config import Settings
from app.errors import InsufficientCreditsError, NotFoundError
from app.hermes_runtime.manager import InstanceEndpoint
from app.wallet.service import WalletService
from tests.conftest import seed_user

# --- Constants -------------------------------------------------------------------------------
_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"
_K_IN = 1.0  # CREDITS_PER_1K_INPUT default
_K_OUT = 5.0  # CREDITS_PER_1K_OUTPUT default


# --- Fakes (instance boundary only; wallet + audit + runs are REAL) --------------------------
class _FakeManager:
    def __init__(self) -> None:
        self.endpoint = InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)

    async def ensure_running(self, user_id: uuid.UUID) -> InstanceEndpoint:
        return self.endpoint


def _settings(*, incremental: bool) -> Settings:
    return Settings(**{"AGENT_INCREMENTAL_BILLING_ENABLED": incremental})  # type: ignore[arg-type]


def _proxy(session: AsyncSession, *, incremental: bool = True) -> AgentProxyService:
    audit = AuditService(session)
    wallet = WalletService(session, audit)
    return AgentProxyService(
        session=session,
        manager=_FakeManager(),  # type: ignore[arg-type]
        wallet=wallet,
        audit=audit,
        settings=_settings(incremental=incremental),
        runs=AgentRunsRepository(session),
        # ADR-066: relay-side snapshot writer (real repo — production wiring).
        snapshots=AgentRunSnapshotsRepository(session),
    )


def _sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _usage_delta(
    step: int, cum_in: int, cum_out: int, *, in_delta: int = 0, out_delta: int = 0
) -> bytes:
    return _sse(
        "usage.delta",
        {
            "run_id": "run",
            "step_index": step,
            "input_tokens": in_delta,
            "output_tokens": out_delta,
            "cumulative_input_tokens": cum_in,
            "cumulative_output_tokens": cum_out,
            "cumulative_total_tokens": cum_in + cum_out,
            "model": "m",
        },
    )


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


def _events_route(body: bytes, run_id: str, status: int = 200) -> object:
    return respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(status, content=body)
    )


def _stop_route(run_id: str) -> object:
    return respx.post(f"{_BASE_URL}/v1/runs/{run_id}/stop").mock(
        return_value=httpx.Response(200, json={"stopped": True})
    )


async def _collect(stream: object) -> bytes:
    out = b""
    async for chunk in stream:  # type: ignore[attr-defined]
        out += chunk
    return out


# --- DB assertion helpers (over a FRESH session) ---------------------------------------------
async def _balance(m: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with m() as s:
        row = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    return int(row) if row is not None else 0


async def _debt(m: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with m() as s:
        row = await s.scalar(text("SELECT debt FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    return int(row) if row is not None else 0


async def _sum_signed(m: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with m() as s:
        row = await s.scalar(
            text(
                "SELECT COALESCE(SUM(CASE WHEN type='credit' THEN amount ELSE -amount END),0) "
                "FROM ledger_transactions WHERE user_id=:u"
            ),
            {"u": str(uid)},
        )
    return int(row or 0)


async def _debits(m: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[tuple[str, int]]:
    """Ordered (idempotency_key, amount) of every debit ledger row for the user."""
    async with m() as s:
        rows = await s.execute(
            text(
                "SELECT idempotency_key, amount FROM ledger_transactions "
                "WHERE user_id=:u AND type='debit' ORDER BY created_at"
            ),
            {"u": str(uid)},
        )
    return [(r.idempotency_key, int(r.amount)) for r in rows]


async def _agent_run(
    m: async_sessionmaker[AsyncSession], run_id: str
) -> tuple[str, str | None, int, int] | None:
    async with m() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, paused_reason, cumulative_credits_spent, last_billed_step "
                    "FROM agent_runs WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one_or_none()
    if row is None:
        return None
    return (
        str(row.status),
        row.paused_reason,
        int(row.cumulative_credits_spent),
        int(row.last_billed_step),
    )


async def _seed_credit(m: async_sessionmaker[AsyncSession], opening: int) -> uuid.UUID:
    """Seed a user whose opening balance comes from a REAL grant (credit ledger row) so the
    invariant balance == Σ(credit) − Σ(debit) is meaningfully testable."""
    uid = uuid.uuid4()
    async with m() as s:
        await seed_user(s, user_id=uid)
        if opening > 0:
            await WalletService(s, AuditService(s)).grant(
                user_id=uid, amount=opening, idempotency_key=f"seed:{uid}", meta={}, reason="seed"
            )
        await s.commit()
    return uid


async def _seed_agent_run(
    m: async_sessionmaker[AsyncSession], run_id: str, uid: uuid.UUID, session_id: str = "sess"
) -> None:
    """Create the root agent_runs row the same way run() does under the flag (ADR-064 §5)."""
    async with m() as s:
        await AgentRunsRepository(s).create_running(run_id, uid, session_id, "m", status="running")
        await s.commit()


# ============================================================================
# §1 Telescoping: Σ per-step charge == usage_to_credits(final) EXACTLY (no inflation).
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_telescoping_sum_equals_posthoc_and_no_perstep_ceil_inflation(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 1000)
    run_id = "run"
    await _seed_agent_run(db_sessionmaker, run_id, uid)

    # Cumulative usage grows; each per-step delta alone would round UP (floor-to-1) and inflate.
    # step0 cum(400,0) owed=ceil(0.4)->1; step1 cum(800,0) owed=ceil(0.8)->1 (want 0, no debit);
    # step2 cum(1200,0) owed=ceil(1.2)->2 (want 1). final usage (1200,0) owed=2 → remainder 0.
    body = (
        _usage_delta(0, 400, 0, in_delta=400)
        + _usage_delta(1, 800, 0, in_delta=400)
        + _usage_delta(2, 1200, 0, in_delta=400)
        + _completed(1200, 0)
    )
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))

    owed_final = usage_to_credits(
        input_tokens=1200, output_tokens=0, credits_per_1k_input=_K_IN, credits_per_1k_output=_K_OUT
    )
    assert owed_final == 2
    # Per-step-ceil of independent deltas would be 1+1+1 = 3 (inflation) — the telescoping scheme
    # must NOT do that.
    naive = sum(
        usage_to_credits(
            input_tokens=d,
            output_tokens=0,
            credits_per_1k_input=_K_IN,
            credits_per_1k_output=_K_OUT,
        )
        for d in (400, 400, 400)
    )
    assert naive == 3 and naive > owed_final

    debits = await _debits(db_sessionmaker, uid)
    total = sum(a for _, a in debits)
    assert (
        total == owed_final == 2
    ), f"Σ per-step charge must equal usage_to_credits(final); {debits}"
    # Only per-step keys (run_id:step) were used; remainder was 0 so no bare-run_id row.
    assert all(":" in k for k, _ in debits)
    assert all(k.startswith(f"{run_id}:") for k, _ in debits)
    # Money invariant holds and mirror == ledger.
    assert await _balance(db_sessionmaker, uid) == 1000 - 2
    assert await _sum_signed(db_sessionmaker, uid) == await _balance(db_sessionmaker, uid)
    ar = await _agent_run(db_sessionmaker, run_id)
    assert ar is not None
    assert ar[0] == "completed"
    assert ar[2] == 2  # cumulative_credits_spent mirror == Σ ledger.amount


# ============================================================================
# §2 Per-step idempotency: a replayed usage.delta of the same step → ONE debit.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_per_step_idempotent_duplicate_delta_one_debit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 1000)
    run_id = "run"
    await _seed_agent_run(db_sessionmaker, run_id, uid)
    # Same step_index=0 delivered TWICE (reconnect / duplicate) → key run:0 hits ON CONFLICT once.
    body = _usage_delta(0, 1000, 0) + _usage_delta(0, 1000, 0)
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))

    debits = await _debits(db_sessionmaker, uid)
    assert debits == [(f"{run_id}:0", 1)], "duplicate step must not double-charge"
    assert await _balance(db_sessionmaker, uid) == 999


# ============================================================================
# §2 Finalization remainder on run.completed: bare run_id key, separate keyspace.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_finalization_remainder_bare_run_id_key(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 1000)
    run_id = "run_fin"
    await _seed_agent_run(db_sessionmaker, run_id, uid)
    # step0 charges 1 (cum 1000,0). final usage (3000,0) owed=3 → remainder = 3 - 1 = 2.
    body = _usage_delta(0, 1000, 0) + _completed(3000, 0)
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))

    debits = dict(await _debits(db_sessionmaker, uid))
    assert debits.get(f"{run_id}:0") == 1  # per-step keyspace
    assert debits.get(run_id) == 2  # bare run_id finalization keyspace (does NOT collide)
    assert sum(debits.values()) == usage_to_credits(
        input_tokens=3000, output_tokens=0, credits_per_1k_input=_K_IN, credits_per_1k_output=_K_OUT
    )
    assert await _balance(db_sessionmaker, uid) == 997


# ============================================================================
# §4 No-debt self-clamp routing (meta.incremental) + ledger invariant under a balance<amount race.
# ============================================================================
@pytest.mark.asyncio
async def test_incremental_consume_clamps_ledger_to_actual_balance_no_debt(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Direct WalletService.consume with meta.incremental: the CTE (_consume_incremental_clamp,
    # FOR UPDATE) must record ledger.amount == the ACTUAL balance delta (LEAST(amount,balance)) even
    # when amount > balance — never inflating Σdebit, never raising, never accruing debt.
    uid = await _seed_credit(db_sessionmaker, 3)
    meta = {
        "source": "agent_run",
        "incremental": True,
        "runId": "run",
        "stepIndex": 0,
        "model": "m",
    }
    async with db_sessionmaker() as s:
        res = await WalletService(s, AuditService(s)).consume(
            user_id=uid, amount=10, idempotency_key="run:0", meta=meta
        )
        await s.commit()
    assert res.charged_amount == 3  # LEAST(10, 3)
    assert res.new_balance == 0
    debits = await _debits(db_sessionmaker, uid)
    assert debits == [
        ("run:0", 3)
    ], "ledger row must record the CLAMPED amount, not the requested 10"
    assert await _debt(db_sessionmaker, uid) == 0  # no debt on the incremental path
    # charged_for_run == Σ debit == actually spent (exact resume seed).
    async with db_sessionmaker() as s:
        charged = await WalletService(s, AuditService(s)).charged_for_run(uid, "run")
    assert charged == 3
    # Money invariant: balance == Σ(credit 3) − Σ(debit 3) == 0.
    assert await _balance(db_sessionmaker, uid) == 0
    assert await _sum_signed(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_non_incremental_consume_shortfall_still_raises_savepoint_path(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Regress (ADR-064 §4): meta WITHOUT incremental (chat/admin/finalization) keeps the savepoint
    # path → InsufficientCreditsError on shortfall, no orphan debit, balance untouched.
    uid = await _seed_credit(db_sessionmaker, 3)
    meta = {"source": "chat"}  # not agent_run → reconcile does not apply → full savepoint rollback
    async with db_sessionmaker() as s:
        with pytest.raises(InsufficientCreditsError):
            await WalletService(s, AuditService(s)).consume(
                user_id=uid, amount=10, idempotency_key="chat:step:1", meta=meta
            )
        await s.commit()
    assert await _debits(db_sessionmaker, uid) == []  # savepoint rolled the insert back
    assert await _balance(db_sessionmaker, uid) == 3  # untouched
    assert await _debt(db_sessionmaker, uid) == 0


# ============================================================================
# §3 charge==0 (balance already 0): debit skipped (no ConflictError), depleted → pause.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_charge_zero_when_balance_drained_pauses_without_debit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 0)  # zero balance
    run_id = "run"
    await _seed_agent_run(db_sessionmaker, run_id, uid)
    _stop_route(run_id)
    body = _usage_delta(0, 1000, 0) + _completed(1000, 0)  # completed must NEVER be reached
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        relayed = await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))

    # want=1, balance=0 → charge=min(1,0)=0 → NO debit (no ConflictError from consume(amount<=0)),
    # depleted → pause. No ledger debit row at all.
    assert await _debits(db_sessionmaker, uid) == []
    assert b"event: run.completed" not in relayed
    assert b'"event": "run.paused"' in relayed or b'"run.paused"' in relayed
    paused = json.loads(relayed.split(b"data: ")[-1].strip())
    assert paused["reason"] == "credits_exhausted"
    assert paused["billed"] == 0
    ar = await _agent_run(db_sessionmaker, run_id)
    assert ar is not None and ar[0] == "paused" and ar[1] == "credits_exhausted"


# ============================================================================
# §3 pause-at-zero after a partial step: stop + run.paused, agent_runs paused, NO debt, net==bal.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_pause_at_zero_emits_run_paused_no_debt(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 2)  # only 2 credits
    run_id = "run"
    await _seed_agent_run(db_sessionmaker, run_id, uid)
    stop = _stop_route(run_id)
    # step0 cum(1000,0) owed=1 want=1 charge=1 (balance 2→1, not depleted);
    # step1 cum(3000,0) owed=3 want=2 balance=1 charge=1 actual=1 < want=2 → depleted → pause.
    body = (
        _usage_delta(0, 1000, 0)
        + _usage_delta(1, 3000, 0)
        + _completed(3000, 0)  # must not be reached
    )
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        relayed = await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))

    # Hermes stop was invoked (interrupt the run).
    assert stop.called  # type: ignore[attr-defined]
    # Synthetic terminal run.paused, NO run.completed.
    assert b"event: run.completed" not in relayed
    paused = json.loads(relayed.split(b"data: ")[-1].strip())
    assert paused["reason"] == "credits_exhausted"
    assert paused["status"] == "paused"
    assert paused["billed"] == 2  # both partial charges
    assert paused["balance"] == 0
    assert paused["usage"]["cumulative_input_tokens"] == 3000
    # agent_runs marked paused; balance drained to 0; NO debt (netBalance == balance).
    ar = await _agent_run(db_sessionmaker, run_id)
    assert ar is not None and ar[0] == "paused" and ar[1] == "credits_exhausted"
    assert await _balance(db_sessionmaker, uid) == 0
    assert await _debt(db_sessionmaker, uid) == 0
    # Money invariant: two per-step debits summing to 2.
    debits = await _debits(db_sessionmaker, uid)
    assert sum(a for _, a in debits) == 2
    assert await _sum_signed(db_sessionmaker, uid) == 0


# ============================================================================
# §6 Seed-from-ledger reconnect: charged restored, steps not re-billed.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_reconnect_seeds_charged_from_ledger_no_rebill(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 1000)
    run_id = "run"
    await _seed_agent_run(db_sessionmaker, run_id, uid)

    # First subscription: bill step0 only, then the SSE ends WITHOUT completion (a drop).
    _events_route(_usage_delta(0, 1000, 0), run_id)
    async with db_sessionmaker() as s1:
        await _collect(_proxy(s1).stream_events(user_id=uid, run_id=run_id))
    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:0", 1)]

    # Re-subscription: charged seeded from the ledger (=1). Replaying step0 → idempotent no-op; a
    # run.completed with the SAME cumulative → remainder 0 → no extra debit.
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=_usage_delta(0, 1000, 0) + _completed(1000, 0))
    )
    async with db_sessionmaker() as s2:
        await _collect(_proxy(s2).stream_events(user_id=uid, run_id=run_id))

    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:0", 1)], "no re-bill after reconnect"
    assert await _balance(db_sessionmaker, uid) == 999


@pytest.mark.asyncio
async def test_charged_for_run_escapes_like_metacharacters(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A run_id containing LIKE metacharacters ('_' matches any char). A decoy debit for a DIFFERENT
    # run must NOT be summed into charged_for_run via a false LIKE match (ESCAPE '\'), else charged
    # inflates → under-bill.
    uid = await _seed_credit(db_sessionmaker, 1000)
    run_id = "run_a"  # underscore is a LIKE wildcard if unescaped

    async def _insert_debit(key: str, amount: int) -> None:
        async with db_sessionmaker() as s:
            await s.execute(
                text(
                    "INSERT INTO ledger_transactions "
                    "(user_id, type, amount, meta, idempotency_key) "
                    "VALUES (:u, 'debit', :a, '{}'::jsonb, :k)"
                ),
                {"u": str(uid), "a": amount, "k": key},
            )
            await s.commit()

    await _insert_debit(f"{run_id}:0", 2)  # legit per-step debit for run_a
    await _insert_debit(run_id, 3)  # legit finalization debit for run_a (bare)
    # Decoy: a different run whose key matches "run_a:%" ONLY if '_' is an unescaped wildcard.
    await _insert_debit("runXa:0", 99)

    async with db_sessionmaker() as s:
        charged = await WalletService(s, AuditService(s)).charged_for_run(uid, run_id)
    assert charged == 5, "only run_a's own debits (2+3) — the decoy must be excluded by ESCAPE"


# ============================================================================
# Flag OFF: no per-step billing, single full run.completed debit, no agent_runs row, resume 404.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_flag_off_single_posthoc_debit_no_agent_run_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_credit(db_sessionmaker, 1000)
    run_id = "run_off"
    # usage.delta present but must be IGNORED for billing when the flag is off (post-hoc ADR-047).
    body = _usage_delta(0, 1000, 0) + _usage_delta(1, 2000, 0) + _completed(2000, 0)
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        relayed = await _collect(
            _proxy(outer, incremental=False).stream_events(user_id=uid, run_id=run_id)
        )

    # usage.delta relayed verbatim, but NOT billed; exactly one full debit on run.completed.
    assert b"event: usage.delta" in relayed
    owed = usage_to_credits(
        input_tokens=2000, output_tokens=0, credits_per_1k_input=_K_IN, credits_per_1k_output=_K_OUT
    )
    debits = await _debits(db_sessionmaker, uid)
    assert debits == [(run_id, owed)], "flag OFF → one post-hoc debit keyed by bare run_id"
    assert await _balance(db_sessionmaker, uid) == 1000 - owed
    # No agent_runs row is written in post-hoc mode.
    assert await _agent_run(db_sessionmaker, run_id) is None


@respx.mock
@pytest.mark.asyncio
async def test_flag_off_resume_unavailable_404(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Post-hoc mode writes no agent_runs row, so resume cannot resolve the run → 404.
    uid = await _seed_credit(db_sessionmaker, 1000)
    async with db_sessionmaker() as outer:
        with pytest.raises(NotFoundError):
            await _proxy(outer, incremental=False).resume(
                user_id=uid, run_id="never_started", message=None
            )
