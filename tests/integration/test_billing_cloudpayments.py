"""Integration: CloudPayments checkout, webhook and GET /v1/tokens/products (ADR-068)."""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

_API_TOKEN = "broadapps-outgoing-bearer-secret"
_APP_ID = "481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d"
_TOKEN_PRODUCT = "100_tokens_9.99"
_SUB_PRODUCT = "week_6.99_nottrial"


@pytest.fixture
async def cp_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", _API_TOKEN)
    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", _APP_ID)
    monkeypatch.setenv("TOKEN_PRODUCTS", json.dumps({_TOKEN_PRODUCT: 100}))
    monkeypatch.setenv("CLOUDPAYMENTS_PRODUCT_TOKENS", json.dumps({_SUB_PRODUCT: 1000}))
    get_settings.cache_clear()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _allow(**_kwargs: Any) -> bool:
        return True

    orig_other = rate_limit.enforce_other_limits
    orig_wh = rate_limit.enforce_cloudpayments_webhook_limits
    rate_limit.enforce_other_limits = _allow  # type: ignore[assignment]
    rate_limit.enforce_cloudpayments_webhook_limits = _allow  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    rate_limit.enforce_cloudpayments_webhook_limits = orig_wh  # type: ignore[assignment]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_checkout_requires_auth(cp_client: AsyncClient) -> None:
    r = await cp_client.post(
        "/v1/billing/cloudpayments/checkout",
        json={"productId": _SUB_PRODUCT, "customerEmail": "a@b.c"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_checkout_unknown_product_422(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await cp_client.post(
        "/v1/billing/cloudpayments/checkout",
        headers=auth_headers(uid),
        json={"productId": "unknown_sku", "customerEmail": "a@b.c"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_checkout_creates_link(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {
                "payment_id": "pay-1",
                "payment_url": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=1",
                "status": "pending",
                "expires_at": None,
            }

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, files: Any = None, headers: Any = None) -> _Resp:
            captured["url"] = url
            captured["files"] = files
            captured["headers"] = headers
            return _Resp()

        async def get(self, *args: Any, **kwargs: Any) -> _Resp:
            return _Resp()

    monkeypatch.setattr("app.billing_cloudpayments.checkout.httpx.AsyncClient", _Client)

    r = await cp_client.post(
        "/v1/billing/cloudpayments/checkout",
        headers=auth_headers(uid),
        json={"productId": _SUB_PRODUCT, "customerEmail": "user@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paymentUrl"].startswith("https://yoomoney.ru/")
    assert body["paymentId"] == "pay-1"
    assert captured["files"]["user_id"] == (None, str(uid))
    assert captured["files"]["product_id"] == (None, _SUB_PRODUCT)
    assert "Bearer " in captured["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_checkout_unconfigured_503(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app import deps
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "")
    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", "")
    get_settings.cache_clear()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/billing/cloudpayments/checkout",
            headers=auth_headers(uid),
            json={"productId": _SUB_PRODUCT},
        )
    get_settings.cache_clear()
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "cloudpayments_checkout_not_configured"


@pytest.mark.asyncio
async def test_webhook_empty_body_200(cp_client: AsyncClient) -> None:
    r = await cp_client.post("/v1/billing/cloudpayments/webhook", content=b"")
    assert r.status_code == 200
    assert r.json() == {"code": 0}


@pytest.mark.asyncio
async def test_webhook_credits_subscription(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    paid_at = datetime.datetime.now(tz=datetime.UTC).isoformat()

    async def _list(self: Any, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {
                "payment_id": "pay-live-1",
                "status": "succeeded",
                "paid_at": paid_at,
                "product": {"code": _SUB_PRODUCT, "payment_type": "subscription"},
            }
        ]

    monkeypatch.setattr(
        "app.billing_cloudpayments.verify.CloudPaymentsVerifyClient.list_payments",
        _list,
    )

    r = await cp_client.post(
        "/v1/billing/cloudpayments/webhook",
        json={
            "TransactionId": "txn-1",
            "AccountId": str(uid),
            "Status": "Completed",
            "OperationType": "Payment",
            "Data": json.dumps({"product_id": _SUB_PRODUCT, "user_id": str(uid)}),
        },
    )
    assert r.status_code == 200
    assert r.json() == {"code": 0}

    async with db_sessionmaker() as s:
        balance = await s.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )
        status = await s.scalar(
            text("SELECT status FROM subscriptions WHERE user_id = :uid"), {"uid": str(uid)}
        )
        events = await s.scalar(text("SELECT count(*) FROM cloudpayments_webhook_events"))
    assert balance == 1000
    assert status == "active"
    assert events == 1


@pytest.mark.asyncio
async def test_products_live_catalog(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    async def _list(self: Any) -> list[dict[str, Any]]:
        return [
            {
                "code": _SUB_PRODUCT,
                "name": "Недельная подписка",
                "payment_type": "subscription",
                "status": "active",
                "price_amount": "699.00",
                "price_currency": "RUB",
                "subscription_interval_unit": "week",
                "is_special_offer": True,
            },
            {
                "code": _TOKEN_PRODUCT,
                "name": "100 токенов",
                "payment_type": "one_time",
                "status": "active",
                "price_amount": "99.00",
                "price_currency": "RUB",
            },
        ]

    monkeypatch.setattr(
        "app.billing_cloudpayments.checkout.CloudPaymentsCheckoutClient.list_products",
        _list,
    )
    r = await cp_client.get("/v1/tokens/products", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    products = {p["productId"]: p for p in r.json()["products"]}
    assert products[_SUB_PRODUCT]["kind"] == "subscription"
    assert products[_SUB_PRODUCT]["credits"] is None
    assert products[_SUB_PRODUCT]["currency"] == "RUB"
    assert products[_SUB_PRODUCT]["price"] == 699
    assert products[_SUB_PRODUCT]["isSpecialOffer"] is True
    assert products[_TOKEN_PRODUCT]["credits"] == 100
    assert products[_TOKEN_PRODUCT]["kind"] == "tokens"


@pytest.mark.asyncio
async def test_presets_locale_query(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await cp_client.get("/v1/presets?locale=ru", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "ru"
    assert body["presets"][0]["title"] == "Планирование недели"


@pytest.mark.asyncio
async def test_presets_locale_unsupported_422(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await cp_client.get("/v1/presets?locale=de", headers=auth_headers(uid))
    assert r.status_code == 422
