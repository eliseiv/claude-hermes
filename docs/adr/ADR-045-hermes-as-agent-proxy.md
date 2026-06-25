# ADR-045 — Hermes-as-agent: контур `/v1/agent/*` как прокси к Hermes `POST /v1/runs`

- Статус: Accepted
- Дата: 2026-06-23
- Связан с: [ADR-044](ADR-044-client-api-key-auth.md) (клиентская auth), [ADR-046](ADR-046-per-user-hermes-runtime.md) (per-user runtime), [ADR-047](ADR-047-usage-based-billing-for-agent.md) (биллинг по usage), [ADR-002](ADR-002-access-policy-state-machine.md) (policy), [ADR-004](ADR-004-blocked-http-200.md) (blocked HTTP 200), [ADR-033](ADR-033-llm-provider-abstraction.md) (LLM-провайдер — Hermes использует СВОЙ внутри инстанса), [01-architecture.md](../01-architecture.md), [modules/agent-proxy/](../modules/agent-proxy/README.md)
- Закрывает: архитектурный выбор «как именно подключается Hermes»

## Context

Принципиальное решение пользователя (НЕ пересматривать): **Hermes подключается как полноценный автономный агент, а не как замена LLM.** Существующий `LLMClient.create_message` ([ADR-033](ADR-033-llm-provider-abstraction.md)) — синхронный per-turn вызов модели; если завести Hermes туда, теряется его собственный tool-loop, память, навыки, обучение. Поэтому Hermes остаётся самостоятельным runtime'ом (per-user инстанс, [ADR-046](ADR-046-per-user-hermes-runtime.md)), а `claude-hermes` становится **control plane** (auth + policy + billing + жизненный цикл) и **прокси** между iOS и Hermes-инстансом пользователя.

Контракт Hermes API-сервера (подтверждён исследованием, бери как есть):
- `POST /v1/runs` → `202 {run_id, status}`; тело `{input, session_id?, model?, instructions?, conversation_history?}`.
- `GET /v1/runs/{id}/events` (SSE): `run.queued|run.running|message.delta|tool.started|tool.completed|approval.request|run.completed{usage:{input_tokens,output_tokens,total_tokens}}|run.failed`.
- `POST /v1/runs/{id}/approval {choice}`, `POST /v1/runs/{id}/stop`.
- Auth к инстансу: `Authorization: Bearer <API_SERVER_KEY>` (per-instance, [ADR-046](ADR-046-per-user-hermes-runtime.md)).

Существующий контур `/v1/chat/*` (прямой LLM, client-side tools, [ADR-033](ADR-033-llm-provider-abstraction.md)) — оставить как опциональный «простой чат», не заводя Hermes в него.

## Decision

### 1. Новый контур `/v1/agent/*` (прокси к Hermes)

Новый роутер `src/app/api_gateway/routers/agent.py`, регистрируется в `main.py`. Эндпоинты (клиентская auth — [ADR-044](ADR-044-client-api-key-auth.md): `X-API-Key` + `X-User-Id`):

| Метод/путь | Прокси к Hermes | Назначение |
|---|---|---|
| `POST /v1/agent/run` | `POST {base}/v1/runs` | policy-gate → `ensure_running(userId)` → запуск прогона; `202 {runId, status}` |
| `GET /v1/agent/runs/{runId}/events` | `GET {base}/v1/runs/{runId}/events` (SSE) | ретрансляция SSE на iOS; биллинг по `run.completed.usage` |
| `POST /v1/agent/runs/{runId}/approval` | `POST {base}/v1/runs/{runId}/approval` | passthrough `{choice}` |
| `POST /v1/agent/runs/{runId}/stop` | `POST {base}/v1/runs/{runId}/stop` | passthrough |

HTTP-клиент — `httpx.AsyncClient` (уже в стеке, [02-tech-stack.md](../02-tech-stack.md)). Адресация инстанса — DNS-имя контейнера в docker-сети control plane (`hermes-user-<id>:8642`), резолвится через registry ([ADR-046](ADR-046-per-user-hermes-runtime.md)). Запрос к инстансу несёт `Authorization: Bearer <API_SERVER_KEY>` (расшифрованный из `hermes_instances.api_key_enc`, [ADR-046](ADR-046-per-user-hermes-runtime.md)).

### 2. `POST /v1/agent/run` — поток

1. **Auth** ([ADR-044](ADR-044-client-api-key-auth.md)): `X-API-Key` + `X-User-Id` → lazy provisioning ([ADR-007](ADR-007-lazy-user-provisioning.md)).
2. **Policy-gate** ([ADR-047 §3](ADR-047-usage-based-billing-for-agent.md)): `PolicyEngine.evaluate` (BR-2/BR-3/BR-5 — активная подписка + кредиты). Blocked → `200 {status:"blocked", blockReason}` ([ADR-004](ADR-004-blocked-http-200.md): бизнес-blocked = HTTP 200).
3. **`ensure_running(userId)`** ([ADR-046](ADR-046-per-user-hermes-runtime.md)) → `InstanceEndpoint(base_url, api_key)` (провижинит/будит контейнер, обновляет `last_active_at`).
4. **Прокси** `POST {base}/v1/runs` с `Authorization: Bearer <api_key>`; маппинг тела (§4).
5. Возврат `202 {runId, status}` (proxy Hermes `run_id` → `runId`).

### 3. SSE-ретрансляция `GET /v1/agent/runs/{runId}/events`

- Открыть SSE к `GET {base}/v1/runs/{runId}/events`, ретранслировать события клиенту as-is (`run.queued|run.running|message.delta|tool.started|tool.completed|approval.request|run.failed`).
- На событии **`run.completed`** извлечь `usage:{input_tokens,output_tokens,total_tokens}` → `WalletService.consume(user_id, amount, idempotency_key=runId, meta)` (конвертация токенов в кредиты, идемпотентность по `runId` — [ADR-047](ADR-047-usage-based-billing-for-agent.md)). Списание выполняется ровно один раз благодаря idempotency-ledger ([ADR-005](ADR-005-idempotency-ledger.md)).
- `run.failed` пробрасывается клиенту; **кредит не списывается** (нет `run.completed.usage`).
- Шаблон стрима — `httpx.AsyncClient.stream` (паттерн из Explore-контракта существующего кода).
- `approval.request` пробрасывается клиенту; клиент отвечает через `POST /v1/agent/runs/{runId}/approval`.

### 4. Маппинг тела запроса (iOS-контракт → Hermes-контракт)

`POST /v1/agent/run` request (iOS) → Hermes `POST /v1/runs` body:

| iOS поле | Hermes поле | Примечание |
|---|---|---|
| `message` | `input` | обязательное |
| `sessionId` | `session_id` | опц.; преемственность диалога внутри инстанса |
| `model` | `model` | опц.; модель Hermes внутри инстанса |
| (нет) | `instructions` | опц.; на старте не пробрасывается клиентом (резерв) |
| (нет) | `conversation_history` | опц.; история ведётся внутри инстанса (`session_id`) — не пробрасываем |

Точные схемы request/response — `src/app/schemas/agent.py`, [modules/agent-proxy/02-api-contracts.md](../modules/agent-proxy/02-api-contracts.md).

### 5. Отношение к `/v1/chat/*` («простой чат» остаётся)

- `/v1/chat/*` ([ADR-033](ADR-033-llm-provider-abstraction.md), client-side tools, биллинг 1 кредит = 1 сообщение [ADR-006](ADR-006-credit-billing-and-subscription-grant.md)) — **не трогается**. Hermes в `LLMClient.create_message` НЕ заводится.
- Два контура сосуществуют: `/v1/chat/*` (простой чат, per-turn LLM) и `/v1/agent/*` (автономный агент Hermes). Headline-фича — `/v1/agent/*`.
- Hermes использует **СВОЙ** `LLM_PROVIDER`/`*_API_KEY`/`LLM_MODEL` внутри инстанса ([ADR-046](ADR-046-per-user-hermes-runtime.md)) — это не наш `LLMClient` ([ADR-033](ADR-033-llm-provider-abstraction.md) границы не нарушаются; провайдер control plane и провайдер инстанса независимы).

### 6. Обработка ошибок прокси

- Инстанс недоступен / `ensure_running` не поднял контейнер / health fail → `502` (технический сбой, не бизнес-blocked).
- Hermes вернул 4xx/5xx → проксируется как соответствующий технический код (не `200 blocked`).
- Бизнес-blocked (policy) — только до прогона, `200 {status:blocked}` ([ADR-004](ADR-004-blocked-http-200.md)).

## Consequences

**Положительные:**
- Hermes сохраняет полную ценность автономного агента (свой tool-loop/память/навыки) — не деградирует до per-turn LLM.
- Control plane переиспользует wallet/subscription/policy/admin/audit/Swagger/БД без переписывания ядра.
- `/v1/chat/*` не ломается; два контура изолированы.
- Биллинг привязан к реальному `usage` агента ([ADR-047](ADR-047-usage-based-billing-for-agent.md)).

**Отрицательные / ограничения:**
- Появляется новый сетевой hop (control plane ↔ инстанс) и SSE-ретрансляция — выше латентность и сложность отказоустойчивости (см. §6).
- Биллинг происходит **после** генерации (на `run.completed`); при разрыве SSL до `run.completed` списание не произойдёт на этом соединении — митигация: idempotency по `runId` + повторная подписка на events / реконсиляция ([Q-047-2](../99-open-questions.md)).
- Привязка к контракту Hermes API-сервера (изменение его событий/полей ломает прокси) — зафиксирован как внешний контракт.

## Alternatives

1. **Завести Hermes в `LLMClient.create_message`.** Прямо отвергнуто пользователем: теряется автономность агента (tool-loop/память/навыки/обучение).
2. **Расширить `/v1/chat/*` режимом «agent».** Отвергнуто: смешивает per-turn-семантику и долгоживущий run/SSE/approvals; разный биллинг (1 кредит vs usage); разная модель ошибок. Отдельный контур чище.
3. **Синхронный (non-SSE) прокси с поллингом статуса.** Отвергнуто: Hermes отдаёт SSE нативно; поллинг хуже по латентности/нагрузке и не передаёт `message.delta`/`tool.*`/`approval.request` в реальном времени.
