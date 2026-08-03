"""CRM admin service — broad-crm universal contract v1."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from app.admin.crm_repo import CrmAdminRepository, CrmPaymentRow, CrmRequestRow, CrmUserRow
from app.admin.service import AdminService
from app.config import Settings
from app.errors import BadRequestError, ConflictError, InsufficientCreditsError, UserNotFoundError


def _utc_iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def _subscription_active(
    status: str | None, expires_at: datetime.datetime | None, now: datetime.datetime
) -> bool:
    if status != "active":
        return False
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.UTC)
    return expires_at > now


@dataclass(frozen=True)
class CrmSubscriptionOutcome:
    tokens: int
    subscription_active: bool
    subscription_expires_at: datetime.datetime | None
    applied: bool


class CrmAdminService:
    def __init__(
        self,
        repo: CrmAdminRepository,
        admin: AdminService,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._admin = admin
        self._settings = settings

    def _known_product_ids(self) -> frozenset[str]:
        products = set(self._settings.adapty_product_tokens().keys())
        products.update(self._settings.token_products().keys())
        return frozenset(products)

    async def list_users(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
        is_paid: bool | None,
    ) -> tuple[int, list[CrmUserRow]]:
        total = await self._repo.count_users(
            search=search,
            date_from=date_from,
            date_to=date_to,
            is_paid=is_paid,
        )
        items = await self._repo.list_users(
            limit=limit,
            offset=offset,
            search=search,
            date_from=date_from,
            date_to=date_to,
            is_paid=is_paid,
        )
        return total, items

    async def get_user(self, user_id: uuid.UUID) -> CrmUserRow | None:
        return await self._repo.get_user_row(user_id)

    async def ledger_totals(self, user_id: uuid.UUID) -> tuple[int, int]:
        return await self._repo.ledger_totals(user_id)

    async def last_payment_at(self, user_id: uuid.UUID) -> datetime.datetime | None:
        return await self._repo.last_payment_at(user_id)

    async def list_payments(
        self, user_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[int, list[CrmPaymentRow]]:
        if not await self._repo.get_user_row(user_id):
            raise UserNotFoundError("user not found")
        total = await self._repo.count_payments(user_id)
        items = await self._repo.list_payments(user_id, limit, offset)
        return total, items

    async def list_requests(
        self, user_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[int, list[CrmRequestRow]]:
        if not await self._repo.get_user_row(user_id):
            raise UserNotFoundError("user not found")
        total = await self._repo.count_requests(user_id)
        items = await self._repo.list_requests(user_id, limit, offset)
        return total, items

    async def stats(
        self,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
    ) -> tuple[int, int]:
        return await self._repo.stats(date_from=date_from, date_to=date_to)

    def list_products(self) -> list[tuple[str, str, str | None, str | None]]:
        items: list[tuple[str, str, str | None, str | None]] = []
        for product_id in sorted(self._settings.adapty_product_tokens().keys()):
            items.append((product_id, product_id, None, "subscription"))
        for product_id in sorted(self._settings.token_products().keys()):
            items.append((product_id, f"Token pack {product_id}", None, "consumable"))
        return items

    async def adjust_tokens(self, user_id: uuid.UUID, amount: int) -> int:
        """Non-idempotent credit/debit (CRM contract §3.1)."""
        key = f"crm-tokens:{uuid.uuid4()}"
        if amount == 0:
            raise BadRequestError("amount must not be zero")
        if amount > 0:
            result = await self._admin.grant(
                user_id=user_id,
                amount=amount,
                idempotency_key=key,
                reason="crm_admin_tokens",
            )
            return result.new_balance
        try:
            result = await self._admin.debit(
                user_id=user_id,
                amount=-amount,
                idempotency_key=key,
                reason="crm_admin_tokens",
            )
        except InsufficientCreditsError as exc:
            raise BadRequestError("balance would go negative") from exc
        return result.new_balance

    async def set_subscription(
        self,
        user_id: uuid.UUID,
        product_id: str,
        expires_in_days: int,
        grant_id: str,
    ) -> CrmSubscriptionOutcome:
        known = self._known_product_ids()
        if known and product_id not in known:
            raise BadRequestError("unknown product_id")

        row = await self._repo.get_user_row(user_id)
        if row is None:
            raise UserNotFoundError("user not found")

        now = datetime.datetime.now(tz=datetime.UTC)
        idempotency_key = f"crm-subscription:{grant_id}"

        existing_grant = await self._repo.get_subscription_grant_event(user_id, idempotency_key)
        if existing_grant is not None:
            try:
                result = await self._admin.subscription_grant(
                    user_id=user_id,
                    plan=existing_grant.plan,
                    expires_at=existing_grant.expires_at,
                    grant_credits=False,
                    idempotency_key=idempotency_key,
                    reason="crm_admin_subscription",
                )
            except ConflictError as exc:
                raise BadRequestError("subscription grant conflict") from exc
            balance_row = await self._repo.get_user_row(user_id)
            balance = balance_row.balance if balance_row else row.balance
            active = _subscription_active("active", result.expires_at, now)
            return CrmSubscriptionOutcome(
                tokens=balance,
                subscription_active=active,
                subscription_expires_at=result.expires_at,
                applied=False,
            )

        base = now
        if row.subscription_status == "active" and row.subscription_expires_at is not None:
            exp = row.subscription_expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.UTC)
            if exp > now:
                base = exp

        expires_at = base + datetime.timedelta(days=expires_in_days)

        try:
            result = await self._admin.subscription_grant(
                user_id=user_id,
                plan=product_id,
                expires_at=expires_at,
                grant_credits=False,
                idempotency_key=idempotency_key,
                reason="crm_admin_subscription",
            )
            applied = not result.idempotent_replay
        except ConflictError as exc:
            raise BadRequestError("subscription grant conflict") from exc

        balance_row = await self._repo.get_user_row(user_id)
        balance = balance_row.balance if balance_row else row.balance
        active = _subscription_active("active", result.expires_at, now)
        return CrmSubscriptionOutcome(
            tokens=balance,
            subscription_active=active,
            subscription_expires_at=result.expires_at,
            applied=applied,
        )


def build_crm_user_list_item(row: CrmUserRow, now: datetime.datetime) -> dict[str, object]:
    return {
        "id": str(row.user_id),
        "external_id": None,
        "is_paid": row.is_paid,
        "payments_count": row.payments_count,
        "renewals_count": row.renewals_count,
        "tokens": float(row.balance),
        "subscription_active": _subscription_active(
            row.subscription_status, row.subscription_expires_at, now
        ),
        "subscription_expires_at": _utc_iso(row.subscription_expires_at),
        "plan_id": row.plan_id,
        "registered_at": _utc_iso(row.registered_at) or "",
    }
