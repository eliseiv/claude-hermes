# Module: Hermes Runtime

- Статус: **Реализован (Спринт 2, Phases 1-5)** (Hermes-интеграция, [ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md))
- Ответственность: жизненный цикл персональных Hermes-инстансов (Docker-контейнер + том `HERMES_HOME` на пользователя) — provision / ensure_running / stop_idle (гибернация) / deprovision / health, registry поверх таблицы `hermes_instances`, фоновый reaper. Внутренний модуль монолита (`src/app/hermes_runtime/`).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [05-security.md](05-security.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
- `HermesInstanceManager.ensure_running(user_id)` возвращает `InstanceEndpoint(base_url, api_key)`: находит инстанс в registry, провижинит при отсутствии, будит при `stopped`, обновляет `last_active_at`.
- `provision(user_id)` создаёт контейнер из `HERMES_IMAGE` с томом `HERMES_HOME`, уникальным `API_SERVER_KEY` (CSPRNG ≥16 симв., шифруется через `byok.kms`), env `API_SERVER_*`/`LLM_*`, `config.yaml` с ограниченным toolset (`[web,file,vision,skills,todo]`), без проброса host-порта, в выделенной docker-сети.
- `stop_idle(threshold)` останавливает контейнеры с `last_active_at` старше `HERMES_IDLE_TIMEOUT_SECONDS` (том сохраняется); фоновый reaper в `lifespan` вызывает его периодически.
- `deprovision(user_id)` удаляет контейнер (том — по политике); `health(user_id)` пробингует `GET /health` инстанса.
- Registry поверх `hermes_instances` (миграция `0013`); `RuntimeBackend` — расширяемый интерфейс (MVP: `DockerBackend`/docker-py; задел Modal/Daytona).
- `API_SERVER_KEY` хранится только зашифрованным (envelope, [ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)); plaintext только in-memory.

## Известные ограничения
- **Stale `provisioning` при краше процесса** ([TD-031](../../100-known-tech-debt.md)): `ensure_running` трактует строку в статусе `provisioning` как живой инстанс; при краше между `create_provisioning` и `mark_running` остаётся строка с `container_id`/`endpoint=NULL` (endpoint — DNS-фолбэк). Узкий recovery-edge → проявляется чистым `502` (`UpstreamError`), без порчи данных/баланса/утечки. Возможный фикс — реплей stale `provisioning` по возрасту `created_at`. Severity: low (robustness).

## Changelog
- 2026-06-24: **[ADR-056](../../adr/ADR-056-provision-readiness-gate-and-volume-ownership.md)** (architect) — фикс двух дефектов надёжности провижининга, вскрытых живым e2e `/v1/agent/run`. **(A) cold-start readiness gap:** `_provision_locked` помечал `running` сразу после `docker run` → прокси в неготовый `api_server:8642` → `502`. Решение: readiness-poll `health(endpoint, api_key)` (Bearer, reuse) после `docker run`, до `mark_running`; бюджет `HERMES_PROVISION_READY_TIMEOUT_SECONDS` (90с) / интервал `HERMES_PROVISION_READY_INTERVAL_SECONDS` (2с); таймаут → cleanup (`remove`+подчистка строки) → `502`; конкурентный `ensure_running` на свежей `provisioning` ждёт готовности, не перепровижинит; инвариант `HERMES_PROVISIONING_STALE_SECONDS` > ready (fail-fast). **(B) volume ownership conflict:** api (uid 10001) пишет `config.yaml`, затем Hermes-образ `chown`'ит том на `HERMES_UID=10000` → reuse-`provision` падает `PermissionError(13)`. Решение (1+3): env `HERMES_UID`/`HERMES_GID`=uid/gid api (10001) → stage2 chown на тот же uid; idempotent-write `config.yaml` (не перезаписывать валидный существующий при reuse). needs_code_sync: `manager.py`/`docker_backend.py`/`config.py`. Scope backend + devops (env-дефолты). Открытые: [Q-056-1](../../99-open-questions.md)/[Q-056-2](../../99-open-questions.md).
- 2026-06-23: bootstrap модуля (architect). Зафиксированы [ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md) (per-user runtime), контракт таблицы `hermes_instances` (миграция `0013`), security multi-tenant, фазы, тесты. Scope backend.
- 2026-06-23: реализован backend (Спринт 2, Phases 1-5, `src/app/hermes_runtime/`): `HermesInstanceManager` (`ensure_running`/`provision`/`stop_idle`/`deprovision`/`health`), `RuntimeBackend` с `DockerBackend` (docker-py), registry поверх `hermes_instances` (миграция `0013`), генерация+envelope-шифрование `API_SERVER_KEY` ([ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)), фоновый reaper в `lifespan`, ограниченный toolset в `config.yaml`. Введены операционные config-ключи `HERMES_REAPER_INTERVAL_SECONDS`, `HERMES_API_KEY_BYTES`, `HERMES_HEALTH_TIMEOUT_SECONDS`, явный `HERMES_LLM_API_KEY` и дефолты `HERMES_*` — зафиксированы в [07-deployment.md §Конфигурация (env)](../../07-deployment.md#конфигурация-env). Статус → «Реализован (Спринт 2, Phases 1-5)». Открытый вопрос дефолта `HERMES_VOLUME_ROOT` — [Q-046-3](../../99-open-questions.md).
