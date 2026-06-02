# Auth — Implementation Phases (backend scope)

Стек — [02-tech-stack.md](../../02-tech-stack.md) (Python 3.12 / FastAPI / SQLAlchemy async / PostgreSQL 16 / Alembic). Команды lint/format/typecheck/test — оттуда же.

## Phase 1 — Config и ключи
- Добавить в `src/app/config.py`:
  - `jwt_private_key` (`JWT_PRIVATE_KEY`), `jwt_private_key_path` (`JWT_PRIVATE_KEY_PATH`), `jwt_public_key_path` (`JWT_PUBLIC_KEY_PATH`) — `JWT_PUBLIC_KEY` уже есть.
  - `jwt_kid` (`JWT_KID`).
  - `auth_access_ttl_seconds` (`AUTH_ACCESS_TTL_SECONDS`, дефолт 3600), `auth_refresh_ttl_seconds` (`AUTH_REFRESH_TTL_SECONDS`, дефолт 2592000).
  - `auth_rate_limit_per_ip` (`AUTH_RATE_LIMIT_PER_IP`, дефолт 10), `auth_jwks_enabled` (`AUTH_JWKS_ENABLED`, дефолт `true`).
- Резолверы ключей: `resolve_private_key()` / `resolve_public_key()` — приоритет `*_PATH` (read file) > строка (`\n`-разэкранирование). Приватный ключ под redaction.
- **Не ломать** существующие `JWT_PUBLIC_KEY`/`JWT_JWKS_URL`/`JWT_ISSUER`/`JWT_AUDIENCE` (verify-path).

## Phase 2 — Миграция 0005
- `auth_devices`, `auth_refresh_tokens` ([04-data-model.md](04-data-model.md)). Expand-only, `down_revision='0004'`. `users` не трогать.

## Phase 3 — TokenIssuer + AuthService
- `src/app/auth/issuer.py` — RS256-подпись (claims `sub/device_id/iss/aud/iat/exp`, заголовок `kid`), reuse значений `JWT_ISSUER`/`JWT_AUDIENCE`.
- `src/app/auth/service.py` — find-or-create по `deviceId` (с гонко-безопасным `ON CONFLICT`), provisioning `users`, выпуск access+refresh, refresh-rotation/reuse-детект/ревокация.

## Phase 4 — Router
- `src/app/api_gateway/routers/auth.py` — `POST /register`, `POST /token`, `POST /refresh`, `GET /jwks` под `/v1/auth`.
- **Вне** `get_current_user`-зависимости; под per-IP rate-limit. `503` если приватный ключ не сконфигурирован.
- Подключить в основной app-роутинг (порядок middleware не нарушать: size→cid→[auth skip для /v1/auth]→rate-limit→handler).

## Phase 5 — Тесты ([06-testing-strategy.md](../../06-testing-strategy.md))
- Round-trip: `register` → выпущенный JWT проходит `JwtVerifier.verify()`.
- Идемпотентность: повторный `register`/`token` того же `deviceId` → тот же `userId`.
- Refresh rotation: старый инвалидируется, reuse → `401` + ревокация.
- Совместимость ADR-007: `sub` без `users`-строки провижинится на первом `/v1/*` (lazy fallback не сломан).
- PEM-в-env: оба механизма (`*_PATH` и `\n`-строка) дают рабочий issuer; отсутствие приватного → `503`.
- Rate-limit per IP на `/v1/auth/*`.

## Что НЕ делать
- Не менять `JwtVerifier.verify()`.
- Не вводить email/пароль/Apple Sign-In (out-of-scope MVP, [Q-018-2](../../99-open-questions.md)).
- Не трогать admin-auth, preview, billing, tool-loop.
