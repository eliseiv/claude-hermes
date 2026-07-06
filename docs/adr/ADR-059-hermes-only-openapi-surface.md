# ADR-059 — Hermes-only публичная OpenAPI-поверхность (legacy скрыты, не отключены)

- Статус: **Reversed (2026-07-06, см. [§7 Ревизия](#7-ревизия-2026-07-06--reversed-полная-поверхность-openapi))** — исходное решение (скрыть legacy через `include_in_schema=False`) отменено: поверхность OpenAPI снова **полная** (весь активный API минус retired `/v1/auth/*`).
- Дата: 2026-07-06 (ревизия 2026-07-06)
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

## 7. Ревизия (2026-07-06) — REVERSED: полная поверхность OpenAPI

**Статус: исходное решение (§1–§4) отменено (reversed).** Тело §1–§6 выше не переписано (immutability-конвенция ADR) — актуальный контракт поверхности OpenAPI читается из этого раздела.

### 7.1. Причина отмены (изменение требования)

Выяснилось, что iOS-приложение использует **НЕ только** Hermes-агентный контур (`/v1/agent/*`), но и унаследованный claude-ios функционал: чаты (`/v1/chat/*`, `/v1/chats*`), рабочие пространства/проекты (`/v1/workspaces*`), каталоги (`/v1/tools`, `/v1/models`, `/v1/presets`), профиль/настройки (`/v1/profile`, `/v1/preferences`), превью (`/v1/preview/*`). Разработчику клиента эти эндпоинты нужны **видимыми в `/docs`** для интеграции и ручного тестирования. Скрытие их из схемы (§1, hide-only через `include_in_schema=False`) оказалось **неверным по требованиям**. Сами эндпоинты всё это время были рабочими (hide-only, §6) — скрывалась только их документация, поэтому отмена **чисто документационная** и не меняет поведение.

### 7.2. Новое решение — снять `include_in_schema=False`

Поверхность `/openapi.json` (`/docs`, `/redoc`) = **весь активный API**: Hermes-ядро + инфраструктура + claude-ios legacy, **КРОМЕ** retired `/v1/auth/*` (issuer-роутер не смонтирован, `404`, вне схемы — [ADR-044 §4a](ADR-044-client-api-key-auth.md); **не** возвращается).

- Механизм — **просто снятие флага** `include_in_schema=False` с девяти legacy-роутеров в `src/app/main.py`; все они регистрируются штатным `app.include_router(module.router)` (как видимые). Никакого нового механизма не вводится.
- Снова **видимые** роутеры (были скрыты в §3): `chat`, `chats`, `workspaces`, `tools`, `models`, `presets`, `profile`, `preferences`, `preview`.
- Остаются видимыми (как в §2, без изменений): `agent`, `admin`, `wallet`, `policy`, `billing_adapty`, `token_purchase`, `byok`, `health`.
- **НЕ регистрируется вовсе:** `auth` (retired HTTP-поверхность `/v1/auth/*`, [ADR-044 §4a](ADR-044-client-api-key-auth.md)) — остаётся несмонтированным (`404`, вне схемы). Тег `Auth` в `_OPENAPI_TAGS` **не** восстанавливается.

### 7.3. Финальный `_OPENAPI_TAGS` и порядок (замещает §4)

Восстанавливаются описания legacy-тегов; сохраняется добавленное в §4 описание тега `Billing (Adapty)`; тег `Auth` **не** возвращается; тег `Subscription` **не** возвращается ([TD-021](../100-known-tech-debt.md) / ревизия [ADR-029](ADR-029-adapty-subscription-webhook.md), под ним нет роутов). `Billing (Adapty)` располагается в биллинг-контуре — между `BYOK` и `Admin` (как в §4). Итоговый порядок (17 тегов):

```
Agent, Chat, Tools, Models, Presets, Policy, Wallet, Tokens, BYOK, Billing (Adapty), Admin, Preview, Chats, Workspaces, Profile, Preferences, Health
```

Это ровно pre-ADR-059 порядок, из которого **убран `Auth`** и в биллинг-контур (между `BYOK` и `Admin`) **вставлен `Billing (Adapty)`**.

### 7.4. Что не меняется

- `title = claude-hermes` (§5) — сохраняется.
- Security schemes, AND-семантика клиентского контура (`clientApiKey`+`userId`), `adminToken`, `adaptyWebhook`, публичные без security — без изменений ([ADR-044](ADR-044-client-api-key-auth.md), [08-api-documentation.md R2](../08-api-documentation.md)).
- Бизнес-контракты, wire-формат, auth-проверки, биллинг, миграции — не затрагиваются (как и в исходном ADR-059, изменение документационное).
- `/v1/auth/*` остаётся retired ([ADR-044 §4a](ADR-044-client-api-key-auth.md)); `/v1/subscription/sync` остаётся retired ([TD-021](../100-known-tech-debt.md)).

### 7.5. Последствия ревизии

- Публичная документация снова показывает полную поверхность активного API — интегратор видит и тестирует claude-ios legacy (chats/workspaces/tools/…) наравне с Hermes-контуром.
- «Серых» (недокументированных, но рабочих) маршрутов больше нет (устраняется отрицательное следствие §Consequences).
- Прямой trade-off: раскрытие API surface в `/docs` шире; митигация неизменна — `DOCS_ENABLED=false` в prod ([08-api-documentation.md R7](../08-api-documentation.md), [05-security.md](../05-security.md)).

needs_code_sync: `src/app/main.py` — снять `include_in_schema=False` с девяти legacy-роутеров (все через штатный `include_router`), восстановить полный `_OPENAPI_TAGS` в порядке §7.3 (добавить обратно `Chat`/`Tools`/`Models`/`Presets`/`Preview`/`Chats`/`Workspaces`/`Profile`/`Preferences`; `Auth` НЕ добавлять).
