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
      "workspaceProjectId": "uuid | null",
      "updatedAt": "ISO8601"
    }
  ],
  "nextCursor": "string | null"
}
```
- Сортировка: `is_pinned DESC, updated_at DESC` (BR-CH-3).
- Поле `workspaceProjectId` присутствует в ответе, но в **Спринте 1 всегда `null`**: колонка `chat_sessions.workspace_project_id` ещё не создана (отложена на **СПРИНТ 2**, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Сервис хардкодит `null` до появления колонки.
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
- `payload` — content-блоки (assistant text / tool_use / tool_result), как в `chat_steps.payload`.
- **Синк с ответом генерации ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)):** `steps[].id` (= `chat_steps.id`) и `steps[].messageStepId` (= `chat_steps.message_step_id`) — те же значения, что отдаёт `ChatResponse.stepId` / `ChatResponse.messageStepId` ([chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#response-200)) для соответствующего шага/хода. Клиент склеивает оптимистично отрисованный шаг с серверной историей по `id` (точный шаг) и группирует tool-loop-раунды хода по `messageStepId`.

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
