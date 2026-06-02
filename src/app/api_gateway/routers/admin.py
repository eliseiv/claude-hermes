"""Admin routes: /v1/admin/wallet/* (admin/02-api-contracts.md, ADR-009).

Authorization is the isolated X-Admin-Token via the ``require_admin`` dependency — NOT the
user JWT. A dedicated per-source-IP rate limit and an 8 KB body-size cap protect the surface.
Strict Pydantic schemas (extra='forbid'). The admin secret is never logged.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Request

from app.admin.service import AdminService
from app.api_gateway.auth import require_admin
from app.api_gateway.openapi_security import admin_scheme
from app.api_gateway.rate_limit import enforce_admin_limits
from app.config import get_settings
from app.deps import client_ip, get_admin_service
from app.errors import PayloadTooLargeError, RateLimitedError
from app.schemas.admin import (
    AdminGrantRequest,
    AdminGrantResponse,
    AdminLedgerTxView,
    AdminWalletResponse,
)

# require_admin guards every route here; the user JWT is not an authorization factor (ADR-009).
# admin_scheme (auto_error=False) only reflects the adminToken security scheme into OpenAPI so
# the Swagger UI lock/Authorize works — it does NOT perform the check (that stays in require_admin).
router = APIRouter(
    prefix="/v1/admin",
    tags=["Admin"],
    dependencies=[Depends(require_admin), Depends(admin_scheme)],
)


def _enforce_admin_body_size(request: Request) -> None:
    """Reject admin bodies over the stricter admin cap (<= 8 KB, ADR-009 §6) → 413."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > get_settings().admin_size_limit_body:
                raise PayloadTooLargeError("admin request body exceeds limit")
        except ValueError:
            pass


async def _enforce_admin_rate_limit(request: Request) -> None:
    if not await enforce_admin_limits(ip=client_ip(request)):
        raise RateLimitedError("admin rate limit exceeded")


@router.post(
    "/wallet/grant",
    response_model=AdminGrantResponse,
    summary="Начислить кредиты пользователю",
    description=(
        "Начисляет кредиты пользователю (саппорт/компенсация). Авторизация — заголовок "
        "`X-Admin-Token`. Идемпотентно по `idempotencyKey`: повтор с тем же payload не "
        "начислит дважды (`idempotentReplay=true`); тот же ключ с другим `amount` — `409`. "
        "Несуществующий `userId` — `404 user_not_found` (admin не создаёт пользователей)."
    ),
)
async def admin_wallet_grant(
    request: Request,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    body: Annotated[AdminGrantRequest, Body()],
) -> AdminGrantResponse:
    _enforce_admin_body_size(request)
    await _enforce_admin_rate_limit(request)
    result = await admin.grant(
        user_id=body.userId,
        amount=body.amount,
        idempotency_key=body.idempotencyKey,
        reason=body.reason,
    )
    return AdminGrantResponse(
        newBalance=result.new_balance,
        ledgerTxId=result.ledger_tx_id,
        idempotentReplay=result.idempotent_replay,
    )


@router.get(
    "/wallet/{userId}",
    response_model=AdminWalletResponse,
    summary="Просмотреть кошелёк пользователя",
    description=(
        "Read-only просмотр баланса и последних транзакций для саппорта. Авторизация — "
        "`X-Admin-Token`. Несуществующий `userId` — `404 user_not_found`."
    ),
)
async def admin_wallet_view(
    request: Request,
    admin: Annotated[AdminService, Depends(get_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="userId")],
) -> AdminWalletResponse:
    await _enforce_admin_rate_limit(request)
    last_n = get_settings().wallet_last_transactions
    view = await admin.get_wallet_view(user_id, last_n)
    return AdminWalletResponse(
        userId=view.user_id,
        balance=view.balance,
        lastTransactions=[
            AdminLedgerTxView(
                id=tx.id,
                type=tx.type,
                amount=tx.amount,
                createdAt=tx.created_at,
                meta=tx.meta,
            )
            for tx in view.last_transactions
        ],
    )
