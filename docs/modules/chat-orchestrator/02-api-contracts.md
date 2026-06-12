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
  "assistantMessage": "string (optional, при assistant_message; ТАКЖЕ при tool_call, если Claude выдал текст вместе с tool_use — ADR-024 п.3 / Q-024-1)",
  "toolCall": { "id": "uuid", "name": "string", "args": { } },
  "toolCalls": [ { "id": "uuid", "name": "string", "args": { } } ],
  "serverTools": [ { "toolName": "string (dot)", "status": "completed | errored", "summary": "string | null" } ],
  "blockReason": "enum (optional, при blocked)",
  "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" }
}
```
- **`toolCalls[]` (множественный, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) присутствует только при `status=tool_call`** — **ВСЕ** client-side tool-вызовы текущего assistant-хода (parallel tool use), в порядке блоков ответа Claude. Каждый элемент `{ id (доменный UUID = tool_calls.id), name (dot), args }`. **Server-side `site.*` в `toolCalls[]` НЕ попадают** (исполняются на бэке в tool-loop, [ADR-011](../../adr/ADR-011-server-side-tools.md)) — массив несёт только client-side (`files.*`/`calendar.*`/`reminders.*`).
- **`toolCall` (одиночный) — deprecated, обратная совместимость ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)).** Присутствует при `status=tool_call` и **равен `toolCalls[0]`** (первый client-side вызов хода). Корректный клиент обязан читать `toolCalls[]` (на мульти-tool ходе одиночный `toolCall` неполон → continuation сломается). Удаление одиночного поля — отдельным ADR после миграции iOS.
- `toolCall.id` / `toolCalls[].id` — **доменный UUID** (`= tool_calls.id`), стабильный публичный идентификатор для iOS и для последующего `/chat/tool-result`. Внутренний Anthropic `tool_use.id` (`toolu_...`) наружу **не** отдаётся (хранится в `tool_calls.provider_tool_use_id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).
- **`serverTools[]` — выполненные server-side инструменты за этот вызов ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md), аддитивно):** список server-side инструментов (`site.*` project-scoped [ADR-011](../../adr/ADR-011-server-side-tools.md), `time.now` global [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)), которые backend исполнил в tool-loop **этого** вызова (`/chat/run` или один `/chat/tool-result`-continuation), в порядке выполнения. **Дополняет** `toolCalls[]` (там — только client-side, исполняемые iOS): server-side в `toolCalls[]` по-прежнему **НЕ** входят. Каждый элемент: `{ toolName, status, summary? }`:
  - `toolName` — доменное имя с точкой (`time.now`, `site.write_file`, …), совпадает с `/v1/tools` `name` и `GET /v1/chats/{id}/steps` `toolName`.
  - `status` — `"completed"` | `"errored"` (итог выполнения; `errored` — инструмент вернул tool-result error, ход при этом **не падает**). Совпадает со статусом `tool_calls`, выставляемым в `_execute_server_side_tool`/`_execute_global_server_side_tool`.
  - `summary` (опц., `string | null`) — **компактный** человекочитаемый итог, лимит длины `_SUMMARY_MAX_CHARS` (120, как в steps-view). **НЕ raw result.** Для `completed` — дефолт `"ok"` или короткий доменный итог (например имя файла) **без путей/URL/signed-token**; для `errored` — короткий код ошибки (например `invalid_timezone`). **Полный** результат server-side инструмента доступен только в истории `GET /v1/chats/{id}` → `steps[].payload` tool-шага ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) и steps-view — `serverTools[]` это **индикатор**, не канал доставки результата.
  - **Семантика «за один вызов» (не за сессию):** перечисляет server-side, выполненные в этом обращении. Дубликаты с историей `/chats` — ожидаемы (удобство флоу, не замена истории).
  - **Присутствие по статусам:** при `status=assistant_message` и `status=tool_call` — **присутствует** (может быть пустым `[]`, если server-side не выполнялись; при `tool_call` перечисляет server-side, отработавшие **до** того, как ход уперся в client-side вызов). При `status=blocked`+**policy** (`blockReason ≠ max_tokens`) — **пустой `[]`** (policy-block до генерации, tool-loop не запускался). При `status=blocked`+**`max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — **может быть НЕ пустым** (server-side раунды могли отработать до обрыва финального витка). Поле присутствует всегда (хотя бы как `[]`) при `assistant_message`/`tool_call`/`blocked`.
  - **Idempotent replay → `serverTools=[]` (by-design, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)):** повторный `/chat/tool-result` для **уже закрытого** хода возвращает сохранённый финальный шаг (`_render_saved_step`, continuation выполняется один раз на закрытие барьера — [ADR-005](../../adr/ADR-005-idempotency-ledger.md)/[ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при таком реплее `serverTools=[]` — server-side выполнения **НЕ** реконструируются (реплей отдаёт финальный результат, не воспроизводит tool-loop). Полный набор server-side выполнений хода доступен в истории `GET /v1/chats/{id}`.
  - **Биллинг неизменен ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)):** server-side раунды не списывают кредиты; `serverTools[]` информационно, на amount не влияет. Аддитивно/обратносовместимо: старые клиенты игнорируют. Каталог инструментов и их число (14) не меняются.
  - **Связь со steps-view:** идея `summary` переиспользована из `StepsViewStepSchema`, но это **отдельное** поле — только server-side выполнения, `status` (`completed`/`errored`) вместо `kind`. steps-view (`GET /v1/chats/{id}/steps`) — отдельный диагностический срез истории; `serverTools[]` — inline-индикатор в самом ответе генерации.
- **Контракт Anthropic tool-loop ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** на КАЖДЫЙ `tool_use` ассистент-хода в следующем витке обязан быть `tool_result`. Поэтому клиент обязан исполнить и вернуть результаты на **все** `toolCalls[]` (см. `/chat/tool-result` батч) — иначе continuation не соберётся (Anthropic `400` → `502`). Одиночный `toolCall` достаточен только когда `len(toolCalls)==1`.
- `blockReason` присутствует только при `status=blocked`.
- `usage` присутствует при `assistant_message`/`tool_call`, **а также при `blocked` с `blockReason=max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при policy-blocked (генерация не выполнялась) — отсутствует.
- **`status=blocked` + `blockReason=max_tokens` (обрезка по лимиту output-токенов, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** Claude обрезан на `ANTHROPIC_MAX_TOKENS` (`stop_reason="max_tokens"`); обрезанные `tool_use` **неполны** и наружу **НЕ** отдаются (`toolCall`/`toolCalls` отсутствуют). В отличие от policy-blocked: `messageStepId`/`stepId` **НЕ null** (ход и обрезанный assistant-шаг созданы), `usage` присутствует, `assistantMessage` — частичный текст хода (если был). **Кредит НЕ списывается** (обрыв — не успешный финальный `assistant_message`, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Клиенту рекомендуется повторить/сократить запрос. С дефолтом `ANTHROPIC_MAX_TOKENS=16000` кейс редкий (safety-net).
- **`assistantMessage` ([Q-024-1](../../99-open-questions.md) Closed = вариант A, [ADR-024 §Decision п.3](../../adr/ADR-024-history-payload-domain-normalization.md)):**
  - `status=assistant_message` — финальный текст Claude (как и раньше, без изменений).
  - `status=tool_call` — **опционально присутствует**: текст из `text`-блоков **того же** assistant-шага, чей `tool_use` вернулся как `toolCall` (тот шаг, на который указывает `stepId`). Значение = текст/конкатенация `text`-блоков этого шага. Если Claude вернул `tool_use` **без** сопутствующего текста — `assistantMessage = null`/опущено. `toolCall` при этом **обязателен** (семантика не меняется); добавление `assistantMessage` аддитивно/обратносовместимо (поле уже опционально-nullable в схеме; новизна — оно теперь может быть НЕ-null при `tool_call`). Backend перестаёт отбрасывать сопутствующий текст (`orchestrator.py:661`) и кладёт его в `assistantMessage`.
  - `status=blocked` — `assistantMessage = null` (генерация не выполнялась).
  - **Согласование с историей и [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md):** `assistantMessage` при `tool_call` = тот же текст, что отдают `text`-блоки `GET /v1/chats/{id}` → `steps[].payload.content[]` шага `stepId` (нормализация истории текстовые блоки не меняет — байт-в-байт хранилище). Инвариант: `ChatResponse.stepId` указывает на этот же assistant-шаг, поэтому run-проекция и история несут один и тот же сопутствующий текст.
- **`messageStepId` / `stepId` — идентификаторы синхронизации с историей чата ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), nullable).** Позволяют клиенту склеить ответ генерации с шагами `GET /v1/chats/{id}` → `steps[]`. Обе величины уже существуют в orchestrator: `messageStepId` = `chat_steps.message_step_id` (ключ хода, см. §below про генерацию), `stepId` = `chat_steps.id` (PK конкретного шага). Семантика по статусам:
  - `status=assistant_message`: `messageStepId` = ход; `stepId` = `id` финального assistant-шага (= `ChatStepSchema.id` этого шага в истории). **Оба присутствуют.**
  - `status=tool_call`: `messageStepId` = ход; `stepId` = `id` assistant-шага, содержащего `tool_use` (тот шаг истории, чей `payload` несёт этот `tool_use`-блок). `toolCall.id` **остаётся как есть** (provider-независимый доменный id tool-вызова для `/chat/tool-result`) — `toolCall.id` ≠ `stepId`. **Оба присутствуют.**
  - `status=blocked` (**policy-blocked**, `blockReason ≠ max_tokens`): `messageStepId = null`, `stepId = null` — блокировка срабатывает в Policy Engine **до** генерации ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md), [ADR-004](../../adr/ADR-004-blocked-http-200.md)), `chat_steps`/ход **не создаются**, ссылаться не на что (согласовано с отсутствием `usage` при policy-blocked).
  - `status=blocked` + **`blockReason=max_tokens`** (обрезка, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)): `messageStepId` = ход, `stepId` = `id` обрезанного assistant-шага — **оба НЕ null** (Claude сгенерировал контент, ход/шаг созданы). `usage` присутствует. Отличие от policy-blocked: здесь блокировка — обрыв **после** начала генерации, а не deny до неё.
- **Инвариант синка id шага/хода (нормативно):** `ChatResponse.messageStepId` / `ChatResponse.stepId` дословно совпадают с `ChatStepSchema.messageStepId` / `ChatStepSchema.id` соответствующего шага в [chats/02-api-contracts.md `GET /v1/chats/{id}` → `steps[]`](../chats/02-api-contracts.md#get-v1chatsid). Аддитивно/обратносовместимо: существующие поля, security, коды, пути не меняются ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).
- **Инвариант синка имени/id инструмента (нормативно, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** `toolCall.name` (dot) и `toolCall.id` (domain UUID = `tool_calls.id`) этого ответа **дословно совпадают** с `tool_use.name`/`tool_use.id` соответствующего блока в `GET /v1/chats/{id}` → `steps[].payload.content[]` (история нормализует свой сырой wire-payload к доменному виду при отдаче — см. [chats/02-api-contracts.md](../chats/02-api-contracts.md#get-v1chatsid)) и с `name` в `/v1/tools`. Сопутствующий текст при `status=tool_call` (`text`-блок того же шага) в истории доступен полностью и **также** пробрасывается в `ChatResponse.assistantMessage` ([Q-024-1](../../99-open-questions.md) Closed = вариант A): тот же текст того же шага (`stepId`) — см. описание `assistantMessage` выше.

### Правила
- Перед генерацией — обязательный вызов Policy Engine (ADR-002).
- `status=blocked` → HTTP 200, машиночитаемый `blockReason` (ADR-004).
- Для `status=tool_call` payload строго типизирован по схемам ниже.
- Тех. ошибки (auth/size/validation/upstream) — 4xx/5xx (см. api-gateway).

## POST /v1/chat/tool-result
Приём результата(ов) локальных tools и продолжение шага. **Батч-форма ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))** — для parallel tool use возвращаются результаты на все `toolCalls[]` хода.

### Request (батч — рекомендуемая форма, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "results": [
    { "toolCallId": "uuid", "result": { "any": "object" } },
    { "toolCallId": "uuid", "error": { "code": "string", "message": "string" } }
  ]
}
```
- `results[]` — результаты на один или несколько tool-вызовов **одного хода**. В каждом элементе ровно одно из `result` / `error` (валидатор `extra=forbid` поэлементно).
- Каждый `result` ≤ 256KB (поэлементно).

### Request (одиночная форма — deprecated, обратная совместимость)
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "toolCallId": "uuid",
  "result": { "any": "object" },
  "error": { "code": "string", "message": "string" }
}
```
- Эквивалентна `results = [{ toolCallId, result|error }]` (батч из одного). Backend принимает обе формы; одиночная — **deprecated** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), удаление — отдельным ADR после миграции iOS.
- Ровно одно из `result` / `error`.
- `result` ≤ 256KB.

### Барьер хода и continuation ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))
- Continuation-виток к Anthropic выполняется **ТОЛЬКО** когда для **всех** client-side `tool_use` текущего assistant-хода собраны `tool_result` (completed/errored). Иначе orphan `tool_use` → Anthropic `400` → `502`.
- **Рекомендуемый путь** — один батч-запрос со всеми результатами хода → барьер закрывается сразу, backend делает continuation и возвращает следующий шаг.
- **Накопительный путь (поддерживается):** результаты можно слать частями (несколько `/chat/tool-result` одного хода). Пока барьер не закрыт — ответ `status=tool_call` с `toolCalls[]` = **оставшиеся** (ещё без результата) client-side вызовы хода (`toolCall` = первый из оставшихся); Anthropic не вызывается; биллинг не выполняется. Когда последний результат закрывает барьер — continuation-виток, следующий шаг.
- Server-side `site.*` результаты в `/chat/tool-result` **не присылаются** — backend их сформировал сам ([ADR-011](../../adr/ADR-011-server-side-tools.md)); барьер хода учитывает только client-side tool-вызовы.

### Response (200)
Та же схема, что у `/v1/chat/run` (включая `messageStepId` / `stepId`, [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), `toolCalls[]`, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), и `serverTools[]`, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) — server-side, выполненные в **этом** continuation-витке).
- `messageStepId` **стабилен в рамках хода**: равен тому, что был выдан в исходном `/chat/run` этого хода (берётся из `tool_calls.message_step_id` по `toolCallId`, см. re-entry ниже) — это и есть смысл синка tool-loop: клиент держит один `messageStepId` на весь ход.
- `stepId` = `id` **нового** шага, который представляет этот ответ: assistant-tool_use следующего раунда (при `status=tool_call`) либо финальный assistant-шаг (при `status=assistant_message`). Ответ всегда указывает на **следующий шаг, порождённый Claude**, а не на только что принятый шаг-`tool_result`.
- `status=blocked` (если возникает на продолжении): `messageStepId`/`stepId` = `null` — как в `/chat/run`.

### Правила
- Проверка принадлежности каждого `toolCallId` текущей сессии: `tool_calls.session_id == sessionId`, иначе `404`/`403` (применяется к каждому элементу `results[]`).
- Re-entry message-шага: `messageStepId` берётся из `tool_calls.message_step_id` найденного `toolCallId` (НЕ генерируется заново). Все элементы батча должны относиться к одному ходу (один `message_step_id`). Все ответы и финальный debit этого шага используют тот же `messageStepId`.
- **Идемпотентность / повторы ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):**
  - повторный `toolCallId` со статусом `completed`/`errored` → результат не перезаписывается, Anthropic повторно не вызывается; если барьер уже закрыт и continuation-шаг сохранён — вернуть его (как сейчас);
  - дубль `toolCallId` внутри одного батча → `422`;
  - continuation-виток к Anthropic выполняется **один раз** на закрытие барьера хода (дополнительно защищён `messageStepId`-идемпотентностью дебита, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- `result` валидируется по схеме соответствующего tool (см. ниже); несоответствие → `422`.

## Классы tools: client-side vs server-side ([ADR-011](../../adr/ADR-011-server-side-tools.md), [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Три класса инструментов ([ADR-026 §1](../../adr/ADR-026-global-server-side-tools-and-time-now.md)):
- **client-side** (`files.*`, `calendar.*`, `reminders.*`) — исполняет **iOS-клиент**: backend отдаёт `status=tool_call`,
  ждёт `tool_result` через `/v1/chat/tool-result`. Описаны в этом документе.
- **server-side, project-scoped** (`site.*`, website-builder, `SERVER_SIDE_TOOLS`) — исполняет **backend** немедленно в tool-loop, формирует `tool_result` сам
  и продолжает к Anthropic **без** round-trip к iOS; **НЕ** отдаётся клиенту как `status=tool_call`. **Требует проекта.** Схемы и поведение —
  [modules/website-builder/02-api-contracts.md](../website-builder/02-api-contracts.md), [ADR-011](../../adr/ADR-011-server-side-tools.md).
- **server-side, global** (`time.now`, `GLOBAL_SERVER_SIDE_TOOLS`, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)) — исполняет **backend** немедленно в tool-loop (как `site.*`), но **НЕ требует проекта** и предлагается Claude **всегда** (включая «чистый чат» без проекта). В `toolCalls[]` наружу **НЕ** попадает. Контракт — [§`time.now`](#timenow--server-side-global-tool-adr-026) ниже.
- Orchestrator различает класс по доменному имени (статические реестры `SERVER_SIDE_TOOLS = {site.*}`, `GLOBAL_SERVER_SIDE_TOOLS = {time.now}`, непересекающиеся). domain↔anthropic
  mapping (точка→подчёркивание) расширяется server-side именами (`site.write_file ↔ site_write_file`, `time.now ↔ time_now`, …). Guard на число
  server-side раундов — `MAX_SERVER_TOOL_ROUNDS` (дефолт 16) — общий для project-scoped и global server-side раундов.
- **Гейтинг по наличию проекта ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)):** `site.*` (`SERVER_SIDE_TOOLS`) предлагаются Claude **только** когда у сессии есть `project_id` (создана с `projectId`). В «чистом чате» (`chat_sessions.project_id IS NULL`) `site.*` в tool-набор **не включаются** — Claude их не видит и не вызывает. **`time.now` (`GLOBAL_SERVER_SIDE_TOOLS`) под этот гейт НЕ подпадает** — предлагается всегда ([ADR-026 §3](../../adr/ADR-026-global-server-side-tools-and-time-now.md)). См. [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022).

## `time.now` — server-side global tool ([ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Инструмент текущей даты/времени. Исполняет **backend** в tool-loop (без round-trip к iOS, как `site.*`), но **БЕЗ проекта** — доступен в любом ходе, включая основной flow чат-агрегатора ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)). Решает репорт «модель отвечает 2024 год»: системный промт статичен и не несёт даты, модель получает время только из результата `time.now`. Не мутирующий (нет `tool_mutation` audit). В `toolCalls[]` наружу не отдаётся (исполнен на бэке).

### Args (`TimeNowArgs`, Pydantic v2, `extra="forbid"`)
```json
{ "tz": "Europe/Moscow" }
```
- `tz` (опц., `str | None`, default `null`) — IANA-имя зоны (напр. `Europe/Moscow`, `America/New_York`). Лимит длины `≤ 64` символа ([Q-026-1](../../99-open-questions.md)). При отсутствии → результат только в UTC.
- `extra="forbid"`: любой иной ключ → ошибка валидации args.

### Result
```json
{
  "utc": "2026-06-10T14:23:05.123456+00:00",
  "unix": 1781446985,
  "weekday": "Wednesday",
  "timezone": "Europe/Moscow",
  "local": "2026-06-10T17:23:05.123456+03:00"
}
```
- `utc` — **всегда**: текущее UTC, ISO8601 (RFC3339) с offset `+00:00`.
- `unix` — **всегда**: целочисленный Unix timestamp (секунды, UTC).
- `weekday` — **всегда**: английское имя дня недели по UTC-дате (`Monday`..`Sunday`).
- `timezone` — **только** при заданном валидном `tz`: нормализованное IANA-имя.
- `local` — **только** при заданном валидном `tz`: ISO8601 с локальным offset.
- Без `tz` → `timezone`/`local` **опущены** (только UTC-набор).

### Ошибки и инварианты
- **Невалидный/неизвестный `tz`** (не парсится `zoneinfo` / `ZoneInfoNotFoundError` / длина > 64) → **tool-result error** `{"error":{"code":"invalid_timezone","message":"..."}}` (через `ToolExecution.error`), **НЕ** падение хода (не `422`, не `502`). Claude получает машиночитаемую ошибку и может повторить без `tz`/с корректной зоной; ход продолжается.
- **UTC-набор от tz-базы не зависит** (вычисляется от `datetime.UTC`) и доступен всегда. Локальное время по `tz` требует tz-базы в образе — обеспечена pure-Python зависимостью `tzdata` ([TD-019](../../100-known-tech-debt.md) **Resolved 2026-06-10**, вариант A); `tz` в prod работает. Невалидная/мусорная зона по-прежнему деградирует к tool-result error `invalid_timezone` (резолв ловит `ZoneInfoNotFoundError`/`ValueError`/`OSError`).
- **Биллинг:** раунд `time.now` не добавляет списаний — 1 кредит = 1 сообщение ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); списание один раз на финальном `assistant_message`.
- **Clock-провайдер:** время берётся через инъектируемый `Clock` (детерминизм qa, [ADR-026 §8](../../adr/ADR-026-global-server-side-tools-and-time-now.md), [06-testing-strategy.md](../../06-testing-strategy.md)), не прямой `datetime.now()`.

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
| `calendar.read` | read | `{ "start": "ISO8601 datetime", "end": "ISO8601 datetime", "calendarId": string? }` ([ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)) | `{ "events": [ { "id": string, "title": string, "start": "ISO8601 datetime", "end": "ISO8601 datetime", "location": string?, "notes": string? } ] }` |
| `calendar.create_events` | mutate | `{ "events": [ { "title": string, "start": "ISO8601 datetime", "end": "ISO8601 datetime", "location": string?, "notes": string?, "calendarId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |
| `reminders.read` | read | `{ "listId": string?, "includeCompleted": bool }` | `{ "reminders": [ { "id": string, "title": string, "due": "ISO8601"?, "completed": bool, "notes": string? } ] }` |
| `reminders.create` | mutate | `{ "reminders": [ { "title": string, "due": "ISO8601"?, "notes": string?, "listId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |

### Общие правила схем
- Все схемы — Pydantic v2, `extra='forbid'`.
- Даты — ISO8601 (RFC3339), UTC или с offset. **Исключение:** календарные `start`/`end` (`calendar.read`, `calendar.create_events`) — ISO8601-datetime в локальном времени **без** offset (naive local), см. раздел «Контракт календарных инструментов: `start`/`end`» ниже и [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md).
- `path` валидируется как относительный/безопасный (без `..`-traversal) на стороне валидатора backend; фактический доступ — ответственность клиента.
- `error` (в tool-result) имеет форму `{ "code": string, "message": string }`; при `error` backend передаёт Claude tool_result с `is_error=true`.

### Контракт календарных инструментов: `start`/`end` (нормативно, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md))
**Единый контракт диапазона для `calendar.read` и `calendar.create_events`** (полная консистентность, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)):

- **Имена аргументов диапазона — идентичны:** `start` / `end` в обоих инструментах. `calendar.read` использует `start`/`end` (ранее `startDate`/`endDate` — **переименовано**, breaking change); `calendar.create_events` — `events[].start` / `events[].end` (без изменений имён).
- **Формат значения — идентичен:** ISO8601 **datetime** в **локальном времени без timezone-offset**, секундная точность — например `"2026-06-11T09:00:00"`. **Date-only (`"2026-06-11"`) больше не является целевым контрактом** для `calendar.read` (backward-compat date-only не поддерживается, [ADR-027 §Decision 2](../../adr/ADR-027-calendar-read-contract-alignment.md)). Naive local — это сложившийся де-факто формат `create_events`; read выровнен под него. Tz-aware — возможное будущее усиление обоих ([Q-027-1](../../99-open-questions.md)).
- **Семантика диапазона — end-exclusive:** интервал `[start, end)` — `start` включительно, `end` исключительно. «Весь день D» = `start="D T00:00:00"`, `end="D+1 T00:00:00"` (полночь следующего дня), а **не** `end="D T23:59:59"` ([ADR-027 §Decision 5](../../adr/ADR-027-calendar-read-contract-alignment.md)). Это даёт достижимость диапазона по времени внутри дня (например 09:00–18:00) и однозначность смежных дней.
- **Валидация формата — НЕ серверная:** `start`/`end` — простой `str` в Pydantic-схеме (без datetime-валидации), **симметрично для read и create** ([ADR-027 §Decision 3](../../adr/ADR-027-calendar-read-contract-alignment.md)). Формат доводится до модели через `TOOL_DESCRIPTIONS` (см. ниже), фактический парсинг datetime — на стороне iOS (EventKit), как и подобает client-side tool ([ADR-011](../../adr/ADR-011-server-side-tools.md)).
- **Описание для модели (`TOOL_DESCRIPTIONS`) — самодостаточно по формату.** Описания `calendar.read` и `calendar.create_events` обязаны явно указывать ISO8601-datetime-формат `start`/`end` (local, no offset, пример `"2026-06-11T09:00:00"`) и end-exclusive-конвенцию «весь день», чтобы модель генерировала datetime, а не date-only. **Корень устранённого бага:** ранее формат жил только в docs и не доходил до модели — модель генерировала date-only ([ADR-027 §Context](../../adr/ADR-027-calendar-read-contract-alignment.md)).
- **Breaking change iOS-контракта `calendar.read`** ([ADR-027 §Consequences](../../adr/ADR-027-calendar-read-contract-alignment.md)): iOS-клиент обязан читать args `start`/`end` (не `startDate`/`endDate`) и трактовать значения как datetime. Требуется скоординированный релиз iOS. Каталог `/v1/tools` остаётся 14 инструментов — меняется только `inputSchema` записи `calendar.read` (генерируется из `_ARGS_BY_TOOL`). BUG-3 name-map (имена инструментов) не затрагивается — меняются имена **аргументов**, не имя tool.
- **Исторические сессии:** старые `chat_steps`/`tool_calls` хранят прежние `startDate`/`endDate`-вызовы как есть; нормализация истории ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) не переписывает `tool_use.input`. Миграция не требуется ([Q-027-2](../../99-open-questions.md)).

### blockReason enum (повтор для удобства)
`trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | rate_limited | policy_denied | max_tokens` (источник — [ADR-004](../../adr/ADR-004-blocked-http-200.md); `max_tokens` добавлен [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) — обрезка ответа по лимиту output-токенов, в отличие от прочих policy-причин срабатывает **после** начала генерации: `usage`/`messageStepId`/`stepId` присутствуют, кредит не списывается).

---

## GET /v1/tools — каталог инструментов ([ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md))
Машиночитаемый каталог всех поддерживаемых backend tools (**14**, включая `time.now`, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)). Источник — `src/app/chat/tools.py` (single source of truth: `_ARGS_BY_TOOL`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `GLOBAL_SERVER_SIDE_TOOLS`, `anthropic_tool_definitions()`). Эндпоинт **не** параметризуется ни `assistantMode`, ни наличием проекта — возвращает полный технический реестр backend (включая `site.*` и `time.now`). Runtime-фильтрация tool-набора, предлагаемого Claude (гейтинг `site.*` по наличию `project_id`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md); фильтрация по режиму, [Q-012-1](../../99-open-questions.md)), — concern tool-loop'а, а не каталога. `time.now` предлагается всегда и в каталоге всегда присутствует.

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
- `execution` — `"server"` если `name ∈ SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS` (`site.*` — [ADR-011](../../adr/ADR-011-server-side-tools.md); `time.now` — [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md); исполняет backend); иначе `"client"` (исполняет iOS).
- `inputSchema` — JSON Schema args (`_ARGS_BY_TOOL[name].model_json_schema()`).
- Порядок — детерминированный (по `_ARGS_BY_TOOL`).

### Полный список (14)
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
| time.now | **server** (global, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)) | нет |

> `time.now` — единственный **global** server-side tool ([ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)): `execution=server`, но в отличие от `site.*` **не требует проекта** и предлагается Claude всегда. domain↔anthropic: `time.now ↔ time_now`.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.
