# Chat Orchestrator — API Contracts

## POST /v1/chat/run
Старт или продолжение агентного шага.

### Request
```json
{
  "userId": "uuid",
  "projectId": "string",
  "sessionId": "uuid (optional)",
  "message": "string",
  "mode": "credits | byok",
  "assistantMode": "chat | code (optional)",
  "workspaceProjectId": "uuid (optional)",
  "attachments": [ { "id": "uuid" } ],
  "context": { "any": "object (optional)" }
}
```
- `sessionId` отсутствует → создаётся новая сессия. На сессию фиксируются: `mode` (billing_mode, credits|byok — **способ оплаты**, [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)), `assistantMode` (тип ассистента chat|code) и `workspaceProjectId` (привязка к рабочему пространству, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
- **`mode` vs `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)):** `mode` = `billing_mode` (оплата, без изменений — обратная совместимость). `assistantMode` = тип ассистента (chat|code), **новое опциональное** поле. При отсутствии → `user_preferences.default_assistant_mode` (модуль [preferences](../preferences/README.md)), при отсутствии preferences → `chat`. `assistantMode` влияет на base-system-prompt и состав tool-реестра ([Q-012-1](../../99-open-questions.md)), **НЕ** на policy/billing.
- `workspaceProjectId` (опц.) — если задан и принадлежит пользователю: `instructions` workspace добавляются к system-prompt, `workspace_files` подаются как контекст ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Чужой/несуществующий → `404`.
- `attachments[]` (опц., ≤ 10) — ссылки на ранее загруженные вложения ([ADR-014](../../adr/ADR-014-multimodal-attachments.md), `POST /v1/attachments`). Orchestrator резолвит, проверяет владельца (`attachments.user_id == sub`, иначе `403`/`404`), собирает Anthropic content-блоки (image → vision; document → нативный `document`/`extracted_text`). Биллинг — обычный chat-шаг (1 кредит, ADR-006 без изменений).
- Size-лимиты: `message` ≤ 32KB, `context` ≤ 64KB (см. [05-security.md](../../05-security.md)). Бинарные данные вложений идут **не** в этот JSON, а отдельным multipart-upload (`POST /v1/attachments`) — JSON-лимит `≤512KB` не нарушается ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)).
- При старте нового пользовательского message-шага Orchestrator генерирует `messageStepId` (UUID), персистирует его в `chat_steps.message_step_id` и `tool_calls.message_step_id`. Он един для всех tool-раундов шага (включая re-entry через `/chat/tool-result`) и используется как ключ идемпотентности credits-debit ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). `messageStepId` — внутренняя величина биллинга, не путать с gateway correlation `requestId` (`X-Request-Id`).

### Response (200)
```json
{
  "status": "assistant_message | tool_call | blocked",
  "sessionId": "uuid",
  "assistantMessage": "string (optional, при assistant_message)",
  "toolCall": { "id": "uuid", "name": "string", "args": { } },
  "blockReason": "enum (optional, при blocked)",
  "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" }
}
```
- `toolCall` присутствует только при `status=tool_call`. `toolCall.id` — **доменный UUID** (`= tool_calls.id`), стабильный публичный идентификатор для iOS и для последующего `/chat/tool-result`. Внутренний Anthropic `tool_use.id` (`toolu_...`) наружу **не** отдаётся (хранится в `tool_calls.provider_tool_use_id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).
- `blockReason` присутствует только при `status=blocked`.
- `usage` присутствует при `assistant_message`/`tool_call` (не при blocked).

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
Та же схема, что у `/v1/chat/run`.

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
Машиночитаемый каталог всех поддерживаемых backend tools (13). Источник — `src/app/chat/tools.py` (single source of truth: `_ARGS_BY_TOOL`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `anthropic_tool_definitions()`). Эндпоинт **не** параметризуется `assistantMode` — возвращает полный технический реестр backend (фильтрация по режиму — concern tool-loop'а, [Q-012-1](../../99-open-questions.md)).

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
