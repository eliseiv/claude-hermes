"""Chat schemas for /v1/chat/run and /v1/chat/tool-result (chat-orchestrator/02)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from app.config import get_settings
from app.schemas.common import StrictModel

# Allowed mediaType values per attachment class (ADR-020, 05-security.md; Q-020-1 extension).
# Fixed in code as a server-side allowlist (not a denylist) — anything else => 422.
AttachmentMediaType = Literal[
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
]


class AttachmentIn(StrictModel):
    """Вложение в base64. Только inline base64 — ссылки (URL) не принимаются."""

    type: Literal["image", "document", "text"] = Field(
        description="Класс вложения: `image` (фото), `document` (PDF) или `text` (текстовый файл)."
    )
    mediaType: AttachmentMediaType = Field(
        description=(
            "MIME-тип содержимого из allowlist: `image/jpeg|png|gif|webp`, `application/pdf`, "
            "`text/plain|markdown|csv`, `application/json`. Вне списка → 422."
        )
    )
    filename: str | None = Field(
        default=None,
        max_length=512,
        description="Имя файла для человекочитаемой разметки (особенно для `text`-вложений).",
    )
    data: str = Field(
        min_length=1,
        description="Содержимое файла в base64. Только inline base64 — URL запрещены.",
    )


class ChatRunRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    projectId: str | None = Field(
        default=None,
        description=(
            "Идентификатор проекта. Опционален: без него создаётся чат без проекта. Если "
            "указан — должен быть непустой строкой. Фиксируется при создании сессии; при "
            "продолжении берётся из сессии, поле запроса игнорируется."
        ),
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
    attachments: list[AttachmentIn] | None = Field(
        default=None,
        description=(
            "Вложения в base64 (фото/PDF/текст), отправляемые модели только в первом "
            "сообщении. Опционально. Только base64, URL запрещены. В `/v1/chat/tool-result` не "
            "принимаются."
        ),
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Опциональный контекст клиента (например, локаль). Ограничен по размеру.",
    )

    @model_validator(mode="after")
    def _check_sizes(self) -> ChatRunRequest:
        # ADR-022: projectId is optional (default None) → «чистый чат». When present it must be a
        # non-empty string (a blank projectId is rejected rather than silently treated as NULL).
        if self.projectId is not None and not self.projectId.strip():
            raise ValueError("projectId must be a non-empty string when provided")
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


class ToolResultItem(StrictModel):
    """Один элемент батча tool-результатов (ADR-025)."""

    toolCallId: uuid.UUID = Field(
        description="Идентификатор вызова инструмента — равен `toolCalls[].id` из `/v1/chat/run`."
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Результат исполнения инструмента. В элементе ровно одно из `result`/`error`.",
    )
    error: ToolErrorBody | None = Field(
        default=None,
        description="Ошибка исполнения инструмента. В элементе ровно одно из `result`/`error`.",
    )

    @model_validator(mode="after")
    def _check_one_of(self) -> ToolResultItem:
        if (self.result is None) == (self.error is None):
            raise ValueError("exactly one of result/error is required per item")
        if self.result is not None:
            import json

            settings = get_settings()
            if len(json.dumps(self.result).encode("utf-8")) > settings.size_limit_tool_result:
                raise ValueError("result exceeds size limit")
        return self


class ChatToolResultRequest(StrictModel):
    """Приём результата(ов) tools (ADR-025).

    Батч-форма (`results[]`) — рекомендуемая: результаты на все `toolCalls[]` одного хода.
    Одиночная форма (`toolCallId` + `result|error` на верхнем уровне) — **deprecated**,
    обратная совместимость; нормализуется в батч из одного элемента (`normalized_results`).
    Указывается ровно одна из двух форм.
    """

    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    sessionId: uuid.UUID = Field(
        description="Идентификатор сессии, в рамках которой шёл tool-loop."
    )
    # Батч-форма (рекомендуемая, ADR-025).
    results: list[ToolResultItem] | None = Field(
        default=None,
        description=(
            "Результаты на один или несколько tool-вызовов одного хода. Рекомендуемая форма для "
            "parallel tool use. В каждом элементе ровно одно из `result`/`error`."
        ),
    )
    # Одиночная форма (deprecated, обратная совместимость).
    toolCallId: uuid.UUID | None = Field(
        default=None,
        description=(
            "DEPRECATED (используйте `results[]`). Идентификатор вызова инструмента — равен "
            "`toolCall.id` из ответа `/v1/chat/run`."
        ),
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="DEPRECATED (одиночная форма). Результат исполнения инструмента.",
    )
    error: ToolErrorBody | None = Field(
        default=None,
        description="DEPRECATED (одиночная форма). Ошибка исполнения инструмента.",
    )

    @model_validator(mode="after")
    def _check(self) -> ChatToolResultRequest:
        has_batch = self.results is not None
        has_single = (
            self.toolCallId is not None or self.result is not None or self.error is not None
        )
        if has_batch == has_single:
            raise ValueError(
                "exactly one of batch form (results) or single form "
                "(toolCallId + result/error) is required"
            )
        if has_batch:
            if not self.results:
                raise ValueError("results must be a non-empty list")
            seen: set[uuid.UUID] = set()
            for item in self.results:
                # Duplicate toolCallId within one batch → 422 (ADR-025 idempotency rules).
                if item.toolCallId in seen:
                    raise ValueError("duplicate toolCallId in batch")
                seen.add(item.toolCallId)
            return self
        # Single (deprecated) form: validate exactly one of result/error + size.
        if self.toolCallId is None:
            raise ValueError("toolCallId is required in single form")
        if (self.result is None) == (self.error is None):
            raise ValueError("exactly one of result/error is required")
        if self.result is not None:
            import json

            settings = get_settings()
            if len(json.dumps(self.result).encode("utf-8")) > settings.size_limit_tool_result:
                raise ValueError("result exceeds size limit")
        return self

    def normalized_results(self) -> list[ToolResultItem]:
        """Normalize to a batch list (ADR-025): single form → list of one. Order preserved."""
        if self.results is not None:
            return self.results
        assert self.toolCallId is not None  # noqa: S101 - guaranteed by _check
        return [ToolResultItem(toolCallId=self.toolCallId, result=self.result, error=self.error)]


class ToolCallSchema(StrictModel):
    id: str = Field(
        description="Идентификатор вызова инструмента. Возвращается клиентом в `toolCallId`."
    )
    name: str = Field(
        description="Имя инструмента для исполнения на устройстве (например, `files.read`)."
    )
    args: dict[str, Any] = Field(description="Аргументы вызова инструмента.")


class ServerToolExecutionSchema(StrictModel):
    """Одно server-side выполнение, выполненное backend за этот вызов /chat/run (ADR-028)."""

    toolCallId: str = Field(
        description=(
            "Доменный идентификатор вызова инструмента (uuid4 = `tool_calls.id`) этого "
            "server-side выполнения (ADR-030). Совпадает с `toolCallId` соответствующего "
            "tool-шага в `GET /v1/chats/{id}` → `steps[].payload.toolCallId` (нормативный "
            "инвариант корреляции). Тот же домен id, что у client-side `toolCalls[].id`; "
            "НЕ provider-`toolu_...` (ADR-008)."
        )
    )
    toolName: str = Field(
        description=(
            "Доменное имя инструмента с точкой (например `time.now`, `site.write_file`). "
            "Совпадает с `name` из `/v1/tools` и `toolName` из `GET /v1/chats/{id}/steps`."
        )
    )
    status: Literal["completed", "errored"] = Field(
        description=(
            "Итог выполнения: `completed` (успех) или `errored` (инструмент вернул ошибку; ход "
            "при этом не падает)."
        )
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Компактный человекочитаемый итог (≤120 символов). НЕ raw-результат: без путей, URL, "
            "имён превью-файлов со signed-token и иных чувствительных данных. Полный результат — "
            "только в истории `GET /v1/chats/{id}` (ADR-024)."
        ),
    )


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
    "UI: generic-сообщение «недоступно», лог/ретрай.\n"
    "- `max_tokens` — ответ модели обрезан лимитом output-токенов. В отличие от прочих "
    "причин — `usage`/`messageStepId`/`stepId` присутствуют, кредит не списан, `toolCall(s)` "
    "не отдаются. UI: повторить/сократить запрос."
)


class ChatResponse(StrictModel):
    """Ответ chat-endpoint: три взаимоисключающих состояния по полю `status`.

    - `status=assistant_message`: есть `assistantMessage`, `usage`; нет `toolCall(s)`,
      `blockReason`.
    - `status=tool_call`: есть `toolCalls[]` (все client-side вызовы хода) и `toolCall`
      (= `toolCalls[0]`, deprecated), `usage`; `assistantMessage` опционален — присутствует, если
      модель выдала текст вместе с tool_use (текст того же шага); нет `blockReason`.
    - `status=blocked`: есть `blockReason`; нет `toolCall(s)`. При `blockReason=max_tokens`
      (ADR-025) `usage`/`messageStepId`/`stepId`/`assistantMessage` присутствуют (ход обрезан после
      начала генерации); при policy-blocked все они `null`.

    `messageStepId`/`stepId` присутствуют при `assistant_message`/`tool_call` и при
    `blocked`+`max_tokens`; `null` при policy-`blocked` (шаг/ход не создаются).

    `serverTools` (ADR-028) — server-side выполнения (`site.*`/`time.now`) этого вызова; всегда
    присутствует (возможно `[]`). Пустой при policy-`blocked`; может быть НЕпустым при
    `blocked`+`max_tokens`.
    """

    status: Literal["assistant_message", "tool_call", "blocked"] = Field(
        description="Состояние ответа: `assistant_message` | `tool_call` | `blocked`.",
    )
    sessionId: uuid.UUID = Field(
        description="Идентификатор сессии диалога (для последующих вызовов)."
    )
    messageStepId: uuid.UUID | None = Field(
        default=None,
        description=(
            "Идентификатор хода для синхронизации с историей чата. Один на сообщение, "
            "общий для всех раундов tool-loop. Совпадает с `messageStepId` шагов хода в "
            "`GET /v1/chats/{id}`. `null` при `status=blocked`."
        ),
    )
    stepId: uuid.UUID | None = Field(
        default=None,
        description=(
            "Идентификатор конкретного шага, который представляет этот ответ. Совпадает с "
            "`id` соответствующего шага в `GET /v1/chats/{id}`. `null` при `status=blocked`."
        ),
    )
    assistantMessage: str | None = Field(
        default=None,
        description=(
            "Текст ответа ассистента. При `status=assistant_message` — финальный ответ. При "
            "`status=tool_call` — опционально текст того же шага, если модель выдала его вместе "
            "с вызовом инструмента (иначе `null`). При `status=blocked` — `null`."
        ),
    )
    toolCalls: list[ToolCallSchema] | None = Field(
        default=None,
        description=(
            "ВСЕ client-side вызовы инструментов текущего хода (parallel tool use, ADR-025). "
            "Присутствует только при `status=tool_call`. Клиент обязан исполнить и вернуть "
            "результаты на все элементы через `/v1/chat/tool-result`. Server-side `site.*` сюда "
            "не входят."
        ),
    )
    toolCall: ToolCallSchema | None = Field(
        default=None,
        description=(
            "DEPRECATED (читайте `toolCalls[]`). Первый client-side вызов хода (= `toolCalls[0]`). "
            "Присутствует при `status=tool_call`. На мульти-tool ходе неполон — continuation "
            "сломается, если читать только его."
        ),
    )
    blockReason: str | None = Field(default=None, description=_BLOCK_REASON_DOC)
    usage: dict[str, Any] | None = Field(
        default=None,
        description="Потребление токенов модели (при `assistant_message`/`tool_call`).",
    )
    serverTools: list[ServerToolExecutionSchema] = Field(
        default_factory=list,
        description=(
            "Server-side инструменты (`site.*`, `time.now`), выполненные backend за ЭТОТ вызов "
            "`/chat/run` (или один `/chat/tool-result`-continuation), в порядке выполнения "
            "(ADR-028). Присутствует всегда: пустой `[]` — server-side не выполнялись (в т.ч. "
            "policy-`blocked`, где tool-loop не запускался); при `blocked=max_tokens` может быть "
            "НЕпустым (раунды до обрыва). client-side вызовы здесь НЕ перечисляются — они в "
            "`toolCalls[]`. Информационное поле: на биллинг не влияет (ADR-006)."
        ),
    )
