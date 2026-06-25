"""Integration: subscription path after the StoreKit /v1/subscription/sync RETIREMENT (TD-021).

ADR-029 revision / TD-021: ``POST /v1/subscription/sync`` (StoreKit JWS) is RETIRED — Adapty is the
single subscription source, so ``SubscriptionService`` keeps ONLY the admin-initiated grant
(ADR-048 §2) hardened with the durable idempotency anchor (ADR-052). The former
``SubscriptionService.sync`` + ``VerifiedTransaction`` flow no longer exists.

This file asserts the RETIREMENT invariant directly (the route is gone → 404). The positive
behaviour of the remaining ``SubscriptionService.admin_grant`` (upsert, durable idempotency,
grantCredits, 409 on payload conflict for BOTH grantCredits paths — ADR-052) is covered in
``tests/integration/test_admin_subscription_grant.py``. The Adapty webhook (the single live
subscription path) is covered in ``tests/integration/test_billing_adapty_webhook.py``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import _TEST_CLIENT_API_KEY, auth_headers, seed_user


# --- TD-021: POST /v1/subscription/sync is retired (route absent → 404) ------------------------
@pytest.mark.asyncio
async def test_subscription_sync_route_retired_returns_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Direct retirement assertion: with a VALID client-contour auth pair the response is 404
    # (no such route), NOT 200 (handled) and NOT 401 (auth). A valid auth pair rules out the 404
    # being an auth artefact — the route genuinely no longer exists.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.post(
        "/v1/subscription/sync",
        json={"userId": str(uid), "transaction": "jws.token.here"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_subscription_sync_retired_even_without_user_id_404(
    client: AsyncClient,
) -> None:
    # The route is gone for everyone: even with only the client key (no X-User-Id) it is a 404,
    # i.e. the absence of the route is not gated behind an auth check that could mask it.
    r = await client.post(
        "/v1/subscription/sync",
        json={"userId": str(uuid.uuid4()), "transaction": "jws.token"},
        headers={"X-API-Key": _TEST_CLIENT_API_KEY},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_tokens_purchase_route_still_exists(client: AsyncClient) -> None:
    # Complement (TD-021): retiring subscription/sync must NOT remove the consumable IAP route.
    # POST /v1/tokens/purchase still exists — an empty body yields 422 (validation), NOT 404.
    # The full token-purchase behaviour is covered in test_token_purchase.py.
    r = await client.post(
        "/v1/tokens/purchase",
        json={},
        headers={"X-API-Key": _TEST_CLIENT_API_KEY, "X-User-Id": str(uuid.uuid4())},
    )
    assert r.status_code != 404, r.text


def test_subscription_service_has_no_sync_method() -> None:
    # Code-level retirement guard: SubscriptionService no longer exposes a `sync` method (StoreKit
    # JWS verification path removed, TD-021). A regression that re-adds it without re-adding the
    # route would be caught upstream; this keeps the retirement explicit at the service surface.
    from app.subscription.service import SubscriptionService

    assert not hasattr(SubscriptionService, "sync")
