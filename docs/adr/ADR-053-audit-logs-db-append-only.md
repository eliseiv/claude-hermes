# ADR-053 — Durable append-only `audit_logs` (REVOKE UPDATE/DELETE + write-only роль БД)

- Статус: Accepted
- Дата: 2026-06-24
- Связан с: [ADR-005](ADR-005-idempotency-ledger.md), [ADR-009](ADR-009-admin-token-auth.md), [ADR-029](ADR-029-adapty-subscription-webhook.md), [03-data-model.md §9](../03-data-model.md), [05-security.md](../05-security.md), [07-deployment.md](../07-deployment.md), [modules/audit/](../modules/audit/README.md)
- Контракт данных: REVOKE + (опц.) триггер на `audit_logs` (миграция `0016`); роль БД приложения (devops)
- Закрывает: [TD-001](../100-known-tech-debt.md)
- **Implementation note (2026-06-24, devops):** инфра-часть реализована. Роли `app_rw` (рантайм, least-privilege) и `app_migrate` (миграции, полные права + `CREATE ON DATABASE` для расширений `pgcrypto`) провижинятся **до** миграции `0016` двумя путями: init-скрипт [`docker/postgres/init/01-roles.sh`](../../docker/postgres/init/01-roles.sh) (свежий том — локалка/e2e/новый prod-инстанс; идемпотентные guarded `CREATE ROLE`, пароли из `APP_RW_PASSWORD`/`APP_MIGRATE_PASSWORD`) и ручная `CREATE ROLE`-процедура (существующий prod-том). Раздельные DSN: `DATABASE_URL` (`app_rw`, рантайм) и `DATABASE_URL_MIGRATE` (`app_migrate`, alembic). `migrations/env.py` разрешает URL по приоритету Alembic `context.config` > `DATABASE_URL_MIGRATE` > `DATABASE_URL` (fallback), сохраняя приоритет Alembic Config для e2e/testcontainers ([TD-008](../100-known-tech-debt.md)). Тело Decision/Consequences не переписано. Детали — [07-deployment.md §Роли БД / §Миграции](../07-deployment.md#роли-бд--durable-append-only-audit_logs-adr-053-prod-harden-td-001).

## Context

`audit_logs` ([03-data-model.md §9](../03-data-model.md)) — журнал аудита (billing_debit, policy_decision, tool_mutation, byok_change, admin_grant, admin_subscription_grant, adapty_subscription, billing_debit_insufficient, и др.). Append-only обеспечивается **только на уровне приложения** (код не делает UPDATE/DELETE). [TD-001](../100-known-tech-debt.md): прямой доступ к БД или ошибка кода теоретически позволяют изменить/удалить запись аудита. Для prod-harden нужна **durable** защита на уровне БД, не зависящая от дисциплины приложения.

## Decision

**Комбинация: REVOKE UPDATE/DELETE на роли приложения + BEFORE-триггер запрета UPDATE/DELETE** (defense-in-depth). REVOKE отсекает обычный путь приложения; триггер ловит остаточные случаи (например, если роль приложения когда-либо получит избыточные привилегии или REVOKE будет пропущен на новой среде).

### 1. Роль БД приложения — least-privilege (devops + миграция)

- Приложение (`api`-контейнер) подключается под ролью `app_rw` (НЕ суперюзер, НЕ владелец схемы). На `audit_logs` у `app_rw`: `GRANT INSERT, SELECT` — **БЕЗ** `UPDATE`, `DELETE`, `TRUNCATE`.
- Миграция (под ролью-владельцем/миграционной ролью `app_migrate`, отдельной от runtime-роли) выполняет:
  ```sql
  REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM app_rw;
  GRANT  INSERT, SELECT          ON audit_logs TO   app_rw;
  ```
- Миграционная роль (`app_migrate`) сохраняет полные права (нужны для DDL/откатов); runtime-роль (`app_rw`) — ограничена. Разведение ролей — devops ([07-deployment.md §Роли БД](../07-deployment.md)).
- **Это требует, чтобы runtime и миграции ходили под РАЗНЫМИ ролями.** Если сейчас обе операции идут под одной ролью — devops вводит вторую роль (DSN runtime ≠ DSN миграций). Без разведения REVOKE на единственной роли заблокирует и легитимные миграционные правки схемы `audit_logs`.

### 2. BEFORE-триггер запрета UPDATE/DELETE (миграция `0016`)

Дополнительно к REVOKE — триггер на уровне таблицы (срабатывает для **любой** роли, включая владельца при случайной операции, кроме явного отключения триггера):

```sql
CREATE OR REPLACE FUNCTION audit_logs_no_mutate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only (ADR-053): % is forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_logs_no_update
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_no_mutate();

CREATE TRIGGER trg_audit_logs_no_delete
    BEFORE DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_no_mutate();
```

- INSERT/SELECT не затрагиваются. Триггер — defense-in-depth поверх REVOKE: даже под привилегированной ролью случайный UPDATE/DELETE падает.
- Намеренная административная коррекция (например, GDPR-erasure) выполняется явным `ALTER TABLE audit_logs DISABLE TRIGGER ...` под привилегированной ролью в отдельной операционной процедуре — осознанный, аудируемый out-of-band шаг (НЕ путь приложения). Политику erasure здесь не вводим (см. Q-053-1).

### 3. Применимость к смежным append-only таблицам

- На этом шаге защита распространяется **только** на `audit_logs` (профиль prod-harden). `ledger_transactions` также append-only по дизайну ([ADR-005](ADR-005-idempotency-ledger.md)), но имеет операционные отличия (нет UPDATE/DELETE в коде; сверка баланса). Распространение того же паттерна на `ledger_transactions` — следующий шаг (Q-053-1), не в scope этого ADR (минимизация изменений; ledger уже защищён CHECK-инвариантами и idempotency).

### 4. Миграция down (откат)

- `down` миграции `0016`: `DROP TRIGGER`/`DROP FUNCTION` + восстановить `GRANT UPDATE, DELETE` (или вернуть прежний grant-профиль). Откат не «теряет» данные (только снимает защиту).

## Consequences

**Положительные:**
- Аудит durable-неизменяем на уровне БД: ошибка кода или избыточная привилегия не модифицируют/не удаляют записи.
- Defense-in-depth: REVOKE (обычный путь) + триггер (остаточные пути).
- Least-privilege runtime-роль снижает поверхность при компрометации `api`.
- Соответствует требованиям комплаенса (триггер закрытия [TD-001](../100-known-tech-debt.md)).

**Отрицательные / ограничения:**
- Требует разведения runtime-роли (`app_rw`) и миграционной роли (`app_migrate`) — операционное изменение deploy (devops): два DSN/набора creds. Без него REVOKE заблокирует миграции.
- Намеренная коррекция аудита (erasure) усложняется (out-of-band под привилегированной ролью с отключением триггера) — приемлемо: редкая, аудируемая, не-приложенческая операция.
- Партиционирование/retention `audit_logs` в будущем потребует `TRUNCATE`/`DROP PARTITION` под привилегированной ролью (не `app_rw`) — учесть при вводе retention.

## Alternatives

1. **Только REVOKE (без триггера).** Отвергнуто как недостаточное для prod-harden: REVOKE снимается ошибочным GRANT, и не защищает от операций под владельцем/суперюзером. Триггер — дешёвая страховка.
2. **Только триггер (без REVOKE).** Отвергнуто: триггер можно отключить (`DISABLE TRIGGER`), и least-privilege роль всё равно желательна по принципу минимума привилегий. Комбинация строже.
3. **Append-only через внешний WORM-storage / hash-chain (tamper-evidence).** Отвергнуто на старте: значительная инфраструктурная сложность; REVOKE+триггер закрывает практическую угрозу [TD-001](../100-known-tech-debt.md). Hash-chaining/внешний WORM — будущее усиление при ужесточении комплаенса (Q-053-1).
4. **Оставить app-level append-only (статус-кво).** Отвергнуто — это и есть [TD-001](../100-known-tech-debt.md); prod-harden требует durable-гарантии.
