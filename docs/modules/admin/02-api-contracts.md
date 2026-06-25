# Admin — API Contracts

Все admin-эндпоинты под префиксом `/v1/admin/*`. Авторизация — заголовок `X-Admin-Token` (изолированный
admin-секрет, [ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`. **Пользовательский
JWT не авторизует admin-действия.** Отсутствие/несовпадение токена → `401`. Отдельный rate limit (дефолт 10 req/min
per source IP, конфигурируемо), `extra='forbid'`, тело ≤ 8 KB.

## POST /v1/admin/credits/grant
Начисление кредитов пользователю (саппорт/компенсация). Канонический путь ([ADR-048 §1](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).

> **Переходный алиас:** `POST /v1/admin/wallet/grant` — прежний путь той же операции, сохраняется на переходный период (обратная совместимость), затем ретируется. Тело/ответ/правила обоих путей **идентичны**; решение о сроке ретиринга алиаса фиксирует `backend` при реализации ([ADR-048 §1](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "amount": 100,
  "idempotencyKey": "string",
  "reason": "string"
}
```
- `userId` — UUID существующего пользователя (см. Правила §Несуществующий userId).
- `amount` — целое **> 0** (BIGINT, целые кредиты, без дробей). `amount <= 0` → `422`.
- `idempotencyKey` — непустая строка, `max_length` 128. Ключ идемпотентности начисления (передаётся в `WalletService.grant(idempotency_key=...)`).
- `reason` — **обязателен**, непустая строка, `max_length` 512. Пишется в audit `admin_grant` и `ledger_transactions.meta`.

### Response (200)
```json
{
  "newBalance": 1100,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `newBalance` — баланс после начисления.
- `ledgerTxId` — id `ledger_transactions` (`type=credit`).
- `idempotentReplay` — `true`, если ключ уже был использован с тем же payload (повторного начисления не было).

### Правила
- Переиспользует `WalletService.grant(user_id, amount, idempotency_key, meta, reason)` (`src/app/wallet/service.py:174`)
  — атомарно, идемпотентно по `(user_id, idempotency_key)`, пишет `ledger_transactions(type=credit)` + audit `billing_credit`.
- **Дополнительно** пишется audit-событие `admin_grant` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`,
  `ledgerTxId`) — отдельно от `billing_credit`, фиксирует именно admin-инициацию. **Секрет `X-Admin-Token` в audit не пишется.**
- Идемпотентность: тот же `idempotencyKey` + тот же payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- Тот же `idempotencyKey`, **другой** `amount` → `409` (конфликт, как в `WalletService.grant`), без начисления.
- **Несуществующий userId → `404 {error.code:"user_not_found"}`** (admin-grant **не создаёт** пользователей — см. 03-architecture; обоснование ниже).
- `reason` отсутствует/пустой → `422`.

## POST /v1/admin/subscription/grant
Ручная выдача/активация подписки пользователю без покупки через App Store/Adapty (саппорт/компенсация/тестовый доступ). Новый эндпоинт ([ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "plan": "string",
  "expiresAt": "ISO8601",
  "idempotencyKey": "string",
  "reason": "string",
  "grantCredits": false
}
```
- `userId` — UUID **существующего** пользователя (см. Правила §Несуществующий userId).
- `plan` — непустая строка, `max_length` 64. Идентификатор плана подписки (хранится в `subscriptions.plan`).
- `expiresAt` — ISO8601-таймштамп окончания периода подписки. **Должен быть в будущем** (> `now()`), иначе `422`.
- `idempotencyKey` — непустая строка, `max_length` 128. Ключ идемпотентности admin-операции.
- `reason` — **обязателен**, непустая строка, `max_length` 512. Пишется в audit `admin_subscription_grant`.
- `grantCredits` — bool, опционален (дефолт `false`). При `true` — дополнительно начисляет `SUBSCRIPTION_CREDITS_PER_PERIOD` ([ADR-006 §2](../../adr/ADR-006-credit-billing-and-subscription-grant.md)) через `WalletService.grant`.

### Response (200)
```json
{
  "status": "active",
  "plan": "string",
  "expiresAt": "ISO8601",
  "creditsGranted": 1000,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `status` — всегда `active` после успешной выдачи.
- `plan` / `expiresAt` — эхо применённых значений (состояние строки `subscriptions` после upsert).
- `creditsGranted` — присутствует и `> 0` **только** при `grantCredits=true`; иначе `null`/опущено.
- `ledgerTxId` — id `ledger_transactions` начисления; присутствует **только** при фактическом начислении кредитов (`grantCredits=true`), иначе `null`/опущено.
- `idempotentReplay` — `true`, если та же admin-операция уже была применена с тем же payload (повторной мутации/начисления не было). При `grantCredits=false` и **изменённом** payload повтор не реплей, а перезапись (later-writer-wins, см. §Правила/Идемпотентность).

### Правила
- **Метод** — `SubscriptionService.admin_grant(user_id, plan, expires_at)` (контракт — [modules/subscription/02-api-contracts.md §SubscriptionService.admin_grant](../subscription/02-api-contracts.md#subscriptionserviceadmin_grant-внутренний-admin-выдача-подписки)):
  - **Upsert `subscriptions`** для `user_id` → `status=active`, `plan=<plan>`, `expires_at=<expiresAt>`.
  - При `grantCredits=true` — `WalletService.grant` с idempotency-ключом **`admin-sub-grant:{idempotencyKey}`** (отдельное пространство ключей от `credits/grant` и StoreKit/Adapty), чтобы повторный admin-grant не начислял дважды.
- **Идемпотентность** ([ADR-052](../../adr/ADR-052-durable-subscription-idempotency.md), расширяет [ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md); durable-якорь — таблица `subscription_grant_events (user_id, idempotency_key)` UNIQUE + `payload_hash`, [03-data-model.md §23](../../03-data-model.md)). **Единая семантика для обоих `grantCredits`** (закрывает [TD-030](../../100-known-tech-debt.md)):
  - Тот же `idempotencyKey` + тот же payload (совпавший `payload_hash` = sha256 `plan|expiresAt|grantCredits`) → `idempotentReplay=true`, без повторного upsert/начисления.
  - Тот же `idempotencyKey`, **другой** payload → **строгий `409`** — теперь **в т.ч. при `grantCredits=false`** (durable-якорь `subscription_grant_events` есть независимо от ledger). Прежнее later-writer-wins-поведение пути `grantCredits=false` устранено ([TD-030](../../100-known-tech-debt.md) закрывается). Намеренное изменение подписки оператором — **новым** `idempotencyKey`.
  - При `grantCredits=true` — **двойная** UNIQUE-граница: `subscription_grant_events` (источник истины конфликта payload) + `ledger_transactions (user_id, "admin-sub-grant:{idempotencyKey}")` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)); обе пишутся в одной транзакции.
- **Audit-событие `admin_subscription_grant`** (actor=admin, `userId`, `plan`, `expiresAt`, `reason`, `idempotencyKey`, `grantCredits`, `ledgerTxId?`). **Секрет `X-Admin-Token` в audit не пишется.**
- **Несуществующий userId → `404 {error.code:"user_not_found"}`** (admin не создаёт пользователей — тот же принцип, что `credits/grant`; см. §Обоснование ниже, [Q-009-2](../../99-open-questions.md)).
- `reason` отсутствует/пустой → `422`. `expiresAt` в прошлом/`now()` → `422`. Лишнее поле (`extra='forbid'`) → `422`.
- После выдачи последующий `POST /v1/agent/run` ([ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md)) проходит policy-gate по активной подписке ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md)).
- **Ограничение источника истины:** admin-grant — второй (после StoreKit/Adapty) путь активации подписки. Митигация — отдельный audit + идемпотентность + узкий круг операторов ([ADR-048 §Consequences](../../adr/ADR-048-admin-credits-and-subscription-grant.md)).

## GET /v1/admin/wallet/{userId}
Read-only просмотр кошелька для саппорта.

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Response (200)
```json
{
  "userId": "uuid",
  "balance": 1100,
  "debt": 0,
  "lastTransactions": [
    { "id": "uuid", "type": "credit|debit", "amount": 100, "createdAt": "ISO8601", "meta": {} }
  ]
}
```
- Переиспользует `WalletService.get_wallet_view(user_id, last_n)` (дефолт `last_n=20`, по `created_at DESC`).
- `debt` — текущий `wallets.debt` ([ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md)): непогашенная несписанная дельта агентного прогона (кредиты). `0` при отсутствии долга или выключенном `AGENT_DEBT_RECONCILE_ENABLED`. Аддитивное поле (обратная совместимость).
- `meta` — без секретов (usage/model/reason).

### Правила
- Несуществующий `userId` → `404 {error.code:"user_not_found"}` (read-only не создаёт пользователя).
- Только чтение; не мутирует состояние и не пишет мутирующий audit (логируется на уровне tool/request lifecycle).

## Обоснование «404 на несуществующем userId» (не admin-provisioning)
Ни одно admin-действие (`credits/grant`, `subscription/grant`, `get-wallet`) **не создаёт** пользователей. Причины:
- Источник истины идентичности — доверенный JWT issuer ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md));
  создание `users` из admin-API ввело бы второй, неаутентифицированный путь рождения идентичности и риск
  начисления на «фантомный» (опечатанный) `userId`.
- `404` делает опечатку в `userId` видимой оператору сразу, вместо молчаливого создания мусорного аккаунта с балансом.
- Реальные пользователи создаются лениво при первом аутентифицированном запросе (ADR-007); к моменту легитимного
  admin-grant пользователь, как правило, уже существует. Если нужно начислить «наперёд» — это отдельный продуктовый
  вопрос, не решается тихим созданием строки. См. [Q-009-2](../../99-open-questions.md) (не блокер; дефолт — `404`).
