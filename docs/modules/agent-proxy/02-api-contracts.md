# Agent Proxy — API Contracts

Все эндпоинты под `/v1/agent/*`. Авторизация — клиентский контур ([ADR-044](../../adr/ADR-044-client-api-key-auth.md)): заголовки `X-API-Key` (клиентский ключ) + `X-User-Id` (UUID субъекта). Swagger security schemes — `clientApiKey` + `userId` ([08-api-documentation.md §R2.1](../../08-api-documentation.md)). Бизнес-блокировки — `200 {status:blocked}` ([ADR-004](../../adr/ADR-004-blocked-http-200.md)); 4xx/5xx — технические.

## POST /v1/agent/run
Запуск автономного прогона агента.

### Headers
- `X-API-Key: <CLIENT_API_KEY>` (обязателен).
- `X-User-Id: <uuid>` (обязателен).

### Request
```json
{
  "message": "string",
  "sessionId": "string|null",
  "model": "string|null"
}
```
- `message` — обязателен (текст хода пользователя). Маппится в Hermes `input`.
- `sessionId` — опц.; преемственность диалога внутри инстанса. Маппится в Hermes `session_id`.
- `model` — опц.; модель Hermes внутри инстанса. Маппится в Hermes `model`.

### Response
- **202** (allowed): `{"runId": "string", "status": "queued|running"}` (proxy Hermes `run_id`→`runId`, `status`).
- **200** (blocked, [ADR-004](../../adr/ADR-004-blocked-http-200.md)): `{"status": "blocked", "blockReason": "credits_empty|subscription_expired|trial_used|debt_outstanding"}`.
- **401** — нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`.
- **502** — инстанс недоступен / `ensure_running` не поднял контейнер / Hermes 5xx. Транзиентная connect-ошибка запуска (`POST /v1/runs`) ретраится ([ADR-062](../../adr/ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md), connect-only) перед `502`; wake после гибернации теперь ждёт готовности `api_server` (readiness-gate wake, [ADR-062](../../adr/ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)) → устранён `502` «каждый 3–4-й запрос».

#### Достижимый набор `blockReason` (credits-ветка)
Источник истины по полному перечню `blockReason` — Policy Engine ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md)). Агентный путь вызывает `evaluate(state, mode=credits)` **только** в `credits`-ветке ([ADR-047 §3](../../adr/ADR-047-usage-based-billing-for-agent.md)), поэтому фактически достижим строго следующий набор:

| `blockReason` | Состояние (credits-ветка [ADR-002](../../adr/ADR-002-access-policy-state-machine.md)) |
|---|---|
| `credits_empty` | подписка `active`, `credits_balance == 0` (BR-3) |
| `subscription_expired` | подписка `expired` (BR-5) |
| `trial_used` | без подписки (`none`), trial уже израсходован (BR-1) |
| `debt_outstanding` | `wallets.debt > 0` (непогашенный долг агентного прогона, [ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md)) — проверяется в policy-gate **до** прогона; гасится пополнением (clawback) |

- `debt_outstanding` ([ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md)) — достижим **только** на агентном пути (`/v1/chat/*` долг не накапливает); под флагом `AGENT_DEBT_RECONCILE_ENABLED` (дефолт `true`). При выключенном флаге недостижим.
- `trial_used` — **достижим**: пользователь без подписки с израсходованным trial получает именно его (ветка `mode=credits`, `subscription=none`, `trial_used=true` в [ADR-002](../../adr/ADR-002-access-policy-state-machine.md)).
- `subscription_required` — **недостижим** на этом пути: его возвращает только `byok`-ветка (`mode=byok` + `subscription=none`). Агентный путь byok-режим не использует ([ADR-047 §3](../../adr/ADR-047-usage-based-billing-for-agent.md), [Q-047-3](../../99-open-questions.md)), поэтому в контракте `/v1/agent/run` он не фигурирует.
- Прочие enum-значения (`byok_disabled`, `byok_invalid`, `rate_limited`, `policy_denied`, `max_tokens`) на этом пути не возникают: byok-причины — другая ветка; `rate_limited` — gateway-concern; `max_tokens` — orchestration-исход `/chat`-пути ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), не применим к агентному прогону. Полная расшифровка значений — [08-api-documentation.md §Расшифровка blockReason](../../08-api-documentation.md).

> **needs_code_sync (backend, оба места).** Код расходится с этим набором — backend синхронизирует **оба** артефакта в `{credits_empty, subscription_expired, trial_used, debt_outstanding}`:
> 1. `src/app/schemas/agent.py` — `AgentRunResponse.blockReason` (тип/enum поля ответа). **ОБЯЗАН включать `debt_outstanding` всегда** (enum НЕ гейтится флагом): дефолт `AGENT_DEBT_RECONCILE_ENABLED=true`, причина достижима при дефолтной конфигурации ([ADR-051 §4](../../adr/ADR-051-agent-debt-reconciliation.md)).
> 2. `src/app/agent_proxy/service.py:46` — frozenset `_AGENT_BLOCK_REASONS`: сейчас содержит недостижимый `subscription_required` и **не** содержит достижимые `trial_used`/`debt_outstanding`, поэтому defensive-ветка ложно логирует их как unexpected. Должен стать `{credits_empty, subscription_expired, trial_used, debt_outstanding}`.
>
> **Разведение «знать значение» (enum/achievable-set) vs «эмитировать» (фактический возврат):** enum `AgentRunResponse.blockReason` и frozenset `_AGENT_BLOCK_REASONS` включают `debt_outstanding` **безусловно** (дефолт флага `true`) — иначе при включённой реконсиляции backend получит ложный «unexpected reason» лог и нарушит [ADR-051 §4](../../adr/ADR-051-agent-debt-reconciliation.md). **Эмиссия** `debt_outstanding` (фактический возврат `blocked/debt_outstanding`) гейтится `AGENT_DEBT_RECONCILE_ENABLED`: при `false` policy-gate не проверяет `wallets.debt` → причина не эмитируется, но **остаётся валидным членом** enum/achievable-set (не «unexpected»). То есть флаг управляет генерацией причины, а НЕ составом enum.

### Правила
- Поток: auth → policy-gate (`PolicyEngine.evaluate`, BR-2/3/5) → `HermesInstanceManager.ensure_running(userId)` → прокси `POST {base}/v1/runs` c `Authorization: Bearer <api_key>` ([ADR-045 §2](../../adr/ADR-045-hermes-as-agent-proxy.md)).
- Policy blocked → прогон **не** запускается (контейнер не будится напрасно), `200 blocked`.
- `mode=byok` агентного пути на старте не вводится ([Q-047-3](../../99-open-questions.md)); policy работает в `credits`-ветке.

## GET /v1/agent/runs/{runId}/events  (SSE)
Ретрансляция событий прогона.

### Headers
- `X-API-Key`, `X-User-Id` (обязательны).

### Поведение
- Открывает SSE к `GET {base}/v1/runs/{runId}/events`, ретранслирует события клиенту: `run.queued|run.running|message.delta|tool.started|tool.completed|approval.request|run.failed`.
- На **`run.completed`** извлекает `usage:{input_tokens,output_tokens,total_tokens}` → `WalletService.consume(user_id, amount, idempotency_key=runId, meta={usage,model,source:"agent_run"})` ([ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)). `amount = ceil(in/1000*CREDITS_PER_1K_INPUT + out/1000*CREDITS_PER_1K_OUTPUT)`, мин. 1 при ненулевом usage.
- **Недобор баланса ([ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md), `AGENT_DEBT_RECONCILE_ENABLED`):** при `amount > balance` `consume` списывает доступный `balance` (частичный ledger-debit) и недобор `delta=amount-balance` кладёт в `wallets.debt`; audit `billing_debit_insufficient` (+ `partialDebited`/`debtAdded`). SSE не рвётся. Следующий прогон блокируется policy (`debt_outstanding`) до погашения долга clawback'ом при пополнении.
- **`run.failed`** → проброс клиенту, **debit не выполняется**.
- Идемпотентность по `runId` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)): повторная подписка/ретрай → один debit.
- **Снапшот-побочный эффект ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md)):** relay попутно пишет состояние прогона в `agent_run_snapshots` (текст, последний инструмент, pending approval, токены) и статусы в `agent_runs` (`failed`/`completed`/`paused`) — источник для `GET /v1/agent/runs/{runId}/state`. Накопление ведётся **независимо от флага** `agent_incremental_billing_enabled`. Правила записи и троттлинг — [05-events.md §Снапшот-побочные эффекты](05-events.md).
- Полный перечень событий — [05-events.md](05-events.md).

## GET /v1/agent/runs/{runId}/state  ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md))
Снапшот состояния прогона **из БД**. Назначение — восстановление UI после kill приложения / смены сети / гибернации Hermes: `/events` не персистит события ([ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md)), поэтому переподключение к закрытому стриму отдаёт `200` без событий.

### Headers
- `X-API-Key`, `X-User-Id` (обязательны).

### Request
Тела нет. Единственный параметр — `runId` в пути.

### Response 200
```json
{
  "runId": "run_7f3c1a",
  "sessionId": "b2b4b0e6-8f2a-4b5d-9d31-6a1c2f0e9a77",
  "status": "waiting_approval",
  "resultText": "Нашёл три подходящих варианта, свожу в таблицу…",
  "lastTool": "web_search",
  "pendingApproval": { "tool": "file_write", "preview": "report.md (12 KB)" },
  "blockReason": null,
  "usage": { "inputTokens": 18432, "outputTokens": 2110 },
  "updatedAt": "2026-07-28T11:42:07Z",
  "continuedFrom": null
}
```

| Поле | Тип | Семантика |
|---|---|---|
| `runId` | string | Идентификатор прогона (= путь запроса). |
| `sessionId` | string | Hermes-сессия прогона (`agent_runs.session_id`); стабильна на всю continuation-цепочку. Информационно — для `resume` клиенту **не нужна**. |
| `status` | `queued` \| `running` \| `waiting_approval` \| `paused` \| `completed` \| `failed` \| `stopped` | Клиентский статус, производный от `agent_runs.status` + наличия `pending_approval` (таблица маппинга ниже). `queued` в v1 **не эмитится** (только forward-compat). |
| `resultText` | string | Накопленный текст ответа агента (склейка `message.delta`). `""` если ничего не накоплено. Обрезан **с сохранением начала** до `AGENT_STATE_RESULT_TEXT_MAX_CHARS` (64 KB). Может отставать — см. «Известные ограничения». |
| `lastTool` | string \| null | Имя последнего инструмента из `tool.started`/`tool.completed`. |
| `pendingApproval` | object \| null | `{tool, preview}` — прогон ждёт ответа на `approval.request`. `preview` обрезан тем же `AGENT_STATE_RESULT_TEXT_MAX_CHARS` (64 KB, head-preserving), что и `resultText` — отдельного лимита под превью нет ([ADR-066 §6](../../adr/ADR-066-agent-run-state-snapshot.md)). `null` — не ждёт. Снимается ответом на `POST …/approval`, следующим `tool.*` или терминальным событием. |
| `blockReason` | string \| null | **Причина паузы** = `agent_runs.paused_reason` (в v1 единственное значение `credits_exhausted`), непусто только при `status=paused`. ⚠️ Это **НЕ** policy-enum `blockReason` из [ADR-004](../../adr/ADR-004-blocked-http-200.md)/[ADR-002](../../adr/ADR-002-access-policy-state-machine.md) (`credits_empty`/`subscription_expired`/…): наборы значений не пересекаются, поле переиспользует имя по запросу клиента ([ADR-066 §5](../../adr/ADR-066-agent-run-state-snapshot.md)). Валидировать policy-enum'ом **нельзя**. |
| `usage` | object | `{inputTokens, outputTokens}` — накопленный расход прогона (из `usage.delta` при incremental-пути, иначе из `run.completed{usage}`). Монотонен. |
| `updatedAt` | string (ISO-8601, UTC) | Время последней записи **состояния**. **Детектор устаревания**: при активном прогоне без подписчика на `/events` не двигается. Retention-sweep ([ADR-066 §7](../../adr/ADR-066-agent-run-state-snapshot.md)) очистку контента **не** считает обновлением и `updatedAt` не сдвигает. |
| `continuedFrom` | string \| null | `run_id` родительского (paused) прогона для child'а, созданного через `/resume` ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)); `null` у корневого. |

### Коды ответа
- **200** — снапшот отдан. Если строка `agent_runs` есть, а снапшота ещё нет (writer не отработал ни одного события) — **тоже 200**, с дефолтами: `resultText:""`, `lastTool:null`, `pendingApproval:null`, `usage:{0,0}`, `updatedAt` = `agent_runs.updated_at`.
- **401** — нет/неверный `X-API-Key` или нет/невалидный `X-User-Id`.
- **404** — прогон не найден **или** принадлежит другому пользователю ([06-rbac.md](06-rbac.md)); **403 не возвращается никогда**. Сюда же попадают прогоны, запущенные **до деплоя** [ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md) (строк `agent_runs` для них нет, backfill не делается).
- **429** — превышен общий клиентский rate-limit (`enforce_other_limits`, тот же контур, что у `chats`).
- **`200 {status:blocked}` на этом маршруте не возникает** — policy-gate не вызывается (чтение состояния не является генерацией).

### Маппинг статусов server → client ([ADR-066 §4](../../adr/ADR-066-agent-run-state-snapshot.md))
| `agent_runs.status` | Условие | `status` в ответе |
|---|---|---|
| `running`, `resumed` | `pending_approval IS NULL` | `running` |
| `running`, `resumed` | `pending_approval IS NOT NULL` | `waiting_approval` |
| `paused` | — | `paused` (+ `blockReason`) |
| `completed` | — | `completed` |
| `failed` | — | `failed` |
| `cancelled` | — | `stopped` |
| — | в v1 не эмитится | `queued` (только forward-compat в `Literal`) |

`waiting_approval` — **производный** статус (вычисляется на чтении), значением enum `agent_run_status` он **не является**. `resumed` относится к **родительской** строке после resume; работа идёт в child-прогоне, чей `runId` вернул `/resume`.

### Правила
- **Строго read-only** ([ADR-066 §5](../../adr/ADR-066-agent-run-state-snapshot.md)): **без `ensure_running`** (гибернированный контейнер **не будится**), **без обращения к Hermes**, **без списаний**, без policy-gate. Только `SELECT` из `agent_runs` + `agent_run_snapshots` ([03-data-model.md §25](../../03-data-model.md)).
- Источник снапшота — побочный эффект SSE-relay `/events` ([05-events.md §Снапшот-побочные эффекты](05-events.md)); поэтому состояние двигается **только пока кто-то подписан** на `/events`.
- Retention: через `AGENT_RUN_SNAPSHOT_TTL_DAYS` (14) у терминальных прогонов очищаются `resultText`/`pendingApproval`; эндпоинт продолжает отдавать `200` со `status`/`usage`/`updatedAt`.

### Известные ограничения (донести iOS)
1. **`resultText` активного прогона отстаёт**, пока к `/events` никто не подключён. Рецепт клиента: `GET /state` → если `running`/`waiting_approval` → **переподключиться к `/events`** и догнать поток; свежесть снапшота видна по `updatedAt`.
2. **Прогоны, запущенные до деплоя, → `404`** (backfill не делается).
3. **`stop` → `stopped` — eventual consistency:** строка флипается на 2xx passthrough `POST …/stop`, Hermes в этот момент может ещё дофлашивать события в открытый relay.
4. **Resume + kill приложения: родительский `runId` «залипает» в `running`.** После успешного `/resume` работа идёт в **child**-прогоне, а родительская строка навсегда остаётся в статусе `resumed`, который маппится в клиентский **`running`** ([ADR-066 §4](../../adr/ADR-066-agent-run-state-snapshot.md)). Если клиент потерял `runId` продолжения (убил приложение сразу после `/resume`) и опрашивает **родительский** `runId`, он увидит `status:running` с **навсегда замороженным** `updatedAt` и текстом до паузы — обратной ссылки parent→child (`continuedTo`) в v1 **нет**, только child→parent (`continuedFrom`). **Рецепт:** `status=running` + не растущий `updatedAt` у прогона, который вы возобновляли → **повторить `POST …/resume`**: он идемпотентен и вернёт `runId` активного продолжения (резолв `active_child`, [ADR-064 §5](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) — второй child при этом **не создаётся**. Аддитивное поле `continuedTo` — кандидат следующей ревизии контракта, зафиксирован как **[Q-066-3](../../99-open-questions.md)** (не tech-debt: контракт работоспособен, вопрос в удобстве клиента).

## POST /v1/agent/runs/{runId}/approval
Passthrough approval-ответа.

### Request
```json
{ "choice": "once|session|always|deny" }
```
- `choice` — одно из `once` | `session` | `always` | `deny`. **Значения — внешний контракт Hermes** ([D:\BA\hermes gateway/platforms/api_server.py](../../adr/ADR-045-hermes-as-agent-proxy.md)); control plane проксирует тело в `POST {base}/v1/runs/{runId}/approval` **as-is** (passthrough, без переопределения семантики). Канонический перечень значений — у Hermes; здесь зафиксирован для синхронности с [08-api-documentation.md §R5](../../08-api-documentation.md). Разблокирует прогон, ожидающий `approval.request`.

## POST /v1/agent/runs/{runId}/stop
Passthrough остановки прогона → `POST {base}/v1/runs/{runId}/stop`.

- **После 2xx passthrough** ([ADR-066 §3](../../adr/ADR-066-agent-run-state-snapshot.md)) — условная запись статуса: `runs_repo.mark_stopped(run_id, user_id)` = `UPDATE agent_runs SET status='cancelled' WHERE run_id=:id AND user_id=:uid AND status IN ('running','resumed')` (**owner-scoped**: RBAC — свойство самого запроса). Условие обязательно: остановка уже завершённого/`paused`-прогона **не должна** затирать терминальный статус. Симметрично условны и `completed`/`failed` — иначе долетевшее после `/stop` терминальное событие затёрло бы `cancelled` (Hermes продолжает дофлашивать события в открытый relay). До [ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md) `cancelled` не записывался никогда. В `/state` отображается как `stopped` (eventual consistency).
- **`mark_stopped(run_id, user_id)` — только этот, клиентский, путь.** Внутренний Hermes-interrupt при pause-at-zero ([ADR-064 §3](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) использует тот же `stop()`, но статус `cancelled` **не выставляет**: иначе пауза по кредитам транзиентно выглядела бы как `stopped` в `/state`, а `POST /resume` в этом окне вернул бы `409 run_not_resumable`.

## POST /v1/agent/runs/{runId}/resume  ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md))
Возобновление прогона, остановленного при исчерпании баланса (`run.paused`, [05-events.md](05-events.md)). Возобновление = **continuation**: запускается **новый** прогон в **той же** Hermes-сессии (память/контекст целы), с догрузкой истории. Доступно **только** для `status='paused'`.

### Headers
- `X-API-Key`, `X-User-Id` (обязательны).

### Request
```json
{ "message": "string|null" }
```
- `message` — опц.; дополнительный ход пользователя при возобновлении (маппится в Hermes `input` нового прогона). При `null`/отсутствии агент продолжает с последнего состояния сессии.

### Response
- **202** (возобновлено): `{"status": "running", "runId": "<new_run_id>", "continuedFrom": "<paused_run_id>"}` (тело — `AgentRunResponse`). Клиент подписывается на `GET /v1/agent/runs/{new_run_id}/events`.
- **200** (blocked, [ADR-004](../../adr/ADR-004-blocked-http-200.md)): баланс всё ещё `0`/долг → `{"status":"blocked","blockReason":"credits_empty|subscription_expired|trial_used|debt_outstanding"}` (тот же достижимый набор, что `POST /v1/agent/run`). Resume без пополнения прогон не запускает.
- **404** — прогон не найден **или** принадлежит другому пользователю (RBAC, [06-rbac.md](06-rbac.md)); `agent_runs[runId].user_id != subject`.
- **409** `run_not_resumable` — `status ∉ {paused, resumed}` (running/completed/failed/cancelled не возобновляемы). Это ранний информационный гвард; авторитетный арбитр — CAS.
- **409** `resume_in_progress` — конкурентный resume выиграл CAS и ещё не зафиксировал child (узкое окно); клиент ретраит → получит `202` с child. **Второй child НЕ создаётся.**
- **409** `session_expired` ([ADR-064 §7](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md), [Q-064-3](../../99-open-questions.md)) — hydrate `GET {base}/api/sessions/{sessionId}/messages` вернул `404` **или пустую историю**: Hermes-сессия истекла/недоступна, continuation невозможен → CAS откатывается `resumed→paused` (прогон остаётся paused), клиент начинает новый прогон через `POST /v1/agent/run`. **Отличие от 502:** `session_expired` (409) — сессии *нет* (детерминированный отказ); 502 — сессия/инстанс временно *недоступны* (транзиентно).
- **401** — нет/неверный `X-API-Key`/`X-User-Id`.
- **502** — инстанс недоступен / `ensure_running` не поднял контейнер / Hermes 5xx / hydrate transport-ошибка или non-2xx (кроме `404` → `session_expired`) / сбой `_launch_run`. После любого сбоя **до** создания child CAS откатывается `resumed→paused` — прогон остаётся возобновляемым.

### Правила
- Поток ([ADR-064 §5](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)): auth → RBAC-404 → пред-гвард `status∈{paused,resumed}` (иначе 409 `run_not_resumable`) → **policy-gate** (read-only, blocked 200 если баланс 0/долг, **до** флипа статуса) → **атомарный CAS `paused→resumed`** (арбитр гонки, отдельная короткая транзакция: `UPDATE agent_runs SET status='resumed' WHERE run_id=:id AND status='paused' RETURNING session_id, model`) → **ветвление:**
  - **выиграл** (`rowcount=1`): `ensure_running` (wake) → **hydrate** `GET {base}/api/sessions/{session_id}/messages` → `conversation_history` (при `404`/пустой истории → `409 session_expired`, откат CAS) → `_launch_run` нового прогона **строго после выигрыша CAS** (та же `session_id`, `model` из CAS RETURNING) → `create_running(new, continued_from=runId, status='running')` → `202 {runId:new, continuedFrom:runId}`;
  - **проиграл/ретрай** (`rowcount=0`): резолв active child (`continued_from_run_id==runId`) — есть → `202 {runId:child, continuedFrom:runId}`; ещё не зафиксирован → `409 resume_in_progress`.
- **Защита от гонки (CRITICAL, [ADR-064 §5](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)):** конкурентные in-flight resume / сетевой ретрай `POST /resume` **не** создают два child в одной `session_id` — CAS `paused→resumed` пропускает к launch **ровно один** запрос; `POST /v1/runs` не идемпотентен ([ADR-062](../../adr/ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)), поэтому launch — **только** после выигрыша CAS (single-flight, исключает интерливинг памяти сессии + двойной биллинг + orphan). При сбое до создания Hermes-run — откат CAS `resumed→paused` (образец claim-rollback [ADR-054](../../adr/ADR-054-trial-claim-reconcile.md)).
- **Свежий keyspace `f"{new_run_id}:{step}"`** ([ADR-064 §5](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) — списания нового прогона не пересекаются с paused (`old_run_id:%`) → без двойного счёта; seed `charged` нового прогона читает ledger по `new_run_id`.
- `session_id` клиенту хранить/передавать **не нужно** — резолвится по paused `run_id` из `agent_runs`.

## AgentRunResponse — аддитивное поле
- `continuedFrom: string|null` ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) — для прогона, созданного через resume, содержит `run_id` родительского (paused) прогона; `null` для корневого прогона (`POST /v1/agent/run`). Аддитивно, обратно совместимо.

## Маппинг iOS ↔ Hermes (сводка)
| iOS (`/v1/agent/run`) | Hermes (`POST /v1/runs`) |
|---|---|
| `message` | `input` |
| `sessionId` | `session_id` |
| `model` | `model` |
| `runId` (в ответе) | `run_id` |

`instructions`/`conversation_history` Hermes на старте клиентом не задаются (история — внутри инстанса по `session_id`).
