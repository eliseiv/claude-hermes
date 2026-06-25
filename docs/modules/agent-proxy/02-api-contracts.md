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
- **502** — инстанс недоступен / `ensure_running` не поднял контейнер / Hermes 5xx.

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
- Полный перечень событий — [05-events.md](05-events.md).

## POST /v1/agent/runs/{runId}/approval
Passthrough approval-ответа.

### Request
```json
{ "choice": "once|session|always|deny" }
```
- `choice` — одно из `once` | `session` | `always` | `deny`. **Значения — внешний контракт Hermes** ([D:\BA\hermes gateway/platforms/api_server.py](../../adr/ADR-045-hermes-as-agent-proxy.md)); control plane проксирует тело в `POST {base}/v1/runs/{runId}/approval` **as-is** (passthrough, без переопределения семантики). Канонический перечень значений — у Hermes; здесь зафиксирован для синхронности с [08-api-documentation.md §R5](../../08-api-documentation.md). Разблокирует прогон, ожидающий `approval.request`.

## POST /v1/agent/runs/{runId}/stop
Passthrough остановки прогона → `POST {base}/v1/runs/{runId}/stop`.

## Маппинг iOS ↔ Hermes (сводка)
| iOS (`/v1/agent/run`) | Hermes (`POST /v1/runs`) |
|---|---|
| `message` | `input` |
| `sessionId` | `session_id` |
| `model` | `model` |
| `runId` (в ответе) | `run_id` |

`instructions`/`conversation_history` Hermes на старте клиентом не задаются (история — внутри инстанса по `session_id`).
