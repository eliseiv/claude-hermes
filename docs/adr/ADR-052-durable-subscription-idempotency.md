# ADR-052 — Durable idempotency-якорь для subscription-grant (оба пути `grantCredits`)

- Статус: Accepted
- Дата: 2026-06-24
- Связан с: [ADR-048](ADR-048-admin-credits-and-subscription-grant.md) (**расширяет §2**), [ADR-005](ADR-005-idempotency-ledger.md) (idempotency-ledger), [ADR-006 §2](ADR-006-credit-billing-and-subscription-grant.md) (grant при подписке), [ADR-009](ADR-009-admin-token-auth.md) (admin-auth), [03-data-model.md](../03-data-model.md), [modules/admin/](../modules/admin/README.md), [modules/subscription/](../modules/subscription/README.md)
- Контракт данных: новая таблица `subscription_grant_events` (миграция `0015`)
- Закрывает: [TD-030](../100-known-tech-debt.md)

## Context

[ADR-048 §2](ADR-048-admin-credits-and-subscription-grant.md) ввёл `POST /v1/admin/subscription/grant` (`SubscriptionService.admin_grant`). Идемпотентность зависит от `grantCredits`:
- `grantCredits=true` → ключ `admin-sub-grant:{idempotencyKey}` записан в `ledger_transactions` → durable-якорь → строгий `409` на «тот же ключ, другой payload».
- `grantCredits=false` → **записи в `ledger_transactions` нет** → durable-якоря нет → **later-writer-wins** (повтор с другим `plan`/`expiresAt` молча перезаписывает подписку, аудируется, но не `409`).

[TD-030](../100-known-tech-debt.md) зафиксировал это как осознанный долг: строгий `409` на коллизию ключа недостижим для пути `grantCredits=false`. Требуется durable idempotency-якорь для **subscription-операций отдельно от ledger**, чтобы строгий `409` был достижим для **обоих** путей `grantCredits`.

## Decision

### 1. Таблица `subscription_grant_events` (миграция `0015`)

Durable-якорь subscription-операций **вне ledger** (DDL — [03-data-model.md §23](../03-data-model.md)):

```sql
CREATE TABLE subscription_grant_events (
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,                 -- admin idempotencyKey операции
    payload_hash    TEXT NOT NULL,                 -- sha256 канонизированного payload (plan|expiresAt|grantCredits)
    plan            TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    grant_credits   BOOLEAN NOT NULL,
    ledger_tx_id    UUID,                          -- id credit-tx при grantCredits=true (nullable)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_subscription_grant_idempotency ON subscription_grant_events (user_id, idempotency_key);
```

- `UNIQUE (user_id, idempotency_key)` — durable-якорь для **обоих** путей `grantCredits` (в т.ч. `false`, где ledger-строки нет).
- `payload_hash` — sha256 канонизированного payload (`plan` ‖ ISO8601 `expiresAt` ‖ `grantCredits`); сравнение payload без хранения «сырого» представления, устойчиво к порядку полей.
- Цепочка: `0014` → `0015` (single head; `0014` — `wallets.debt`, [ADR-051](ADR-051-agent-debt-reconciliation.md)).

### 2. Алгоритм `admin_grant` — единый для обоих `grantCredits`

В **одной БД-транзакции** (по образцу `adapty_webhook_events`, [ADR-029 §6](ADR-029-adapty-subscription-webhook.md)):

1. `INSERT INTO subscription_grant_events (user_id, idempotency_key, payload_hash, ...) ... ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING ...`.
2. **Конфликт (ничего не вставлено)** → прочитать существующую строку:
   - `payload_hash` совпадает → **идемпотентный реплей**: ответ `idempotentReplay=true`, без повторного upsert/grant (вернуть состояние из строки, `ledgerTxId` из `ledger_tx_id`).
   - `payload_hash` **отличается** → **строгий `409`** (тот же ключ, другой payload) — **для обоих `grantCredits`**, мутации нет. Это устраняет later-writer-wins пути `grantCredits=false`.
3. **Вставлено (новая операция)** →
   - upsert `subscriptions` → `status=active`, `plan`, `expires_at`;
   - при `grantCredits=true` — `WalletService.grant(idempotency_key="admin-sub-grant:{idempotencyKey}", ...)` (как [ADR-048 §2](ADR-048-admin-credits-and-subscription-grant.md)); полученный `ledgerTxId` записать в `subscription_grant_events.ledger_tx_id`;
   - audit `admin_subscription_grant` (как [ADR-048 §2](ADR-048-admin-credits-and-subscription-grant.md), `idempotencyKey` не редактируется — [ADR-050](ADR-050-redaction-idempotencykey-allowlist.md)).
4. Commit. Сбой → откат всей транзакции (на ретрае `idempotency_key` снова свободен — INSERT откатился; двойного начисления нет: `grant` идемпотентен, `subscription_grant_events` — единая точка дедупликации).

### 3. Двойная UNIQUE-граница при `grantCredits=true`

При `grantCredits=true` действуют **две** независимые UNIQUE-границы: `subscription_grant_events (user_id, idempotency_key)` (новая) и `ledger_transactions (user_id, idempotency_key="admin-sub-grant:*")` ([ADR-005](ADR-005-idempotency-ledger.md)). Они согласованы (обе пишутся в одной транзакции). Источник истины конфликта payload — `subscription_grant_events.payload_hash` (покрывает `plan`/`expiresAt`/`grantCredits`, а не только `amount`, как ledger-конфликт). Это **усиливает** прежний `409` пути `grantCredits=true` (раньше конфликт ловился только по ledger-`amount`; теперь — по полному payload подписки).

### 4. Контракт ответа — без breaking change

- Request/Response `POST /v1/admin/subscription/grant` ([admin/02-api-contracts.md](../modules/admin/02-api-contracts.md)) **не меняются**. Меняется лишь **поведение идемпотентности** для `grantCredits=false`: «тот же ключ, другой payload» теперь → `409` (раньше — later-writer-wins перезапись). Это сужение (строже), не расширение контракта; legitimate-повтор с тем же payload по-прежнему `idempotentReplay=true`.
- `SubscriptionService.admin_grant` ([subscription/02-api-contracts.md](../modules/subscription/02-api-contracts.md)) дополняется durable-якорем (внутренний контракт метода).

## Consequences

**Положительные:**
- Строгий `409` на коллизию ключа достижим для **обоих** путей `grantCredits` — закрывает [TD-030](../100-known-tech-debt.md).
- Единый алгоритм идемпотентности subscription-grant (не ветвится по `grantCredits` для дедупликации) — проще и предсказуемее.
- `payload_hash` ловит конфликт по полному payload подписки (`plan`/`expiresAt`/`grantCredits`), а не только по ledger-`amount`.
- Паттерн повторяет проверенный `adapty_webhook_events` (одна транзакция, ON CONFLICT DO NOTHING).

**Отрицательные / ограничения:**
- Новая таблица ради единственного admin-пути (та же причина, по которой [TD-030](../100-known-tech-debt.md) откладывал durable-якорь). Оправдано в рамках prod-harden: строгий конфликт-detection нужен для операторской корректности перед приёмом трафика.
- Поведение `grantCredits=false` меняется (later-writer-wins → `409` на разный payload) — операторы, полагавшиеся на «перезапись повтором», должны слать другой `idempotencyKey` для намеренного изменения подписки. Зафиксировать в [admin/02-api-contracts.md](../modules/admin/02-api-contracts.md).

## Alternatives

1. **Оставить как есть (later-writer-wins для `grantCredits=false`).** Отвергнуто — это и есть [TD-030](../100-known-tech-debt.md); prod-harden требует строгого конфликт-detection.
2. **Колонка `idempotency_key` UNIQUE прямо в `subscriptions`.** Отвергнуто: `subscriptions` — одна строка на пользователя (PK `user_id`), а операций grant с разными ключами может быть много во времени; журнал событий в отдельной таблице корректнее (история операций + дедуп).
3. **Записывать «нулевой» ledger-tx при `grantCredits=false` ради durable-якоря.** Отвергнуто: `ledger_transactions.amount > 0` CHECK ([03-data-model.md](../03-data-model.md)); фиктивная нулевая/единичная строка засоряет ledger и сверку — durable-якорь должен быть вне ledger (как и для subscription-семантики в целом).
