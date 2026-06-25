"""Admin service (ADR-009, ADR-048, ADM-4..11): thin wrapper over Wallet/Subscription services.

Does NOT duplicate billing/subscription logic — it adds admin authorization context (caller
already passed ``require_admin``), a user-existence check (admin never creates users, ADR-007),
extra ``admin_grant`` / ``admin_subscription_grant`` audit events, and the corresponding metrics.
Idempotency, ledger writes and the ``billing_credit`` audit stay in WalletService.grant; the
subscriptions upsert and per-period credit grant stay in SubscriptionService.admin_grant.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_ADMIN_GRANT, AuditEvent, AuditService
from app.errors import ConflictError, UserNotFoundError
from app.models import LedgerTransaction
from app.observability.metrics import admin_grant_total, admin_subscription_grant_total
from app.subscription.service import AdminGrantResult as SubscriptionAdminGrantResult
from app.subscription.service import SubscriptionService
from app.wallet.service import WalletService


@dataclass(frozen=True)
class AdminGrantResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


@dataclass(frozen=True)
class AdminWalletView:
    user_id: uuid.UUID
    balance: int
    debt: int
    last_transactions: list[LedgerTransaction]


class AdminService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
        subscription: SubscriptionService,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit
        self._subscription = subscription

    async def _user_exists(self, user_id: uuid.UUID) -> bool:
        """Parameterized existence check; admin never creates users (ADR-009, ADR-007)."""
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        return exists is not None

    async def _require_user_exists(self, user_id: uuid.UUID) -> None:
        """Admin grant/view never creates users — missing userId is a 404 (ADR-009, ADR-007).

        Parameterized lookup. Done BEFORE WalletService.grant (which would _ensure_wallet but
        never create the users row) so an operator typo surfaces as user_not_found, not a silent
        phantom account.
        """
        if not await self._user_exists(user_id):
            admin_grant_total.labels(result="not_found").inc()
            raise UserNotFoundError("user not found")

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
    ) -> AdminGrantResult:
        """Credit a user's wallet on behalf of an operator (saap/compensation).

        Reuses WalletService.grant verbatim (atomic, idempotent by (user_id, idempotency_key),
        writes ledger credit + billing_credit audit). Adds an admin_grant audit event recording
        the admin initiation (actor=admin, reason). The X-Admin-Token secret is never part of any
        payload. A reused key with a different amount surfaces as 409 (conflict).
        """
        await self._require_user_exists(user_id)
        meta: dict[str, Any] = {"source": "admin", "reason": reason}
        try:
            result = await self._wallet.grant(
                user_id=user_id,
                amount=amount,
                idempotency_key=idempotency_key,
                meta=meta,
                reason=reason,
            )
        except ConflictError:
            admin_grant_total.labels(result="conflict").inc()
            raise

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_ADMIN_GRANT,
                payload={
                    "actor": "admin",
                    "userId": str(user_id),
                    "amount": amount,
                    "reason": reason,
                    "idempotencyKey": idempotency_key,
                    "ledgerTxId": str(result.ledger_tx_id),
                    "idempotentReplay": result.idempotent_replay,
                },
            )
        )
        admin_grant_total.labels(result="success").inc()
        return AdminGrantResult(
            new_balance=result.new_balance,
            ledger_tx_id=result.ledger_tx_id,
            idempotent_replay=result.idempotent_replay,
        )

    async def subscription_grant(
        self,
        *,
        user_id: uuid.UUID,
        plan: str,
        expires_at: datetime.datetime,
        grant_credits: bool,
        idempotency_key: str,
        reason: str,
    ) -> SubscriptionAdminGrantResult:
        """Manually activate/grant a subscription on behalf of an operator (ADR-048 §2).

        Thin admin orchestration over SubscriptionService.admin_grant: checks user existence first
        (404, never creates users), translates idempotency conflicts to 409, records the
        admin_subscription_grant_total metric. The atomic subscriptions upsert + optional credit
        grant + admin_subscription_grant audit live in SubscriptionService (one transaction).
        """
        if not await self._user_exists(user_id):
            admin_subscription_grant_total.labels(result="not_found").inc()
            raise UserNotFoundError("user not found")
        try:
            result = await self._subscription.admin_grant(
                user_id,
                plan,
                expires_at,
                grant_credits=grant_credits,
                idempotency_key=idempotency_key,
                reason=reason,
            )
        except ConflictError:
            admin_subscription_grant_total.labels(result="conflict").inc()
            raise
        admin_subscription_grant_total.labels(result="success").inc()
        return result

    async def get_wallet_view(self, user_id: uuid.UUID, last_n: int) -> AdminWalletView:
        """Read-only wallet view for support. Missing userId → 404 (never creates a user)."""
        await self._require_user_exists(user_id)
        balance, debt, txs = await self._wallet.get_wallet_view(user_id, last_n)
        return AdminWalletView(user_id=user_id, balance=balance, debt=debt, last_transactions=txs)
