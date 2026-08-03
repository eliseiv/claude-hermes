"""CRM admin routes — broad-crm universal contract v1 under /v1/admin."""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.admin.crm_service import (
    CrmAdminService,
    _subscription_active,
    _utc_iso,
    build_crm_user_list_item,
)
from app.api_gateway.auth import require_admin
from app.api_gateway.routers.admin import _enforce_admin_body_size, _enforce_admin_rate_limit
from app.deps import get_crm_admin_service
from app.schemas.crm_admin import (
    CrmBalanceBlock,
    CrmPaymentItem,
    CrmPaymentListResponse,
    CrmProductItem,
    CrmProductListResponse,
    CrmRequestItem,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmSubscriptionBlock,
    CrmSubscriptionSetRequest,
    CrmSubscriptionSetResponse,
    CrmTokensAdjustRequest,
    CrmTokensAdjustResponse,
    CrmUserDetailResponse,
    CrmUserListItem,
    CrmUserListResponse,
)

router = APIRouter(
    prefix="/v1/admin",
    tags=["Admin (CRM)"],
    dependencies=[Depends(require_admin)],
)


def _parse_optional_dt(value: str | None) -> datetime.datetime | None:
    if value is None:
        return None
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


@router.get("/users", response_model=CrmUserListResponse)
async def crm_list_users(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    limit: Annotated[int, Query(le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    is_paid: Annotated[bool | None, Query()] = None,
) -> CrmUserListResponse:
    await _enforce_admin_rate_limit(request)
    now = datetime.datetime.now(tz=datetime.UTC)
    total, rows = await crm.list_users(
        limit=limit,
        offset=offset,
        search=search,
        date_from=_parse_optional_dt(date_from),
        date_to=_parse_optional_dt(date_to),
        is_paid=is_paid,
    )
    items = [CrmUserListItem(**build_crm_user_list_item(row, now)) for row in rows]
    return CrmUserListResponse(total=total, items=items)


@router.get("/users/{user_id}", response_model=CrmUserDetailResponse)
async def crm_user_detail(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path()],
) -> CrmUserDetailResponse:
    await _enforce_admin_rate_limit(request)
    row = await crm.get_user(user_id)
    if row is None:
        from app.errors import UserNotFoundError

        raise UserNotFoundError("user not found")
    now = datetime.datetime.now(tz=datetime.UTC)
    credited, spent = await crm.ledger_totals(user_id)
    last_pay = await crm.last_payment_at(user_id)
    return CrmUserDetailResponse(
        id=str(row.user_id),
        external_id=None,
        registered_at=_utc_iso(row.registered_at) or "",
        balance=CrmBalanceBlock(
            tokens=float(row.balance),
            credited_total=float(credited),
            spent_total=float(spent),
        ),
        subscription=CrmSubscriptionBlock(
            plan_id=row.plan_id,
            plan_name=row.plan_id,
            price=None,
            active=_subscription_active(row.subscription_status, row.subscription_expires_at, now),
            expires_at=_utc_iso(row.subscription_expires_at),
            last_payment_at=_utc_iso(last_pay),
            last_payment_method=None,
        ),
        revenue=None,
        media_stats=None,
    )


@router.get("/users/{user_id}/payments", response_model=CrmPaymentListResponse)
async def crm_user_payments(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path()],
    limit: Annotated[int, Query(le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CrmPaymentListResponse:
    await _enforce_admin_rate_limit(request)
    total, rows = await crm.list_payments(user_id, limit, offset)
    items = [
        CrmPaymentItem(
            title=row.title,
            description=row.description,
            amount=row.amount,
            currency=row.currency,
            status=row.status,
            occurred_at=_utc_iso(row.occurred_at) or "",
        )
        for row in rows
    ]
    return CrmPaymentListResponse(total=total, items=items)


@router.get("/users/{user_id}/requests", response_model=CrmRequestListResponse)
async def crm_user_requests(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path()],
    limit: Annotated[int, Query(le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CrmRequestListResponse:
    await _enforce_admin_rate_limit(request)
    total, rows = await crm.list_requests(user_id, limit, offset)
    items = [
        CrmRequestItem(
            endpoint=row.endpoint,
            prompt_preview=row.prompt_preview,
            status_code=row.status_code,
            status=row.status,
            duration_sec=row.duration_sec,
            sent_at=_utc_iso(row.sent_at) or "",
        )
        for row in rows
    ]
    return CrmRequestListResponse(total=total, items=items)


@router.get("/stats", response_model=CrmStatsResponse)
async def crm_stats(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
) -> CrmStatsResponse:
    await _enforce_admin_rate_limit(request)
    users_total, paid_users = await crm.stats(
        date_from=_parse_optional_dt(date_from),
        date_to=_parse_optional_dt(date_to),
    )
    return CrmStatsResponse(
        users_total=users_total,
        paid_users=paid_users,
        payments_sum_usd=0.0,
    )


@router.get("/products", response_model=CrmProductListResponse)
async def crm_products(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
) -> CrmProductListResponse:
    await _enforce_admin_rate_limit(request)
    items = [
        CrmProductItem(product_id=pid, name=name, price=price, period=period)
        for pid, name, price, period in crm.list_products()
    ]
    return CrmProductListResponse(items=items)


@router.post("/users/{user_id}/tokens", response_model=CrmTokensAdjustResponse)
async def crm_adjust_tokens(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path()],
    body: CrmTokensAdjustRequest,
) -> CrmTokensAdjustResponse:
    _enforce_admin_body_size(request)
    await _enforce_admin_rate_limit(request)
    balance = await crm.adjust_tokens(user_id, body.amount)
    return CrmTokensAdjustResponse(id=str(user_id), tokens=float(balance))


@router.post("/users/{user_id}/subscription", response_model=CrmSubscriptionSetResponse)
async def crm_set_subscription(
    request: Request,
    crm: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path()],
    body: CrmSubscriptionSetRequest,
) -> CrmSubscriptionSetResponse:
    _enforce_admin_body_size(request)
    await _enforce_admin_rate_limit(request)
    outcome = await crm.set_subscription(
        user_id,
        body.product_id,
        body.expires_in_days,
        body.grant_id,
    )
    return CrmSubscriptionSetResponse(
        id=str(user_id),
        tokens=float(outcome.tokens),
        subscription_active=outcome.subscription_active,
        subscription_expires_at=_utc_iso(outcome.subscription_expires_at),
        applied=outcome.applied,
    )
