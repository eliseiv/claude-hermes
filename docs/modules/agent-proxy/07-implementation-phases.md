# Agent Proxy — Implementation Phases

Соответствует Спринтам 1/3/4 плана Hermes-интеграции ([ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md), [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)). Зависит от [Hermes Runtime](../hermes-runtime/07-implementation-phases.md) (Спринт 2). **Phase 6/7 — incremental billing + pause + resume ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md), под флагом, требует патча образа Hermes). Phase 8 — снапшот состояния прогона + `GET …/state` ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md), без флага, патча образа НЕ требует).**

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

## Phase 8 — Снапшот состояния прогона + `/state` ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md))
Порядок этапов обязателен: каждый следующий опирается на предыдущий, первые два ценны и без эндпоинта.

1. **Схема.** `AgentRunSnapshot` в `src/app/models/tables.py` + миграция `migrations/versions/…0019_agent_run_snapshots.py` (expand-only, по образцу `0018`; `down_revision="0018_agent_runs"`, single head; DDL — [03-data-model.md §25](../../03-data-model.md)). Новый `src/app/agent_proxy/snapshots_repo.py`: upsert `ON CONFLICT (run_id) DO UPDATE` с **per-column** replay-guard (`CASE` по `char_length` для `result_text`, `GREATEST` для токенов, `CASE WHEN :assert_approval` для `pending_approval`, безусловный `EXCLUDED.*` для `last_tool`/`updated_at`; отдельный owner-scoped `clear_pending_approval(run_id, user_id)` под `POST …/approval` — **row-level `WHERE` запрещён**, [ADR-066 §6](../../adr/ADR-066-agent-run-state-snapshot.md)) + **идемпотентный** retention-sweep (guard `AND (result_text <> '' OR pending_approval IS NOT NULL)`, `updated_at` не трогается) под **частичный** индекс `ix_agent_run_snapshots_sweep`; owner-scoped `get(run_id, user_id)`/`clear_pending_approval(run_id, user_id)` + tenancy-гвард `WHERE t.user_id = EXCLUDED.user_id` в апсерте. DI в `src/app/deps.py`. Три настройки в `src/app/config.py` ([07-deployment.md](../../07-deployment.md)).
2. **Lifecycle-фиксы** ([ADR-066 §3](../../adr/ADR-066-agent-run-state-snapshot.md)) — **независимы от эндпоинта**: снять флаговый guard с `create_running` (корневой прогон + resume-child) и с записи терминального статуса. **Все переходы статуса — условные** `UPDATE … WHERE status IN ('running','resumed')` (в т.ч. `completed`/`failed`, иначе долетевшее после `/stop` терминальное событие затрёт `cancelled`). Терминальный статус пишет единый `_mark_terminal(run_id, status)` (прежний `_mark_completed` удалён), вызываемый в обработчике `run.completed`/`run.failed` **до** биллинга и независимо от его исхода — иначе отказ списания оставляет прогон вечно `running`. Новый owner-scoped `runs_repo.mark_stopped(run_id, user_id)` (`AND user_id=:uid`) вызывается **только** на клиентском пути после 2xx `POST /stop`; внутренний interrupt pause-at-zero (тот же `self.stop()`, [ADR-064 §3](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) его **не** выполняет — иначе пауза транзиентно станет `cancelled` и сломает `/state` + `/resume`. Биллинг (`record_step`, per-step debits, pause-at-zero, финализация, resume) остаётся под `agent_incremental_billing_enabled`.
3. **Снапшот-writer в relay** (`stream_events`): вынести накопление `partial_text`/`steps`/токенов из ветки `if incremental:`; апсерты по событиям с явным `commit()` — правила и троттлинг в [05-events.md §Снапшот-побочные эффекты](05-events.md); расширить набор обрабатываемых событий на `approval.request`; очистка `pending_approval` также в `approval()` после 2xx — через `snapshots_repo.clear_pending_approval(run_id, user_id)`, а не апсертом. Сервисная обёртка `_mark_terminal(run_id, status)` вызывает репозиторный `mark_status` (условный переход), коммитит и проглатывает `SQLAlchemyError` — сбой записи статуса не рвёт стрим. `preview` в `_build_pending_approval()` обрезается тем же `AGENT_STATE_RESULT_TEXT_MAX_CHARS`.
4. **Эндпоинт** `GET /v1/agent/runs/{runId}/state` в `src/app/api_gateway/routers/agent.py` + `AgentProxyService.get_state(user_id, run_id)`; схемы `AgentRunStateResponse`/`AgentPendingApproval` в `src/app/schemas/agent.py` (StrictModel, camelCase, русские descriptions); чистая функция маппинга статусов ([02-api-contracts.md](02-api-contracts.md)). **Read-only инварианты зафиксировать в docstring роута:** без `ensure_running`, без обращения к Hermes, без списаний, без policy-gate. OpenAPI responses `200`/`401`/`404`/`429` с примерами ([08-api-documentation.md §R5](../../08-api-documentation.md)).
5. **Retention** — sweep в существующем reaper-цикле по `AGENT_RUN_SNAPSHOT_TTL_DAYS` ([ADR-066 §7](../../adr/ADR-066-agent-run-state-snapshot.md)); может отгружаться последним.

> Тесты — [09-testing.md](09-testing.md) (incremental/telescoping/pause-no-debt/seed-reconnect/финализация/флаг-OFF/concurrent-resume CAS/миграция 0018). Hermes-инстанс мокается (respx/httpx-mock) в unit; реальный инстанс — integration/e2e. Патч образа Hermes (`usage.delta` + hydrate) — отдельная devops/Hermes-зона.
