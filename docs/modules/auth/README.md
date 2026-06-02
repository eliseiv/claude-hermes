# Module: Auth (встроенный issuer, device-based)

- Статус: **Реализован** (MVP-расширение, 2026-06-02). Эндпоинты register/token/refresh/jwks, device-based identity, refresh-token rotation, миграция `0005`. Offline-сьют 775/775 зелёный, production-ready. Prod-требование — сгенерировать RSA-пару подписи (без приватного ключа `/v1/auth/*` → `503`).
- Ответственность: первичная аутентификация устройства и **выпуск** RS256 JWT собственным backend'ом ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)). Закрывает [Q-005-1](../../99-open-questions.md) — issuer = встроенный, не внешний IdP.
- Верификация выпущенных токенов — существующим `JwtVerifier` (`src/app/api_gateway/auth.py`), **без изменения** его логики (тот же RS256, `iss`/`aud`/`exp`/`sub`). Issuer/audience — собственные (`https://broadnova.shop` / `claude-ios`).

## Документы
- [00-overview.md](00-overview.md) — scope / out-of-scope
- [01-context.md](01-context.md) — зависимости, соседи
- [02-api-contracts.md](02-api-contracts.md) — эндпоинты register/token/refresh/jwks
- [03-architecture.md](03-architecture.md) — issuer, ключи, согласование с провижинингом
- [04-data-model.md](04-data-model.md) — `auth_devices`, `auth_refresh_tokens` (миграция 0005)
- [05-security.md](05-security.md) — ключи, PEM-в-env, rate-limit, anti-abuse
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md) — backend scope

## Модель (кратко)
- **Device-based identity:** клиент шлёт `deviceId` (или backend генерирует) → backend находит/создаёт `userId` → выдаёт RS256 JWT (`sub=userId`, `device_id`, `iss`, `aud`, `exp`).
- **Эндпоинты:** `POST /v1/auth/register`, `POST /v1/auth/token`, `POST /v1/auth/refresh`, `GET /v1/auth/jwks`. Все — **без** пользовательского JWT (точка его получения), защита — rate-limit per IP.
- **Токены:** access-token (RS256 JWT, TTL 1ч) + opaque refresh-token (TTL 30д, hashed-store, single-use rotation).
- **Email/пароль и Apple Sign-In** — опциональное расширение, НЕ MVP, путь не закрыт ([Q-018-2](../../99-open-questions.md)).

## DoD
- [x] `POST /v1/auth/register` — find-or-create identity по `deviceId`, выдача access+refresh, явный provisioning `users`.
- [x] `POST /v1/auth/token` — повторная выдача для известного устройства (идемпотентно по `deviceId`).
- [x] `POST /v1/auth/refresh` — single-use rotation, reuse → `401` + ревокация цепочки.
- [x] `GET /v1/auth/jwks` — публичный ключ (без приватного).
- [x] Round-trip: выпущенный токен верифицируется собственным `JwtVerifier`.
- [x] Lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) сохранён как fallback; trial/policy не сломаны.
- [x] Ключи: `JWT_PRIVATE_KEY_PATH`/`JWT_PUBLIC_KEY_PATH` (файл) или `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` (`\n`-экранирование); приватный ключ под redaction. Issuer без сконфигурированного приватного ключа → `/v1/auth/*` отвечают `503`.
- [x] Rate-limit `/v1/auth/*` per IP; миграция `0005` (`auth_devices`, `auth_refresh_tokens`), цепочка `0001`→`0005`.

## Changelog
- 2026-06-02: bootstrap модуля (architect). [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md), закрывает [Q-005-1](../../99-open-questions.md). Новые Q-018-1 (anti-Sybil), Q-018-2 (email/Apple Sign-In апгрейд).
- 2026-06-02: модуль **реализован и протестирован** (register/token/refresh/jwks, device-based, refresh-rotation, миграция `0005`); offline-сьют 775/775, production-ready. [Q-005-1](../../99-open-questions.md) закрыт реализацией. Prod-предзапусковый шаг — сгенерировать RSA-пару и задать `JWT_PRIVATE_KEY(_PATH)`/`JWT_PUBLIC_KEY` + `JWT_ISSUER`/`JWT_AUDIENCE` (см. [07-deployment.md prod-checklist](../../07-deployment.md#prod-readiness-checklist-must-configure-before-launch)).
