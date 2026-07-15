# ADR-063 — Клиентское отображение долга: аддитивные поля `debt` + `netBalance` в wallet и policy

- Статус: Accepted
- Дата: 2026-07-15
- Связан с: [ADR-051](ADR-051-agent-debt-reconciliation.md) (**расширяет клиентскую видимость** `wallets.debt`), [ADR-047](ADR-047-usage-based-billing-for-agent.md) (usage-based billing агентного пути), [ADR-061](ADR-061-admin-wallet-debit.md) (admin-view долга), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (1 кредит = 1 сообщение), [modules/wallet-ledger/](../modules/wallet-ledger/README.md), [modules/policy-engine/](../modules/policy-engine/README.md), [API-REFERENCE.md](../API-REFERENCE.md), [08-api-documentation.md](../08-api-documentation.md)
- Контракт данных: изменений схемы БД нет (читается существующая колонка `wallets.debt`, миграция `0014` из [ADR-051](ADR-051-agent-debt-reconciliation.md))
- Контракт API: **только аддитивные** поля в `WalletResponse` и `EffectivePolicyResponse` — обратно совместимо

## Context

[ADR-051](ADR-051-agent-debt-reconciliation.md) ввёл `wallets.debt` — накопленную несписанную дельту агентного прогона (пользователь потребил токены, но кредитов не хватило). Долг гасится clawback'ом при следующем пополнении и блокирует новый агентный прогон (`debt_outstanding`).

Пробел: долг виден **только** в admin-API (`AdminWalletResponse.debt`, [ADR-061](ADR-061-admin-wallet-debit.md)). Клиентские ответы `GET /v1/wallet` (`WalletResponse.balance`) и `GET /v1/policy/effective` (`EffectivePolicyResponse.creditsBalance`) всегда возвращают `wallets.balance ≥ 0` (инвариант `CHECK (balance >= 0)`, [03-data-model.md](../03-data-model.md)). Клиент, у которого есть долг, видит `balance = 0` и не понимает, почему следующий агентный прогон блокируется и почему после пополнения зачислилось меньше номинала (clawback). Требование продукта: клиент должен **видеть долговую ситуацию как отрицательный баланс**.

Ограничения решения (заданы продуктом):
- НЕ менять существующие `balance` / `creditsBalance` — остаются `≥ 0` (обратная совместимость всех текущих клиентов).
- Долг выразить **новыми** полями.
- Покрыть **оба** клиентских эндпоинта: `GET /v1/wallet` и `GET /v1/policy/effective`.

## Decision

В `WalletResponse` и `EffectivePolicyResponse` добавляются **два аддитивных** поля с идентичными именами и семантикой в обоих ответах:

| Поле | Тип | Семантика |
|---|---|---|
| `debt` | int, `≥ 0` | Непогашенная несписанная дельта агентного прогона в кредитах ([ADR-051](ADR-051-agent-debt-reconciliation.md)). `0` при отсутствии долга или выключенном `AGENT_DEBT_RECONCILE_ENABLED`. Семантика **едина** с `AdminWalletResponse.debt` ([ADR-061](ADR-061-admin-wallet-debit.md)). |
| `netBalance` | int (может быть `< 0`) | Эффективный баланс с учётом долга: `netBalance = balance − debt` (в policy: `creditsBalance − debt`). При наличии долга — отрицательное число; клиент показывает его как отрицательный баланс. |

Существующие `balance` / `creditsBalance` **не меняются** (`≥ 0`, значение `wallets.balance`).

### Почему оба поля, а не только `netBalance`

- `netBalance` даёт прямое число для показа «отрицательного баланса» — минимум логики на клиенте.
- `debt` даёт **прозрачность**: клиент может отдельно показать «долг N кредитов», объяснить блокировку агентного прогона (`debt_outstanding`) и предупредить, что часть следующего пополнения уйдёт на погашение (clawback). Без `debt` клиент вынужден реконструировать долг как `max(0, balance − netBalance)` — хрупко и неочевидно.
- `debt` уже канонизирован в admin-контуре ([ADR-061](ADR-061-admin-wallet-debit.md)); единое имя/семантика между admin и клиентом исключает расхождение трактовок.

Оба поля — единственная нужная надстройка; ни `balance`, ни `creditsBalance` не переопределяются.

### Единый источник и инварианты

1. **Единый источник долга — колонка `wallets.debt`.** В wallet-пути значение приходит из `WalletService.get_wallet_view` (уже возвращает `debt` третьим элементом кортежа). В policy-пути читается **та же** колонка `wallets.debt` (loader). Оба доступа (`WalletService.current_debt` / прямой read колонки) возвращают одно значение — консистентность `wallet ↔ policy` для одного пользователя гарантирована by construction (один столбец, один транзакционный снапшот запроса).
2. **Инвариант «флаг off → долг 0».** `wallets.debt` инкрементируется исключительно на агентном пути под `AGENT_DEBT_RECONCILE_ENABLED` ([ADR-051 §2](ADR-051-agent-debt-reconciliation.md), эмиссионный gate в `AgentProxyService`). При выключенном флаге колонка никогда не растёт и остаётся `0` → `debt = 0` и `netBalance == balance` (`== creditsBalance`). В read-путях **проверка флага не нужна** — инвариант держится на стороне записи. Задокументировано; отдельной ветки в коде чтения не вводится.
3. **Аддитивность.** Поля добавляются в response-модели; существующие клиенты, не знающие о них, игнорируют новые ключи. Никакой смены типа/семантики `balance`/`creditsBalance`.
4. **Именование.** Одинаковые имена `debt` / `netBalance` в обоих ответах: клиент читает один и тот же ключ независимо от эндпоинта. Хотя базовое поле называется `balance` (wallet) и `creditsBalance` (policy), оба равны `wallets.balance`, поэтому `netBalance` численно совпадает в обоих ответах для одного пользователя.

## Consequences

- Клиент показывает отрицательный баланс из `netBalance` и явный долг из `debt` в обоих экранах (кошелёк и предварительная проверка прав).
- Обратная совместимость полная: `balance`/`creditsBalance` неизменны, изменения только аддитивные.
- Policy-путь выполняет один дополнительный PK-lookup `wallets.debt` (индексированный, тот же запрос-снапшот) — стоимость пренебрежима; `load_policy_state` и `PolicyState` (чистый вход engine) **не меняются** — долг не добавляется в `PolicyState` (engine.evaluate() долгом не оперирует, см. [ADR-051 §4](ADR-051-agent-debt-reconciliation.md)).
- Единая семантика `debt` в admin/client/policy — нет риска расхождения трактовок.

## Alternatives

- **Только `netBalance`.** Меньше полей, но теряется прозрачность долга и его причинно-следственной связи с блокировкой/clawback; клиент реконструирует долг эвристикой. Отвергнуто.
- **Сделать `balance`/`creditsBalance` знаковыми (`< 0` при долге).** Нарушает обратную совместимость и инвариант `CHECK (balance >= 0)`; ломает все текущие клиенты. Отвергнуто продуктовым ограничением.
- **Отдельный эндпоинт `GET /v1/wallet/debt`.** Лишний round-trip для UI, который и так дёргает wallet/policy; дублирует контур. Отвергнуто.
- **Добавить `debt` в `PolicyState` и вернуть через engine.** Загрязняет чистую функцию policy семантикой, которой она не оперирует ([ADR-051 §4](ADR-051-agent-debt-reconciliation.md)). Отвергнуто — долг читается в `effective()` рядом с engine, не внутри него.

## needs_code_sync

- `src/app/schemas/wallet.py` — `WalletResponse`: поля `debt: int`, `netBalance: int`.
- `src/app/schemas/policy.py` — `EffectivePolicyResponse`: поля `debt: int`, `netBalance: int`.
- `src/app/api_gateway/routers/wallet.py` (`get_wallet`) — прокинуть уже получаемый `debt` (сейчас `_debt`): `debt=debt`, `netBalance=balance - debt`.
- `src/app/policy/loader.py` — `EffectivePolicy` (+ поле `debt`), `effective()` читает `wallets.debt` (та же колонка, что `WalletService.current_debt`) и заполняет `debt`.
- `src/app/api_gateway/routers/policy.py` (`policy_effective`) — `debt=result.debt`, `netBalance=result.credits_balance - result.debt`.
