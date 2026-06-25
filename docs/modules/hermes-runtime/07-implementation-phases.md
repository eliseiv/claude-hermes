# Hermes Runtime — Implementation Phases

Соответствует Спринту 2 плана Hermes-интеграции ([ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md)).

## Phase 1 — Data layer
- Миграция `0013`: enum `hermes_instance_status`, таблица `hermes_instances`, индекс `ix_hermes_instances_status_active` ([04-data-model.md](04-data-model.md)).
- `registry.py` — CRUD/upsert поверх `hermes_instances`.
- Модель в `src/app/models/tables.py`.

## Phase 2 — RuntimeBackend (Docker)
- `RuntimeBackend` (Protocol) + `DockerBackend` (docker-py): `provision`/`start`/`stop`/`remove`/`health`.
- Параметры запуска контейнера (том, env, `config.yaml`, сеть, без host-порта) — [02-api-contracts.md](02-api-contracts.md).
- Зависимость docker-py — добавить в `pyproject.toml`/`uv.lock` ([02-tech-stack.md](../../02-tech-stack.md)).

## Phase 3 — Manager + шифрование ключа
- `HermesInstanceManager`: `ensure_running`/`provision`/`stop_idle`/`deprovision`/`health`.
- Шифрование `API_SERVER_KEY` через `byok.kms` ([ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)); генерация CSPRNG.
- Гонко-безопасность `ensure_running` (`user_id` PK + блокировка/`ON CONFLICT`).
- Wiring `get_hermes_manager()` в `deps.py`.

## Phase 4 — Гибернация (reaper)
- Фоновая задача `stop_idle` в `lifespan` (`src/app/main.py`), интервал/порог из config (`HERMES_IDLE_TIMEOUT_SECONDS`).
- Устойчивость к рестарту (состояние в БД).

## Phase 5 — Config + интеграция
- Config-настройки (`config.py`): `HERMES_IMAGE`/`HERMES_DOCKER_NETWORK`/`HERMES_VOLUME_ROOT`/`HERMES_DEFAULT_TOOLSET`/`HERMES_IDLE_TIMEOUT_SECONDS`/`HERMES_LLM_PROVIDER`+ключ/`HERMES_MODEL` ([07-deployment.md](../../07-deployment.md)).
- Интеграция с [Agent Proxy](../agent-proxy/README.md) (`ensure_running`/`health`).

> Тесты — [09-testing.md](09-testing.md) (Docker мокается в unit; реальный контейнер — integration/e2e).
