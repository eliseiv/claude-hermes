"""Read models for CRM admin API (SQL aggregations)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CrmUserRow:
    user_id: uuid.UUID
    registered_at: datetime.datetime
    balance: int
    subscription_status: str | None
    plan_id: str | None
    subscription_expires_at: datetime.datetime | None
    is_paid: bool
    payments_count: int
    renewals_count: int


@dataclass(frozen=True)
class CrmPaymentRow:
    title: str
    description: str | None
    amount: float
    currency: str
    status: str
    occurred_at: datetime.datetime


@dataclass(frozen=True)
class CrmRequestRow:
    endpoint: str
    prompt_preview: str | None
    status_code: int
    status: str
    duration_sec: float | None
    sent_at: datetime.datetime


@dataclass(frozen=True)
class CrmSubscriptionGrantEvent:
    plan: str
    expires_at: datetime.datetime


_PAYMENT_CREDIT_FILTER = (
    "(lt.meta->>'source' = 'token_purchase' "
    "OR lt.meta ? 'adaptyEventId' "
    "OR lt.idempotency_key LIKE 'adapty-event:%')"
)

_SEARCH_CLAUSE = "(CAST(u.id AS TEXT) ILIKE :search OR CAST(u.id AS TEXT) = :search_exact)"


class CrmAdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_users(
        self,
        *,
        search: str | None,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
        is_paid: bool | None,
    ) -> int:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if search:
            clauses.append(_SEARCH_CLAUSE)
            params["search"] = f"%{search}%"
            params["search_exact"] = search
        if date_from is not None:
            clauses.append("u.created_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("u.created_at <= :date_to")
            params["date_to"] = date_to
        if is_paid is True:
            clauses.append(
                "EXISTS (SELECT 1 FROM ledger_transactions lt "
                "WHERE lt.user_id = u.id AND lt.type = 'credit' AND " + _PAYMENT_CREDIT_FILTER + ")"
            )
        if is_paid is False:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM ledger_transactions lt "
                "WHERE lt.user_id = u.id AND lt.type = 'credit' AND " + _PAYMENT_CREDIT_FILTER + ")"
            )
        where = " AND ".join(clauses)
        row = await self._session.scalar(
            text(f"SELECT COUNT(*) FROM users u WHERE {where}"),
            params,
        )
        return int(row or 0)

    async def list_users(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
        is_paid: bool | None,
    ) -> list[CrmUserRow]:
        clauses = ["1=1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search:
            clauses.append(_SEARCH_CLAUSE)
            params["search"] = f"%{search}%"
            params["search_exact"] = search
        if date_from is not None:
            clauses.append("u.created_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("u.created_at <= :date_to")
            params["date_to"] = date_to
        if is_paid is True:
            clauses.append(
                "EXISTS (SELECT 1 FROM ledger_transactions lt "
                "WHERE lt.user_id = u.id AND lt.type = 'credit' AND " + _PAYMENT_CREDIT_FILTER + ")"
            )
        if is_paid is False:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM ledger_transactions lt "
                "WHERE lt.user_id = u.id AND lt.type = 'credit' AND " + _PAYMENT_CREDIT_FILTER + ")"
            )
        where = " AND ".join(clauses)
        rows = await self._session.execute(
            text(
                f"""
                SELECT
                    u.id,
                    u.created_at,
                    COALESCE(w.balance, 0) AS balance,
                    s.status AS subscription_status,
                    s.plan AS plan_id,
                    s.expires_at AS subscription_expires_at,
                    EXISTS (
                        SELECT 1 FROM ledger_transactions lt
                        WHERE lt.user_id = u.id AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                    ) AS is_paid,
                    (
                        SELECT COUNT(*)::int FROM ledger_transactions lt
                        WHERE lt.user_id = u.id AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                    ) AS payments_count,
                    (
                        SELECT COUNT(*)::int FROM adapty_webhook_events awe
                        WHERE awe.user_id = u.id AND awe.event_type = 'subscription_renewed'
                    ) AS renewals_count
                FROM users u
                LEFT JOIN wallets w ON w.user_id = u.id
                LEFT JOIN subscriptions s ON s.user_id = u.id
                WHERE {where}
                ORDER BY u.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        return [
            CrmUserRow(
                user_id=row.id,
                registered_at=row.created_at,
                balance=int(row.balance),
                subscription_status=row.subscription_status,
                plan_id=row.plan_id,
                subscription_expires_at=row.subscription_expires_at,
                is_paid=bool(row.is_paid),
                payments_count=int(row.payments_count),
                renewals_count=int(row.renewals_count),
            )
            for row in rows
        ]

    async def get_user_row(self, user_id: uuid.UUID) -> CrmUserRow | None:
        rows = await self.list_users(
            limit=1,
            offset=0,
            search=str(user_id),
            date_from=None,
            date_to=None,
            is_paid=None,
        )
        for row in rows:
            if row.user_id == user_id:
                return row
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        if exists is None:
            return None
        # User exists but search by full id might miss if search filter odd — direct fetch
        result = await self._session.execute(
            text(
                f"""
                SELECT
                    u.id,
                    u.created_at,
                    COALESCE(w.balance, 0) AS balance,
                    s.status AS subscription_status,
                    s.plan AS plan_id,
                    s.expires_at AS subscription_expires_at,
                    EXISTS (
                        SELECT 1 FROM ledger_transactions lt
                        WHERE lt.user_id = u.id AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                    ) AS is_paid,
                    (
                        SELECT COUNT(*)::int FROM ledger_transactions lt
                        WHERE lt.user_id = u.id AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                    ) AS payments_count,
                    (
                        SELECT COUNT(*)::int FROM adapty_webhook_events awe
                        WHERE awe.user_id = u.id AND awe.event_type = 'subscription_renewed'
                    ) AS renewals_count
                FROM users u
                LEFT JOIN wallets w ON w.user_id = u.id
                LEFT JOIN subscriptions s ON s.user_id = u.id
                WHERE u.id = :uid
                """
            ),
            {"uid": str(user_id)},
        )
        one = result.one_or_none()
        if one is None:
            return None
        return CrmUserRow(
            user_id=one.id,
            registered_at=one.created_at,
            balance=int(one.balance),
            subscription_status=one.subscription_status,
            plan_id=one.plan_id,
            subscription_expires_at=one.subscription_expires_at,
            is_paid=bool(one.is_paid),
            payments_count=int(one.payments_count),
            renewals_count=int(one.renewals_count),
        )

    async def get_subscription_grant_event(
        self, user_id: uuid.UUID, idempotency_key: str
    ) -> CrmSubscriptionGrantEvent | None:
        result = await self._session.execute(
            text(
                "SELECT plan, expires_at FROM subscription_grant_events "
                "WHERE user_id = :uid AND idempotency_key = :key"
            ),
            {"uid": str(user_id), "key": idempotency_key},
        )
        one = result.one_or_none()
        if one is None:
            return None
        return CrmSubscriptionGrantEvent(plan=one.plan, expires_at=one.expires_at)

    async def ledger_totals(self, user_id: uuid.UUID) -> tuple[int, int]:
        row = await self._session.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(amount) FILTER (WHERE type = 'credit'), 0) AS credited,
                    COALESCE(SUM(amount) FILTER (WHERE type = 'debit'), 0) AS spent
                FROM ledger_transactions
                WHERE user_id = :uid
                """
            ),
            {"uid": str(user_id)},
        )
        one = row.one()
        return int(one.credited), int(one.spent)

    async def last_payment_at(self, user_id: uuid.UUID) -> datetime.datetime | None:
        value = await self._session.scalar(
            text(
                f"""
                SELECT MAX(created_at) FROM ledger_transactions lt
                WHERE lt.user_id = :uid AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                """
            ),
            {"uid": str(user_id)},
        )
        return value if isinstance(value, datetime.datetime) else None

    async def count_payments(self, user_id: uuid.UUID) -> int:
        row = await self._session.scalar(
            text(
                f"""
                SELECT COUNT(*) FROM ledger_transactions lt
                WHERE lt.user_id = :uid AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                """
            ),
            {"uid": str(user_id)},
        )
        return int(row or 0)

    async def list_payments(
        self, user_id: uuid.UUID, limit: int, offset: int
    ) -> list[CrmPaymentRow]:
        rows = await self._session.execute(
            text(
                f"""
                SELECT lt.amount, lt.meta, lt.created_at, lt.idempotency_key
                FROM ledger_transactions lt
                WHERE lt.user_id = :uid AND lt.type = 'credit' AND {_PAYMENT_CREDIT_FILTER}
                ORDER BY lt.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"uid": str(user_id), "limit": limit, "offset": offset},
        )
        out: list[CrmPaymentRow] = []
        for row in rows:
            meta = row.meta or {}
            source = meta.get("source")
            product_id = meta.get("productId") or meta.get("vendorProductId")
            event_type = meta.get("eventType")
            title: str
            description: str | None
            if source == "token_purchase":
                title = f"Покупка токенов ({product_id or 'pack'})"
                description = "App Store consumable"
            elif meta.get("adaptyEventId") or str(row.idempotency_key).startswith("adapty-event:"):
                title = f"Подписка ({product_id or 'plan'})"
                description = str(event_type) if event_type is not None else "Adapty"
            else:
                title = "Начисление кредитов"
                description = str(source) if source is not None else None
            out.append(
                CrmPaymentRow(
                    title=title,
                    description=description,
                    amount=float(row.amount),
                    currency="CREDITS",
                    status="success",
                    occurred_at=row.created_at,
                )
            )
        return out

    async def count_requests(self, user_id: uuid.UUID) -> int:
        agent = await self._session.scalar(
            text("SELECT COUNT(*) FROM agent_runs WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        chats = await self._session.scalar(
            text("SELECT COUNT(*) FROM chat_sessions WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        return int(agent or 0) + int(chats or 0)

    async def list_requests(
        self, user_id: uuid.UUID, limit: int, offset: int
    ) -> list[CrmRequestRow]:
        rows = await self._session.execute(
            text(
                """
                SELECT endpoint, prompt_preview, status_code, status, duration_sec, sent_at
                FROM (
                    SELECT
                        'agent_run' AS endpoint,
                        LEFT(COALESCE(s.result_text, ''), 120) AS prompt_preview,
                        CASE WHEN ar.status IN ('completed', 'cancelled') THEN 200
                             WHEN ar.status = 'failed' THEN 500 ELSE 200 END AS status_code,
                        CASE WHEN ar.status IN ('completed', 'cancelled') THEN 'ok'
                             WHEN ar.status = 'failed' THEN 'error' ELSE 'ok' END AS status,
                        EXTRACT(EPOCH FROM (ar.updated_at - ar.created_at)) AS duration_sec,
                        ar.created_at AS sent_at
                    FROM agent_runs ar
                    LEFT JOIN agent_run_snapshots s ON s.run_id = ar.run_id
                    WHERE ar.user_id = :uid
                    UNION ALL
                    SELECT
                        'text_chat' AS endpoint,
                        LEFT(COALESCE(cs.title, ''), 120) AS prompt_preview,
                        200 AS status_code,
                        'ok' AS status,
                        NULL AS duration_sec,
                        cs.created_at AS sent_at
                    FROM chat_sessions cs
                    WHERE cs.user_id = :uid
                ) combined
                ORDER BY sent_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"uid": str(user_id), "limit": limit, "offset": offset},
        )
        return [
            CrmRequestRow(
                endpoint=row.endpoint,
                prompt_preview=row.prompt_preview or None,
                status_code=int(row.status_code),
                status=row.status,
                duration_sec=float(row.duration_sec) if row.duration_sec is not None else None,
                sent_at=row.sent_at,
            )
            for row in rows
        ]

    async def stats(
        self,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
    ) -> tuple[int, int]:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if date_from is not None:
            clauses.append("u.created_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("u.created_at <= :date_to")
            params["date_to"] = date_to
        where = " AND ".join(clauses)
        row = await self._session.execute(
            text(
                f"""
                SELECT
                    COUNT(*)::int AS users_total,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM ledger_transactions lt
                            WHERE lt.user_id = u.id
                              AND lt.type = 'credit'
                              AND {_PAYMENT_CREDIT_FILTER}
                        )
                    )::int AS paid_users
                FROM users u
                WHERE {where}
                """
            ),
            params,
        )
        one = row.one()
        return int(one.users_total), int(one.paid_users)
