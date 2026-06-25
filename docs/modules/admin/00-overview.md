# Admin — Overview

## Назначение
Операторская/саппорт-функция: начисление кредитов пользователю вне обычного биллинг-потока подписки
(компенсации, ручные гранты, поддержка), ручная выдача/активация подписки без покупки через App Store/Adapty
([ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md)) и read-only просмотр кошелька для разбора обращений.

## Scope (этот проход)
- `POST /v1/admin/credits/grant` — начислить `amount` кредитов пользователю `userId`, идемпотентно по `idempotencyKey`,
  с обязательным `reason`. Переиспользует существующий `WalletService.grant()` (`src/app/wallet/service.py:174`).
  `POST /v1/admin/wallet/grant` — **переходный алиас** той же операции ([ADR-048 §1](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).
- `POST /v1/admin/subscription/grant` — ручно активировать подписку (`plan`, `expiresAt`) пользователю `userId`,
  идемпотентно по `admin-sub-grant:{idempotencyKey}`, опц. начислить кредиты периода (`grantCredits`). Через
  `SubscriptionService.admin_grant()` ([ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).
- `GET /v1/admin/wallet/{userId}` — баланс + последние ledger-транзакции (read-only, для саппорта).
- Изолированная admin-авторизация: `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`.
- Аудит `admin_grant` / `admin_subscription_grant`, отдельный rate limit, strict validation, size-лимиты.

## Out of scope
- Мутации сверх начисления кредитов и выдачи подписки (нет admin-списания, нет правки BYOK/trial, нет удаления/создания пользователей).
- Admin-UI (только HTTP API).
- Персональная идентичность/атрибуция конкретного оператора (actor — обезличенный `admin`, [Q-009-1](../../99-open-questions.md)).
- Scope/least-privilege на уровне токена (один секрет = все admin-операции; разделение прав — [Q-009-1](../../99-open-questions.md)).

## Бизнес-правила
- BR-ADM-1: admin **не пользователь системы** — `require_admin` не создаёт строку `users` для actor'а, не запускает
  lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)), не читает/не трогает `users.trial_used`.
- BR-ADM-2: начисление идемпотентно по `idempotencyKey` (через `WalletService.grant`, unique index `ux_ledger_idempotency`);
  повторный вызов с тем же ключом и payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- BR-ADM-3: `reason` обязателен и пишется в audit (`admin_grant` / `admin_subscription_grant`) (и в `ledger_transactions.meta`, без секретов).
- BR-ADM-4: целевой `userId` **должен существовать** — ни одно admin-действие не создаёт пользователей (см. 03-architecture §Несуществующий userId).
- BR-ADM-5: `subscription/grant` идемпотентен по `admin-sub-grant:{idempotencyKey}` (отдельное пространство ключей от `credits/grant` и StoreKit/Adapty), upsert `subscriptions(status=active)` атомарно с опц. начислением кредитов; повтор с тем же payload → `idempotentReplay=true`, без двойной мутации/начисления ([ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).
