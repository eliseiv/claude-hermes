# ADR-064 — Пошаговый биллинг agent-run, остановка при нулевом балансе (pause), возобновление (resume)

- Статус: Accepted
- Дата: 2026-07-15
- Связан с: [ADR-045](ADR-045-hermes-as-agent-proxy.md) (**расширяет** SSE-контракт: `usage.delta` + `run.paused`; зависимость от Hermes-репо), [ADR-047](ADR-047-usage-based-billing-for-agent.md) (**расширяет / частично-ревизует** §2/§4 — списание становится incremental, `run.completed` → финализация остатка, idempotency `runId`→`runId:step`), [ADR-051](ADR-051-agent-debt-reconciliation.md) (**сужает область**: debt на incremental-пути не копится, остаётся race/legacy-fallback), [ADR-063](ADR-063-client-facing-debt-and-net-balance.md) (paused не создаёт долг → `netBalance==balance`), [ADR-046](ADR-046-per-user-hermes-runtime.md) (per-user runtime, `ensure_running`), [ADR-005](ADR-005-idempotency-ledger.md) (idempotency-ledger), [ADR-002](ADR-002-access-policy-state-machine.md) (policy-gate), [ADR-004](ADR-004-blocked-http-200.md) (blocked HTTP 200), [ADR-044](ADR-044-client-api-key-auth.md) (клиентская auth), [03-data-model.md](../03-data-model.md), [modules/agent-proxy/](../modules/agent-proxy/README.md), [modules/wallet-ledger/](../modules/wallet-ledger/README.md)
- Контракт данных: новая таблица `agent_runs` + enum `agent_run_status` (миграция `0018`, цепочка `0017`→`0018`, single head)
- Контракт API: новый `POST /v1/agent/runs/{runId}/resume`; аддитивное поле `AgentRunResponse.continuedFrom`; новые SSE-события `usage.delta` (Hermes) и `run.paused` (control plane)
- Внешний контракт Hermes: событие `usage.delta` (патч образа) + `GET /api/sessions/{sessionId}/messages` (hydrate) — см. §7
- **Реализация зависимости образа (2026-07-15 → [ADR-065](ADR-065-patched-hermes-image-ghcr.md)):** «патч образа Hermes» (§7) поставляется как **наш патченый образ `ghcr.io/eliseiv/hermes-agent@sha256:<digest>`** (digest-pin), собираемый off-server и pull'имый на `.156` ([ADR-065](ADR-065-patched-hermes-image-ghcr.md)). Патч верифицирован по коду: `agent/conversation_loop.py:2061` (эмиссия `usage.delta`) + `gateway/platforms/api_server.py:4485` (проброс в SSE). Флаг `agent_incremental_billing_enabled` включается **только после** образа на `.156` + зелёного e2e ([ADR-065 §6](ADR-065-patched-hermes-image-ghcr.md)).
- **Расширение (2026-07-28 → [ADR-066](ADR-066-agent-run-state-snapshot.md), развязка `agent_runs` от флага):** §6 читается так, что строка `agent_runs` создаётся **только** под флагом `agent_incremental_billing_enabled` — **устарело**. [ADR-066 §3](ADR-066-agent-run-state-snapshot.md) делает `create_running` и запись терминального статуса безусловными (узкий `_mark_completed` заменён единым `_mark_terminal(run_id, status)`, вызываемым в обработчике `run.completed`/`run.failed` **до** биллинга и независимо от его исхода — иначе отказ списания оставлял бы прогон вечно `running`) (`agent_runs` — lifecycle-запись, пишется всегда, в т.ч. при флаге OFF); под флагом остаётся **только биллинг** (`record_step`, per-step debits, pause-at-zero, финализация остатка). Дополнительно начинают выставляться статусы `failed` (на `run.failed`) и `cancelled` (условный owner-scoped `UPDATE` в `mark_stopped(run_id, user_id)` на 2xx клиентского `POST /stop`) — до сих пор они были в enum, но не записывались. Тарификация §1–§2, pause §3, no-debt consume §4 и CAS-resume §5 — **без изменений**. Тело ADR не переписано.

## Context

[ADR-047](ADR-047-usage-based-billing-for-agent.md) зафиксировал биллинг агентного прогона **постфактум**: на терминальном `run.completed` control plane извлекает `usage:{input_tokens,output_tokens,total_tokens}` и списывает `amount = usage_to_credits(final)` (idempotency по `runId`). Если итоговая стоимость превысила баланс — пользователь **уходит в долг** ([ADR-051](ADR-051-agent-debt-reconciliation.md), `wallets.debt`): работа уже выполнена upstream, откатить нельзя, недобор реконсилируется clawback'ом при пополнении + policy-блок `debt_outstanding`.

Требование пользователя — **заменить долг на управляемую остановку**: списывать по мере tool-loop, при исчерпании баланса **останавливать** прогон с возвратом промежуточного результата (без долга), а после пополнения — **возобновлять** с сохранением контекста сессии.

Реализуемость (разведка кода Hermes `D:\BA\hermes` + claude-hermes, факты верифицированы):
- **Остановка при 0** — Hermes умеет: `POST /v1/runs/{id}/stop` → interrupt, проверяется в начале каждой итерации loop (уже проксируется, [agent-proxy/02-api-contracts.md](../modules/agent-proxy/02-api-contracts.md)). ✅
- **Пошаговый usage** — Hermes сейчас отдаёт `usage` **только** в `run.completed`; per-step usage существует внутри loop (`agent/conversation_loop.py`, аккумуляция session-счётчиков ~строка 2059), но наружу в SSE **не эмитится**. Требует **патча образа Hermes** (пользователь одобрил): новое событие `usage.delta` (§7). ⚠️
- **Возобновление** — Hermes **не** имеет native in-place resume. Путь — **continuation**: control plane поднимает **новый** прогон в **той же** Hermes-сессии (память/транскрипт целы), догружая историю через `conversation_history`. Пользователь выбрал continuation. ⚠️

Ключевые инварианты, которые НЕ должны сломаться:
- **Сверка** `balance == Σ(credit) − Σ(debit)` ([03-data-model.md](../03-data-model.md), [ADR-047 §6](ADR-047-usage-based-billing-for-agent.md)).
- **Точная сумма**: суммарное списание за завершённый прогон обязано **точно совпасть** с прежней постфактум-суммой `usage_to_credits(final)` — incremental не должен инфлировать цену.
- **Commit-точка потоковой персистенции** ([ADR-047 §6](ADR-047-usage-based-billing-for-agent.md)): биллинг выполняется **внутри** тела `StreamingResponse`-генератора, ПОСЛЕ teardown request-сессии (`session_scope` уже сделал commit) — поэтому каждый per-step debit **обязан** сам коммитить свою сессию (как это уже делает `_bill_completed`, `agent_proxy/service.py:340/358`).

## Decision

Вводится **пошаговый (incremental) биллинг** агентного прогона под фиче-флагом `agent_incremental_billing_enabled` (OFF = текущее постфактум-поведение [ADR-047](ADR-047-usage-based-billing-for-agent.md), безопасный rollout), **остановка при нулевом балансе** (`run.paused`, без долга) и **возобновление** (`resume` через continuation + новая таблица `agent_runs`).

### 1. Тарификация «cumulative-owed минус charged» (телескопическая, точная)

Инвариант точности достигается тем, что **charged трекается в КРЕДИТАХ** (не в токенах) и списание идёт от **кумулятивного** usage, а НЕ пошаговым `ceil` дельт.

На каждом `usage.delta` (§7 — несёт кумулятивные `cumulative_input_tokens`/`cumulative_output_tokens`):
```
owed_now = usage_to_credits(cumulative_input_tokens, cumulative_output_tokens)   # billing.py, ADR-047 §2
want     = owed_now − charged                                                     # charged — кредиты, списанные за run
if want > 0:
    charge   = min(want, balance)          # no-debt self-clamp (§4); charge ≥ 0
    if charge > 0:                         # MINOR-fix: consume(amount<=0) бросает ConflictError → пропускаем debit
        _bill_step(charge, key=f"{run_id}:{step_index}")   # consume(incremental) + commit каждый шаг
        charged += charge
    depleted = charge < want               # баланса не хватило на полный want
```
- **`charge == 0` не выполняет debit** (concurrent chat-debit обнулил `balance` → `charge = min(want,0) = 0`): `consume(amount<=0)` поднял бы `ConflictError` (`wallet/service.py:195`), поэтому debit пропускается явным `if charge > 0`. При этом `depleted = (charge < want) = True` → штатная пауза (§3).
- `usage_to_credits` — существующая чистая функция (`src/app/agent_proxy/billing.py:16`, `ceil(in/1000·k_in + out/1000·k_out)`, мин. 1 при ненулевом usage). Переиспользуется **как есть**.
- **Телескопирование:** т.к. `owed_now` считается от кумулятива, а вычитается уже списанное в кредитах, сумма всех per-step `charge` при полном прогоне сходится **ровно** к `usage_to_credits(final_cumulative)` — то есть к прежней постфактум-сумме [ADR-047 §2](ADR-047-usage-based-billing-for-agent.md). Гарантия суммы: `Σ charge_i = owed_final − 0 = usage_to_credits(final)`.
- **Per-step `ceil` дельт ЗАПРЕЩЁН** (антипаттерн): `Σ ceil(delta_i) ≥ ceil(Σ delta_i)` — каждый шаг округляется вверх независимо → систематическая **инфляция** цены относительно постфактум. Единственная корректная схема — cumulative-owed-minus-charged.

### 2. Per-step идемпотентность и финализация остатка

- **Per-step debit:** idempotency-ключ `f"{run_id}:{step_index}"` (`step_index` = `usage.delta.step_index`, монотонный счётчик API-вызовов loop, §7). Повторная подписка/реконнект/дубль `usage.delta` того же шага → один debit (unique index `ux_ledger_idempotency`, [ADR-005](ADR-005-idempotency-ledger.md)). `meta = {source:"agent_run", runId, stepIndex, usage:{...}, model, incremental:true}`.
- **Commit каждый шаг:** после `consume` per-step вызывается `await self._session.commit()` (обязательно — потоковый контекст, teardown request-сессии уже прошёл; тот же паттерн, что `_bill_completed` `service.py:340/358`, `db.py`).
- **Финализация остатка на `run.completed`** (правка `_bill_completed`, [ADR-047 §4](ADR-047-usage-based-billing-for-agent.md)): `remainder = usage_to_credits(final) − charged`; при `remainder > 0` — один debit с idempotency-ключом `run_id` (**голый**, не `run_id:N` — отдельное пространство ключей, не конфликтует с per-step). Причины ненулевого остатка: последний шаг без отдельного `usage.delta`, расхождение финального `usage` с последним кумулятивом. При **выключенном** флаге `charged=0` → `remainder = usage_to_credits(final)` = **полное текущее поведение** [ADR-047](ADR-047-usage-based-billing-for-agent.md) (обратная совместимость by construction).
- **Финализация — по пути [ADR-047](ADR-047-usage-based-billing-for-agent.md)/[ADR-051](ADR-051-agent-debt-reconciliation.md)** (debt-capable, §4): остаток мал, обычно покрыт балансом; долг возможен только в редкой гонке с concurrent chat-debit (fallback, §4).
- **Paused-прогон финализации НЕ имеет:** для остановленного прогона `run.completed` от Hermes **не приходит** (мы его interrupt'нули) → финализация не выполняется; пользователь оплатил ровно per-step charges до точки паузы. Остаток добирается в НОВОМ прогоне после resume (§5).

### 3. Pause-at-zero: `run.paused` без долга

При `depleted` (баланса не хватило на полный `want` очередного шага):
1. `_bill_step` списывает `charge = min(want, balance)` (баланс → 0; частичный аффорданс шага, §4).
2. **Stop:** `self.stop(user_id, run_id)` → Hermes interrupt (существующий passthrough) — прогон прекращается, дальнейшие API-вызовы loop не выполняются.
3. **Синтетическое терминальное SSE `run.paused`** (генерирует **control plane**, НЕ Hermes): `data: {"event":"run.paused","run_id","reason":"credits_exhausted","status":"paused","billed":<charged>,"message":<промежуточный текст>,"steps":[...]}`. Источник промежуточного результата — **локальный буфер relay** (накопленные `message.delta` + собранные `tool.*`), self-contained, без round-trip к Hermes.
4. **`return`** из генератора (стрим закрывается **без** `run.completed`).
5. `agent_runs.status = 'paused'`, `paused_reason = 'credits_exhausted'`, `cumulative_credits_spent`/`last_billed_step` зафиксированы (§6).

**Долг НЕ создаётся.** Списание идёт `charge ≤ balance` → условный `UPDATE ... WHERE balance >= amount` всегда проходит → ветка `InsufficientCreditsError`/`wallets.debt` ([ADR-051](ADR-051-agent-debt-reconciliation.md)) **недостижима** на incremental-пути by construction. Согласовано с [ADR-063](ADR-063-client-facing-debt-and-net-balance.md): paused-прогон не двигает `wallets.debt` → `netBalance == balance` (== `creditsBalance`).

**Граница остановки — один шаг.** Защита от перерасхода имеет гранулярность **одного** LLM-вызова loop: на шаге, где `want > balance`, реально потреблённые Hermes токены этого шага уже произошли, но списывается только `min(want,balance)`, а недобор `(want − balance)` **не** конвертируется в долг — сервис поглощает его на границе stop. Максимальный непокрытый перерасход ограничен дельтой одного `usage.delta`. Это осознанный размен «нет долга ↔ поглощение ≤ одного шага» (ср. debt-модель [ADR-051](ADR-051-agent-debt-reconciliation.md), где недобор шёл в долг). Детализация частичного шага — [Q-064-2](../99-open-questions.md).

### 4. No-debt вариант consume для incremental + сохранение debt как fallback

- **Per-step (incremental) consume — ВЫДЕЛЕННАЯ третья ветка, маршрутизируемая ДО `_debit_in_savepoint`.** `WalletService.consume` разветвляется по `meta` **в начале**, до существующей savepoint-ветки (`wallet/service.py:208-232`): `if meta.get("incremental"): return await self._consume_incremental_clamp(...)`. **Это критично:** при `meta.incremental` **нельзя** проваливаться в `_debit_in_savepoint` (`wallet/service.py:127-183`) — та ветка на нехватке баланса (условный `UPDATE ... WHERE balance >= :amount`, 0 строк) **поднимает `InsufficientCreditsError`** (`wallet/service.py:180-181`), что **противоположно** требуемому self-clamp. В гонке с concurrent chat-debit это бросило бы исключение вместо клэмпа → порвало бы SSE-стрим и нарушило гарантию «нет долга». Ветвление по `meta.incremental` **не** проходит через `_agent_reconcile_applies` (та лишь отключает ADR-051 debt-ветку и всё равно ведёт в raising `_debit_in_savepoint`) — `incremental` **маршрутизирует** в отдельный метод, а НЕ является источником клэмпа через `_agent_reconcile_applies`.
- **`_consume_incremental_clamp` (новый метод, no-debt, self-clamp):** `INSERT debit (key=run_id:step, meta) ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING id`; при новой строке — `UPDATE wallets SET balance = balance − LEAST(:amount, balance), updated_at = now() WHERE user_id = :uid RETURNING balance` — **без `raise`, без `wallets.debt`**, возвращает фактически списанную (`LEAST(:amount,balance)`, гарантирует `balance ≥ 0` CHECK) величину (для трекинга `charged` + детекта depletion в гонке). На `ON CONFLICT` (реплей того же шага) — no-op, вернуть existing tx (idempotent_replay). Т.к. caller подаёт `amount = min(want, balance)` со свежего чтения, `LEAST` срабатывает только в узком окне гонки с concurrent chat-debit — тогда фактически списано меньше `amount`, но всё ещё без долга и без исключения.
- **Финализация на `run.completed` — прежний путь** (`_bill_completed` → `consume` без `incremental`-флага): debt-capable ([ADR-051](ADR-051-agent-debt-reconciliation.md)) остаётся **fallback** для (а) флага OFF (полное постфактум-списание) и (б) редкой гонки, когда `remainder > balance`. Так [ADR-051](ADR-051-agent-debt-reconciliation.md) не отменяется — сужается его область: **основной** incremental-путь долг не копит, долг живёт только на finalization/legacy-ветке.
- **Инвариант ledger** ([ADR-047 §6](ADR-047-usage-based-billing-for-agent.md)) сохранён: каждый per-step debit — обычная ledger-строка `type='debit'`, `amount>0`; сумма строк по `run_id:%` (+ финализация по `run_id`) = фактически списанные кредиты; `balance == Σ(credit) − Σ(debit)` держится.

### 5. Resume: continuation через новый прогон в той же сессии

**Стабильный `session_id` (стык компонентов).** В `run()` control plane вычисляет `effective_session_id = body.sessionId or uuid4()`, передаёт его в Hermes body (`session_id`). **При включённом `agent_incremental_billing_enabled`** `run()` создаёт **корневую** строку `agent_runs(run_id=<Hermes run_id>, user_id, session_id=effective, model, status='running', continued_from_run_id=NULL)` в начале прогона (согласовано с [03-data-model.md §24](../03-data-model.md): таблица заполняется на incremental-пути под флагом). При **выключенном** флаге `agent_runs` не пишется и resume недоступен (постфактум-режим [ADR-047](ADR-047-usage-based-billing-for-agent.md)). Один `session_id` — на всю continuation-цепочку (память + транскрипт в одной Hermes-сессии). Resume находит `session_id` по paused `run_id` — **клиенту хранить/передавать session_id не нужно**.

**`POST /v1/agent/runs/{runId}/resume`** (тело `{message?: string|null}`):
1. **Auth** ([ADR-044](ADR-044-client-api-key-auth.md)): `X-API-Key` + `X-User-Id`.
2. **RBAC:** `agent_runs[runId].user_id == subject` иначе **404** (чужой/несуществующий прогон невидим — как namespaced runId, [agent-proxy/06-rbac.md](../modules/agent-proxy/06-rbac.md)).
3. **Пред-гвард статуса (информационный):** `status ∉ {paused, resumed}` (running/completed/failed/cancelled) → **409** `run_not_resumable`. Это ранний отказ; **авторитетный** арбитр — атомарный CAS (шаг 5), а не это чтение.
4. **Policy-gate** ([ADR-002](ADR-002-access-policy-state-machine.md)/[ADR-047 §3](ADR-047-usage-based-billing-for-agent.md)): повторная `evaluate` в credits-ветке (read-only, **до** любого изменения статуса) — если баланс всё ещё `0`/долг → `200 {status:"blocked", blockReason}` ([ADR-004](ADR-004-blocked-http-200.md), тот же достижимый набор, что `run`). Resume без пополнения не флипает статус и не запускает прогон.
5. **Атомарный CAS `paused → resumed` (арбитр гонки, единственный сериализующий шаг, отдельная короткая транзакция — образец claim-before [ADR-054](ADR-054-trial-claim-reconcile.md)):**
   ```sql
   UPDATE agent_runs SET status='resumed', updated_at=now()
   WHERE run_id=:runId AND status='paused'
   RETURNING session_id, model
   ```
   - **`rowcount == 1` (выиграл CAS):** переходит к launch (шаги 6–9). **Только победитель** запускает child — конкурентные in-flight resume и сетевые ретраи `POST /resume` (пока первый обрабатывается) увидят уже `resumed` и не запустят второй child.
   - **`rowcount == 0` (проиграл / ретрай / уже возобновлён):** идемпотентный резолв — найти active child (`agent_runs` где `continued_from_run_id == runId`): **есть** → `202 {runId: child, continuedFrom: runId}`; **ещё не зафиксирован** (узкое окно между CAS победителя и его chain-insert, шаг 9) → **`409 resume_in_progress`** (клиент ретраит → получит child). **Второй child НЕ создаётся.**
6. **`ensure_running(userId)`** ([ADR-046](ADR-046-per-user-hermes-runtime.md)) — wake инстанса (readiness-gate [ADR-062](ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)). Только победитель CAS.
7. **Hydrate:** `GET {base}/api/sessions/{session_id}/messages` (внешний Hermes-контракт, §7) → `conversation_history` (explicit).
8. **Launch нового прогона — строго ПОСЛЕ выигрыша CAS** (исключает конкурентные/orphan Hermes-прогоны): `_launch_run(POST {base}/v1/runs, body={input: message?, session_id: <та же>, model: <из CAS RETURNING>, conversation_history: <hydrated>})` → `new_run_id`. Т.к. `POST /v1/runs` НЕ идемпотентен ([ADR-062](ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)), single-flight-гарантия CAS — предусловие того, что в одной `session_id` не окажется двух параллельных child-прогонов Hermes.
9. **Chain (транзакция):** `create_running(new_run_id, user_id, session_id, model, continued_from_run_id=runId, status='running')`. `runId` уже `resumed` (флипнут CAS на шаге 5). Порядок «CAS(commit) → launch → create child» гарантирует: (а) один child; (б) launch не начнётся, если CAS не выигран.
10. **Ответ `202 {runId: new_run_id, continuedFrom: runId}`.** Клиент подписывается на `GET /v1/agent/runs/{new_run_id}/events`.

**Reconcile на неуспехе после CAS.** Если после выигрыша CAS `ensure_running`/hydrate/`_launch_run` **не** создали Hermes-прогон (сбой до `POST /v1/runs` или connect-fail — run не создан) → откат `resumed → paused` (`UPDATE agent_runs SET status='paused' WHERE run_id=:runId AND status='resumed' AND NOT EXISTS(child)`, образец claim-rollback [ADR-054 §4](ADR-054-trial-claim-reconcile.md)) → прогон остаётся возобновляемым. **HTTP-исход отката различается по причине** (реализовано, сверено с [Q-064-3](../99-open-questions.md)): hydrate вернул `404`/**пустую историю** (сессия истекла, continuation невозможен детерминированно) → `409 session_expired` (клиент начинает новый прогон `POST /v1/agent/run`); транзиентная недоступность (`ensure_running`/hydrate transport или non-2xx / сбой `_launch_run`) → `502` (клиент ретраит resume). В обоих случаях CAS откачен и `run_id` снова `paused`. Остаточный редкий кейс: launch **создал** Hermes-run, но chain-insert (шаг 9) упал → orphan Hermes-run (без строки `agent_runs`) — останавливается idle-reaper hermes-runtime, реконсиляция как [Q-064-1](../99-open-questions.md).

**Свежий keyspace = нет двойного счёта.** Новый прогон биллится под ключами `f"{new_run_id}:{step}"` (+ финализация `new_run_id`) — пространство **не пересекается** с paused-прогоном (`old_run_id:%`). Seed `charged` нового прогона (§6) читает ledger по `new_run_id` — не видит списаний старого. **Повторный вход в billing/policy-оценку на continuation-шаге не блокирует и не дублирует учёт первого прогона:** per-run keyspace разводит их полностью; CAS сериализует сам запуск (ровно один child на paused-прогон); policy-gate на resume — свежая оценка после пополнения (шаг 4), не зависит от per-step списаний старого прогона.

### 6. Таблица `agent_runs` (миграция `0018`) — lifecycle + resume-цепочка

```sql
CREATE TYPE agent_run_status AS ENUM
    ('running', 'paused', 'resumed', 'completed', 'failed', 'cancelled');

CREATE TABLE agent_runs (
    run_id                   TEXT PRIMARY KEY,                    -- Hermes run id
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id               TEXT NOT NULL,                       -- стабильный ключ resume (Hermes-сессия)
    status                   agent_run_status NOT NULL DEFAULT 'running',
    cumulative_credits_spent BIGINT  NOT NULL DEFAULT 0 CHECK (cumulative_credits_spent >= 0),
    last_billed_step         INTEGER NOT NULL DEFAULT 0 CHECK (last_billed_step >= 0),
    paused_reason            TEXT,                                -- напр. 'credits_exhausted'
    continued_from_run_id    TEXT REFERENCES agent_runs(run_id) ON DELETE SET NULL,  -- self-FK: цепочка resume
    model                    TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_agent_runs_user_status    ON agent_runs (user_id, status);
CREATE INDEX ix_agent_runs_session        ON agent_runs (session_id);
CREATE INDEX ix_agent_runs_continued_from ON agent_runs (continued_from_run_id);
```
- **`run_id` TEXT PK** (Hermes-строка, не UUID). **`user_id` FK CASCADE** (изоляция; RBAC-404 на чужой). **`session_id`** стабилен на цепочку.
- **`status`** enum(6). Переходы: `running → {paused, completed, failed, cancelled}`; `paused → resumed` (при resume); `resumed` — терминальный маркер «возобновлён» (у него есть child с `continued_from_run_id = self`).
- **`continued_from_run_id`** self-FK `ON DELETE SET NULL` — цепочка continuation (child → parent). Корень цепочки — `NULL`.
- **Инвариант:** `cumulative_credits_spent == Σ ledger.amount` по debit-строкам `idempotency_key = run_id OR LIKE 'run_id:%'` для **этого** прогона. **Ledger — источник истины биллинга** ([ADR-005](ADR-005-idempotency-ledger.md)); `agent_runs.cumulative_credits_spent`/`last_billed_step` — денормализованное зеркало (admin/analytics + reconcile-якорь для [Q-064-1](../99-open-questions.md)), обновляется per-step; при расхождении зеркало↔ledger **приоритет у ledger** (пересчёт через `charged_for_run`).
- **Миграция `0018_agent_runs`** (revision id `0018_agent_runs`, ≤32 символов; `down_revision = "0017_hermes_provisioning"` — полный id, не короткий `0017`; single head): `CREATE TYPE agent_run_status` + `CREATE TABLE agent_runs` + 3 индекса + CHECK'и. `downgrade`: `DROP TABLE` + `DROP TYPE`. Применить upgrade+downgrade к **реальной** БД (не только offline).

**Seed `charged` из ledger (reconnect-safe).** В начале `stream_events` (и на resume — по новому `run_id`): `WalletService.charged_for_run(user_id, run_id) = SUM(amount)` по debit-строкам `idempotency_key = :run_id OR idempotency_key LIKE :run_id_escaped || ':%' ESCAPE '\'`, где `:run_id_escaped` — `run_id` с **экранированными LIKE-метасимволами** `%`/`_`/`\` (иначе Hermes-`run_id`, содержащий `%`/`_`, дал бы **ложные совпадения** → завышение `charged` → недобилл). `run_id` — строка Hermes (не UUID), поэтому экранирование обязательно (или структурный split-фильтр по `:`). Допустимый charset Hermes `run_id` подтвердить при патче образа (рядом с [Q-064-4](../99-open-questions.md)); при safe-charset (без `%`/`_`) `ESCAPE` — defense-in-depth. Так после обрыва SSE и повторной подписки `charged` восстанавливается из ledger — уже списанные шаги не списываются снова (idempotency per-step это гарантирует и напрямую, seed лишь исключает лишние no-op consume).

### 7. Внешний контракт Hermes (зависимость образа)

**Событие `usage.delta`** (патч образа Hermes, эмитится per LLM API-вызов внутри tool-loop):
```
data: {"event":"usage.delta","run_id":"<id>","step_index":<int>,
       "input_tokens":<delta_int>,"output_tokens":<delta_int>,
       "cumulative_input_tokens":<int>,"cumulative_output_tokens":<int>,
       "cumulative_total_tokens":<int>,"model":"<str>"}
```
- **`cumulative_*` — источник тарификации** (§1, анти-двойной-счёт); `input_tokens`/`output_tokens` (дельта) — информативны/для аудита.
- **`step_index`** — монотонный счётчик API-вызовов loop (`session_api_calls`); ключ per-step идемпотентности. Монотонность подтвердить при патче — [Q-064-4](../99-open-questions.md).
- Точки патча (ориентир, проверить при реализации патча): `agent/conversation_loop.py` (~строка 2059, внутри `if response.usage:` — вызов `tool_progress_callback("usage.delta", ...)`); `gateway/platforms/api_server.py` (~строка 4478, ветка `elif event_type == "usage.delta": _push({...})`). `tool_progress_callback`/`_push` — уже существующий thread-safe канал к SSE-очереди (`call_soon_threadsafe`).
- Обратная совместимость: старые клиенты игнорируют неизвестное событие; при **выключенном** `agent_incremental_billing_enabled` control plane ретранслирует `usage.delta` без биллинга (постфактум как прежде).

**`GET /api/sessions/{session_id}/messages`** (hydrate, §5.7) — внешний Hermes-контракт: возвращает историю сообщений сессии для формирования `conversation_history` нового прогона. Наличие/форму подтвердить при патче; `session_expired`/пустая история → [Q-064-3](../99-open-questions.md).

**`run.paused`** — генерирует **control plane** (не Hermes); в Hermes-контракт не входит (§3).

### 8. Клиентский контракт (донести iOS-разработчику)

- **`run.paused`** (reason `credits_exhausted`) — **терминальное** SSE-событие: стрим закроется **без** `run.completed`. Клиент показывает промежуточный результат (`message`/`steps`) + предлагает пополнение.
- **`usage.delta`** — новое событие; клиент может игнорировать или показывать расход в реальном времени.
- **`POST /v1/agent/runs/{runId}/resume`** — вызвать после пополнения; вернёт **НОВЫЙ** `runId` (+ `continuedFrom`), подписаться на его `/events`. `session_id` клиенту хранить не нужно.

## Consequences

**Положительные:**
- Долг заменён управляемой остановкой: пользователь платит за реально потреблённое по мере loop, при 0 — прогон останавливается с промежуточным результатом (`run.paused`), без ухода в минус.
- **Точная сумма:** телескопическая схema (§1) даёт суммарное списание, **точно равное** прежнему постфактум `usage_to_credits(final)` — цена не инфлирует.
- Continuation-resume сохраняет память/контекст (одна Hermes-сессия), клиент не хранит session_id (резолв по paused runId).
- Долг ([ADR-051](ADR-051-agent-debt-reconciliation.md)) на основном пути не копится → `netBalance==balance` ([ADR-063](ADR-063-client-facing-debt-and-net-balance.md)); долг остаётся узким fallback (finalization/race/flag-off).
- Полностью откатываемо флагом `agent_incremental_billing_enabled=false` (постфактум-поведение [ADR-047](ADR-047-usage-based-billing-for-agent.md)).

**Отрицательные / ограничения:**
- Зависимость от **патча образа Hermes** (`usage.delta`, hydrate-endpoint) — внешний контракт; без патча фича не работает (флаг OFF безопасен).
- Сервис поглощает недобор ≤ одного шага на границе stop (§3) — осознанный размен «нет долга» ([Q-064-2](../99-open-questions.md)).
- Per-step commit + seed-из-ledger повышают число мелких транзакций и запросов на прогон (vs один debit постфактум) — приемлемо (шагов в loop немного; commit амортизируется).
- Новая таблица `agent_runs` + resume-контур — рост поверхности (миграция, endpoint, RBAC, тесты).
- Reconnect с потерей `usage.delta` между `last_billed_step` и обрывом — реконсиляция отложена ([Q-064-1](../99-open-questions.md)); seed-из-ledger + per-step idempotency защищают от **двойного** счёта, но не гарантируют дозачёт «пропущенного» шага без reconcile.

## Alternatives

1. **Оставить постфактум + debt ([ADR-047](ADR-047-usage-based-billing-for-agent.md)/[ADR-051](ADR-051-agent-debt-reconciliation.md)).** Отвергнуто требованием: пользователь хочет остановку вместо долга.
2. **Pre-debit hold (резерв «на максимум»).** Отвергнуто (как в [ADR-051](ADR-051-agent-debt-reconciliation.md)): usage известен только по факту, hold искажает доступный баланс сильнее; incremental точнее.
3. **Per-step `ceil` дельт вместо cumulative-owed-minus-charged.** Отвергнуто: инфлирует цену (`Σ ceil(delta) ≥ ceil(Σ delta)`), нарушает инвариант точной суммы (§1).
4. **Native in-place resume Hermes.** Неприменимо: образ не поддерживает; continuation (новый прогон + `conversation_history` в той же сессии) — единственный доступный путь.
5. **Списание в токенах, конвертация в конце.** Отвергнуто: не даёт остановку «по кредитам» в реальном времени; кредиты — целочисленная валюта биллинга ([03-data-model.md](../03-data-model.md)).
