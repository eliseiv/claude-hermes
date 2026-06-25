# Agent Proxy — Testing

Стратегия — [06-testing-strategy.md](../../06-testing-strategy.md). Hermes-инстанс мокается (respx/httpx) в unit; реальный инстанс — integration/e2e.

## Unit (Hermes мокается, `HermesInstanceManager` мокается)
- `POST /v1/agent/run`:
  - policy blocked (нет подписки / 0 кредитов) → `200 {status:blocked, blockReason}`, прогон НЕ запущен, `ensure_running` не вызван.
  - allowed → `ensure_running` вызван, прокси `POST /v1/runs` с `Authorization: Bearer <api_key>`, маппинг `message→input`/`sessionId→session_id`/`model→model`, ответ `202 {runId}`.
  - инстанс недоступен / Hermes 5xx → `502` (не `200 blocked`).
  - нет `X-API-Key` / нет/невалидный `X-User-Id` → `401`.
- SSE-ретрансляция:
  - события `run.running`/`message.delta`/`tool.*`/`approval.request` пробрасываются клиенту as-is.
  - `run.completed{usage}` → `WalletService.consume(idempotency_key=runId)` ровно один раз; `amount=ceil(...)`, мин. 1.
  - `run.failed` → проброс, **без** debit.
  - повторная подписка/дубль `run.completed` того же `runId` → один debit (идемпотентность).
- `approval`/`stop` — passthrough к Hermes с корректным `runId`/Bearer.

## Integration (testcontainers Postgres + Redis; Hermes мок)
- policy ↔ wallet ↔ ledger: consume пишет `ledger_transactions(type=debit, idempotency_key=runId, meta.usage)`; баланс уменьшается один раз; `balance>=0` CHECK соблюдён.
- Источник credit-tx: agent-debit (`source=agent_run`, ключ `runId`) не конфликтует с chat-debit (`messageStepId`).

## E2E (реальный Docker + Hermes-образ, [09-e2e-testing.md](../../09-e2e-testing.md))
- `POST /v1/agent/run` нового `userId` → поднимается инстанс → `runId`; `GET .../events` стримит SSE до `run.completed`; баланс уменьшается ровно один раз (idempotency по `runId`).
- При 0 кредитов / неактивной подписке → `200 blocked` (BR-3/BR-5).
- `approval.request` → `POST .../approval` разблокирует прогон.
- `/v1/chat/*` (простой чат) продолжает работать независимо (регресс).

## Безопасность
- `API_SERVER_KEY` не появляется в логах/ответах клиенту (redaction).
- Bearer к инстансу никогда не пробрасывается клиенту; клиент видит только ретранслированные доменные события.
