# ADR-066 — Снапшот состояния agent-прогона (`GET /v1/agent/runs/{runId}/state`), relay-side персистенция

- Статус: Accepted (**ревизия 2026-07-29 → [ADR-067](ADR-067-agent-run-background-consumer.md)**)
- Дата: 2026-07-28
- ⚠️ **Ревизия 2026-07-29 ([ADR-067](ADR-067-agent-run-background-consumer.md)) — читать вместе с [§Ревизия 2026-07-29](#ревизия-2026-07-29--вечный-running-устранён-только-при-живом-подписчике) в конце файла.** Прод-E2E опроверг две модели, на которых стоят §1/§6/§8 и §Consequences: (а) заявка §3 «терминальный статус до биллинга устраняет вечный `running`» верна **только пока relay подключён** — без подписчика `run.completed` теряется, `_mark_terminal` не вызывается вовсе; (б) митигация «повторная подписка довыполнит debit / догонит текст» **неисполнима**: перемер 2026-07-30 показал, что поток одноразовый — повторная подписка не получает ни реплея, ни новых событий даже на живом прогоне ([Q-066-1](../99-open-questions.md) **Closed**). Снапшот-writer, `_mark_terminal` и биллинг переезжают в фоновый consumer ([ADR-067](ADR-067-agent-run-background-consumer.md)); контракт `/state` при этом **не меняется** — как и предусматривал §8.
- Связан с: [ADR-045](ADR-045-hermes-as-agent-proxy.md) (SSE-relay `/events` — источник снапшота), [ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md) (**расширяет**: таблица `agent_runs` из lifecycle-под-флагом становится безусловной lifecycle-записью; статусы `failed`/`cancelled` начинают записываться), [ADR-046](ADR-046-per-user-hermes-runtime.md) (гибернация инстанса `HERMES_IDLE_TIMEOUT_SECONDS` — корневая причина потери состояния), [ADR-044](ADR-044-client-api-key-auth.md) (клиентская auth), [ADR-004](ADR-004-blocked-http-200.md) (business-blocked vs технические коды), [ADR-047](ADR-047-usage-based-billing-for-agent.md)/[Q-047-2](../99-open-questions.md) (биллинговый пробел при обрыве SSE — **НЕ закрывается** этим ADR), [03-data-model.md §25](../03-data-model.md), [modules/agent-proxy/](../modules/agent-proxy/README.md)
- Контракт данных: новая таблица `agent_run_snapshots` (миграция `0019`, цепочка `0018`→`0019`, single head); enum `agent_run_status` **не меняется**
- Контракт API: новый `GET /v1/agent/runs/{runId}/state` (аддитивный, read-only)
- Новые настройки: `AGENT_STATE_RESULT_TEXT_MAX_CHARS`, `AGENT_STATE_FLUSH_INTERVAL_SECONDS`, `AGENT_RUN_SNAPSHOT_TTL_DAYS`

## Context

Запрос iOS-разработчика: после kill приложения во время работы агента `GET /v1/agent/runs/{runId}/events` отдаёт `200` **без событий** — клиент не может восстановить ни статус, ни прогресс, ни результат прогона.

Причина подтверждена кодом:

1. **`/events` — чистый SSE-relay без персистенции** (`src/app/api_gateway/routers/agent.py:110-144` → `src/app/agent_proxy/service.py:278-362`). Накопленный текст ответа живёт в **локальных переменных генератора** одного HTTP-соединения; события Hermes нигде не сохраняются. Обрыв соединения = потеря всего накопленного состояния для клиента.
2. **Инстанс Hermes гибернируется** через `HERMES_IDLE_TIMEOUT_SECONDS` (дефолт `1800`, [ADR-046](ADR-046-per-user-hermes-runtime.md)) и при рестарте теряет свой in-memory реестр прогонов — даже обращение к самому Hermes после гибернации состояние не вернёт.
3. **`agent_runs` ([ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md), миграция `0018`) не покрывает задачу:** в ней нет текста ответа, нет токенов, нет последнего инструмента, нет pending approval; строка создаётся **только** под флагом `agent_incremental_billing_enabled` (дефолт `false`) — то есть на дефолтной конфигурации прогонов в БД **вообще нет**.
4. **`approval.request` ретранслируется, но нигде не сохраняется** — server-side «прогон ждёт подтверждения» не существует; после реконнекта клиент не знает, что от него ждут ответа.
5. Статусы `failed`/`cancelled` присутствуют в enum `agent_run_status`, но **никогда не записываются** (`run.failed` и `POST /stop` строку не трогают).

Нужен эндпоинт, который отдаёт снапшот состояния прогона **из нашей БД**, независимо от жизни SSE-соединения и от того, спит ли контейнер Hermes.

## Decision

### 1. Источник снапшота — существующий SSE-relay (v1), не отдельный consumer

Снапшот пишется как **побочный эффект** уже работающего relay `stream_events`: пока клиент (любой) подписан на `/events`, control plane проходит по событиям и апсертит состояние в `agent_run_snapshots`. Отдельный фоновый consumer, который держал бы SSE к Hermes независимо от клиента, **отложен** (см. §8 и Alternatives).

Следствие, принятое сознательно: **пока никто не подписан на `/events`, снапшот активного прогона не двигается** — `resultText` отстаёт. Клиент детектит это по `updatedAt` и переподключается к `/events` (§6, «Известные ограничения»).

> ⚠️ **Ревизия 2026-07-29 ([ADR-067 §1](ADR-067-agent-run-background-consumer.md)).** Рецепт «переподключиться к `/events` и догнать» негоден как гарантия: он требует действия клиента, а измеренный факт (3 прогона → 0 списаний, прогон висит `running`) показывает, что клиент переподключается не всегда. ✅ **Перемер 2026-07-30** ([Q-066-1](../99-open-questions.md) **Closed**) показал большее: поток одноразовый — повторная подписка не получает ни реплея, ни новых событий, то есть рецепт был неисполним в принципе, а не только ненадёжен. Отложенный здесь фоновый consumer принят [ADR-067](ADR-067-agent-run-background-consumer.md) в broker-форме.

### 2. Отдельная таблица `agent_run_snapshots` (1:1 к `agent_runs`), миграция `0019`

Снапшот **не** доклеивается колонками в `agent_runs`: у строк принципиально разный профиль записи и разный retention.

| | `agent_runs` ([ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md)) | `agent_run_snapshots` (этот ADR) |
|---|---|---|
| Назначение | «денежная»/lifecycle-строка: статус, кредиты, resume-цепочка | UX-снапшот: текст, инструмент, approval, токены |
| Частота записи | единицы раз за прогон (per-step только под флагом биллинга) | десятки раз за прогон (троттлинг `message.delta`) |
| Хранение | бессрочно (аудит/реконсиляция) | текст чистится через `AGENT_RUN_SNAPSHOT_TTL_DAYS` |
| Чувствительность | суммы, без user-content | **содержит user-facing текст модели** |

DDL — [03-data-model.md §25](../03-data-model.md). Ключевое: `run_id TEXT PK REFERENCES agent_runs(run_id) ON DELETE CASCADE`, `user_id UUID FK users ON DELETE CASCADE` (+индекс), `result_text TEXT NOT NULL DEFAULT ''`, `last_tool TEXT NULL`, `pending_approval JSONB NULL` (`{"tool":…, "preview":…}`), `input_tokens`/`output_tokens BIGINT NOT NULL DEFAULT 0`, `updated_at timestamptz`.

**Статус в снапшоте НЕ дублируется** — источник истины статуса остаётся `agent_runs.status` (иначе два места правды и рассинхрон). Enum `agent_run_status` **не расширяется**: `failed`/`cancelled` в нём уже есть, а `waiting_approval` — **производный** статус (§4), не значение enum.

### 3. `agent_runs` становится безусловной lifecycle-записью (развязка от флага биллинга)

`create_running` (корневой прогон в `run()` и child в `resume()`) и запись терминального статуса вызываются **всегда**, без гварда `agent_incremental_billing_enabled`. Прежний узкоспециализированный `_mark_completed` **удалён** — вместо него единый **сервисный** `_mark_terminal(run_id, status)`, вызываемый из обработчиков терминальных событий в `stream_events` (`run.completed` → `_mark_terminal(run_id, 'completed')`, `run.failed` → `_mark_terminal(run_id, 'failed')`). Разделение слоёв: `_mark_terminal` — обёртка сервиса (`commit` + проглатывание `SQLAlchemyError`, чтобы сбой записи статуса не рвал SSE-стрим), сам условный переход выполняет репозиторный **`runs_repo.mark_status(run_id, status)`** (`WHERE status IN ('running','resumed')`).

> ⚠️ **Ревизия 2026-07-29 ([ADR-067](ADR-067-agent-run-background-consumer.md)) к формулировкам ниже.** Инвариант «отказ списания больше не оставляет прогон в `running`» верен, но покрывает **только один** источник вечного `running`. Весь §3 исполняется **внутри обработчика события в `stream_events`**, поэтому держится **только пока relay подключён**. Второй, доминирующий на проде источник — **подписчика не было вовсе**: `run.completed` теряется безвозвратно, `_mark_terminal` не вызывается ни разу, статус остаётся `running` навсегда (замер: прогон висел `running` >15 мин после фактического завершения; 0 списаний на 3 прогонах). Читать «устраняет вечный `running`» как «устраняет вечный `running` **при живом подписчике**». Полное устранение — [ADR-067](ADR-067-agent-run-background-consumer.md) (consumer §1–§2 + orphan-reaper §5); правила самого §3 (порядок, условность переходов, owner-scoping) при этом переносятся **без изменений**, меняется только исполнитель.

**Инвариант порядка (существенный): терминальный статус пишется ДО биллинга и независимо от его исхода.** `_mark_terminal` вызывается в обработчике события **перед** `consume`/финализацией остатка и не находится с ними в общей транзакции-судьбе: любой отказ списания (нехватка баланса, конфликт idempotency, транспортная ошибка кошелька) **не** оставляет прогон в статусе `running`. До этого фикса упавшее списание давало «вечный `running`»: `/state` бесконечно показывал бы работающий прогон, `/resume` считал бы его невозобновляемым, а reaper-retention никогда бы его не подобрал (он смотрит только на терминальные). Обратный порядок (биллинг → статус) запрещён.

**`mark_stopped` — owner-scoped.** Сигнатура `mark_stopped(run_id, user_id)`, SQL добавляет `AND user_id = :uid` к условию статуса: `UPDATE agent_runs SET status='cancelled' WHERE run_id=:id AND user_id=:uid AND status IN ('running','resumed')`. Скоуп по владельцу делает RBAC-инвариант ([06-rbac.md](../modules/agent-proxy/06-rbac.md)) свойством самого запроса, а не только предшествующей проверки: даже при ошибке в вызывающем коде чужой прогон остановить нельзя.

Под флагом `agent_incremental_billing_enabled` остаётся **только биллинг**: `record_step`, per-step debits, pause-at-zero, финализация остатка, resume-policy — семантика [ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md) §1–§5 не меняется.

Одновременно чинятся никогда-не-записываемые статусы:

| Событие/действие | Запись в `agent_runs` |
|---|---|
| `run.failed` в `stream_events` | `UPDATE … SET status='failed' WHERE run_id=:id AND status IN ('running','resumed')` — **условный** (+ commit, + финальный флаш снапшота) |
| `run.completed` | `UPDATE … SET status='completed' WHERE run_id=:id AND status IN ('running','resumed')` — **условный** (было под флагом — теперь всегда) |
| `POST /v1/agent/runs/{runId}/stop` (**клиентский путь**), после 2xx passthrough | `runs_repo.mark_stopped(run_id, user_id)`: `UPDATE … SET status='cancelled' WHERE run_id=:id AND user_id=:uid AND status IN ('running','resumed')` — **условный + owner-scoped** |
| `run.paused` (синтетическое) | `status='paused'` + `paused_reason` — остаётся частью incremental-контура ([ADR-064 §3](ADR-064-incremental-agent-run-billing-and-pause-resume.md)) |

**Все переходы статуса — условные (`WHERE status IN ('running','resumed')`), без исключений.** Безусловная запись терминального статуса создала бы гонку «last writer wins»: после `POST /stop` Hermes ещё какое-то время дофлашивает события в открытый relay, и долетевшее `run.completed`/`run.failed` перезаписало бы уже выставленный `cancelled` — клиент увидел бы `completed` у прогона, который сам же остановил, а история потеряла бы факт отмены. Условие делает первый терминальный статус победителем и одновременно защищает `paused` от затирания.

**`mark_stopped(run_id, user_id)` вызывается ТОЛЬКО на клиентском пути `POST /v1/agent/runs/{runId}/stop`.** Это критично: pause-at-zero ([ADR-064 §3](ADR-064-incremental-agent-run-billing-and-pause-resume.md) п.2) выполняет Hermes-interrupt **тем же** `self.stop()`, и если пометка статуса окажется внутри общего метода, прогон при исчерпании баланса транзиентно станет `cancelled` до записи `paused` — со следствиями: `/state` отдаст `stopped` вместо `paused` (клиент не покажет предложение пополнения), а `POST /resume` в этом окне получит `409 run_not_resumable` (гвард требует `paused`/`resumed`). Реализация обязана развести пути одним из двух способов: (а) `mark_stopped(run_id, user_id)` вызывается в роутере/сервисном методе клиентского `/stop` **после** passthrough, а не внутри общего `stop()`; либо (б) внутренний interrupt использует отдельный метод (напр. `_interrupt_run()`), не помечающий статус. Внутренний путь pause-at-zero статус `cancelled` не выставляет **никогда**.

Фиксы ценны **сами по себе**, вне зависимости от эндпоинта: до сих пор упавший или остановленный прогон навсегда оставался `running`.

### 4. Маппинг статусов server → client (чистая функция)

| `agent_runs.status` | Условие | Статус в ответе `/state` |
|---|---|---|
| `running`, `resumed` | `pending_approval IS NULL` | `running` |
| `running`, `resumed` | `pending_approval IS NOT NULL` | `waiting_approval` |
| `paused` | — | `paused` (+ `blockReason`) |
| `completed` | — | `completed` |
| `failed` | — | `failed` |
| `cancelled` | — | `stopped` |
| — | не эмитится в v1 | `queued` (в `Literal` для forward-compat) |

- `waiting_approval` — **производный**, вычисляется на чтении (`status ∈ {running, resumed}` **AND** `pending_approval IS NOT NULL`). Новых значений в enum БД не появляется.
- `resumed → running`: `resumed` — это статус **родительской** строки после успешного resume; работа идёт в child-прогоне. Клиент подписывается на `runId` из ответа `/resume`; `continuedFrom` в `/state` даёт обратную связь child→parent. `resultText` parent-строки остаётся текстом до паузы и больше не растёт — это ожидаемо.
- `cancelled → stopped`: клиентское имя выбрано под словарь iOS (`POST /stop` → `stopped`); серверное значение enum не переименовывается (миграция enum — дороже, чем маппинг на чтении).

### 5. Эндпоинт `GET /v1/agent/runs/{runId}/state` — строго read-only

Полный контракт — [modules/agent-proxy/02-api-contracts.md](../modules/agent-proxy/02-api-contracts.md). Инварианты, обязательные к соблюдению в реализации (и к фиксации в docstring роута):

- **Никакого `ensure_running`.** Эндпоинт **не будит** гибернированный контейнер: иначе опрос состояния из фона стоил бы cold-start (~30–40 с, [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md)) и ресурсов на каждый polling-тик.
- **Никакого обращения к Hermes.** Только `SELECT` из `agent_runs` + `agent_run_snapshots`.
- **Никаких списаний.** Чтение состояния бесплатно; `WalletService` не вызывается.
- **Никакого policy-gate.** Блокировка по кредитам ([ADR-002](ADR-002-access-policy-state-machine.md)/[ADR-004](ADR-004-blocked-http-200.md)) относится к запуску генерации, а не к чтению уже произошедшего. `200 {status:blocked}` на этом маршруте **не возникает**.
- Auth — `CurrentUser` (`X-API-Key` + `X-User-Id`, [ADR-044](ADR-044-client-api-key-auth.md)); rate limit — общий `enforce_other_limits` (как у `chats`), превышение → `429`.
- Ownership — паттерн `/resume` ([ADR-064 §5](ADR-064-incremental-agent-run-billing-and-pause-resume.md)): строки нет **или** `user_id` не совпал → `404`, **никогда 403** ([modules/agent-proxy/06-rbac.md](../modules/agent-proxy/06-rbac.md)).
- Чтение — **owner-scoped на уровне запроса**: `snapshots_repo.get(run_id, user_id)` (`AND user_id = :uid`), симметрично `mark_stopped`/`clear_pending_approval`. RBAC-проверка по `agent_runs` остаётся первым шагом; скоуп в самом `SELECT` — defense-in-depth под [Q-066-2](../99-open-questions.md) (при коллизии `run_id` между пользователями вернётся `None`, а не чужой снапшот).
- Снапшот может **отсутствовать** при существующей строке `agent_runs` (writer ещё не отработал ни одного события) → `200` с дефолтами: ⚠️ **Ревизия 2026-07-29 ([ADR-067 §4](ADR-067-agent-run-background-consumer.md)):** ветка становится **остаточной** — consumer создаёт строку снапшота **при старте прогона** (ради heartbeat'а), поэтому при живом consumer'е она существует с первой секунды. Ветка сохраняется для прогонов, у которых consumer не встал вовсе, и при `AGENT_RUN_CONSUMER_ENABLED=false`. Именно из-за этой ветки heartbeat **нельзя** держать в `agent_runs.updated_at`: он утёк бы в `updatedAt` ровно здесь. Значения дефолтов: `resultText:""`, `lastTool:null`, `pendingApproval:null`, `usage:{0,0}`, `updatedAt` = `agent_runs.updated_at`.

**Именование `blockReason` (осознанное расхождение).** В ответе `/state` поле `blockReason` несёт `agent_runs.paused_reason` (в v1 — единственное значение `credits_exhausted`), а **не** policy-enum `blockReason` из [ADR-004](ADR-004-blocked-http-200.md)/[ADR-002](ADR-002-access-policy-state-machine.md) (`credits_empty|subscription_expired|…`). Имя выбрано по запросу клиента (единое поле «почему стоим» в UI). Наборы значений **не пересекаются**, поэтому неоднозначности на стороне клиента нет, но расхождение зафиксировано здесь и в контракте явно, чтобы никто не начал валидировать это поле policy-enum'ом.

### 6. Снапшот-writer в relay (`stream_events`)

Накопление `partial_text`/`steps`/токенов **выносится из ветки `if incremental:`** — накапливается всегда, независимо от флага биллинга. Апсерт — с явным `commit()` (streaming-context паттерн `_bill_step`, [ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md)). Побочные эффекты по событиям — [modules/agent-proxy/05-events.md §Снапшот-побочные эффекты](../modules/agent-proxy/05-events.md).

Три механики, критичные для корректности:

1. **Троттлинг `message.delta`.** Флаш `result_text` — не чаще одного раза в `AGENT_STATE_FLUSH_INTERVAL_SECONDS` (дефолт `3.0`). Терминальные события (`run.completed`/`run.failed`/`run.paused`) и `approval.request` флашатся **немедленно**, минуя троттлинг: их задержка ломала бы UX (клиент не увидел бы, что от него ждут approval).
2. **Replay-guard — per-column, НЕ row-level.** Hermes при переподключении к `/events` реплеит буфер **с начала**, поэтому второй потребитель может начать с короткого префикса и затереть более полный текст. Защита — монотонность **отдельных колонок**:

   > ⚠️ **Ревизия 2026-07-30 ([Q-066-1](../99-open-questions.md), перемер на активном прогоне — Closed).** Итог: **историю получает первый подписчик** (посылка верна именно для него), а **повторная подписка не получает ничего — ни реплея, ни новых событий**. Значит сценарий «второй потребитель начинает с короткого префикса и затирает более полный текст», ради которого guard вводился, **не возникает вовсе**: второй потребитель не пишет ничего. **Практический вывод не меняется:** SQL ниже остаётся в силе как есть и после [ADR-067](ADR-067-agent-run-background-consumer.md), где writer'ом становится consumer. Меняется статус обоснования: guard из «защиты от replay-подмены» становится **чистым defense-in-depth** (страховка на случай двух consumer'ов при истёкшем lease, [ADR-067 §4](ADR-067-agent-run-background-consumer.md)), поскольку сценарий, от которого он защищал, измеренно не наступает. ⚠️ Само утверждение «первый подписчик получает историю» (H4) отдельной пробой **не упражнялось** — на вывод это не влияет (guard в любом случае defense-in-depth), но ссылаться на него как на факт нельзя.


```sql
INSERT INTO agent_run_snapshots AS t (run_id, user_id, result_text, last_tool, pending_approval,
                                      input_tokens, output_tokens, updated_at)
VALUES (:run_id, :user_id, :result_text, :last_tool, :pending_approval, :in_tok, :out_tok, now())
ON CONFLICT (run_id) DO UPDATE SET
    result_text      = CASE WHEN char_length(EXCLUDED.result_text) >= char_length(t.result_text)
                             AND left(EXCLUDED.result_text, char_length(t.result_text)) = t.result_text
                            THEN EXCLUDED.result_text ELSE t.result_text END,   -- префиксный guard
    input_tokens     = GREATEST(t.input_tokens,  EXCLUDED.input_tokens),
    output_tokens    = GREATEST(t.output_tokens, EXCLUDED.output_tokens),
    pending_approval = CASE WHEN :assert_approval
                            THEN EXCLUDED.pending_approval ELSE t.pending_approval END,
    last_tool        = EXCLUDED.last_tool,        -- безусловно
    updated_at       = EXCLUDED.updated_at        -- безусловно
WHERE t.user_id = EXCLUDED.user_id;               -- tenancy-гвард (НЕ staleness-гейт, см. ниже)
```

**Префиксный guard `result_text` (усиление длины).** Проверки длины недостаточно: она защищает от **укорачивания**, но не от **подмены** — более длинный текст, не продолжающий сохранённый, прошёл бы. Условие `left(EXCLUDED.result_text, char_length(t.result_text)) = t.result_text` требует, чтобы входящий текст **продолжал** уже накопленный. Поведение по сценариям: при replay-с-начала эквивалентно прежней проверке длины (префикс совпадает, текст растёт); при отсутствии replay (второй потребитель получает только новые дельты, [Q-066-1](../99-open-questions.md)) входящий фрагмент общего начала не имеет → **снапшот замерзает** на последнем полном тексте вместо подмены обрывком без начала. Замирание видно клиенту по `updatedAt` и лечится добором через `/events`; подмена текста была бы наблюдаемой порчей данных. Какая из двух семантик у образа фактически — определяется **по наблюдаемому контракту, а не по логам**: замер `char_length(resultText)`/`updatedAt` до и после повторной подписки на `/events` ([Q-066-1](../99-open-questions.md), процедура в [09-testing.md §E2E](../modules/agent-proxy/09-testing.md)). DEBUG-маркер `result_text frozen` в `service.py` для этого **не годится на проде** (`LOG_LEVEL=INFO`) — он вспомогательный, для локальной отладки.

**Tenancy-гвард `WHERE t.user_id = EXCLUDED.user_id`** — defense-in-depth под допущение о глобальной уникальности `run_id` ([Q-066-2](../99-open-questions.md)): `run_id` генерируется инстансом каждого пользователя независимо, а PK у нас глобальный. При гипотетической коллизии между пользователями конфликтный апсерт даёт `rowcount = 0` вместо перезаписи чужого снапшота.

> **Это не противоречит запрету row-level `WHERE` ниже.** Запрет мотивирован **staleness-гейтингом**: нельзя гейтить строку целиком по «свежести» данных (длине текста), потому что вместе с текстом заморозятся `pending_approval`/`last_tool`/`updated_at`. Тенантный предикат — про **владельца**, а не про свежесть: для легитимной записи владельца он **истинен всегда** и потому не блокирует ни одного корректного апсерта. Различие: гейт по данным события — запрещён, гейт по identity — обязателен.

**Параметр `:assert_pending_approval`** (`assert_approval` в SQL) отделяет апсерты, которые **утверждают** значение `pending_approval`, от тех, которые его лишь переносят:

Ниже — **все четыре** реальных call-site апсерта:

| Точка вызова (апсерт) | `assert_pending_approval` | Записывает `pending_approval` |
|---|---|---|
| `approval.request` | `true` | `{tool, preview}` |
| `tool.started` / `tool.completed` | `true` | `NULL` (агент поехал дальше) |
| `run.completed` / `run.failed` / `run.paused` | `true` | `NULL` (терминальное состояние) |
| **троттлинговый флаш `message.delta`** | **`false`** | **не трогает** — переносит текущее значение |

> **`POST …/approval` в таблице отсутствует намеренно — это не апсерт.** Ответ пользователя снимает approval отдельным owner-scoped `clear_pending_approval(run_id, user_id)`: чистый `UPDATE` **без `INSERT`** (строка снапшота к этому моменту заведомо существует — её создал `approval.request`), коммит — **teardown request-сессии**, а не streaming-контекст. Параметр `assert_pending_approval` к этому пути **не относится**. Это единственный writer снапшота вне relay и потому единственный, кому нужен owner-скоуп (у relay-путей владелец уже проверен при открытии стрима).

Без этого разделения троттлинговый флаш текста «воскрешал» бы уже снятый approval: клиент отвечает `POST …/approval` (снимаем `pending_approval`), а следующий периодический флаш `message.delta`, несущий в своём payload устаревший снимок состояния writer'а, записал бы старое значение обратно — `/state` вернулся бы в `waiting_approval` по прогону, который уже продолжает работу. Флаг гарантирует, что `pending_approval` меняют **только** события, которые действительно про approval-состояние.

> **Row-level `WHERE` как staleness-гейт по-прежнему запрещён.** Гейтить строку **целиком по свежести данных** (длине/префиксу текста) нельзя: пока идёт replay-окно, не записались бы **ни** `pending_approval` (то есть `waiting_approval` не отдался бы клиенту **никогда**), **ни** `last_tool` (замерзает на устаревшем инструменте), **ни** `updated_at` (поле начинает врать, а на нём стоит клиентский детект устаревания). Именно поэтому префиксная проверка живёт **в `CASE` колонки `result_text`**, а не в `WHERE` строки. Единственный допустимый row-level предикат — **тенантный** (`t.user_id = EXCLUDED.user_id`, см. выше): он про identity, истинен для любой легитимной записи владельца и потому ничего не блокирует. Гейт `pending_approval` — **колоночный и по семантике источника** (`:assert_pending_approval`); монотонность по-прежнему нужна только `result_text` и токенам.
3. **Head-preserving truncation — к обеим строкам снапшота.** `AGENT_STATE_RESULT_TEXT_MAX_CHARS` (дефолт `65536` = 64 KB) ограничивает **и** `result_text`, **и** `pending_approval.preview` (`_build_pending_approval()`): обе — user content, оба поля отдаются клиенту, отдельного knob'а под preview не вводится. Тем же лимитом ограничен `output` синтетического `run.paused` (relay-буфер схлопывается при каждом флаше). `result_text` обрезается **с головы** (сохраняется начало). Head-preserving выбран ради replay-guard: при обрезке хвоста префикс перестал бы быть стабильным и сравнение длин потеряло бы смысл. По достижении потолка длина фиксируется, `>=` продолжает пропускать апдейты — это безопасно, т.к. первые 64 KB идентичны. ~~Полный текст всегда доступен через `/events`.~~ ⚠️ **Ревизия 2026-07-29 ([ADR-067](ADR-067-agent-run-background-consumer.md)):** формулировка «полный текст всегда доступен через `/events`» была верна для прямого relay и **под broker-моделью неверна** — ring тоже ограничен (`AGENT_RUN_EVENT_BUFFER_MAX`/`..._MAX_BYTES`), ради чего и введён обязательный маркер `run.truncated`. **Полноту текста длинного прогона не гарантирует ни один источник:** `/state.resultText` — 64 KB head-preserving, `/events` — содержимое ring'а текущего поколения. Неполнота при этом **наблюдаема** (маркер), а не молчалива.

`pending_approval` очищается в трёх местах: на `tool.started`/`tool.completed` (агент поехал дальше) и на терминальных событиях — оба через апсерт с `assert_pending_approval=true`; и в `approval()` после успешного passthrough — через отдельный owner-scoped `clear_pending_approval(run_id, user_id)` (см. сноску выше). Иначе `waiting_approval` «залипал» бы после ответа пользователя.

**При выключенном `agent_incremental_billing_enabled`** writer работает полностью: текст/инструменты/approval пишутся как обычно, токены берутся из `run.completed{usage}` (событий `usage.delta` в этом режиме нет).

### 7. Retention — 14 дней, чистится только контент

Существующий reaper-цикл (`lifespan`, паттерн [ADR-046](ADR-046-per-user-hermes-runtime.md)) для терминальных прогонов старше `AGENT_RUN_SNAPSHOT_TTL_DAYS` (дефолт `14`) выполняет:

```sql
UPDATE agent_run_snapshots SET result_text = '', pending_approval = NULL
WHERE updated_at < now() - :ttl
  AND (result_text <> '' OR pending_approval IS NOT NULL)   -- guard идемпотентности, ОБЯЗАТЕЛЕН
  AND run_id IN (SELECT run_id FROM agent_runs
                 WHERE status IN ('completed','failed','cancelled','paused'));
```

Sweep обслуживается **частичным индексом** `ix_agent_run_snapshots_sweep ON agent_run_snapshots (updated_at) WHERE result_text <> '' OR pending_approval IS NOT NULL`: предикат индекса совпадает с guard'ом запроса, поэтому очищенные строки из индекса **выпадают**. В установившемся режиме (всё старое уже вычищено) индекс пуст/мал, и тик reaper'а стоит **O(0)** вместо скана по всей истории прогонов — фоновая задача не деградирует с ростом таблицы.

Частичный индекс **заменяет** плановый полный `(updated_at)`, а не дополняет его: единственный потребитель полного — тот же sweep, а лишний индекс по `updated_at` бил бы по **самой горячей записи** таблицы (апсерт двигает `updated_at` примерно раз в 3 с на активный прогон) — чистая write-amplification без читателя. Итоговый состав индексов таблицы — **два**: `ix_agent_run_snapshots_user` + частичный `ix_agent_run_snapshots_sweep` ([03-data-model.md §25](../03-data-model.md)); оба — в той же миграции `0019` (конвенция: одна миграция на ADR).

> ⚠️ **Ревизия 2026-07-29 ([ADR-067 §4/§5](ADR-067-agent-run-background-consumer.md)) — решение «два индекса» ПОДТВЕРЖДЕНО, но было под угрозой.** Миграция `0020` добавляет в таблицу колонку `consumer_heartbeat_at` (heartbeat фонового consumer'а), и первый вариант заводил под неё **третий** индекс `WHERE consumer_heartbeat_at IS NOT NULL` — **отвергнут именно по критерию этого параграфа**: его предикат **не самоочищается** (истинен для каждой тронутой строки), поэтому индекс рос бы со всей историей прогонов, давая write-amplification на горячей колонке без свойства, ради которого частичность вводилась. Вместо него — самоочищающийся `ix_agent_runs_active` на **`agent_runs`** (`(created_at) WHERE status IN ('running','resumed')` — ведущий столбец `created_at`, т.к. возраст кандидата считается по `COALESCE(consumer_heartbeat_at, created_at)`, а `updated_at` в свипе не участвует), то есть в таблице, куда heartbeat не пишется. **Состав индексов `agent_run_snapshots` остаётся двумя.** Зафиксировано здесь, чтобы ревизия не прошла молча.

Два инварианта sweep'а, оба обязательны:

1. **Идемпотентность.** Guard `AND (result_text <> '' OR pending_approval IS NOT NULL)` отсекает уже очищенные строки. Без него каждый тик reaper'а (`HERMES_REAPER_INTERVAL_SECONDS`, дефолт 300 с) переписывал бы **все** терминальные снапшоты старше TTL **вечно** — постоянный поток no-op `UPDATE`, растущий с историей: раздувание MVCC-версий, лишняя работа autovacuum и WAL на ровном месте. Повторный проход по очищенному прогону обязан давать `rowcount = 0`.
2. **`updated_at` sweep НЕ трогает.** Поле означает «время последней записи **состояния** прогона» и является клиентским детектором устаревания (§5); сдвиг его чисткой контента означал бы для клиента «состояние только что обновилось», что ложь. `UPDATE` в sweep'е перечисляет **только** `result_text` и `pending_approval`; триггеров/`DEFAULT`-ов, автоматически двигающих `updated_at`, у таблицы нет.

Строка **не удаляется**: `/state` продолжает отдавать `200` со статусом, токенами и `updatedAt` — «прогон был, вот его исход» доступно бессрочно, а user-content не хранится дольше нужного. Активные (`running`/`resumed`) прогоны не трогаются ни при каком возрасте.

### 8. Биллинговый пробел [Q-047-2](../99-open-questions.md) этим ADR НЕ закрывается

Снапшот привязан к тому же relay, что и биллинг: **нет подписчика — нет ни списаний, ни обновлений снапшота**. Прогон, доработавший в фоне без клиента, по-прежнему не тарифицируется до повторной подписки на `/events` (idempotency по `runId` довыполнит debit). Единый фикс обеих проблем — **фоновый consumer** (§Alternatives 1), отложен; при его внедрении снапшот-writer переезжает в него без изменения контракта `/state`.

> ⚠️ **Ревизия 2026-07-29 ([ADR-067](ADR-067-agent-run-background-consumer.md)).** Два утверждения этого параграфа несостоятельны:
> 1. **«idempotency по `runId` довыполнит debit при повторной подписке» — не гарантия, а надежда на действие клиента.** Измерено: 3 прогона → 0 списаний, `trialRemaining`=1, прогон висит `running` >15 мин — клиент не переподключился ни разу. Idempotency защищает от **двойного** списания, но не восстанавливает **несделанное**. (✅ Перемер 2026-07-30 — [Q-066-1](../99-open-questions.md) **Closed** — показал большее: поток одноразовый, повторная подписка не получает ни реплея, ни новых событий.)
> 2. **«отложен» — более не отложен.** [ADR-067](ADR-067-agent-run-background-consumer.md) принимает consumer в **broker-форме**. ⚠️ Формулировка «наивный параллельный consumer физически невозможен» прошла две ревизии: **2026-07-29 снята как недоказанная** (требовала гипотезы H2 об одновременных подписчиках, которая не измерялась), **2026-07-30 ВОССТАНОВЛЕНА на другом основании** — прямым замером односторонности потока ([Q-066-1](../99-open-questions.md): повторная подписка инертна даже на живом прогоне), тогда как H2 **утратила предмет** ([Q-067-5](../99-open-questions.md)). Альтернатива помечена **ОТВЕРГНУТО** в [ADR-067 §Alternatives 2](ADR-067-agent-run-background-consumer.md). Broker при этом выбирался по основаниям, не зависящим от поведения образа ([ADR-067 §0](ADR-067-agent-run-background-consumer.md)).
>
> Верным осталось главное: **снапшот-writer переезжает в consumer без изменения контракта `/state`** — так и сделано ([ADR-067 §2](ADR-067-agent-run-background-consumer.md)). [Q-047-2](../99-open-questions.md) закрывается [ADR-067](ADR-067-agent-run-background-consumer.md); до его реализации пробел живёт как **[TD-037](../100-known-tech-debt.md)**.

## Consequences

**Положительные:**
- Клиент восстанавливает состояние прогона после kill приложения/смены сети/гибернации Hermes — один дешёвый `GET`, без пробуждения контейнера и без денег.
- `waiting_approval` становится server-side наблюдаемым: до сих пор запрос подтверждения существовал только внутри живого SSE-соединения и терялся вместе с ним.
- Чинятся мёртвые статусы: упавший (`failed`) и остановленный (`cancelled`) прогоны перестают вечно числиться `running` — это чинит и достоверность resume-гвардов ([ADR-064 §5](ADR-064-incremental-agent-run-billing-and-pause-resume.md)), и будущую реконсиляцию.
- `agent_runs` заполняется на дефолтной конфигурации (флаг биллинга OFF) → появляется фактическая lifecycle-история агентных прогонов.
- Разделение таблиц изолирует write-amplification снапшота от «денежной» строки и даёт независимый retention.

**Отрицательные / ограничения:**
- **`resultText` активного прогона отстаёт**, пока никто не подписан на `/events` (§1). Митигация — `updatedAt` в ответе + рецепт клиента: `GET /state` → если `running`/`waiting_approval` → переподключиться к `/events` и догнать. ⚠️ **Ревизия 2026-07-30:** рецепт **неисполним** — перемер на активном прогоне ([Q-066-1](../99-open-questions.md) **Closed**) показал, что поток одноразовый: повторная подписка не получает ни реплея, ни новых событий. После [ADR-067](ADR-067-agent-run-background-consumer.md) отставание исчезает по существу: снапшот двигает consumer, а не клиент, и переподключение к `/events` даёт реплей из ring.
- **Прогоны, запущенные до деплоя, → `404`** (строк `agent_runs` для них нет; backfill невозможен — исходных данных не существует). Осознанно, без миграции данных.
- **`stop → stopped` — eventual consistency:** строка флипается на 2xx passthrough, Hermes в этот момент может ещё дофлашивать события в открытый relay; кратковременно `/state` покажет `stopped` при ещё идущем добивании стрима.
- Рост записи в БД на активный прогон (апсерт раз в ~3 с на прогон с подписчиком) — приемлемо: троттлинг ограничивает частоту, строка узкая, индексов два.
- В БД появляется **user-facing текст модели** (`result_text`) — новый класс данных at-rest. Митигации: TTL 14 дней (§7), потолок 64 KB, FK CASCADE от `users` (удаление пользователя чистит снапшоты), `result_text`/`pending_approval` **не попадают в логи и audit** (в снапшот пишет репозиторий, а не observability-путь; redaction-контракт [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)/[05-security.md](../05-security.md) не меняется).
- Поле `blockReason` в `/state` семантически отличается от policy-`blockReason` (§5) — зафиксировано, но остаётся точкой возможной путаницы у нового читателя контракта.

## Alternatives

1. **Фоновый consumer SSE (server-side, независимый от клиента).** Держит подписку на `/events` весь прогон, пишет снапшот и выполняет биллинг → закрыл бы заодно [Q-047-2](../99-open-questions.md). **Отложен, не отвергнут:** требует управления жизненным циклом задач (кто владеет подпиской при нескольких воркерах, что при рестарте контейнера api, как не задвоить debit при живом клиентском relay) — это отдельное решение сопоставимого объёма. V1 сознательно ограничен побочным эффектом уже существующего пути; миграция на consumer не меняет контракт `/state`.
   > ⚠️ **Ревизия 2026-07-29 → принят [ADR-067](ADR-067-agent-run-background-consumer.md).** Три перечисленных здесь возражения разрешились так: **владение при 4 воркерах** — Redis-lease + pub/sub-фан-аут ([ADR-067 §3–§4](ADR-067-agent-run-background-consumer.md)); **рестарт api** — orphan-reaper как страховка ([ADR-067 §5](ADR-067-agent-run-background-consumer.md)), полноценный подхват отложен ([Q-067-1](../99-open-questions.md)); **двойной debit при живом клиентском relay** — возражение **растворилось**, а не решено: в broker-форме клиентский relay не тарифицирует вообще. Формулировка «независимый от клиента» заменена на **broker**: consumer сделан **единственным** upstream-подписчиком, клиент переведён на downstream. ⚠️ Обоснование «потому что буфер одноразовый» **снято как недоказанное** (требует гипотезы H2, [Q-067-5](../99-open-questions.md)); действующее обоснование — единый владелец биллинга и состояния + межпроцессный фан-аут при 4 воркерах ([ADR-067 §0](ADR-067-agent-run-background-consumer.md)).
2. **Отдать состояние из Hermes (state-endpoint на стороне инстанса).** Отвергнуто: (а) потребовало бы `ensure_running` — чтение состояния будило бы контейнер и стоило бы cold-start на каждый polling-тик; (б) после гибернации Hermes **теряет in-memory реестр прогонов**, т.е. ровно в целевом сценарии отдать нечего; (в) добавило бы ещё одну зависимость от патча образа ([ADR-065](ADR-065-patched-hermes-image-ghcr.md)) с сопутствующим maintenance-долгом ([TD-036](../100-known-tech-debt.md)).
3. **Персистить сырой event-log прогона (таблица событий) и собирать состояние на чтении.** Отвергнуто для v1: на порядок больше записи (каждая `message.delta` — строка), нужен свой retention и агрегация на чтении; ценности сверх снапшота для заявленного UX не даёт. Может вернуться, если понадобится полный replay истории прогона.
4. **Добавить колонки снапшота в `agent_runs`.** Отвергнуто: смешивает «денежную» строку с горячо-обновляемым user-content, навязывает общий retention и раздувает строку, которую читают resume/биллинг-пути (§2).
5. **Держать снапшот в Redis/in-memory кэше.** Отвергнуто: Redis в стеке нет ([ADR-001](ADR-001-stack-choice.md)), а in-memory не переживает рестарт api-контейнера — то есть не решает исходную проблему (потерю состояния при рестарте).
   > ⚠️ **Ревизия 2026-07-29 — фактическая ошибка.** **Redis 7 в стеке есть** ([ADR-001](ADR-001-stack-choice.md), [02-tech-stack.md](../02-tech-stack.md): rate limiting, idempotency-метки; `src/app/api_gateway/rate_limit.py`, healthcheck `/ready` его пингует). Вывод альтернативы («снапшот — в Postgres») **не меняется** и остаётся верным по второй причине: Redis не даёт durability, нужной снапшоту. Но обоснование «Redis нет» ложно и не должно использоваться повторно — [ADR-067 §3](ADR-067-agent-run-background-consumer.md) опирается на Redis именно как на **эфемерный** транспорт (ring + pub/sub), оставляя durable-состояние в Postgres.
6. **Не хранить текст, отдавать только статус.** Отвергнуто: основной запрос iOS — показать пользователю накопленный ответ после возврата в приложение; один статус этого не закрывает.

## Ревизия 2026-07-29 — «вечный `running`» устранён только при живом подписчике

Источник: прод-E2E фичи ([ADR-065](ADR-065-patched-hermes-image-ghcr.md)-образ, `.156`). Тело ADR выше не переписано (immutability); ниже — сводка расхождений. **Столбец «Статус» отличает измеренное от предполагаемого — это существенно:** часть первой редакции ревизии выдавала гипотезы за факты и исправлена в тот же день.

| Утверждение ADR-066 | Что установлено | Статус | Где адресовано |
|---|---|---|---|
| §3: «отказ списания больше не оставляет прогон в `running`» → читалось как полное устранение вечного `running` | Верно **только при живом relay**: весь §3 исполняется внутри обработчика события в `stream_events`; без подписчика `run.completed` не обрабатывается, `_mark_terminal` не вызывается → `running` навсегда (наблюдение: >15 мин после завершения) | **Факт** (наша БД, наш код) | §3 (маркер), [ADR-067 §0–§2, §5–§6](ADR-067-agent-run-background-consumer.md) |
| §8 / [Q-047-2](../99-open-questions.md): «повторная подписка на `/events` довыполнит debit» | Негодна как гарантия: требует действия клиента, которого не происходит — 3 прогона, 0 списаний, `trialRemaining`=1 | **Факт** (наша БД) | §8 (маркер), [ADR-067 §0](ADR-067-agent-run-background-consumer.md) |
| §Consequences: «`GET /state` → переподключиться к `/events` и догнать» | Тот же дефект: гарантия построена на действии клиента | **Факт** | §Consequences (маркер) |
| §6: «Hermes при переподключении реплеит буфер **с начала**» | **Неверно для переподключения** — при нём не приходит ничего (факт). Вариант «первый подписчик получает историю» (H4) **согласуется с наблюдением, но отдельной пробой не упражнялся**: момент открытия первой подписки относительно первых событий в артефактах не зафиксирован | **Факт** (про переподключение) + **H4 не упражнялась** | §6 (маркер), [Q-066-1](../99-open-questions.md), [ADR-067 §Непроверенные внешние допущения](ADR-067-agent-run-background-consumer.md) |
| §6: семантика replay — «с начала» либо «только новые» | **Ни то, ни другое: поток ОДНОРАЗОВЫЙ.** Историю получает первый подписчик; повторная подписка не получает **ни реплея, ни новых событий** — проверено на заведомо живом прогоне | **Факт** (прод-перемер 2026-07-30, воспроизводится) | [Q-066-1](../99-open-questions.md) **Closed**, [ADR-067 §Context](ADR-067-agent-run-background-consumer.md) |
| §Alternatives 1: фоновый consumer «независимый от клиента» | Заменён на broker. Обоснование «параллельный consumer физически невозможен» **снято 2026-07-29 как недоказанное** (требовало H2) и **восстановлено 2026-07-30 на другом основании** — прямом замере односторонности потока; H2 при этом утратила предмет | **Факт** (перемер 2026-07-30); сам выбор broker'а делался по основаниям, не зависящим от образа ([ADR-067 §0](ADR-067-agent-run-background-consumer.md)) | §Alternatives 1 (маркер), [Q-066-1](../99-open-questions.md), [Q-067-5](../99-open-questions.md) |
| §Alternatives 5: «Redis в стеке нет» | **Redis 7 в стеке есть** ([ADR-001](ADR-001-stack-choice.md), `rate_limit.py`, `/ready`); вывод альтернативы остаётся верным по другой причине (durability) | **Факт** (наш репозиторий) | §Alternatives 5 (маркер) |

**Масштаб (замер 2026-07-28/29, наша БД):** 3 реальных прогона 3 пользователей (usage `6294/56`, `6306/883`, третий) → **0 списаний кредитов**, `trialRemaining` = `1` у всех троих, прогон #1 в статусе `running` >15 минут после фактического завершения.

**Что в ADR-066 осталось в силе полностью:** контракт `GET /v1/agent/runs/{runId}/state`, схема `agent_run_snapshots` и миграция `0019`, маппинг статусов server→client (§4), правила апсерта (§6: per-column guard, троттлинг, head-preserving truncation, tenancy-гвард), retention-sweep (§7), read-only-инварианты эндпоинта (§5). [ADR-067](ADR-067-agent-run-background-consumer.md) меняет **исполнителя** записи, а не её правила, и не требует миграции.

**Дефект парсинга `message.delta` (сопутствующий, вне этого ADR).** Прод показал `resultText` тождественно пустым у всех трёх прогонов: docs описывали полезную нагрузку неточно, фактическая форма патченого образа — поле `delta` со **значением-строкой** ([modules/agent-proxy/05-events.md](../modules/agent-proxy/05-events.md)). Правила §6 при этом корректны — не работал только извлекатель. Побочно дефект сделал непригодным дискриминатор процедуры замера [Q-066-1](../99-open-questions.md) (`char_length(resultText)`), из-за чего перемер обязателен. Фикс — backend, отдельно и **до** перемера.
