# Agent Proxy — Context

## Зависимости (входящие)
- **iOS-клиент** — вызывает `/v1/agent/*` (клиентская auth `X-API-Key` + `X-User-Id`).
- [API Gateway](../api-gateway/README.md) — auth ([ADR-044](../../adr/ADR-044-client-api-key-auth.md)), lazy provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)), rate limit, размещение роутера.

## Зависимости (исходящие)
- [Policy Engine](../policy-engine/README.md) — `evaluate` (BR-2/3/5) до прогона ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md)).
- [Hermes Runtime](../hermes-runtime/README.md) — `ensure_running(userId)` → `InstanceEndpoint`, `health` ([ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md)).
- [Wallet / Ledger](../wallet-ledger/README.md) — `consume(idempotency_key=runId)` на `run.completed` ([ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)).
- [Audit](../audit/README.md) — события прогона/списания.
- **Hermes-инстанс** (`POST /v1/runs`, SSE `/events`, `/approval`, `/stop`) — через `httpx.AsyncClient`.
- **PostgreSQL** — `agent_runs` (lifecycle прогона, [ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) и `agent_run_snapshots` (снапшот состояния, [ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md), [03-data-model.md §24–§25](../../03-data-model.md)).

## Границы
- **`GET …/state` не добавляет зависимостей** ([ADR-066 §5](../../adr/ADR-066-agent-run-state-snapshot.md)): читает только нашу БД — **не** обращается к Hermes-инстансу, **не** дёргает `ensure_running` ([Hermes Runtime](../hermes-runtime/README.md)), **не** трогает [Wallet](../wallet-ledger/README.md) и [Policy Engine](../policy-engine/README.md). Патча образа Hermes ([ADR-065](../../adr/ADR-065-patched-hermes-image-ghcr.md)) фича **не требует** — в отличие от incremental-биллинга.
- НЕ управляет жизненным циклом инстанса (это [Hermes Runtime](../hermes-runtime/README.md)).
- НЕ вызывает наш `LLMClient` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) — Hermes использует свой LLM внутри инстанса.
- НЕ затрагивает `/v1/chat/*` (отдельный контур, биллинг 1 кредит = 1 сообщение [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).

## Соседи
- [Hermes Runtime](../hermes-runtime/README.md) — нижележащий модуль (инстансы).
- [Chat Orchestrator](../chat-orchestrator/README.md) — параллельный контур «простого чата».
- [Wallet](../wallet-ledger/README.md) / [Policy Engine](../policy-engine/README.md) / [Subscription](../subscription/README.md) — переиспользуются как есть.
