"""Chat schemas for /v1/chat/run and /v1/chat/tool-result (chat-orchestrator/02)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from app.config import get_settings
from app.schemas.common import StrictModel


class ChatRunRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    projectId: str = Field(
        min_length=1, description="Идентификатор проекта/приложения на стороне клиента."
    )
    sessionId: uuid.UUID | None = Field(
        default=None,
        description="Идентификатор сессии диалога. Если не задан — создаётся новая сессия.",
    )
    message: str = Field(min_length=1, description="Текст сообщения пользователя.")
    mode: Literal["credits", "byok"] = Field(
        description=(
            "Режим тарификации: `credits` (кредиты подписки) или `byok` (свой ключ Anthropic). "
            "Не путать с `assistantMode` (тип ассистента)."
        ),
    )
    assistantMode: Literal["chat", "code"] | None = Field(
        default=None,
        description=(
            "Тип ассистента: `chat` или `code`. Опционально; при отсутствии берётся дефолт из "
            "настроек пользователя, затем `chat`. Фиксируется при создании сессии. "
            "Ортогонален `mode` (оплата)."
        ),
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Опциональный контекст клиента (например, локаль). Ограничен по размеру.",
    )

    @model_validator(mode="after")
    def _check_sizes(self) -> ChatRunRequest:
        settings = get_settings()
        if len(self.message.encode("utf-8")) > settings.size_limit_message:
            raise ValueError("message exceeds size limit")
        if self.context is not None:
            import json

            if len(json.dumps(self.context).encode("utf-8")) > settings.size_limit_context:
                raise ValueError("context exceeds size limit")
        return self


class ToolErrorBody(StrictModel):
    code: str = Field(description="Машиночитаемый код ошибки исполнения инструмента.")
    message: str = Field(description="Человекочитаемое описание ошибки инструмента.")


class ChatToolResultRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    sessionId: uuid.UUID = Field(
        description="Идентификатор сессии, в рамках которой шёл tool-loop."
    )
    toolCallId: uuid.UUID = Field(
        description=(
            "Идентификатор вызова инструмента — равен `toolCall.id` из ответа `/v1/chat/run`."
        ),
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Результат исполнения инструмента. Указывается одно из `result`/`error`.",
    )
    error: ToolErrorBody | None = Field(
        default=None,
        description="Ошибка исполнения инструмента. Указывается ровно одно из `result`/`error`.",
    )

    @model_validator(mode="after")
    def _check(self) -> ChatToolResultRequest:
        if (self.result is None) == (self.error is None):
            raise ValueError("exactly one of result/error is required")
        if self.result is not None:
            import json

            settings = get_settings()
            if len(json.dumps(self.result).encode("utf-8")) > settings.size_limit_tool_result:
                raise ValueError("result exceeds size limit")
        return self


class ToolCallSchema(StrictModel):
    id: str = Field(
        description="Идентификатор вызова инструмента. Возвращается клиентом в `toolCallId`."
    )
    name: str = Field(
        description="Имя инструмента для исполнения на устройстве (например, `files.read`)."
    )
    args: dict[str, Any] = Field(description="Аргументы вызова инструмента.")


_BLOCK_REASON_DOC = (
    "Причина бизнес-блокировки (присутствует только при `status=blocked`). Значения:\n\n"
    "- `trial_used` — бесплатная пробная генерация использована, подписки нет. "
    "UI: предложить оформить подписку.\n"
    "- `subscription_required` — действие требует активной подписки, её нет. "
    "UI: экран оформления подписки.\n"
    "- `subscription_expired` — подписка была, но истекла/отозвана. UI: предложить продлить.\n"
    "- `credits_empty` — баланс кредитов исчерпан (режим `credits`). "
    "UI: показать баланс, предложить пополнение/подписку.\n"
    "- `byok_disabled` — режим `byok` выбран, но BYOK выключен пользователем. UI: включить BYOK.\n"
    "- `byok_invalid` — ключ BYOK отсутствует или невалиден. UI: добавить/исправить ключ.\n"
    "- `rate_limited` — мягкое превышение лимита оркестрации (жёсткое — `429`). "
    "UI: «слишком часто», предложить повторить позже.\n"
    "- `policy_denied` — общий fallback для непредвиденного состояния Policy Engine. "
    "UI: generic-сообщение «недоступно», лог/ретрай."
)


class ChatResponse(StrictModel):
    """Ответ chat-endpoint: три взаимоисключающих состояния по полю `status`.

    - `status=assistant_message`: есть `assistantMessage`, `usage`; нет `toolCall`, `blockReason`.
    - `status=tool_call`: есть `toolCall`, `usage`; нет `assistantMessage`, `blockReason`.
    - `status=blocked`: есть `blockReason`; нет `assistantMessage`, `toolCall`, `usage`.
    """

    status: Literal["assistant_message", "tool_call", "blocked"] = Field(
        description="Состояние ответа: `assistant_message` | `tool_call` | `blocked`.",
    )
    sessionId: uuid.UUID = Field(
        description="Идентификатор сессии диалога (для последующих вызовов)."
    )
    assistantMessage: str | None = Field(
        default=None, description="Текст ответа ассистента (только при `status=assistant_message`)."
    )
    toolCall: ToolCallSchema | None = Field(
        default=None, description="Запрос на вызов инструмента (только при `status=tool_call`)."
    )
    blockReason: str | None = Field(default=None, description=_BLOCK_REASON_DOC)
    usage: dict[str, Any] | None = Field(
        default=None,
        description="Потребление токенов модели (при `assistant_message`/`tool_call`).",
    )
