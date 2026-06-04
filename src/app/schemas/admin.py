"""Admin schemas for /v1/admin/* (admin/02-api-contracts.md, ADR-009).

Strict Pydantic v2 (extra='forbid'): amount > 0, non-empty reason, bounded idempotencyKey.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import StrictModel


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


class AdminLedgerTxView(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор транзакции реестра.")
    type: Literal["credit", "debit"] = Field(description="Тип транзакции.")
    amount: int = Field(description="Сумма транзакции в кредитах.")
    createdAt: datetime.datetime = Field(description="Время создания (UTC).")
    meta: dict[str, Any] = Field(description="Метаданные (без секретов).")


class AdminWalletResponse(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор пользователя.")
    balance: int = Field(description="Текущий баланс кредитов.")
    lastTransactions: list[AdminLedgerTxView] = Field(
        description="Последние транзакции реестра (новые первыми)."
    )
