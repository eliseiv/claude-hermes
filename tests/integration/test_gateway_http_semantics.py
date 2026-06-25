"""Integration: HTTP semantics at the gateway (AC-10, ADR-044).

401 (missing/invalid client key or missing/invalid X-User-Id), 413 (size), 422 (validation /
forged StoreKit), business-blocked → 200 with blockReason. The former 403 "body userId != JWT
sub" cross-check is GONE under ADR-044 §3 (require_owner is a no-op): the subject is X-User-Id by
definition, so a body userId differing from X-User-Id no longer 403s — the op runs on X-User-Id.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


@pytest.mark.asyncio
async def test_missing_token_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uuid.uuid4()),
            "projectId": "p",
            "message": "hi",
            "mode": "credits",
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_invalid_client_key_401(client: AsyncClient) -> None:
    # ADR-044: a wrong X-API-Key (even with a valid X-User-Id) is rejected constant-time → 401.
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
        headers={"X-API-Key": "totally-wrong-key", "X-User-Id": str(uuid.uuid4())},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_missing_user_id_401(client: AsyncClient) -> None:
    # ADR-044: a valid client key but NO X-User-Id has no subject → 401.
    from tests.conftest import _TEST_CLIENT_API_KEY

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
        headers={"X-API-Key": _TEST_CLIENT_API_KEY},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_userid_mismatch_no_longer_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-044 §3: require_owner is a no-op. A body userId differing from X-User-Id must NOT 403.
    # The op runs on the X-User-Id subject (uid, trial-unused) → business path, not a 403.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    other = uuid.uuid4()
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(other), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),  # X-User-Id = uid, body userId = other
    )
    assert r.status_code != 403


@pytest.mark.asyncio
async def test_body_validation_422(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    # empty message violates min_length=1
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_oversized_body_413(client: AsyncClient) -> None:
    # ADR-020: the transport size limit is now PER-ROUTE. The general ≤512KB cap still applies to
    # ordinary routes (here /v1/wallet) — an oversized body is rejected at the middleware with 413
    # before parsing. /v1/chat/run has its OWN raised limit (12MB for inline base64 attachments)
    # and is covered separately in test_chat_attachments.py; it must NOT be 413 at 600KB.
    big = b"x" * (600 * 1024)  # > size_limit_body (512KiB)
    uid = uuid.uuid4()
    r = await client.post(
        "/v1/wallet/me",
        content=big,
        headers={**auth_headers(uid), "content-type": "application/json"},
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_retired_subscription_sync_route_returns_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # TD-021 / ADR-029 revision: POST /v1/subscription/sync is RETIRED (Adapty is the single
    # subscription source). The route no longer exists → 404 (NOT 422/200). The forged-StoreKit-422
    # semantics now live exclusively on /v1/tokens/purchase (consumable IAP, ADR-015), covered by
    # test_token_purchase.py. A valid client-contour auth pair is supplied so the 404 is genuinely
    # "no such route" and not an auth 401.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.post(
        "/v1/subscription/sync",
        json={"userId": str(uid), "transaction": "anything"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_business_blocked_returns_200(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)  # trial used, no subscription → blocked
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "trial_used"


@pytest.mark.asyncio
async def test_security_headers_present(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Request-Id" in r.headers
