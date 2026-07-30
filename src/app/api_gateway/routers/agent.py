"""Agent routes: /v1/agent/* — proxy to the user's Hermes instance (agent-proxy/02, ADR-045/047).

Thin client-facing contour (auth: X-API-Key + X-User-Id, ADR-044). The launch endpoint policy-gates
then proxies ``POST /v1/runs``; the events endpoint relays the Hermes SSE stream and bills the
wallet on ``run.completed`` (usage-based, idempotent by runId, ADR-047); approval/stop are
passthroughs. All instance addressing is by the subject's ``X-User-Id`` (RBAC: a foreign run is
unreachable, agent-proxy/06-rbac.md).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from app.agent_proxy.broker import parse_cursor
from app.agent_proxy.service import AgentProxyService
from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_agent_proxy_service
from app.errors import RateLimitedError
from app.schemas.agent import (
    AgentApprovalRequest,
    AgentPendingApproval,
    AgentResumeRequest,
    AgentRunRequest,
    AgentRunResponse,
    AgentRunStateResponse,
    AgentRunStateUsage,
)

router = APIRouter(prefix="/v1/agent", tags=["Agent"])

_AgentService = Annotated[AgentProxyService, Depends(get_agent_proxy_service)]


async def _rate_limit(user_id: uuid.UUID) -> None:
    """Shared client rate limit (the same contour as `chats`) → 429 on excess."""
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


_RUN_REQUEST_EXAMPLES = {
    "launch": {
        "summary": "Запуск прогона",
        "value": {
            "message": "Спланируй и собери лендинг по моим заметкам в памяти.",
            "sessionId": "3f1c2a7e-9b54-4d2e-8a11-6c0d5e7f1a23",
            "model": None,
        },
    },
    "new_session": {
        "summary": "Новый диалог (без sessionId)",
        "value": {"message": "Привет! Что ты умеешь?"},
    },
}

_RUN_RESPONSE_EXAMPLES = {
    "queued": {
        "summary": "Прогон принят (202)",
        "value": {"status": "queued", "runId": "run_8a1f...", "blockReason": None},
    },
    "blocked": {
        "summary": "Блокировка по бизнес-правилам (HTTP 200)",
        "description": (
            "Нет активной подписки или исчерпан баланс кредитов. Успешный ответ 200, не ошибка. "
            "Прогон не запущен, инстанс не разбужен, кредит не списан."
        ),
        "value": {"status": "blocked", "runId": None, "blockReason": "credits_empty"},
    },
}

_RUN_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Блокировка по бизнес-правилам (HTTP 200, ADR-004).",
        "model": AgentRunResponse,
        "content": {
            "application/json": {"examples": {"blocked": _RUN_RESPONSE_EXAMPLES["blocked"]}}
        },
    },
    202: {
        "description": "Прогон принят; стримьте события через `GET .../events`.",
        "model": AgentRunResponse,
        "content": {"application/json": {"examples": {"queued": _RUN_RESPONSE_EXAMPLES["queued"]}}},
    },
    401: {"description": "Нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`."},
    502: {"description": "Инстанс недоступен / `ensure_running` не поднял / Hermes 5xx."},
}


@router.post(
    "/run",
    response_model=AgentRunResponse,
    summary="Запустить автономный прогон агента",
    description=(
        "Policy-gate (подписка + кредиты) → `ensure_running` → прокси `POST /v1/runs` к "
        "персональному Hermes-инстансу. При блокировке по бизнес-правилам — HTTP 200 с полем "
        "`blockReason` (прогон не запускается, кредит не списывается). При успехе — HTTP 202 с "
        "`runId`; события прогона стримятся через `GET /v1/agent/runs/{runId}/events`."
    ),
    responses=_RUN_RESPONSES,
)
async def agent_run(
    current: CurrentUser,
    service: _AgentService,
    response: Response,
    body: Annotated[AgentRunRequest, Body(openapi_examples=_RUN_REQUEST_EXAMPLES)],
) -> AgentRunResponse:
    result = await service.run(
        user_id=current.user_id,
        message=body.message,
        session_id=body.sessionId,
        model=body.model,
    )
    if result.blocked:
        # Business block is a 200 success (ADR-004), not an error.
        response.status_code = status.HTTP_200_OK
        return AgentRunResponse(status="blocked", runId=None, blockReason=result.block_reason)
    response.status_code = status.HTTP_202_ACCEPTED
    return AgentRunResponse(status=result.status or "queued", runId=result.run_id, blockReason=None)


@router.get(
    "/runs/{run_id}/events",
    summary="Стримить события прогона (SSE)",
    description=(
        "Ретранслирует события Hermes-инстанса как Server-Sent Events: "
        "`run.queued`/`run.running`/`message.delta`/`tool.started`/`tool.completed`/"
        "`approval.request`/`usage.delta`/`run.completed`/`run.failed`. На `run.completed{usage}` "
        "кредиты списываются по реальному usage (идемпотентно по `runId`); на `run.failed` — без "
        "списания. На `approval.request` ответьте через `POST /v1/agent/runs/{runId}/approval`.\n\n"
        "ADR-064 (под флагом `AGENT_INCREMENTAL_BILLING_ENABLED`): на каждом `usage.delta` кредиты "
        "списываются пошагово (кумулятивно, без долга); при исчерпании баланса прогон "
        "останавливается и приходит синтетическое терминальное событие `run.paused` "
        "(`reason=credits_exhausted`) — `run.completed` НЕ следует. После пополнения возобновите "
        "через `POST /v1/agent/runs/{runId}/resume`."
    ),
    responses={
        200: {
            "description": "Поток событий (text/event-stream).",
            "content": {"text/event-stream": {}},
        },
        400: {"description": "Невалидный `?afterSeq=` (не целое / отрицательное)."},
        401: {"description": "Нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`."},
        404: {"description": "Прогон не найден или принадлежит другому пользователю (RBAC)."},
        502: {"description": "Инстанс недоступен / поток событий Hermes недоступен."},
    },
)
async def agent_run_events(
    run_id: str,
    current: CurrentUser,
    service: _AgentService,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after_seq: Annotated[str | None, Query(alias="afterSeq")] = None,
) -> StreamingResponse:
    # ADR-067 §3.2: resolve the reconnect cursor BEFORE the stream starts, so an invalid ?afterSeq=
    # is a plain 400 instead of an error inside an already-open text/event-stream response, where
    # the client would see a truncated stream rather than a status code. `after_seq` is typed as a
    # STRING and validated here rather than declared `int`: FastAPI would answer 422 for a
    # non-integer, and the contract says 400.
    cursor = parse_cursor(last_event_id=last_event_id, after_seq=after_seq)
    # RBAC BEFORE the response starts, for exactly the reason the cursor is parsed here: Starlette
    # commits `http.response.start` (status 200) before the first item is pulled from the generator,
    # so a NotFoundError raised inside it can never become a 404 — it becomes a RuntimeError over an
    # already-started response, and under BaseHTTPMiddleware it wedges the connection.
    await service.assert_run_owner(user_id=current.user_id, run_id=run_id)
    stream = service.stream_events(user_id=current.user_id, run_id=run_id, cursor=cursor)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_STATE_RESPONSE_EXAMPLES = {
    "waiting_approval": {
        "summary": "Прогон ждёт подтверждения",
        "value": {
            "runId": "run_7f3c1a",
            "sessionId": "b2b4b0e6-8f2a-4b5d-9d31-6a1c2f0e9a77",
            "status": "waiting_approval",
            "resultText": "Нашёл три подходящих варианта, свожу в таблицу…",
            "lastTool": "web_search",
            "pendingApproval": {"tool": "file_write", "preview": "report.md (12 KB)"},
            "blockReason": None,
            "usage": {"inputTokens": 18432, "outputTokens": 2110},
            "updatedAt": "2026-07-28T11:42:07Z",
            "continuedFrom": None,
        },
    },
    "paused": {
        "summary": "Пауза по исчерпанию кредитов",
        "description": (
            "`blockReason` здесь — причина ПАУЗЫ прогона (`agent_runs.paused_reason`), а не "
            "policy-enum ADR-004. После пополнения — `POST /v1/agent/runs/{runId}/resume`."
        ),
        "value": {
            "runId": "run_7f3c1a",
            "sessionId": "b2b4b0e6-8f2a-4b5d-9d31-6a1c2f0e9a77",
            "status": "paused",
            "resultText": "Собрал первую часть отчёта…",
            "lastTool": "file_read",
            "pendingApproval": None,
            "blockReason": "credits_exhausted",
            "usage": {"inputTokens": 51200, "outputTokens": 8300},
            "updatedAt": "2026-07-28T11:44:19Z",
            "continuedFrom": None,
        },
    },
    "empty": {
        "summary": "Снапшот ещё не заполнен (200 с дефолтами)",
        "description": (
            "Строка прогона есть, но writer не отработал ни одного события (никто не подписывался "
            "на `/events`). Это не ошибка — 200 с пустыми значениями."
        ),
        "value": {
            "runId": "run_7f3c1a",
            "sessionId": "b2b4b0e6-8f2a-4b5d-9d31-6a1c2f0e9a77",
            "status": "running",
            "resultText": "",
            "lastTool": None,
            "pendingApproval": None,
            "blockReason": None,
            "usage": {"inputTokens": 0, "outputTokens": 0},
            "updatedAt": "2026-07-28T11:40:00Z",
            "continuedFrom": None,
        },
    },
}

_STATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Снапшот состояния прогона. Отдаётся и когда снапшот ещё пуст (дефолты). "
            "`200 {status:blocked}` на этом маршруте не возникает — policy-gate не вызывается."
        ),
        "model": AgentRunStateResponse,
        "content": {"application/json": {"examples": _STATE_RESPONSE_EXAMPLES}},
    },
    401: {"description": "Нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`."},
    404: {
        "description": (
            "Прогон не найден или принадлежит другому пользователю (RBAC; 403 не возвращается "
            "никогда). Сюда же попадают прогоны, запущенные до внедрения снапшота."
        )
    },
    429: {"description": "Превышен общий клиентский rate-limit."},
}


@router.get(
    "/runs/{run_id}/state",
    response_model=AgentRunStateResponse,
    summary="Снапшот состояния прогона",
    description=(
        "Возвращает состояние прогона ИЗ БАЗЫ: статус, накопленный текст ответа, последний "
        "инструмент, ожидаемое подтверждение, расход токенов и время последней записи состояния. "
        "Назначение — восстановление UI после kill приложения, смены сети или гибернации "
        "инстанса: поток `/events` события не сохраняет, поэтому переподключение к закрытому "
        "стриму отдаёт 200 без событий.\n\n"
        "Строго read-only (ADR-066 §5): гибернированный контейнер НЕ будится (`ensure_running` не "
        "вызывается), обращения к Hermes нет, кредиты не списываются, policy-gate не "
        "выполняется.\n\n"
        "Ограничения: `resultText` активного прогона отстаёт, пока к `/events` никто не подключён "
        "(рецепт — при `running`/`waiting_approval` переподключиться к `/events` и догнать поток; "
        "свежесть видна по `updatedAt`); прогоны, запущенные до внедрения снапшота, отдают 404; "
        "`stopped` после `POST …/stop` — eventually consistent."
    ),
    responses=_STATE_RESPONSES,
)
async def agent_run_state(
    run_id: str,
    current: CurrentUser,
    service: _AgentService,
) -> AgentRunStateResponse:
    """Read-only снапшот прогона: только SELECT из `agent_runs` + `agent_run_snapshots`.

    Инварианты маршрута (ADR-066 §5): без `ensure_running` (контейнер не будится — иначе фоновый
    опрос состояния стоил бы cold-start на каждый тик), без обращения к Hermes, без списаний, без
    policy-gate. Чужой/несуществующий прогон → 404 (никогда 403).
    """
    await _rate_limit(current.user_id)
    view = await service.get_state(user_id=current.user_id, run_id=run_id)
    pending = view.pending_approval
    return AgentRunStateResponse(
        runId=view.run_id,
        sessionId=view.session_id,
        status=view.status,
        resultText=view.result_text,
        lastTool=view.last_tool,
        pendingApproval=(
            AgentPendingApproval(tool=pending.get("tool"), preview=pending.get("preview"))
            if pending is not None
            else None
        ),
        blockReason=view.block_reason,
        usage=AgentRunStateUsage(inputTokens=view.input_tokens, outputTokens=view.output_tokens),
        updatedAt=view.updated_at,
        continuedFrom=view.continued_from,
    )


_RESUME_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Блокировка по бизнес-правилам (HTTP 200, ADR-004): баланс пуст/долг.",
        "model": AgentRunResponse,
    },
    202: {
        "description": (
            "Прогон возобновлён: НОВЫЙ `runId` + `continuedFrom` (исходный приостановленный "
            "`runId`). Подпишитесь на `GET /v1/agent/runs/{runId}/events` нового прогона."
        ),
        "model": AgentRunResponse,
    },
    401: {"description": "Нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`."},
    404: {"description": "Прогон не найден или принадлежит другому пользователю (RBAC)."},
    409: {
        "description": (
            "`run_not_resumable` (не в статусе paused/resumed) | `resume_in_progress` "
            "(конкурентное возобновление в процессе — повторите) | `session_expired` "
            "(контекст сессии Hermes недоступен)."
        )
    },
    502: {"description": "Инстанс недоступен / запуск продолжения не удался (CAS откачен)."},
}


@router.post(
    "/runs/{run_id}/resume",
    response_model=AgentRunResponse,
    summary="Возобновить приостановленный прогон",
    description=(
        "ADR-064: возобновляет прогон, остановленный из-за исчерпания баланса (`run.paused`). "
        "После пополнения запускает НОВЫЙ прогон-продолжение в той же Hermes-сессии (память и "
        "контекст сохраняются) и возвращает HTTP 202 с новым `runId` и `continuedFrom`. Если "
        "баланс всё ещё пуст/в долге — HTTP 200 с `blockReason` (продолжение не запускается). "
        "`session_id` клиенту хранить не нужно — он резолвится по приостановленному `runId`."
    ),
    responses=_RESUME_RESPONSES,
)
async def agent_run_resume(
    run_id: str,
    current: CurrentUser,
    service: _AgentService,
    response: Response,
    body: AgentResumeRequest,
) -> AgentRunResponse:
    result = await service.resume(user_id=current.user_id, run_id=run_id, message=body.message)
    if result.blocked:
        # Business block is a 200 success (ADR-004), not an error.
        response.status_code = status.HTTP_200_OK
        return AgentRunResponse(status="blocked", runId=None, blockReason=result.block_reason)
    response.status_code = status.HTTP_202_ACCEPTED
    return AgentRunResponse(
        status="running",
        runId=result.run_id,
        continuedFrom=result.continued_from,
        blockReason=None,
    )


@router.post(
    "/runs/{run_id}/approval",
    summary="Ответить на запрос подтверждения",
    description=(
        "Passthrough тела `{choice}` в `POST /v1/runs/{runId}/approval` Hermes-инстанса. "
        "Разблокирует прогон, ожидающий `approval.request`."
    ),
    responses={
        401: {"description": "Нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`."},
        502: {"description": "Инстанс недоступен / запрос к Hermes не выполнен."},
    },
)
async def agent_run_approval(
    run_id: str,
    current: CurrentUser,
    service: _AgentService,
    body: AgentApprovalRequest,
) -> dict[str, Any]:
    return await service.approval(user_id=current.user_id, run_id=run_id, body=body.model_dump())


@router.post(
    "/runs/{run_id}/stop",
    summary="Остановить прогон",
    description="Passthrough в `POST /v1/runs/{runId}/stop` Hermes-инстанса.",
    responses={
        401: {"description": "Нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`."},
        502: {"description": "Инстанс недоступен / запрос к Hermes не выполнен."},
    },
)
async def agent_run_stop(
    run_id: str,
    current: CurrentUser,
    service: _AgentService,
) -> dict[str, Any]:
    return await service.stop(user_id=current.user_id, run_id=run_id)
