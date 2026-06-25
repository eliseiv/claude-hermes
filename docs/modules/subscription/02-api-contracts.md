# Subscription — API Contracts

> **Ретирование `POST /v1/subscription/sync` (prod-harden, ревизия [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md), [Q-029-2](../../99-open-questions.md) Closed, [TD-021](../../100-known-tech-debt.md)).** Для изолированного сервиса claude-hermes StoreKit-путь подписок **ретируется**: роут `POST /v1/subscription/sync` и подписочная ветка `StoreKitVerifier`→grant удаляются; Adapty-вебхук ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)) — **единственный** путь подписок. Двойное начисление устранено by construction. `POST /v1/tokens/purchase` (consumable StoreKit IAP, [ADR-015](../../adr/ADR-015-consumable-token-iap.md)) и общий StoreKit-verifier для consumable **сохраняются** (не подписка). Раздел ниже описывает **ретируемый** `sync`-контракт (оставлен для трассируемости до удаления кода backend'ом).

## POST /v1/subscription/sync  (РЕТИРУЕТСЯ — [TD-021](../../100-known-tech-debt.md)/[ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md))
### Request
```json
{
  "userId": "uuid",
  "transaction": { "...StoreKit transaction payload (signed)..." }
}
```
- `transaction` — подписанный StoreKit payload (JWS signed transaction / App Store receipt). Конкретный формат — App Store Server API.

### Response (200)
```json
{
  "isSubscribed": true,
  "expiresAt": "ISO8601 | null",
  "plan": "string | null"
}
```

### Правила
- Сервер **верифицирует** транзакцию (подпись/через App Store Server API), не доверяет клиенту.
- Идемпотентность: повторный sync той же транзакции не создаёт дублирующих grant (по transactionId в meta).
- При успешной активации/продлении нового периода → Wallet.grant фикс. пакета `SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт 1000) кредитов, идемпотентно по `transactionId` периода ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- **Разовая покупка пакетов токенов** (consumable IAP) — **отдельный** endpoint `POST /v1/tokens/purchase` (модуль [token-purchase](../token-purchase/README.md), [ADR-015](../../adr/ADR-015-consumable-token-iap.md)), НЕ через `subscription/sync`. Использует общий StoreKit verifier, но отдельный путь grant (idempotency по consumable `transactionId`, `meta.source=token_purchase`). Subscription grant этим не затрагивается.
- refund/revocation → `status=expired`, `isSubscribed=false`.
- Невалидная/поддельная транзакция → `422`/`400` (тех. ошибка), подписка не меняется.
- StoreKit payload не логируется (redaction, [05-security.md](../../05-security.md)).
- **Test-mode (только e2e/CI, `STOREKIT_TEST_MODE=true`):** `transaction` принимается как HS256-JWS,
  подписанный `STOREKIT_TEST_SECRET`; извлекаются те же поля (`transactionId`/`expiresDate`/`productId`/
  …), активация и grant идут штатно. В prod (`STOREKIT_TEST_MODE=false`, дефолт) принимаются только
  реальные Apple-подписанные транзакции. См. [03-architecture.md](03-architecture.md#test-mode-верификации-storekit_test_mode), [TD-007](../../100-known-tech-debt.md).

## SubscriptionService.admin_grant (внутренний, admin-выдача подписки)
Метод сервиса для ручной активации подписки оператором — **не** HTTP-эндпоинт этого модуля. Вызывается из admin-роутера `POST /v1/admin/subscription/grant` ([ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md), HTTP-контракт — [modules/admin/02-api-contracts.md §POST /v1/admin/subscription/grant](../admin/02-api-contracts.md#post-v1adminsubscriptiongrant)).

### Сигнатура
```
SubscriptionService.admin_grant(
    user_id: UUID,
    plan: str,
    expires_at: datetime,
    *, grant_credits: bool = False,
    idempotency_key: str,
    reason: str,
) -> AdminGrantResult
```
- `AdminGrantResult` несёт `status="active"`, `plan`, `expires_at`, `credits_granted | None`, `ledger_tx_id | None`, `idempotent_replay: bool` (маппится в response admin-эндпоинта).

### Поведение
- **Durable-якорь `subscription_grant_events` ([ADR-052](../../adr/ADR-052-durable-subscription-idempotency.md), миграция `0015`, [03-data-model.md §23](../../03-data-model.md)).** В одной транзакции: `INSERT INTO subscription_grant_events (user_id, idempotency_key, payload_hash=sha256(plan|expiresAt|grantCredits), ...) ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING ...`.
- **Upsert `subscriptions`** для `user_id`: `status=active`, `plan=plan`, `expires_at=expires_at` — выполняется **только при новой вставке** в `subscription_grant_events`.
- При `grant_credits=true` — `WalletService.grant(user_id, SUBSCRIPTION_CREDITS_PER_PERIOD, idempotency_key="admin-sub-grant:"+idempotency_key, meta={source:"admin_subscription_grant", reason}, reason)` ([ADR-006 §2](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); полученный `ledger_tx_id` пишется в `subscription_grant_events.ledger_tx_id`. Idempotency-пространство **отдельно** от StoreKit `transactionId` и от `credits/grant`.
- Атомарность: INSERT events + upsert подписки + (опц.) grant кредитов + audit — в одной транзакции; при сбое — полный откат (на ретрае ключ снова свободен).
- **Источник истины:** этот путь — admin-альтернатива Adapty-активации. Не верифицирует платёжную транзакцию (осознанное admin-действие, аудируется как `admin_subscription_grant`). Ограничения — [ADR-048 §Consequences](../../adr/ADR-048-admin-credits-and-subscription-grant.md).
- **Несуществующий `user_id`** → ошибка `user_not_found` (admin-роутер транслирует в `404`); строка `users` не создаётся ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md), [Q-009-2](../../99-open-questions.md)).
- **Идемпотентность — durable для ОБОИХ `grant_credits` ([ADR-052](../../adr/ADR-052-durable-subscription-idempotency.md), закрывает [TD-030](../../100-known-tech-debt.md)):** конфликт + совпавший `payload_hash` → `idempotent_replay=true` (без upsert/grant); конфликт + другой `payload_hash` → **`409`** (admin-роутер транслирует), в т.ч. при `grant_credits=false`. При `grant_credits=true` — двойная UNIQUE-граница (`subscription_grant_events` + `ux_ledger_idempotency`). Прежнее later-writer-wins для `grant_credits=false` устранено. Контракт HTTP — [admin/02-api-contracts.md §POST /v1/admin/subscription/grant](../admin/02-api-contracts.md#post-v1adminsubscriptiongrant).
