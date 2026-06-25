# Module: Agent Proxy

- Статус: **Спроектирован, ожидает реализации** (Hermes-интеграция)
- Ответственность: контур `/v1/agent/*` — прокси чата к персональному Hermes-инстансу пользователя (`POST /v1/runs`), ретрансляция SSE-событий на iOS, policy-gate до прогона, биллинг по реальному usage на `run.completed`. Headline-фича сервиса (автономный агент). Простой чат `/v1/chat/*` остаётся отдельно.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [05-events.md](05-events.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
- `POST /v1/agent/run` — auth ([ADR-044](../../adr/ADR-044-client-api-key-auth.md)) → policy-gate (BR-2/3/5, [ADR-002](../../adr/ADR-002-access-policy-state-machine.md)) → `ensure_running(userId)` ([ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md)) → прокси `POST {base}/v1/runs` (`Authorization: Bearer <API_SERVER_KEY>`) → `202 {runId, status}`. Blocked → `200 {status:blocked, blockReason}` ([ADR-004](../../adr/ADR-004-blocked-http-200.md)).
- `GET /v1/agent/runs/{runId}/events` — SSE-ретрансляция из Hermes; на `run.completed{usage}` → `WalletService.consume(idempotency_key=runId)` (конвертация токенов в кредиты, [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)); `run.failed` → без debit.
- `POST /v1/agent/runs/{runId}/approval`, `POST /v1/agent/runs/{runId}/stop` — passthrough к Hermes.
- Маппинг тела `message→input`, `sessionId→session_id`, `model→model`.
- `/v1/chat/*` не затронут; Hermes в `LLMClient.create_message` не заводится.

## Changelog
- 2026-06-23: bootstrap модуля (architect). Зафиксированы [ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md) (agent-proxy), [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md) (биллинг по usage), контракты эндпоинтов/SSE, RBAC, фазы, тесты. Scope backend.
- 2026-06-23: исправлен достижимый набор `blockReason` для `POST /v1/agent/run` (architect, по факту кода + [ADR-002](../../adr/ADR-002-access-policy-state-machine.md)). Финал: `credits_empty | subscription_expired | trial_used`. Добавлен достижимый `trial_used` (подписки нет, trial израсходован); убран недостижимый `subscription_required` (byok-ветка). Источник истины перечня — Policy Engine ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md)). **Требует синхронизации backend (оба места, отдельный шаг):** (1) схема `src/app/schemas/agent.py` (`AgentRunResponse.blockReason`); (2) frozenset `_AGENT_BLOCK_REASONS` в `src/app/agent_proxy/service.py:46` — сейчас содержит недостижимый `subscription_required` и **не** содержит достижимый `trial_used`, из-за чего defensive-ветка ложно логирует `trial_used` как unexpected. Оба должны стать `{credits_empty, subscription_expired, trial_used}`.
