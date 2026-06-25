# ADR-056 — Provision readiness-gate (cold-start) и согласование владения томом Hermes-инстанса

- Статус: Accepted
- Дата: 2026-06-24
- Расширяет / уточняет: [ADR-046](ADR-046-per-user-hermes-runtime.md) §1 (`provision`/`ensure_running` lifecycle), §3 (registry-статусы), §5 (reaper/гибернация), §6 (изоляция тома). Тело ADR-046 не переписано (immutability) — контракт readiness-gate и ownership уточняется здесь.
- Связан с: [ADR-055](ADR-055-hermes-instance-llm-config-contract.md) (config.yaml `model`-секция, fail-fast валидация — выполняется ДО `docker run`, т.е. до readiness-poll этого ADR), [ADR-045](ADR-045-hermes-as-agent-proxy.md) (agent-proxy `502` при недоступности инстанса), [ADR-003](ADR-003-byok-envelope-encryption.md) (envelope-decrypt `API_SERVER_KEY` для health Bearer), [TD-031](../100-known-tech-debt.md) (stale `provisioning` по возрасту), [05-security.md](../05-security.md), [07-deployment.md](../07-deployment.md), [modules/hermes-runtime/](../modules/hermes-runtime/README.md)
- needs_code_sync:
  - `src/app/hermes_runtime/manager.py::_provision_locked` — readiness-poll `health(endpoint, api_key)` ПОСЛЕ `docker run`, ДО `mark_running`; cleanup при таймауте; передача `HERMES_UID`/`HERMES_GID` в `RuntimeBackend.provision`; idempotent-write `config.yaml`.
  - `src/app/hermes_runtime/manager.py::ensure_running` — ветка «свежая `provisioning`-строка»: дождаться готовности (re-poll health / повторное чтение статуса) вместо немедленного прокси/реплея.
  - `src/app/hermes_runtime/docker_backend.py::DockerBackend.provision` — добавить env `HERMES_UID`/`HERMES_GID` в спецификацию контейнера; `health(endpoint, api_key)` (готов).
  - `src/app/config.py` — новые поля `hermes_provision_ready_timeout_seconds`, `hermes_provision_ready_interval_seconds`, `hermes_uid`, `hermes_gid`.

## Context

Живой e2e агентного пути `/v1/agent/run` (после фикса конфигурации LLM, [ADR-055](ADR-055-hermes-instance-llm-config-contract.md)) вскрыл два дефекта надёжности провижининга per-user Hermes-инстанса:

### Дефект A — cold-start readiness gap
`HermesInstanceManager.ensure_running` → `_provision_locked` выполняет `docker run` и **сразу** помечает строку `running` / возвращает endpoint; Agent Proxy ([ADR-045](ADR-045-hermes-as-agent-proxy.md)) проксирует `POST /v1/runs` **мгновенно**. Но Hermes-образ (~5.3 GB) бутится ~30–40 с (s6-overlay stage2: remap UID/GID, chown тома, seed config, sync skills → запуск `api_server`). До готовности `api_server` на `:8642` контрол-плейн получает «hermes instance unreachable» → `UpstreamError` → `502`. При этом registry-строка уже `running` (неконсистентна: контейнер жив, но не отвечает) и повторный запрос либо снова бьёт в неготовый инстанс, либо (при ином статусе) уходит в перепровижининг.

### Дефект B — volume ownership conflict
`docker_backend._provision_sync`: api-контейнер (non-root **uid 10001**, [05-security.md §Docker socket](../05-security.md#multi-tenant-изоляция-hermes-инстансов-adr-046-adr-045)) делает `os.makedirs(<HERMES_VOLUME_ROOT>/<user_id>)` + пишет `config.yaml`, затем `docker run` Hermes. Hermes-образ при старте (s6 stage2) `chown`'ит свой `/opt/data` (= bind-mount host-тома) на свой `HERMES_UID` (дефолт **10000**). → владелец host-директории тома становится `10000`; при **повторном** `provision` (reuse после гибернации/реплея/деплоя) api (uid 10001) уже не может перезаписать `config.yaml` → `PermissionError [Errno 13]`.

**Контракт образа Hermes (`Dockerfile`, реальный):** `useradd -u 10000 hermes`, `HERMES_HOME=/opt/data`, `VOLUME ["/opt/data"]`; «UID can be overridden via `HERMES_UID` at runtime»; контейнер стартует как `root`, s6 stage2-hook делает `usermod`/`groupmod` + `chown /opt/data` на `HERMES_UID`/`HERMES_GID`, затем сервисы дропают права на `hermes`. То есть образ поддерживает приведение владельца тома к произвольному UID/GID через env.

### Транзакционный инвариант registry (критично для исполнимости A)
`hermes_instances.user_id` — PK; гонко-безопасность `ensure_running` обеспечивается **не** долгоживущим row-lock'ом через всю операцию, а паттерном **короткой транзакции** `INSERT ... ON CONFLICT (user_id) DO NOTHING` + повторное чтение ([04-data-model.md §Инварианты](../modules/hermes-runtime/04-data-model.md), образец `auth_devices`). Строка `provisioning` коммитится **до** `docker run` и служит арбитром гонки (конкурентный `ensure_running` видит свежую `provisioning` → не перепровижинит). Это тот же класс инварианта, что сломал advisory-lock-подход в [ADR-054](ADR-054-trial-claim-reconcile.md) (commit освобождает xact-scoped lock до длинной операции). Поэтому readiness-poll **обязан** жить ПОСЛЕ commit'а `provisioning`-строки, а `mark_running` — отдельный поздний commit. Никакого «удержания lock'а через docker run + poll» — это было бы неисполнимо.

## Decision

### 1. Readiness-gate в `provision` (дефект A)

`_provision_locked` после успешного `docker run` (и ПОСЛЕ commit'а `provisioning`-строки — арбитра гонки) **поллит готовность** инстанса перед `mark_running`:

- **Где:** в `_provision_locked`, между `RuntimeBackend.provision(...)` (контейнер создан/запущен) и `registry.mark_running(...)`.
- **Что поллит:** `RuntimeBackend.health(endpoint, api_key)` — `GET http://hermes-user-<id>:8642/health` с `Authorization: Bearer <API_SERVER_KEY>` (расшифрованный, [ADR-003](ADR-003-byok-envelope-encryption.md); reuse существующего `health(endpoint, api_key)` — [02-api-contracts.md](../modules/hermes-runtime/02-api-contracts.md)). `endpoint`/`api_key` уже известны в `_provision_locked` (сгенерированы там же), отдельный envelope-decrypt не нужен.
- **Цикл:** интервал `HERMES_PROVISION_READY_INTERVAL_SECONDS` (дефолт `2`), общий бюджет `HERMES_PROVISION_READY_TIMEOUT_SECONDS` (дефолт `90` — заведомо больше штатного cold-start ~30–40 с с запасом). Каждая итерация: один `health`-вызов под индивидуальным таймаутом `HERMES_HEALTH_TIMEOUT_SECONDS` (существующий); ошибка/неготовность → ждать интервал и повторить, пока не исчерпан бюджет.
- **Порядок mark_running:** `registry.mark_running` (status `provisioning → running`, `last_active_at=now()`) вызывается **только после** `health=200`. До этого строка остаётся `provisioning` (контейнер жив, ждём health). Endpoint возвращается из `ensure_running` **после** `mark_running`.
- **Cleanup при таймауте:** health не стал `200` за бюджет → `_provision_locked` выполняет **откат**: `RuntimeBackend.remove(container)` (удалить неготовый контейнер) + `registry.deprovision`/пометка строки так, чтобы НЕ осталась неконсистентная `running`-строка и НЕ осталась «вечная» `provisioning`. Затем `UpstreamError` наверх → Agent Proxy `502`. Чистое состояние: после таймаута следующий `ensure_running` начинает провижининг заново (нет залипшего контейнера/строки). Том сохраняется (память/навыки не теряются); удаляется только контейнер.
- **Семантика статуса `provisioning`:** теперь = «контейнер запущен, ждём health». Это согласовано с [TD-031](../100-known-tech-debt.md) (см. §3).

### 2. `ensure_running` на «живом-но-`provisioning`» (дефект A, конкурентный путь)

`ensure_running`, обнаружив **свежую** `provisioning`-строку (моложе `HERMES_PROVISIONING_STALE_SECONDS`, [TD-031](../100-known-tech-debt.md)) — это конкурентный провижининг другого запроса, который сейчас в readiness-poll:

- **НЕ** перепровижинит и **НЕ** проксирует немедленно. Вместо этого **ждёт готовности**: повторно читает статус строки (перешла ли в `running`) и/или поллит `health(endpoint, api_key)` под тем же бюджетом `HERMES_PROVISION_READY_TIMEOUT_SECONDS`, пока строка не станет `running` (→ вернуть endpoint) либо бюджет не исчерпан (→ `UpstreamError`/`502`; параллельный provisioner сам сделает cleanup по своему таймауту).
- Это устраняет окно, где второй запрос бил в неготовый инстанс. Реализация идемпотентна: повторный `ensure_running` не создаёт второй контейнер (PK + `provisioning`-арбитр), не дублирует readiness-логику деструктивно (cleanup делает только владелец `_provision_locked`, по своему таймауту).

### 3. Согласование с TD-031 (stale `provisioning`)

Readiness-wait меняет нормальную длительность жизни `provisioning`-строки (теперь до `~HERMES_PROVISION_READY_TIMEOUT_SECONDS`, а не «миг между create и mark»). Инвариант **`HERMES_PROVISIONING_STALE_SECONDS` > `HERMES_PROVISION_READY_TIMEOUT_SECONDS`** обязателен, иначе reaper/`ensure_running` признает живой readiness-wait stale и устроит конкурентный реплей. Дефолты это соблюдают: stale `120` > ready `90`. **Зафиксировано как инвариант конфигурации** (валидируется в `config.py`: при `stale ≤ ready` — fail-fast на старте, [Q-056-1](../99-open-questions.md) — стоит ли делать stale производным от ready). Семантика stale-реплея ([TD-031](../100-known-tech-debt.md)) сохраняется для строк, переживших краш процесса (старше stale-порога) — это ортогонально readiness-wait (тот живёт в пределах ready-бюджета < stale-порога).

### 4. Согласование владения томом (дефект B): вариант (1) + (3)

**(1) Приведение `HERMES_UID`/`HERMES_GID` к uid/gid api-процесса.** `docker_backend.provision` прокидывает Hermes-контейнеру env **`HERMES_UID`/`HERMES_GID` = uid/gid процесса api** (по умолчанию **10001/10001**, [05-security.md](../05-security.md)). s6 stage2 образа `usermod`/`groupmod` + `chown /opt/data` на этот UID/GID → владелец host-директории тома совпадает с пользователем, который пишет `config.yaml` → `PermissionError` при reuse устранён by construction. Значения берутся из backend-конфига (`HERMES_UID`/`HERMES_GID`, дефолт `10001`), **не** определяются рантайм-интроспекцией процесса (детерминизм, тестируемость; должны совпадать с uid/gid api-контейнера из `docker-compose`).

**(3) Idempotent-write `config.yaml`.** При reuse (повторный `provision` для существующего тома) `config.yaml` **не перезаписывается**, если уже существует и валиден (непустой, парсится как YAML, содержит обязательные секции `platform_toolsets.api_server` + `model.default`/`model.provider`). Перезапись `config.yaml` — только при первом провижининге (файла нет) либо явном `deprovision`+`provision` (full replay, [TD-031](../100-known-tech-debt.md)). Это снимает зависимость reuse-пути от прав на запись и делает provision идемпотентным по тому. Невалидный/повреждённый существующий `config.yaml` → перезаписать (recovery), что благодаря (1) теперь возможно по правам.

**Почему (1)+(3), а не (2):** (2) «групповые права + общий gid» решает только запись, но оставляет двух разных владельцев (10000 vs 10001) и не делает provision идемпотентным; (1) убирает рассинхрон владельца в корне, (3) убирает лишнюю перезапись. Вместе — детерминированное единое владение + idempotent provision.

## Consequences

**Положительные:**
- `/v1/agent/run` после cold-start не отдаёт `502` на гонке «контейнер поднят, api ещё не слушает» — control plane ждёт реальной готовности.
- Нет неконсистентной `running`-строки при неготовом контейнере (mark_running строго после health; cleanup при таймауте).
- Единое владение томом (uid/gid api = HERMES_UID/GID) → reuse-`provision` не падает на `PermissionError`; idempotent-write убирает лишнюю перезапись.
- Конкурентный `ensure_running` ждёт готовности вместо двойного провижининга/удара в неготовый инстанс.

**Отрицательные / ограничения:**
- Первый запрос после cold-start/пробуждения блокируется до `health=200` (до `HERMES_PROVISION_READY_TIMEOUT_SECONDS`) — ожидаемая латентность cold-start (раньше была «быстрый `502`», теперь «медленный успех»). Бюджет ограничивает худший случай.
- Инвариант `stale > ready` — конфигурационная связанность двух env (валидируется fail-fast).
- `HERMES_UID`/`HERMES_GID` должны совпадать с фактическим uid/gid api-контейнера; рассинхрон конфигов (compose vs env) вернёт дефект B ([Q-056-2](../99-open-questions.md) — выводить ли из одного источника).
- При смене uid api-контейнера для **существующих** томов (созданных под старым владельцем) первый `provision` под новым `HERMES_UID` исправит владельца (stage2 chown) — миграции данных не требуется (том = данные пользователя, владелец перезатирается образом).

## Alternatives

1. **Оставить мгновенный mark_running + ретраи на стороне Agent Proxy.** Отвергнуто: размазывает readiness-логику по двум модулям, не чинит неконсистентную `running`-строку, повторные `502` видны клиенту.
2. **Readiness через Docker healthcheck контейнера (`HEALTHCHECK`/`docker wait` на healthy).** Отложено: образ Hermes — публичный pinned (мы не управляем его `HEALTHCHECK`); прикладной `GET /health` под Bearer — точный сигнал готовности именно `api_server`, уже доступен (`health(endpoint, api_key)`). Пересмотр при появлении контрактного healthcheck в образе.
3. **Вариант B-(2) (общий gid + групповые права на `config.yaml`).** Отвергнуто как единственное решение — см. «Почему (1)+(3)».
4. **Не трогать UID (B): писать `config.yaml` под root через временный helper-контейнер.** Отвергнуто: усложняет (доп. контейнер на каждый provision), требует root-операций на томе, не делает provision идемпотентным; (1)+(3) проще и детерминированнее.
