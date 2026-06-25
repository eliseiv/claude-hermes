# ADR-051 — Реконсиляция несписанной дельты агентного прогона (debt clawback + policy-block)

- Статус: Accepted
- Дата: 2026-06-24
- Связан с: [ADR-047](ADR-047-usage-based-billing-for-agent.md) (**расширяет §4/§6**, закрывает [Q-047-2](../99-open-questions.md) в части реконсиляции), [ADR-005](ADR-005-idempotency-ledger.md) (idempotency-ledger), [ADR-002](ADR-002-access-policy-state-machine.md) (policy state machine), [ADR-004](ADR-004-blocked-http-200.md) (blocked HTTP 200), [ADR-006 §2](ADR-006-credit-billing-and-subscription-grant.md) (grant при подписке), [ADR-045](ADR-045-hermes-as-agent-proxy.md) (agent-proxy), [03-data-model.md](../03-data-model.md), [modules/wallet-ledger/](../modules/wallet-ledger/README.md), [modules/agent-proxy/](../modules/agent-proxy/README.md)
- Контракт данных: новая колонка `wallets.debt` (миграция `0014`)
- Закрывает: [TD-029](../100-known-tech-debt.md)

## Context

[ADR-047 §6](ADR-047-usage-based-billing-for-agent.md) зафиксировал поведение при `InsufficientCredits` на агентном `run.completed`: `consume` самодостаточно-атомарен (SAVEPOINT-откат INSERT+UPDATE — нет orphan-debit-строки), а несписанная дельта фиксируется audit-событием `billing_debit_insufficient` (НЕ ledger-строкой). **Списание при этом не выполняется** — реальное потребление токенов зафиксировано только в audit, кредиты за прогон не получены.

Остаточный кейс: прогон стартует при положительном, но недостаточном для итогового usage балансе (pre-debit hold не вводился). Дельта не теряется (audit), но **не реконсилируется**: нет механизма добора долга при следующем пополнении, нет блокировки следующего прогона при накопленном долге. [Q-047-2](../99-open-questions.md) и [TD-029](../100-known-tech-debt.md) оставили это открытым.

Варианты из ТЗ:
- **(a) pre-debit hold/резерв** — резерв кредита до прогона + capture/release по usage.
- **(b) clawback** несписанной дельты из audit при следующем `grant`/пополнении.
- **(c) policy-блок** следующего прогона при наличии незакрытого долга.

## Decision

Выбран **гибрид (c) + (b)** (отвергнут (a) hold — см. Alternatives). Дешевле hold: не требует трёхфазного ledger (hold/capture/release) и переменной семантики «отрицательного баланса». Долг учитывается одним скаляром на кошелёк.

### 1. Учёт долга — колонка `wallets.debt` (миграция `0014`)

- Новая колонка `wallets.debt BIGINT NOT NULL DEFAULT 0`, `CHECK (debt >= 0)` (целые кредиты, инвариант [03-data-model.md](../03-data-model.md): без float). Семантика — **накопленная несписанная дельта в кредитах** (сколько пользователь недоплатил за прошлые агентные прогоны).
- Долг — **не** `ledger_transactions`-строка (ledger остаётся чистым, сверка `balance == Σ(credit) − Σ(debit)` не нарушается — инвариант [ADR-047 §6](ADR-047-usage-based-billing-for-agent.md), вариант (c) «orphan как ledger-флаг» по-прежнему отвергнут). `wallets.debt` — отдельный агрегат, как `wallets.balance`.

### 2. Запись долга при `InsufficientCredits` (расширение [ADR-047 §6](ADR-047-usage-based-billing-for-agent.md))

На агентном `run.completed`, когда `consume` поднимает `InsufficientCreditsError` (savepoint-откат debit, баланс не тронут):
- В **той же** вложенной транзакции, что и audit `billing_debit_insufficient`, выполнить `UPDATE wallets SET debt = debt + (amount - balance), updated_at = now() WHERE user_id = :u`, где `amount` — требуемое списание, `balance` — текущий остаток. **Списывается частично-возможное:** сначала `consume` списывает доступный остаток (см. §2.1), недобор идёт в `debt`.
- **2.1 Частичное списание остатка (уточнение `consume`).** При `amount > balance` на агентном пути `consume` списывает **весь доступный `balance`** (`debit amount=balance`, обычная ledger-строка, идемпотентность по `runId`), а недобор `delta = amount - balance` записывает в `wallets.debt`. Баланс → 0. Это меняет §6 ADR-047 (там был полный savepoint-откал без частичного списания) — теперь дельта расщепляется: «сколько смогли» в ledger, «сколько не смогли» в debt. Идемпотентность по `runId` сохраняется: повтор того же `run.completed` не дублирует ни ledger-debit, ни инкремент `debt` (см. §4).
- audit `billing_debit_insufficient` дополняется полями `partialDebited` (списанная часть = бывший balance) и `debtAdded` (= delta). usage-каунты — под redaction-allowlist ([ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)), не редактируются.

### 3. Clawback при пополнении (вариант (b), расширение `WalletService.grant`)

При любом `credit`-начислении (`WalletService.grant`: подписка/Adapty/admin/token-purchase, [03-data-model.md §Источники credit-tx](../03-data-model.md)) — **до** увеличения `balance` погасить долг из начисляемой суммы:
- `repaid = min(grant_amount, debt)`; `debt -= repaid`; `balance += (grant_amount - repaid)`.
- **Атомарно** в той же транзакции, что INSERT `ledger_transactions(type=credit, amount=grant_amount)` (ledger фиксирует полную сумму начисления; долг гасится из неё на уровне `wallets`). Сверка `balance` остаётся консистентной: `balance` отражает то, что реально доступно после погашения долга; разница (`repaid`) — закрытие прошлой задолженности, не «потеря» кредитов (пользователь её уже потребил токенами).
- Идемпотентность grant ([ADR-005](ADR-005-idempotency-ledger.md)) сохраняется: повторный `grant` с тем же `idempotency_key` — no-op (ни ledger, ни `balance`/`debt` не трогаются). Clawback выполняется **ровно один раз** на фактическое (не реплейное) начисление — внутри той же ветки, что INSERT ledger-строки.
- audit `billing_debt_repaid` (`userId`, `repaid`, `debtRemaining`, `grantLedgerTxId`) — append-only.

### 4. Policy-блок следующего прогона при долге (вариант (c), расширение policy-gate [ADR-002](ADR-002-access-policy-state-machine.md)/[ADR-047 §3](ADR-047-usage-based-billing-for-agent.md))

- Policy-gate `POST /v1/agent/run` ([ADR-045 §2](ADR-045-hermes-as-agent-proxy.md)) перед прогоном дополнительно проверяет `wallets.debt`. При `debt > 0` → **`blocked` до прогона** (контейнер не будится), HTTP 200 ([ADR-004](ADR-004-blocked-http-200.md)) с новым `blockReason = "debt_outstanding"`.
- `debt_outstanding` добавляется в enum `blockReason` и в достижимый набор `/v1/agent/run` ([agent-proxy/02-api-contracts.md](../modules/agent-proxy/02-api-contracts.md)): `credits_empty | subscription_expired | trial_used | debt_outstanding`. На `/v1/chat/*` **недостижим** (debt — только агентный путь).
- Долг гасится §3 (пополнение) → после погашения `debt=0` → следующий прогон проходит policy. Это замыкает контур: пользователь с непогашенным долгом не запускает новый дорогой прогон, пока не пополнит баланс (что автоматически спишет долг).
- **Идемпотентность §2.1 vs §4:** §4 не даёт стартовать новому прогону при `debt>0`, но прогон, уже стартовавший при `balance>0` и ушедший в недобор, фиксирует долг через §2.1 на своём `run.completed` (idempotent по `runId`). Гонки нет: §4 проверяется до `ensure_running`, §2.1 — на завершении конкретного `runId`.

### 5. Конфигурируемость

- Поведение реконсиляции включается флагом `AGENT_DEBT_RECONCILE_ENABLED` (env, дефолт `true`). При `false` — поведение [ADR-047 §6](ADR-047-usage-based-billing-for-agent.md) как есть (только audit `billing_debit_insufficient`, без `debt`-учёта и policy-блока) — fallback для отладки/постепенного включения. `debt`-колонка создаётся миграцией независимо от флага.

## Consequences

**Положительные:**
- Несписанная дельта реконсилируется без потери (clawback при пополнении) и без накопления злоупотреблений (policy-блок).
- Ledger остаётся чистым и сверяемым (долг — отдельный агрегат `wallets.debt`, не ledger-строка) — инвариант [ADR-047 §6](ADR-047-usage-based-billing-for-agent.md) сохранён.
- Дешевле hold: один скаляр на кошелёк, без трёхфазного capture/release.
- Идемпотентность по `runId`/`idempotency_key` сохраняется по построению.

**Отрицательные / ограничения:**
- Пользователь с долгом не может запустить новый агентный прогон до пополнения — продуктовое решение (анти-абуз «дорогой прогон при около-нулевом балансе»). Принято осознанно.
- Частичное списание остатка (§2.1) усложняет `consume` (расщепление debit/debt) против чисто-savepoint-отката ADR-047 §6 — оправдано тем, что пользователь платит за то, что смог.
- `wallets.debt` — новый агрегат, требует учёта в admin-wallet-view (отображать долг оператору) — аддитивно к [admin/02-api-contracts.md](../modules/admin/02-api-contracts.md) `GET /v1/admin/wallet/{userId}`.

## Alternatives

1. **(a) pre-debit hold/резерв + capture/release.** Отвергнуто: вводит трёхфазную семантику ledger (hold-строка → capture/release), переменный «зарезервированный» баланс, усложняет `GET /v1/wallet` и сверку. Hold оправдан при предоплатной модели; здесь usage известен только постфактум (`run.completed`), поэтому hold пришлось бы делать «на максимум», что искажает доступный баланс сильнее, чем post-factum debt.
2. **Только (b) clawback без (c) policy-блока.** Отвергнуто: без блока пользователь мог бы запускать прогон за прогоном при нулевом балансе, накапливая долг без обязательства пополнять (долг гасится только при добровольном пополнении) — анти-абуз слабее.
3. **Только (c) policy-блок без (b) clawback.** Отвергнуто: блок без механизма погашения = «вечный блок» (долг никогда не списывается, пользователь застревает). (b) даёт путь выхода: пополнил → долг погашен → разблокирован.
4. **Отрицательный `balance` (снять CHECK `balance >= 0`).** Отвергнуто: ломает инвариант [03-data-model.md](../03-data-model.md) (`balance >= 0`), смешивает «доступные кредиты» и «долг» в одном поле, усложняет все чтения баланса.
