# Hermes Runtime — Testing

Стратегия — по [06-testing-strategy.md](../../06-testing-strategy.md). Docker мокается в unit; реальный контейнер — в integration/e2e.

## Unit (Docker мокается)
- `RuntimeBackend` мокается: `ensure_running` при отсутствии строки → вызывает `provision` (один контейнер); при `stopped` → `start` + `status=running`; при `running` → только `last_active_at`.
- `provision` генерирует уникальный `API_SERVER_KEY` (≥16 симв.), шифрует через mock/`LocalKmsClient`, пишет `hermes_instances` с `provisioning`→`running`; plaintext ключ в БД не попадает.
- `stop_idle` выбирает только `running` старше порога, переводит в `stopped` (том не удаляется).
- Гонка `ensure_running` (два параллельных вызова одного `user_id`) → один контейнер (PK/`ON CONFLICT`).
- `config.yaml` toolset = безопасный набор (без terminal/browser/code_execution/computer_use).
- Шифрование: `api_key_enc` расшифровывается обратно в исходный ключ (round-trip через `byok.kms`).

## Integration (testcontainers Postgres; Docker — реальный, опц.)
- Registry: миграция `0013` применяется; CRUD `hermes_instances`; индекс используется в `stop_idle`-выборке.
- Реальный Docker (если доступен в CI): провижининг контейнера из тестового образа, `health`, `stop`/`start`, `deprovision`; изоляция томов двух `user_id`.

## E2E (реальный Docker + Hermes-образ)
- Первый `POST /v1/agent/run` нового `userId` → поднимается контейнер; повторный → реюз; после idle-таймаута → `stopped`, следующий запрос → будится.
- Изоляция: два разных `userId` → разные тома/память.
- Порт инстанса не виден с хоста (проверка отсутствия host-binding).

## Безопасность (обязательные кейсы)
- `API_SERVER_KEY` не появляется в логах (redaction) и не хранится plaintext в БД.
- Toolset инстанса не содержит `terminal`/`browser`/`code_execution`/`computer_use` (проверка содержимого `config.yaml` / `GET /v1/toolsets` инстанса).
