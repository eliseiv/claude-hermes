"""Integration: admin wallet/debit (ADR-061). Real PostgreSQL.

Covers the full follow_up_for_qa for ADR-061 (POST /v1/admin/wallet/debit):
- success: ledger type=debit + meta.source='admin_debit', balance -= amount, idempotentReplay=false,
  newBalance correct, ledgerTxId present.
- "leave N": balance=B, debit(B-N) → newBalance==N (composition with GET wallet).
- zero-out: debit(amount==balance) → newBalance==0 (CHECK balance>=0 holds).
- idempotency: same key+payload → idempotentReplay=true, same ledgerTxId, no double debit.
- insufficient: amount>balance → 409 insufficient_credits, balance untouched, NO orphan debit row
  (savepoint rollback), metric admin_debit_total{insufficient}.
- key reuse with different amount OR previously used for credit → 409 conflict, no debit.
- unknown userId → 404 user_not_found, no users/wallet/ledger row created (consume not reached).
- audit: success writes BOTH billing_debit (Wallet) AND admin_debit (Admin, actor=admin, reason,
  NO X-Admin-Token secret).
- ADR-051 debt: with wallets.debt>0 the debit touches ONLY balance, debt unchanged, at BOTH
  AGENT_DEBT_RECONCILE_ENABLED=true and =false (source != agent_run → reconcile never applies).
- security (direct for THIS endpoint, enumerated-contour guard): client X-API-Key without
  X-Admin-Token → 401; body > 8 KB → 413; admin rate-limit → 429; X-Admin-Token never in audit.

Each enumerated contour is asserted by a DIRECT test; the endpoint's adminToken-only security and
Admin tag are asserted directly in tests/integration/test_api_documentation.py (_ADMIN_PATHS /
_ENDPOINT_TAG now enumerate /v1/admin/wallet/debit — test-scope gate for the new API surface).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_ADMIN_SECRET = "admin-secret-debit-0123456789abcdef0123456789abcdef01"
_ADMIN_PREV = "admin-secret-debit-prev-0123456789abcdef0123456789ab"
_ADMIN_HEADERS = {"X-Admin-Token": _ADMIN_SECRET}


@pytest.fixture
async def admin_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with admin secrets set and the admin rate-limit forced open (deterministic)."""
    settings = get_settings()
    orig_secret, orig_prev = settings.admin_api_secret, settings.admin_api_secret_prev
    settings.admin_api_secret = _ADMIN_SECRET
    settings.admin_api_secret_prev = _ADMIN_PREV

    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import admin as admin_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    async def _allow_admin(**_kwargs: Any) -> bool:
        return True

    orig_admin = rate_limit.enforce_admin_limits
    rate_limit.enforce_admin_limits = _allow_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = _allow_admin  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    settings.admin_api_secret = orig_secret
    settings.admin_api_secret_prev = orig_prev
    rate_limit.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]


# --------------------------- db helpers ---------------------------
async def _balance(maker: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with maker() as s:
        row = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": uid})
        return int(row) if row is not None else 0


async def _debt(maker: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with maker() as s:
        row = await s.scalar(text("SELECT debt FROM wallets WHERE user_id=:u"), {"u": uid})
        return int(row) if row is not None else 0


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: str) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": uid}) or 0)


async def _set_debt(maker: async_sessionmaker[AsyncSession], uid: str, debt: int) -> None:
    async with maker() as s:
        await s.execute(text("UPDATE wallets SET debt=:d WHERE user_id=:u"), {"d": debt, "u": uid})
        await s.commit()


async def _audit_payloads(
    maker: async_sessionmaker[AsyncSession], uid: str, event_type: str
) -> list[dict[str, Any]]:
    async with maker() as s:
        rows = await s.scalars(
            text(
                "SELECT payload FROM audit_logs WHERE user_id=:u AND event_type=:e "
                "ORDER BY created_at"
            ),
            {"u": uid, "e": event_type},
        )
        return list(rows)


# ============================================================================
# Success: ledger debit + meta.source, balance decrement, response shape
# ============================================================================
@pytest.mark.asyncio
async def test_debit_success_decrements_balance_and_writes_ledger(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 30, "idempotencyKey": "d-ok", "reason": "correction"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["newBalance"] == 70
    assert body["idempotentReplay"] is False
    assert body["ledgerTxId"] is not None
    assert await _balance(db_sessionmaker, str(uid)) == 70

    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT type, amount, meta->>'source' AS source, meta->>'reason' AS reason "
                    "FROM ledger_transactions WHERE user_id=:u"
                ),
                {"u": str(uid)},
            )
        ).one()
    assert row.type == "debit"
    assert int(row.amount) == 30
    assert row.source == "admin_debit"
    assert row.reason == "correction"


@pytest.mark.asyncio
async def test_debit_leaves_n_via_delta(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-061 §Decision.5: "make it N" = read current balance, debit(current - N). Here B=100, N=25.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 75, "idempotencyKey": "d-leave", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["newBalance"] == 25
    # Compose with the admin wallet view (GET), the operator's read side of the flow.
    v = await admin_client.get(f"/v1/admin/wallet/{uid}", headers=_ADMIN_HEADERS)
    assert v.status_code == 200, v.text
    assert v.json()["balance"] == 25


@pytest.mark.asyncio
async def test_debit_zero_out_balance(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # amount == balance → newBalance 0; the CHECK (balance >= 0) is satisfied (no rollback).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=40)
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 40, "idempotencyKey": "d-zero", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["newBalance"] == 0
    assert await _balance(db_sessionmaker, str(uid)) == 0


# ============================================================================
# Idempotency: same key + payload → replay, no double debit
# ============================================================================
@pytest.mark.asyncio
async def test_debit_idempotent_replay_no_double_debit(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    payload = {"userId": str(uid), "amount": 30, "idempotencyKey": "d-dup", "reason": "x"}
    r1 = await admin_client.post("/v1/admin/wallet/debit", json=payload, headers=_ADMIN_HEADERS)
    r2 = await admin_client.post("/v1/admin/wallet/debit", json=payload, headers=_ADMIN_HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["idempotentReplay"] is False
    assert r2.json()["idempotentReplay"] is True
    assert r1.json()["ledgerTxId"] == r2.json()["ledgerTxId"]
    assert await _balance(db_sessionmaker, str(uid)) == 70  # debited once, not 40
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        str(uid),
    )
    assert debits == 1
    # billing_debit (from consume) is written ONCE — only on the real debit, not on the replay.
    billing = await _audit_payloads(db_sessionmaker, str(uid), "billing_debit")
    assert len(billing) == 1
    # admin_debit is an operator-action log: mirroring AdminService.grant it is recorded on EVERY
    # admin call (the replay too), each carrying its own idempotentReplay flag.
    admin = await _audit_payloads(db_sessionmaker, str(uid), "admin_debit")
    assert len(admin) == 2
    assert [p["idempotentReplay"] for p in admin] == [False, True]


# ============================================================================
# Insufficient balance: 409 insufficient_credits, no mutation, no orphan row
# ============================================================================
@pytest.mark.asyncio
async def test_debit_insufficient_409_no_mutation_no_orphan_row(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.observability.metrics import admin_debit_total

    before = admin_debit_total.labels(result="insufficient")._value.get()
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=10)
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 50, "idempotencyKey": "d-insuf", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "insufficient_credits"  # NOT clamped, NOT generic conflict
    assert await _balance(db_sessionmaker, str(uid)) == 10  # untouched
    # Savepoint rollback: the just-inserted debit row must be undone — NO orphan row.
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        str(uid),
    )
    assert debits == 0
    after = admin_debit_total.labels(result="insufficient")._value.get()
    assert after == before + 1
    # No admin_debit audit was written for the failed operation.
    assert await _audit_payloads(db_sessionmaker, str(uid), "admin_debit") == []


# ============================================================================
# Key reuse with a different payload / a credit key → 409 conflict, no debit
# ============================================================================
@pytest.mark.asyncio
async def test_debit_same_key_different_amount_409_conflict(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 20, "idempotencyKey": "d-conf", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 55, "idempotencyKey": "d-conf", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text
    assert await _balance(db_sessionmaker, str(uid)) == 80  # only the first debit applied
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        str(uid),
    )
    assert debits == 1


@pytest.mark.asyncio
async def test_debit_key_previously_used_for_credit_409_conflict(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A key used by a prior CREDIT grant reused for a debit → 409 conflict (shared key-space,
    # existing.type != 'debit'), with NO balance change from the debit attempt.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    g = await admin_client.post(
        "/v1/admin/credits/grant",
        json={"userId": str(uid), "amount": 50, "idempotencyKey": "shared-key", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert g.status_code == 200, g.text
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 20, "idempotencyKey": "shared-key", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text
    assert await _balance(db_sessionmaker, str(uid)) == 50  # unchanged by the rejected debit
    debits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'",
        str(uid),
    )
    assert debits == 0


# ============================================================================
# Unknown user → 404, no provisioning, consume not reached
# ============================================================================
@pytest.mark.asyncio
async def test_debit_unknown_user_404_no_provisioning(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    missing = uuid.uuid4()
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(missing), "amount": 5, "idempotencyKey": "d-404", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "user_not_found"
    async with db_sessionmaker() as s:
        users = await s.scalar(text("SELECT count(*) FROM users WHERE id=:u"), {"u": str(missing)})
        wallet = await s.scalar(
            text("SELECT count(*) FROM wallets WHERE user_id=:u"), {"u": str(missing)}
        )
        ledger = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"), {"u": str(missing)}
        )
    assert int(users) == 0  # users NOT provisioned
    assert int(wallet) == 0  # _ensure_wallet (inside consume) NOT reached
    assert int(ledger) == 0


# ============================================================================
# Audit: success writes BOTH billing_debit and admin_debit; no admin secret
# ============================================================================
@pytest.mark.asyncio
async def test_debit_success_writes_billing_and_admin_audit_no_secret(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={
            "userId": str(uid),
            "amount": 30,
            "idempotencyKey": "d-audit",
            "reason": "support reason",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    billing = await _audit_payloads(db_sessionmaker, str(uid), "billing_debit")
    admin = await _audit_payloads(db_sessionmaker, str(uid), "admin_debit")
    assert len(billing) == 1  # WalletService.consume side
    assert len(admin) == 1  # AdminService.debit side
    p = admin[0]
    assert p["actor"] == "admin"
    assert p["userId"] == str(uid)
    assert p["amount"] == 30
    assert p["reason"] == "support reason"
    assert p["idempotencyKey"] == "d-audit"
    assert p["ledgerTxId"] == r.json()["ledgerTxId"]
    assert p["idempotentReplay"] is False

    # The X-Admin-Token secret never leaks into any audit payload for this user.
    async with db_sessionmaker() as s:
        rows = await s.scalars(
            text("SELECT payload::text FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
        )
        blob = " ".join(rows)
    assert _ADMIN_SECRET not in blob
    assert _ADMIN_PREV not in blob


# ============================================================================
# ADR-051 debt: debit touches only balance; debt unchanged at BOTH flag states
# ============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("reconcile_enabled", [True, False])
async def test_debit_does_not_touch_debt_regardless_of_flag(
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    reconcile_enabled: bool,
) -> None:
    # ADR-061 §5 / ADR-051: admin_debit uses meta.source='admin_debit' != 'agent_run', so
    # _agent_reconcile_applies is False regardless of AGENT_DEBT_RECONCILE_ENABLED. The debit takes
    # the plain savepoint path: balance -= amount, wallets.debt is NOT touched (no accrual, no
    # clawback). Behaviour must be identical for the flag on AND off.
    settings = get_settings()
    original = settings.agent_debt_reconcile_enabled
    settings.agent_debt_reconcile_enabled = reconcile_enabled
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, balance=100)
        await _set_debt(db_sessionmaker, str(uid), 7)  # pre-existing outstanding debt
        assert await _debt(db_sessionmaker, str(uid)) == 7

        r = await admin_client.post(
            "/v1/admin/wallet/debit",
            json={
                "userId": str(uid),
                "amount": 40,
                "idempotencyKey": f"d-debt-{reconcile_enabled}",
                "reason": "x",
            },
            headers=_ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        assert r.json()["newBalance"] == 60  # only balance moved
        assert await _balance(db_sessionmaker, str(uid)) == 60
        assert await _debt(db_sessionmaker, str(uid)) == 7  # debt untouched, identical both flags
        # admin_debit audit carries no debt fields (plain path, not the shortfall path).
        admin = await _audit_payloads(db_sessionmaker, str(uid), "admin_debit")
        assert len(admin) == 1
        assert "debt" not in admin[0]
        assert "debtAdded" not in admin[0]
    finally:
        settings.agent_debt_reconcile_enabled = original


# ============================================================================
# Security / negatives (direct for /v1/admin/wallet/debit — enumerated-contour guard)
# ============================================================================
@pytest.mark.asyncio
async def test_debit_client_key_without_admin_token_401(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A valid client contour (X-API-Key + X-User-Id) but NO X-Admin-Token must NOT authorize.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uid), "amount": 5, "idempotencyKey": "d-401", "reason": "x"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 401
    assert await _balance(db_sessionmaker, str(uid)) == 100  # no debit occurred


@pytest.mark.asyncio
async def test_debit_body_over_8kb_413(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Schema-valid body inflated past the 8 KB admin cap (ADR-009 §6) → 413.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    padding = " " * (9 * 1024)
    raw = f'{{"userId": "{uid}", "amount": 5, "idempotencyKey": "big", "reason": "x"{padding}}}'
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        content=raw.encode(),
        headers={**_ADMIN_HEADERS, "Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text
    assert await _balance(db_sessionmaker, str(uid)) == 100  # rejected before any debit


@pytest.mark.asyncio
async def test_debit_rate_limit_returns_429(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
    from app.api_gateway.routers import admin as admin_router

    async def _deny(**_kwargs: Any) -> bool:
        return False

    prev = admin_router.enforce_admin_limits
    admin_router.enforce_admin_limits = _deny  # type: ignore[assignment]
    try:
        r = await admin_client.post(
            "/v1/admin/wallet/debit",
            json={"userId": str(uid), "amount": 5, "idempotencyKey": "d-429", "reason": "x"},
            headers=_ADMIN_HEADERS,
        )
        assert r.status_code == 429
    finally:
        admin_router.enforce_admin_limits = prev  # type: ignore[assignment]
    assert await _balance(db_sessionmaker, str(uid)) == 100  # no debit on a rate-limited request


@pytest.mark.asyncio
async def test_debit_no_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/wallet/debit",
        json={"userId": str(uuid.uuid4()), "amount": 5, "idempotencyKey": "d-na", "reason": "x"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_debit_openapi_security_is_admin_token_only(admin_client: AsyncClient) -> None:
    # Direct OpenAPI assert for the NEW route (belt with the enumerated _ADMIN_PATHS test in
    # test_api_documentation.py).
    r = await admin_client.get("/openapi.json")
    assert r.status_code == 200, r.text
    op = r.json()["paths"]["/v1/admin/wallet/debit"]["post"]
    assert op.get("security") == [{"adminToken": []}], op.get("security")
