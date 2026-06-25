# ADR-046 — Per-user Hermes runtime: жизненный цикл Docker-инстансов, гибернация, registry

- Статус: Accepted
- Дата: 2026-06-23
- Связан с: [ADR-045](ADR-045-hermes-as-agent-proxy.md) (agent-proxy), [ADR-003](ADR-003-byok-envelope-encryption.md) (KMS для `api_key_enc`), [ADR-047](ADR-047-usage-based-billing-for-agent.md) (биллинг), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (deploy-топология), [01-architecture.md](../01-architecture.md), [03-data-model.md](../03-data-model.md), [05-security.md](../05-security.md), [07-deployment.md](../07-deployment.md), [modules/hermes-runtime/](../modules/hermes-runtime/README.md)
- Контракт данных: новая таблица `hermes_instances` (миграция `0013`)
- **Ревизия 2026-06-24 → [ADR-055](ADR-055-hermes-instance-llm-config-contract.md):** §1 (env-проброс) и §5/§6 (рендер `config.yaml` тома) уточнены — реальный e2e вскрыл дефект конфигурации LLM инстанса (401, openrouter-дефолт). Актуальный контракт `config.yaml model.*` + env-набор + валидация провайдера — в [ADR-055](ADR-055-hermes-instance-llm-config-contract.md). Тело ниже не переписано (immutability); в части `LLM_MODEL`/`LLM_PROVIDER` env и отсутствия `model`-секции — **устарело**, см. ADR-055.
- **Уточнение 2026-06-24 → [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md):** §1 `provision`/`ensure_running` (cold-start), §3 (статусы), §5 (reaper) и §6 (владение томом) уточнены — живой e2e `/v1/agent/run` вскрыл два дефекта надёжности: **(A)** `mark_running` сразу после `docker run` → прокси в неготовый `api_server` → `502`; **(B)** конфликт владельца тома (api uid 10001 vs Hermes `chown` на uid 10000) → `PermissionError` при reuse-`provision`. Актуальный контракт readiness-gate (poll `health` до `mark_running`, cleanup при таймауте, ожидание конкурентным `ensure_running`) + согласование владения (env `HERMES_UID`/`HERMES_GID` = uid api + idempotent-write `config.yaml`) — в [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md). Тело ниже не переписано (immutability): «`docker run` и СРАЗУ возвращает endpoint» в §1 и порядок mark — **уточнены** ADR-056.

## Context

[ADR-045](ADR-045-hermes-as-agent-proxy.md) проксирует чат к персональному Hermes-инстансу пользователя. Решение пользователя — **сразу полная per-user оркестрация**: Docker-инстанс Hermes на пользователя + гибернация (стоп простаивающих, пробуждение по запросу) + адресация в docker-сети + жизненный цикл.

Контракт запуска инстанса Hermes (бери как есть): Docker-образ Hermes, том `/opt/data` = `HERMES_HOME`, env `API_SERVER_ENABLED=true`, `API_SERVER_KEY` (≥16 симв., уникальный на инстанс), `API_SERVER_HOST=0.0.0.0`, `API_SERVER_PORT=8642`, `LLM_PROVIDER`/`*_API_KEY`/`LLM_MODEL`; команда `gateway run`. Один контейнер + том на пользователя; порт **не публикуется** на хост (доступ только из docker-сети control plane); адресация по DNS-имени контейнера (`hermes-user-<id>:8642`). Ограничение инструментов — через `config.yaml` в томе (`platform_toolsets.api_server`).

## Decision

### 1. Модуль `src/app/hermes_runtime/`

Новый пакет (внутренний модуль монолита):
- **`manager.py` — `HermesInstanceManager`** (оркестрация жизненного цикла):
  - `ensure_running(user_id) -> InstanceEndpoint(base_url, api_key)` — найти в registry; контейнера нет → `provision`; остановлен → разбудить (`docker start`); обновить `last_active_at`; вернуть endpoint (`hermes-user-<id>:8642` + расшифрованный `API_SERVER_KEY`).
  - `provision(user_id)` — `docker run` образа Hermes: смонтировать том `HERMES_HOME` пользователя, сгенерировать уникальный `API_SERVER_KEY` (CSPRNG, ≥16 симв.), задать env (`API_SERVER_ENABLED=true`, `API_SERVER_HOST=0.0.0.0`, `API_SERVER_PORT=8642`, `LLM_PROVIDER`/ключ/`LLM_MODEL`), записать в том `config.yaml` с ограниченным toolset (§5), подключить к выделенной docker-сети control plane, **без проброса host-портов**; записать строку `hermes_instances` (`api_key_enc` — зашифрован, §4).
  - `stop_idle(threshold)` — останавливать контейнеры с `last_active_at` старше порога (`HERMES_IDLE_TIMEOUT_SECONDS`); `status → stopped`.
  - `deprovision(user_id)` — удалить контейнер; том сохраняется по политике (память/навыки пользователя не теряются при ребуте/пересоздании).
  - `health(user_id)` — пробинг `GET /health` инстанса.
- **`docker_backend.py`** — обёртка над Docker SDK (docker-py, кандидат — [02-tech-stack.md](../02-tech-stack.md)), реализующая абстрактный интерфейс `RuntimeBackend`.
- **`registry.py`** — репозиторий поверх таблицы `hermes_instances` (§3).

### 2. Расширяемый `RuntimeBackend` (задел под Modal/Daytona)

Интерфейс `RuntimeBackend` (по образцу `KmsClient` [ADR-003](ADR-003-byok-envelope-encryption.md) / `LLMClient` [ADR-033](ADR-033-llm-provider-abstraction.md)): `provision`/`start`/`stop`/`remove`/`health`. На MVP — реализация `DockerBackend` (docker-py). Будущие бэкенды (Modal/Daytona — у Hermes есть `tools/environments/{modal,daytona}.py`) подключаются в тот же интерфейс без изменения `manager.py`/`registry.py`. Выбор бэкенда — config (резерв; MVP фиксирует docker).

### 3. Registry — таблица `hermes_instances` (миграция `0013`)

Контракт данных (DDL — [03-data-model.md](../03-data-model.md) §22):
- `user_id` (PK, FK `users(id)` ON DELETE CASCADE) — один инстанс на пользователя.
- `container_id` (TEXT) — id Docker-контейнера.
- `endpoint` (TEXT) — DNS-имя:порт в docker-сети (`hermes-user-<id>:8642`).
- `api_key_enc` (BYTEA + сопутствующие envelope-поля) — зашифрованный `API_SERVER_KEY` (§4).
- `status` (TEXT/enum) ∈ `provisioning|running|stopped`.
- `port` (INT, nullable) — на старте порт не публикуется (резерв для альтернативных бэкендов).
- `last_active_at` (TIMESTAMPTZ) — для гибернации (`stop_idle`).
- `created_at` (TIMESTAMPTZ).

Адресация — по `endpoint` (DNS контейнера), не по host-порту.

### 4. Шифрование per-instance `API_SERVER_KEY` at-rest (reuse [ADR-003](ADR-003-byok-envelope-encryption.md))

- `API_SERVER_KEY` каждого инстанса — секрет, шифруется envelope-схемой через существующий `byok.kms` (`KmsClient`, `LocalKmsClient` на MVP, [ADR-003](ADR-003-byok-envelope-encryption.md)): случайный DEK → AES-256-GCM(key) → `api_key_enc`+`nonce`; DEK → KMS → `encrypted_dek`. Plaintext ключ — только in-memory на время прокси-вызова ([ADR-045](ADR-045-hermes-as-agent-proxy.md)), затем обнуляется; в логи не попадает (redaction `*key*`).
- Переиспользуется тот же интерфейс/мастер-ключ, что и BYOK — без новой криптоинфраструктуры.

### 5. Гибернация и фоновый reaper

- Простаивающие контейнеры (`last_active_at` старше `HERMES_IDLE_TIMEOUT_SECONDS`) останавливаются (`stop_idle`); том сохраняется. Следующий `POST /v1/agent/run` будит контейнер (`ensure_running` → `docker start`).
- **Фоновый reaper** — задача в `lifespan` (`src/app/main.py`), периодически вызывает `stop_idle`. Интервал/порог — config. Reaper устойчив к рестарту процесса (состояние — в `hermes_instances`, не в памяти).

### 6. Изоляция и toolset (см. [05-security.md](../05-security.md))

- Один контейнер + том `HERMES_HOME` на пользователя (приватные память/навыки/сессии). Порт **не публикуется** на хост; доступ только из выделенной docker-сети control plane.
- При провижининге в том пишется `config.yaml` с `platform_toolsets.api_server` = безопасный набор `[web, file, vision, skills, todo]` (ориентир — пресет `hermes-api-server`), **без** `terminal`/`browser`/`code_execution`/`computer_use`. Набор конфигурируем (`HERMES_DEFAULT_TOOLSET`) — задел под тарифы. `approvals.mode` для headless — безопасный дефолт (deny опасных). Детали — [05-security.md §Multi-tenant изоляция Hermes-инстансов](../05-security.md).

## Consequences

**Положительные:**
- Полная изоляция данных/памяти пользователей (контейнер+том на пользователя).
- Гибернация снижает стоимость простаивающих инстансов; том сохраняет состояние между сном/пробуждением.
- Расширяемый `RuntimeBackend` — миграция на Modal/Daytona без переписывания manager/registry.
- Шифрование `API_SERVER_KEY` переиспользует готовый KMS ([ADR-003](ADR-003-byok-envelope-encryption.md)).

**Отрицательные / ограничения:**
- Control plane требует доступа к Docker socket (или удалённому Docker API) — повышенная привилегия, операционный риск ([07-deployment.md](../07-deployment.md), [05-security.md](../05-security.md)).
- Cold start при пробуждении остановленного контейнера — латентность первого запроса после простоя.
- Per-user контейнеры масштабируются хуже многотенантного процесса при тысячах пользователей — приемлемо на старте; пересмотр (пулы/serverless-бэкенд) при росте ([Q-046-1](../99-open-questions.md)).
- Политика хранения томов (когда удалять `HERMES_HOME` неактивного пользователя) — [Q-046-2](../99-open-questions.md).

## Alternatives

1. **Один общий многотенантный Hermes-процесс.** Отвергнуто: Hermes держит приватные память/навыки/сессии per-user в `HERMES_HOME`; общий процесс не изолирует данные и навыки пользователей.
2. **Контейнер на каждый запрос (ephemeral).** Отвергнуто: теряется персональная память/навыки между запросами (ценность агента), cold start на каждый run.
3. **Публикация host-порта на инстанс + адресация по порту.** Отвергнуто: расширяет поверхность атаки (порт на хосте), усложняет учёт портов; DNS-адресация в выделенной сети безопаснее.
4. **Сразу Modal/Daytona вместо Docker.** Отложено: docker-py проще на старте и не требует внешнего провайдера; `RuntimeBackend` оставляет путь миграции.
