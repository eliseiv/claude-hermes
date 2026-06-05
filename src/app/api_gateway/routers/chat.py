"""Chat routes: /v1/chat/run, /v1/chat/tool-result (chat-orchestrator/02)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request

from app.api_gateway.rate_limit import enforce_chat_limits
from app.chat.orchestrator import ChatOrchestrator, ChatRunOut
from app.deps import (
    CurrentUser,
    client_ip,
    get_orchestrator,
    require_owner,
)
from app.errors import RateLimitedError
from app.observability.context import set_session_id
from app.schemas.chat import (
    ChatResponse,
    ChatRunRequest,
    ChatToolResultRequest,
    ToolCallSchema,
)

router = APIRouter(prefix="/v1/chat", tags=["Chat"])

# --- Согласованные id для end-to-end tool-loop примеров (run -> tool_call -> tool-result) ---
_SESSION_ID = "3f1c2a7e-9b54-4d2e-8a11-6c0d5e7f1a23"
_TOOL_CALL_ID = "a7b9c1d2-3e4f-5061-7283-94a5b6c7d8e9"
# Один messageStepId на весь ход (стабилен через tool-loop); stepId — у каждого шага свой.
_MESSAGE_STEP_ID = "b1e2d3c4-5f60-4718-9a2b-3c4d5e6f7081"
_STEP_ID_TOOL_CALL = "c2f3e4d5-6071-4829-ab3c-4d5e6f708192"
_STEP_ID_FINAL = "d3041526-7182-493a-bc4d-5e6f708192a3"
_STEP_ID_TOOL_RESULT_FINAL = "e4152637-8293-4a4b-cd5e-6f708192a3b4"

# Tiny valid base64-encoded 1x1 PNG for the Swagger attachment example (not a real photo).
_EXAMPLE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

_RUN_RESPONSE_EXAMPLES = {
    "assistant_message": {
        "summary": "Ответ ассистента (финал)",
        "description": "Модель ответила текстом, генерация списана. Без toolCall/blockReason.",
        "value": {
            "status": "assistant_message",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_FINAL,
            "assistantMessage": "Конечно! Вот краткое содержание вашего файла…",
            "usage": {"inputTokens": 1240, "outputTokens": 320},
        },
    },
    "tool_call": {
        "summary": "Запрос на вызов инструмента",
        "description": (
            "Ассистент просит клиента выполнить инструмент на устройстве (здесь — `files.read`). "
            "Клиент исполняет его и возвращает результат через `POST /v1/chat/tool-result`, "
            "передав тот же id в `toolCallId`."
        ),
        "value": {
            "status": "tool_call",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_CALL,
            "toolCall": {
                "id": _TOOL_CALL_ID,
                "name": "files.read",
                "args": {"path": "/Documents/notes.md"},
            },
            "usage": {"inputTokens": 980, "outputTokens": 64},
        },
    },
    "blocked": {
        "summary": "Блокировка по бизнес-правилам (HTTP 200)",
        "description": (
            "Баланс кредитов исчерпан. Это успешный ответ 200, а не ошибка. UI показывает "
            "баланс и предлагает пополнение/подписку."
        ),
        "value": {
            "status": "blocked",
            "sessionId": _SESSION_ID,
            "messageStepId": None,
            "stepId": None,
            "blockReason": "credits_empty",
        },
    },
}

_RUN_REQUEST_EXAMPLES = {
    "clean_chat": {
        "summary": "Чистый чат без projectId",
        "description": "Без `projectId` сессия создаётся без проекта.",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "message": "Объясни, как работает async/await в Python.",
            "mode": "credits",
        },
    },
    "credits_mode": {
        "summary": "Запуск шага диалога, режим credits",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "projectId": "my-ios-project",
            "sessionId": _SESSION_ID,
            "message": "Прочитай файл notes.md и сделай краткое содержание.",
            "mode": "credits",
            "context": {"locale": "ru-RU"},
        },
    },
    "with_attachment": {
        "summary": "Сообщение с вложением, фото",
        "description": (
            "Поле `attachments` принимает фото, PDF и текстовые файлы в base64. `type` — класс "
            "вложения, `mediaType` — MIME из allowlist, `data` — содержимое в base64. Вложения "
            "отправляются модели только в первом сообщении; в `/v1/chat/tool-result` не "
            "принимаются. Только base64, URL запрещены."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "projectId": "my-ios-project",
            "message": "Что на этом фото?",
            "mode": "credits",
            "attachments": [
                {
                    "type": "image",
                    "mediaType": "image/png",
                    "filename": "photo.png",
                    "data": _EXAMPLE_PNG_B64,
                }
            ],
        },
    },
}

_TOOL_RESULT_RESPONSE_EXAMPLES = {
    "assistant_message": {
        "summary": "Финал tool-loop",
        "description": "После получения результата инструмента модель выдала итоговый ответ.",
        "value": {
            "status": "assistant_message",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_RESULT_FINAL,
            "assistantMessage": "В файле notes.md перечислены задачи на неделю…",
            "usage": {"inputTokens": 1500, "outputTokens": 210},
        },
    },
}

_TOOL_RESULT_REQUEST_EXAMPLES = {
    "result": {
        "summary": "Продолжение tool-loop: успешный результат",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "toolCallId": _TOOL_CALL_ID,
            "result": {"content": "# Заметки\n- задача 1\n- задача 2"},
        },
    },
    "error": {
        "summary": "Продолжение tool-loop: ошибка инструмента",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "toolCallId": _TOOL_CALL_ID,
            "error": {"code": "not_found", "message": "Файл не найден на устройстве"},
        },
    },
}

_CHAT_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"description": "Невалидная схема запроса."},
    429: {"description": "Жёсткое превышение rate limit (мягкое приходит как blocked, HTTP 200)."},
}


def _to_response(out: ChatRunOut) -> ChatResponse:
    set_session_id(str(out.session_id))
    tool_call = (
        ToolCallSchema(id=out.tool_call.id, name=out.tool_call.name, args=out.tool_call.args)
        if out.tool_call is not None
        else None
    )
    return ChatResponse(
        status=out.status,
        sessionId=out.session_id,
        messageStepId=out.message_step_id,
        stepId=out.step_id,
        assistantMessage=out.assistant_message,
        toolCall=tool_call,
        blockReason=out.block_reason,
        usage=out.usage,
    )


@router.post(
    "/run",
    response_model=ChatResponse,
    summary="Запустить шаг диалога",
    description=(
        "Принимает сообщение пользователя и возвращает одно из трёх состояний: "
        "`assistant_message` (готовый ответ), `tool_call` (выполните инструмент на устройстве "
        "и пришлите результат в `/v1/chat/tool-result`) или `blocked`. "
        "Блокировки приходят с HTTP 200 и полем `blockReason`; технические ошибки — `4xx`/`5xx`. "
        "Необязательный заголовок `X-Device-Id` задаёт устройство для rate limit."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _RUN_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_run(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_orchestrator)],
    body: Annotated[ChatRunRequest, Body(openapi_examples=_RUN_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    out = await orchestrator.run(
        user_id=current.user_id,
        project_id=body.projectId,
        session_id=body.sessionId,
        message=body.message,
        mode=body.mode,
        assistant_mode=body.assistantMode,
        attachments=body.attachments,
    )
    return _to_response(out)


@router.post(
    "/tool-result",
    response_model=ChatResponse,
    summary="Передать результат инструмента",
    description=(
        "Пришлите результат инструмента из предыдущего `tool_call`: ровно одно из полей "
        "`result` или `error`. `toolCallId` обязан совпадать с `toolCall.id` из `/v1/chat/run`. "
        "Обычно возвращает `assistant_message`, может снова запросить `tool_call`. "
        "Блокировки приходят с HTTP 200 и полем `blockReason`; технические ошибки — `4xx`/`5xx`."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _TOOL_RESULT_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_tool_result(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_orchestrator)],
    body: Annotated[ChatToolResultRequest, Body(openapi_examples=_TOOL_RESULT_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    out = await orchestrator.tool_result(
        user_id=current.user_id,
        session_id=body.sessionId,
        tool_call_id=body.toolCallId,
        result=body.result,
        error=body.error.model_dump() if body.error is not None else None,
    )
    return _to_response(out)
