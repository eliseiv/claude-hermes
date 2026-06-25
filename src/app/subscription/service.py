"""Subscription service: admin subscription-grant with durable idempotency (subscription/03).

The StoreKit ``/v1/subscription/sync`` path is RETIRED (ADR-029 revision, TD-021): Adapty is the
single subscription source, so the only remaining operation here is the admin-initiated grant
(ADR-048 §2) hardened with a durable idempotency anchor (ADR-052): the ``subscription_grant_events``
table makes a strict 409 on "same idempotencyKey, different payload" reachable for BOTH
``grantCredits`` paths (closes TD-030). The shared ``StoreKitVerifier`` still serves consumable
``/v1/tokens/purchase`` (ADR-015) but is no longer used here.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_ADMIN_SUBSCRIPTION_GRANT,
    AuditEvent,
    AuditService,
)
from app.config import get_settings
from app.errors import ConflictError
from app.models import Subscription
from app.wallet.service import WalletService


@dataclass(frozen=True)
class AdminGrantResult:
    status: str
    plan: str
    expires_at: datetime.datetime
    credits_granted: int | None
    ledger_tx_id: uuid.UUID | None
    idempotent_replay: bool


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _payload_hash(plan: str, expires_at: datetime.datetime, grant_credits: bool) -> str:
    """sha256 of the canonical subscription-grant payload (ADR-052 §1).

    Canonical form: ``plan ‖ ISO8601(expires_at) ‖ grant_credits`` joined with a separator. The
    timestamp is normalized to a tz-aware UTC instant so two requests for the same instant produce
    the same hash regardless of input tz/offset representation.
    """
    normalized = (
        expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=datetime.UTC)
    )
    canonical = f"{plan}\x1f{normalized.astimezone(datetime.UTC).isoformat()}\x1f{grant_credits}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit

    async def admin_grant(
        self,
        user_id: uuid.UUID,
        plan: str,
        expires_at: datetime.datetime,
        *,
        grant_credits: bool = False,
        idempotency_key: str,
        reason: str,
    ) -> AdminGrantResult:
        """Manual operator activation of a subscription with a durable idempotency anchor.

        ADR-052: in one transaction, ``INSERT INTO subscription_grant_events (...) ON CONFLICT
        (user_id, idempotency_key) DO NOTHING RETURNING ...`` (pattern of adapty_webhook_events):
        - Conflict + matching ``payload_hash`` → idempotent replay (no upsert/grant; state echoed
          from the stored row).
        - Conflict + DIFFERENT ``payload_hash`` → strict 409 (ConflictError) for BOTH grantCredits
          paths, no mutation — this removes the former later-writer-wins of grant_credits=false.
        - New insert → upsert ``subscriptions`` (active/plan/expires_at), optional
          ``WalletService.grant("admin-sub-grant:{idempotencyKey}")``, audit
          ``admin_subscription_grant``; the credit ledger_tx_id is recorded back into the event row.

        User existence is checked by the admin router BEFORE this call (404 user_not_found); this
        method never creates users. Failure rolls back the whole transaction (on retry the key is
        free again; no double grant — grant is idempotent and the event row is the dedup point).
        """
        payload_hash = _payload_hash(plan, expires_at, grant_credits)

        inserted = await self._session.scalar(
            text(
                "INSERT INTO subscription_grant_events "
                "(user_id, idempotency_key, payload_hash, plan, expires_at, grant_credits) "
                "VALUES (:uid, :key, :phash, :plan, :expires, :gc) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING idempotency_key"
            ),
            {
                "uid": str(user_id),
                "key": idempotency_key,
                "phash": payload_hash,
                "plan": plan,
                "expires": expires_at,
                "gc": grant_credits,
            },
        )

        if inserted is None:
            # Conflict: the key already exists. Compare payload_hash to decide replay vs 409.
            return await self._handle_existing_grant_event(
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )

        # New operation: upsert the subscription, optionally grant credits, audit.
        return await self._apply_new_grant(
            user_id=user_id,
            plan=plan,
            expires_at=expires_at,
            grant_credits=grant_credits,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    async def _handle_existing_grant_event(
        self,
        *,
        user_id: uuid.UUID,
        idempotency_key: str,
        payload_hash: str,
    ) -> AdminGrantResult:
        """Resolve a conflicting subscription_grant_events row: idempotent replay or strict 409."""
        row = await self._session.execute(
            text(
                "SELECT payload_hash, plan, expires_at, grant_credits, ledger_tx_id "
                "FROM subscription_grant_events "
                "WHERE user_id = :uid AND idempotency_key = :key"
            ),
            {"uid": str(user_id), "key": idempotency_key},
        )
        existing = row.one_or_none()
        if existing is None:  # pragma: no cover - defensive (row vanished mid-transaction)
            raise ConflictError("idempotency conflict")
        if existing.payload_hash != payload_hash:
            # Same key, different payload → strict 409 for BOTH grantCredits paths (ADR-052 §2).
            raise ConflictError("idempotency key reused with different payload")
        # Pure replay: echo the stored state, no upsert/grant (ADR-052 §2).
        credits_granted = (
            get_settings().subscription_credits_per_period if existing.grant_credits else None
        )
        ledger_tx_id = (
            uuid.UUID(str(existing.ledger_tx_id)) if existing.ledger_tx_id is not None else None
        )
        return AdminGrantResult(
            status="active",
            plan=existing.plan,
            expires_at=existing.expires_at,
            credits_granted=credits_granted,
            ledger_tx_id=ledger_tx_id,
            idempotent_replay=True,
        )

    async def _apply_new_grant(
        self,
        *,
        user_id: uuid.UUID,
        plan: str,
        expires_at: datetime.datetime,
        grant_credits: bool,
        idempotency_key: str,
        reason: str,
    ) -> AdminGrantResult:
        """Upsert subscription + optional credit grant + audit for a newly-anchored grant event."""
        row = await self._session.scalar(
            text("SELECT user_id FROM subscriptions WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        if row is None:
            self._session.add(
                Subscription(user_id=user_id, status="active", plan=plan, expires_at=expires_at)
            )
        else:
            await self._session.execute(
                text(
                    "UPDATE subscriptions "
                    "SET status = 'active', plan = :plan, expires_at = :expires, "
                    "    updated_at = now() "
                    "WHERE user_id = :uid"
                ),
                {"uid": str(user_id), "plan": plan, "expires": expires_at},
            )
        await self._session.flush()

        credits_granted: int | None = None
        ledger_tx_id: uuid.UUID | None = None
        if grant_credits:
            settings = get_settings()
            grant = await self._wallet.grant(
                user_id=user_id,
                amount=settings.subscription_credits_per_period,
                idempotency_key=f"admin-sub-grant:{idempotency_key}",
                meta={"source": "admin_subscription_grant", "reason": reason},
                reason="admin_subscription",
            )
            credits_granted = settings.subscription_credits_per_period
            ledger_tx_id = grant.ledger_tx_id
            # Record the credit-tx id back into the durable anchor (ADR-052 §2).
            await self._session.execute(
                text(
                    "UPDATE subscription_grant_events SET ledger_tx_id = :txid "
                    "WHERE user_id = :uid AND idempotency_key = :key"
                ),
                {"txid": str(ledger_tx_id), "uid": str(user_id), "key": idempotency_key},
            )

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_ADMIN_SUBSCRIPTION_GRANT,
                payload={
                    "actor": "admin",
                    "userId": str(user_id),
                    "plan": plan,
                    "expiresAt": expires_at.isoformat(),
                    "reason": reason,
                    "idempotencyKey": idempotency_key,
                    "grantCredits": grant_credits,
                    "ledgerTxId": str(ledger_tx_id) if ledger_tx_id is not None else None,
                },
            )
        )
        return AdminGrantResult(
            status="active",
            plan=plan,
            expires_at=expires_at,
            credits_granted=credits_granted,
            ledger_tx_id=ledger_tx_id,
            idempotent_replay=False,
        )
