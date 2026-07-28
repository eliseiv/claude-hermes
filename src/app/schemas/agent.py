"""Agent-proxy schemas for /v1/agent/* (agent-proxy/02-api-contracts.md, ADR-045/047).

Request/response models of the client-facing contour. The SSE event stream
(GET /v1/agent/runs/{runId}/events) and the approval/stop passthrough bodies follow Hermes'
external contract and are relayed as-is, so only the run-launch request, the run-launch response,
and the approval body are modelled here as strict Pydantic v2 schemas.
"""

from __future__ import annotations

import datetime
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
    continuedFrom: str | None = Field(
        default=None,
        description=(
            "ADR-064: при ответе на `POST /v1/agent/runs/{runId}/resume` — `run_id` "
            "приостановленного прогона, продолжением которого является новый `runId`. "
            "Отсутствует (`null`) для обычного запуска `POST /v1/agent/run`."
        ),
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


class AgentResumeRequest(StrictModel):
    """Тело `POST /v1/agent/runs/{runId}/resume` — возобновление прогона (ADR-064).

    `message` (опц.) — дополнительный ход пользователя, добавляемый при продолжении. Если не задан,
    прогон продолжается с восстановленным контекстом сессии без нового пользовательского ввода.
    """

    message: str | None = Field(
        default=None,
        description="Опциональный дополнительный ход пользователя при возобновлении.",
    )


class AgentPendingApproval(StrictModel):
    """Запрос подтверждения, которого ждёт прогон (ADR-066). `null` в ответе = не ждёт."""

    tool: str | None = Field(
        default=None,
        description="Имя инструмента, для которого запрошено подтверждение (или null).",
    )
    preview: str | None = Field(
        default=None,
        description="Краткое описание действия для показа пользователю (или null).",
    )


class AgentRunStateUsage(StrictModel):
    """Накопленный расход токенов прогона (ADR-066). Монотонен, не уменьшается."""

    inputTokens: int = Field(description="Накопленные входные токены прогона.")
    outputTokens: int = Field(description="Накопленные выходные токены прогона.")


class AgentRunStateResponse(StrictModel):
    """Ответ `GET /v1/agent/runs/{runId}/state` — снапшот состояния прогона из БД (ADR-066).

    Назначение — восстановление UI после kill приложения / смены сети / гибернации Hermes: поток
    `/events` события не персистит, поэтому переподключение к закрытому стриму отдаёт 200 без
    событий. Эндпоинт строго read-only: контейнер не будится, к Hermes обращения нет, списаний нет.
    """

    runId: str = Field(description="Идентификатор прогона (совпадает с путём запроса).")
    sessionId: str = Field(
        description=(
            "Hermes-сессия прогона; стабильна на всю цепочку продолжений. Информационно — для "
            "`resume` клиенту не нужна."
        )
    )
    status: Literal[
        "queued", "running", "waiting_approval", "paused", "completed", "failed", "stopped"
    ] = Field(
        description=(
            "Клиентский статус, производный от статуса прогона и наличия запроса подтверждения: "
            "`running` (в работе) | `waiting_approval` (ждёт ответа на `approval.request`) | "
            "`paused` (пауза по кредитам, см. `blockReason`) | `completed` | `failed` | `stopped` "
            "(остановлен через `POST …/stop`). `queued` в v1 не эмитится (forward-compat)."
        )
    )
    resultText: str = Field(
        description=(
            'Накопленный текст ответа агента (склейка `message.delta`); `""` если ничего не '
            "накоплено. Обрезан с сохранением начала до `AGENT_STATE_RESULT_TEXT_MAX_CHARS`. "
            "Может отставать: снапшот двигается, только пока кто-то подписан на `/events` — "
            "свежесть видна по `updatedAt`."
        )
    )
    lastTool: str | None = Field(
        default=None,
        description="Имя последнего инструмента (`tool.started`/`tool.completed`) или null.",
    )
    pendingApproval: AgentPendingApproval | None = Field(
        default=None,
        description=(
            "`{tool, preview}` — прогон ждёт ответа на запрос подтверждения; null — не ждёт. "
            "Снимается ответом на `POST …/approval`, следующим `tool.*` или терминальным событием."
        ),
    )
    blockReason: str | None = Field(
        default=None,
        description=(
            "Причина ПАУЗЫ прогона (в v1 единственное значение `credits_exhausted`); непусто "
            "только при `status=paused`. ⚠️ Это НЕ policy-enum `blockReason` из ADR-004 "
            "(`credits_empty`/`subscription_expired`/…): наборы значений не пересекаются, "
            "валидировать policy-enum'ом нельзя (ADR-066 §5)."
        ),
    )
    usage: AgentRunStateUsage = Field(description="Накопленный расход токенов прогона. Монотонен.")
    updatedAt: datetime.datetime = Field(
        description=(
            "Время последней записи состояния (ISO8601, UTC) — детектор устаревания. При активном "
            "прогоне без подписчика на `/events` не двигается; очистка контента по retention "
            "`updatedAt` не сдвигает."
        )
    )
    continuedFrom: str | None = Field(
        default=None,
        description=(
            "`runId` родительского (приостановленного) прогона для продолжения, созданного через "
            "`POST …/resume`; null у корневого прогона."
        ),
    )


class AgentApprovalRequest(StrictModel):
    """Тело `POST /v1/agent/runs/{runId}/approval` — passthrough к Hermes (ADR-045 §3).

    Значения `choice` — внешний контракт Hermes; control plane проксирует тело as-is.
    """

    choice: Literal["once", "session", "always", "deny"] = Field(
        description="Решение по запросу подтверждения: `once`|`session`|`always`|`deny`."
    )
