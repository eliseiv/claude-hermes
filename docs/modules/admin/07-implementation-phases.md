# Admin — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| ADM-1 | Config: `ADMIN_API_SECRET` (+ опц. `ADMIN_API_SECRET_PREV`), `ADMIN_RATE_LIMIT_PER_MIN` (дефолт 10) в pydantic-settings. Добавить `X-Admin-Token` в redaction allowlist. | — |
| ADM-2 | Зависимость `require_admin` (constant-time compare обоих секретов, `401` при несовпадении; **без** provisioning/trial/`get_current_user`). | ADM-1 |
| ADM-3 | Роутер `api_gateway/routers/admin.py` (`/v1/admin/*`) + отдельный rate limit per source IP + size-лимит ≤ 8 KB. | ADM-2 |
| ADM-4 | `POST /v1/admin/credits/grant` (канонический) + `POST /v1/admin/wallet/grant` (переходный алиас, тот же handler): Pydantic-схема (`extra='forbid'`, `amount>0`, `reason` непустой); проверка существования `users(userId)` → `404`; вызов `WalletService.grant`; audit `admin_grant`; ответ `{newBalance, ledgerTxId, idempotentReplay}`. | ADM-3, Wallet |
| ADM-5 | `GET /v1/admin/wallet/{userId}`: проверка существования → `404`; `WalletService.get_wallet_view`. | ADM-3, Wallet |
| ADM-6 | Audit: новый `eventType=admin_grant` в каталоге Audit; убедиться, что секрет не логируется. | ADM-4, Audit |
| ADM-7 | Метрика `admin_grant_total{result=success|conflict|not_found}` (observability). | ADM-4 |
| ADM-8 | `SubscriptionService.admin_grant(user_id, plan, expires_at, *, grant_credits, idempotency_key, reason)` ([ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md), [контракт](../subscription/02-api-contracts.md#subscriptionserviceadmin_grant-внутренний-admin-выдача-подписки)): атомарный upsert `subscriptions(status=active)` + опц. `WalletService.grant` с ключом `admin-sub-grant:{idempotencyKey}`; конфликт ключа → `409`. | ADM-3, Subscription, Wallet |
| ADM-9 | `POST /v1/admin/subscription/grant`: Pydantic-схема (`extra='forbid'`, `plan` непустой, `expiresAt` в будущем, `reason` непустой, `grantCredits` опц.); проверка существования `users(userId)` → `404`; вызов `SubscriptionService.admin_grant`; ответ `{status, plan, expiresAt, creditsGranted?, ledgerTxId?, idempotentReplay}`. | ADM-8 |
| ADM-10 | Audit: новый `eventType=admin_subscription_grant` в каталоге Audit (без секрета). | ADM-9, Audit |
| ADM-11 | Метрика `admin_subscription_grant_total{result=success|conflict|not_found}` (observability). | ADM-9 |

> Фазы ADM-1..7 — реализованы (credits/grant + get-wallet). ADM-8..11 — спроектированы под [ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md), ожидают backend.
>
> Admin-модуль не дублирует биллинг/подписку — тонкая обёртка над существующими `WalletService.grant`/`get_wallet_view` и новым `SubscriptionService.admin_grant`
> ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md), [ADR-009](../../adr/ADR-009-admin-token-auth.md), [ADR-048](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).
