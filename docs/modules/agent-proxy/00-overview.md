# Agent Proxy — Overview

## Назначение
Контур `/v1/agent/*` подключает автономного агента Hermes как полноценного «коллегу»: чат iOS проксируется к персональному Hermes-инстансу пользователя (его собственный tool-loop/память/навыки), события стримятся обратно на iOS, биллинг — по реальному usage прогона. Это headline-возможность сервиса; простой per-turn чат `/v1/chat/*` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) остаётся как опция.

## In scope
- `POST /v1/agent/run` — запуск прогона (policy-gate → ensure_running → прокси `POST /v1/runs`).
- `GET /v1/agent/runs/{runId}/events` — поток событий прогона клиенту. С [ADR-067](../../adr/ADR-067-agent-run-background-consumer.md) — **читающий downstream** из broker'а (Redis ring + pub/sub), к Hermes не подключается; контракт для клиента не изменился.
- **Фоновый consumer прогона** ([ADR-067](../../adr/ADR-067-agent-run-background-consumer.md)) — единственный upstream-подписчик Hermes; исполняет биллинг, снапшот и терминальный статус **независимо от наличия клиента**. Страховка — orphan-reaper.
- `POST /v1/agent/runs/{runId}/approval` — passthrough approval.
- `POST /v1/agent/runs/{runId}/stop` — passthrough stop (+ пометка прогона `cancelled`, [ADR-066 §3](../../adr/ADR-066-agent-run-state-snapshot.md)).
- `POST /v1/agent/runs/{runId}/resume` — возобновление после паузы по кредитам ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)).
- `GET /v1/agent/runs/{runId}/state` — снапшот состояния прогона из БД для восстановления UI после kill приложения ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md)); read-only, инстанс не будит.
- Персистенция lifecycle прогона (`agent_runs`) и снапшота (`agent_run_snapshots`) — **побочный эффект consumer'а** ([ADR-067 §2](../../adr/ADR-067-agent-run-background-consumer.md); до 2026-07-29 — клиентского SSE-relay, что и давало нетарифицируемые прогоны, [TD-037](../../100-known-tech-debt.md)).
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
- [ADR-067](../../adr/ADR-067-agent-run-background-consumer.md) — фоновый consumer + broker-модель `/events`: единственная upstream-подписка наша, клиент читает downstream; закрывает утечку выручки на прогонах без подписчика ([TD-037](../../100-known-tech-debt.md), [Q-047-2](../../99-open-questions.md)). Миграции нет; форма событий не меняется, **семантика подключения — меняется** (каждое соединение начинается с реплея), требует согласования с iOS.

## Открытые вопросы
- [Q-067-1](../../99-open-questions.md) — доедут ли свежей подписке события, произведённые после дренажа буфера (от этого зависит подхват прогона после рестарта `api`).
- [Q-067-2](../../99-open-questions.md) — лимит одновременных активных прогонов на пользователя (consumer держит ресурс и не даёт инстансу заснуть).
- [Q-067-3](../../99-open-questions.md) — нужен ли клиенту маркер усечения replay-буфера в `/events`.
- [Q-066-1](../../99-open-questions.md) — **Partially Closed 2026-07-29:** закрыто сырое наблюдение (вторая последовательная подписка получила 0 байт); интерпретация «дренаж буфера» — **гипотеза H1**, живость прогона в момент замера не зафиксирована → обязателен перемер с критерием живости.
- [Q-067-4](../../99-open-questions.md) — несёт ли hydrate `GET /api/sessions/{id}/messages` usage/токены (**проверить ДО Phase 9**: меняет объём orphan-reaper'а).
- [Q-067-5](../../99-open-questions.md) — поддерживает ли образ одновременных подписчиков на один `runId` (не измерялось).
- ~~[Q-066-2](../../99-open-questions.md)~~ — **Closed 2026-07-29:** `run_id` = `run_` + 32 hex (~128 бит) ⇒ глобальная уникальность валидна; tenancy-гвард остаётся как defense-in-depth.
- [Q-066-3](../../99-open-questions.md) — обратная ссылка `continuedTo` (parent→child) в `/state`; кандидат следующей ревизии контракта.
- [Q-047-1](../../99-open-questions.md) — коэффициенты конвертации usage→кредиты / округление.
- ~~[Q-047-2](../../99-open-questions.md)~~ — **Closed 2026-07-29 ([ADR-067](../../adr/ADR-067-agent-run-background-consumer.md)):** прогон без подписчика тарифицирует фоновый consumer; прежняя митигация «повторная подписка довыполнит debit» была неверной.
- [Q-047-3](../../99-open-questions.md) — BYOK для агентного пути.
