# Hermes Runtime — Security

Каноническая модель — [../../05-security.md §Multi-tenant изоляция Hermes-инстансов](../../05-security.md#multi-tenant-изоляция-hermes-инстансов-adr-046-adr-045). Здесь — модульный фокус.

## Изоляция
- **Контейнер + том на пользователя** — приватные память/навыки/сессии не пересекаются (`hermes_instances.user_id` PK).
- **Порт не публикуется на хост** — `API_SERVER_PORT=8642` доступен только из `HERMES_DOCKER_NETWORK`; адресация по DNS контейнера.
- **`ensure_running` резолвит инстанс строго по `user_id` субъекта** (из `X-User-Id`, [ADR-044](../../adr/ADR-044-client-api-key-auth.md)) — нет доступа к чужому инстансу.

## Ограничение toolset
- `config.yaml` в томе: `platform_toolsets.api_server` = `HERMES_DEFAULT_TOOLSET` (дефолт `[web, file, vision, skills, todo]`), **БЕЗ** `terminal`/`browser`/`code_execution`/`computer_use` (исключён RCE/браузер/произвольный код). Ориентир — пресет `hermes-api-server`.
- `approvals.mode` — безопасный дефолт (deny опасных без явного approval; approvals ретранслируются клиенту через [Agent Proxy](../agent-proxy/README.md)).

## Шифрование секретов
- Per-instance `API_SERVER_KEY` (CSPRNG ≥16 симв.) — envelope encryption через `byok.kms` (`api_key_enc`/`encrypted_dek`/`nonce`, [ADR-003](../../adr/ADR-003-byok-envelope-encryption.md)). Plaintext — только in-memory на время прокси-вызова, затем обнуляется; redaction `*key*`; никогда в логах/БД-plaintext.

## Docker socket
- Control plane требует доступа к Docker socket / удалённому Docker API — повышенная привилегия (≈ root на хосте). Socket монтируется только в `api` control plane, не в инстансы. Операционные митигации — [../../07-deployment.md §Hermes runtime](../../07-deployment.md#hermes-runtime--деплой-per-user-инстансов-adr-046-adr-045).

## Логирование
- Не логируются: `API_SERVER_KEY`, env с ключами провайдера. Логируются метаданные: `userId`, `container_id`, `status`, `endpoint` (без секрета), события жизненного цикла.
