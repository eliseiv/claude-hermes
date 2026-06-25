"""Agent-proxy schemas for /v1/agent/* (agent-proxy/02-api-contracts.md, ADR-045/047).

Request/response models of the client-facing contour. The SSE event stream
(GET /v1/agent/runs/{runId}/events) and the approval/stop passthrough bodies follow Hermes'
external contract and are relayed as-is, so only the run-launch request, the run-launch response,
and the approval body are modelled here as strict Pydantic v2 schemas.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel


class AgentRunRequest(StrictModel):
    """Запуск автономного прогона агента (маппится на Hermes `POST /v1/runs`)."""

    message: str = Field(
        min_length=1,
        description="Текст хода пользователя. Маппится в Hermes `input` (обязателен).",
    )
    sessionId: str | None = Field(
        default=None,
        description=(
            "Преемственность диалога внутри инстанса. Маппится в Hermes `session_id` (опц.)."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Модель Hermes внутри инстанса. Маппится в Hermes `model` (опц.).",
    )


class AgentRunResponse(StrictModel):
    """Ответ на `POST /v1/agent/run`.

    Allowed (HTTP 202): `status` ∈ {queued, running} + `runId` (proxy Hermes `run_id`).
    Blocked (HTTP 200, ADR-004): `status="blocked"` + `blockReason`; `runId` отсутствует.
    """

    status: Literal["queued", "running", "blocked"] = Field(
        description="`queued`/`running` — прогон принят (202); `blocked` — заблокирован (200)."
    )
    runId: str | None = Field(
        default=None,
        description="Идентификатор прогона Hermes (`run_id`). Есть только при не-blocked ответе.",
    )
    blockReason: str | None = Field(
        default=None,
        description=(
            "Причина блокировки: `credits_empty` | `subscription_expired` | `trial_used` | "
            "`debt_outstanding`. Присутствует только при `status=blocked`. `debt_outstanding` "
            "(ADR-051) — достижим только на агентном пути под AGENT_DEBT_RECONCILE_ENABLED "
            "(дефолт true); входит в enum безусловно (ADR-051 §4)."
        ),
    )


class AgentApprovalRequest(StrictModel):
    """Тело `POST /v1/agent/runs/{runId}/approval` — passthrough к Hermes (ADR-045 §3).

    Значения `choice` — внешний контракт Hermes; control plane проксирует тело as-is.
    """

    choice: Literal["once", "session", "always", "deny"] = Field(
        description="Решение по запросу подтверждения: `once`|`session`|`always`|`deny`."
    )
