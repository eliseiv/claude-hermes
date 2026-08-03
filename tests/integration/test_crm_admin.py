"""Integration: CRM admin contract endpoints (/v1/admin/users, broad-crm v1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, seed_user

_ADMIN_SECRET = "crm-admin-secret-integration-0123456789abcdef0123456789"
_ADMIN_HEADERS = {"X-Admin-Key": _ADMIN_SECRET}


@pytest.fixture
async def crm_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
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


@pytest.mark.asyncio
async def test_crm_users_list_and_detail(
    crm_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as session:
        uid = await seed_user(session)
    r = await crm_client.get("/v1/admin/users", headers=_ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert any(item["id"] == uid for item in body["items"])

    detail = await crm_client.get(f"/v1/admin/users/{uid}", headers=_ADMIN_HEADERS)
    assert detail.status_code == 200
    d = detail.json()
    assert d["id"] == uid
    assert d["balance"]["tokens"] >= 0
    assert d["revenue"] is None
    assert d["media_stats"] is None


@pytest.mark.asyncio
async def test_crm_tokens_and_subscription(
    crm_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as session:
        uid = await seed_user(session)
    grant = await crm_client.post(
        f"/v1/admin/users/{uid}/tokens",
        headers=_ADMIN_HEADERS,
        json={"amount": 50},
    )
    assert grant.status_code == 200
    assert grant.json()["tokens"] >= 50

    sub = await crm_client.post(
        f"/v1/admin/users/{uid}/subscription",
        headers=_ADMIN_HEADERS,
        json={
            "product_id": "pro_monthly",
            "expires_in_days": 30,
            "grant_id": "crm-grant-1",
        },
    )
    assert sub.status_code == 200
    s = sub.json()
    assert s["subscription_active"] is True
    assert s["applied"] is True

    replay = await crm_client.post(
        f"/v1/admin/users/{uid}/subscription",
        headers=_ADMIN_HEADERS,
        json={
            "product_id": "pro_monthly",
            "expires_in_days": 30,
            "grant_id": "crm-grant-1",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["applied"] is False


@pytest.mark.asyncio
async def test_crm_stats_products_empty_requests(
    crm_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as session:
        uid = await seed_user(session)
    stats = await crm_client.get("/v1/admin/stats", headers=_ADMIN_HEADERS)
    assert stats.status_code == 200
    assert stats.json()["users_total"] >= 1

    products = await crm_client.get("/v1/admin/products", headers=_ADMIN_HEADERS)
    assert products.status_code == 200
    assert "items" in products.json()

    payments = await crm_client.get(f"/v1/admin/users/{uid}/payments", headers=_ADMIN_HEADERS)
    assert payments.status_code == 200

    requests = await crm_client.get(f"/v1/admin/users/{uid}/requests", headers=_ADMIN_HEADERS)
    assert requests.status_code == 200
    assert "items" in requests.json()


@pytest.mark.asyncio
async def test_crm_missing_admin_key_403(crm_client: AsyncClient) -> None:
    r = await crm_client.get("/v1/admin/users")
    assert r.status_code == 403
    assert "detail" in r.json()


@pytest.mark.asyncio
async def test_crm_user_not_found_404(crm_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    r = await crm_client.get(f"/v1/admin/users/{missing}", headers=_ADMIN_HEADERS)
    assert r.status_code == 404
    assert "detail" in r.json()
