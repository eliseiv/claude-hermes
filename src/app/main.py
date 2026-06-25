"""FastAPI app factory: middleware, routers, exception handlers (api-gateway/03)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.api_gateway.middleware import (
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
    SizeLimitMiddleware,
)
from app.api_gateway.rate_limit import close_redis
from app.api_gateway.routers import (
    admin,
    agent,
    auth,
    billing_adapty,
    byok,
    chat,
    chats,
    health,
    models,
    policy,
    preferences,
    presets,
    preview,
    profile,
    token_purchase,
    tools,
    wallet,
    workspaces,
)
from app.auth.cleanup_reaper import run_cleanup_reaper
from app.config import get_settings
from app.db import dispose_engine
from app.errors import AppError
from app.hermes_runtime.reaper import run_reaper
from app.observability.context import get_request_id
from app.observability.logging import configure_logging

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.storekit_test_mode and settings.storekit_test_secret:
        # test-mode: TD-007 (09-e2e-testing.md §2.4). Secret is never logged.
        logger.warning(
            "STOREKIT_TEST_MODE is ENABLED — accepting HS256 test transactions. "
            "MUST be false in production."
        )
    # ADR-046 §5 Phase 4: start the hibernation reaper only on instances that run Hermes
    # (HERMES_IMAGE configured). Other instances skip it (no Docker socket dependency).
    reaper_task: asyncio.Task[None] | None = None
    if settings.hermes_image.strip():
        reaper_task = asyncio.create_task(run_reaper(settings))
    # TD-013: the auth refresh-token cleanup reaper runs on every instance (auth is universal; no
    # Docker dependency). State lives in auth_refresh_tokens → survives restart.
    cleanup_task: asyncio.Task[None] = asyncio.create_task(run_cleanup_reaper(settings))
    try:
        yield
    finally:
        for task in (reaper_task, cleanup_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await dispose_engine()
        await close_redis()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "requestId": get_request_id()}},
    )


_API_DESCRIPTION = """\
Backend-оркестратор Claude для iOS-приложения.

### Авторизация
Все `/v1/*` требуют **два** заголовка: `X-API-Key` (клиентский ключ `CLIENT_API_KEY`) и
`X-User-Id` (UUID пользователя). Нажмите **Authorize**, заполните обе схемы (`clientApiKey` +
`userId`) — оба заголовка применятся ко всем вызовам. Endpoint `/health`, `/ready`, `/metrics`
авторизацию не требуют; `/v1/admin/*` — отдельный заголовок `X-Admin-Token`.

### Блокировки приходят с HTTP 200
Бизнес-блокировка генерации — это успешный ответ `200` с телом
`{status: "blocked", blockReason}`, а не ошибка. Технические ошибки — `4xx`/`5xx` с телом
`{error: {code, message, requestId}}`. Значения `blockReason` см. в описании одноимённого поля.
"""

_OPENAPI_TAGS = [
    {
        "name": "Auth",
        "description": (
            "Спящий контур выпуска токенов (регистрация устройства, выпуск/обновление токенов, "
            "JWKS). На горячем клиентском пути не используется; авторизация — `X-API-Key` + "
            "`X-User-Id`. Без авторизации (публичные)."
        ),
    },
    {
        "name": "Agent",
        "description": (
            "Автономный агент Hermes: запуск прогона (`POST /v1/agent/run`), SSE-стрим событий "
            "(`GET /v1/agent/runs/{runId}/events`) с биллингом по реальному usage, ответ на "
            "подтверждение (`/approval`) и остановка (`/stop`). Блокировки по бизнес-правилам — "
            "HTTP 200 с `blockReason`."
        ),
    },
    {
        "name": "Chat",
        "description": (
            "Диалог с ассистентом и tool-loop. Сценарий: `POST /v1/chat/run` → ответ "
            "`tool_call` → клиент исполняет инструмент → `POST /v1/chat/tool-result` → "
            "`assistant_message`. `toolCall.id` передаётся обратно в `toolCallId`. Блокировки "
            "по бизнес-правилам приходят с HTTP 200 и полем `blockReason`."
        ),
    },
    {
        "name": "Tools",
        "description": "Каталог инструментов tool-loop: имя, описание, mutating, место исполнения.",
    },
    {
        "name": "Models",
        "description": ("Доступные модели активного провайдера инстанса для селектора модели."),
    },
    {
        "name": "Presets",
        "description": "Пресеты промтов для чипов на главном экране чата.",
    },
    {
        "name": "Policy",
        "description": (
            "Эффективные права пользователя для UI: можно ли генерировать и почему нет "
            "(`reasons[]` с теми же значениями, что и `blockReason`)."
        ),
    },
    {
        "name": "Wallet",
        "description": "Баланс кредитов и списание (1 кредит = 1 сообщение).",
    },
    {
        "name": "Tokens",
        "description": "Покупка пакетов токенов и каталог продуктов.",
    },
    {
        "name": "BYOK",
        "description": "Свой ключ Anthropic: сохранение, включение/выключение, удаление.",
    },
    {
        "name": "Admin",
        "description": (
            "Операторские действия под заголовком `X-Admin-Token`. Клиентский ключ / "
            "пользовательская идентичность здесь не авторизуют. Начисление кредитов, ручная "
            "выдача/активация подписки и просмотр кошелька."
        ),
    },
    {
        "name": "Preview",
        "description": (
            "Публичная отдача сгенерированных сайтов по подписанной ссылке. Без JWT: "
            "авторизация в подписи."
        ),
    },
    {
        "name": "Chats",
        "description": (
            "История чатов: список, поиск, шаги, переименование, закрепление, удаление. "
            "Доступ только владельца; чужой/несуществующий чат — 404."
        ),
    },
    {
        "name": "Workspaces",
        "description": (
            "Рабочие пространства (iOS «Projects»): имя, описание, кастомные инструкции и "
            "файлы-знания как контекст чатов проекта. Доступ только владельца; чужой/"
            "несуществующий workspace — 404."
        ),
    },
    {
        "name": "Profile",
        "description": "Профиль пользователя: отображаемое имя и `accountId`.",
    },
    {
        "name": "Preferences",
        "description": (
            "Пользовательские настройки: дефолтный тип ассистента (chat|code), уведомления и "
            "дефолты Code-контекста."
        ),
    },
    {
        "name": "Health",
        "description": "Служебные проверки (без авторизации): liveness, readiness, метрики.",
    },
]


# Client contour schemes that MUST be required together (AND), not as alternatives (ADR-044 R2.4,
# agent-proxy/06-rbac.md): both X-API-Key and X-User-Id are mandatory on every client /v1/*.
_CLIENT_CONTOUR_SCHEMES = frozenset({"clientApiKey", "userId"})


def _merge_client_contour_security(security: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the client contour's two scheme requirements into one AND requirement (ADR-044).

    FastAPI emits one OpenAPI ``securityRequirement`` object *per* ``SecurityBase`` dependency, so
    ``get_current_user`` (which depends on both ``clientApiKey`` and ``userId`` schemes) yields
    ``[{"clientApiKey": []}, {"userId": []}]`` — that is OR semantics ("either header suffices").
    The contract (ADR-044 R2.1/R2.4, agent-proxy/06-rbac.md) requires BOTH headers together, i.e. a
    single requirement object ``{"clientApiKey": [], "userId": []}`` (AND). We rewrite exactly that
    pair and leave any other contour (``adminToken``, ``adaptyWebhook``, public/none) untouched.
    """
    client_keys = {k for req in security for k in req} & _CLIENT_CONTOUR_SCHEMES
    if client_keys != _CLIENT_CONTOUR_SCHEMES:
        # Not the client contour (admin / adapty / public): leave the security list as emitted.
        return security
    merged: dict[str, list[str]] = {}
    rest: list[dict[str, Any]] = []
    for req in security:
        if set(req) <= _CLIENT_CONTOUR_SCHEMES:
            merged.update(req)
        else:
            rest.append(req)
    return [merged, *rest]


def custom_openapi(app: FastAPI) -> dict[str, Any]:
    """Build the OpenAPI schema, enforcing AND for the client contour pair (ADR-044 R2).

    Cached on ``app.openapi_schema`` like FastAPI's default. Only post-processes per-operation
    ``security``; scheme declarations themselves come from the ``SecurityBase`` dependencies
    (``openapi_security.py``), so the source of truth for auth is unchanged (R2.4).
    """
    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            security = operation.get("security")
            if security:
                operation["security"] = _merge_client_contour_security(security)
    app.openapi_schema = schema
    return schema


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="claude-ios-backend",
        version="0.1.0",
        description=_API_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Middleware (added in reverse execution order; outermost added last).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(SizeLimitMiddleware)

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, "validation_error", "request validation failed")

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return _error_response(500, "internal_error", "internal error")

    for module in (
        auth,
        chat,
        agent,
        tools,
        models,
        presets,
        policy,
        wallet,
        token_purchase,
        byok,
        admin,
        preview,
        chats,
        workspaces,
        profile,
        preferences,
        billing_adapty,
    ):
        app.include_router(module.router)
    app.include_router(health.router)

    # ADR-044 R2.4: enforce AND for the client contour (clientApiKey + userId) in OpenAPI.
    app.openapi = lambda: custom_openapi(app)  # type: ignore[method-assign]

    return app


app = create_app()
