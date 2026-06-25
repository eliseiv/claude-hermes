# ADR-054 — Сериализация trial: claim-before-generation + reconcile-on-failure

- Статус: Accepted
- Дата: 2026-06-24
- Связан с: [ADR-002](ADR-002-access-policy-state-machine.md) (**расширяет §Trial concurrency**), [ADR-005](ADR-005-idempotency-ledger.md) (атомарный flip), [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) (max_tokens — не успешный финал), [03-data-model.md](../03-data-model.md) (`users.trial_used`), [modules/chat-orchestrator/03-architecture.md](../modules/chat-orchestrator/03-architecture.md)
- Закрывает: [TD-006](../100-known-tech-debt.md)

## Context

[ADR-002 §Trial concurrency](ADR-002-access-policy-state-machine.md) зафиксировал окно двойной бесплатной trial-генерации как осознанный риск ([TD-006](../100-known-tech-debt.md)): два параллельных первых `/v1/chat/run` (subscription=none, trial_used=false, mode=credits) оба проходят policy-allow **до** flip `trial_used` и оба генерируют бесплатный ответ.

**Почему исходное ТЗ TD-006 нереализуемо (MAJOR-4).** Прежнее митигирование («`pg_advisory_xact_lock(user_id)` в той же транзакции, что flip `trial_used`») предполагало, что генерация и flip — в одной транзакции. Но orchestrator (`orchestrator.py:962`) делает `session.commit()` **внутри** generate-loop **до** LLM-вызова. Xact-scoped advisory lock освобождается на этом commit — **до** генерации и до flip. Сериализация нулевая: второй параллельный запрос берёт lock сразу после commit первого, пока первый ещё генерирует и не флипнул trial. ТЗ несовместимо с архитектурным инвариантом (commit до LLM нужен, чтобы не держать БД-коннект на всё время генерации).

Нужен механизм, который **реально** сериализует первый trial, **не** держит lock/коннект на время генерации и **не** нарушает MAJOR-4.

## Decision

**Вариант (а) с reconcile: claim-before-generation + rollback-on-failure.** Trial «застолбляется» атомарно в **отдельной короткой транзакции** в начале trial-allow ветки `/chat/run` — **до** генерации; при неуспешной генерации claim откатывается.

### 1. Claim trial (короткая транзакция, до генерации)

На ветке trial-allow (`mode=credits`, `subscription=none`, policy-allow по trial) **до** вызова LLM:
```sql
-- отдельная короткая транзакция (НЕ generate-транзакция):
UPDATE users SET trial_used = TRUE
WHERE id = :user_id AND trial_used = FALSE
RETURNING id;
COMMIT;
```
- **`RETURNING`/affected rows = 1** → этот запрос «выиграл» trial → продолжить генерацию.
- **affected rows = 0** → trial уже занят (параллельным запросом ИЛИ прошлым ходом) → второй запрос НЕ генерирует бесплатно: повторно вычислить policy для актуального состояния (`trial_used=true`, subscription=none) → `blocked`, `blockReason=trial_used` (HTTP 200, [ADR-004](ADR-004-blocked-http-200.md)). Это и есть сериализация: атомарный условный UPDATE — единственный арбитр гонки (как `mark_trial_used`, [ADR-005](ADR-005-idempotency-ledger.md)), без advisory lock и без удержания коннекта на генерацию.
- Транзакция claim — **только** этот UPDATE+commit; коротка, конкурентные claim сериализуются на row-lock `users` и завершаются мгновенно. MAJOR-4 не нарушается (отдельная транзакция, не generate-loop).

### 2. Reconcile (откат claim при неуспешной генерации)

Чтобы сохранить семантику «trial сгорает только при УСПЕШНОЙ генерации» ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md): `max_tokens`/обрыв не флипает trial; та же логика для upstream-ошибки/исключения), claim **откатывается**, если ход НЕ завершился успешным финальным `assistant_message`:
```sql
-- отдельная короткая транзакция, в ветке неуспеха (max_tokens / upstream error / exception):
UPDATE users SET trial_used = FALSE
WHERE id = :user_id AND trial_used = TRUE;
COMMIT;
```
- Откат выполняется **только** если текущий запрос реально выполнил claim (выиграл §1) — иначе чужой trial не трогаем. Реализация: флаг `claimed_trial` в области запроса; reconcile-rollback под `try/except/finally` вокруг генерации.
- **Что считается неуспехом** (rollback claim): `stop_reason=max_tokens` ([ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), upstream-ошибка (`UpstreamError`→502), любое исключение генерации, отмена. **Успех** (claim остаётся): `status=assistant_message` (финальный успешный ход). Промежуточный `status=tool_call` (tool-loop) — **claim НЕ откатывается** (ход в процессе, не неуспех): trial остаётся занятым на время tool-round'ов и финализируется успехом или откатывается при провале continuation.
- Идемпотентность отката: условие `WHERE trial_used=TRUE` + флаг `claimed_trial` гарантируют, что повторный/частичный путь не флипнет чужой/уже-сброшенный trial.

### 2a. Continuation tool-loop под claimed trial (`/chat/tool-result`)

**Проблема (выявлена при реализации [TD-006](../100-known-tech-debt.md), §1/§2 покрывали только `/chat/run`).** Trial-ход может вернуть `status=tool_call` с **client-side** инструментами (`files.*`/`calendar.*`/`reminders.*` предлагаются модели всем, независимо от billing-mode). Тогда `/chat/run` завершился: claim выполнен (§1, `trial_used=TRUE`), но финального `assistant_message` ещё **нет** — ход продолжится через `/chat/tool-result` (continuation). На continuation (`orchestrator.tool_result`, при закрытии барьера хода) выполняется **повторная** policy-оценка `evaluate(state, mode)`. Для `state(subscription=none, trial_used=TRUE)` policy (чистая функция, [§3](#3-совместимость-с-major-4-и-существующим-flip)) детерминированно возвращает `Decision.blocked(trial_used)` — и continuation **ошибочно** блокируется `blockReason=trial_used` **своим же** claim'ом: ход рвётся посреди tool-loop, trial сгорает, ответа нет. Это **регрессия именно claim-before**: до ADR-054 flip происходил **после** полного хода, поэтому во время tool-round'ов `trial_used` оставался `FALSE` и continuation проходил policy-allow. §2 это предусматривает («trial остаётся занятым на время tool-round'ов и финализируется успехом или откатывается при провале continuation»), но механизм не был расписан, а флаг `claimed_trial` — **request-scoped** (не переносится в новый запрос `/chat/tool-result`).

**Детерминированный признак «in-progress trial-ход, застолбивший свой trial».** Решается **в orchestrator** (НЕ в engine — policy остаётся чистой функцией, [§3](#3-совместимость-с-major-4-и-существующим-flip)). Детерминизм опирается на **инвариант пути `/chat/tool-result`**, а не на форму проверки внутри метода репозитория: re-evaluate в `tool_result` достигается **только** после хода, вернувшего `status=tool_call` (создавшего assistant-шаг с `tool_use`-блоками и ≥1 client-side `tool_call`) и **только** при закрытии барьера этого хода. Поэтому существование любого assistant-шага данного `message_step_id` на этом пути **по построению** означает, что это tool_use-ход. Признак использует `ChatRepository.assistant_tool_step_id(session_id, message_step_id) is not None` как индикатор «у этого `message_step_id` уже есть assistant-шаг». **NB:** фактический метод (`src/app/chat/repository.py`) фильтрует **только** `role='assistant'` (возвращает последний assistant-шаг хода по max `seq`) и **не** проверяет наличие `tool_use`-блоков отдельно — на пути `tool_result` это эквивалентно проверке «tool_use-ход существует», т.к. сюда попадают лишь ходы, вернувшие `tool_call`. Признак **continuation-of-own-trial-turn**:

```
is_inflight_trial_turn ⟺
    mode == credits
    AND state.subscription_status == none
    AND state.trial_used == TRUE
    AND assistant_tool_step_id(session_id, message_step_id) is not None
```

Этот признак однозначно отличает **продолжение уже-застолблённого trial-хода** (claim сделан §1 на `/chat/run` того же `message_step_id`) от **нового `/chat/run`, заблокированного прошлым trial** (другой кодовый путь — `run`, а не `tool_result`; там re-evaluate-блок происходит до создания assistant-шага хода). По построению (claim-before + инвариант пути `tool_result`, см. выше) единственный способ оказаться в `tool_result` с `trial_used=TRUE`, `subscription=none` и существующим assistant-шагом этого `message_step_id` — это что **именно этот ход** выполнил claim на своём `/chat/run` и вернул `tool_call`.

**Правило не-блокировки.** В `tool_result`, на закрытии барьера, **до** возврата `_blocked(decision)`: если `decision.block_reason == trial_used` **и** `is_inflight_trial_turn` — НЕ блокировать; продолжить generate-loop. Эквивалентно: для in-progress trial-хода subscription=none-ветка policy **не трактуется** как блок по `trial_used` (continuation не блокируется своим же claim). Любой **другой** `block_reason` (`credits_empty`, `subscription_*`, `byok_*`, `rate_limited`) на continuation **остаётся блоком** как прежде — признак снимает **только** ложный `trial_used` своего же хода, ничего более. `evaluate()` вызывается без изменений; решение принимается над её результатом в orchestrator.

**Reconcile на continuation.** Флаг `claimed_trial` request-scoped и в `/chat/tool-result` не переносится — поэтому «нужно ли откатить claim при неуспехе continuation» определяется **тем же** детерминированным признаком из состояния: **continuation-ход является trial-turn ⟺ `is_inflight_trial_turn`** (он же снял ложный блок выше). Тогда семантика [§2](#2-reconcile-откат-claim-при-неуспешной-генерации) применяется к continuation **идентично** `/chat/run`:
- continuation НЕ завершился успешным `assistant_message` (`status=blocked`/`max_tokens`, `UpstreamError`/`502`, исключение, отмена) **и** ход был `is_inflight_trial_turn` → **откатить claim** в отдельной короткой транзакции: `UPDATE users SET trial_used=FALSE WHERE id=:uid AND trial_used=TRUE` (идемпотентно, «не трогать чужой trial» — `WHERE trial_used=TRUE` + признак гарантируют, что откатывается только trial именно этого пользователя/хода);
- continuation завершился `assistant_message` → reconcile **не** выполняется, claim остаётся `TRUE`;
- continuation вернул промежуточный `status=tool_call` (следующий tool-round того же хода) → **не неуспех**: claim остаётся, reconcile откладывается до финализации (симметрия с `/chat/run` §8a).

Признак `is_inflight_trial_turn` вычисляется на входе в generate-loop continuation (после re-evaluate, до LLM) из уже загруженного `state` + одного запроса `assistant_tool_step_id`; результат используется и для не-блокировки, и как замена request-scoped `claimed_trial` в reconcile-обёртке (`try/except/finally`) этого continuation.

**Семантика успеха/неуспеха идентична [§8a run](../modules/chat-orchestrator/03-architecture.md):** `assistant_message`=успех (claim остаётся); `blocked`/`max_tokens`/upstream/exception=reconcile-откат; промежуточный `tool_call`=claim остаётся.

**Инварианты сохранены.** Policy engine не меняется (признак и решение — в orchestrator). Billing неизменен (trial без debit — `_billing_plan` для `subscription=none, trial_used=FALSE` даёт `mark_trial`; на continuation `trial_used` уже `TRUE`, поэтому `mark_trial=FALSE` — повторного flip нет, что **корректно**: claim уже застолбил trial на `/chat/run`). MAJOR-4 (commit-до-LLM) не трогается. claim/reconcile — отдельные короткие транзакции. **Без миграции** (признак выводится из существующих данных: `users.trial_used` + `chat_steps`/`tool_calls` хода) и **без изменения публичного контракта** (`/chat/tool-result` request/response неизменны; trial-ход теперь корректно доходит до `assistant_message` вместо ложного `blocked/trial_used`).

### 3. Совместимость с MAJOR-4 и существующим flip

- Прежний flip `mark_trial_used` **после** успешной генерации (в generate-транзакции, поток run §8) — **снимается** для trial-ветки: trial теперь флипается в §1 (claim) до генерации, а не после. Идемпотентность сохраняется (условный UPDATE). На успехе §2 не выполняется → trial остаётся `TRUE`.
- Generate-транзакция и её `commit()` до LLM (MAJOR-4) **не меняются** — claim/reconcile живут в отдельных коротких транзакциях вокруг неё.
- Policy Engine ([ADR-002](ADR-002-access-policy-state-machine.md)) — чистая функция, **не меняется**. Сериализацию даёт claim-UPDATE, не policy.

### 4. Остаточный риск (явно зафиксирован)

- **Окно «потерянного trial» при краше между claim (§1) и reconcile (§2/§2a).** Если процесс падает/теряет коннект ПОСЛЕ успешного claim, но ДО reconcile-rollback на неуспешной генерации — `trial_used` остаётся `TRUE`, хотя пользователь не получил бесплатный ответ. Пользователь теряет свой единственный trial. Окно расширяется на continuation ([§2a](#2a-continuation-tool-loop-под-claimed-trial-chattool-result)): trial-ход может оставаться застолблённым на протяжении нескольких `/chat/tool-result` round'ов; краш на любом из них (или клиент, не приславший tool_result) оставляет `trial_used=TRUE` без успешного `assistant_message`. Направление и митигация — те же ([Q-054-1](../99-open-questions.md): watchdog «claimed но нет успешного assistant_message за TTL» покрывает и run, и continuation).
  - **Оценка:** редкий кейс (краш в узком окне между двумя короткими транзакциями вокруг сбойной генерации). Направление риска **противоположно** исходному TD-006: раньше — пользователь получал ЛИШНЮЮ бесплатную генерацию (убыток сервиса); теперь — в редком крэш-кейсе пользователь НЕ получает положенную (убыток UX пользователя, не сервиса). Это предпочтительнее для анти-абуза.
  - **Митигация (операционная, без кода на старте):** оператор может вернуть trial через admin (`POST /v1/admin/...` — при наличии; или прямой `UPDATE users SET trial_used=false` под `app_migrate`) по обращению. Автоматический watchdog «claimed но не сгенерировал» — будущее усиление ([Q-054-1](../99-open-questions.md)).
- **Двойная бесплатная генерация устранена** (главная цель TD-006): атомарный claim-UPDATE — единственный арбитр; второй параллельный запрос видит `trial_used=TRUE` и блокируется.

## Consequences

**Положительные:**
- Окно двойной бесплатной trial-генерации **реально закрыто** (claim до генерации, атомарный условный UPDATE сериализует).
- MAJOR-4 не нарушается: claim/reconcile — отдельные короткие транзакции, generate-loop и его commit-до-LLM не трогаются; коннект на время генерации не держится.
- Семантика «trial сгорает только при успехе» сохранена через reconcile-rollback (симметрия с [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)).
- Переиспользует существующий атомарный условный UPDATE-паттерн ([ADR-005](ADR-005-idempotency-ledger.md)); без advisory lock.

**Отрицательные / ограничения:**
- Остаточный риск «потерянного trial» при краше в окне claim↔reconcile (§4) — редкий, направлен в сторону сервиса (анти-абуз), митигируется операционно.
- Логика trial усложняется (claim + reconcile вместо одного flip-after-success) — оправдано закрытием реального окна абуза.
- Доп. короткие транзакции (1 claim + 0/1 reconcile) на trial-ход — пренебрежимая нагрузка (только первый ход пользователя).

## Alternatives

1. **Advisory lock в generate-транзакции (исходное ТЗ TD-006).** Отвергнуто — несовместимо с MAJOR-4 (`commit()` до LLM освобождает xact-lock до flip; нулевая сериализация).
2. **Advisory lock в отдельной транзакции, удерживаемый на всю генерацию (session-scoped `pg_advisory_lock`/`unlock`).** Отвергнуто: держит коннект/lock на всё время генерации (минуты), исчерпывает пул соединений, фактически сериализует все ходы пользователя — нарушает дух MAJOR-4.
3. **Принять риск без кода (статус-кво ADR-002 §Trial concurrency).** Отвергнуто по цели пользователя: TD-006 нужно **реально** закрыть, а не оставить как принятый риск.
4. **Flip без reconcile (вариант (а) без §2 — claim навсегда).** Отвергнуто: меняет семантику «trial сгорает только при успехе» (пользователь терял бы trial на КАЖДОЙ сбойной первой генерации, включая штатный `max_tokens`) — слишком частый UX-убыток. Reconcile сужает потерю до редкого крэш-кейса.
