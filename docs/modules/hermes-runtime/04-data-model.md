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
- `created_at` (TIMESTAMPTZ).
- Индекс `ix_hermes_instances_status_active (status, last_active_at)` — для reaper (`stop_idle`).

## Миграция
- Номер: **`0013`**, цепочка `0012`→`0013` (single head; down_revision = full revision id `0012`). Expand-only (новая таблица + enum). Следующая после `20260619_0012_auth_identities`.
- Создаёт: enum `hermes_instance_status`, таблицу `hermes_instances`, индекс `ix_hermes_instances_status_active`.

## Инварианты
- `user_id` PK → ровно один инстанс на пользователя; гонка `ensure_running` разрешается блокировкой строки / `ON CONFLICT (user_id) DO NOTHING` + повторное чтение (паттерн `auth_devices`).
- `api_key_enc`/`encrypted_dek`/`nonce` — обязательны (NOT NULL); plaintext `API_SERVER_KEY` в БД запрещён.
- FK на `users` гарантируется lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) до `ensure_running`.
- Том `HERMES_HOME` — вне БД (на хосте, `HERMES_VOLUME_ROOT`); БД хранит только метаданные инстанса.
