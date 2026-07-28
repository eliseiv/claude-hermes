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
- **Флаг OFF:** `charged=0` → `remainder=full` на `run.completed` = постфактум [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md); биллинговые поля `agent_runs` (`cumulative_credits_spent`/`last_billed_step`) не двигаются; `usage.delta` ретранслируется без биллинга; `resume` недоступен. **Строка `agent_runs` при этом создаётся** (lifecycle-запись, [ADR-066 §3](../../adr/ADR-066-agent-run-state-snapshot.md)) — регресс-проверка: при OFF `agent_runs[runId]` существует со `status` жизненного цикла.

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

## Снапшот состояния прогона + `GET /v1/agent/runs/{runId}/state` ([ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md))

### Unit — writer и маппинг
- **Троттлинг `message.delta`:** серия дельт внутри `AGENT_STATE_FLUSH_INTERVAL_SECONDS` → **один** апсерт; терминальные события и `approval.request` флашатся немедленно (минуя троттлинг).
- **`tool.started`/`tool.completed`** → `last_tool` обновлён, `pending_approval` очищен. **`approval.request`** → `pending_approval={tool,preview}`; `POST …/approval` (2xx) → очищен.
- **Replay-guard:** повторная подписка, где Hermes реплеит буфер с начала (короткий префикс), **не укорачивает** `result_text`; токены не убывают (`GREATEST`).
- **Truncation `preview`:** `pending_approval.preview` длиннее `AGENT_STATE_RESULT_TEXT_MAX_CHARS` обрезается тем же лимитом head-preserving (общий knob с `result_text`).
- **`clear_pending_approval` owner-scoped:** снятие approval чужим `user_id` → `rowcount=0`, `pendingApproval` не снят.
- **`assert_pending_approval` — троттлинговый флаш не воскрешает approval:** `approval.request` → `pending_approval` выставлен → `POST …/approval` (2xx) снимает его → следующий периодический флаш `message.delta` (идёт с `assert_pending_approval=false`) **не** возвращает старое значение; `/state` остаётся `running`, не откатывается в `waiting_approval`. Зеркальный кейс: апсерты с `assert=true` (`approval.request`/`tool.*`/терминальные) значение действительно меняют.
- **Терминальный статус пишется ДО биллинга и независимо от его исхода:** при `run.completed`, где `consume`/финализация поднимает ошибку (нехватка баланса, конфликт idempotency, сбой кошелька), `agent_runs.status` всё равно `completed` — «вечного `running`» не остаётся; `/state` отдаёт `completed`, retention-sweep такой прогон видит.
- **`mark_stopped` owner-scoped:** вызов с чужим `user_id` → `rowcount=0`, статус прогона не меняется (RBAC — свойство самого `UPDATE`, а не только предшествующей проверки).
- **Префиксный guard `result_text` (MAJOR):** входящий текст **длиннее** сохранённого, но **не продолжает** его (общего начала нет — сценарий no-replay, [Q-066-1](../../99-open-questions.md)) → `result_text` **не заменён**, снапшот замер на прежнем полном тексте. Парный позитив: входящий длиннее **и** продолжает сохранённый → заменён. Тест обязан падать на реализации с одной лишь проверкой длины.
- **Tenancy-гвард (MAJOR, [Q-066-2](../../99-open-questions.md)):** апсерт при конфликте `run_id` с **чужим** `user_id` → **0 строк** (чужой снапшот не перезаписан, `WHERE t.user_id = EXCLUDED.user_id`); `snapshots_repo.get(run_id, чужой user_id)` → `None`. Симметрично для `clear_pending_approval`/`mark_stopped` (уже покрыты выше).
- **Replay-guard per-column (MAJOR, регресс на row-level `WHERE`):** **внутри replay-окна** (входящий `result_text` короче сохранённого) апсерт с `approval.request` всё равно выставляет `pending_approval` → `/state` отдаёт `waiting_approval`; в том же кейсе обновляются `last_tool` и `updated_at`, а `result_text` **не укорачивается**. Тест обязан падать на реализации с `ON CONFLICT … DO UPDATE … WHERE` (гейт строки целиком).
- **Truncation:** текст длиннее `AGENT_STATE_RESULT_TEXT_MAX_CHARS` обрезан **с сохранением начала** (граничные значения: ровно потолок / потолок+1).
- **Статусы:** `run.failed` → `_mark_terminal(runId,'failed')`; `mark_stopped(run_id, user_id)` → `cancelled` **только** из `running`/`resumed` и **только** для владельца (чужой `user_id` → `rowcount=0`) (регресс: `completed`/`paused` не затираются); строки создаются при выключенном флаге биллинга.
- **Терминальные события после `stop` не затирают `cancelled` (MAJOR):** прогон помечен `cancelled` → долетевшие `run.completed`/`run.failed` из ещё открытого relay **не** меняют статус (условие `WHERE status IN ('running','resumed')`); `/state` продолжает отдавать `stopped`.
- **Pause-at-zero не проходит через `cancelled` (MAJOR):** при исчерпании баланса внутренний Hermes-interrupt ([ADR-064 §3](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) **не** вызывает `mark_stopped(...)` — наблюдаемая последовательность статусов `running → paused` без промежуточного `cancelled`; сразу после `run.paused` `/state` отдаёт `paused` (+`blockReason`), а `POST /resume` — **не** `409 run_not_resumable`.
- **Чистая функция маппинга** server→client — все семь исходов, включая `running+pending_approval → waiting_approval` и `cancelled → stopped`.

### Integration (`tests/integration/test_agent_run_state_api.py`, Postgres testcontainers; Hermes НЕ нужен)
- **401-матрица** — новый путь добавлен в общий auth-parametrize (нет `X-API-Key` / нет / невалидный `X-User-Id`).
- **404** — несуществующий `runId`; прогон другого пользователя (RBAC, **не 403**).
- **200 для каждого маппинга** — `running`, `waiting_approval`, `paused` (+`blockReason`), `completed`, `failed`, `stopped`.
- **200 без снапшота** — строка `agent_runs` есть, `agent_run_snapshots` нет → дефолты (`resultText:""`, `usage:{0,0}`, `updatedAt` из `agent_runs`).
- **Read-only инварианты (MAJOR):** `ensure_running` **не вызван**, HTTP к Hermes отсутствует, `wallets.balance`/`ledger_transactions` не изменились после запроса.
- **Retention:** терминальный прогон старше `AGENT_RUN_SNAPSHOT_TTL_DAYS` → `resultText:""`/`pendingApproval:null`, но `200` со статусом и `usage`; активный прогон того же возраста не затронут.
- **Retention идемпотентен (MAJOR):** повторный sweep по уже очищенному прогону → **`rowcount=0`** (guard `AND (result_text <> '' OR pending_approval IS NOT NULL)`), `updatedAt` **не двигается** ни на первом, ни на повторном проходе (чистка контента не является обновлением состояния). Тест обязан падать на реализации без guard'а и на реализации, трогающей `updated_at`.
- **OpenAPI-поверхность** ([ADR-059](../../adr/ADR-059-hermes-only-openapi-surface.md)) — `tests/integration/test_api_documentation.py` обновлён на новый route (тег `Agent`, security `clientApiKey`+`userId`, коды `200/401/404/429`).

### Миграция `0019` (real DB, testcontainers)
- `upgrade` + `downgrade` на реальной БД; `alembic heads` == 1 (цепочка `0018`→`0019`); таблица + 2 индекса (`ix_agent_run_snapshots_user` + **частичный** `ix_agent_run_snapshots_sweep` с предикатом `result_text <> '' OR pending_approval IS NOT NULL`; полного индекса по `updated_at` **нет** — он был бы write-amplification на самой горячей записи) + CHECK'и создаются/удаляются; enum `agent_run_status` **не изменён**; FK CASCADE от `agent_runs` и `users` работают.

### E2E (ручной, `docker-compose.e2e.hermes.yml`)
- Запустить прогон → убить SSE-потребителя → `GET /state` (статус/текст на момент обрыва, `updatedAt` не растёт) → переподключиться к `/events` → `GET /state` снова (текст догнал).

#### Замер семантики replay ([Q-066-1](../../99-open-questions.md)) — процедура, наблюдаемая на `LOG_LEVEL=INFO`
DEBUG-маркер `result_text frozen` (`service.py`) на проде **не виден** (там `INFO`), поэтому вывод делается по наблюдаемому контракту, а не по логам: запустить длинный прогон → замерить `char_length(resultText)` через `GET …/state` → оборвать SSE-потребителя → подписаться на `/events` заново → замерять повторно.

⚠️ **`updatedAt` — НЕ дискриминатор семантики, а проверка валидности замера (liveness):** `updated_at = EXCLUDED.updated_at` пишется **безусловно**, вне `CASE` для `result_text` ([ADR-066 §6](../../adr/ADR-066-agent-run-state-snapshot.md)), поэтому движется при **обеих** семантиках — отвергается только текст. Три исхода:

| `updatedAt` | `resultText` | Вывод |
|---|---|---|
| движется | растёт | **replay-с-начала** |
| движется | замер | **no-replay** — guard отвергает фрагмент без общего начала (искомый вывод) |
| **не** движется | — | **замер невалиден**: вторая подписка ничего не пишет (проверить, что `/events` реально открылся — напр. молчаливый `502` wake-gap, [ADR-062](../../adr/ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)). Выводов о семантике **не делать**, повторить замер |

Дополнительный INFO-наблюдаемый сигнал живости второй подписки — `lastTool` (обновляется безусловно на `tool.*`). WARNING `snapshot upsert skipped (tenancy)` ([Q-066-2](../../99-open-questions.md)) виден на `INFO` без изменений уровня. Альтернатива — временно поднять `LOG_LEVEL=DEBUG` на `api` (только если нужен именно маркер).

## Безопасность
- `API_SERVER_KEY` не появляется в логах/ответах клиенту (redaction).
- `result_text`/`pending_approval` (user-content, [ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md)) не попадают в логи и audit-события; `/state` чужого прогона недоступен (404).
- Bearer к инстансу никогда не пробрасывается клиенту; клиент видит только ретранслированные доменные события.
