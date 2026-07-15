# Hermes Runtime — Data Model

Каноническая DDL — [03-data-model.md §22 hermes_instances](../../03-data-model.md). Здесь — модульные заметки.

## Таблица `hermes_instances` (миграция `0013`)
- `user_id` (PK, FK `users(id)` ON DELETE CASCADE) — один инстанс на пользователя.
- `container_id` (TEXT, nullable) — id Docker-контейнера (NULL в `provisioning` до запуска).
- `endpoint` (TEXT, nullable) — DNS-имя:порт в docker-сети (`hermes-user-<id>:8642`).
- `api_key_enc` (BYTEA) + `encrypted_dek` (BYTEA) + `nonce` (BYTEA) — envelope-шифрованный `API_SERVER_KEY` ([ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)). Plaintext не хранится.
- `status` (enum `hermes_instance_status`) ∈ `provisioning|running|stopped`.
- `port` (INT, nullable) — порт на старте не публикуется (резерв под альт. `RuntimeBackend`).
- `last_active_at` (TIMESTAMPTZ) — для гибернации.
- `created_at` (TIMESTAMPTZ) — **иммутабельное** время создания инстанса (НЕ двигается wake/mark_*).
- `provisioning_started_at` (TIMESTAMPTZ, nullable, миграция `0017`, [ADR-062 §1a](../../adr/ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)) — время начала ТЕКУЩЕЙ provisioning-попытки; **stale-якорь** для `_is_stale_provisioning` (age = now − `provisioning_started_at`), а НЕ `created_at`. Выставляется `create_provisioning` (cold-start) и wake-`mark_provisioning` = `now()`. Для разбуженного инстанса `created_at` — исходное создание (часы/сутки назад) → анкеровка на нём ложно признавала бы живую wake-попытку stale.
- Индекс `ix_hermes_instances_status_active (status, last_active_at)` — для reaper (`stop_idle`).

## Миграции
- **`0013`**, цепочка `0012`→`0013` (single head; down_revision = full revision id `0012`). Expand-only (новая таблица + enum). Создаёт: enum `hermes_instance_status`, таблицу `hermes_instances`, индекс `ix_hermes_instances_status_active`.
- **`0017`** ([ADR-062](../../adr/ADR-062-wake-readiness-gate-and-connect-only-launch-retry.md)), down_revision = `0016_audit_logs_append_only` (текущий single head — проверить фактически перед реализацией). Expand-only: `ALTER TABLE hermes_instances ADD COLUMN provisioning_started_at TIMESTAMPTZ NULL`; backfill in-flight `provisioning`-строк `= created_at` (сохранить текущую stale-семантику на деплое).

## Инварианты
- `user_id` PK → ровно один инстанс на пользователя; гонка `ensure_running` разрешается блокировкой строки / `ON CONFLICT (user_id) DO NOTHING` + повторное чтение (паттерн `auth_devices`).
- `api_key_enc`/`encrypted_dek`/`nonce` — обязательны (NOT NULL); plaintext `API_SERVER_KEY` в БД запрещён.
- FK на `users` гарантируется lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) до `ensure_running`.
- Том `HERMES_HOME` — вне БД (на хосте, `HERMES_VOLUME_ROOT`); БД хранит только метаданные инстанса.
