# ADR-048 — Два admin-эндпоинта: `credits/grant` (реюз) и `subscription/grant` (новый)

- Статус: Accepted
- Дата: 2026-06-23
- Связан с: [ADR-009](ADR-009-admin-token-auth.md) (admin-auth — **неизменна**), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (grant кредитов / подписка), [ADR-007](ADR-007-lazy-user-provisioning.md) (provisioning), [ADR-002](ADR-002-access-policy-state-machine.md) (policy), [modules/admin/](../modules/admin/README.md), [modules/subscription/](../modules/subscription/README.md)

## Context

Control-plane-операторам нужны два admin-действия: начисление кредитов и ручная выдача/активация подписки (например, выдать доступ пользователю без покупки через App Store/Adapty). Защита — существующий ADMIN API-KEY (`X-Admin-Token`, [ADR-009](ADR-009-admin-token-auth.md), `require_admin`) — **без изменений**.

Существует `POST /v1/admin/wallet/grant` ([ADR-009](ADR-009-admin-token-auth.md), [modules/admin/02-api-contracts.md](../modules/admin/02-api-contracts.md)) — начисление кредитов через `WalletService.grant` (идемпотентно). Эндпоинта ручной выдачи подписки нет.

## Decision

### 1. `POST /v1/admin/credits/grant` — начисление кредитов (реюз/переименование)

- Функционально = существующий `POST /v1/admin/wallet/grant` ([ADR-009](ADR-009-admin-token-auth.md)): `WalletService.grant(user_id, amount, idempotency_key, meta, reason)`, идемпотентно по `(user_id, idempotency_key)`, audit `admin_grant`.
- Путь приводится к требуемому виду `/v1/admin/credits/grant`. Прежний путь `/v1/admin/wallet/grant` сохраняется как алиас на переходный период (обратная совместимость) либо ретируется — решение фиксируется backend'ом при реализации; контракт тела/ответа не меняется.
- Request/Response — как у текущего grant ([modules/admin/02-api-contracts.md §POST /v1/admin/credits/grant](../modules/admin/02-api-contracts.md#post-v1admincreditsgrant)): `{userId, amount>0, idempotencyKey, reason}` → `{newBalance, ledgerTxId, idempotentReplay}`. Несуществующий `userId` → `404 user_not_found` ([ADR-009](ADR-009-admin-token-auth.md), [Q-009-2](../99-open-questions.md): admin не провижинит users).

### 2. `POST /v1/admin/subscription/grant` — ручная выдача подписки (новый)

- Новый метод `SubscriptionService.admin_grant(user_id, plan, expires_at)` (контракт метода — [modules/subscription/02-api-contracts.md §SubscriptionService.admin_grant](../modules/subscription/02-api-contracts.md#subscriptionserviceadmin_grant-внутренний-admin-выдача-подписки)):
  - **Upsert `subscriptions`** в `status=active`, `plan=<plan>`, `expires_at=<expires_at>` (для существующего `user_id`).
  - **Опционально** начисляет `SUBSCRIPTION_CREDITS_PER_PERIOD` ([ADR-006 §2](ADR-006-credit-billing-and-subscription-grant.md)) через `WalletService.grant` — идемпотентно (ключ grant привязан к admin-операции, напр. `admin-sub-grant:{idempotencyKey}`), чтобы повторный admin-grant не начислял дважды.
  - **Семантика идемпотентности зависит от `grantCredits`** (durable-якорь — только `ledger_transactions.idempotency_key`, [03-data-model.md](../03-data-model.md)): при `grantCredits=true` ключ записан в ledger → **строгий `409`** на «тот же ключ, другой payload». При `grantCredits=false` записи в ledger нет → durable-якоря нет → **later-writer-wins** (повтор с другим `plan`/`expiresAt` перезаписывает подписку, аудируется в `admin_subscription_grant`; не `409`). Принято осознанно на старте: subscription-upsert идемпотентен по строке, коллизия с другим payload — операторская ошибка, видимая в audit. Durable idempotency-хранилище для subscription-grant (отдельная таблица/колонка с UNIQUE на ключ вне ledger) — отложено как [TD-030](../100-known-tech-debt.md).
  - Audit-событие `admin_subscription_grant` (actor=admin, `userId`, `plan`, `expiresAt`, `reason`, `grantCredits`, `idempotencyKey`, `ledgerTxId?`, без секрета; `idempotencyKey` не редактируется — [ADR-050](ADR-050-redaction-idempotencykey-allowlist.md)).
- Request: `{userId, plan, expiresAt, idempotencyKey, reason, grantCredits?: bool}`. Response: `{status:"active", plan, expiresAt, creditsGranted?, ledgerTxId?, idempotentReplay}`. Точная схема — [modules/admin/02-api-contracts.md §POST /v1/admin/subscription/grant](../modules/admin/02-api-contracts.md#post-v1adminsubscriptiongrant), `src/app/schemas/admin.py`.
- **Несуществующий `userId` → `404 user_not_found`** (тот же принцип, что credits/grant: admin не создаёт пользователей, [ADR-009](ADR-009-admin-token-auth.md)/[Q-009-2](../99-open-questions.md)).
- После выдачи подписки последующий `POST /v1/agent/run` ([ADR-045](ADR-045-hermes-as-agent-proxy.md)) проходит policy-gate (активная подписка, [ADR-002](ADR-002-access-policy-state-machine.md)).

### 3. Защита — `require_admin` / `X-Admin-Token` ([ADR-009](ADR-009-admin-token-auth.md), без изменений)

- Оба эндпоинта под изолированной admin-авторизацией: `require_admin`, constant-time `X-Admin-Token`, отдельный rate limit, `extra='forbid'`, size-лимиты, redaction `X-Admin-Token`. Пользовательский клиентский ключ ([ADR-044](ADR-044-client-api-key-auth.md)) admin-действия **не** авторизует; admin-токен не даёт доступа к пользовательским ресурсам. Эскалация невозможна by construction.
- Swagger security scheme — `adminToken` (`APIKeyHeader`, [08-api-documentation.md §R2.2](../08-api-documentation.md)), без изменений.

## Consequences

**Положительные:**
- Операторская выдача доступа (кредиты + подписка) без App Store/Adapty — поддержка/компенсации/тестовый доступ.
- Реюз `WalletService.grant` (идемпотентность готова) и admin-auth ([ADR-009](ADR-009-admin-token-auth.md)) — минимум нового кода.
- Идемпотентность подписочного admin-grant исключает двойное начисление при повторе.

**Отрицательные / ограничения:**
- Ручная выдача подписки — второй (admin) путь активации подписки помимо StoreKit/Adapty. Источник истины подписок размывается; митигация: отдельное audit-событие + идемпотентность + узкий круг операторов. Риск двойного начисления между путями — те же ключи различны ([03-data-model.md §Источники credit-tx](../03-data-model.md)); admin-grant использует свой idempotency-ключ.
- `admin_grant` обезличен (actor=admin, [ADR-009](ADR-009-admin-token-auth.md) §ограничения) — атрибуция оператора при необходимости — [Q-009-1](../99-open-questions.md).

## Alternatives

1. **Только начисление кредитов, без выдачи подписки.** Отвергнуто решением пользователя: нужен ручной путь активации подписки (выдать доступ без покупки).
2. **Выдавать подписку через эмуляцию StoreKit/Adapty-события.** Отвергнуто: сложнее и опаснее (подделка платёжного события); прямой `SubscriptionService.admin_grant` чище и аудируется как admin-действие.
3. **Отдельный admin-JWT с scope для subscription.** Избыточно при узком круге операторов ([ADR-009](ADR-009-admin-token-auth.md) Alternatives, [Q-009-1](../99-open-questions.md)).
