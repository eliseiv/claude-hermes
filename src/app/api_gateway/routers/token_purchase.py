"""Token-purchase routes: POST /v1/tokens/purchase, GET /v1/tokens/products (ADR-015, ADR-068)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.api_gateway.rate_limit import enforce_other_limits
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.config import get_settings
from app.deps import (
    CurrentUser,
    get_cloudpayments_checkout_client,
    get_token_purchase_service,
    require_owner,
)
from app.errors import RateLimitedError
from app.schemas.token_purchase import (
    TokenProduct,
    TokenProductsResponse,
    TokenPurchaseRequest,
    TokenPurchaseResponse,
)
from app.token_purchase.service import TokenPurchaseService

router = APIRouter(prefix="/v1/tokens", tags=["Tokens"])


@router.post(
    "/purchase",
    response_model=TokenPurchaseResponse,
    summary="Купить пакет токенов",
    description=(
        "Пришлите подписанную StoreKit-транзакцию в поле `transaction`. Начисляет кредиты по "
        "`productId`. Повторная отправка той же транзакции не начисляет дважды "
        "(`creditsAdded=0`). Неизвестный `productId` или поддельная транзакция — `422`. "
        "Требует активной подписки, иначе `403 {code: subscription_required}`."
    ),
)
async def purchase_tokens(
    body: TokenPurchaseRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[TokenPurchaseService, Depends(get_token_purchase_service)],
) -> TokenPurchaseResponse:
    require_owner(body.userId, current)
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    result = await service.purchase(current.user_id, body.transaction)
    return TokenPurchaseResponse(
        creditsAdded=result.credits_added,
        newBalance=result.new_balance,
        transactionId=result.transaction_id,
    )


@router.get(
    "/products",
    response_model=TokenProductsResponse,
    summary="Каталог пакетов токенов",
    description=(
        "Возвращает каталог продуктов для пейволла: живой каталог RU-оплаты, иначе статичный "
        "`PRODUCTS_CATALOG`, иначе пакеты из `TOKEN_PRODUCTS`."
    ),
)
async def list_token_products(
    current: CurrentUser,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
) -> TokenProductsResponse:
    settings = get_settings()
    data = await client.list_products()
    if data:
        token_products = settings.token_products()
        minor = settings.token_products_price_minor_units
        live = [
            p
            for p in (_from_broadapps(x, token_products, minor_units=minor) for x in data)
            if p is not None
        ]
        if live:
            return _catalog_response(live)
    catalog = settings.products_catalog()
    if catalog:
        items: list[TokenProduct] = []
        for raw in catalog:
            try:
                items.append(TokenProduct.model_validate(raw))
            except ValidationError:
                continue
        if items:
            return _catalog_response(items)
    return _catalog_response(
        [
            TokenProduct(productId=product_id, credits=credits)
            for product_id, credits in settings.token_products().items()
        ]
    )


def _catalog_response(products: list[TokenProduct]) -> TokenProductsResponse:
    marked = get_settings().token_products_default()
    if marked:
        products = [
            p.model_copy(update={"isDefault": True}) if p.productId in marked else p
            for p in products
        ]
    return TokenProductsResponse(products=products)


def _from_broadapps(
    item: Any, token_products: dict[str, int], *, minor_units: bool = False
) -> TokenProduct | None:
    """Map one broadapps product dict to a TokenProduct; skip inactive / malformed items."""
    if not isinstance(item, dict):
        return None
    code = item.get("code")
    if not isinstance(code, str) or not code:
        return None
    if item.get("status") not in (None, "active"):
        return None
    is_sub = item.get("payment_type") == "subscription"
    price: int | None = None
    amount = item.get("price_amount")
    if isinstance(amount, str | int | float):
        try:
            price = round(float(amount) * 100) if minor_units else int(float(amount))
        except (TypeError, ValueError):
            price = None
    special = item.get("is_special_offer")
    period = item.get("subscription_interval_unit")
    currency = item.get("price_currency")
    name = item.get("name")
    return TokenProduct(
        productId=code,
        title=name if isinstance(name, str) else None,
        kind="subscription" if is_sub else "tokens",
        period=period if isinstance(period, str) else None,
        price=price,
        currency=currency if isinstance(currency, str) else None,
        credits=None if is_sub else token_products.get(code),
        isSpecialOffer=special is True,
    )
