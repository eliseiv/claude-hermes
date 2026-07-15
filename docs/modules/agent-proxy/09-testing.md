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

## Incremental billing + pause/resume ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md), под флагом `agent_incremental_billing_enabled`)

### Unit / Integration — тарификация
- **Телескопическая сумма:** серия `usage.delta` (кумулятивы) → `Σ per-step charge == usage_to_credits(final_cumulative)` **точно** (совпадает с постфактум [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)); проверить, что per-step `ceil` дельт НЕ применяется (нет инфляции: сравнить с `Σ ceil(delta_i)`).
- **Per-step идемпотентность:** дубль/реплей `usage.delta` того же `step_index` → один debit (ключ `runId:step`, `ux_ledger_idempotency`).
- **Seed из ledger (reconnect):** обрыв SSE после N шагов + повторная подписка → `charged` восстановлен через `charged_for_run` (SUM по `runId OR runId:%`, `ESCAPE '\'`); шаги 1..N не списаны повторно. Кейс `run_id` с LIKE-метасимволами (`%`/`_`) → нет ложных совпадений (экранирование).
- **Финализация остатка:** `run.completed` → `remainder = usage_to_credits(final) − charged`, при `>0` debit idempotency `runId` (голый, не конфликтует с `runId:step`).
- **`charge == 0`** (concurrent chat-debit обнулил balance): debit пропущен (нет `ConflictError` от `consume(amount<=0)`), но `depleted=True` → пауза.
- **Флаг OFF:** `charged=0` → `remainder=full` на `run.completed` = постфактум [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md); `agent_runs` не пишется; `usage.delta` ретранслируется без биллинга; `resume` недоступен.

### Unit / Integration — pause-at-zero + no-debt
- **Stop-at-0:** при `charge < want` → списано `min(want,balance)` (balance→0), вызван `stop(runId)` (Hermes interrupt), эмитировано терминальное `run.paused` (`reason=credits_exhausted`, промежуточный JSON из relay-буфера), `agent_runs.status='paused'`, стрим закрыт БЕЗ `run.completed`.
- **Долг НЕ вырос:** `wallets.debt == 0` после паузы; `netBalance == balance` ([ADR-063](../../adr/ADR-063-client-facing-debt-and-net-balance.md)).
- **No-debt clamp routing (MAJOR):** `consume(meta.incremental=true)` маршрутизируется в `_consume_incremental_clamp` **до** `_debit_in_savepoint`; в гонке с concurrent chat-debit (balance < amount) — списано `LEAST(amount,balance)` **без** `InsufficientCreditsError` и **без** `wallets.debt` (стрим не рвётся). Регресс: `meta` без `incremental` (chat/admin/finalization) по-прежнему идёт savepoint-путём (raise на нехватке).

### Integration — resume + concurrency
- **Resume happy-path:** paused → `POST /resume` → CAS `paused→resumed`, новый прогон (та же `session_id`), `agent_runs` chain (`continued_from_run_id==old`, old→`resumed`), `202 {runId:new, continuedFrom:old}`; свежий keyspace `new:step` (списания не пересекаются с `old:%`).
- **Concurrent-resume idempotency (CRITICAL, CAS):** два параллельных `POST /resume` того же paused `runId` → **ровно один** child-прогон Hermes (CAS пропускает одного); проигравший получает `202` с тем же child ИЛИ `409 resume_in_progress` (до фиксации child); НЕТ двух child в одной `session_id`, НЕТ двойного launch/биллинга, НЕТ orphan. Последовательный повтор после завершения (`status='resumed'`) → тот же child.
- **Гварды:** `status ∉ {paused,resumed}` → `409 run_not_resumable`; чужой/несуществующий `runId` → `404` (RBAC); баланс всё ещё 0/долг → `200 blocked` (policy-gate до CAS, статус не флипнут).
- **Reconcile на сбое:** сбой до `POST /v1/runs` после выигрыша CAS → откат `resumed→paused` (прогон снова возобновляем), `502`.
- **Hydrate:** `GET {base}/api/sessions/{session_id}/messages` → `conversation_history` подан в новый прогон; недоступна сессия → `409`/`502` ([Q-064-3](../../99-open-questions.md)).

### Миграция `0018` (real DB, testcontainers)
- `upgrade` + `downgrade` на реальной БД; `alembic heads` == 1 (single head, цепочка `0017`→`0018`); enum `agent_run_status` + таблица + 3 индекса + CHECK'и создаются/удаляются.

## Безопасность
- `API_SERVER_KEY` не появляется в логах/ответах клиенту (redaction).
- Bearer к инстансу никогда не пробрасывается клиенту; клиент видит только ретранслированные доменные события.
