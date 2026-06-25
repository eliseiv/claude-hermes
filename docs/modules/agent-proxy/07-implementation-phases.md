# Agent Proxy — Implementation Phases

Соответствует Спринтам 1/3/4 плана Hermes-интеграции ([ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md), [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)). Зависит от [Hermes Runtime](../hermes-runtime/07-implementation-phases.md) (Спринт 2).

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

> Тесты — [09-testing.md](09-testing.md). Hermes-инстанс мокается (respx/httpx-mock) в unit; реальный инстанс — integration/e2e.
