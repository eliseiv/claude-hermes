"""FastAPI app factory: middleware, routers, exception handlers (api-gateway/03)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api_gateway.middleware import (
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
    SizeLimitMiddleware,
)
from app.api_gateway.rate_limit import close_redis
from app.api_gateway.routers import (
    admin,
    auth,
    byok,
    chat,
    chats,
    health,
    policy,
    preferences,
    preview,
    profile,
    subscription,
    token_purchase,
    tools,
    wallet,
)
from app.config import get_settings
from app.db import dispose_engine
from app.errors import AppError
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
    yield
    await dispose_engine()
    await close_redis()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "requestId": get_request_id()}},
    )


_API_DESCRIPTION = """\
Backend-оркестратор Claude для iOS-приложения: принимает запросы от приложения, проверяет
права доступа, ходит в Anthropic Messages API и ведёт биллинг.

### Как авторизоваться
Все `/v1/*` endpoint требуют заголовок `Authorization: Bearer <JWT>` (RS256). В claim `sub`
лежит `userId`; поле `userId` в теле запроса обязано совпадать с `sub`, иначе вернётся `403`.
Нажмите **Authorize** и введите `Bearer <JWT>` один раз — он применится ко всем вызовам.
Служебные endpoint (`/health`, `/ready`, `/metrics`) JWT не требуют.

### Правила доступа (бизнес)
Сначала одна бесплатная пробная генерация (trial). Дальше — либо активная подписка с
кредитами (1 кредит = 1 сообщение), либо собственный ключ Anthropic (BYOK). Эффективные
права для UI отдаёт `GET /v1/policy/effective`.

### Важно: блокировки приходят с HTTP 200
Бизнес-блокировка генерации — это **успешный** ответ `200 OK` с телом
`{status: "blocked", blockReason}`, а **не** ошибка 4xx. Технические ошибки —
4xx/5xx со стандартным `{error: {code, message, requestId}}`. Перечень значений `blockReason`
и их трактовку для UI см. в описании поля `blockReason` (endpoint Chat).

### Как получить токен
Вызовите `POST /v1/auth/register` (без авторизации), скопируйте `accessToken`, нажмите
**Authorize** и вставьте его — он применится ко всем защищённым `/v1/*`.
"""

_OPENAPI_TAGS = [
    {
        "name": "Auth",
        "description": (
            "Получение и обновление токена доступа. Точка входа для тестирования: регистрация "
            "устройства, выпуск и обновление токенов, публичный ключ (JWKS). Без JWT."
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
        "name": "Subscription",
        "description": "Синхронизация подписки StoreKit и начисление кредитов периода.",
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
            "Операторские действия под заголовком `X-Admin-Token`. Пользовательский JWT здесь "
            "не авторизует. Начисление кредитов и просмотр кошелька."
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
        "description": "Служебные проверки и метрики (без JWT): liveness, readiness, Prometheus.",
    },
]


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
        tools,
        policy,
        wallet,
        subscription,
        token_purchase,
        byok,
        admin,
        preview,
        chats,
        profile,
        preferences,
    ):
        app.include_router(module.router)
    app.include_router(health.router)

    return app


app = create_app()
