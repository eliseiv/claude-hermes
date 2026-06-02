# Auth — Data Model

Две новые таблицы, **миграция `0005`** (expand-only, цепочка `0001`→`0002`→`0003`→`0004`→`0005`). Сводный DDL — [03-data-model.md](../../03-data-model.md) (таблицы 18–19). `users` **не меняется** (идентичность по-прежнему `users.id ≡ sub`, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)).

## 18. auth_devices
```sql
CREATE TABLE auth_devices (
    device_id   TEXT PRIMARY KEY,                                   -- стабильный id устройства (клиент или сгенерированный backend)
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_auth_devices_user ON auth_devices (user_id);
```
> Маппинг `deviceId → userId` (find-or-create, [03-architecture.md](03-architecture.md)). `device_id` — PK (одно устройство = одна идентичность). `UNIQUE` обеспечивается PK; гонка одновременной регистрации разрешается `ON CONFLICT (device_id) DO NOTHING` + повторное чтение.

## 19. auth_refresh_tokens
```sql
CREATE TABLE auth_refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   TEXT NOT NULL REFERENCES auth_devices(device_id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,                       -- sha256(opaque refresh token), НЕ plaintext
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,                          -- single-use: проставляется при rotation
    revoked_at  TIMESTAMPTZ,                          -- ревокация цепочки при reuse-детекте/logout
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_refresh_token_hash ON auth_refresh_tokens (token_hash);
CREATE INDEX ix_refresh_user_device ON auth_refresh_tokens (user_id, device_id);
```
> Opaque refresh-token хранится **только** как хэш. `used_at`/`revoked_at` реализуют single-use rotation и анти-кражу: предъявление токена с непустым `used_at` → reuse → ревокация цепочки (`SET revoked_at=now WHERE user_id=? AND device_id=?`). Истёкшие/использованные/отозванные строки — кандидаты на фоновую очистку ([TD-013](../../100-known-tech-debt.md), не блокер MVP).

## Инварианты
- `auth_devices.user_id` и `auth_refresh_tokens.user_id` всегда указывают на существующую `users`-строку (provisioning при `register`, FK `ON DELETE CASCADE`).
- `users.id ≡ sub` сохраняется: `register` задаёт `userId` явно, как и lazy-path.
- Refresh-token валиден ⟺ `used_at IS NULL AND revoked_at IS NULL AND expires_at > now()`.
- Один активный (не used/revoked) refresh-token на устройство в норме — после rotation предыдущий помечен `used_at`.
