"""Policy schemas for /v1/policy/effective (policy-engine/02)."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel


class EffectivePolicyResponse(StrictModel):
    isSubscribed: bool = Field(description="Есть ли активная подписка.")
    trialRemaining: int = Field(description="Остаток бесплатных пробных генераций (trial).")
    creditsBalance: int = Field(description="Текущий баланс кредитов (1 кредит = 1 сообщение).")
    debt: int = Field(
        description=(
            "Непогашенная несписанная дельта агентного прогона в кредитах (ADR-051). 0 при "
            "отсутствии долга или выключенном AGENT_DEBT_RECONCILE_ENABLED."
        )
    )
    netBalance: int = Field(
        description=(
            "Эффективный баланс с учётом долга: creditsBalance − debt. Может быть < 0 при "
            "наличии долга (клиент показывает как отрицательный баланс)."
        )
    )
    byokEnabled: bool = Field(
        description="Включён ли пользователем собственный ключ Anthropic (BYOK)."
    )
    canGenerateCreditsMode: bool = Field(description="Доступна ли генерация в режиме `credits`.")
    canGenerateByokMode: bool = Field(description="Доступна ли генерация в режиме `byok`.")
    reasons: list[str] = Field(
        description=(
            "Причины недоступности генерации (подмножество значений `blockReason`: "
            "`trial_used`, `subscription_required`, `subscription_expired`, `credits_empty`, "
            "`byok_disabled`, `byok_invalid`, `rate_limited`, `policy_denied`). Те же значения "
            "использует `blockReason` в Chat."
        ),
    )
