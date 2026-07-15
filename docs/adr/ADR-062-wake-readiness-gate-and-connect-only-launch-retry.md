# ADR-062 — Readiness-gate на wake-пути и connect-only retry запуска run

- Статус: Accepted
- Дата: 2026-07-15
- Расширяет / уточняет: [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md) §1/§2 (readiness-gate провижининга; распространяется на wake-путь), [ADR-045](ADR-045-hermes-as-agent-proxy.md) §2/§6 (`_launch_run` `POST /v1/runs`, `502` при недоступности инстанса). Тела ADR-056 и ADR-045 не переписаны (immutability) — контракт wake-readiness и launch-retry уточняется здесь.
- Связан с: [ADR-046](ADR-046-per-user-hermes-runtime.md) §1/§5 (lifecycle wake/hibernate), [ADR-054](ADR-054-trial-claim-reconcile.md) (транзакционный инвариант: commit освобождает xact-scoped state — нельзя держать row-lock через длинную операцию), [ADR-003](ADR-003-byok-envelope-encryption.md) (envelope-decrypt `API_SERVER_KEY` для health Bearer), [TD-031](../100-known-tech-debt.md) (stale `provisioning`), [modules/hermes-runtime/](../modules/hermes-runtime/README.md), [modules/agent-proxy/](../modules/agent-proxy/README.md)
- needs_code_sync:
  - `src/app/hermes_runtime/manager.py::ensure_running` — ветка `status == "stopped"` (wake): распространить readiness-gate ADR-056 §1 на wake (сейчас `start` + немедленный `mark_running` БЕЗ readiness-poll — код расходится с уже задокументированным потоком `S → readiness-poll → mark_running`, `modules/hermes-runtime/03-architecture.md` mermaid). Коротко-транзакционный паттерн: `T=now(UTC)` → `start` → `mark_provisioning(user_id, provisioning_started_at=T)` → commit (арбитр) → `_wait_for_ready` → `mark_running`/**условный** re-hibernate `mark_stopped_if_provisioning(user_id, T)` (§1a/§1b).
  - `src/app/hermes_runtime/manager.py::_is_stale_provisioning` (строки 277-288) — анкеровать возраст на `row.provisioning_started_at` (fallback `created_at` для defensive), а НЕ на `created_at` (§1a); обновить докстринг.
  - `src/app/hermes_runtime/registry.py` — (1) `create_provisioning(...)` — дополнительно выставлять `provisioning_started_at=now()`; (2) новый `mark_provisioning(user_id, provisioning_started_at)` (перевод `stopped → provisioning`, сохранить `container_id`/`endpoint`/`port`, выставить `provisioning_started_at`); (3) новый `mark_stopped_if_provisioning(user_id, provisioning_started_at) -> int` (условный `UPDATE … SET status='stopped' WHERE user_id AND status='provisioning' AND provisioning_started_at=:T`, вернуть rowcount).
  - `src/app/models/tables.py::HermesInstance` — новая колонка `provisioning_started_at: Mapped[datetime|None]` (TIMESTAMPTZ, nullable).
  - `migrations/versions/` — **новая миграция `0017`** (down_revision=`0016_audit_logs_append_only`, head на текущий момент — проверить фактически): `ALTER TABLE hermes_instances ADD COLUMN provisioning_started_at TIMESTAMPTZ NULL`; backfill существующих `provisioning`-строк `= created_at` (сохранить текущую stale-семантику для in-flight строк на деплое). Expand-only.
  - `src/app/agent_proxy/service.py::_launch_run` — connect-only retry `POST /v1/runs` (idempotency-safe: только фаза установки соединения) перед `502`.
  - `src/app/config.py` — новые поля `hermes_launch_retry_attempts`, `hermes_launch_retry_backoff_seconds`.

## Context

Прод-инцидент: `POST /v1/agent/run` периодически (~каждый 3–4-й запрос после простоя) отдаёт `502`; повтор через ~минуту проходит. Диагностика по логам:

1. Reaper усыпляет idle-контейнер: `stop_idle(HERMES_IDLE_TIMEOUT_SECONDS=1800)`, тик каждые `300`с → `docker stop` (лог контейнера: `signal=SIGTERM parent_name=s6-supervise`). Registry-строка → `stopped`.
2. Следующий запрос: `ensure_running` → ветка `stopped` (`manager.py`): `RuntimeBackend.start` (`docker start`) → **сразу** `mark_running` → возврат endpoint.
3. Agent Proxy `_launch_run` шлёт `POST {base}/v1/runs` → `httpx` transport error (`hermes run launch transport error`) → `UpstreamError` → **`502`**.

### Корень — readiness-gate НЕ распространён на wake-путь (код ↔ docs mismatch)

ADR-056 ввёл readiness-gate (poll `GET /health` до `200` перед `mark_running`), но реализовал его **только** в `_provision_locked` (cold-start провижининга). Ветка wake (`stopped → running`) в `ensure_running` осталась **без gate**: `start` → немедленный `mark_running`. При этом уже задокументированный поток `modules/hermes-runtime/03-architecture.md` (mermaid) показывает `S[RuntimeBackend.start] → RW[readiness-poll health=200] → mark_running` — то есть **docs описывают gated-wake, а код его не реализует**. Это расхождение и есть корень: после `docker start` внутренний `api_server` Hermes (s6-overlay: remap uid/gid, seed, запуск aiohttp-listener) ещё не слушает `:8642`, а `mark_running` уже выставлен и Agent Proxy бьёт `POST /v1/runs` в неподнятый сокет → connect-фаза падает → transport error → `502`.

### Проверка сигнала готовности: `/health` достаточен

Диагностический тезис «`/health` отвечает `200` раньше, чем готов `/v1/runs`» проверен по коду образа Hermes (`gateway/platforms/api_server.py`): **все** маршруты (`/health`, `/v1/health`, `/v1/runs`, `/v1/runs/{id}/events`, …) регистрируются на ОДНОМ aiohttp-`router` и **до** старта TCP-listener (`add_get("/health", …)` … `add_post("/v1/runs", …)` → затем `AppRunner.setup()` → `TCPSite.start()`). Следовательно `/health=200` ⟺ listener поднят ⟺ `/v1/runs` уже маршрутизируем. Прод-симптом — именно **transport error** (connect-фаза), а не `5xx` от готового хендлера, что подтверждает: причина — отсутствие gate на wake (запрос уходит ДО `TCPSite.start()`), а не «лживый `/health`». Поэтому reuse `health(endpoint, api_key)` как сигнала готовности корректен; отдельный «api_server-warmup»-эндпоинт не нужен (пересмотр — [Q-062-1](../99-open-questions.md), если после фикса появятся post-gate `5xx`, а не transport-ошибки).

### Идемпотентность `POST /v1/runs` — retry небезопасен после отправки тела

`_handle_runs` образа Hermes **создаёт run на каждый вызов**, без idempotency-ключа (нет дедупа по клиентскому ключу; проверено по коду). Значит слепой retry на любой transport-ошибке рискует создать **дубль run** (двойной прогон/двойной биллинг). Retry допустим только когда запрос **гарантированно не дошёл** — на фазе установки соединения.

## Decision

Комбинация **B (корень) + A (страховка)**; **C отклонён** как не устраняющий корень.

### 1. Направление B — readiness-gate на wake-пути (`ensure_running`, ветка `stopped`)

Распространить readiness-gate ADR-056 §1 на wake, приведя код к уже задокументированному потоку. Поток (коротко-транзакционный, инвариант ADR-054/ADR-056 §«транзакционный инвариант»):

0. `T := now(UTC)` (метка НАЧАЛА этой wake-provisioning-попытки; генерируется в приложении — детерминизм/тестируемость и общий источник для якоря и guard'а cleanup'а).
1. `get_for_update(user_id)` → row `stopped` (row-lock held).
2. `RuntimeBackend.start(container_ref)` (`docker start`; возвращается по запуску процесса, НЕ по готовности `api_server`).
3. `registry.mark_provisioning(user_id, provisioning_started_at=T)` → `stopped → provisioning`, сохранив `container_id`/`endpoint`/`port`, и **выставив `provisioning_started_at=T`** (перезапуск stale-якоря — §1a).
4. **`session.commit()`** → освобождает row-lock; `provisioning`-строка становится арбитром гонки (конкурентный `ensure_running` видит свежую `provisioning` → ждёт `running` через существующий `_await_concurrent_ready`, НЕ перепровижинит, НЕ бьёт в неготовый инстанс).
5. `_wait_for_ready(endpoint, api_key)` — poll `health` до `200`, **без удержания DB** (бюджет `HERMES_PROVISION_READY_TIMEOUT_SECONDS`, интервал `HERMES_PROVISION_READY_INTERVAL_SECONDS`, проба под `HERMES_HEALTH_TIMEOUT_SECONDS` — те же, что для провижининга; отдельные знобы не вводятся).
6. `health=200` → `registry.mark_running(...)` + `commit` → вернуть `InstanceEndpoint`.
7. **Таймаут** → **условный** wake-cleanup (guard по идентичности попытки, §1b): `registry.mark_stopped_if_provisioning(user_id, provisioning_started_at=T)` = `UPDATE … SET status='stopped' WHERE user_id=:id AND status='provisioning' AND provisioning_started_at=:T`.
   - **rowcount=1** (мы всё ещё владеем этой provisioning-попыткой): `RuntimeBackend.stop(container_ref)` best-effort (честное `stopped`; том/память сохранены; `remove` НЕ делаем — контейнер валиден) → `commit` → `UpstreamError`/`502`.
   - **rowcount=0** (строкой уже завладел другой актор — replay поднял НОВЫЙ `running`-контейнер, либо идёт новая provisioning-попытка): НЕ трогаем ни строку, ни контейнер (иначе перезатрём/остановим чужой здоровый инстанс — MAJOR) → `rollback` → `UpstreamError`/`502`.
   Следующий запрос делает чистый повторный wake/ожидание.

**Почему НЕ держать row-lock через poll:** удержание `FOR UPDATE` до `HERMES_PROVISION_READY_TIMEOUT_SECONDS` (90с) заблокировало бы конкурентные same-user запросы и занимало бы соединение пула на всё окно — тот же класс дефекта, что xact-scoped lock в [ADR-054](ADR-054-trial-claim-reconcile.md) (MAJOR-4) и обоснование короткой транзакции в [ADR-056 §«транзакционный инвариант»](ADR-056-provision-readiness-gate-and-volume-ownership.md). Поэтому commit `provisioning`-арбитра ДО poll — обязателен.

#### §1a. Перезапуск stale-якоря на wake — обязателен (иначе арбитр гонки сломан)

`_is_stale_provisioning` считает возраст строки от timestamp-якоря. **До этого ADR** якорь = `created_at`, который выставляется ТОЛЬКО при INSERT (`create_provisioning`) и НЕ двигается `mark_running`/`mark_stopped`/`touch_active`. Для разбуженного инстанса (`provision → running → stopped → wake`, часы/сутки спустя) `created_at` — время ИСХОДНОГО провижининга. Если wake переводит строку в `provisioning`, НЕ обновив якорь, то `age = now − created_at ≫ HERMES_PROVISIONING_STALE_SECONDS(120)` → живая wake-попытка **немедленно** признаётся stale: конкурентный `ensure_running` (stale-проверка стоит ПЕРЕД веткой свежей `provisioning`) уходит в `_replay_stale_provisioning` → `RuntimeBackend.remove` сносит контейнер, который in-flight wake прямо сейчас поллит, + перепровижининг с нуля. Без фикса: (1) арбитр гонки недостижим (конкурентный caller никогда не попадёт в `_await_concurrent_ready`); (2) инвариант «живой wait ≤ ready < stale» — ложен (якорь про исходное создание, не про текущую попытку).

**Решение — выделенная колонка `provisioning_started_at` (вариант B, миграция `0017`), НЕ reuse `created_at`:** `_is_stale_provisioning` анкорится на `provisioning_started_at`; `create_provisioning` (cold-start) и `mark_provisioning` (wake) выставляют её `= now()` начала попытки; `created_at` остаётся **иммутабельным** временем создания инстанса. Почему не reuse `created_at` (вариант A, без миграции): (1) `created_at` семантически = «инстанс создан» — сдвиг на каждый wake создаёт латентную ловушку для любого будущего admin-view/метрики/отладки (сейчас внешних потребителей `hermes_instances.created_at` нет — единственный потребитель `_is_stale_provisioning` — но перегрузка смысла хрупка); (2) собственный докстринг `_is_stale_provisioning` явно опирается на «`created_at` — время вставки, никогда не двигается, в отличие от `last_active_at`» — reuse сделал бы этот инвариант ложным. Выделенная колонка самодокументируема («когда началась ТЕКУЩАЯ provisioning-попытка») и делает инвариант stale истинным. Миграция — expand-only, дешёвая и рутинная для этого репозитория.

#### §1b. Условный wake-cleanup — обязателен (иначе clobber чужого `running`)

Безусловный `mark_stopped` (`UPDATE … WHERE user_id`) на wake-таймауте некорректен: при гонке (пока wake таймаутит, конкурентный replay/provision уже поднял НОВЫЙ контейнер и пометил `running`) поздний `mark_stopped` перезатрёт свежий `running → stopped` → здоровый контейнер запущен в docker, но `stopped` в registry → ресурс-лик + лишний wake. Поэтому cleanup **условен** по идентичности попытки (`status='provisioning' AND provisioning_started_at=T`, шаг 7): пишем/останавливаем ТОЛЬКО если строка всё ещё та provisioning-попытка, что застолбил этот wake; иначе — не трогаем (владелец сменился). Guard по `provisioning_started_at=T` строже, чем `status='provisioning'` в одиночку: он отсекает и кейс, где после replay идёт УЖЕ ДРУГАЯ provisioning-попытка (иной `T`).

**Согласование с TD-031 / инвариантом stale>ready:** wake-`provisioning` ждёт ≤ `HERMES_PROVISION_READY_TIMEOUT_SECONDS` (90с) < `HERMES_PROVISIONING_STALE_SECONDS` (120с) — живой wait (якорь `provisioning_started_at=T`, §1a) не будет ошибочно признан stale. Статус `provisioning` теперь означает «контейнер стартует/ждём readiness (provision ИЛИ wake)»; stale-реплей (crash-остаток старше порога) корректно `remove`+`provision` по `container_id` (том сохраняется). CHECK-constraint `HERMES_INSTANCE_STATUS=(provisioning,running,stopped)` НЕ меняется — новый статус не вводится; миграция `0017` добавляет только колонку `provisioning_started_at` (expand-only).

### 2. Направление A — connect-only retry `_launch_run` (страховка)

В `agent_proxy._launch_run` при transport-ошибке `POST /v1/runs` — до `HERMES_LAUNCH_RETRY_ATTEMPTS` попыток (дефолт `3` — т.е. до 2 доп. попыток) с backoff `HERMES_LAUNCH_RETRY_BACKOFF_SECONDS` (дефолт `2.0`с), **только если ошибка на фазе установки соединения** (запрос гарантированно не дошёл до сервера). Классификация `httpx` (см. `idempotency_analysis`):

- **Retry (safe, connect-фаза, тело НЕ отправлено):** **явный кортеж** `(httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)`.
- **НЕ retry (post-send, run мог быть создан → дубль):** `httpx.WriteError`/`WriteTimeout`, `httpx.ReadError`/`ReadTimeout`, `httpx.RemoteProtocolError`, прочие `httpx.HTTPError` → сразу `UpstreamError`/`502`.
- **Non-2xx ответ** (сервер ответил) → retry НЕ делается (детерминированный исход) → `UpstreamError`/`502`, как сейчас.

> ⚠️ **Классификация ТОЛЬКО явным кортежем — НЕ по базовому классу.** В используемой версии `httpx` иерархия исключений НЕ позволяет ловить safe-набор через общего предка: `ConnectTimeout` — подкласс `TimeoutException`+`TransportError`, но **НЕ** `ConnectError`; `PoolTimeout`/`ReadTimeout`/`ConnectTimeout` делят базу `TimeoutException`; `ConnectError`/`WriteError`/`ReadError` делят базу `NetworkError`. Значит `except httpx.ConnectError` НЕ поймает `ConnectTimeout` (пропустит безопасный кейс), а `except httpx.TimeoutException`/`httpx.NetworkError`/`httpx.TransportError` захватит `ReadTimeout`/`WriteError` (post-send) → риск дубль-run. Ловить строго `isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))`, иначе — re-raise как `UpstreamError`.

Retry — defense-in-depth: покрывает остаточную гонку (короткое окно между `docker start` и `TCPSite.start()`, если readiness-gate почему-то пропустил пробу) и транзиентный connect-blip, НЕ создавая дублей. Контракт `POST /v1/agent/run` для клиента не меняется (успех → `202`/`run_id`; исчерпание connect-retry → `502`).

### 3. Направление C — тюнинг idle-timeout (отклонено как корневое)

Поднятие `HERMES_IDLE_TIMEOUT_SECONDS` лишь снижает частоту wake-gap, не устраняя корень; не принимается в этом ADR (значение остаётся `1800`). При необходимости — операционная настройка env, не архитектурное решение.

## Consequences

**Положительные:**
- Wake-путь после гибернации ждёт реальной готовности `api_server` → устранён основной источник `502` «каждый 3–4-й запрос».
- Код приведён в соответствие с уже задокументированным потоком `03-architecture.md` (закрыт docs↔code mismatch).
- Нет неконсистентной `running`-строки на неготовом разбуженном контейнере; при таймауте wake — честное `stopped` (re-hibernate) с **условным** guard'ом (§1b) → не клоббит конкурентно поднятый `running`, без ресурс-лика.
- Stale-арбитр корректен для wake: якорь `provisioning_started_at` (§1a) отражает начало ТЕКУЩЕЙ попытки → живой wake-wait не признаётся stale, `created_at` остаётся иммутабельным.
- Connect-only retry гасит остаточные транзиентные connect-ошибки БЕЗ риска дубль-run (idempotency соблюдена).

**Отрицательные / ограничения:**
- Первый запрос после гибернации блокируется до `health=200` (ожидаемая wake-латентность, ограничена бюджетом) — «медленный успех» вместо «быстрого `502`».
- Wake-`provisioning` добавляет коротко-транзакционный commit-цикл в ветку wake (было: один commit) — приемлемо, паттерн уже используется в `_provision_locked`.
- Требуется миграция `0017` (add-column `provisioning_started_at`, expand-only) — цена варианта B за семантическую чистоту `created_at` (см. Alternatives 6).
- Connect-retry увеличивает worst-case латентность неуспешного запуска на `(attempts-1)·backoff` (дефолт ≤ 4с) перед `502`.
- Остаётся допущение «`/health=200` ⟺ `/v1/runs` готов», валидное для текущего образа Hermes (единый listener, маршруты до `TCPSite.start()`). При смене контракта образа (пере-регистрация маршрутов после старта listener) потребуется более точный ready-signal — [Q-062-1](../99-open-questions.md).

## Alternatives

1. **Только A (retry на Agent Proxy), без B.** Отклонено: не чинит корень (неготовый инстанс), retry ограничен connect-фазой (иначе дубль-run), а после `TCPSite.start()` пропущенного gate запрос всё равно уходит в неготовый api-pipeline; размазывает readiness по двум модулям (тот же довод, что ADR-056 Alt-1).
2. **Retry на любой transport-ошибке (включая ReadTimeout/RemoteProtocolError).** Отклонено: `POST /v1/runs` не идемпотентен (нет ключа) → дубль-run/двойной биллинг. Только connect-фаза безопасна.
3. **Новый статус `waking` вместо reuse `provisioning`.** Отклонено: требует ALTER CHECK-constraint + правки reaper/stale-replay/`_await_concurrent_ready`; `provisioning` уже несёт нужную семантику «стартует/ждём readiness» и всю машинерию — reuse проще и ниже риск.
4. **Более глубокий ready-probe (`/v1/health`/`/health/detailed`/пробный `GET /v1/runs`).** Отложено: `/v1/health` = тот же `_handle_health`; `/health/detailed` читает status-файл gateway, не точнее для api-listener; пробный `POST/GET /v1/runs` создаёт/грузит run. `/health` достаточен (единый listener). Пересмотр — [Q-062-1](../99-open-questions.md).
5. **Docker `HEALTHCHECK`/`docker wait` на healthy.** Отложено (как в ADR-056 Alt-2): образ — публичный pinned, его `HEALTHCHECK` мы не контролируем.
6. **Reuse `created_at` как stale-якоря на wake (вариант A, без миграции).** Отклонено в пользу выделенной колонки `provisioning_started_at` (§1a): сдвиг `created_at` на каждый wake ломает его семантику «инстанс создан» (латентная ловушка для будущих admin-view/метрик/отладки) и делает ложным собственный инвариант докстринга `_is_stale_provisioning` («`created_at` никогда не двигается»). Хотя внешних потребителей `hermes_instances.created_at` сейчас нет (единственный — сам `_is_stale_provisioning`), перегрузка смысла хрупка; выделенная колонка самодокументируема и делает stale-инвариант истинным. Цена — дешёвая expand-only миграция `0017`.
7. **Безусловный `mark_stopped` на wake-таймауте.** Отклонено (MAJOR): при гонке (конкурентный replay/provision поднял новый `running`) перезатирает свежий `running → stopped` → здоровый контейнер `running`-в-docker при `stopped`-в-registry (ресурс-лик + лишний wake). Принят условный `mark_stopped_if_provisioning(user_id, T)` с guard'ом по идентичности попытки (§1b).
