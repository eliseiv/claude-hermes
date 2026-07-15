"""Integration: client-facing debt/netBalance surfacing (ADR-063). Real PostgreSQL.

ADR-063 adds two ADDITIVE fields — ``debt`` and ``netBalance`` — to BOTH client responses
``GET /v1/wallet`` (WalletResponse) and ``GET /v1/policy/effective`` (EffectivePolicyResponse).
Debt is read from the SAME ``wallets.debt`` column (ADR-051) exposed in the admin contour
(ADR-061). Existing ``balance`` / ``creditsBalance`` are unchanged (``>= 0``, value of
``wallets.balance``). netBalance == balance − debt (== creditsBalance − debt) and may be < 0.

Each enumerated contour of the task is covered by a DIRECT test:
- wallet at debt>0: debt==N, netBalance==balance−N (<0), balance>=0;
- policy at debt>0: debt==N, netBalance==creditsBalance−N (<0), creditsBalance>=0;
- debt==0 in BOTH endpoints: debt==0, netBalance==balance / ==creditsBalance;
- wallet↔policy consistency for one user/snapshot: same debt AND same netBalance;
- AGENT_DEBT_RECONCILE_ENABLED=false: a would-be-debt scenario leaves debt==0 in BOTH;
- backward-compat: balance/creditsBalance present, not renamed, int, >=0 under any debt;
- semantic unity: client /v1/wallet debt == admin GET /v1/admin/wallet/{userId} debt.

Debt is created through the EXISTING accrual path (agent-run consume with amount>balance under
AGENT_DEBT_RECONCILE_ENABLED, ADR-051 §2) — the same mechanism the wallet-ledger / ADR-051 suites
use — exercised via WalletService against the container, so no Docker/agent instance is touched.
The HTTP reads go through the real routers (client contour + admin contour).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.config import get_settings
from app.errors import InsufficientCreditsError
from app.wallet.service import WalletService
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_AGENT_META = {"source": "agent_run", "runId": "run-adr063", "usage": {}, "model": "m"}
_ADMIN_SECRET = "admin-secret-063-0123456789abcdef0123456789abcdef0123"
_ADMIN_HEADERS = {"X-Admin-Token": _ADMIN_SECRET}


# ----------------------------- helpers -----------------------------
async def _wallet_row(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> tuple[int, int]:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT balance, debt FROM wallets WHERE user_id=:u"), {"u": str(uid)}
            )
        ).one()
    return int(row.balance), int(row.debt)


async def _accrue_debt(
    maker: async_sessionmaker[AsyncSession],
    *,
    balance: int,
    amount: int,
    subscription: str | None = None,
    key: str = "run-adr063",
) -> uuid.UUID:
    """Seed a user with `balance` credits, then run an agent-path consume of `amount` (> balance)
    under AGENT_DEBT_RECONCILE_ENABLED → partial debit to 0 + debt = amount − balance (ADR-051).

    Returns the user id. The accrued debt equals `amount - balance` and `wallets.balance` becomes 0.
    """
    async with maker() as s:
        uid = await seed_user(s, subscription=subscription, balance=balance)
    async with maker() as s:
        await WalletService(s, AuditService(s)).consume(
            user_id=uid, amount=amount, idempotency_key=key, meta={**_AGENT_META, "runId": key}
        )
        await s.commit()
    return uid


@pytest.fixture
async def admin_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with the admin secret set (for the semantic-unity cross-check against the
    admin GET /v1/admin/wallet/{userId} contour). Shares the container DB with the client."""
    settings = get_settings()
    orig_secret = settings.admin_api_secret
    settings.admin_api_secret = _ADMIN_SECRET

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
    rate_limit.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]


# ============================================================================
# GET /v1/wallet at debt>0: debt==N, netBalance==balance−N (<0), balance>=0
# ============================================================================
@pytest.mark.asyncio
async def test_wallet_surfaces_debt_and_negative_net_balance(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _accrue_debt(db_sessionmaker, balance=5, amount=7, key="w-debt")
    # Sanity on the underlying row: balance driven to 0, debt captured the uncharged delta.
    assert await _wallet_row(db_sessionmaker, uid) == (0, 2)

    r = await client.get("/v1/wallet", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["debt"] == 2
    assert body["balance"] == 0  # existing field, >= 0 (CHECK balance>=0)
    assert body["balance"] >= 0
    assert body["netBalance"] == body["balance"] - body["debt"]  # == -2
    assert body["netBalance"] == -2
    assert body["netBalance"] < 0  # client renders it as a negative balance


# ============================================================================
# GET /v1/policy/effective at debt>0: debt==N, netBalance==creditsBalance−N (<0)
# ============================================================================
@pytest.mark.asyncio
async def test_policy_effective_surfaces_debt_and_negative_net_balance(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _accrue_debt(
        db_sessionmaker, balance=5, amount=8, subscription="active", key="p-debt"
    )
    assert await _wallet_row(db_sessionmaker, uid) == (0, 3)

    r = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["debt"] == 3
    assert body["creditsBalance"] == 0  # existing field, >= 0
    assert body["creditsBalance"] >= 0
    assert body["netBalance"] == body["creditsBalance"] - body["debt"]  # == -3
    assert body["netBalance"] == -3
    assert body["netBalance"] < 0


# ============================================================================
# debt == 0 in BOTH endpoints: netBalance == balance / == creditsBalance
# ============================================================================
@pytest.mark.asyncio
async def test_no_debt_net_balance_equals_balance_both_endpoints(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    assert await _wallet_row(db_sessionmaker, uid) == (10, 0)

    w = await client.get("/v1/wallet", headers=auth_headers(uid))
    assert w.status_code == 200, w.text
    wb = w.json()
    assert wb["debt"] == 0
    assert wb["balance"] == 10
    assert wb["netBalance"] == wb["balance"] == 10  # no debt → net == balance

    p = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert p.status_code == 200, p.text
    pb = p.json()
    assert pb["debt"] == 0
    assert pb["creditsBalance"] == 10
    assert pb["netBalance"] == pb["creditsBalance"] == 10  # no debt → net == creditsBalance


# ============================================================================
# wallet ↔ policy consistency for one user / snapshot: same debt AND same netBalance
# ============================================================================
@pytest.mark.asyncio
async def test_wallet_and_policy_debt_and_net_balance_consistent(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-063 §"Единый источник": both endpoints read the SAME wallets.debt column → identical
    # debt AND netBalance for one user (balance == creditsBalance == wallets.balance).
    uid = await _accrue_debt(
        db_sessionmaker, balance=4, amount=10, subscription="active", key="c-debt"
    )
    assert await _wallet_row(db_sessionmaker, uid) == (0, 6)

    w = (await client.get("/v1/wallet", headers=auth_headers(uid))).json()
    p = (await client.get("/v1/policy/effective", headers=auth_headers(uid))).json()
    assert w["debt"] == p["debt"] == 6
    assert w["netBalance"] == p["netBalance"] == -6
    assert w["balance"] == p["creditsBalance"] == 0  # same underlying wallets.balance


# ============================================================================
# AGENT_DEBT_RECONCILE_ENABLED=false: a would-be-debt scenario → debt==0 in BOTH
# ============================================================================
@pytest.mark.asyncio
async def test_flag_off_no_debt_surfaced_in_either_endpoint(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flag off → the agent shortfall takes the legacy full-rollback path (InsufficientCreditsError),
    # wallets.debt NEVER grows (ADR-051 write-side invariant). Read-side has no flag branch, so both
    # endpoints show debt==0 and netBalance==balance/creditsBalance.
    monkeypatch.setenv("AGENT_DEBT_RECONCILE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=3)
        async with db_sessionmaker() as s:
            with pytest.raises(InsufficientCreditsError):
                await WalletService(s, AuditService(s)).consume(
                    user_id=uid, amount=9, idempotency_key="flagoff-063", meta=_AGENT_META
                )
            await s.rollback()
        assert await _wallet_row(db_sessionmaker, uid) == (3, 0)  # no debt accrued

        w = (await client.get("/v1/wallet", headers=auth_headers(uid))).json()
        assert w["debt"] == 0
        assert w["balance"] == 3
        assert w["netBalance"] == w["balance"] == 3

        p = (await client.get("/v1/policy/effective", headers=auth_headers(uid))).json()
        assert p["debt"] == 0
        assert p["creditsBalance"] == 3
        assert p["netBalance"] == p["creditsBalance"] == 3
    finally:
        get_settings.cache_clear()


# ============================================================================
# Backward compatibility: balance/creditsBalance present, not renamed, int, >=0 under debt
# ============================================================================
@pytest.mark.asyncio
async def test_backward_compat_existing_fields_present_and_nonnegative(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # With a debt outstanding, the pre-ADR-063 fields keep their names, int type and >=0 value
    # (they are NOT overwritten by netBalance). Only additive keys appear alongside them.
    uid = await _accrue_debt(
        db_sessionmaker, balance=2, amount=9, subscription="active", key="bc-debt"
    )
    db_balance, db_debt = await _wallet_row(db_sessionmaker, uid)
    assert (db_balance, db_debt) == (0, 7)

    w = (await client.get("/v1/wallet", headers=auth_headers(uid))).json()
    assert "balance" in w and isinstance(w["balance"], int) and w["balance"] >= 0
    assert w["balance"] == db_balance  # unchanged semantics: still wallets.balance
    assert w["balance"] != w["netBalance"]  # existing field is NOT the signed net value
    assert set(w) == {"balance", "debt", "netBalance", "lastTransactions"}  # only additive keys

    p = (await client.get("/v1/policy/effective", headers=auth_headers(uid))).json()
    assert "creditsBalance" in p and isinstance(p["creditsBalance"], int)
    assert p["creditsBalance"] >= 0
    assert p["creditsBalance"] == db_balance  # unchanged: value of wallets.balance
    assert p["creditsBalance"] != p["netBalance"]
    # Pre-ADR-063 policy keys are all still present and not renamed.
    assert {
        "isSubscribed",
        "trialRemaining",
        "creditsBalance",
        "byokEnabled",
        "canGenerateCreditsMode",
        "canGenerateByokMode",
        "reasons",
    } <= set(p)


# ============================================================================
# Semantic unity: client /v1/wallet debt == admin GET /v1/admin/wallet/{userId} debt
# ============================================================================
@pytest.mark.asyncio
async def test_client_debt_equals_admin_wallet_debt(
    client: AsyncClient,
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # ADR-063 §4 / ADR-061: identical semantics — the client field and the admin field read the
    # SAME wallets.debt column, so both report the same value for one user.
    uid = await _accrue_debt(db_sessionmaker, balance=5, amount=9, key="unity-debt")
    assert await _wallet_row(db_sessionmaker, uid) == (0, 4)

    client_debt = (await client.get("/v1/wallet", headers=auth_headers(uid))).json()["debt"]
    admin_r = await admin_client.get(f"/v1/admin/wallet/{uid}", headers=_ADMIN_HEADERS)
    assert admin_r.status_code == 200, admin_r.text
    admin_debt = admin_r.json()["debt"]
    assert client_debt == admin_debt == 4
