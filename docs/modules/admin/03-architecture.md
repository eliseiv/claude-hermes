# Admin — Architecture

## Размещение
- Новый пакет `src/app/admin/` (router + thin service-обёртка над Wallet) и роутер `api_gateway/routers/admin.py`
  под префиксом `/v1/admin`. Структура — в [02-tech-stack.md](../../02-tech-stack.md#структура-проекта-фактическая).
- Admin-роуты подключаются с зависимостью `require_admin` и **без** `get_current_user`.

## Авторизация: `require_admin` (ADR-009)
```
require_admin(x_admin_token: str = Header(...)) -> None  # actor="admin", без userId
```
- Сравнивает `X-Admin-Token` с `ADMIN_API_SECRET` (и опц. `ADMIN_API_SECRET_PREV` на время ротации) — **constant-time**
  (`hmac.compare_digest`). Несовпадение/отсутствие → `401`.
- **НЕ** выполняет lazy-provisioning, **НЕ** читает/трогает `users.trial_used`, **НЕ** создаёт строку `users` для actor.
- Никакого `sub`/пользовательской идентичности: actor фиксируется как `admin` в audit.
- Изоляция: разные секреты/заголовки/зависимости с пользовательским путём → эскалация невозможна by construction (ADR-009 §4).

## Поток grant
```mermaid
sequenceDiagram
    participant OP as Operator
    participant GW as API Gateway (/v1/admin)
    participant ADM as Admin Service
    participant W as Wallet
    participant AU as Audit

    OP->>GW: POST /v1/admin/wallet/grant (X-Admin-Token, {userId, amount, idempotencyKey, reason})
    GW->>GW: require_admin (constant-time compare; rate limit; size limit; validate extra=forbid)
    alt токен невалиден
        GW-->>OP: 401
    else токен валиден
        GW->>ADM: grant(userId, amount, idempotencyKey, reason)
        ADM->>ADM: проверка существования users(userId)
        alt userId не существует
            ADM-->>OP: 404 user_not_found
        else существует
            ADM->>W: WalletService.grant(user_id, amount, idempotency_key, meta{reason}, reason)
            W->>AU: audit billing_credit (idempotent)
            W-->>ADM: {newBalance, ledgerTxId, idempotentReplay}
            ADM->>AU: audit admin_grant (actor=admin, reason, userId, amount, ledgerTxId; БЕЗ секрета)
            ADM-->>OP: 200 {newBalance, ledgerTxId, idempotentReplay}
        end
    end
```

## Несуществующий userId — решение
Admin-grant **не создаёт** пользователей (обоснование — [02-api-contracts.md §Обоснование](02-api-contracts.md#обоснование-404-на-несуществующем-userid-не-admin-provisioning)).
Проверка существования `users(userId)` выполняется **до** вызова `WalletService.grant` (который сам делает `_ensure_wallet`,
но не `users`). Отсутствие → `404 user_not_found`. Это сохраняет инвариант ADR-007: единственный путь рождения
идентичности — доверенный issuer.

## Переиспользование Wallet
- `grant`: вызывается **как есть** (`src/app/wallet/service.py:174`); сигнатура `grant(user_id, amount, idempotency_key, meta, reason)`,
  идемпотентна по `(user_id, idempotency_key)`, пишет ledger credit + audit `billing_credit`. `meta` admin-grant включает
  `{"source": "admin", "reason": reason}` (без секретов).
- `consume`: для `POST /v1/admin/wallet/debit` ([ADR-061](../../adr/ADR-061-admin-wallet-debit.md)) вызывается **как есть** (метод `consume` в `src/app/wallet/service.py`); сигнатура `consume(user_id, amount, idempotency_key, meta, session_id=None)`, идемпотентна по `(user_id, idempotency_key)`, savepoint-атомарна, пишет ledger debit + audit `billing_debit`. `meta` admin-debit = `{"source": "admin_debit", "reason": reason}` → `_agent_reconcile_applies=False` → обычный путь, `wallets.debt` не затрагивается. `session_id=None` → без валидации сессии. `amount > balance` → `InsufficientCreditsError` → `409 insufficient_credits` (savepoint-откат). **Новый метод Wallet не требуется.**
- `get_wallet_view`: для `GET /v1/admin/wallet/{userId}`.
- Admin-модуль **не** дублирует биллинг-логику — только тонкая обёртка (auth + проверка userId + дополнительный audit `admin_grant`/`admin_debit`).

## Защита
- Отдельный rate limit на `/v1/admin/*` (per source IP, дефолт 10 req/min, env-конфиг). Изолирован от пользовательских лимитов.
- Size-лимит тела admin-grant ≤ 8 KB.
- `X-Admin-Token` добавлен в redaction allowlist (никогда не логируется; ADR-009 §6).
- strict Pydantic v2 (`extra='forbid'`), `amount > 0`, `reason` непустой.
