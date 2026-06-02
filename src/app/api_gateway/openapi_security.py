"""OpenAPI security scheme reflection (08-api-documentation.md, R2).

Declares two security schemes so Swagger UI shows the lock icon and the Authorize button works:
- ``bearerAuth`` (HTTP Bearer JWT) — user `/v1/*` endpoints.
- ``adminToken`` (apiKey header ``X-Admin-Token``) — `/v1/admin/*` endpoints.

This is documentation only: actual auth verification stays in `app.api_gateway.auth` /
`app.deps.get_current_user` (JWT) and `require_admin` (admin). ``auto_error=False`` ensures
these dependencies never raise and never short-circuit the real check — they only contribute
the security scheme to OpenAPI.
"""

from __future__ import annotations

from fastapi.security import APIKeyHeader, HTTPBearer

# scheme_name fixes the OpenAPI components.securitySchemes key to `bearerAuth`.
# bearerFormat=JWT documents the token shape; description (RU) explains the auth model.
bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="JWT",
    auto_error=False,
    description=(
        "JWT (RS256). В claim `sub` — userId; `userId` в теле запроса обязан совпадать "
        "с `sub`, иначе `403`. Введите токен как `Bearer <JWT>` через кнопку Authorize — "
        "он применится ко всем защищённым вызовам. Реальная проверка подписи/exp/iss/aud "
        "выполняется на сервере; это объявление — только для клиента и Swagger UI."
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
        "Authorize. Пользовательский JWT admin-действия не авторизует. Реальная проверка "
        "выполняется на сервере; это объявление — только для Swagger UI."
    ),
)
