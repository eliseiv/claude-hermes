# ADR-061 — Admin-эндпоинт списания баланса `POST /v1/admin/wallet/debit`

- Статус: Accepted
- Дата: 2026-07-15
- Связан с: [ADR-009](ADR-009-admin-token-auth.md) (admin-auth — **неизменна**), [ADR-048](ADR-048-admin-credits-and-subscription-grant.md) (admin credits/grant — симметричный контур начисления), [ADR-005](ADR-005-idempotency-ledger.md) (idempotency-ledger), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (billing), [ADR-047 §6](ADR-047-usage-based-billing-for-agent.md) (самодостаточная атомарность `consume`), [ADR-051](ADR-051-agent-debt-reconciliation.md) (debt/clawback — **инвариант долга не затрагивается**), [modules/admin/](../modules/admin/README.md), [modules/wallet-ledger/](../modules/wallet-ledger/README.md), [03-data-model.md](../03-data-model.md)
- Контракт данных: **без миграции** (переиспользует `ledger_transactions(type=debit)` + `wallets.balance`, оба уже существуют)

## Context

Admin-API ([ADR-009](ADR-009-admin-token-auth.md), [ADR-048](ADR-048-admin-credits-and-subscription-grant.md)) умеет только **начислять** кредиты: `POST /v1/admin/credits/grant` (+ переходный алиас `/v1/admin/wallet/grant`), схема `AdminGrantRequest.amount > 0`. **Списания/корректировки баланса оператором нет.** На проде понадобилось вручную скорректировать баланс тестового пользователя — пришлось выполнять **прямой SQL** (`UPDATE wallets SET balance=...` + `INSERT ledger_transactions(type=debit)`), в обход `WalletService`. Прямой SQL:
- обходит бизнес-логику (`_ensure_wallet`, savepoint-атомарность, идемпотентность, audit `billing_debit`) — риск рассинхронизации инварианта `balance == Σ(credit) − Σ(debit)` и отсутствия следа в audit;
- не идемпотентен (повтор двойного списания);
- не проверяет `CHECK (balance >= 0)` заранее (падает транзакцией, а не понятной ошибкой).

Нужен **штатный** admin-эндпоинт корректировки баланса вниз, симметричный `credits/grant`.

## Decision

### Выбор семантики: **списание на дельту (debit)**, НЕ set-абсолют

Рассмотрены три варианта (ТЗ):
- **(A) `POST /v1/admin/wallet/debit`** — списание на `amount > 0` (симметрично `credits/grant`): ledger `type=debit`, `balance -= amount`.
- **(B) `POST /v1/admin/wallet/adjust`** — установка **абсолютного** `targetBalance`, сервис вычисляет `delta` и пишет credit/debit.
- **(C)** оба.

**Выбран (A) — debit-на-дельту.** Обоснование:

1. **Durable-идемпотентность есть только у (A).** Единственный durable-якорь идемпотентности — `ledger_transactions.idempotency_key` (UNIQUE `(user_id, idempotency_key)`, [ADR-005](ADR-005-idempotency-ledger.md)). У debit'а `amount` фиксирован в payload и совпадает с ledger-строкой → повтор с тем же ключом детерминированно реплеится (как `grant`). У set-абсолюта (B) реальная дельта вычисляется из **live-баланса** на момент запроса; при повторе баланс уже другой, durable-якоря на «целевое значение» нет → двойное применение либо нужен отдельный анти-повтор-механизм. (A) переиспользует готовую идемпотентность `WalletService.consume` без нового кода.

2. **Симметрия и минимум кода.** (A) — зеркало `credits/grant`: та же форма тела/ответа, тот же контур `adminToken`, тот же принцип «реюз `WalletService`, admin лишь добавляет user-check + audit + метрику». `consume` (списание) уже существует и самодостаточно-атомарен ([ADR-047 §6](ADR-047-usage-based-billing-for-agent.md)).

3. **Чистая семантика `CHECK (balance >= 0)`.** Списание больше баланса → существующий `InsufficientCreditsError` (savepoint-откат, orphan-строки не остаётся) → `409 insufficient_credits`. Ошибка уже реализована в `consume`, не нужно новой.

4. **Set-абсолют (B) конфликтует с clawback долга ([ADR-051](ADR-051-agent-debt-reconciliation.md)).** Увеличение баланса идёт через `grant`, который при `AGENT_DEBT_RECONCILE_ENABLED` **гасит долг из начисления** (`repaid = min(amount, debt)`), поэтому итоговый `balance ≠ targetBalance` при `debt > 0` — семантика «сделать ровно N» нарушается. Реализовать (B) без этого противоречия = дублировать балансовую арифметику в обход `grant`/`consume`, чего мы избегаем.

5. **Кейс «сделать чтобы осталось N» покрывается композицией.** Оператор читает текущий баланс через `GET /v1/admin/wallet/{userId}` ([ADR-048](ADR-048-admin-credits-and-subscription-grant.md)) и применяет `debit` на `current − N` (если N < current) или `credits/grant` на `N − current` (если N > current). Триада `GET wallet` + `credits/grant` + `wallet/debit` даёт полный контроль коррекции баланса без сложности и рисков идемпотентности set-абсолюта. Простота — критерий ([00-vision](../00-vision.md)).

Вариант (C) отвергнут как избыточный: set-абсолют не добавляет возможностей поверх триады, но вносит недетерминированную идемпотентность и конфликт с долгом. Если продуктовая потребность в «одном атомарном set-N» подтвердится — вводится отдельным ADR с durable-якорем на операцию (не ledger).

### 1. Контракт `POST /v1/admin/wallet/debit`

- Тело `AdminDebitRequest` (StrictModel, `extra='forbid'`): `{ userId: uuid, amount: int>0, idempotencyKey: str(1..128), reason: str(1..512) }` — **та же форма**, что `AdminGrantRequest`.
- Ответ (200) — форма `AdminGrantResponse`: `{ newBalance, ledgerTxId, idempotentReplay }` (backend переиспользует `AdminGrantResponse` либо объявляет alias `AdminDebitResponse` идентичной формы).
- `newBalance` — баланс после списания; `ledgerTxId` — id `ledger_transactions(type=debit)`; `idempotentReplay=true` — тот же ключ + тот же payload (повторного списания не было).
- Контур **`adminToken`** (`X-Admin-Token`, `require_admin`), тег **Admin**, admin body cap ≤ 8 KB, admin rate limit — как у остальных `/v1/admin/*` ([ADR-009](ADR-009-admin-token-auth.md)).

### 2. Реализация — реюз `WalletService.consume` (без нового метода сервиса и без прямого SQL)

- Новый `AdminService.debit(user_id, amount, idempotency_key, reason)` — зеркало `AdminService.grant`:
  - `_require_user_exists` → несуществующий `userId` → `404 user_not_found` (admin не создаёт пользователей, [ADR-007](ADR-007-lazy-user-provisioning.md)/[ADR-009](ADR-009-admin-token-auth.md));
  - вызывает `WalletService.consume(user_id, amount, idempotency_key, meta={"source":"admin_debit","reason":reason}, session_id=None)`;
  - `session_id=None` → `consume` пропускает `_validate_session`; списание вне какой-либо чат-сессии;
  - пишет **дополнительный** audit `admin_debit` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`, `ledgerTxId`, `idempotentReplay`) — сверх `billing_debit`, который пишет `consume`. Секрет `X-Admin-Token` в audit **не** пишется;
  - метрика `admin_debit_total{result}` (`success|conflict|insufficient|not_found`).
- **Новый метод `WalletService` НЕ требуется** — `consume` уже покрывает: `_ensure_wallet`, savepoint-атомарный INSERT debit + условный `UPDATE balance = balance − amount WHERE balance >= amount`, идемпотентность по `(user_id, idempotency_key)`, audit `billing_debit`.

### 3. Недостаточный баланс — `409 insufficient_credits` (НЕ clamp)

При `amount > balance` `consume` матчит 0 строк условным UPDATE → поднимает `InsufficientCreditsError` (savepoint-откат just-inserted debit-строки, баланс не тронут) → `409 {error.code:"insufficient_credits"}`. **Clamp (тихое списание до 0) отвергнут:** тихо занизить `amount` до баланса скрыло бы намерение оператора и дало бы `balance ≠ ожидаемого`. Оператор видит текущий баланс через `GET /v1/admin/wallet/{userId}` и обязан задать корректный `amount` (для обнуления — `amount = current balance`). Переиспользуется **существующий** код ошибки `insufficient_credits` (тот же, что chat-debit), а не новый `insufficient_balance` — единообразие с пользовательским контуром.

### 4. Идемпотентность

- По `(user_id, idempotencyKey)` через ledger (как `grant`): тот же ключ + `type=debit` + тот же `amount` → тот же `ledgerTxId`, `idempotentReplay=true`, **без повторного списания**.
- Тот же ключ, **другой** `amount` (или существующая строка `type=credit`) → `409 conflict` («idempotency key reused with different payload», из `consume`).
- Namespace ключей — общий с прочими ledger-операциями пользователя (grant/agent/chat); оператор обязан использовать свежий уникальный `idempotencyKey` на каждую логическую коррекцию.

### 5. Взаимодействие с debt/clawback ([ADR-051](ADR-051-agent-debt-reconciliation.md)) — инвариант долга сохранён

- Admin-debit использует `meta.source="admin_debit"`. `WalletService._agent_reconcile_applies` требует `meta.source == "agent_run"` → для admin-debit возвращает `False` **независимо** от `AGENT_DEBT_RECONCILE_ENABLED`. Следовательно admin-debit идёт **обычным** savepoint-путём и **не** трогает `wallets.debt` (ни частичного списания, ни accrual долга, ни clawback).
- Списание **не читает и не изменяет** `wallets.debt`. Долг растёт только из недобора агентного прогона (§2.1 ADR-051) и гасится только clawback'ом на `grant` (§3 ADR-051). Admin-debit ортогонален долгу — инвариант `debt >= 0` и семантика долга не нарушаются.
- Коррекция **самого долга** оператором — вне scope этого ADR (при подтверждённой потребности — отдельный `admin/debt-adjust` эндпоинт, [Q-061-1](../99-open-questions.md)).

### 6. Путь эндпоинта

Канонический путь — **`/v1/admin/wallet/debit`** (namespace `wallet`, где уже живёт `GET /v1/admin/wallet/{userId}`; «debit» — ledger-термин кошелька). Алиас не вводится (новый эндпоинт, legacy-совместимость не требуется). В OpenAPI — тег Admin, scheme `adminToken` ([08-api-documentation.md R2.2](../08-api-documentation.md)).

## Consequences

**Положительные:**
- Штатная коррекция баланса вниз без прямого SQL: атомарно, идемпотентно, с audit-следом (`billing_debit` + `admin_debit`).
- Минимум кода: реюз `WalletService.consume` и admin-auth ([ADR-009](ADR-009-admin-token-auth.md)); без миграции, без нового метода сервиса.
- Инвариант `balance == Σ(credit) − Σ(debit)` и `CHECK (balance >= 0)` соблюдены by construction; долг ([ADR-051](ADR-051-agent-debt-reconciliation.md)) не затрагивается.
- Триада `GET wallet` + `credits/grant` + `wallet/debit` покрывает полную коррекцию баланса (в т.ч. «сделать ровно N»).

**Отрицательные / ограничения:**
- «Сделать ровно N» — двухшаговая операция оператора (прочитать баланс → debit/grant на дельту), не один атомарный вызов. Принято осознанно ради durable-идемпотентности и отсутствия конфликта с clawback долга.
- Admin-debit не корректирует `wallets.debt` — коррекция долга вне scope ([Q-061-1](../99-open-questions.md)).
- `admin_debit` обезличен (actor=admin, как `admin_grant`) — атрибуция конкретного оператора — [Q-009-1](../99-open-questions.md).

## Alternatives

1. **Set-абсолют `wallet/adjust` (вариант B).** Отвергнут: недетерминированная идемпотентность (дельта от live-баланса, нет durable-якоря на целевое значение), конфликт с clawback долга (`grant` гасит долг → `balance ≠ targetBalance`), дублирование балансовой арифметики в обход `grant`/`consume`. См. Decision §Выбор семантики.
2. **Оба эндпоинта (вариант C).** Избыточно: set-абсолют не добавляет возможностей поверх триады `GET wallet`+`grant`+`debit`, но вносит перечисленные риски.
3. **Clamp вместо ошибки при `amount > balance`.** Отвергнут: тихое занижение скрывает намерение оператора, даёт неожиданный итоговый баланс; `409 insufficient_credits` явно сигнализирует ошибку размера.
4. **Оставить прямой SQL / ad-hoc скрипт.** Отвергнут: обходит идемпотентность, атомарность и audit, риск рассинхронизации инварианта ledger — исходная проблема.
5. **Новый код ошибки `insufficient_balance`.** Отвергнут: переиспользуем существующий `insufficient_credits` (тот же смысл «недостаточно кредитов для списания») ради единообразия с chat-debit.
