# Chat Orchestrator — API Contracts

## POST /v1/chat/run
Старт или продолжение агентного шага.

### Request
```json
{
  "userId": "uuid",
  "projectId": "string (optional)",
  "sessionId": "uuid (optional)",
  "message": "string",
  "mode": "credits | byok",
  "assistantMode": "chat | code (optional)",
  "workspaceProjectId": "uuid (optional)",
  "attachments": [
    {
      "type": "image | document | text",
      "mediaType": "image/png",
      "filename": "photo.png (optional)",
      "data": "<base64>"
    }
  ],
  "context": { "any": "object (optional)" }
}
```
- `sessionId` отсутствует → создаётся новая сессия. На сессию фиксируются: `mode` (billing_mode, credits|byok — **способ оплаты**, [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)), `assistantMode` (тип ассистента chat|code), `projectId` (опц., см. ниже) и `workspaceProjectId` (привязка к рабочему пространству, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
- **`projectId` (опц., [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)).** Основной поток сервиса — **чат-агрегатор**; website-builder — **опциональная** фича. Поле фиксируется на сессию при создании (как `mode`/`assistantMode`):
  - **без `projectId`** → «чистый чат»: сессия создаётся с `project_id = NULL`; server-side `site.*` tools **НЕ предлагаются** Claude (нет проекта для записи); прочие client-side tools (`files.*`/`calendar.*`/`reminders.*`) доступны по обычным правилам;
  - **с `projectId`** → website-builder доступен: `site.*` входят в tool-набор, как сейчас.
  - **Resume-сессия:** `projectId` берётся из сессии (`chat_sessions.project_id`); поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode` ([ADR-022 §4](../../adr/ADR-022-optional-project-and-tool-gating.md)). Гейтинг tools — [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022). Биллинг/policy от наличия `projectId` **не зависят** (1 кредит = 1 сообщение).
- **`mode` vs `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)):** `mode` = `billing_mode` (оплата, без изменений — обратная совместимость). `assistantMode` = тип ассистента (chat|code), **новое опциональное** поле. При отсутствии → `user_preferences.default_assistant_mode` (модуль [preferences](../preferences/README.md)), при отсутствии preferences → `chat`. `assistantMode` влияет на base-system-prompt и состав tool-реестра ([Q-012-1](../../99-open-questions.md)), **НЕ** на policy/billing.
- `workspaceProjectId` (опц.) — если задан и принадлежит пользователю: `instructions` workspace добавляются к system-prompt, `workspace_files` подаются как контекст ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Чужой/несуществующий → `404`.
- `attachments[]` (опц., ≤ `ATTACHMENT_MAX_COUNT`, дефолт 10) — **inline base64-вложения** ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), заменяет двухшаговую модель [ADR-014](../../adr/ADR-014-multimodal-attachments.md)). Принимаются **только** в первом (новом) пользовательском message-шаге `/chat/run`; в `/chat/tool-result` — **не** принимаются. Поля вложения:
  - `type` ∈ `image | document | text` — класс вложения.
  - `mediaType` — конкретный MIME, строго из allowlist (см. ниже); вне allowlist → `422 unsupported_media_type`.
  - `filename` (опц.) — для человекочитаемой разметки (особенно `text`-вложений).
  - `data` — base64-кодированное содержимое (валидный base64; невалидный → `422`).
  - **Маппинг в Anthropic content-блоки:** `image` → `{"type":"image","source":{"type":"base64",...}}`; `document` (PDF) → нативный `{"type":"document","source":{"type":"base64","media_type":"application/pdf",...}}`; `text` → `{"type":"text","text":"<filename>\n```\n<UTF-8 текст>\n```"}`.
  - **Allowlist `mediaType`:** `image` — `image/jpeg`, `image/png`, `image/gif`, `image/webp`; `document` — `application/pdf`; `text` — `text/plain`, `text/markdown`, `text/csv`, `application/json` ([Q-020-1](../../99-open-questions.md) — расширение).
  - **Валидация (фокус ревью, [05-security.md](../../05-security.md)):** соответствие `type`/`mediaType` реальному содержимому по magic bytes; лимиты проверяются **до** декодирования base64; PDF — guard числа страниц (анти-bomb). URL-вложения запрещены (нет backend-fetch).
  - **Реплей/хранение ([ADR-020 §3](../../adr/ADR-020-inline-base64-attachments-mvp.md)):** на первом витке полные content-блоки отправляются Claude; в `chat_steps.payload` сохраняется **лёгкий текстовый плейсхолдер** (НЕ base64); на последующих tool-витках реплеится только плейсхолдер (тяжёлый контент не повторяется).
  - **Биллинг:** обычный chat-шаг (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) без изменений); vision/PDF-токены входят в message-шаг, отдельной тарификации нет.
- Size-лимиты: `message` ≤ 32KB, `context` ≤ 64KB (см. [05-security.md](../../05-security.md)). **Тело `/v1/chat/run` имеет повышенный transport-лимит** (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB) для inline base64-вложений — общий лимит `≤512KB` прочих роутов **не меняется**, повышение применяется только к роуту `/v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), [05-security.md](../../05-security.md)). Лимиты на вложения: одно ≤ `ATTACHMENT_MAX_BYTES_IMAGE` (дефолт 5 MB) / `ATTACHMENT_MAX_BYTES_DOCUMENT` (дефолт 8 MB), суммарно ≤ `ATTACHMENT_TOTAL_BYTES` (дефолт 10 MB).
- При старте нового пользовательского message-шага Orchestrator генерирует `messageStepId` (UUID), персистирует его в `chat_steps.message_step_id` и `tool_calls.message_step_id`. Он един для всех tool-раундов шага (включая re-entry через `/chat/tool-result`) и используется как ключ идемпотентности credits-debit ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). `messageStepId` — внутренняя величина биллинга, не путать с gateway correlation `requestId` (`X-Request-Id`).

### Response (200)
```json
{
  "status": "assistant_message | tool_call | blocked",
  "sessionId": "uuid",
  "messageStepId": "uuid | null",
  "stepId": "uuid | null",
  "assistantMessage": "string (optional, при assistant_message)",
  "toolCall": { "id": "uuid", "name": "string", "args": { } },
  "blockReason": "enum (optional, при blocked)",
  "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" }
}
```
- `toolCall` присутствует только при `status=tool_call`. `toolCall.id` — **доменный UUID** (`= tool_calls.id`), стабильный публичный идентификатор для iOS и для последующего `/chat/tool-result`. Внутренний Anthropic `tool_use.id` (`toolu_...`) наружу **не** отдаётся (хранится в `tool_calls.provider_tool_use_id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).
- `blockReason` присутствует только при `status=blocked`.
- `usage` присутствует при `assistant_message`/`tool_call` (не при blocked).
- **`messageStepId` / `stepId` — идентификаторы синхронизации с историей чата ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), nullable).** Позволяют клиенту склеить ответ генерации с шагами `GET /v1/chats/{id}` → `steps[]`. Обе величины уже существуют в orchestrator: `messageStepId` = `chat_steps.message_step_id` (ключ хода, см. §below про генерацию), `stepId` = `chat_steps.id` (PK конкретного шага). Семантика по статусам:
  - `status=assistant_message`: `messageStepId` = ход; `stepId` = `id` финального assistant-шага (= `ChatStepSchema.id` этого шага в истории). **Оба присутствуют.**
  - `status=tool_call`: `messageStepId` = ход; `stepId` = `id` assistant-шага, содержащего `tool_use` (тот шаг истории, чей `payload` несёт этот `tool_use`-блок). `toolCall.id` **остаётся как есть** (provider-независимый доменный id tool-вызова для `/chat/tool-result`) — `toolCall.id` ≠ `stepId`. **Оба присутствуют.**
  - `status=blocked`: `messageStepId = null`, `stepId = null` — блокировка срабатывает в Policy Engine **до** генерации ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md), [ADR-004](../../adr/ADR-004-blocked-http-200.md)), `chat_steps`/ход **не создаются**, ссылаться не на что (согласовано с отсутствием `usage` при blocked).
- **Инвариант синка (нормативно):** `ChatResponse.messageStepId` / `ChatResponse.stepId` дословно совпадают с `ChatStepSchema.messageStepId` / `ChatStepSchema.id` соответствующего шага в [chats/02-api-contracts.md `GET /v1/chats/{id}` → `steps[]`](../chats/02-api-contracts.md#get-v1chatsid). Аддитивно/обратносовместимо: существующие поля, security, коды, пути не меняются ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).

### Правила
- Перед генерацией — обязательный вызов Policy Engine (ADR-002).
- `status=blocked` → HTTP 200, машиночитаемый `blockReason` (ADR-004).
- Для `status=tool_call` payload строго типизирован по схемам ниже.
- Тех. ошибки (auth/size/validation/upstream) — 4xx/5xx (см. api-gateway).

## POST /v1/chat/tool-result
Приём результата локального tool и продолжение шага.

### Request
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "toolCallId": "uuid",
  "result": { "any": "object" },
  "error": { "code": "string", "message": "string" }
}
```
- Ровно одно из `result` / `error` (валидатор `extra=forbid`).
- `result` ≤ 256KB.

### Response (200)
Та же схема, что у `/v1/chat/run` (включая `messageStepId` / `stepId`, [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).
- `messageStepId` **стабилен в рамках хода**: равен тому, что был выдан в исходном `/chat/run` этого хода (берётся из `tool_calls.message_step_id` по `toolCallId`, см. re-entry ниже) — это и есть смысл синка tool-loop: клиент держит один `messageStepId` на весь ход.
- `stepId` = `id` **нового** шага, который представляет этот ответ: assistant-tool_use следующего раунда (при `status=tool_call`) либо финальный assistant-шаг (при `status=assistant_message`). Ответ всегда указывает на **следующий шаг, порождённый Claude**, а не на только что принятый шаг-`tool_result`.
- `status=blocked` (если возникает на продолжении): `messageStepId`/`stepId` = `null` — как в `/chat/run`.

### Правила
- Проверка принадлежности `toolCallId` текущей сессии: `tool_calls.session_id == sessionId`, иначе `404`/`403`.
- Re-entry message-шага: `messageStepId` берётся из `tool_calls.message_step_id` найденного `toolCallId` (НЕ генерируется заново). Все ответы и финальный debit этого шага используют тот же `messageStepId`.
- Идемпотентность: повторный `toolCallId` со статусом `completed` → не пересылать в Anthropic, вернуть сохранённый следующий шаг (ADR-005).
- `result` валидируется по схеме соответствующего tool (см. ниже); несоответствие → `422`.

## Классы tools: client-side vs server-side ([ADR-011](../../adr/ADR-011-server-side-tools.md))
- **client-side** (`files.*`, `calendar.*`, `reminders.*`) — исполняет **iOS-клиент**: backend отдаёт `status=tool_call`,
  ждёт `tool_result` через `/v1/chat/tool-result`. Описаны в этом документе.
- **server-side** (`site.*`, website-builder) — исполняет **backend** немедленно в tool-loop, формирует `tool_result` сам
  и продолжает к Anthropic **без** round-trip к iOS; **НЕ** отдаётся клиенту как `status=tool_call`. Схемы и поведение —
  [modules/website-builder/02-api-contracts.md](../website-builder/02-api-contracts.md), [ADR-011](../../adr/ADR-011-server-side-tools.md).
- Orchestrator различает класс по доменному имени (статический реестр `SERVER_SIDE_TOOLS = {site.*}`). domain↔anthropic
  mapping (точка→подчёркивание) расширяется server-side именами (`site.write_file ↔ site_write_file`, …). Guard на число
  server-side раундов — `MAX_SERVER_TOOL_ROUNDS` (дефолт 16).
- **Гейтинг по наличию проекта ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)):** `site.*` (`SERVER_SIDE_TOOLS`) предлагаются Claude **только** когда у сессии есть `project_id` (создана с `projectId`). В «чистом чате» (`chat_sessions.project_id IS NULL`) `site.*` в tool-набор **не включаются** — Claude их не видит и не вызывает. См. [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022).

## Tools (backend ↔ iOS, client-side) — строго типизированные схемы
Backend только инициирует tool-call; исполняет клиент. Все мутирующие tools (`files.write`, `files.mkdir`, `calendar.create_events`, `reminders.create`) → audit-запись. Server-side `site.write_file`/`site.delete` также мутирующие (audit) — см. website-builder.

### Имена tools: доменный (iOS) vs Anthropic-формат
Публичный контракт с iOS (ТЗ §5) использует **доменные имена с точкой** (`files.read`, `calendar.create_events`, …). Anthropic Messages API требует имя tool по шаблону `^[a-zA-Z0-9_-]{1,128}$` — **точка недопустима**, dotted-имя → `400 invalid_request_error` (BUG-3, воспроизведено: dotted→400, underscore→200).

**Решение (без breaking change §5):** ввести двунаправленный маппинг `domain-name (точка) ↔ anthropic-name (подчёркивание)`. Преобразование детерминированное — замена `.`→`_`:

| Domain-name (iOS-facing, публичный) | Anthropic-name (только в Anthropic tool definitions) |
|---|---|
| `files.read` | `files_read` |
| `files.write` | `files_write` |
| `files.list` | `files_list` |
| `files.mkdir` | `files_mkdir` |
| `calendar.read` | `calendar_read` |
| `calendar.create_events` | `calendar_create_events` |
| `reminders.read` | `reminders_read` |
| `reminders.create` | `reminders_create` |

**Правила маппинга (нормативно):**
- Маппинг — единственный источник истины для соответствия имён; набор tools фиксирован (8 шт.), поэтому маппинг — статическая таблица (двунаправленный dict), а не «слепое» преобразование строк на лету. Обратный маппинг (`anthropic-name → domain-name`) валидирует, что Claude вернул известный tool; неизвестное имя → ошибка обработки tool_use (трактуется как upstream-аномалия, не доходит до iOS).
- При **сборке запроса** к Anthropic (`messages.create`, поле `tools[].name`) backend подставляет **anthropic-name**.
- При **парсинге ответа** Claude (`content` block `type=tool_use`, поле `name`) backend применяет **обратный маппинг** → доменное имя. Наружу — в `toolCall.name` ответов `/v1/chat/run` и `/v1/chat/tool-result`, а также в `tool_calls.tool_name` (БД/audit) — идёт **только доменный формат с точкой**.
- Строгая типизация args/result привязана к **доменным именам** (таблица схем ниже не меняется). Anthropic-имена — исключительно транспортная деталь слоя Anthropic-клиента и нигде, кроме поля `tools[].name`/`tool_use.name` протокола Anthropic, не фигурируют.
- Публичный tool-контракт с iOS (`toolCall.name`, схемы args/result) **не меняется** — это не breaking change.

| Tool | Тип | Args schema | Result schema |
|---|---|---|---|
| `files.read` | read | `{ "path": string }` | `{ "path": string, "content": string, "encoding": "utf8\|base64", "size": int }` |
| `files.write` | mutate | `{ "path": string, "content": string, "encoding": "utf8\|base64", "overwrite": bool }` | `{ "path": string, "bytesWritten": int }` |
| `files.list` | read | `{ "path": string, "recursive": bool }` | `{ "entries": [ { "name": string, "path": string, "isDir": bool, "size": int } ] }` |
| `files.mkdir` | mutate | `{ "path": string, "createIntermediates": bool }` | `{ "path": string, "created": bool }` |
| `calendar.read` | read | `{ "startDate": "ISO8601", "endDate": "ISO8601", "calendarId": string? }` | `{ "events": [ { "id": string, "title": string, "start": "ISO8601", "end": "ISO8601", "location": string?, "notes": string? } ] }` |
| `calendar.create_events` | mutate | `{ "events": [ { "title": string, "start": "ISO8601", "end": "ISO8601", "location": string?, "notes": string?, "calendarId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |
| `reminders.read` | read | `{ "listId": string?, "includeCompleted": bool }` | `{ "reminders": [ { "id": string, "title": string, "due": "ISO8601"?, "completed": bool, "notes": string? } ] }` |
| `reminders.create` | mutate | `{ "reminders": [ { "title": string, "due": "ISO8601"?, "notes": string?, "listId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |

### Общие правила схем
- Все схемы — Pydantic v2, `extra='forbid'`.
- Даты — ISO8601 (RFC3339), UTC или с offset.
- `path` валидируется как относительный/безопасный (без `..`-traversal) на стороне валидатора backend; фактический доступ — ответственность клиента.
- `error` (в tool-result) имеет форму `{ "code": string, "message": string }`; при `error` backend передаёт Claude tool_result с `is_error=true`.

### blockReason enum (повтор для удобства)
`trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | rate_limited | policy_denied` (источник — [ADR-004](../../adr/ADR-004-blocked-http-200.md)).

---

## GET /v1/tools — каталог инструментов ([ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md))
Машиночитаемый каталог всех поддерживаемых backend tools (13). Источник — `src/app/chat/tools.py` (single source of truth: `_ARGS_BY_TOOL`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `anthropic_tool_definitions()`). Эндпоинт **не** параметризуется ни `assistantMode`, ни наличием проекта — возвращает полный технический реестр backend (включая `site.*`). Runtime-фильтрация tool-набора, предлагаемого Claude (гейтинг `site.*` по наличию `project_id`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md); фильтрация по режиму, [Q-012-1](../../99-open-questions.md)), — concern tool-loop'а, а не каталога.

### Auth
- **JWT-protected** (как все `/v1/*`, кроме `/v1/preview/*`): `Authorization: Bearer <JWT>` обязателен. Каталог не секретен, но единообразие gateway-auth и снижение анонимного API-surface — обоснование в [ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md). Клиент к этому моменту уже имеет JWT (получен через `/v1/auth/register`, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)).
- Метод `GET` (read-only, кэшируемо). Per-user rate-limit как у прочих read-эндпоинтов.

### Response (200)
```json
{
  "tools": [
    {
      "name": "files.read",
      "description": "Read a file from the user's device.",
      "mutating": false,
      "execution": "client",
      "inputSchema": { "type": "object", "properties": { "path": { "type": "string" } }, "required": ["path"] }
    },
    {
      "name": "site.write_file",
      "description": "Write or overwrite a file in the website project...",
      "mutating": true,
      "execution": "server",
      "inputSchema": { "type": "object", "properties": { "...": {} } }
    }
  ]
}
```
- `name` — **доменное** имя с точкой (как в публичном iOS-контракте), НЕ anthropic-underscore (`files_read` — деталь Anthropic-транспорта, BUG-3).
- `description` — из `descriptions` в `anthropic_tool_definitions()`.
- `mutating` — `name ∈ MUTATING_TOOLS` (требует audit при исполнении).
- `execution` — `"server"` если `name ∈ SERVER_SIDE_TOOLS` (`site.*`, исполняет backend, [ADR-011](../../adr/ADR-011-server-side-tools.md)); иначе `"client"` (исполняет iOS).
- `inputSchema` — JSON Schema args (`_ARGS_BY_TOOL[name].model_json_schema()`).
- Порядок — детерминированный (по `_ARGS_BY_TOOL`).

### Полный список (13)
| name | execution | mutating |
|---|---|---|
| files.read | client | нет |
| files.write | client | **да** |
| files.list | client | нет |
| files.mkdir | client | **да** |
| calendar.read | client | нет |
| calendar.create_events | client | **да** |
| reminders.read | client | нет |
| reminders.create | client | **да** |
| site.write_file | **server** | **да** |
| site.preview | **server** | нет |
| site.list | **server** | нет |
| site.read | **server** | нет |
| site.delete | **server** | **да** |

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.
