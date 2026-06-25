"""OpenAPI security scheme reflection (08-api-documentation.md, R2; ADR-044).

Declares the security schemes so Swagger UI shows the lock icon and the Authorize button works:
- ``clientApiKey`` (apiKey header ``X-API-Key``) + ``userId`` (apiKey header ``X-User-Id``) — the
  CLIENT contour for user ``/v1/*`` endpoints (ADR-044). Both are required together: the trusted
  client key authenticates the request, ``X-User-Id`` carries the trusted subject identity.
- ``adminToken`` (apiKey header ``X-Admin-Token``) — `/v1/admin/*` endpoints (ADR-009, unchanged).
- ``adaptyWebhook`` (HTTP Bearer) — the Adapty webhook (ADR-029, unchanged).
- ``bearerAuth`` (HTTP Bearer JWT) — DORMANT (ADR-044): the JWT/Apple contour is not deleted, but
  this scheme is no longer attached to client ``/v1/*`` operations. It is kept declared in code as
  the documented upgrade path; nothing depends on it on the hot client path.

All schemes are ``SecurityBase`` instances and are consumed as dependencies *inside*
``app.deps.get_current_user`` (reads ``X-API-Key`` + ``X-User-Id``) and
``app.api_gateway.auth.require_admin`` (reads ``X-Admin-Token``). Being SecurityBase, they
contribute the security scheme to each protected operation's OpenAPI ``security`` (lock icon /
Authorize) WITHOUT adding a duplicate header *parameter* to the operation. Actual auth verification
still lives in those dependencies: ``auto_error=False`` keeps the schemes from raising on a
missing/malformed header, so the real 401 / constant-time checks decide the outcome unchanged.
"""

from __future__ import annotations

from fastapi.security import APIKeyHeader, HTTPBearer

# scheme_name fixes the OpenAPI components.securitySchemes key to `clientApiKey` (ADR-044 R2.1).
# apiKey-in-header (X-API-Key). The real constant-time check stays in verify_client_api_key.
client_api_key_scheme = APIKeyHeader(
    name="X-API-Key",
    scheme_name="clientApiKey",
    auto_error=False,
    description=(
        "Клиентский API-ключ. Вставьте `CLIENT_API_KEY` в заголовок `X-API-Key` через "
        "Authorize — применится ко всем `/v1/*` клиентского контура. Реальная "
        "constant-time проверка — на сервере; это объявление — только для Swagger UI."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `userId` (ADR-044 R2.1).
# apiKey-in-header (X-User-Id). Carries the trusted subject UUID; declared alongside clientApiKey
# so the tester supplies both headers via Authorize. Identity is trusted (the key is trusted).
user_id_scheme = APIKeyHeader(
    name="X-User-Id",
    scheme_name="userId",
    auto_error=False,
    description=(
        "UUID пользователя. Идентичность доверяется (ключ доверенный). Обязателен вместе "
        "с `X-API-Key` — вставьте UUID субъекта в заголовок `X-User-Id` через Authorize."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `bearerAuth`.
# DORMANT (ADR-044): JWT/Apple are not deleted, but bearerAuth is NOT attached to client /v1/*
# operations. Kept declared as the documented identity-upgrade path. bearerFormat=JWT documents
# the token shape; description (RU) explains the dormant model.
bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="JWT",
    auto_error=False,
    description=(
        "JWT (RS256) — СПЯЩИЙ контур (ADR-044): на клиентских `/v1/*` не используется. "
        "В claim `sub` — userId. Оставлен как путь апгрейда идентичности; на горячем "
        "клиентском пути применяется `clientApiKey` + `userId`."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `adminToken`.
# apiKey-in-header documents the X-Admin-Token mechanism; the real check stays in require_admin.
admin_scheme = APIKeyHeader(
    name="X-Admin-Token",
    scheme_name="adminToken",
    auto_error=False,
    description=(
        "Изолированный admin-токен. Вставьте секрет в заголовок `X-Admin-Token` через "
        "Authorize. Клиентский ключ / пользовательская идентичность admin-действия не "
        "авторизуют. Реальная проверка — на сервере; это объявление — только для Swagger UI."
    ),
)

# scheme_name fixes the OpenAPI components.securitySchemes key to `adaptyWebhook`.
# HTTP Bearer documents the static webhook secret; the real constant-time check stays in
# require_adapty_webhook (ADR-029). Separate from the client contour and adminToken.
adapty_webhook_scheme = HTTPBearer(
    scheme_name="adaptyWebhook",
    auto_error=False,
    description=(
        "Статический bearer-секрет вебхука Adapty (`ADAPTY_WEBHOOK_SECRET`). Вызывает Adapty, "
        "не клиент. Введите секрет как `Bearer <secret>` через Authorize. НЕ клиентский ключ "
        "и НЕ admin-токен. Реальная constant-time проверка — на сервере."
    ),
)
