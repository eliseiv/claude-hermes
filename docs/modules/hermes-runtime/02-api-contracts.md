# Hermes Runtime — Внутренние контракты

Модуль не публикует HTTP-эндпоинты. Контракт — внутренний интерфейс `HermesInstanceManager` и `RuntimeBackend` ([ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md)).

## HermesInstanceManager (src/app/hermes_runtime/manager.py)

```
class InstanceEndpoint:
    base_url: str        # http://hermes-user-<id>:8642 (DNS в docker-сети control plane)
    api_key: str         # расшифрованный API_SERVER_KEY (in-memory only)

class HermesInstanceManager:
    async def ensure_running(user_id: UUID) -> InstanceEndpoint
    async def provision(user_id: UUID) -> InstanceEndpoint
    async def stop_idle(threshold_seconds: int) -> int            # сколько остановлено
    async def deprovision(user_id: UUID) -> None
    async def health(user_id: UUID) -> bool                       # GET /health инстанса
```

- **`ensure_running`** — найти строку в registry; нет → `provision`; `status=stopped` → `RuntimeBackend.start` + **readiness-wait** (см. ниже) + `status=running`; обновить `last_active_at=now()`; расшифровать `api_key_enc` → `InstanceEndpoint`. Идемпотентен при гонке (повторный вызов того же `user_id` не создаёт второй контейнер — `user_id` PK + блокировка строки/`ON CONFLICT`). **Свежая `provisioning`-строка (конкурентный provisioner, [ADR-056](../../adr/ADR-056-provision-readiness-gate-and-volume-ownership.md)):** НЕ перепровижинит и НЕ проксирует немедленно — **ждёт** перехода строки в `running` / `health=200` под бюджетом `HERMES_PROVISION_READY_TIMEOUT_SECONDS`; по исчерпании → `UpstreamError`/`502` (cleanup делает владелец `_provision_locked`). Stale `provisioning` (старше `HERMES_PROVISIONING_STALE_SECONDS`) — реплей по [TD-031](../../100-known-tech-debt.md).
- **`provision`** — сгенерировать `API_SERVER_KEY` (CSPRNG ≥16 симв.), записать `hermes_instances` `status=provisioning` (commit — арбитр гонки), `RuntimeBackend.provision(...)` (см. ниже, env включает `HERMES_UID`/`HERMES_GID`), **readiness-poll `health(endpoint, api_key)` до `200`** (интервал `HERMES_PROVISION_READY_INTERVAL_SECONDS`, бюджет `HERMES_PROVISION_READY_TIMEOUT_SECONDS`), **затем** `mark_running` (`provisioning→running`), зашифровать ключ (`byok.kms`). **Таймаут readiness → cleanup** (`RuntimeBackend.remove` + подчистка строки, без неконсистентной `running`/залипшей `provisioning`) → `UpstreamError`. Том сохраняется (удаляется только контейнер). Контракт readiness/ownership — [ADR-056](../../adr/ADR-056-provision-readiness-gate-and-volume-ownership.md).
  - **Readiness-gate (cold-start, [ADR-056 §1](../../adr/ADR-056-provision-readiness-gate-and-volume-ownership.md)):** poll живёт в `_provision_locked` **после** `docker run`, **до** `mark_running`. `endpoint`/`api_key` уже известны там (сгенерированы) — отдельный envelope-decrypt не нужен. Reuse существующего `health(endpoint, api_key)` (Bearer). Инвариант: `HERMES_PROVISIONING_STALE_SECONDS` > `HERMES_PROVISION_READY_TIMEOUT_SECONDS` (валидируется fail-fast в `config.py`; иначе readiness-wait был бы признан stale).
  - **Idempotent-write `config.yaml` (reuse, [ADR-056 §4](../../adr/ADR-056-provision-readiness-gate-and-volume-ownership.md)):** при reuse существующий валидный `config.yaml` (непустой, парсится YAML, есть `platform_toolsets.api_server` + `model.default`/`model.provider`) **не перезаписывается**; перезапись только при первом провижининге (файла нет), full replay ([TD-031](../../100-known-tech-debt.md)) или невалидном/повреждённом файле (recovery).
- **`stop_idle`** — выбрать `status=running AND last_active_at < now()-threshold` → `RuntimeBackend.stop` → `status=stopped`. Том сохраняется.
- **`deprovision`** — `RuntimeBackend.remove(container)` + удалить/пометить строку (том — по политике, [Q-046-2](../../99-open-questions.md)).

## RuntimeBackend (src/app/hermes_runtime/docker_backend.py)

Расширяемый интерфейс (MVP — `DockerBackend` на docker-py; задел Modal/Daytona):

```
class RuntimeBackend(Protocol):
    async def provision(user_id, image, env, volume, network, config_yaml) -> ContainerRef
    async def start(container_ref) -> None
    async def stop(container_ref) -> None
    async def remove(container_ref) -> None
    async def health(endpoint, api_key) -> bool
```
- `health(endpoint, api_key)` — пробинг `GET /health` инстанса. Принимает `api_key` (расшифрованный `API_SERVER_KEY`), т.к. инстанс Hermes требует `Authorization: Bearer <API_SERVER_KEY>` на всех своих маршрутах (см. §Контракт инстанса Hermes). Manager-уровень `HermesInstanceManager.health(user_id)` извлекает endpoint+ключ из registry (ключ — envelope-decrypt, [ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)) и вызывает backend `health(endpoint, api_key)`. Plaintext-ключ — только in-memory на время вызова.

### Параметры провижининга контейнера (контракт запуска Hermes — [ADR-055](../../adr/ADR-055-hermes-instance-llm-config-contract.md))
- Образ: `HERMES_IMAGE`.
- Том: `<HERMES_VOLUME_ROOT>/<user_id>` → `/opt/data` (= `HERMES_HOME`).
- **Env** (`manager._container_env`, [ADR-055 §4](../../adr/ADR-055-hermes-instance-llm-config-contract.md)): `API_SERVER_ENABLED=true`, `API_SERVER_KEY=<сгенерированный>`, `API_SERVER_HOST=0.0.0.0`, `API_SERVER_PORT=8642`, **`HERMES_UID=<uid api>`, `HERMES_GID=<gid api>`** (дефолт `10001`/`10001`, [ADR-056 §4](../../adr/ADR-056-provision-readiness-gate-and-volume-ownership.md): s6 stage2 `usermod`/`groupmod`+`chown /opt/data` на этот uid/gid → владелец тома совпадает с пишущим `config.yaml` api-процессом, нет `PermissionError` при reuse). **`LLM_MODEL` и `LLM_PROVIDER` env НЕ передаются** (образ игнорирует `LLM_MODEL`; провайдер берётся из `config.yaml model.provider`). **Канал ключа зависит от провайдера:**
  - Провайдер **с env-ключом** (`anthropic`, `openrouter`, …): `<PROVIDER>_API_KEY=<HERMES_LLM_API_KEY>`. Имя key-env — из явной map провайдер→key-env (НЕ `f"{provider.upper()}_API_KEY"`): `anthropic→ANTHROPIC_API_KEY`, `openrouter→OPENROUTER_API_KEY`, `gemini→GOOGLE_API_KEY`, `huggingface→HF_TOKEN`, `zai→GLM_API_KEY`, `kimi-coding→KIMI_API_KEY`, `nous-api→NOUS_API_KEY`, `nvidia→NVIDIA_API_KEY` (полная таблица — [ADR-055 §4](../../adr/ADR-055-hermes-instance-llm-config-contract.md)).
  - Провайдер **без env-ключа** (`custom` ∈ `HERMES_PROVIDERS_CONFIG_API_KEY`, [ADR-055 §6](../../adr/ADR-055-hermes-instance-llm-config-contract.md)): `HERMES_INSTANCE_LLM_KEY=<HERMES_LLM_API_KEY>` (env-ссылка для `config.yaml model.api_key`); `<PROVIDER>_API_KEY` **НЕ** передаётся (образ его игнорирует — `custom` объявлен `env_vars=()`).
- Команда: `gateway run`.
- Сеть: `HERMES_DOCKER_NETWORK` (без `ports:` — порт не публикуется на хост).
- **`config.yaml` в томе** ([ADR-055 §1](../../adr/ADR-055-hermes-instance-llm-config-contract.md)):
  - `platform_toolsets.api_server` = `HERMES_DEFAULT_TOOLSET` (дефолт `[web, file, vision, skills, todo]`); `approvals.mode` — безопасный дефолт (`deny`). **Не ослабляются.**
  - **`model.default: "<HERMES_LLM_PROVIDER>/<HERMES_MODEL>"`** (control plane собирает из двух полей; `HERMES_MODEL` — «голое» имя модели, без префикса провайдера).
  - **`model.provider: "<HERMES_LLM_PROVIDER>"`** — КОНКРЕТНЫЙ провайдер, НЕ `auto` (`auto` дефолтит на openrouter base_url → 401).
  - **`model.base_url: "<HERMES_LLM_BASE_URL>"`** — эмитится ТОЛЬКО для `custom`/`azure-foundry`/`lmstudio` (пусто → строка отсутствует, образ подставляет провайдер-дефолт).
  - **`model.api_key: "${HERMES_INSTANCE_LLM_KEY}"`** — эмитится ТОЛЬКО для провайдеров без env-ключа (`custom` ∈ `HERMES_PROVIDERS_CONFIG_API_KEY`, [ADR-055 §6](../../adr/ADR-055-hermes-instance-llm-config-contract.md)). Значение — **env-ссылка**, НЕ плейнтекст ключа (образ раскрывает `${}` при загрузке config; секрет остаётся в env контейнера, не пишется в файл тома). Для провайдеров с env-ключом (`anthropic`, …) `model.api_key` **НЕ** эмитится.
  - Значения `provider`/`model`/`base_url` валидируются к безопасному charset перед эмиссией (safe-инъекция YAML, как toolset); `model.api_key` эмитится как env-ссылка-константа `"${HERMES_INSTANCE_LLM_KEY}"` → YAML-инъекция через содержимое ключа структурно невозможна.
- Имя контейнера: `hermes-user-<id>` (per control-plane-инстанс — с префиксом во избежание коллизий на общем Docker daemon).

### Сигнатура рендера config.yaml ([ADR-055 §1](../../adr/ADR-055-hermes-instance-llm-config-contract.md))
```
def render_instance_config(*, toolset: list[str], provider: str, model: str, base_url: str = "", api_key: str = "") -> str
```
Заменяет прежнюю `render_instance_config(toolset)`. Эмитит `platform_toolsets.api_server` + `approvals.mode` + секцию `model` (см. выше). Параметр `api_key` ([ADR-055 §6](../../adr/ADR-055-hermes-instance-llm-config-contract.md)): для провайдеров без env-ключа (`HERMES_PROVIDERS_CONFIG_API_KEY`) добавляет строку `model.api_key: "${HERMES_INSTANCE_LLM_KEY}"` (env-ссылка); иначе строка не эмитится.

### Валидация провайдера (fail-fast, [ADR-055 §2](../../adr/ADR-055-hermes-instance-llm-config-contract.md))
`_require_provision_config` (до `docker run`) дополнительно проверяет:
- `HERMES_LLM_PROVIDER` ∈ allowlist образа ∧ ≠ `auto` (`openai` — невалиден, нет direct-провайдера) → иначе `UpstreamError(...)`;
- `HERMES_MODEL` непуст → иначе `UpstreamError(...)`;
- провайдер требует base_url (`custom`/`azure-foundry` — у них НЕТ дефолтного endpoint) ⇒ `HERMES_LLM_BASE_URL` непуст → иначе `UpstreamError(...)`. **`lmstudio` НЕ в этом наборе:** у образа есть дефолтный endpoint `http://127.0.0.1:1234/v1`, поэтому base_url для него **опционален** (как для `openrouter`/`anthropic` с дефолтным endpoint). Код: `HERMES_PROVIDERS_REQUIRING_BASE_URL = {custom, azure-foundry}`.

## Контракт инстанса Hermes (внешний, потребляется Agent Proxy)
- Auth: `Authorization: Bearer <API_SERVER_KEY>`.
- `GET /health` — пробинг (health).
- `POST /v1/runs`, `GET /v1/runs/{id}/events` (SSE), `POST /v1/runs/{id}/approval`, `POST /v1/runs/{id}/stop` — используются [Agent Proxy](../agent-proxy/02-api-contracts.md).
