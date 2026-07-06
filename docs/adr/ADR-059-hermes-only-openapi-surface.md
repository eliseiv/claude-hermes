# ADR-059 — Hermes-only публичная OpenAPI-поверхность (legacy скрыты, не отключены)

- Статус: Accepted
- Дата: 2026-07-06
- Расширяет: [08-api-documentation.md](../08-api-documentation.md) (R4 теги/группировка, R6 метаданные), [ADR-045](ADR-045-hermes-as-agent-proxy.md) (agent как headline-контур)
- Связан с: [ADR-044](ADR-044-client-api-key-auth.md) (клиентский контур), [ADR-029](ADR-029-adapty-subscription-webhook.md) (adapty webhook)

## Context

Сервис `claude-hermes` поставляется как backend Hermes-агента для iOS. Публичная поверхность API (`/docs`, `/redoc`, `/openapi.json`) исторически документирует полный набор эндпоинтов, включая legacy-контуры простого чата и вспомогательные ресурсы (`chat`, `chats`, `workspaces`, `tools`, `models`, `presets`, `profile`, `preferences`, `preview`). Для Hermes-поставки нужна **сфокусированная** документация: только эндпоинты Hermes-сервиса.

Решением пользователя зафиксировано: legacy-эндпоинты **СКРЫТЬ** из OpenAPI/Swagger, **НЕ отключая** их — маршруты остаются зарегистрированными и функциональными (обратная совместимость, внутренние вызовы, тесты), но не документируются.

Agent-путь (`/v1/agent/*`) самодостаточен: не зависит от `chat`/`chats`/`workspaces`; policy-гейт выполняется in-process. Скрытие legacy из схемы не влияет на работу agent-контура.

## Decision

### 1. Механизм — hide-only через `include_in_schema=False`

- В `src/app/main.py` (цикл регистрации роутеров, ~стр. 301–320) legacy-роутеры регистрируются с `include_in_schema=False`: `app.include_router(module.router, include_in_schema=False)`.
- `include_in_schema=False` **исключает** операции из `/openapi.json` (и, следовательно, из `/docs` и `/redoc`), но **НЕ снимает роутинг**: эндпоинты остаются активными и отвечают как раньше (тот же код, та же авторизация, те же ответы). Это **документационное** изменение, не поведенческое.

### 2. Видимая поверхность (Hermes-сервис)

Остаются в OpenAPI-схеме роутеры: **`agent`, `admin`, `wallet`, `policy`, `billing_adapty`, `token_purchase`, `byok`** + служебный **`health`**.

Соответствующие теги (видимые): `Agent`, `Policy`, `Wallet`, `Tokens`, `BYOK`, `Billing (Adapty)`, `Admin`, `Health`.

### 3. Скрытая поверхность (legacy, `include_in_schema=False`, функциональны)

Роутеры: **`chat`, `chats`, `workspaces`, `tools`, `models`, `presets`, `profile`, `preferences`, `preview`**. Их теги (`Chat`, `Chats`, `Workspaces`, `Tools`, `Models`, `Presets`, `Profile`, `Preferences`, `Preview`) убираются из `_OPENAPI_TAGS` (под ними нет видимых операций).

Тег `Auth` также убирается из `_OPENAPI_TAGS`: issuer-роутер `/v1/auth/*` не зарегистрирован в `create_app()` (спящий контур, [ADR-044 §4](ADR-044-client-api-key-auth.md)) — под тегом нет операций.

### 4. `_OPENAPI_TAGS` и порядок тегов (R4)

`_OPENAPI_TAGS` в `src/app/main.py` сокращается до видимых тегов в порядке пользовательского сценария Hermes:

```
Agent, Policy, Wallet, Tokens, BYOK, Billing (Adapty), Admin, Health
```

Добавляется описание тега **`Billing (Adapty)`** (ранее объявлялся только на роутере `billing_adapty`, без записи в `_OPENAPI_TAGS`) — теперь это видимый контур, ему нужна RU-описание и место в порядке.

### 5. Метаданные (R6)

`title` = **`claude-hermes`** (уже установлено в коде `create_app()`). Документ [08-api-documentation.md](../08-api-documentation.md) R6 приводится в соответствие (было указано `claude-ios-backend`). `description` и правило blocked=200 — без изменений.

### 6. Инварианты

- Маршруты legacy **активны** — любые прямые вызовы (внутренние, e2e, обратная совместимость) работают как раньше. Скрытие только документационное.
- Авторизация, биллинг, policy — без изменений.
- Никаких миграций, никаких изменений контрактов данных.

## Consequences

**Положительные:**
- Публичная документация сфокусирована на Hermes-контурах; интегратор не отвлекается на legacy.
- Меньше раскрытие API surface (косвенная security-польза, ср. [08-api-documentation.md R7](../08-api-documentation.md)).
- Ноль рисков регрессии поведения: маршруты не тронуты.

**Отрицательные / ограничения:**
- Скрытые эндпоинты остаются доступными, но недокументированными → «серые» маршруты. Осознанно: полное отключение не требовалось и сломало бы обратную совместимость/тесты.
- Тесты документации (`test_api_documentation.py`) и метаданных требуют синхронизации с сокращённой поверхностью (см. ТЗ qa).

## Alternatives

1. **Полностью удалить/отключить legacy-роутеры.** Отвергнуто решением пользователя: маршруты должны остаться рабочими (обратная совместимость, внутренние зависимости, зелёные тесты).
2. **Отдельное OpenAPI-приложение (sub-app) только для Hermes.** Избыточно: `include_in_schema=False` достигает цели одним флагом на роутер, без дублирования app-фабрики.
3. **Оставить полную схему, полагаясь на теги-разделители.** Отвергнуто: не даёт сфокусированной Hermes-поверхности, которую запросил пользователь.
