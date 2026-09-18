"""CloudPaymentsWebhookService: callback = trigger -> verify -> reconcile -> credit (ADR-068)."""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_CLOUDPAYMENTS_PAYMENT, AuditEvent, AuditService
from app.billing_cloudpayments import parser, verify
from app.billing_cloudpayments.verify import CloudPaymentsVerifyClient, CreditablePayment
from app.billing_common.resolve import resolve_user
from app.config import Settings
from app.errors import CloudPaymentsWebhookMisconfiguredError
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of handling one webhook call. The router maps it to an HTTP-200 ``{"code": 0}``."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None


def _ignored(reason: str) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason)


def _level_for(result: str, reason: str | None) -> int:
    if result in ("applied", "duplicate"):
        return logging.INFO
    if reason in ("user_not_found", "no_creditable_payment", "payment_skipped"):
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    return logging.INFO


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class CloudPaymentsWebhookService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
        settings: Settings,
        verify_client: CloudPaymentsVerifyClient,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit
        self._settings = settings
        self._verify = verify_client

    def _log_outcome(
        self,
        outcome: WebhookOutcome,
        *,
        transaction_id: str | None = None,
        user_id: uuid.UUID | None = None,
        resolved_via: str | None = None,
        verify_result: str | None = None,
        credited_count: int | None = None,
        payment_statuses: list[str] | None = None,
    ) -> WebhookOutcome:
        log_event(
            logger,
            _level_for(outcome.result, outcome.reason),
            "cloudpayments_webhook_outcome",
            result=outcome.result,
            reason=outcome.reason,
            transactionId=transaction_id,
            userId=str(user_id) if user_id is not None else None,
            resolvedVia=resolved_via,
            verify=verify_result,
            creditedCount=credited_count,
            paymentStatuses=payment_statuses,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Process one raw callback: trigger -> resolve -> verify -> reconcile -> credit."""
        if not self._settings.cloudpayments_api_token:
            raise CloudPaymentsWebhookMisconfiguredError("cloudpayments api token not configured")

        if not raw:
            return self._log_outcome(_ignored("empty_body"))
        try:
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._log_outcome(_ignored("invalid_json"))
        if not isinstance(body, dict):
            return self._log_outcome(_ignored("not_an_object"))

        status = parser.parse_status(body)
        operation_type = parser.parse_operation_type(body)
        transaction_id = parser.parse_transaction_id(body)
        if not parser.parse_gate(status, operation_type):
            return self._log_outcome(
                _ignored("not_a_completed_payment"), transaction_id=transaction_id
            )

        data = parser._parse_data(body) or {}
        device_id = parser.parse_user_id(body, data)
        if device_id is None:
            return self._log_outcome(_ignored("invalid_account_id"), transaction_id=transaction_id)

        resolved = await resolve_user(self._session, device_id)
        if resolved is None:
            return self._log_outcome(_ignored("user_not_found"), transaction_id=transaction_id)
        resolved_user_id, resolved_via = resolved

        await self._session.commit()

        payments_data = await self._list_payments_both(user_id=resolved_user_id, other=device_id)
        statuses = verify.payment_statuses(payments_data)

        creditable = verify.select_creditable_payments(
            payments_data,
            paid_statuses=self._settings.cloudpayments_paid_statuses(),
            now=_now(),
            freshness_hours=self._settings.cloudpayments_payment_freshness_hours,
        )
        if not creditable:
            return self._log_outcome(
                _ignored("no_creditable_payment"),
                transaction_id=transaction_id,
                user_id=resolved_user_id,
                resolved_via=resolved_via,
                verify_result="ok",
                credited_count=0,
                payment_statuses=statuses,
            )

        credited = 0
        skipped = 0
        for payment in creditable:
            applied = await self._apply_payment(payment, resolved_user_id)
            if applied == "credited":
                credited += 1
            elif applied == "skipped":
                skipped += 1

        if credited >= 1:
            outcome = WebhookOutcome(result="applied")
        elif skipped >= 1:
            outcome = _ignored("payment_skipped")
        else:
            outcome = WebhookOutcome(result="duplicate")
        return self._log_outcome(
            outcome,
            transaction_id=transaction_id,
            user_id=resolved_user_id,
            resolved_via=resolved_via,
            verify_result="ok",
            credited_count=credited,
            payment_statuses=statuses,
        )

    async def _list_payments_both(
        self, *, user_id: uuid.UUID, other: uuid.UUID | None
    ) -> list[dict[str, object]]:
        """Ask the provider about payments under BOTH identifiers and merge the answers.

        Checkout sends our internal ``userId``; older clients / the callback may carry a
        deviceId. Querying one key would lose one of the two epochs. Dedup is by payment_id.
        """
        merged = list(await self._verify.list_payments(device_id=user_id))
        if other is not None and other != user_id:
            merged.extend(await self._verify.list_payments(device_id=other))
        seen: set[object] = set()
        unique: list[dict[str, object]] = []
        for item in merged:
            key = item.get("payment_id") if isinstance(item, dict) else None
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            unique.append(item)
        return unique

    async def _apply_payment(
        self, payment: CreditablePayment, user_id: uuid.UUID
    ) -> Literal["credited", "duplicate", "skipped"]:
        """Credit ONE verified payment in its own transaction."""
        if payment.payment_type == "one_time":
            kind = parser.KIND_TOKENS
        elif payment.payment_type == "subscription":
            kind = parser.KIND_SUBSCRIPTION
        else:
            self._log_payment_skipped(payment, user_id, "unknown_payment_type")
            return "skipped"

        one_time_ids = frozenset(self._settings.token_products())

        if (
            kind == parser.KIND_TOKENS
            and payment.product_code not in one_time_ids
            and parser.classify_product(payment.product_code, None, one_time_ids)
            == parser.KIND_SUBSCRIPTION
        ):
            kind = parser.KIND_SUBSCRIPTION
            log_event(
                logger,
                logging.WARNING,
                "cloudpayments_payment_type_mismatch",
                paymentId=payment.payment_id,
                productId=payment.product_code,
                paymentType=payment.payment_type,
                creditedAs=parser.KIND_SUBSCRIPTION,
                userId=str(user_id),
            )

        if kind == parser.KIND_TOKENS:
            resolved = self._settings.token_products().get(payment.product_code)
            if resolved is None or resolved <= 0:
                self._log_payment_skipped(payment, user_id, "unknown_product")
                return "skipped"
            credits = resolved
            reason = "cloudpayments_tokens"
        else:
            credits = self._settings.cloudpayments_product_tokens().get(
                payment.product_code, self._settings.cloudpayments_subscription_tokens_grant
            )
            reason = "cloudpayments_subscription"

        try:
            sanitized = {
                "paymentId": payment.payment_id,
                "productCode": payment.product_code,
                "paymentType": payment.payment_type,
                "status": payment.status,
                "kind": kind,
            }
            inserted = await self._session.scalar(
                text(
                    "INSERT INTO cloudpayments_webhook_events "
                    "(transaction_id, user_id, product_id, kind, payload) "
                    "VALUES (:txn, :uid, :product_id, :kind, CAST(:payload AS JSONB)) "
                    "ON CONFLICT (transaction_id) DO NOTHING "
                    "RETURNING transaction_id"
                ),
                {
                    "txn": payment.payment_id,
                    "uid": str(user_id),
                    "product_id": payment.product_code,
                    "kind": kind,
                    "payload": json.dumps(sanitized),
                },
            )
            if inserted is None:
                await self._session.rollback()
                return "duplicate"

            sub_status: str | None = None
            plan: str | None = None
            expires_at: datetime.datetime | None = None
            if kind == parser.KIND_SUBSCRIPTION:
                unit = parser.infer_interval_unit_from_code(payment.product_code)
                expires_at = parser._compute_expiry(_now(), unit, 1)
                plan = payment.product_code
                sub_status = "active"
                await self._upsert_subscription(user_id, plan, expires_at)

            await self._wallet.grant(
                user_id=user_id,
                amount=credits,
                idempotency_key=f"cp-txn:{payment.payment_id}",
                reason=reason,
                meta={
                    "paymentId": payment.payment_id,
                    "productCode": payment.product_code,
                    "kind": kind,
                },
            )
            await self._audit.record(
                AuditEvent(
                    user_id=user_id,
                    event_type=EVENT_CLOUDPAYMENTS_PAYMENT,
                    payload={
                        "transactionId": payment.payment_id,
                        "paymentId": payment.payment_id,
                        "productId": payment.product_code,
                        "kind": kind,
                        "semantics": kind,
                        "paymentType": payment.payment_type,
                        "status": sub_status,
                        "plan": plan,
                        "expiresAt": expires_at.isoformat() if expires_at is not None else None,
                        "creditsGranted": credits,
                        "paidAt": payment.paid_at.isoformat(),
                    },
                )
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        log_event(
            logger,
            logging.DEBUG,
            "cloudpayments_payment_credited",
            paymentId=payment.payment_id,
            productId=payment.product_code,
            kind=kind,
            userId=str(user_id),
            creditsGranted=credits,
        )
        return "credited"

    def _log_payment_skipped(
        self, payment: CreditablePayment, user_id: uuid.UUID, reason: str
    ) -> None:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_payment_skipped",
            reason=reason,
            paymentId=payment.payment_id,
            productId=payment.product_code,
            paymentType=payment.payment_type,
            userId=str(user_id),
        )

    async def _upsert_subscription(
        self, user_id: uuid.UUID, plan: str, expires_at: datetime.datetime
    ) -> None:
        """Activate/extend the subscription in one statement."""
        await self._session.execute(
            text(
                "INSERT INTO subscriptions "
                "(user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
        )
        await self._session.flush()
