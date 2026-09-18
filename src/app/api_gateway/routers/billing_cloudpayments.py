"""RU payment routes under /v1/billing/cloudpayments (ADR-068)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api_gateway.rate_limit import enforce_cloudpayments_webhook_limits, enforce_other_limits
from app.billing_cloudpayments.auth import require_cloudpayments_webhook
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import Settings, get_settings
from app.deps import (
    CurrentUser,
    client_ip,
    get_cloudpayments_checkout_client,
    get_cloudpayments_webhook_service,
)
from app.errors import CloudPaymentsCheckoutNotConfiguredError, RateLimitedError
from app.schemas.billing_cloudpayments import (
    CloudPaymentsCheckoutRequest,
    CloudPaymentsCheckoutResponse,
    CloudPaymentsWebhookResponse,
)

router = APIRouter(prefix="/v1/billing/cloudpayments", tags=["Billing (CloudPayments)"])


@router.post(
    "/webhook",
    response_model=CloudPaymentsWebhookResponse,
    dependencies=[Depends(require_cloudpayments_webhook)],
    summary="Приём платежа RU (webhook)",
    description=(
        "Серверный вебхук платёжного агрегатора (вызывает агрегатор, не клиент). Публичный: "
        "событие лишь ТРИГГЕР — начисление выполняется только после подтверждения платежа через "
        "платёжный сервис. Тело читается сырым, без валидации схемы. Ответ всегда `200`, тело "
        '`{"code": 0}` (событие принято: платёж начислен либо проигнорирован). `429` — при частых '
        "вызовах с одного IP; `500` — если способ оплаты не сконфигурирован, при недоступности "
        "верификации или сбое БД (тогда агрегатор повторяет доставку)."
    ),
)
async def cloudpayments_webhook(
    request: Request,
    service: Annotated[CloudPaymentsWebhookService, Depends(get_cloudpayments_webhook_service)],
) -> JSONResponse:
    if not await enforce_cloudpayments_webhook_limits(ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")
    raw = await request.body()
    await service.handle(raw)
    return JSONResponse({"code": 0}, status_code=200)


@router.post(
    "/checkout",
    response_model=CloudPaymentsCheckoutResponse,
    summary="Создать ссылку на оплату (RU)",
    description=(
        "Создаёт платёжную ссылку для российской оплаты и возвращает `paymentUrl` — откройте его "
        "для оплаты. Требуется авторизация. Укажите `productId` и `customerEmail`. Доступно "
        "не на всех инсталляциях (`503`, если способ оплаты недоступен)."
    ),
)
async def cloudpayments_checkout(
    body: CloudPaymentsCheckoutRequest,
    current: CurrentUser,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CloudPaymentsCheckoutResponse:
    if not settings.cloudpayments_checkout_configured():
        raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    client.validate_product(body.productId)
    result = await client.create_payment_link(
        user_id=current.user_id,
        product_id=body.productId,
        customer_email=body.customerEmail,
    )
    return CloudPaymentsCheckoutResponse(
        paymentId=result.payment_id,
        paymentUrl=result.payment_url,
        status=result.status,
        expiresAt=result.expires_at,
    )
