# Agent Proxy — Implementation Phases

Соответствует Спринтам 1/3/4 плана Hermes-интеграции ([ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md), [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)). Зависит от [Hermes Runtime](../hermes-runtime/07-implementation-phases.md) (Спринт 2). **Phase 6/7 — incremental billing + pause + resume ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md), под флагом, требует патча образа Hermes).**

## Phase 1 — Auth swap (предусловие, Спринт 1, [ADR-044](../../adr/ADR-044-client-api-key-auth.md))
- `verify_client_api_key()` (`src/app/api_gateway/auth.py`) + переписанный `get_current_user` (`X-API-Key` + `X-User-Id`).
- OpenAPI-схемы `clientApiKey`+`userId` (`openapi_security.py`), Swagger.
- `require_owner` → no-op. JWT/Apple остаются (дремлют).

## Phase 2 — Контракт и схемы
- `src/app/schemas/agent.py` — request/response `/v1/agent/*` ([02-api-contracts.md](02-api-contracts.md)).
- Роутер `src/app/api_gateway/routers/agent.py`, регистрация в `main.py`.
- `httpx.AsyncClient` для прокси/SSE.

## Phase 3 — run + policy + ensure_running
- `POST /v1/agent/run`: policy-gate (`PolicyEngine.evaluate`, blocked → `200`) → `HermesInstanceManager.ensure_running` → прокси `POST /v1/runs` (`Authorization: Bearer <api_key>`) → `202 {runId}`.
- Маппинг тела (`message→input` и т.д.).
- Обработка ошибок инстанса (`502`).

## Phase 4 — SSE + биллинг ([ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md))
- `GET /v1/agent/runs/{runId}/events` — SSE-ретрансляция; парсинг событий ([05-events.md](05-events.md)).
- На `run.completed.usage` → `WalletService.consume(idempotency_key=runId)`; конвертация токенов (`CREDITS_PER_1K_*`, ceil, мин. 1).
- `run.failed` → без debit. Audit `agent_run`/`billing_debit`.
- `POST .../approval`, `POST .../stop` — passthrough.

## Phase 5 — Config + интеграция
- `CREDITS_PER_1K_INPUT`/`CREDITS_PER_1K_OUTPUT` в config ([07-deployment.md](../../07-deployment.md)).
- Интеграция с [Hermes Runtime](../hermes-runtime/README.md); e2e end-to-end (run → SSE → consume).

## Phase 6 — Incremental billing + pause-at-zero ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md) §1–§4, под флагом `agent_incremental_billing_enabled`)
- Предусловие: **патч образа Hermes** — событие `usage.delta` (`cumulative_*_tokens`, `step_index`) — **devops/Hermes-зона** (пересборка образа + digest).
- `src/app/config.py` — `agent_incremental_billing_enabled` (default OFF).
- Таблица `agent_runs` + enum + миграция `0018` (`src/app/models/tables.py`, `migrations/versions/…0018_agent_runs.py`); `src/app/agent_proxy/runs_repo.py` (create_running/get/record_step/mark_paused/mark_status/active_child).
- `src/app/wallet/service.py` — `charged_for_run` (SUM по `runId OR runId:%`, `ESCAPE '\'`) + `_consume_incremental_clamp` (маршрутизация по `meta.incremental` **до** `_debit_in_savepoint`; `LEAST(:amount,balance)`, без raise/без debt).
- `src/app/agent_proxy/service.py` — incremental billing в `stream_events` (seed `charged`, per-step `_bill_step`+commit, `if charge>0`), pause-at-zero `_pause_run` (stop + синтетическое `run.paused`), финализация остатка в `_bill_completed` (idempotency `runId`), корневая строка `agent_runs` в `run()` + `effective_session_id`.

## Phase 7 — Resume (continuation) ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md) §5)
- `src/app/schemas/agent.py` — `AgentResumeRequest{message?}` + `AgentRunResponse.continuedFrom`.
- `src/app/api_gateway/routers/agent.py` — `POST /v1/agent/runs/{runId}/resume`.
- `service.py resume()` — RBAC-404 → пред-гвард → policy-gate (blocked 200) → **атомарный CAS `paused→resumed`** (арбитр гонки) → ветвление (winner: ensure_running→hydrate→_launch_run **после** CAS→chain `create_running(continued_from)`; loser/ретрай: active child → `202`/`409 resume_in_progress`) → reconcile-откат CAS на сбое.
- Hydrate `_fetch_session_transcript` (`GET {base}/api/sessions/{session_id}/messages`) → `conversation_history`.

> Тесты — [09-testing.md](09-testing.md) (incremental/telescoping/pause-no-debt/seed-reconnect/финализация/флаг-OFF/concurrent-resume CAS/миграция 0018). Hermes-инстанс мокается (respx/httpx-mock) в unit; реальный инстанс — integration/e2e. Патч образа Hermes (`usage.delta` + hydrate) — отдельная devops/Hermes-зона.
