"""Integration: admin subscription/credits grant (ADR-048, ADR-050). Real PostgreSQL.

Covers the full follow_up_for_qa for ADR-048:
- A. POST /v1/admin/subscription/grant: upsert active, idempotency (durable vs later-writer-wins),
  grantCredits, 404/422 validation, atomicity, policy-gate, security, metric + audit.
- B. POST /v1/admin/credits/grant + /v1/admin/wallet/grant alias parity.
- C13. audit admin_subscription_grant / admin_grant carry the REAL idempotencyKey after redaction.

Each enumerated contour is asserted by a DIRECT test (see module-level coverage map in the QA
report). later-writer-wins (TD-030) is asserted as the ACCEPTED behaviour (overwrite, NOT 409).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_ADMIN_SECRET = "admin-secret-subgrant-0123456789abcdef0123456789abcd"
_ADMIN_PREV = "admin-secret-subgrant-prev-0123456789abcdef0123456789"
_ADMIN_HEADERS = {"X-Admin-Token": _ADMIN_SECRET}


def _future(days: int = 30) -> str:
    return (datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=days)).isoformat()


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


async def _sub_row(maker: async_sessionmaker[AsyncSession], uid: str) -> dict[str, Any] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, plan, expires_at, updated_at "
                    "FROM subscriptions WHERE user_id=:u"
                ),
                {"u": uid},
            )
        ).first()
    return None if row is None else dict(row._mapping)


async def _balance(maker: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with maker() as s:
        row = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": uid})
        return int(row) if row is not None else 0


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: str) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": uid}) or 0)


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
# A1. upsert active (new row + existing row); response shape
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_creates_active_subscription_for_new_row(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)  # no subscription row yet
    exp = _future()
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": exp,
            "idempotencyKey": "sg-new",
            "reason": "support",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["plan"] == "pro_monthly"
    assert body["idempotentReplay"] is False
    assert body["creditsGranted"] is None  # grantCredits defaults false
    assert body["ledgerTxId"] is None
    row = await _sub_row(db_sessionmaker, str(uid))
    assert row is not None
    assert row["status"] == "active"
    assert row["plan"] == "pro_monthly"


@pytest.mark.asyncio
async def test_subgrant_upserts_existing_subscription_row(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        # Pre-existing expired subscription on a different plan.
        uid = await seed_user(s, subscription="expired", expires_in_hours=-5)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_yearly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-existing",
            "reason": "compensation",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    row = await _sub_row(db_sessionmaker, str(uid))
    assert row is not None
    assert row["status"] == "active"
    assert row["plan"] == "pro_yearly"


# ============================================================================
# A2. pure replay (grantCredits=false): idempotentReplay=true, updated_at NOT bumped (no-op)
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_pure_replay_is_noop_updated_at_unchanged(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    payload = {
        "userId": str(uid),
        "plan": "pro_monthly",
        "expiresAt": _future(),
        "idempotencyKey": "sg-replay",
        "reason": "x",
    }
    r1 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["idempotentReplay"] is False
    row1 = await _sub_row(db_sessionmaker, str(uid))
    assert row1 is not None
    updated_before = row1["updated_at"]

    r2 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotentReplay"] is True
    row2 = await _sub_row(db_sessionmaker, str(uid))
    assert row2 is not None
    # No-op upsert on pure replay: updated_at must NOT be bumped (ADR-048 §2 idempotency).
    assert row2["updated_at"] == updated_before


# ============================================================================
# A3. grantCredits=true: grants once, replay does not double-credit, durable 409 on conflict
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_grant_credits_true_credits_once(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    payload = {
        "userId": str(uid),
        "plan": "pro_monthly",
        "expiresAt": _future(),
        "idempotencyKey": "sg-credit",
        "reason": "x",
        "grantCredits": True,
    }
    r1 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    expected = get_settings().subscription_credits_per_period
    assert b1["creditsGranted"] == expected
    assert b1["ledgerTxId"] is not None
    assert b1["idempotentReplay"] is False
    assert await _balance(db_sessionmaker, str(uid)) == expected

    # Ledger key is admin-sub-grant:{idempotencyKey} — exactly one credit row.
    credit_rows = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'",
        str(uid),
    )
    assert credit_rows == 1
    async with db_sessionmaker() as s:
        key = await s.scalar(
            text(
                "SELECT idempotency_key FROM ledger_transactions "
                "WHERE user_id=:u AND type='credit'"
            ),
            {"u": str(uid)},
        )
    assert key == "admin-sub-grant:sg-credit"

    # Replay with same payload: ledger idempotent_replay, no double credit.
    r2 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["ledgerTxId"] == b1["ledgerTxId"]
    assert r2.json()["idempotentReplay"] is True
    assert await _balance(db_sessionmaker, str(uid)) == expected  # still once


@pytest.mark.asyncio
async def test_subgrant_grant_credits_durable_409_on_different_credit_amount(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Durable anchor = ledger_transactions.idempotency_key. The same admin idempotencyKey reused
    # with a credit grant whose ledger key already exists but a DIFFERENT recorded credit amount
    # surfaces as 409 from WalletService.grant. We force the conflicting prior row directly to make
    # the "different amount, same ledger key" collision unambiguous.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
        await s.execute(
            text(
                "INSERT INTO wallets (user_id, balance) VALUES (:u, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"u": str(uid)},
        )
        await s.execute(
            text(
                "INSERT INTO ledger_transactions "
                "(id, user_id, type, amount, idempotency_key, meta) "
                "VALUES (:id, :u, 'credit', :amt, :k, '{}'::jsonb)"
            ),
            {
                "id": str(uuid.uuid4()),
                "u": str(uid),
                "amt": 7,  # different from SUBSCRIPTION_CREDITS_PER_PERIOD
                "k": "admin-sub-grant:sg-conflict",
            },
        )
        await s.commit()

    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-conflict",
            "reason": "x",
            "grantCredits": True,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text


# ============================================================================
# A4. grantCredits=false default: no ledger; later-writer-wins (TD-030) overwrite, NOT 409
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_no_credits_default_does_not_touch_ledger(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-noc",
            "reason": "x",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["creditsGranted"] is None
    assert body["ledgerTxId"] is None
    assert await _balance(db_sessionmaker, str(uid)) == 0
    ledger = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u",
        str(uid),
    )
    assert ledger == 0


@pytest.mark.asyncio
async def test_subgrant_grant_credits_false_durable_409_on_different_payload(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # TD-030 CLOSED by ADR-052: the durable subscription_grant_events anchor (UNIQUE
    # (user_id, idempotency_key) + payload_hash) makes a strict 409 reachable for BOTH grantCredits
    # paths — INCLUDING grantCredits=false, where there is NO ledger row. The SAME idempotencyKey
    # carrying a DIFFERENT plan/expiresAt now → strict 409 WITHOUT mutation (replaces the former
    # later-writer-wins overwrite).
    #
    # Masking-regression guard (CU rule): this test REPLACES the former later-writer-wins test,
    # which asserted the OPPOSITE (200 overwrite)
    # and would now silently pass-or-fail under the new behaviour. The invariant being verified is
    # the NEW one (409, no mutation), asserted directly.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    exp1 = _future(30)
    r1 = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": exp1,
            "idempotencyKey": "sg-lww",
            "reason": "first",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r1.status_code == 200, r1.text
    row1 = await _sub_row(db_sessionmaker, str(uid))
    assert row1 is not None and row1["plan"] == "pro_monthly"
    updated_before = row1["updated_at"]

    exp2 = _future(90)
    r2 = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_yearly",  # different plan, SAME idempotencyKey
            "expiresAt": exp2,
            "idempotencyKey": "sg-lww",
            "reason": "second",
        },
        headers=_ADMIN_HEADERS,
    )
    # ADR-052: strict 409 on same key + different payload (grantCredits=false path).
    assert r2.status_code == 409, r2.text
    # NO mutation: the subscription still carries the FIRST payload, updated_at unchanged.
    row2 = await _sub_row(db_sessionmaker, str(uid))
    assert row2 is not None
    assert row2["plan"] == "pro_monthly"
    assert row2["updated_at"] == updated_before
    # Only the first grant was audited (the conflicting second was rejected before any audit).
    payloads = await _audit_payloads(db_sessionmaker, str(uid), "admin_subscription_grant")
    assert len(payloads) == 1
    assert payloads[0]["plan"] == "pro_monthly"


@pytest.mark.asyncio
async def test_subgrant_grant_credits_false_pure_replay_idempotent(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-052: grantCredits=false with the SAME idempotencyKey AND the SAME payload → idempotent
    # replay against the durable anchor (idempotentReplay=true), no second audit, no mutation.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    payload = {
        "userId": str(uid),
        "plan": "pro_monthly",
        "expiresAt": _future(45),
        "idempotencyKey": "sg-noc-replay",
        "reason": "x",
        # grantCredits omitted → false: this is the path WITHOUT a ledger anchor (TD-030 case).
    }
    r1 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["idempotentReplay"] is False
    row1 = await _sub_row(db_sessionmaker, str(uid))
    assert row1 is not None
    updated_before = row1["updated_at"]

    r2 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotentReplay"] is True  # durable anchor replay (grantCredits=false)
    row2 = await _sub_row(db_sessionmaker, str(uid))
    assert row2 is not None
    assert row2["updated_at"] == updated_before  # no-op upsert
    # Exactly one audit row (replay does not re-audit).
    payloads = await _audit_payloads(db_sessionmaker, str(uid), "admin_subscription_grant")
    assert len(payloads) == 1


@pytest.mark.asyncio
async def test_subgrant_durable_event_row_records_ledger_tx_id_when_credits(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-052 §2: a new grantCredits=true operation upserts the subscription, grants credits AND
    # writes the credit ledger_tx_id back into subscription_grant_events.ledger_tx_id.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-event-tx",
            "reason": "x",
            "grantCredits": True,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    ledger_tx_id = r.json()["ledgerTxId"]
    assert ledger_tx_id is not None
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT payload_hash, grant_credits, ledger_tx_id "
                    "FROM subscription_grant_events "
                    "WHERE user_id=:u AND idempotency_key='sg-event-tx'"
                ),
                {"u": str(uid)},
            )
        ).one()
    assert row.grant_credits is True
    assert row.payload_hash  # non-empty sha256 of the canonical payload
    assert str(row.ledger_tx_id) == ledger_tx_id


# ============================================================================
# A5. 404 unknown user (no provisioning); 422 validation cases
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_unknown_user_404_no_provisioning(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    missing = uuid.uuid4()
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(missing),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-404",
            "reason": "x",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "user_not_found"
    async with db_sessionmaker() as s:
        users = await s.scalar(text("SELECT count(*) FROM users WHERE id=:u"), {"u": str(missing)})
        subs = await s.scalar(
            text("SELECT count(*) FROM subscriptions WHERE user_id=:u"), {"u": str(missing)}
        )
    assert int(users) == 0  # users NOT provisioned
    assert int(subs) == 0


@pytest.mark.asyncio
async def test_subgrant_expires_in_past_422(admin_client: AsyncClient) -> None:
    past = (datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=1)).isoformat()
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uuid.uuid4()),
            "plan": "pro_monthly",
            "expiresAt": past,
            "idempotencyKey": "sg-past",
            "reason": "x",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_subgrant_empty_reason_422(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uuid.uuid4()),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-er",
            "reason": "",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_subgrant_empty_plan_422(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uuid.uuid4()),
            "plan": "",
            "expiresAt": _future(),
            "idempotencyKey": "sg-ep",
            "reason": "x",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_subgrant_extra_field_422(admin_client: AsyncClient) -> None:
    # extra='forbid' — an unexpected field (e.g. a leaked secret) is a 422.
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uuid.uuid4()),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-extra",
            "reason": "x",
            "adminToken": "leak",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 422, r.text


# ============================================================================
# A6. Atomicity: a failing credit grant rolls back the subscription upsert too
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_atomicity_credit_failure_rolls_back_subscription(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Pre-seed a conflicting ledger row (same admin-sub-grant key, different amount) so the credit
    # grant raises ConflictError → 409. The subscriptions upsert in the SAME transaction must be
    # rolled back: NO subscription row may be left behind.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)  # no subscription row
        await s.execute(
            text(
                "INSERT INTO wallets (user_id, balance) VALUES (:u, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"u": str(uid)},
        )
        await s.execute(
            text(
                "INSERT INTO ledger_transactions "
                "(id, user_id, type, amount, idempotency_key, meta) "
                "VALUES (:id, :u, 'credit', :amt, :k, '{}'::jsonb)"
            ),
            {
                "id": str(uuid.uuid4()),
                "u": str(uid),
                "amt": 13,  # different from SUBSCRIPTION_CREDITS_PER_PERIOD
                "k": "admin-sub-grant:sg-atomic",
            },
        )
        await s.commit()

    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-atomic",
            "reason": "x",
            "grantCredits": True,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 409, r.text
    # Atomic rollback: the subscription upsert that ran before the credit grant must NOT persist.
    row = await _sub_row(db_sessionmaker, str(uid))
    assert row is None, f"subscription leaked despite credit-grant failure: {row}"


# ============================================================================
# A7. policy-gate: after grant, the user is policy-allowed (asserted at the policy layer — cheap)
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_makes_user_pass_policy_gate(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-048 §2 / ADR-002: after a subscription grant the user must clear the policy-gate that
    # /v1/agent/run consults. Asserting at the policy layer (load_policy_state/effective) is the
    # cheap, deterministic equivalent of an agent/run integration — same evaluate() path.
    #
    # Two invariants of the gate transition (ADR-002 credits-mode semantics): (1) an active
    # subscription flips is_subscribed and removes the subscription_required block; (2) the
    # credits-mode gate additionally needs a non-zero balance — exactly why the operator flow grants
    # the period credits (grantCredits=true). We assert the realistic "give a user access" flow:
    # after subscription+credits grant the user is policy-allowed for credits mode.
    from app.policy.engine import BlockReason
    from app.policy.loader import effective

    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)  # no subscription → blocked before grant
    # Sanity: before any grant the user is NOT subscribed and is blocked (trial-eligible only).
    async with db_sessionmaker() as s:
        before = await effective(s, uid)
    assert before.is_subscribed is False

    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-policy",
            "reason": "x",
            "grantCredits": True,  # operator flow: activate subscription AND fund the period
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        eff = await effective(s, uid)
    assert eff.is_subscribed is True  # subscription_required no longer blocks
    assert BlockReason.subscription_required not in eff.reasons
    assert eff.credits_balance == get_settings().subscription_credits_per_period
    assert eff.can_generate_credits_mode is True  # active subscription + credits clears the gate


# ============================================================================
# A8. Security: no admin token → 401; OpenAPI security == [{adminToken:[]}] (see also
#     tests/integration/test_api_documentation.py enumerated _ADMIN_PATHS for the direct assert)
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_no_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uuid.uuid4()),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-noauth",
            "reason": "x",
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_subgrant_user_jwt_does_not_authorize(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-jwt",
            "reason": "x",
        },
        headers=auth_headers(uid),  # client-contour creds, no X-Admin-Token
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_subgrant_openapi_security_is_admin_token_only(
    admin_client: AsyncClient,
) -> None:
    # Direct OpenAPI assert for the NEW route (task A8). Belt with the enumerated _ADMIN_PATHS test
    # in test_api_documentation.py.
    r = await admin_client.get("/openapi.json")
    assert r.status_code == 200, r.text
    schema = r.json()
    op = schema["paths"]["/v1/admin/subscription/grant"]["post"]
    assert op.get("security") == [{"adminToken": []}], op.get("security")


# ============================================================================
# A9. metric + audit fields
# ============================================================================
@pytest.mark.asyncio
async def test_subgrant_metric_success_incremented(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.observability.metrics import admin_subscription_grant_total

    before = admin_subscription_grant_total.labels(result="success")._value.get()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-metric",
            "reason": "x",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    after = admin_subscription_grant_total.labels(result="success")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_subgrant_metric_not_found_incremented(
    admin_client: AsyncClient,
) -> None:
    from app.observability.metrics import admin_subscription_grant_total

    before = admin_subscription_grant_total.labels(result="not_found")._value.get()
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uuid.uuid4()),
            "plan": "pro_monthly",
            "expiresAt": _future(),
            "idempotencyKey": "sg-metric-nf",
            "reason": "x",
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 404, r.text
    after = admin_subscription_grant_total.labels(result="not_found")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_subgrant_audit_records_all_fields_and_no_admin_secret(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    exp = _future()
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "plan": "pro_monthly",
            "expiresAt": exp,
            "idempotencyKey": "sg-audit",
            "reason": "support reason",
            "grantCredits": True,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    payloads = await _audit_payloads(db_sessionmaker, str(uid), "admin_subscription_grant")
    assert len(payloads) == 1
    p = payloads[0]
    assert p["actor"] == "admin"
    assert p["userId"] == str(uid)
    assert p["plan"] == "pro_monthly"
    assert p["reason"] == "support reason"
    assert p["grantCredits"] is True
    assert p["ledgerTxId"] is not None  # grantCredits=true → ledger tx recorded
    # C13: the audit carries the REAL idempotencyKey (NOT ***REDACTED***) after the redaction layer.
    assert p["idempotencyKey"] == "sg-audit"

    # No admin secret leaked into any audit payload for this user.
    async with db_sessionmaker() as s:
        rows = await s.scalars(
            text("SELECT payload::text FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
        )
        blob = " ".join(rows)
    assert _ADMIN_SECRET not in blob
    assert _ADMIN_PREV not in blob


# ============================================================================
# C13 (credits/grant side): admin_grant audit carries the REAL idempotencyKey after redaction
# ============================================================================
@pytest.mark.asyncio
async def test_credits_grant_audit_carries_real_idempotency_key(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/credits/grant",
        json={"userId": str(uid), "amount": 40, "idempotencyKey": "cg-key-77", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    payloads = await _audit_payloads(db_sessionmaker, str(uid), "admin_grant")
    assert len(payloads) == 1
    assert payloads[0]["idempotencyKey"] == "cg-key-77"  # NOT redacted (ADR-050)


# ============================================================================
# B10. credits/grant and wallet/grant (alias) — identical response/behaviour; idempotency; security
# ============================================================================
@pytest.mark.asyncio
async def test_credits_grant_canonical_path_grants(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    r = await admin_client.post(
        "/v1/admin/credits/grant",
        json={"userId": str(uid), "amount": 60, "idempotencyKey": "cg-1", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["newBalance"] == 60
    assert body["idempotentReplay"] is False
    assert await _balance(db_sessionmaker, str(uid)) == 60


@pytest.mark.asyncio
async def test_credits_grant_and_wallet_alias_identical_behaviour(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # The alias /v1/admin/wallet/grant and the canonical /v1/admin/credits/grant share the same
    # idempotency key-space and behaviour: hitting one then the alias with the same key is a replay
    # (no double credit), and the response shape is identical.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    payload = {"userId": str(uid), "amount": 35, "idempotencyKey": "cg-alias", "reason": "x"}
    r1 = await admin_client.post("/v1/admin/credits/grant", json=payload, headers=_ADMIN_HEADERS)
    r2 = await admin_client.post("/v1/admin/wallet/grant", json=payload, headers=_ADMIN_HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert set(r1.json().keys()) == set(r2.json().keys())  # identical response shape
    assert r1.json()["idempotentReplay"] is False
    assert r2.json()["idempotentReplay"] is True  # alias sees the same key → replay
    assert r1.json()["ledgerTxId"] == r2.json()["ledgerTxId"]
    assert await _balance(db_sessionmaker, str(uid)) == 35  # credited once across both paths


@pytest.mark.asyncio
async def test_credits_grant_no_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/credits/grant",
        json={"userId": str(uuid.uuid4()), "amount": 5, "idempotencyKey": "cg-na", "reason": "x"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_credits_grant_openapi_security_is_admin_token_only(
    admin_client: AsyncClient,
) -> None:
    r = await admin_client.get("/openapi.json")
    assert r.status_code == 200, r.text
    schema = r.json()
    op = schema["paths"]["/v1/admin/credits/grant"]["post"]
    assert op.get("security") == [{"adminToken": []}], op.get("security")
