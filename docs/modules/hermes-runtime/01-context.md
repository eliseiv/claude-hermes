# Hermes Runtime — Context

## Зависимости (входящие)
- [Agent Proxy](../agent-proxy/README.md) — единственный потребитель: вызывает `ensure_running(user_id)` перед прокси к инстансу, `health` для диагностики.
- `lifespan` (`src/app/main.py`) — запускает фоновый reaper (`stop_idle`).
- [API Gateway](../api-gateway/README.md) — гарантирует существование строки `users` (lazy provisioning, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) до `ensure_running` (FK `hermes_instances.user_id`).

## Зависимости (исходящие)
- **Docker daemon** (через docker-py / `RuntimeBackend`) — управление контейнерами/томами/сетью.
- **`byok.kms`** (`KmsClient`/`LocalKmsClient`, [ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)) — шифрование/расшифровка per-instance `API_SERVER_KEY`.
- **PostgreSQL** (`hermes_instances`) — registry состояния инстансов.
- **Hermes-инстанс** (`GET /health`) — health-пробинг.

## Границы
- Модуль НЕ вызывает Hermes API-сервер `POST /v1/runs`/`/events` напрямую — это [Agent Proxy](../agent-proxy/README.md). Hermes-runtime отвечает только за «инстанс существует, запущен и адресуем».
- Модуль НЕ авторизует клиента и НЕ списывает кредиты.
- Hermes использует **свой** `LLM_PROVIDER`/ключ/модель внутри инстанса — не наш `LLMClient` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md) не затрагивается).

## Соседи
- [Agent Proxy](../agent-proxy/README.md) — потребитель.
- [BYOK](../byok/README.md) — переиспользуется `kms` (envelope encryption).
- [Wallet / Ledger](../wallet-ledger/README.md), [Policy Engine](../policy-engine/README.md) — используются Agent Proxy, не напрямую этим модулем.
