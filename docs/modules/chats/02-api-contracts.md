# Chats — API Contracts

Все эндпоинты — JWT, владелец = `sub`. Чужой/несуществующий чат → `404`.

## GET /v1/chats
Список чатов пользователя.

### Query
- `q` (опц.) — поиск: ILIKE по `title` и по тексту первого user-сообщения.
- `cursor` (опц.) — пагинация (opaque, по `updated_at`+`id`).
- `limit` (опц., дефолт 30, max 100).

> **СПРИНТ 2 (отложено).** Фильтр `workspaceProjectId` в Спринте 1 **отсутствует** (эндпоинт его не принимает) — он появится в Спринте 2 вместе с модулем `workspaces` и колонкой `chat_sessions.workspace_project_id` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).

### Response (200)
```json
{
  "items": [
    {
      "id": "uuid",
      "title": "string | null",
      "preview": "string (срез последнего сообщения)",
      "assistantMode": "chat | code",
      "isPinned": false,
      "projectId": "string | null",
      "workspaceProjectId": "uuid | null",
      "updatedAt": "ISO8601"
    }
  ],
  "nextCursor": "string | null"
}
```
- Сортировка: `is_pinned DESC, updated_at DESC` (BR-CH-3).
- **`projectId` (свободная строка, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md), аддитивно):** `= chat_sessions.project_id` — тот же свободный строковый идентификатор website-builder-проекта, что клиент передал в `POST /v1/chat/run` при создании сессии ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)). Формат и семантика **идентичны** `projectId` из `/chat/run`. **`null` = «чистый чат»** — сессия создана без `projectId` (website-builder не активирован, `site.*` Claude не предлагались); это основной режим сервиса. Поле позволяет iOS в списке отличить проектные чаты от чистых без запроса истории.
- **`projectId` ≠ `workspaceProjectId`** — независимые, не взаимозаменяемые поля ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)): `projectId` — свободная строка website-builder (есть сейчас), `workspaceProjectId` — UUID рабочего пространства (Спринт 2). Оба присутствуют в ответе одновременно.
- Поле `workspaceProjectId` присутствует в ответе, но в **Спринте 1 всегда `null`**: колонка `chat_sessions.workspace_project_id` ещё не создана (отложена на **СПРИНТ 2**, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Сервис хардкодит `null` до появления колонки. **[ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) его не трогает** — добавляется только `projectId`.
- N+1 на `preview` (отдельный запрос на каждый чат страницы) — осознанный tech-debt [`TD-012`](../../100-known-tech-debt.md), приемлемо для текущего per-user масштаба.

## GET /v1/chats/{id}
История шагов чата.

### Response (200)
```json
{
  "id": "uuid",
  "title": "string | null",
  "assistantMode": "chat | code",
  "mode": "credits | byok",
  "steps": [
    {
      "id": "uuid",
      "messageStepId": "uuid",
      "role": "user | assistant | tool",
      "payload": { },
      "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" },
      "createdAt": "ISO8601"
    }
  ]
}
```
- `steps` — упорядочены по `chat_steps.seq` (монотонный порядок вставки, [ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)), **НЕ** по `created_at` (равен для шагов одной транзакции). `createdAt` отдаётся как информационный timestamp каждого шага.
- `payload` — payload шага. **Отдаётся в ДОМЕННОМ виде (нормализация при отдаче, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)), а НЕ в сыром виде хранилища.** Нормализация применяется на границе сериализации ответа (хранение `chat_steps.payload` и реплей в Claude не меняются). Форма зависит от `role`:
  - **`role="tool"` (результат tool-шага):** хранится в кастомной доменной форме `{toolCallId (domain UUID), providerToolUseId, toolName (dot), result|error}` — **НЕ** wire `tool_result`-блок в `content[]` (см. [chat-orchestrator/04-data-model.md](../chat-orchestrator/04-data-model.md)). При отдаче `providerToolUseId` **стрипается** (внутренний raw `toolu_...`, ADR-008 — наружу не утекает); `toolCallId` уже доменный (= `tool_calls.id`, совпадает с `/chat/run` `toolCall.id`).
  - **`role="assistant"` content-блоки (`type=text` / `type=tool_use`):**
    - **`tool_use.name`:** underscore → dot (`calendar_create_events` → `calendar.create_events`) через `to_domain_tool_name` — совпадает с `/v1/tools` `name`, `/chat/run` `toolCall.name`, `/v1/chats/{id}/steps` `toolName`.
    - **`tool_use.id`:** провайдерский `toolu_...` → доменный `tool_calls.id` (UUID) по карте `provider_tool_use_id → id` сессии (один запрос на сессию, без N+1). Совпадает с `/chat/run` `toolCall.id`. Provider `toolu_...` наружу в истории **не утекает**.
  - **Wire `tool_result`-блок в `content[]`** — альтернативный путь, который нормализация ADR-024 тоже покрывает (`_normalize_tool_result_block`: `tool_use_id` `toolu_...`→domain UUID), но оркестратор его сейчас **не пишет** (результат tool-шага идёт кастомной формой выше; путь оставлен как forward-compat-защита). На обоих путях provider `toolu_...` наружу не утекает.
  - **Текстовые блоки (`type=text`) и `tool_use.input`** — **не меняются** (байт-в-байт как в хранилище).
  - **Полнота шага (нестыковка 3, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** один assistant-шаг МОЖЕТ содержать `payload.content = [text, tool_use]` (или несколько `tool_use` при parallel tool use) вместе. История отдаёт **полный, упорядоченный** массив блоков шага — это канонический источник полного хода (в отличие от дискриминированного `ChatResponse`, который отдаёт одно состояние раунда). Клиент читает полный ход из `steps[].payload.content[]`.
- **Инвариант синка имени/id (нормативно, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** в любом `tool_use`/`tool_result`-блоке истории `name` (dot) и `id`/`tool_use_id` (domain UUID) **дословно совпадают** с `/chat/run` `toolCall.name`/`toolCall.id` того же вызова и с `/v1/tools` `name`.
- **Синк id шага/хода ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)):** `steps[].id` (= `chat_steps.id`) и `steps[].messageStepId` (= `chat_steps.message_step_id`) — те же значения, что отдаёт `ChatResponse.stepId` / `ChatResponse.messageStepId` ([chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#response-200)) для соответствующего шага/хода. Клиент склеивает оптимистично отрисованный шаг с серверной историей по `id` (точный шаг) и группирует tool-loop-раунды хода по `messageStepId`.

## GET /v1/chats/{id}/steps
Steps-view для UI («N steps»): агрегированные шаги последнего (или указанного) message-шага — tool-calls и assistant-reasoning.

### Query
- `messageStepId` (опц.) — конкретный message-шаг; по умолчанию — последний.

### Response (200)
```json
{
  "messageStepId": "uuid",
  "stepCount": 3,
  "steps": [
    {
      "kind": "reasoning | tool_call | tool_result | assistant_message",
      "toolName": "string | null",
      "summary": "string (краткое описание шага для UI)",
      "createdAt": "ISO8601"
    }
  ]
}
```
- Источник — `chat_steps` + `tool_calls` по `message_step_id`. Порядок шагов внутри message-шага — по `chat_steps.seq` ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)), НЕ `created_at`. `toolName` — доменное имя (с точкой), как в `tool_calls.tool_name`. Никаких секретов/raw provider id наружу.
- **Parallel tool use ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** assistant-ход с несколькими `tool_use`-блоками порождает несколько строк `tool_calls` (по одной на вызов) → несколько `kind=tool_call` шагов steps-view одного `messageStepId` (по `toolName` каждого). Это согласуется с `toolCalls[]` ответа `/chat/run` — каждый элемент массива соответствует своему `tool_call`-шагу.

## PATCH /v1/chats/{id}
Переименование и/или закрепление.

### Request
```json
{ "title": "string (optional)", "isPinned": true }
```
- Хотя бы одно поле. `extra='forbid'`. `title` ≤ 200 символов.

### Response (200)
```json
{ "id": "uuid", "title": "string | null", "isPinned": false, "updatedAt": "ISO8601" }
```

## DELETE /v1/chats/{id}
Удаление чата.

### Response (200)
```json
{ "deleted": true }
```
- Каскадно удаляет `chat_steps`/`tool_calls` (FK). `attachments.session_id` → NULL. Идемпотентно: повторный DELETE уже удалённого → `404`.
