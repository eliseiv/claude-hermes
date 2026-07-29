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
- Запустить прогон → убить SSE-потребителя → `GET /state` (статус/текст на момент обрыва) → переподключиться к `/events` → `GET /state` снова. **Ожидаемый результат ОПРЕДЕЛЁН перемером 2026-07-30:** до [ADR-067](../../adr/ADR-067-agent-run-background-consumer.md) переподписка не даёт **ничего** (поток одноразовый), текст не догоняет — это и есть корректное поведение сценария, а не дефект. **После [ADR-067](../../adr/ADR-067-agent-run-background-consumer.md)** ожидание меняется по сути: снапшот двигает consumer, поэтому `resultText` растёт **и во время обрыва**, а переподключение к `/events` даёт реплей из нашего ring (upstream при этом не трогается вовсе).

#### Семантика потока событий Hermes ([Q-066-1](../../99-open-questions.md)) — **ЗАКРЫТО перемером 2026-07-30**

**Результат (devops, после фикса парсинга, на ЗАВЕДОМО ЖИВОМ прогоне; прогоны `run_b170a402…`, `run_cfda1890…`; воспроизводится устойчиво):**

> **Поток одноразовый (single-consumption).** Историю прогона получает **первый** подписчик. **Любая повторная подписка не получает НИЧЕГО — ни реплея, ни новых событий продолжающегося прогона.** `updatedAt` при второй подписке не двигается, то есть событий не приходит вовсе, а не «приходят, но не пишутся».

| Гипотеза до перемера | Исход |
|---|---|
| `from_start` (реплей с начала при каждой подписке) | **опровергнута** |
| `new_only` (только новые события) | **опровергнута** |
| `replay-once-then-drain` (H1: реплей первому, дальше — только новые второму) | **опровергнута — реальность строже:** второй подписке не идут и новые |
| H4 (первый подписчик получает историю) | ⚠️ **НЕ упражнялась** — момент открытия подписки A относительно первых событий в артефактах не зафиксирован; если A открывалась сразу после `202`, она получила живой поток, а не историю. Проверяется отдельной пробой: подписка **заведомо позже** первых событий (выждать ≥ 10 с после `202`) |

⚠️ **Два дефекта прежней процедуры, из-за которых первый замер не считался** (устранены в этом перемере): (1) **конфаундер живости** — прогон мог завершиться до второй подписки, и «0 байт» объяснялось бы тривиально; теперь мерили на заведомо активном прогоне; (2) **негодный дискриминатор** `char_length(resultText)` — из-за дефекта парсинга `message.delta` текст был тождественно пуст, и обе ветки исходов давали «замер»; перемер выполнен **после** фикса.

**Методический вывод (переносим на будущие замеры):** дискриминатор не должен зависеть от артефакта, корректность которого сама под вопросом, а «отсутствие данных» нельзя интерпретировать, не доказав, что источник данных был жив.

**Что это закрывает и меняет:**
- [Q-067-1](../../99-open-questions.md) — **отрицательно**: подхват прогона (adoption) и ретрай собственной подписки **невозможны**;
- [Q-067-5](../../99-open-questions.md) — **утратил предмет** (параллельный consumer нереализуем);
- инвариант «consumer подписывается раньше клиента» ([ADR-067 §1](../../adr/ADR-067-agent-run-background-consumer.md)) — **доказанно необходим**;
- orphan-reaper — **единственный и постоянный** путь доводки; рестарт `api` под прогоном необратимо теряет биллинговый хвост → [Q-067-12](../../99-open-questions.md);
- префиксный replay-guard ([ADR-066 §6](../../adr/ADR-066-agent-run-state-snapshot.md)) — **чистый defense-in-depth**: второй потребитель не пишет ничего.

**Регресс-тест (после реализации [ADR-067](../../adr/ADR-067-agent-run-background-consumer.md), Hermes мокается):** мок отдаёт поток **только первому** подписчику, второму — пустой ответ; проверить, что (а) consumer подписывается **раньше** клиента и получает поток целиком; (б) клиентский `/events` при этом полноценно обслуживается **из broker'а**, а не из upstream; (в) при обрыве подписки consumer'а **переподключения не происходит** — прогон уходит к reaper'у.

#### Прод-капча `tests/fixtures/hermes_prod_run_adr065.sse` — провенанс

| | |
|---|---|
| **Что это** | Капча **живого прод-прогона** `run_d931839587a64e3885b4d096cf7440d0` (devops, 2026-07-29), образ [ADR-065](../../adr/ADR-065-patched-hermes-image-ghcr.md). Прогон остановлен pause-at-zero по исчерпанию баланса |
| **Правки** | **Только удаление** целых блоков `message.delta` из середины ответа (сокращение user content). Значения, ключи, экранирование и разделители **не переписаны** |
| **Состав** | 15 × `message.delta`, 1 × `usage.delta`, 1 × `run.paused` |
| **Не покрывает** | `run.completed` ([Q-067-10](../../99-open-questions.md) — **денежный путь**), `tool.started`/`tool.completed`/`approval.request` ([Q-067-11](../../99-open-questions.md)); при нулевом балансе и без инструментов эти события недостижимы |

**Правило (shared-блок v3, [06-testing-strategy.md §Фикстуры внешних интеграций](../../06-testing-strategy.md)):** фикстуры внешних интеграций снимаются **с первоисточника**, а не сочиняются вместе с парсером. Именно нарушение этого правила дало прод-дефект: фикстуры `message.delta` были написаны как `{"text": …}`, образ шлёт `delta` голой строкой, `resultText` был пуст, а сюита — зелёная.

⚠️ **Тесты, использующие ещё не снятые формы** (`run.completed`, `tool.*`, `approval.request`), проверяют **наше допущение**, а не контракт образа. Их зелёный статус приёмкой не является — см. Q-067-10/Q-067-11.

#### Приёмочный критерий фикса парсинга `message.delta` (owner: qa, ДО перемера)
Прод-капча `tests/fixtures/hermes_prod_run_adr065.sse` — готовый вход: прогон `run_d931839587a64e3885b4d096cf7440d0`, 15 дельт вида `{"delta":"Я"}`, `{"delta":" не"}`, … Проигрывание капчи через writer обязано дать **непустой** `result_text`, равный конкатенации всех `delta` **без разделителей**. Парный негатив: событие с `delta` в виде объекта (`{"text": …}`) не должно ронять relay. Дополнительно — `run.paused`, построенный на этом буфере, обязан иметь **непустые** `output`/`steps` (в капче они пусты — это отпечаток дефекта, а не контракт).

#### Форма `run.completed` ([Q-067-10](../../99-open-questions.md)) — обязательный артефакт, владелец добычи devops/qa
**Ни одна капча не содержит `run.completed`** — все прод-прогоны обрываются на `run.paused`. При `agent_incremental_billing_enabled=false` (дефолт) выведенное отсюда списание — **единственное за прогон**, поэтому промах формы = бесплатный прогон, неотличимый от легитимного нуля.

**Добыть (одно из двух):** (а) **devops** — прод-прогон на **пополненном** кошельке, доведённый до штатного завершения, с капчей потока; (б) **qa** — контрактный тест против реального образа, фиксирующий терминальное событие. Артефакт — сырая капча по правилам провенанса выше; форму внести в [05-events.md](05-events.md) и закрыть Q-067-10.

**Что проверить в капче:** носитель счётчиков (вложенный `usage` vs плоский верхний уровень), имена (`cumulative_*` vs per-step), наличие `total_tokens`, наличие `timestamp` ([Q-067-7](../../99-open-questions.md)).

**Регресс до закрытия (уже реализуем, к форме нечувствителен):** relay читает **union** носителей — кумулятивные имена во всех носителях раньше любых per-step; тест обязан ловить приоритет: блок `{"usage":{"input_tokens":12,...},"cumulative_input_tokens":6313,...}` тарифицируется по **6313**, а не по 12. Плюс: нераспознанный носитель при ненулевом блоке даёт **WARNING**, а не молчаливый ноль.

#### Формы `tool.*` / `approval.request` ([Q-067-11](../../99-open-questions.md)) — владелец добычи qa
Капча снята с прогона **без инструментов**. Нужен прогон с гарантированным вызовом инструмента и с approval-сценарием. Цена промаха — путь снапшота/UI (`lastTool` заморожен, `waiting_approval` не отдаётся), не деньги; поэтому номер отдельный от Q-067-10 и закрывается **другим** артефактом.

#### Форма ответа hydrate ([Q-067-4](../../99-open-questions.md), H3) — проверить ДО Phase 9
Вызвать `GET {base}/api/sessions/{sessionId}/messages` на реальном образе после завершённого прогона; зафиксировать JSON-схему в [05-events.md](05-events.md). **Ключевой вопрос: несёт ли ответ usage/токены.** Положительный результат меняет объём [ADR-067 §5](../../adr/ADR-067-agent-run-background-consumer.md) (reaper сможет тарифицировать точно и при флаге OFF) — поэтому проба выполняется **до** реализации, а не после.

### Фоновый consumer + broker ([ADR-067](../../adr/ADR-067-agent-run-background-consumer.md)) — что покрыть тестами

**Регресс-ядро (MAJOR, падает на сегодняшней реализации):**
- **Биллинг без единого клиентского подписчика:** прогон запущен, `/events` **никем** не открывался, мок Hermes доигрывает поток до `run.completed{usage}` → в ledger есть debit с idempotency `runId`, `agent_runs.status='completed'`, снапшот заполнен. Обе ветки флага `agent_incremental_billing_enabled`.
- **Порядок сохранён:** `_mark_terminal` вызывается **до** биллинга; при исключении из `consume` статус всё равно терминальный ([ADR-066 §3](../../adr/ADR-066-agent-run-state-snapshot.md)).
- **Клиентский `/events` ничего не пишет:** при открытом клиентском стриме дублирующих ledger-строк, апсертов снапшота и `mark_status` с этого пути нет.

**Broker (Redis — testcontainers):**
- Подписчик, подключившийся в середине прогона, получает **реплей из ring, затем live**, без дублей (дедуп по `seq`) и без пропусков на границе (`SUBSCRIBE` — **до** `LRANGE`).
- **`seq` берётся из Redis `INCR`:** после «перехвата» прогона другим воркером нумерация продолжается, а не начинается с нуля.
- **`EXPIRE` продлевается на каждом событии:** прогон длиннее `AGENT_RUN_EVENT_BUFFER_TTL_SECONDS` — ring **жив**, поздний подписчик получает реплей.
- **Двойной потолок ring:** обрезка срабатывает и по числу событий, и по `AGENT_RUN_EVENT_BUFFER_MAX_BYTES` (крупные события).
- **Backpressure:** переполнение `AGENT_RUN_SUBSCRIBER_QUEUE_MAX` отключает **подписчика**; consumer продолжает, биллинг и снапшот доходят до конца.
- **Правила закрытия downstream ([ADR-067 §3.3](../../adr/ADR-067-agent-run-background-consumer.md)) — по одному тесту на условие:** терминальное событие; терминальный `agent_runs.status` при открытии; нет lease + пустой ring; статус стал терминальным во время стрима; idle-timeout без lease. **Ни в одном случае клиент не висит бесконечно.**
- **Best-effort ring:** недоступность Redis при записи не роняет consumer — биллинг и снапшот доходят.

**Владение и живучесть:**
- **Lease:** два воркера на один `runId` → upstream-подписку держит один; при принудительном истечении lease и появлении второго consumer'а **двойного списания нет**.
- **Heartbeat в `agent_run_snapshots.consumer_heartbeat_at`** идёт даже когда событий нет (длинный tool-call) и при флаге OFF; строка снапшота создаётся **при старте** consumer'а, а не лениво.
- **Потолок длительности** (`AGENT_RUN_MAX_DURATION_SECONDS`): consumer самозавершается, снимает lease и heartbeat, пишет audit и **не выставляет терминальный статус сам**; прогон затем добивает reaper.
- **Обрыв своей upstream-подписки:** §6.1 без переподключения; **ретрая нет** ([ADR-067 §6.2](../../adr/ADR-067-agent-run-background-consumer.md)).

**Orphan-reaper:**
- Кандидат = нет lease **и** протух heartbeat (`COALESCE(snapshot.consumer_heartbeat_at, agent_runs.created_at)`) **и** `INFO server → uptime_in_seconds >= AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS` → best-effort `consume` (idempotency `runId`) + `status='failed'` + audit; **повторный тик идемпотентен**.
- **Fail-closed по `INFO`:** ошибка/недоступность `INFO server` → свип **не выполняется** (ни одной финализации).
- **Fail-fast конфигурации:** `AGENT_RUN_MAX_DURATION_SECONDS <= 0` → приложение **не стартует** (валидация в `config.py`). Значение бесшумно снимает единственную гарантию верхней границы, поэтому падение на старте — требуемое поведение, а не грубость.
- **Перезапуск Redis не вызывает массовой финализации:** все lease исчезли, но живые consumer'ы обновляют heartbeat → кандидатов нет. Тест обязан падать на реализации, где условие только «нет lease».
- **`AGENT_RUN_ORPHAN_MAX_PER_TICK`** ограничивает число финализаций за тик.
- **Недоступность Redis** → свип не выполняется вовсе.

**Курсор реконнекта, поколение ring'а и усечение ([ADR-067 §3.2/§3.4](../../adr/ADR-067-agent-run-background-consumer.md)):**
- Реконнект **с** `Last-Event-ID: <epoch>-<seq>` → приходят только события с `seq >` курсора **того же поколения**, дублей нет.
- **Смена поколения (CRITICAL-регресс):** прогон идёт, клиент держит курсор → **`FLUSHDB` / перезапуск Redis** → consumer жив, lease восстановлен, `seq` начинает с 1 → клиент переподключается со **старым** курсором. Ожидание: `epoch` не совпал → курсор трактуется как пустой → клиент получает **`run.truncated` + полный реплей нового поколения + live**. ⚠️ **Тест обязан падать на реализации с голым `seq`**, где клиент получил бы навсегда открытый молчащий стрим (события с `seq` 1,2,3… отбрасывались бы как `<= 500`, а правила закрытия §3.3 не сработали бы из-за живого lease).
- **Смена поколения ПОД ОТКРЫТЫМ соединением (CRITICAL-регресс, отдельно от реконнекта):** клиент **уже подключён и получает поток** → `FLUSHDB` / перезапуск Redis → pub/sub broker'а переустанавливается, consumer **жив** (lease восстановлен) и публикует с новым `epoch`, `seq` 1,2,3… Ожидание: broker обнаруживает несовпадение `epoch` **на самом событии**, сбрасывает внутренний курсор в `0`, отдаёт **`run.truncated`** и продолжает поток нового поколения. ⚠️ **Тест обязан падать на реализации, где `epoch` сверяется только при открытии стрима:** там все живые события отбрасывались бы как `seq <= 500`, правила закрытия §3.3 не сработали бы (lease живой), и клиент получил бы **молчащий стрим до конца прогона**. Проверить все три точки сверки: на событии, при восстановлении pub/sub, в периодической сверке §3.3 п. 4.
- **`epoch` присутствует в элементах ring'а и в сообщениях канала** (а не только в отдельном ключе) — иначе несовпадение не видно на событии.
- **`epoch` создаётся при старте consumer'а вместе с lease**, а не первым событием: у прогона, ещё не эмитившего событий, ключ **есть** (иначе «ключа нет» смешивало бы «ещё не эмитил» и «поколение потеряно»).
- **Курсор «из будущего»** (`seq` больше текущего максимума) → трактуется как пустой, тот же путь.
- **Пустой курсор ≡ 0:** первое подключение к **уже обрезанному** ring'у → `run.truncated` эмитится (правило «первый `seq` > 1»). ⚠️ Парный негатив: при полном ring'е с первого события маркера нет.
- **Приоритет источников:** пришли и `Last-Event-ID`, и `?afterSeq=` → используется `Last-Event-ID`.
- **Валидация `?afterSeq=`:** не целое / отрицательное → **`400`**; невалидный `Last-Event-ID` → **не** `400`, а пустой курсор.
- Каждое событие несёт SSE-поле `id:`; клиент, игнорирующий его, работает как раньше.

**Живость выводится из прогресса, а не из расписания ([ADR-067 §6.1](../../adr/ADR-067-agent-run-background-consumer.md)):**
- **Молчащий, но живой upstream:** нет доменных событий 10–15 минут (длинный tool-call) → consumer **НЕ** самозавершается, heartbeat идёт (`state = awaiting_upstream`), lease держится, reaper прогон не трогает, клиентский стрим **не закрывается**. ⚠️ Регресс отозванного idle-таймаута — тест обязан падать на реализации с порогом по молчанию доменных событий.
- **Зависание НАШЕЙ обработки (CRITICAL-регресс):** рабочая задача входит в `processing` и не выходит (мок блокирующей записи в БД) дольше `AGENT_RUN_PROCESSING_STALL_SECONDS` при **живом сокете и живом пире** → супервизор **прекращает heartbeat, снимает lease и отменяет рабочую задачу**; прогон становится кандидатом reaper'а. ⚠️ Тест обязан падать на реализации, где heartbeat/lease — независимые от прогресса периодические задачи.
- **Beacon выставляется НА ПЕРЕХОДЕ, а не после итерации (регресс MAJOR):** сценарий «итерация завершилась → пришло событие → обработчик завис». Если `processing` выставляется **перед** входом в обработку — зависание детектируется за `STALL`; если после итерации — beacon остался бы в `awaiting_upstream`, что означает «живы» безусловно, и зависание **не было бы замечено никогда**. Тест обязан ловить именно эту разницу.
- **Прогресс, а не оборот цикла:** цикл быстро крутится (состояния меняются), но `bytes_read` и `last_published_seq` **не растут** дольше `STALL` → зависание. Состояние `connecting` лимитом **не свободно** (в отличие от `awaiting_upstream`).
- **`MAX_DURATION` применяется супервизором через отмену** рабочей задачи, а не внутренним таймером: сценарий «рабочая задача зависла + истёк потолок» обязан завершиться, а не зависнуть.
- **Смерть супервизора отменяет рабочую задачу (MAJOR-регресс, `TaskGroup`):** супервизор снят принудительно → **рабочая задача тоже отменена**; прогон честно осиротел. ⚠️ **Тест обязан падать на реализации, где рабочая задача продолжает работу:** там reaper через `ORPHAN_TIMEOUT` пометил бы **работающий** прогон `failed` и списал по неполному кумулятиву с `idempotency_key=runId`, после чего штатная финализация с **тем же ключом** была бы отброшена как дубль (недобор окончателен и молчалив), а `_mark_terminal('completed')` стал бы no-op по условному переходу — прогон **навсегда `failed`**. Парная проверка: отмена рабочей задачи отменяет супервизор.
- **Мёртвый пир:** закрытие сокета → ошибка чтения → §6.4 (флаш, снятие lease и heartbeat, audit `agent_run_consumer_disconnected`), **без** переподключения и **без** терминального статуса.
- **Отказ на connect-фазе → connect-only retry** ([ADR-067 §6.4.1](../../adr/ADR-067-agent-run-background-consumer.md) случай 2, **под флагом `AGENT_RUN_CONSUMER_CONNECT_RETRY_ENABLED`, дефолт `false`**): при `true` — `ConnectError`/`ConnectTimeout`/`PoolTimeout` при установке подписки → повторная попытка (та же политика, что у `_launch_run`), успешная вторая попытка даёт **полноценный** прогон с биллингом; при `false` (дефолт до замера [Q-067-13](../../99-open-questions.md)) — сразу §6.4, как сегодня. ⚠️ Отдельный тест на **инертную установленную подписку**: заголовки получены, событий нет никогда → прогон висит до `MAX_DURATION` (а не до `ORPHAN_TIMEOUT`), инстанс не гибернируется — это и есть цена ложной H10, зафиксировать как известное поведение. ⚠️ Парный негатив: ошибка **после получения заголовков** (`ReadError`/`ReadTimeout`/`RemoteProtocolError`) и любой non-2xx → **ретрая нет**, сразу §6.4. Тест обязан ловить обе границы — запрет, распространённый на connect-фазу, стоил бы навсегда бесплатного прогона.
- **Исчерпание connect-ретраев:** `POST /v1/agent/run` всё равно отдаёт `202`, audit `agent_run_consumer_failed`, прогон подхватывает reaper.
- **Шаг 2 §6.4 выполняется даже при неуспехе шага 1:** финальный флаш снапшота упал/завис → lease и heartbeat всё равно сняты.

**Heartbeat не протекает в клиентский контракт:**
- **Heartbeat-only запись при длительном молчании** идёт отдельным `UPDATE … SET consumer_heartbeat_at = now()` и **НЕ двигает** ни `agent_run_snapshots.updated_at`, ни `agent_runs.updated_at`. ⚠️ Тест обязан падать на реализации, где heartbeat выполняется обычным апсертом снапшота (тот пишет `updated_at` безусловно → утечка в `/state.updatedAt`, обесценивающая миграцию `0020`).
- `GET …/state` при отсутствующей строке снапшота отдаёт `updatedAt`, **не зависящий** от heartbeat'а. ⚠️ Тест обязан падать на реализации, где heartbeat пишется в `agent_runs.updated_at`.
- **Строка снапшота создаётся при старте consumer'а** (ради heartbeat'а), поэтому ветка «снапшота нет» при живом consumer'е недостижима — проверяется отдельно для `AGENT_RUN_CONSUMER_ENABLED=false` и для прогона, у которого consumer не встал.

**Миграция `0020` (real DB, testcontainers):** `upgrade`+`downgrade`; `alembic heads` == 1 (цепочка `0019`→`0020`); колонка `consumer_heartbeat_at` и частичный индекс **`ix_agent_runs_active`** на **`agent_runs`** (`(created_at) WHERE status IN ('running','resumed')`) создаются/удаляются. ⚠️ Индекса по `consumer_heartbeat_at` быть **не должно** — состав индексов `agent_run_snapshots` остаётся **два** ([ADR-066 §7](../../adr/ADR-066-agent-run-state-snapshot.md)).

**Kill-switch и гибернация:**
- При `AGENT_RUN_CONSUMER_ENABLED=false` поведение совпадает с легаси ([TD-038](../../100-known-tech-debt.md)).
- Consumer обновляет `hermes_instances.last_active_at`; `stop_idle` не гасит инстанс с активным прогоном.

**E2E (ручной, прод-подобный):** запустить прогон → **не открывать** `/events` вовсе → дождаться завершения → `GET …/state` показывает терминальный статус и непустой `resultText`, `GET /v1/wallet` показывает списание, `trialRemaining` уменьшился. Прямой регресс измеренного инцидента.

**E2E клиентского контракта ([ADR-067 §3.4](../../adr/ADR-067-agent-run-background-consumer.md)):** два последовательных подключения к `/events` одного прогона → второе получает **полный реплей с начала**. Зафиксировать явно: клиент, склеивающий `message.delta` поверх соединений, удвоит текст — согласовать с iOS до выкатки.

## Безопасность
- `API_SERVER_KEY` не появляется в логах/ответах клиенту (redaction).
- `result_text`/`pending_approval` (user-content, [ADR-066](../../adr/ADR-066-agent-run-state-snapshot.md)) не попадают в логи и audit-события; `/state` чужого прогона недоступен (404).
- Bearer к инстансу никогда не пробрасывается клиенту; клиент видит только ретранслированные доменные события.
