"""Integration: agent-path billing data-integrity on insufficient balance (ADR-047 §6).

Real PostgreSQL (testcontainers) wiring the REAL ``WalletService`` (savepoint-atomic ``consume``)
and the REAL ``AgentProxyService`` SSE relay; only the Hermes instance is faked (respx) and the
``HermesInstanceManager`` is a fake endpoint. Unlike tests/unit/test_agent_proxy_service.py (which
fakes the wallet and therefore cannot observe the ledger), these tests assert on the COMMITTED
ledger/balance/audit state through a FRESH session — the only way to catch the orphan-debit defect
the ADR-047 §6 savepoint fix targets.

The defect (pre-fix): ``_bill_completed`` SWALLOWS ``InsufficientCreditsError`` so the SSE stream is
not broken; the outer session then COMMITS. If ``consume`` relied on the caller's outer ROLLBACK,
the INSERTed debit row (whose conditional UPDATE matched 0 rows) would be committed WITHOUT a
balance decrement — an orphan ``type='debit'`` row breaking ``balance == Σ(credit) − Σ(debit)`` and
surfacing as a phantom charge in ``GET /v1/wallet``. The savepoint fix rolls that INSERT back inside
``consume`` regardless of the outer commit.

Covers QA scope #1-#6 (agent data-integrity, self-contained atomicity, chat 409 path, idempotent
runId replay, successful debit savepoint release, amount/meta conflict).
"""

from __future__ import annotations

import contextlib
import uuid

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.service import AgentProxyService
from app.audit.service import AuditService
from app.config import Settings
from app.errors import ConflictError, InsufficientCreditsError
from app.hermes_runtime.manager import InstanceEndpoint
from app.observability.redaction import REDACTED
from app.wallet.service import WalletService
from tests.conftest import seed_user

# --- Constants -------------------------------------------------------------------------------
_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"


# --- Fakes (instance boundary only; wallet + audit are REAL) ---------------------------------
class FakeManager:
    """Stand-in for HermesInstanceManager; returns a fixed endpoint (no Docker)."""

    def __init__(self) -> None:
        self.endpoint = InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)
        self.ensure_running_calls: list[uuid.UUID] = []

    async def ensure_running(self, user_id: uuid.UUID) -> InstanceEndpoint:
        self.ensure_running_calls.append(user_id)
        return self.endpoint


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # defaults: input=1.0, output=5.0/1k.


def _proxy(session: AsyncSession) -> AgentProxyService:
    """Real AgentProxyService with REAL WalletService + AuditService over the test session."""
    audit = AuditService(session)
    wallet = WalletService(session, audit)
    return AgentProxyService(
        session=session,
        manager=FakeManager(),  # type: ignore[arg-type]
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
    """Σ(credit) − Σ(debit) over the committed ledger (the balance reconciliation invariant)."""
    row = await session.scalar(
        text(
            "SELECT COALESCE(SUM(CASE WHEN type='credit' THEN amount ELSE -amount END), 0) "
            "FROM ledger_transactions WHERE user_id = :u"
        ),
        {"u": str(uid)},
    )
    return int(row)


async def _seed_user_with_credit(
    db_sessionmaker: async_sessionmaker[AsyncSession], opening: int
) -> uuid.UUID:
    """Seed a user whose opening balance comes from a REAL grant (credit ledger row).

    ``seed_user(balance=N)`` sets ``wallets.balance`` directly WITHOUT a matching credit
    row, so the reconciliation invariant ``balance == Σ(credit) − Σ(debit)``
    (03-data-model.md, ADR-047 §6) would not hold against the ledger. Granting the opening
    credits the production way creates the credit row, so the invariant is meaningfully
    testable end-to-end.
    """
    uid = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)  # user only; no direct wallet balance.
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


async def _audit_payloads(session: AsyncSession, uid: uuid.UUID, event_type: str) -> list[dict]:
    rows = await session.scalars(
        text(
            "SELECT payload FROM audit_logs WHERE user_id = :u AND event_type = :e "
            "ORDER BY created_at"
        ),
        {"u": str(uid), "e": event_type},
    )
    # JSONB payload is returned by asyncpg as a dict already
    # (see test_byok / test_chat_attachments).
    return list(rows)


# ============================================================================
# #1 KEY data-integrity test: run.completed at amount > balance > 0 →
#    ADR-051 §2.1 partial-debit + debt (NOT the old full savepoint-rollback). The agent path with
#    AGENT_DEBT_RECONCILE_ENABLED (default true) debits the AVAILABLE balance (partial ledger row),
#    accrues the shortfall into wallets.debt (NOT a ledger row), drives balance→0, and records
#    billing_debit_insufficient with partialDebited/debtAdded. The ledger invariant
#    balance == Σ(credit) − Σ(debit) still holds (debt is a SEPARATE aggregate). SSE not broken.
#
#    Masking-regression guard (CU rule): this REPLACES the pre-ADR-051 assertion (debit_count==0 /
#    balance untouched) which the new partial-debit path deliberately overrides. The invariant being
#    verified is the NEW one (partial debit + debt), asserted directly.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_agent_shortfall_partial_debit_and_debt_adr051(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=5)  # below cost 7 → shortfall.

    run_id = "run_insufficient_1"
    # usage 2000 in / 1000 out → amount = ceil(2000/1000*1 + 1000/1000*5) = 7 > balance 5.
    body = _sse("message.delta", '{"text":"working"}') + _sse(
        "run.completed",
        '{"usage":{"input_tokens":2000,"output_tokens":1000,"total_tokens":3000},"model":"m"}',
    )
    _events_route(body, run_id)

    # The outer session mirrors the SSE generator's session_scope: it COMMITS after the relay.
    async with db_sessionmaker() as outer:
        relayed = await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))
        await outer.commit()

    # SSE stream not broken — every byte relayed verbatim.
    assert relayed == body
    assert b"event: run.completed" in relayed

    # Fresh session: ADR-051 §2.1 — partial debit (=balance) committed, balance→0, debt=delta.
    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 1, "partial debit row must be committed (ADR-051)"
        assert await _balance(check, uid) == 0, "balance must be drained to 0 on shortfall"
        # debt = amount(7) - balance(5) = 2.
        debt = await check.scalar(
            text("SELECT debt FROM wallets WHERE user_id=:u"), {"u": str(uid)}
        )
        assert int(debt) == 2
        # The ledger invariant still holds: debt is a SEPARATE aggregate, not a ledger row.
        # Σ(credit 5) − Σ(debit 5) == balance 0.
        assert await _sum_signed(check, uid) == await _balance(check, uid)

        # billing_debit_insufficient audit recorded with partialDebited/debtAdded (no secrets).
        ins = await _audit_payloads(check, uid, "billing_debit_insufficient")
        assert len(ins) == 1
        payload = ins[0]
        assert payload["runId"] == run_id
        assert payload["requiredAmount"] == 7
        assert payload["partialDebited"] == 5
        assert payload["debtAdded"] == 2
        assert payload["debt"] == 2
        assert payload["model"] == "m"
        assert payload["usage"] == {
            "input_tokens": 2000,
            "output_tokens": 1000,
            "total_tokens": 3000,
        }
        # ADR-049: usage token-COUNTS survive redaction as real ints, NOT ***REDACTED***.
        usage = payload["usage"]
        assert all(
            isinstance(usage[k], int) for k in ("input_tokens", "output_tokens", "total_tokens")
        ), f"usage counts must be ints, not redacted strings: {usage!r}"
        assert REDACTED not in str(usage), "usage must not be redacted (ADR-049)"
        # No secrets leaked into the audit payload.
        assert _API_KEY not in str(payload)
        assert "authorization" not in {k.lower() for k in payload}
        # The partial-debit path records ONLY billing_debit_insufficient (with partialDebited),
        # NOT a separate billing_debit audit (the ledger row is the partial debit; ADR-051 §2.1).
        assert await _audit_payloads(check, uid, "billing_debit") == []


# ============================================================================
# #2 Self-contained atomicity of consume: caller swallows InsufficientCreditsError, then commits →
#    INSERT debit undone by savepoint, no orphan row, invariant preserved (no agent proxy).
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_consume_savepoint_rolls_back_even_when_caller_swallows_and_commits(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=2)

    async with db_sessionmaker() as outer:
        audit = AuditService(outer)
        svc = WalletService(outer, audit)
        # Caller swallows the error (agent-SSE semantics) and STILL commits the outer tx.
        with contextlib.suppress(InsufficientCreditsError):
            await svc.consume(
                user_id=uid, amount=10, idempotency_key="run_swallow", meta={"model": "m"}
            )
        await outer.commit()

    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 0
        assert await _balance(check, uid) == 2
        assert await _sum_signed(check, uid) == await _balance(check, uid)


# ============================================================================
# #3 chat /wallet/consume insufficient → InsufficientCreditsError propagates (409-equiv); no orphan.
# ============================================================================
@pytest.mark.asyncio
async def test_chat_consume_insufficient_propagates_and_no_orphan(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as seed_s:
        uid = await seed_user(seed_s, balance=0)

    async with db_sessionmaker() as s:
        svc = WalletService(s, AuditService(s))
        with pytest.raises(InsufficientCreditsError):
            await svc.consume(user_id=uid, amount=1, idempotency_key="step-1", meta={"model": "m"})
        # Chat path: the HTTP request tx rolls back wholesale
        # (mirrors client fixture's session_scope).
        await s.rollback()

    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 0
        assert await _balance(check, uid) == 0


@pytest.mark.asyncio
async def test_insufficient_credits_is_conflict_409_subclass() -> None:
    # /wallet/consume maps InsufficientCreditsError → 409 (it is a ConflictError subclass).
    assert issubclass(InsufficientCreditsError, ConflictError)
    assert InsufficientCreditsError.status_code == 409
    assert InsufficientCreditsError.code == "insufficient_credits"


# ============================================================================
# #4 Idempotent replay by runId: second run.completed with the same runId → exactly one debit,
#    savepoint released correctly, no second billing_debit_insufficient when balance is sufficient.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_agent_idempotent_replay_same_run_id_single_debit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=100)

    run_id = "run_idem_1"
    # amount = ceil(1000/1000*1 + 0) = 1.
    completed = _sse(
        "run.completed",
        '{"usage":{"input_tokens":1000,"output_tokens":0,"total_tokens":1000},"model":"m"}',
    )
    _events_route(completed, run_id)

    # First subscription: one effective debit.
    async with db_sessionmaker() as s1:
        await _collect(_proxy(s1).stream_events(user_id=uid, run_id=run_id))
        await s1.commit()
    # Re-subscription to the SAME runId: consume is called again but idempotency by runId → no dup.
    async with db_sessionmaker() as s2:
        await _collect(_proxy(s2).stream_events(user_id=uid, run_id=run_id))
        await s2.commit()

    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 1, "runId idempotency must yield exactly one debit"
        assert await _balance(check, uid) == 99
        assert await _sum_signed(check, uid) == await _balance(check, uid)
        # Sufficient balance → no insufficient audit on either pass.
        assert await _audit_payloads(check, uid, "billing_debit_insufficient") == []
        assert len(await _audit_payloads(check, uid, "billing_debit")) == 1


# ============================================================================
# #5 Successful debit: savepoint release (nested commit), balance decremented, billing_debit audit.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_agent_successful_debit_savepoint_release(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=50)

    run_id = "run_ok_1"
    # amount = ceil(2000/1000*1 + 1000/1000*5) = 7.
    body = _sse(
        "run.completed",
        '{"usage":{"input_tokens":2000,"output_tokens":1000,"total_tokens":3000},"model":"m"}',
    )
    _events_route(body, run_id)

    async with db_sessionmaker() as outer:
        await _collect(_proxy(outer).stream_events(user_id=uid, run_id=run_id))
        await outer.commit()

    async with db_sessionmaker() as check:
        assert await _debit_count(check, uid) == 1
        assert await _balance(check, uid) == 43  # 50 - 7
        assert await _sum_signed(check, uid) == await _balance(check, uid)
        debits = await _audit_payloads(check, uid, "billing_debit")
        assert len(debits) == 1
        assert debits[0]["amount"] == 7
        assert debits[0]["newBalance"] == 43
        assert await _audit_payloads(check, uid, "billing_debit_insufficient") == []


# ============================================================================
# #6 Same idempotency_key with a different amount → 409 ConflictError (outside the
#    savepoint-fail path). Verifies the replay branch lives outside the savepoint and
#    still guards payload equality.
# ============================================================================
@pytest.mark.asyncio
async def test_consume_same_key_different_amount_conflicts_no_orphan(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_user_with_credit(db_sessionmaker, opening=100)

    key = "run_conflict_1"
    async with db_sessionmaker() as s1:
        await WalletService(s1, AuditService(s1)).consume(
            user_id=uid, amount=1, idempotency_key=key, meta={"model": "m"}
        )
        await s1.commit()

    async with db_sessionmaker() as s2:
        svc = WalletService(s2, AuditService(s2))
        with pytest.raises(ConflictError, match="different payload"):
            await svc.consume(user_id=uid, amount=2, idempotency_key=key, meta={"model": "m"})
        await s2.rollback()

    async with db_sessionmaker() as check:
        # Exactly the one original debit; balance reflects only it (99); no orphan
        # from the conflict.
        assert await _debit_count(check, uid) == 1
        assert await _balance(check, uid) == 99
        assert await _sum_signed(check, uid) == await _balance(check, uid)
