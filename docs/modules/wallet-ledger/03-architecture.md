# Wallet / Ledger — Architecture

## consume (самодостаточно-атомарно, ADR-005 / ADR-047 §6)
```sql
-- INSERT+UPDATE обёрнуты в SAVEPOINT (SQLAlchemy session.begin_nested()):
-- consume сам откатывает свою частичную работу при недостатке баланса и НЕ полагается
-- на внешний ROLLBACK вызывающего (ADR-047 §6 — устранение orphan debit-строк на агентном пути).
SAVEPOINT consume;
INSERT INTO ledger_transactions (id, user_id, type, amount, meta, idempotency_key)
VALUES (gen_random_uuid(), :uid, 'debit', :amount, :meta, :idempotency_key)
ON CONFLICT (user_id, idempotency_key) DO NOTHING
RETURNING id;
-- :idempotency_key = значение поля requestId запроса /wallet/consume.
-- Для chat-debit Orchestrator передаёт туда messageStepId (ADR-005/ADR-006), НЕ gateway correlation requestId.
-- Для agent-debit idempotency_key = runId (ADR-047 §4).
-- 0 строк (конфликт) -> идемпотентный повтор: RELEASE SAVEPOINT; SELECT существующую tx + текущий balance, вернуть их
-- иначе:
UPDATE wallets SET balance = balance - :amount, updated_at = now()
WHERE user_id = :uid AND balance >= :amount;
-- 0 строк -> ROLLBACK TO SAVEPOINT consume (INSERT debit-строки отменён, баланс не тронут)
--           -> raise InsufficientCreditsError (insufficient_credits / 409 на /wallet/consume)
-- иначе (списано) -> RELEASE SAVEPOINT consume; audit billing_debit
```
- **Самодостаточная атомарность (ADR-047 §6):** откат частичной работы (INSERT без успешного UPDATE) выполняется откатом savepoint **внутри `consume`**, а не внешним ROLLBACK транзакции HTTP-запроса. Это корректно и тогда, когда вызывающий проглатывает `InsufficientCreditsError` (агентный SSE-путь, который НЕ рвёт стрим) — orphan debit-строки `type='debit'` без уменьшения баланса не возникает; инвариант `balance == Σ(credit) − Σ(debit)` сохраняется.
- При идемпотентном повторе сверяется, что `amount`/`meta` совпадают; иначе `409` (другой payload на тот же ключ).
- **Поведение выше — для chat-debit** (полный откол savepoint → `InsufficientCreditsError` → `409`, повтор не списывает). На **agent-debit** при `AGENT_DEBT_RECONCILE_ENABLED=true` действует частичное списание + долг (ниже).

### consume на agent-пути с реконсиляцией ([ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md), `AGENT_DEBT_RECONCILE_ENABLED`)
При `amount > balance` (недобор) `consume` **не** делает полный откат, а расщепляет дельту:
```sql
-- внутри SAVEPOINT:
-- 1) списать доступный остаток (частичный debit), idempotency по runId:
--    INSERT ledger_transactions(type='debit', amount=balance, idempotency_key=runId, meta);  (если balance>0)
--    UPDATE wallets SET balance = 0, updated_at=now() WHERE user_id=:uid;
-- 2) недобор -> долг:
--    UPDATE wallets SET debt = debt + (:amount - :prev_balance) WHERE user_id=:uid;  -- CHECK debt>=0
-- 3) audit billing_debit_insufficient {runId, usage, model, requiredAmount, partialDebited=:prev_balance, debtAdded}
-- RELEASE SAVEPOINT. SSE не рвётся; InsufficientCreditsError на агентном пути НЕ поднимается (заменён долгом).
```
- Идемпотентность по `runId` сохраняется: частичный debit пишется под `idempotency_key=runId`; повтор `run.completed` → `ON CONFLICT DO NOTHING` (нет дубля debit), и инкремент `debt` выполняется в той же ветке только при фактической вставке (не на реплее).
- `wallets.debt` — отдельный агрегат (НЕ ledger-строка), сверка `balance == Σ(credit)−Σ(debit)` не нарушается.
- При `balance == 0` на старте обработки `run.completed` этот путь недостижим — policy-gate (`debt_outstanding` или `credits_empty`) отсёк бы прогон до старта; частичный debit актуален для прогона, стартовавшего при `balance>0`, но недостаточном для итогового usage.

## grant
`type=credit`, идемпотентность по ключу. **Clawback долга ([ADR-051 §3](../../adr/ADR-051-agent-debt-reconciliation.md), `AGENT_DEBT_RECONCILE_ENABLED`):** при фактическом (не идемпотентно-реплейном) начислении — до увеличения `balance` погасить долг из суммы:
```sql
-- в одной транзакции с INSERT ledger_transactions(type='credit', amount=:grant_amount, idempotency_key=:key):
-- repaid := LEAST(:grant_amount, wallets.debt);
-- UPDATE wallets SET debt = debt - repaid,
--                    balance = balance + (:grant_amount - repaid),
--                    updated_at = now()
-- WHERE user_id = :uid;
-- audit billing_debt_repaid {userId, repaid, debtRemaining, grantLedgerTxId}  (если repaid>0)
```
- Ledger фиксирует **полную** сумму `grant_amount` (начисление не «теряется»); долг гасится на уровне `wallets` (разница `repaid` — закрытие прошлого потребления, не потеря кредитов). Идемпотентный повтор `grant` (тот же ключ) → no-op (ни ledger, ни `balance`/`debt`). Clawback выполняется ровно один раз на фактическое начисление.
- При выключенном флаге — `grant` как прежде (без clawback, `debt` не трогается).

## Конкурентность
- Несколько реплик API: корректность гарантируется БД (unique index + условный UPDATE), без app-level локов.
- Изоляция: `READ COMMITTED` достаточно за счёт условия `balance >= amount` на UPDATE.

## Двойная защита баланса
1. `WHERE balance >= :amount` в UPDATE.
2. DB CHECK `balance >= 0`.

## Auto-provisioning
- Если у пользователя ещё нет `wallets`-строки — создаётся с `balance=0` при первом обращении (idempotent upsert).
