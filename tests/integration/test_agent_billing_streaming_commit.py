"""Regression: agent SSE billing PERSISTS without a manual outer commit (ADR-047 §6, ADR-051 §2.1).

WHY THIS FILE EXISTS (masking-regression guard, CU rule).
``tests/integration/test_agent_billing_savepoint.py`` (#1/#4/#5) asserts the COMMITTED ledger state
but each test does a MANUAL ``await outer.commit()`` AFTER ``stream_events``. That manual commit
SUBSTITUTED the internal commit that the production code was missing — so those tests were green
BEFORE the fix and did NOT guard the regression. The live e2e then surfaced the real defect: a
successful agent run logged "agent run billed" but wrote NO debit and left the balance unchanged,
because ``_bill_completed`` runs INSIDE the StreamingResponse body generator — AFTER FastAPI has torn
down the request session dependency (``session_scope`` yield→commit happens on Depends-resume, which
for a StreamingResponse is BEFORE the body iterates). ``consume`` only released a SAVEPOINT
(begin_nested), never the outer transaction, so nothing persisted.

The fix added ``await self._session.commit()`` inside ``_bill_completed`` on BOTH the success branch
and after ``_record_insufficient`` (ADR-051 partial-debit + debt branch); the generic-except branch
rolls back.

These tests therefore DELIBERATELY DO NOT call ``outer.commit()`` after ``stream_events`` — they rely
solely on the production internal commit and verify persistence through a FRESH, independent session
from the SAME container-bound sessionmaker (real engine, real commits — see conftest ``_engine`` /
``db_sessionmaker``). If the internal commit were removed, the fresh session would see no debit and
the tests would fail (proven by the negative-guard test below, which patches the session's commit to
a no-op and asserts the absence-of-persistence).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.service import AgentProxyService
from app.audit.service import AuditService
from app.config import Settings
from app.hermes_runtime.manager import InstanceEndpoint
from app.wallet.service import WalletService
from tests.conftest import seed_user

# --- Constants -------------------------------------------------------------------------------
_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"


# --- Fakes (instance boundary only; wallet + audit are REAL) ---------------------------------
class _FakeManager:
    """Stand-in for HermesInstanceManager; returns a fixed endpoint (no Docker)."""

    def __init__(self) -> None:
        self.endpoint = InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)

    async def ensure_running(self, user_id: uuid.UUID) -> InstanceEndpoint:
        return self.endpoint


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # defaults: input=1.0, output=5.0/1k.


def _proxy(session: AsyncSession) -> AgentProxyService:
    """Real AgentProxyService with REAL WalletService + AuditService over the test session."""
    audit = AuditService(session)
    wallet = WalletService(session, audit)
    return AgentProxyService(
        session=session,
        manager=_FakeManager(),  # type: ignore[arg-type]
        wallet=wallet,
        audit=audit,
        settings=_settings(),
    )


def _sse(name: str, data_json: str) -> bytes:
    return f"event: {name}\ndata: {data_json}\n\n".encode()


def _events_route(body: bytes, run_id: str, status: int = 200) -> object:
    return respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(status, content=body)
    )


async def _collect(stream: object) -> bytes:
    out = b""
    async for chunk in stream:  # type: ignore[attr-defined]
        out += chunk
    return out


# --- Ledger / audit assertions over a FRESH session ------------------------------------------
async def _balance(session: AsyncSession, uid: uuid.UUID) -> int:
    row = await session.scalar(
        text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(uid)}
    )
    return int(row) if row is not None else 0


async def _debit_count(session: AsyncSession, uid: uuid.UUID) -> int:
    row = await session.scalar(
        text("SELECT count(*) FROM ledger_transactions WHERE user_id = :u AND type='debit'"),
        {"u": str(uid)},
    )
    return int(row)


async def _sum_signed(session: AsyncSession, uid: uuid.UUID) -> int:
    row = await session.scalar(
        text(
            "SELECT COALESCE(SUM(CASE WHEN type='credit' THEN amount ELSE -amount END), 0) "
            "FROM ledger_transactions WHERE user_id = :u"
        ),
        {"u": str(uid)},
    )
    return int(row)


async def _debt(session: AsyncSession, uid: uuid.UUID) -> int:
    row = await session.scalar(text("SELECT debt FROM wallets WHERE user_id = :u"), {"u": str(uid)})
    return int(row) if row is not None else 0


async def _audit_payloads(session: AsyncSession, uid: uuid.UUID, event_type: str) -> list[dict]:
    rows = await session.scalars(
        text(
            "SELECT payload FROM audit_logs WHERE user_id = :u AND event_type = :e "
            "ORDER BY created_at"
        ),
        {"u": str(uid), "e": event_type},
    )
    return list(rows)


async def _seed_user_with_credit(
    db_sessionmaker: async_sessionmaker[AsyncSession], opening: int
) -> uuid.UUID:
    """Seed a user whose opening balance comes from a REAL grant (credit ledger row) so the
    reconciliation invariant balance == Σ(credit) − Σ(debit) is meaningfully testable."""
    uid = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
        if opening > 0:
            await WalletService(s, AuditService(s)).grant(
                user_id=uid,
                amount=opening,
                idempotency_key=f"seed-grant:{uid}",
                meta={},
                reason="seed",
            )
        await s.commit()
    return uid


# ============================================================================
# #A Successful run.completed PERSISTS the debit WITHOUT a manual outer commit (the e2e defect).
#    Replays the live scenario: amount=7, balance 2000 → 1993. NO ``outer.commit()`` after the
#    stream — the production internal ``self._session.commit()`` in _bill_completed must persist it.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_agent_success_persists_debit_without_manual_outer_commit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=2000)

    run_id = "run_stream_commit_ok"
    # usage 2000 in / 1000 out → amount = ceil(2000/1000*1 + 1000/1000*5) = 7.
    body = _sse("message.delta", '{"text":"working"}') + _sse(
        "run.completed",
        '{"usage":{"input_tokens":2000,"output_tokens":1000,"total_tokens":3000},"model":"m"}',
    )
    _events_route(body, run_id)

    # Mirror the REAL StreamingResponse path: iterate the body generator to completion and then
    # CLOSE the session WITHOUT committing it. Production must have committed internally.
    async with db_sessionmaker() as outer:
        relayed = await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))
        # NOTE: deliberately NO ``await outer.commit()`` here (masking-regression guard).

    assert relayed == body
    assert b"event: run.completed" in relayed

    # Fresh, independent session: the debit + balance decrement must be visible (persisted).
    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 1, (
            "successful agent run must persist exactly one debit via the INTERNAL streaming commit "
            "(no manual outer commit) — this is the e2e defect guard"
        )
        assert await _balance(check, uid) == 1993, "balance must decrement 2000 → 1993"
        assert await _sum_signed(check, uid) == await _balance(check, uid)
        debits = await _audit_payloads(check, uid, "billing_debit")
        assert len(debits) == 1
        assert debits[0]["amount"] == 7
        assert debits[0]["newBalance"] == 1993


# ============================================================================
# #B Insufficient/partial branch (ADR-051 §2.1) PERSISTS partial debit + debt + audit WITHOUT a
#    manual outer commit (the commit added after _record_insufficient).
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_agent_partial_debit_persists_without_manual_outer_commit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=5)  # below cost 7 → shortfall.

    run_id = "run_stream_commit_partial"
    body = _sse(
        "run.completed",
        '{"usage":{"input_tokens":2000,"output_tokens":1000,"total_tokens":3000},"model":"m"}',
    )
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        relayed = await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))
        # NO manual outer.commit() — the partial-debit branch commits internally.

    assert relayed == body

    async with db_sessionmaker() as check:
        # Partial debit (=available balance 5) persisted, balance drained to 0, debt = 7 - 5 = 2.
        assert await _debit_count(check, uid) == 1, "partial debit must persist via internal commit"
        assert await _balance(check, uid) == 0
        assert await _debt(check, uid) == 2
        # Ledger invariant holds (debt is a separate aggregate, not a ledger row).
        assert await _sum_signed(check, uid) == await _balance(check, uid)
        # billing_debit_insufficient audit persisted with partialDebited / debtAdded.
        ins = await _audit_payloads(check, uid, "billing_debit_insufficient")
        assert len(ins) == 1, "insufficient audit must persist via internal commit"
        payload = ins[0]
        assert payload["runId"] == run_id
        assert payload["requiredAmount"] == 7
        assert payload["partialDebited"] == 5
        assert payload["debtAdded"] == 2
        assert payload["debt"] == 2
        # The partial-debit path records ONLY billing_debit_insufficient, not billing_debit.
        assert await _audit_payloads(check, uid, "billing_debit") == []


# ============================================================================
# #C Idempotent runId replay across two SEPARATE streaming subscriptions, each relying on its own
#    INTERNAL commit (no manual outer commit on either) → exactly ONE debit.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_agent_idempotent_replay_persists_single_debit_without_manual_commit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=100)

    run_id = "run_stream_commit_idem"
    # amount = ceil(1000/1000*1 + 0) = 1.
    completed = _sse(
        "run.completed",
        '{"usage":{"input_tokens":1000,"output_tokens":0,"total_tokens":1000},"model":"m"}',
    )
    _events_route(completed, run_id)

    # First subscription — internal commit must persist the single debit.
    async with db_sessionmaker() as s1:
        await _collect(_proxy(s1).stream_events(user_id=uid, run_id=run_id))
        # NO manual commit.

    # Intermediate fresh-session check: the first debit is already persisted (proves internal commit
    # happened on pass 1, not merely on a later manual commit).
    async with db_sessionmaker() as mid:
        assert await _debit_count(mid, uid) == 1, "first pass must persist via internal commit"
        assert await _balance(mid, uid) == 99

    # Re-subscription to the SAME runId — consume() hits ON CONFLICT; the internal commit is a
    # harmless no-op; no duplicate debit.
    async with db_sessionmaker() as s2:
        await _collect(_proxy(s2).stream_events(user_id=uid, run_id=run_id))
        # NO manual commit.

    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 1, "runId idempotency must yield exactly one debit"
        assert await _balance(check, uid) == 99
        assert await _sum_signed(check, uid) == await _balance(check, uid)
        assert len(await _audit_payloads(check, uid, "billing_debit")) == 1
        assert await _audit_payloads(check, uid, "billing_debit_insufficient") == []


# ============================================================================
# #D NEGATIVE-GUARD: prove the tests above REALLY guard the defect. Patch the proxy session's
#    ``commit`` to a no-op (simulating the pre-fix "internal commit missing" regression) WITHOUT
#    touching production src. The fresh session must then see NO persisted debit — i.e. the success
#    assertions in #A would FAIL. This confirms #A is not green by accident (e.g. via a stray
#    auto-commit elsewhere) but specifically because of ``self._session.commit()`` in _bill_completed.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_negative_guard_no_internal_commit_loses_debit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=2000)

    run_id = "run_stream_commit_neg"
    body = _sse(
        "run.completed",
        '{"usage":{"input_tokens":2000,"output_tokens":1000,"total_tokens":3000},"model":"m"}',
    )
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        # Simulate the regression: the session never commits (mirrors the missing internal commit
        # while the StreamingResponse teardown also never commits this session). The savepoint
        # released inside consume() is NOT enough to persist across sessions.
        outer.commit = AsyncMock()  # type: ignore[method-assign]
        relayed = await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))
        # No manual commit either — exactly the pre-fix runtime situation.

    # Stream still relayed intact (billing never breaks the stream).
    assert relayed == body

    # Fresh session: with the internal commit neutralised, the debit is LOST (the regression).
    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 0, (
            "negative-guard: without the internal commit the debit must NOT persist — proves the "
            "positive tests genuinely depend on self._session.commit() in _bill_completed"
        )
        assert (
            await _balance(check, uid) == 2000
        ), "balance must be unchanged when commit is a no-op"
