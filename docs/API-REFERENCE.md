# API Reference — backend для iOS-приложения (Claude orchestration)

Сводный справочник по всем эндпоинтам сервиса для product-менеджера и интеграторов iOS.
Документ человекочитаемый: метод, путь, назначение, заголовки, тело запроса/ответа, коды ответа.
Внутренние архитектурные обоснования вынесены в ADR (`docs/adr/`) и здесь не дублируются — при необходимости даны ссылки.

База: все бизнес-эндпоинты под префиксом `/v1`. Транспорт — только HTTPS. Формат тела — JSON (`Content-Type: application/json`).

---

## Оглавление

1. [Базовые принципы](#1-базовые-принципы)
2. [Аутентификация и заголовки](#2-аутентификация-и-заголовки)
3. [Коды ответа (общие)](#3-коды-ответа-общие)
4. Эндпоинты по модулям:
   - [Auth](#21-auth-выпуск-токена) · [Chat](#4-chat) · [Tools](#22-tools-каталог-инструментов) · [Policy](#5-policy) · [Wallet](#6-wallet) · [Subscription](#7-subscription) · [BYOK](#8-byok) · [Admin](#9-admin) · [Website-builder / Preview](#10-website-builder--preview) · [Health / Docs](#11-health--docs) · [Chats](#17-chats) · [Profile](#18-profile) · [Preferences](#19-preferences) · [Tokens](#20-tokens)
5. [blockReason — справочник (8 значений)](#12-blockreason--справочник)
6. [Tool-протокол: client-side vs server-side](#13-tool-протокол)
7. [Монетизация (кратко)](#14-монетизация-кратко)
8. [Превью сайта для iOS](#15-превью-сайта-для-ios)
9. [Лимиты и rate limits](#16-лимиты-и-rate-limits)
10. [Как тестировать через Swagger](#23-как-тестировать-через-swagger)

---

## 1. Базовые принципы

- **Один HTTP-запрос — один логический шаг.** Чат работает по протоколу tool-use: backend может вернуть запрос на исполнение инструмента (tool_call), iOS исполняет и присылает результат.
- **Бизнес-блокировки — это НЕ ошибки.** Если пользователю нельзя сгенерировать сообщение (нет кредитов, истёк trial и т.п.), сервис отвечает `200 OK` с `status="blocked"` и машиночитаемым `blockReason`. См. [раздел 12](#12-blockreason--справочник). HTTP 4xx/5xx — только технические ошибки.
- **Деньги/кредиты — целые числа.** 1 кредит = 1 сообщение. Дробей нет.
- **Изоляция данных.** Пользователь видит только свои ресурсы — это обеспечивается сверкой `userId` тела с `sub` JWT.

---

## 2. Аутентификация и заголовки

### Три независимых контура авторизации

| Контур | Кто использует | Механизм | Заголовок | Swagger security scheme |
|---|---|---|---|---|
| **Пользовательский** | iOS-приложение, эндпоинты `/v1/*` (кроме `/v1/auth/*`, admin, preview) | JWT Bearer (RS256) | `Authorization: Bearer <JWT>` | `bearerAuth` (http bearer, format JWT) |
| **Admin** | Операторские/саппорт-инструменты, `/v1/admin/*` | статический секрет | `X-Admin-Token: <ADMIN_API_SECRET>` | `adminToken` (apiKey, header `X-Admin-Token`) |
| **Preview** | Браузер (открывает превью сайта) | подпись внутри URL (HMAC+TTL) | нет — авторизация в самой ссылке | — (публичный по signed URL) |

> Эндпоинты выпуска токена `/v1/auth/register|token|refresh` и `GET /v1/auth/jwks` — **public** (без `Authorization`): это точка получения JWT. Защита — per-IP rate-limit. См. [§21](#21-auth-выпуск-токена).

Контуры **взаимно изолированы**: пользовательский JWT не даёт доступа к admin-эндпоинтам, admin-токен — к пользовательским ресурсам. Эскалация невозможна by design.

### Пользовательский JWT (RS256)

- Алгоритм подписи — **RS256** (асимметричный: приватный ключ — секрет подписи, публичный — для verify).
- **Issuer встроен в backend** ([ADR-018](adr/ADR-018-embedded-auth-issuer.md)): токен выпускается через `/v1/auth/*` (см. [§21](#21-auth-выпуск-токена)) и верифицируется тем же сервисом (self-consistent, `iss=https://broadnova.shop`, `aud=claude-ios`).
- Обязательные claims: `sub` = `userId` (UUID), `exp` (срок), `iat`, `device_id`, `iss`, `aud`. Заголовок `kid`.
- Просроченный/невалидный токен → `401`.
- `userId` в теле запроса **обязан** совпадать с `sub` токена, иначе `403`.
- Первичная аутентификация — **device-based** (анонимная): клиент получает токен по `deviceId` через `POST /v1/auth/register`. Пользователь создаётся при register (явно) либо лениво при первом `/v1/*` запросе (источник идентичности — встроенный issuer).

### Admin-токен

- Статический высокоэнтропийный секрет `ADMIN_API_SECRET`, передаётся в `X-Admin-Token`.
- Сравнение — constant-time. Отсутствие/несовпадение → `401`.
- Поддержана ротация (второй секрет на grace-период).

### Preview signed URL

- Подпись `HMAC-SHA256` под отдельным секретом `PREVIEW_URL_SECRET` поверх `projectId|ownerUserId|exp`, с TTL (дефолт 15 минут).
- Открывается прямой ссылкой в браузере/`WKWebView`, без cookies и без JWT.

### Общие заголовки запроса

| Заголовок | Обязательность | Назначение |
|---|---|---|
| `Authorization: Bearer <JWT>` | обязателен для всех `/v1/*` (кроме `/v1/preview/*`) | пользовательская аутентификация |
| `X-Admin-Token` | обязателен для `/v1/admin/*` | admin-аутентификация |
| `X-Device-Id` | опционален для `/v1/chat/*` | override `device_id` для per-device rate limit; при отсутствии — fallback на `device_id` из JWT-claim. Если и claim пуст — per-device лимит не применяется |
| `Content-Type: application/json` | обязателен для POST | формат тела |
| `X-Request-Id` | опционален | correlation id одного HTTP-запроса (логи/трейсы). Если не передан — сервис генерирует и вернёт его в ответе. Это **не** ключ идемпотентности биллинга |
| `X-Scrape-Token` | только для `/metrics`, если включена защита токеном | доступ к метрикам |

### Что секрет и что не логируется

Никогда не логируются и не попадают в audit/трейсы: заголовок `Authorization` и JWT целиком, `X-Admin-Token`, BYOK API-ключ (`apiKey`), StoreKit `transaction`, `PREVIEW_URL_SECRET`, любые поля содержащие `key`/`token`/`secret`. В логах — только correlation id (`X-Request-Id`), `sessionId` и нечувствительные поля.

---

## 3. Коды ответа (общие)

| Код | Когда |
|---|---|
| **200** | успех; **в том числе бизнес-blocked** для `/v1/chat/*` (тело со `status="blocked"`, `blockReason`) |
| **401** | нет/невалидный/просроченный JWT; нет/неверный `X-Admin-Token` |
| **403** | `userId` в теле ≠ `sub` JWT; для preview — невалидная подпись/истёкший URL/чужой владелец |
| **404** | ресурс/сессия/пользователь не найдены; для preview — проект или файл не найдены |
| **409** | конфликт идемпотентности (тот же ключ — другой payload); недостаточно кредитов на момент списания |
| **413** | превышен общий размер тела запроса (transport-уровень, до парсинга) |
| **422** | невалидная схема/значение поля; превышен лимит отдельного поля; невалидная StoreKit-транзакция |
| **429** | превышен rate limit |
| **502 / 5xx** | внутренняя ошибка или ошибка upstream (Anthropic / App Store / KMS) |

Стандартный формат технической ошибки (4xx/5xx):
```json
{ "error": { "code": "validation_error", "message": "human readable", "requestId": "..." } }
```
`code` ∈ `unauthorized | forbidden | not_found | conflict | payload_too_large | validation_error | rate_limited | internal_error | upstream_error`.

> Бизнес-блокировки **не** используют этот формат — они приходят как `200` со `status="blocked"`.

---

## 4. Chat

Основной поток: пользователь шлёт сообщение, backend оркестрирует диалог с Claude, при необходимости запрашивает исполнение инструментов. Сервис — прежде всего **чат-агрегатор Claude**; работает **без `projectId`** (website-builder — опциональная фича, [ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)).

### POST /v1/chat/run
Старт или продолжение агентного шага (отправка пользовательского сообщения).

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен), `X-Device-Id` (опц.; override per-device rate-limit, иначе fallback на `device_id` из JWT-claim), `Content-Type: application/json`, `X-Request-Id` (опц.).

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` JWT |
| `projectId` | string, **опц.** | Ключ проекта website-builder. **Опционален** ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)): без него — обычный чат-агрегатор (основной поток), server-side `site.*` tools Claude **не** предлагаются; с ним — доступен website-builder (`site.*`). Фиксируется при создании сессии; при resume берётся из сессии (поле запроса игнорируется). На биллинг/policy не влияет. |
| `sessionId` | string (uuid), опц. | нет → создаётся новая сессия, `mode` фиксируется на сессию |
| `message` | string | ≤ 32 KB |
| `mode` | `credits` \| `byok` | **billing_mode** — способ оплаты генерации (фиксируется на сессию) |
| `assistantMode` | `chat` \| `code`, опц. | **assistant_mode** — тип ассистента (ADR-012). При отсутствии — дефолт из `GET /v1/preferences` (`defaultAssistantMode`), затем `chat`. Фиксируется при создании сессии. **Ортогонален `mode`** |
| `attachments` | array, опц. | **inline base64-вложения** (фото/PDF/текст), ≤ 10. Только в первом (новом) user-turn; в `/chat/tool-result` не принимаются. Каждый элемент: `{ type: image\|document\|text, mediaType, filename?, data (base64) }`. См. ниже. ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) |
| `context` | object, опц. | ≤ 64 KB сериализованного JSON |

> **`assistantMode` ≠ `mode`.** `assistantMode` (`chat`/`code`) — *какой* ассистент отвечает (продуктовый профиль ответа), задаётся клиентом или берётся из preferences. `mode` (`credits`/`byok`) — *чем платим* за генерацию. Это два независимых измерения: возможна любая из 4 комбинаций (напр. `code`+`byok`). Подробнее — [ADR-012](adr/ADR-012-assistant-mode-vs-billing-mode.md).

> **Вложения (`attachments[]`, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)).** Мультимодальный ввод передаётся inline в base64 (без отдельного upload-эндпоинта). Классы и allowlist `mediaType`:
> - `type: image` — `image/jpeg`, `image/png`, `image/gif`, `image/webp` → Claude vision;
> - `type: document` — `application/pdf` → нативный PDF-разбор Claude;
> - `type: text` — `text/plain`, `text/markdown`, `text/csv`, `application/json` → инлайн как текст с разметкой имени файла.
>
> MIME вне allowlist → `422`; рассогласование заявленного `mediaType` и реального содержимого (magic bytes) → `422`; невалидный base64 → `422`. URL-вложения не поддерживаются (только inline). Лимиты (дефолты, конфигурируемы): ≤ 10 вложений; одно ≤ 5 MB (image) / 8 MB (document); суммарно ≤ 10 MB; PDF ≤ 100 страниц; тело `/v1/chat/run` ≤ 12 MB (повышенный лимит **только** этого роута). **Биллинг неизменен: сообщение с вложениями = 1 кредит** ([ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)). Содержимое вложений не логируется.
>
> Пример `attachments[]`:
> ```json
> [
>   { "type": "image", "mediaType": "image/png", "filename": "task.png", "data": "<base64>" },
>   { "type": "document", "mediaType": "application/pdf", "filename": "report.pdf", "data": "<base64>" }
> ]
> ```

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `status` | `assistant_message` \| `tool_call` \| `blocked` | исход шага |
| `sessionId` | string (uuid) | сессия (новая или переданная) |
| `messageStepId` | string (uuid) \| null | ключ **хода** (один на сообщение, переиспользуется во всех tool-раундах); `null` при `blocked`. Совпадает с `steps[].messageStepId` в истории. ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md)) |
| `stepId` | string (uuid) \| null | id **конкретного** assistant/tool-шага этого ответа; `null` при `blocked`. Совпадает с `steps[].id` в истории `GET /v1/chats/{id}`. ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md)) |
| `assistantMessage` | string, опц. | присутствует при `assistant_message` |
| `toolCall` | object `{ id, name, args }`, опц. | присутствует при `tool_call`; `id` — публичный UUID для последующего `/chat/tool-result` (≠ `stepId`) |
| `blockReason` | enum, опц. | присутствует при `blocked` (см. [раздел 12](#12-blockreason--справочник)) |
| `usage` | object `{ inputTokens, outputTokens, model }` | при `assistant_message`/`tool_call`; нет при `blocked` |

> **Синхронизация с историей чата ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md)).** `messageStepId`/`stepId` дают клиенту ключ для склейки ответа генерации с шагами `GET /v1/chats/{id}` → `steps[]`: `stepId` = точный шаг (`steps[].id`), `messageStepId` = ход для группировки tool-loop-раундов (`steps[].messageStepId`). При `status=blocked` шаг/ход не создаются (блок до генерации) → оба `null`. На `/v1/chat/tool-result` `messageStepId` стабилен в пределах хода (равен исходному `/chat/run`), `stepId` — id нового шага этого ответа.

Значения `status`:
- **`assistant_message`** — финальный текстовый ответ Claude. На этом шаге списывается 1 кредит (для `mode=credits`).
- **`tool_call`** — Claude запросил исполнение client-side инструмента; iOS обязан исполнить и вернуть результат через `/v1/chat/tool-result`. Кредит на промежуточных tool-раундах не списывается. Server-side инструменты (`site.*`) исполняет backend сам и наружу как `tool_call` не отдаёт.
- **`blocked`** — генерация запрещена бизнес-правилом; смотри `blockReason`.

**Коды:** `200` (вкл. blocked); `401` (нет JWT); `403` (`userId ≠ sub`); `404` (сессия не найдена); `413` (тело > 12 MB — повышенный лимит этого роута под inline base64-вложения, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)); `422` (схема/`message` > 32 KB/`context` > 64 KB/вложение вне allowlist/невалидный base64/MIME-mismatch/PDF page-guard); `429` (rate limit); `502/5xx` (ошибка Anthropic/внутренняя).

---

### POST /v1/chat/tool-result
Приём результата client-side инструмента от iOS и продолжение того же шага.

**Заголовки:** как у `/v1/chat/run`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` |
| `sessionId` | string (uuid) | сессия шага |
| `toolCallId` | string (uuid) | = `toolCall.id` из предыдущего ответа |
| `result` | object | результат исполнения (≤ 256 KB) |
| `error` | object `{ code, message }` | если инструмент завершился ошибкой |

Ровно одно из `result` / `error`.

**Response (200):** та же схема, что у `/v1/chat/run` (следующий шаг — снова `assistant_message` / `tool_call` / `blocked`).

**Коды:** `200`; `401`; `403` (чужой `toolCallId`/`userId ≠ sub`); `404` (`toolCallId` не принадлежит сессии); `413`; `422` (`result` не соответствует схеме инструмента или > 256 KB); `429`; `502/5xx`.

---

## 5. Policy

### GET /v1/policy/effective
Эффективные права пользователя для отрисовки UI (что доступно, почему заблокировано).

**Заголовки:** `Authorization: Bearer <JWT>`. `userId` берётся из `sub` — query-параметров нет.

**Response (200):**
| Поле | Тип | Семантика |
|---|---|---|
| `isSubscribed` | bool | активная подписка |
| `trialRemaining` | int | 1, если нет подписки и trial не использован; иначе 0 |
| `creditsBalance` | int | текущий баланс кредитов |
| `byokEnabled` | bool | BYOK включён и ключ валиден |
| `canGenerateCreditsMode` | bool | можно ли генерировать в режиме credits |
| `canGenerateByokMode` | bool | можно ли генерировать в режиме byok |
| `reasons` | array | причины блокировки для недоступных режимов (подмножество blockReason **без** `rate_limited`) |

> `rate_limited` в `reasons[]` не входит — это транспортный концерн (HTTP `429`), а не бизнес-policy.

**Коды:** `200`; `401`; `429`; `5xx`.

---

## 6. Wallet

### GET /v1/wallet
Баланс и последние транзакции пользователя.

**Заголовки:** `Authorization: Bearer <JWT>`.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `balance` | int | текущий баланс |
| `lastTransactions` | array `{ id, type, amount, createdAt, meta }` | последние N (дефолт 20), `type` ∈ `credit`\|`debit`; `meta` без секретов |

**Коды:** `200`; `401`; `429`; `5xx`.

---

### POST /v1/wallet/consume
Списание кредитов (внутренний биллинговый контракт; штатно вызывается оркестратором, прямой клиентский вызов возможен, но не нужен в обычном flow).

**Заголовки:** `Authorization: Bearer <JWT>`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` |
| `sessionId` | string (uuid) | сессия |
| `requestId` | string | **ключ идемпотентности** списания (для chat-debit это `messageStepId` шага, не `X-Request-Id`) |
| `amount` | int > 0 | целые кредиты; для chat-debit всегда 1 |
| `meta` | object | usage/model для аудита; на `amount` не влияет |

**Response (200):** `{ "newBalance": int, "ledgerTxId": "uuid" }`. Повторный тот же `requestId` с тем же payload → возвращает существующий `ledgerTxId` без повторного списания (идемпотентно).

**Коды:** `200`; `401`; `403`; `404` (`session_not_found`); `409` (тот же ключ — другой payload, либо `insufficient_credits`); `422`; `429`; `5xx`.

---

## 7. Subscription

### POST /v1/subscription/sync
Синхронизация статуса подписки по StoreKit-транзакции. Сервер верифицирует транзакцию (подпись/App Store Server API), не доверяя клиенту. При активации/продлении начисляется фиксированный пакет кредитов.

**Заголовки:** `Authorization: Bearer <JWT>`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` |
| `transaction` | object | подписанный StoreKit payload (JWS / App Store receipt). **Не логируется** |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `isSubscribed` | bool | активна ли подписка |
| `expiresAt` | string (ISO8601) \| null | срок действия |
| `plan` | string \| null | план |

Идемпотентно по `transactionId` периода: повторный sync той же транзакции не начисляет кредиты повторно. Refund/revocation → `isSubscribed=false`.

**Коды:** `200`; `401`; `403`; `422` (невалидная/поддельная транзакция — подписка не меняется); `429`; `502/5xx` (ошибка App Store API).

---

## 8. BYOK

«Bring Your Own Key» — пользователь приносит собственный Anthropic API-ключ. Ключ шифруется at-rest (envelope encryption), наружу plaintext никогда не возвращается и не логируется.

Общий формат ответа всех трёх эндпоинтов: `{ "byokEnabled": bool, "keyStatus": <enum>, "activeModel": string | null }`.

**`keyStatus` — 6 значений** ([ADR-016](adr/ADR-016-extended-byok-statuses.md)):

| keyStatus | Значение |
|---|---|
| `missing` | ключ не задан |
| `validating` | ключ сохранён, валидация в процессе |
| `valid` | ключ рабочий (прошёл проверку Anthropic) |
| `invalid` | ключ отклонён (Anthropic вернул 401) |
| `offline` | валидацию не удалось выполнить из-за сетевой ошибки (не финальный вердикт) |
| `expired` | ключ был `valid`, но впоследствии отозван/истёк |

> Старые клиенты, знающие только `valid`/`invalid`/`missing`, обязаны трактовать любой неизвестный статус как «не `valid`» (BYOK недоступен), не падая.

**`activeModel`** — строка с активной моделью (например `claude-sonnet-4-6`) при `keyStatus=valid`; во всех остальных статусах — `null`.

### POST /v1/byok/set
Сохранить и провалидировать ключ.
**Заголовки:** `Authorization: Bearer <JWT>`.
**Request:** `{ "userId": "uuid", "apiKey": "string" }` (`apiKey` ≤ 4 KB, не логируется).
**Поведение:** шифрование (AES-256-GCM + KMS-обёртка DEK) → upsert; лёгкая валидация вызовом Anthropic → `keyStatus=valid|invalid`. Невалидный ключ сохраняется со `status=invalid`, `byokEnabled` не включается.
**Коды:** `200`; `401`; `403`; `422`; `429`; `502/5xx`.

### POST /v1/byok/toggle
Включить/выключить использование BYOK.
**Заголовки:** `Authorization: Bearer <JWT>`.
**Request:** `{ "userId": "uuid", "enabled": bool }`.
**Поведение:** включить можно только при `keyStatus=valid`; иначе возвращается текущий статус без включения.
**Коды:** `200`; `401`; `403`; `422`; `429`; `5xx`.

### POST /v1/byok/delete
Удалить сохранённый ключ.
**Заголовки:** `Authorization: Bearer <JWT>`.
**Request:** `{ "userId": "uuid" }`.
**Response (200):** `{ "byokEnabled": false, "keyStatus": "missing", "activeModel": null }`.
**Коды:** `200`; `401`; `403`; `429`; `5xx`.

---

## 9. Admin

Операторские/саппорт-действия. **Авторизация — только `X-Admin-Token`** (пользовательский JWT не подходит). Отдельный rate limit (дефолт 10 req/min per source IP), тело ≤ 8 KB, строгая валидация. Admin-эндпоинты **не** создают пользователей.

### POST /v1/admin/wallet/grant
Ручное начисление кредитов пользователю (саппорт/компенсация).

**Заголовки:** `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен), `Content-Type: application/json`.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | существующий пользователь |
| `amount` | int > 0 | целые кредиты; `≤ 0` → `422` |
| `idempotencyKey` | string (≤ 128) | ключ идемпотентности начисления |
| `reason` | string (≤ 512) | **обязателен**; пишется в audit и meta |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `newBalance` | int | баланс после начисления |
| `ledgerTxId` | string (uuid) | id credit-транзакции |
| `idempotentReplay` | bool | `true`, если ключ уже использовался с тем же payload (повторного начисления не было) |

Каждое начисление пишет audit-событие `admin_grant` (без секрета токена).

**Коды:** `200`; `401` (нет/неверный `X-Admin-Token`); `404` (`user_not_found` — admin не создаёт пользователей); `409` (тот же `idempotencyKey`, другой `amount`); `422` (нет `reason` / `amount ≤ 0` / схема); `429` (admin rate limit); `5xx`.

### GET /v1/admin/wallet/{userId}
Read-only просмотр кошелька для саппорта.

**Заголовки:** `X-Admin-Token` (обязателен).

**Path:** `userId` — UUID пользователя.

**Response (200):**
```json
{ "userId": "uuid", "balance": 1100,
  "lastTransactions": [ { "id": "uuid", "type": "credit|debit", "amount": 100, "createdAt": "ISO8601", "meta": {} } ] }
```

**Коды:** `200`; `401`; `404` (`user_not_found`); `429`; `5xx`.

---

## 10. Website-builder / Preview

> **Опциональная фича ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)).** Основной поток сервиса — чат-агрегатор без проекта. Website-builder активируется **только** когда сессия создана с `projectId`; без проекта server-side `site.*` Claude не предлагаются и эта часть не задействуется.

Две части: **(A) server-side инструменты `site.*`** (исполняет backend, наружу как HTTP не торчат — это инструменты внутри chat tool-loop) и **(B) публичный preview-эндпоинт** для открытия сгенерированного сайта.

### A. Server-side инструменты `site.*`

Исполняются **backend'ом** внутри tool-loop `/chat/run` — iOS их **не** исполняет и не видит как `tool_call`. `userId` и проект берутся из серверного контекста сессии (модель не может записать в чужой проект). **Предлагаются Claude только при наличии `chat_sessions.project_id`** ([ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)); в «чистом чате» (без `projectId`) отсутствуют. Перечислены здесь для понимания возможностей сервиса; интеграция iOS с ними прямая не требуется.

| Инструмент | Тип | Назначение |
|---|---|---|
| `site.write_file` | mutate (audit) | записать/перезаписать файл сайта (`path`, `content`, `contentType`, `encoding`) |
| `site.preview` | utility | сгенерировать signed URL превью (`{ url, expiresAt }`) |
| `site.list` | read | список файлов проекта |
| `site.read` | read | прочитать файл |
| `site.delete` | mutate (audit) | удалить файл |

Ошибки исполнения инструмента (превышение лимита, неверный content-type, path-traversal, файл не найден) возвращаются Claude как `is_error` внутри loop, **не** как HTTP-ошибка пользователю.

### B. GET /v1/preview/{projectId}/{token}/{path}
Публичная отдача статики сгенерированного сайта по signed URL. **Без пользовательского JWT** — авторизация в подписи.

**Заголовки запроса:** не требуются (публичный).

**Path-параметры:**
| Параметр | Прим. |
|---|---|
| `projectId` | внутренний UUID проекта |
| `token` | `<exp>.<hmac>` — срок + HMAC-SHA256 подпись |
| `path` | относительный путь файла (может содержать `/`) |

**Response (200):** тело файла; `Content-Type` строго из сохранённого `content_type` (allowlist). Заголовки безопасности: `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'self'; frame-ancestors 'self'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Cache-Control: private, no-store`. **Без `Set-Cookie`**.

**Коды:** `200`; `403` (подпись невалидна / TTL истёк / чужой владелец); `404` (проект или файл не найдены — намеренно `404`, чтобы не раскрывать существование чужих ресурсов).

---

## 11. Health / Docs

Служебные эндпоинты — **без авторизации** (кроме опц. защиты `/metrics`).

| Метод | Путь | Auth | Назначение | Ответ |
|---|---|---|---|---|
| GET | `/health` | нет | liveness (процесс жив) | `200 {status:"ok"}` |
| GET | `/healthz` | нет | **алиас `/health`** для healthcheck Traefik/smoke ([ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)) | `200 {status:"ok"}` |
| GET | `/ready` | нет | readiness (БД + Redis доступны) | `200 {db:"ok",redis:"ok"}` или `503` |
| GET | `/metrics` | scrape-токен/сеть | Prometheus-метрики | exposition; `403` без `X-Scrape-Token` (если включён) |
| GET | `/docs`, `/redoc`, `/openapi.json` | нет (управляется флагом) | OpenAPI-документация (на русском, JWT scheme, теги по модулям) | схема API; `404` если `DOCS_ENABLED=false` (рекомендация для prod) |

---

## 17. Chats

История и управление чатами пользователя поверх существующих сессий (`chat_sessions`/`chat_steps`/`tool_calls`). Все эндпоинты строго скоупятся `sub` JWT: чужой/несуществующий чат → `404` (существование чужих ресурсов не раскрывается). Сортировка списка: закреплённые сверху (`isPinned`), затем по свежести (`updatedAt`).

**Общие заголовки:** `Authorization: Bearer <JWT>` (обязателен).

### GET /v1/chats
Список чатов с пагинацией и поиском.

**Query-параметры:**
| Параметр | Тип | Прим. |
|---|---|---|
| `q` | string, опц. | поиск по заголовку и тексту первого сообщения |
| `cursor` | string, опц. | непрозрачный (opaque) курсор следующей страницы — берётся из `nextCursor` предыдущего ответа |
| `limit` | int, опц. | размер страницы, 1..100 (дефолт 30) |

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `items` | array | элементы текущей страницы |
| `items[].id` | string (uuid) | идентификатор чата |
| `items[].title` | string \| null | заголовок (автоген из первого сообщения; null до генерации) |
| `items[].preview` | string \| null | срез текста последнего сообщения |
| `items[].assistantMode` | `chat` \| `code` | тип ассистента сессии |
| `items[].isPinned` | bool | закреплён ли чат |
| `items[].workspaceProjectId` | string (uuid) \| null | привязка к рабочему пространству (Спринт 2; сейчас всегда `null`) |
| `items[].updatedAt` | string (ISO8601) | время последнего обновления |
| `nextCursor` | string \| null | курсор следующей страницы; `null`, если страниц больше нет |

**Коды:** `200`; `401`; `422` (`limit` вне диапазона/битый `cursor`); `429`; `5xx`.

### GET /v1/chats/{id}
История шагов чата (упорядочены по времени).

**Path:** `id` — UUID чата.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `id` | string (uuid) | идентификатор чата |
| `title` | string \| null | заголовок |
| `assistantMode` | `chat` \| `code` | тип ассистента (assistant_mode) |
| `mode` | `credits` \| `byok` | режим оплаты сессии (billing_mode) |
| `steps` | array | шаги, упорядоченные по `chat_steps.seq` (порядок вставки, ADR-021); `createdAt` — информационный timestamp |
| `steps[].id` | string (uuid) | идентификатор шага |
| `steps[].messageStepId` | string (uuid) | message-шаг (биллинг-ключ) |
| `steps[].role` | `user` \| `assistant` \| `tool` | роль шага |
| `steps[].payload` | object | content-блоки шага (без raw provider id) |
| `steps[].usage` | object \| null | потребление токенов (для assistant-шагов) |
| `steps[].createdAt` | string (ISO8601) | время создания шага |

**Коды:** `200`; `401`; `404` (чужой/несуществующий чат); `429`; `5xx`.

### GET /v1/chats/{id}/steps
Steps-view — агрегированные шаги одного message-шага (tool-calls/reasoning) для UI («N steps»). Только доменные имена инструментов, без raw provider id.

**Path:** `id` — UUID чата.
**Query:** `messageStepId` (uuid, опц.) — конкретный message-шаг; по умолчанию последний.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `messageStepId` | string (uuid) | message-шаг, для которого построен steps-view |
| `stepCount` | int | число шагов |
| `steps[].kind` | `reasoning` \| `tool_call` \| `tool_result` \| `assistant_message` | тип шага для UI |
| `steps[].toolName` | string \| null | доменное имя инструмента (с точкой) или `null` |
| `steps[].summary` | string | краткое человекочитаемое описание |
| `steps[].createdAt` | string (ISO8601) | время шага |

**Коды:** `200`; `401`; `404`; `429`; `5xx`.

### PATCH /v1/chats/{id}
Переименование и/или закрепление. Требуется хотя бы одно поле.

**Path:** `id` — UUID чата.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `title` | string, опц. | новый заголовок (≤ 200 символов) |
| `isPinned` | bool, опц. | закрепить/открепить |

**Response (200):** `{ "id": "uuid", "title": string|null, "isPinned": bool, "updatedAt": "ISO8601" }`.

**Коды:** `200`; `401`; `404`; `422` (ни одного поля / `title` > 200); `429`; `5xx`.

### DELETE /v1/chats/{id}
Удаление чата (каскадно — шаги и tool-calls). Повторное удаление уже удалённого → `404`.

**Path:** `id` — UUID чата.
**Response (200):** `{ "deleted": true }`.
**Коды:** `200`; `401`; `404`; `429`; `5xx`.

---

## 18. Profile

Профиль пользователя: редактируемое `displayName` + производный `accountId`. Скоуп — `sub` JWT.

**Заголовки:** `Authorization: Bearer <JWT>`.

### GET /v1/profile
**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `accountId` | string | человекочитаемый идентификатор, производная от `userId`; формат `XXXX-XXXX-XXXXX` (две 4-значные цифровые группы + 5-символьная alphanumeric группа из алфавита `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, напр. `8472-1936-AXQ5K`). Детерминирован и стабилен; **не** секрет и **не** ключ авторизации (авторизация всегда по JWT `sub`) |
| `displayName` | string \| null | отображаемое имя (или `null`, если не задано) |
| `createdAt` | string (ISO8601) | дата создания аккаунта |

**Коды:** `200`; `401`; `429`; `5xx`.

### PATCH /v1/profile
**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `displayName` | string | новое имя (≤ 80 символов); пустая строка → сброс в `null` |

**Response (200):** тот же объект, что у `GET /v1/profile`, с обновлённым `displayName`.

**Коды:** `200`; `401`; `422` (`displayName` > 80 / схема); `429`; `5xx`.

---

## 19. Preferences

Пользовательские настройки. Источник дефолта `assistantMode` для `/chat/run` ([ADR-012](adr/ADR-012-assistant-mode-vs-billing-mode.md)). Если строка ещё не создана — возвращаются дефолты (`chat` / `true` / `{}`).

**Заголовки:** `Authorization: Bearer <JWT>`.

### GET /v1/preferences
**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `defaultAssistantMode` | `chat` \| `code` | дефолтный тип ассистента; ортогонален billing_mode |
| `notificationsEnabled` | bool | единый toggle уведомлений (push-токены — модуль notifications, Спринт 3) |
| `codeDefaults` | object | дефолты Code-контекста (язык и т.п.); без секретов |

**Коды:** `200`; `401`; `429`; `5xx`.

### PATCH /v1/preferences
Частичное обновление (любое подмножество полей); создаёт строку при отсутствии (upsert). Требуется хотя бы одно поле.

**Request:** любое подмножество `{ "defaultAssistantMode": "chat"|"code", "notificationsEnabled": bool, "codeDefaults": object }`. `codeDefaults` — ≤ 8 KB сериализованного JSON, без секретов (ключи вида `key`/`token`/`secret` → `422`).

**Response (200):** полный актуальный объект настроек (как у `GET`).

**Коды:** `200`; `401`; `422` (ни одного поля / `codeDefaults` > 8 KB / секреты в `codeDefaults` / схема); `429`; `5xx`.

---

## 20. Tokens

Разовая покупка пакетов токенов через **consumable StoreKit IAP** → начисление кредитов на баланс ([ADR-015](adr/ADR-015-consumable-token-iap.md)). Отдельно от подписки (`/v1/subscription/sync` — auto-renewable). Тег `Tokens`. Скоуп — `sub` JWT.

**Заголовки:** `Authorization: Bearer <JWT>` (обязателен), `Content-Type: application/json`.

> ✅ **Требует активной подписки ([Q-015-1](99-open-questions.md) Closed = вариант B, 2026-06-02):** покупка токенов доступна **только подписчикам** (`subscription.status == active`) — это докупка кредитов сверх месячного пакета. Без активной подписки `POST /v1/tokens/purchase` возвращает `403` с `code=subscription_required`. PM/iOS: показывать покупку токенов только активным подписчикам; для неподписанных — CTA на оформление подписки.

### POST /v1/tokens/purchase
Обработка consumable-покупки пакета токенов: верификация StoreKit-транзакции (сервер не доверяет клиенту), маппинг `productId → credits` (server-side), идемпотентное начисление кредитов.

**Request:**
| Поле | Тип | Прим. |
|---|---|---|
| `userId` | string (uuid) | = `sub` JWT |
| `transaction` | object | подписанный StoreKit **consumable** payload (JWS / App Store Server API). **Не логируется** (redaction) |

**Поведение:** сначала сервер проверяет **активную подписку** пользователя ([Q-015-1](99-open-questions.md) = вариант B) — нет активной подписки → `403 subscription_required`, начисление не происходит. Затем верифицирует транзакцию (общий с subscription verifier, включая `STOREKIT_TEST_MODE` для e2e), извлекает `transactionId` и `productId`, разрешает число кредитов через server-side маппинг `TOKEN_PRODUCTS` (клиент не задаёт количество кредитов) и начисляет их как `credit`-транзакцию идемпотентно по `transactionId` (`meta.source=token_purchase`). Повтор той же транзакции не начисляет повторно.

**Response (200):**
| Поле | Тип | Прим. |
|---|---|---|
| `creditsAdded` | int | начислено кредитов за эту покупку; `0` при идемпотентном повторе (уже обработанная транзакция) |
| `newBalance` | int | баланс после начисления |
| `transactionId` | string | идентификатор обработанной StoreKit-транзакции |

**Коды:** `200`; `401` (нет/невалидный JWT); `403` — два случая: `code=subscription_required` (**нет активной подписки**, [Q-015-1](99-open-questions.md) вариант B) или `code=forbidden` (`userId ≠ sub`); `422` (невалидная/поддельная транзакция — кредиты не начисляются; либо неизвестный `productId` вне `TOKEN_PRODUCTS`); `429` (rate limit); `502/5xx` (ошибка App Store API / внутренняя). Примечание: `403 subscription_required` — это error-ответ операции пополнения, **не** `200+blocked` (правило [ADR-004](adr/ADR-004-blocked-http-200.md) действует только для эндпоинтов генерации/политики).

### GET /v1/tokens/products
Каталог доступных пакетов токенов (`productId → credits`). Цены отображает клиент из StoreKit; backend отдаёт только число кредитов на пакет.

**Response (200):**
```json
{ "products": [
    { "productId": "tokens_1500", "credits": 1500 },
    { "productId": "tokens_600",  "credits": 600 },
    { "productId": "tokens_250",  "credits": 250 },
    { "productId": "tokens_100",  "credits": 100 }
] }
```
Источник — server-side `TOKEN_PRODUCTS` (env/config, [07-deployment.md](07-deployment.md)). Состав/числа конфигурируемы и должны совпадать с заведёнными в App Store Connect IAP.

**Коды:** `200`; `401`; `429`; `5xx`.

---

## 12. blockReason — справочник

Когда `status="blocked"` (HTTP `200`), поле `blockReason` принимает одно из 8 значений. Что показывать в UI:

| blockReason | Значение | Что показать пользователю в UI |
|---|---|---|
| `trial_used` | Бесплатная попытка (1 lifetime) уже израсходована, подписки нет | Предложить оформить подписку |
| `subscription_required` | Действие требует подписки, её нет | CTA на покупку подписки |
| `subscription_expired` | Подписка истекла | Предложить продлить подписку |
| `credits_empty` | Кредиты закончились (есть подписка/режим credits) | Сообщить об исчерпании кредитов; предложить дождаться следующего периода или продлить |
| `byok_disabled` | Режим byok выбран, но BYOK не включён | Подсказать включить BYOK или добавить ключ в настройках |
| `byok_invalid` | BYOK-ключ невалиден (отклонён Anthropic) | Попросить обновить/перепроверить API-ключ |
| `rate_limited` | Слишком много запросов (транспортный лимит) | «Слишком часто, попробуйте позже». Приходит как HTTP `429` на gateway-уровне; **не** входит в `policy/effective.reasons[]` |
| `policy_denied` | Запрет по политике (общий fallback) | Общее «Действие сейчас недоступно» |

> `reasons[]` в `GET /v1/policy/effective` содержит подмножество из первых 7 значений (без `rate_limited`) — для предварительной отрисовки доступности режимов в UI.

---

## 13. Tool-протокол

Чат работает по протоколу tool-use. Инструменты делятся на два класса по исполнителю.

### Client-side (исполняет iOS)
Backend возвращает `status="tool_call"`, iOS исполняет на устройстве и присылает результат через `POST /v1/chat/tool-result`.

| Инструмент | Тип | Назначение |
|---|---|---|
| `files.read` | read | прочитать файл |
| `files.write` | mutate | записать файл |
| `files.list` | read | список файлов/директорий |
| `files.mkdir` | mutate | создать директорию |
| `calendar.read` | read | прочитать события календаря |
| `calendar.create_events` | mutate | создать события |
| `reminders.read` | read | прочитать напоминания |
| `reminders.create` | mutate | создать напоминания |

### Server-side (исполняет backend)
Инструменты `site.*` (website-builder) — backend исполняет немедленно внутри tool-loop и продолжает диалог с Claude **без** round-trip к iOS. Наружу как `tool_call` **не** отдаются. См. [раздел 10A](#10-website-builder--preview).

### Формат
- **tool_call** (от backend к iOS): `toolCall = { "id": "<uuid>", "name": "<доменное имя, напр. files.read>", "args": { ... } }`. `id` — публичный стабильный идентификатор, используется в следующем `/chat/tool-result`.
- **tool-result** (от iOS к backend): `{ "userId", "sessionId", "toolCallId": "<id из toolCall>", "result": { ... } }` либо `{ ..., "error": { "code", "message" } }`. Ровно одно из `result`/`error`.
- Имена инструментов в публичном контракте — доменные, с точкой (`files.read`). Внутреннее преобразование к формату Anthropic — деталь реализации, iOS её не касается.

---

## 14. Монетизация (кратко)

- **Trial:** ровно **1 бесплатное сообщение lifetime** на пользователя (флаг `trial_used`). Израсходован → `trial_used`.
- **Подписка:** при активации/продлении периода начисляется **фиксированный пакет кредитов** (`SUBSCRIPTION_CREDITS_PER_PERIOD`, дефолт **1000**) на баланс. Кредиты накапливаются между периодами (на старте не сгорают).
- **Покупка токенов (consumable IAP, [раздел 20](#20-tokens)):** второй источник пополнения баланса — разовая покупка пакета токенов начисляет кредиты ([ADR-015](adr/ADR-015-consumable-token-iap.md)). Покупка токенов **требует активной подписки** ([Q-015-1](99-open-questions.md) Closed = вариант B): без активной подписки запрос отклоняется → `403 subscription_required` (policy-guard до grant, [ADR-002](adr/ADR-002-access-policy-state-machine.md)). См. [раздел 20](#20-tokens).
- **Стоимость:** **1 кредит = 1 завершённое сообщение** (финальный `assistant_message`). Промежуточные tool-раунды не тарифицируются. Длина ответа на стоимость не влияет.
- **Режимы генерации (`mode`):**
  - `credits` — списывается кредит с баланса, генерация на сервисном Anthropic-ключе.
  - `byok` — пользователь приносит свой Anthropic-ключ; кредиты не списываются.

---

## 15. Превью сайта для iOS

Как iOS открывает сгенерированный Claude сайт:

1. В ходе чата (`/v1/chat/run`) Claude вызывает server-side инструмент `site.write_file` (backend сохраняет файлы) и затем `site.preview`.
2. Backend генерирует **signed URL** вида `GET /v1/preview/{projectId}/{token}/{entry}` (по умолчанию `entry=index.html`) с TTL (дефолт **15 минут**). URL возвращается в результат tool-loop и доходит до пользователя как часть ответа Claude.
3. iOS открывает этот URL **напрямую** в браузере / `WKWebView` — без передачи JWT и без cookies. Авторизация целиком в подписи URL.
4. Контент отдаётся в **sandbox** (CSP `sandbox`, `X-Frame-Options: SAMEORIGIN`, `nosniff`, `no-store`), без cookies/credentials — пользовательский JS изолирован от API-origin.
5. После истечения TTL ссылка перестаёт работать (`403`) — нужно запросить новую через `site.preview` (новый chat-шаг).

---

## 16. Лимиты и rate limits

### Размер payload
| Лимит | Значение | Нарушение |
|---|---|---|
| Общий размер тела запроса (все роуты, кроме `/chat/run`) | ≤ 512 KB | `413` |
| Тело `/v1/chat/run` (повышенный — под inline base64-вложения, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) | ≤ 12 MB | `413` |
| `message` (`/chat/run`) | ≤ 32 KB | `422` |
| `context` (`/chat/run`) | ≤ 64 KB | `422` |
| `attachments[]` (`/chat/run`, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) | ≤ 10 шт.; одно ≤ 5 MB image / 8 MB document; суммарно ≤ 10 MB; PDF ≤ 100 стр. | `413`/`422` |
| `result` (`/chat/tool-result`) | ≤ 256 KB | `422` |
| `apiKey` (BYOK) | ≤ 4 KB | `422` |
| Тело admin-запроса | ≤ 8 KB | `413`/`422` |

### Rate limits (дефолты; калибруются на проде)
| Эндпоинт | Лимиты |
|---|---|
| `POST /v1/chat/run` | 30 req/min per user · 60 req/min per device · 120 req/min per IP |
| Прочие POST `/v1/*` | 60 req/min per user |
| `/v1/admin/*` | 10 req/min per source IP |

Превышение → `429` (с телом `{ error.code: "rate_limited" }`).

### Лимиты website-builder
| Лимит | Значение |
|---|---|
| Размер одного файла сайта | ≤ 1 MB |
| Суммарный размер проекта | ≤ 10 MB |
| Число файлов в проекте | ≤ 200 |
| TTL preview signed URL | 15 минут (дефолт) |

Все значения — конфигурируемые дефолты из server-side настроек; на проде могут быть откалиброваны.

---

## 21. Auth (выпуск токена)

Встроенный issuer ([ADR-018](adr/ADR-018-embedded-auth-issuer.md), [modules/auth](modules/auth/README.md)): backend САМ выпускает и верифицирует RS256 JWT. Эндпоинты `/v1/auth/*` — **без** пользовательского JWT (точка его получения); защита — per-IP rate-limit. Первичная аутентификация — **device-based** (анонимная). Email/пароль и Apple Sign-In — опциональное расширение, не MVP ([Q-018-2](99-open-questions.md)).

### POST /v1/auth/register
Создать/найти идентичность устройства и выдать токены.
**Заголовки:** `Content-Type: application/json` (без `Authorization`).
**Request:** `{ "deviceId": "string (опц.)" }` — если не передан/пуст, backend сгенерирует UUIDv4.
**Response 200:**
```json
{ "userId": "uuid", "deviceId": "string", "accessToken": "<JWT>", "tokenType": "Bearer",
  "expiresIn": 3600, "refreshToken": "<opaque>", "refreshExpiresIn": 2592000 }
```
- Известный `deviceId` → возвращается тот же `userId` (идемпотентно). Новый → новый `userId` + provisioning.
**Коды:** `200`; `422` невалидный `deviceId`; `429` rate-limit; `503` issuer не сконфигурирован (нет приватного ключа).

### POST /v1/auth/token
Токены для **уже зарегистрированного** устройства. **Request:** `{ "deviceId": "string" }` (обязателен). Ответ — как у register. **Коды:** `200`; `422`; `429`; `503`.

### POST /v1/auth/refresh
Обмен refresh-token на новую пару (single-use rotation). **Request:** `{ "refreshToken": "<opaque>" }`. Ответ — новая пара (тот же `userId`). Reuse использованного/истёкшего → `401` + ревокация цепочки устройства. **Коды:** `200`; `401`; `422`; `429`.

### GET /v1/auth/jwks
JWKS с публичным ключом (для самопроверки/отладки). Опционально-публичный (`AUTH_JWKS_ENABLED`, дефолт `true`); приватный ключ никогда не отдаётся. **Коды:** `200`; `404` (выключен/не сконфигурирован).

### Токены (кратко)
- **Access-token:** RS256 JWT, TTL 1ч; claims `sub`/`device_id`/`iss`/`aud`/`iat`/`exp`, заголовок `kid`. Используется как `Authorization: Bearer` для всех `/v1/*`.
- **Refresh-token:** opaque, TTL 30д, single-use rotation, хранится как хэш (серверная ревокация при logout/краже).

---

## 22. Tools (каталог инструментов)

### GET /v1/tools
Машиночитаемый каталог всех поддерживаемых backend tools (13). [ADR-019](adr/ADR-019-tools-catalog-endpoint.md), [chat-orchestrator/02-api-contracts](modules/chat-orchestrator/02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019).
**Заголовки:** `Authorization: Bearer <JWT>` (обязателен — как все `/v1/*`; каталог не секретен, но контур единый).
**Response 200:**
```json
{ "tools": [ { "name": "files.read", "description": "...", "mutating": false,
  "execution": "client", "inputSchema": { "type": "object", "properties": { } } } ] }
```
- `name` — доменное имя с точкой. `mutating` (bool) — требует ли audit. `execution` — `"client"` (исполняет iOS) или `"server"` (`site.*`, исполняет backend, [ADR-011](adr/ADR-011-server-side-tools.md)). `inputSchema` — JSON Schema args.
- Возвращает **полный** реестр (не фильтруется по `assistantMode`). Список из 13: `files.read/write/list/mkdir`, `calendar.read/create_events`, `reminders.read/create` (client), `site.write_file/preview/list/read/delete` (server).
**Коды:** `200`; `401`; `429`.

---

## 23. Как тестировать через Swagger

Интерактивная документация — `/docs` (Swagger UI) при `DOCS_ENABLED=true` (dev/staging; на `broadnova.shop` сейчас включена). Порядок ручной проверки:

1. **Получить токен.** Открой `POST /v1/auth/register` → «Try it out» → тело `{}` (или `{ "deviceId": "my-test-device" }`) → «Execute». В ответе скопируй `accessToken`.
   - Повторно для того же `deviceId` — `POST /v1/auth/token`. Обновить пару — `POST /v1/auth/refresh` с `refreshToken`.
   - Если ответ `503` — на сервере не сконфигурирован приватный ключ подписи (issuer выключен); см. [§21](#21-auth-выпуск-токена) и prod-checklist деплоя.
2. **Авторизоваться (`bearerAuth`).** Кнопка «Authorize» (вверху справа) → схема `bearerAuth` → вставь `accessToken` (без слова `Bearer`, Swagger добавит сам) → «Authorize». Теперь все `/v1/*` шлются с `Authorization: Bearer <JWT>`.
3. **Дёргать защищённые эндпоинты.** Любой `/v1/*` (например `GET /v1/tools`, `GET /v1/policy/effective`, `POST /v1/chat/run`). В теле, где есть `userId`, подставляй `userId` = `sub` из токена (иначе `403`).
4. **Admin-эндпоинты (`adminToken`).** Для `/v1/admin/*` — отдельная схема в «Authorize»: `adminToken` → вставь значение `ADMIN_API_SECRET` (уйдёт как заголовок `X-Admin-Token`). Пользовательский `bearerAuth` к admin-эндпоинтам не подходит — контуры изолированы.
5. **Preview** через Swagger не тестируется обычным способом — `GET /v1/preview/*` открывается прямой signed-URL ссылкой в браузере (авторизация в URL, не в заголовке).

> Если `/docs` отдаёт `404` — на этом окружении `DOCS_ENABLED=false` (рекомендация для prod). Тогда тестируй через `curl`/Postman по контрактам выше.

---

*Документ — внешний deliverable для PM/интеграторов. Источник истины по контрактам — `docs/modules/*/02-api-contracts.md` и `docs/05-security.md`; продуктовые правила — ADR-004 (blockReason), ADR-006 (монетизация), ADR-009 (admin), ADR-010 (preview), ADR-011 (server-side tools), ADR-018 (auth), ADR-019 (tools catalog).*
