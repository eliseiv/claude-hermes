"""CRM admin API schemas (broad-crm universal contract v1, 2026-07-23)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel


class CrmUserListItem(StrictModel):
    id: str
    external_id: str | None = None
    is_paid: bool
    payments_count: int
    renewals_count: int
    tokens: float
    subscription_active: bool
    subscription_expires_at: str | None = None
    plan_id: str | None = None
    registered_at: str


class CrmUserListResponse(StrictModel):
    total: int
    items: list[CrmUserListItem]


class CrmBalanceBlock(StrictModel):
    tokens: float
    credited_total: float | None = None
    spent_total: float | None = None


class CrmSubscriptionBlock(StrictModel):
    plan_id: str | None = None
    plan_name: str | None = None
    price: str | None = None
    active: bool
    expires_at: str | None = None
    last_payment_at: str | None = None
    last_payment_method: str | None = None


class CrmUserDetailResponse(StrictModel):
    id: str
    external_id: str | None = None
    registered_at: str
    balance: CrmBalanceBlock
    subscription: CrmSubscriptionBlock
    revenue: dict[str, object] | None = None
    media_stats: dict[str, object] | None = None


class CrmPaymentItem(StrictModel):
    title: str
    description: str | None = None
    amount: float
    currency: str
    status: Literal["success", "failed"]
    occurred_at: str


class CrmPaymentListResponse(StrictModel):
    total: int
    items: list[CrmPaymentItem]


class CrmRequestItem(StrictModel):
    endpoint: str
    prompt_preview: str | None = None
    status_code: int
    status: Literal["ok", "slow", "error"]
    duration_sec: float | None = None
    sent_at: str


class CrmRequestListResponse(StrictModel):
    total: int
    items: list[CrmRequestItem]


class CrmStatsResponse(StrictModel):
    users_total: int
    paid_users: int
    payments_sum_usd: float


class CrmProductItem(StrictModel):
    product_id: str
    name: str
    price: str | None = None
    period: str | None = None


class CrmProductListResponse(StrictModel):
    items: list[CrmProductItem]


class CrmTokensAdjustRequest(StrictModel):
    amount: int = Field(description="Positive = credit, negative = debit.")


class CrmTokensAdjustResponse(StrictModel):
    id: str
    tokens: float


class CrmSubscriptionSetRequest(StrictModel):
    product_id: str = Field(min_length=1, max_length=255)
    expires_in_days: int = Field(gt=0, le=3650)
    grant_id: str = Field(min_length=1, max_length=255)


class CrmSubscriptionSetResponse(StrictModel):
    id: str
    tokens: float
    subscription_active: bool
    subscription_expires_at: str | None = None
    applied: bool
