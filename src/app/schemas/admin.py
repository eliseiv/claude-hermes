"""Admin schemas for /v1/admin/* (admin/02-api-contracts.md, ADR-009).

Strict Pydantic v2 (extra='forbid'): amount > 0, non-empty reason, bounded idempotencyKey.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import StrictModel


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class AdminGrantRequest(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор существующего пользователя.")
    amount: int = Field(gt=0, description="Сколько кредитов начислить (целое > 0).")
    idempotencyKey: str = Field(
        min_length=1,
        max_length=128,
        description="Ключ идемпотентности начисления.",
    )
    reason: str = Field(
        min_length=1,
        max_length=512,
        description="Причина начисления (обязательна).",
    )


class AdminGrantResponse(StrictModel):
    newBalance: int = Field(description="Баланс кредитов после начисления.")
    ledgerTxId: uuid.UUID = Field(description="Идентификатор транзакции реестра.")
    idempotentReplay: bool = Field(
        description="true, если ключ уже использовался с тем же payload (повтор без начисления)."
    )


class AdminSubscriptionGrantRequest(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор существующего пользователя.")
    plan: str = Field(
        min_length=1,
        max_length=64,
        description="Идентификатор плана подписки (хранится в subscriptions.plan).",
    )
    expiresAt: datetime.datetime = Field(
        description="Окончание периода подписки (ISO8601). Должен быть в будущем."
    )
    idempotencyKey: str = Field(
        min_length=1,
        max_length=128,
        description="Ключ идемпотентности admin-операции выдачи подписки.",
    )
    reason: str = Field(
        min_length=1,
        max_length=512,
        description="Причина выдачи подписки (обязательна).",
    )
    grantCredits: bool = Field(
        default=False,
        description="При true — дополнительно начисляет SUBSCRIPTION_CREDITS_PER_PERIOD кредитов.",
    )

    @field_validator("expiresAt")
    @classmethod
    def _expires_in_future(cls, value: datetime.datetime) -> datetime.datetime:
        """expiresAt должен быть строго в будущем (> now()), иначе 422 (admin/02-api-contracts)."""
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=datetime.UTC)
        if normalized <= _utcnow():
            raise ValueError("expiresAt must be in the future")
        return normalized


class AdminSubscriptionGrantResponse(StrictModel):
    status: Literal["active"] = Field(description="Статус подписки после выдачи (всегда active).")
    plan: str = Field(description="Применённый план подписки.")
    expiresAt: datetime.datetime = Field(description="Применённое окончание периода (ISO8601).")
    creditsGranted: int | None = Field(
        default=None,
        description="Начислено кредитов (> 0) только при grantCredits=true; иначе null.",
    )
    ledgerTxId: uuid.UUID | None = Field(
        default=None,
        description="Идентификатор транзакции начисления только при grantCredits=true; иначе null.",
    )
    idempotentReplay: bool = Field(
        description="true, если та же admin-операция уже была применена с тем же payload."
    )


class AdminLedgerTxView(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор транзакции реестра.")
    type: Literal["credit", "debit"] = Field(description="Тип транзакции.")
    amount: int = Field(description="Сумма транзакции в кредитах.")
    createdAt: datetime.datetime = Field(description="Время создания (UTC).")
    meta: dict[str, Any] = Field(description="Метаданные (без секретов).")


class AdminWalletResponse(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор пользователя.")
    balance: int = Field(description="Текущий баланс кредитов.")
    debt: int = Field(
        description=(
            "Непогашенная несписанная дельта агентного прогона в кредитах (ADR-051). 0 при "
            "отсутствии долга или выключенном AGENT_DEBT_RECONCILE_ENABLED."
        )
    )
    lastTransactions: list[AdminLedgerTxView] = Field(
        description="Последние транзакции реестра (новые первыми)."
    )
