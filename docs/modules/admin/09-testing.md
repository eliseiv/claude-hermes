# Admin — Testing

## Unit
- `require_admin`: валидный `X-Admin-Token` → проходит; неверный/отсутствует → `401`; сравнение constant-time (по контракту,
  не таймингом). Совпадение с `ADMIN_API_SECRET_PREV` (ротация) → проходит. Пустые секреты в конфиге не матчатся.
- Pydantic-схема credits/grant: `amount<=0` → `422`; пустой/отсутствующий `reason` → `422`; лишнее поле (`extra='forbid'`) → `422`.
- Pydantic-схема subscription/grant: пустой `plan` → `422`; `expiresAt` в прошлом/`now()` → `422`; пустой/отсутствующий `reason` → `422`; лишнее поле → `422`; `grantCredits` опц. (дефолт `false`).

## Integration (реальный PostgreSQL)
- `credits/grant` на существующем `userId` → `ledger_transactions(type=credit)`, баланс += amount, `idempotentReplay=false`.
- Повторный `credits/grant` тот же `idempotencyKey` + payload → `idempotentReplay=true`, баланс не меняется, тот же `ledgerTxId`.
- Тот же `idempotencyKey`, другой `amount` → `409`, без начисления.
- **Алиас:** `POST /v1/admin/wallet/grant` и `POST /v1/admin/credits/grant` дают идентичный результат/идемпотентность (один handler).
- Несуществующий `userId` (оба эндпоинта) → `404 user_not_found`, строка `users` **не** создана, баланс/подписка не появились.
- `require_admin` не создаёт `users` для actor (нет provisioning): после серии admin-запросов нет «admin»-строки в `users`.
- `users.trial_used` не изменяется admin-операциями.
- audit: успешный `credits/grant` создаёт **и** `billing_credit` (Wallet), **и** `admin_grant` (Admin). Секрет в payload отсутствует.
- `GET /v1/admin/wallet/{userId}`: корректные `balance` + `lastTransactions` (DESC); несуществующий → `404`.
- **`subscription/grant` (grantCredits=false)** на существующем `userId` → `subscriptions(status=active, plan, expires_at)` (upsert), баланс **не** меняется, `creditsGranted`/`ledgerTxId` опущены, `idempotentReplay=false`.
- **`subscription/grant` (grantCredits=true)** → дополнительно `ledger_transactions(type=credit)` на `SUBSCRIPTION_CREDITS_PER_PERIOD`, `creditsGranted>0`, `ledgerTxId` присутствует.
- Повторный `subscription/grant` тот же `idempotencyKey` + payload → `idempotentReplay=true`, подписка не переписывается повторно, кредиты не начисляются второй раз (ключ `admin-sub-grant:{idempotencyKey}`).
- Тот же `idempotencyKey`, **другой** payload (`plan`/`expiresAt`/`grantCredits`) → `409`, без мутации подписки/баланса.
- **Изоляция idempotency-пространств:** одинаковый `idempotencyKey` в `credits/grant` и `subscription/grant` → независимые начисления (разные ключи `…` vs `admin-sub-grant:…`), без коллизии.
- **Атомарность `subscription/grant`:** при искусственном сбое после upsert и до grant — полный откат (ни активной подписки, ни кредитов).
- После `subscription/grant` последующий `POST /v1/agent/run` проходит policy-gate (активная подписка) — e2e.
- audit: успешный `subscription/grant` создаёт `admin_subscription_grant` (при grantCredits=true — дополнительно `billing_credit`). Секрет в payload отсутствует.

## Security
- Пользовательский JWT/клиентский `X-API-Key` на `/v1/admin/*` (без `X-Admin-Token`) → `401` (клиентская auth не авторизует admin).
- `X-Admin-Token` на пользовательском роуте (`/v1/wallet`) не даёт доступа (там нужен JWT).
- `X-Admin-Token` не попадает в логи/audit (redaction).
- Rate limit `/v1/admin/*`: превышение дефолта → `429`, изолировано от пользовательских лимитов.
- Size-лимит admin-grant: тело > 8 KB → `413`.
