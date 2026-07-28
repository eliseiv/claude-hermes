# Agent Proxy — Overview

## Назначение
Контур `/v1/agent/*` подключает автономного агента Hermes как полноценного «коллегу»: чат iOS проксируется к персональному Hermes-инстансу пользователя (его собственный tool-loop/память/навыки), события стримятся обратно на iOS, биллинг — по реальному usage прогона. Это headline-возможность сервиса; простой per-turn чат `/v1/chat/*` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) остаётся как опция.

## In scope
- `POST /v1/agent/run` — запуск прогона (policy-gate → ensure_running → прокси `POST /v1/runs`).
- `GET /v1/agent/runs/{runId}/events` — SSE-ретрансляция + биллинг на `run.completed`.
- `POST /v1/agent/runs/{runId}/approval` — passthrough approval.
- `POST /v1/agent/runs/{runId}/stop` — passthrough stop (+ пометка прогона `cancelled`, [ADR-066 §3](../../adr/ADR-066-agent-run-state-snapshot.md)).
- `POST /v1/agent/runs/{runId}/resume` — возобновление после паузы по кредитам ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)).
- `GET /v1/agent/runs/{runId}/state` — снапшот состояния прогона из БД для восстановления UI после kill приложения ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md)); read-only, инстанс не будит.
- Персистенция lifecycle прогона (`agent_runs`) и снапшота (`agent_run_snapshots`) как побочный эффект SSE-relay.
- Маппинг iOS-контракта на контракт Hermes API-сервера.

## Out of scope
- Жизненный цикл инстансов (provision/start/stop) — [Hermes Runtime](../hermes-runtime/README.md) ([ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md)).
- Авторизация клиента — [API Gateway](../api-gateway/README.md) / [ADR-044](../../adr/ADR-044-client-api-key-auth.md).
- Внутренняя логика агента Hermes (это Hermes, не наш код).
- `/v1/chat/*` (простой чат) — [Chat Orchestrator](../chat-orchestrator/README.md), не трогается.

## Ключевые решения
- [ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md) — Hermes-as-agent, контур `/v1/agent/*`, прокси + SSE.
- [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md) — биллинг по usage, idempotency по `runId`, policy-gate.
- [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) / [ADR-004](../../adr/ADR-004-blocked-http-200.md) — policy + blocked HTTP 200.
- [ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md) — incremental-биллинг, pause-at-zero, resume; таблица `agent_runs`.
- [ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md) — снапшот состояния прогона (`agent_run_snapshots`, read-only `/state`, retention 14 дней); `agent_runs` — безусловная lifecycle-запись.

## Открытые вопросы
- [Q-066-1](../../99-open-questions.md) — семантика replay Hermes при re-subscribe (буфер с начала vs только новые события); подтвердить при патче образа / E2E.
- [Q-066-2](../../99-open-questions.md) — допущение о глобальной уникальности Hermes `run_id` при глобальном `TEXT PK` (tenancy-гвард как defense-in-depth).
- [Q-066-3](../../99-open-questions.md) — обратная ссылка `continuedTo` (parent→child) в `/state`; кандидат следующей ревизии контракта.
- [Q-047-1](../../99-open-questions.md) — коэффициенты конвертации usage→кредиты / округление.
- [Q-047-2](../../99-open-questions.md) — разрыв SSL до `run.completed`, usage больше остатка (hold/reconcile).
- [Q-047-3](../../99-open-questions.md) — BYOK для агентного пути.
