# Module: Admin

- Статус: Реализован (credits/grant + get-wallet); спроектирован — subscription/grant ([ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md)) и wallet/debit ([ADR-061](../../adr/ADR-061-admin-wallet-debit.md)), ожидают backend
- Ответственность: операторские/саппорт-действия над аккаунтами под изолированной admin-авторизацией. Начисление кредитов пользователю (`credits/grant`, переходный алиас `wallet/grant`), **списание/коррекция баланса** (`wallet/debit`, [ADR-061](../../adr/ADR-061-admin-wallet-debit.md)), ручная выдача подписки (`subscription/grant`, [ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md)) и read-only просмотр кошелька для саппорта.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
- `POST /v1/admin/credits/grant` (канонический; `POST /v1/admin/wallet/grant` — переходный алиас) начисляет кредиты через существующий `WalletService.grant()`, идемпотентно по `idempotencyKey`, с обязательным `reason`; ответ `{newBalance, ledgerTxId, idempotentReplay}`.
- `POST /v1/admin/subscription/grant` ([ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md)) ручно активирует подписку через `SubscriptionService.admin_grant(user_id, plan, expires_at)`: upsert `subscriptions(status=active)`, опц. начисление `SUBSCRIPTION_CREDITS_PER_PERIOD` при `grantCredits=true`; идемпотентно по `admin-sub-grant:{idempotencyKey}`; `404 user_not_found`; ответ `{status, plan, expiresAt, creditsGranted?, ledgerTxId?, idempotentReplay}`.
- `POST /v1/admin/wallet/debit` ([ADR-061](../../adr/ADR-061-admin-wallet-debit.md)) списывает `amount>0` кредитов через `WalletService.consume(session_id=None, meta.source="admin_debit")`, идемпотентно по `idempotencyKey`, с обязательным `reason`; `amount > balance` → `409 insufficient_credits`; `wallets.debt` не затрагивается; ответ `{newBalance, ledgerTxId, idempotentReplay}`.
- `GET /v1/admin/wallet/{userId}` отдаёт баланс + последние ledger-транзакции (read-only).
- Авторизация — изолированный `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)), отдельная зависимость `require_admin`, не пересекается с пользовательским JWT/клиентским ключом, не запускает provisioning, не трогает trial.
- Аудит-события `admin_grant`, `admin_subscription_grant` и `admin_debit` (actor=admin, reason, без секрета). Отдельный rate limit, strict validation, size-лимиты.

## Changelog
- 2026-06-01: bootstrap модуля (architect). Зафиксированы [ADR-009](../../adr/ADR-009-admin-token-auth.md) (admin-auth), контракты grant/get-wallet, RBAC, фазы, тесты. Scope backend.
- 2026-06-01: реализован backend (`src/app/api_gateway/routers/admin.py`, `src/app/admin/service.py`): `POST /v1/admin/wallet/grant` + `GET /v1/admin/wallet/{userId}` под `require_admin`/`X-Admin-Token`, audit `admin_grant`, отдельный rate limit. Отревьюен и протестирован — offline-сьют зелёный (455/455, вкл. e2e admin-grant/get-wallet). Статус → «Реализован».
- 2026-06-23: проектирование Hermes-интеграции ([ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md), architect). Канонизирован путь `POST /v1/admin/credits/grant` (`wallet/grant` → переходный алиас); добавлен контракт нового `POST /v1/admin/subscription/grant` (метод `SubscriptionService.admin_grant`, audit `admin_subscription_grant`, идемпотентность `admin-sub-grant:{idempotencyKey}`). Обновлены 00-overview/06-rbac/07-implementation-phases/09-testing. subscription/grant — статус «спроектирован», ожидает реализации backend.
- 2026-07-15: проектирование admin-списания баланса ([ADR-061](../../adr/ADR-061-admin-wallet-debit.md), architect). Закрыт прод-пробел (коррекция баланса делалась прямым SQL). Добавлен контракт `POST /v1/admin/wallet/debit` — семантика debit-на-дельту (не set-абсолют), реюз `WalletService.consume` (без нового метода сервиса, без миграции), audit `admin_debit`, метрика `admin_debit_total`, `409 insufficient_credits` при недостаче (не clamp), `wallets.debt` не затрагивается. Обновлены 02-api-contracts/09-testing (+ 00-overview/06-rbac). wallet/debit — статус «спроектирован», ожидает реализации backend. Q-061-1 (коррекция самого долга — вне scope).
