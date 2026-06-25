# ADR-047 — Биллинг агентного пути по реальному usage (токены), идемпотентность по `runId`

- Статус: Accepted
- Дата: 2026-06-23
- Связан с: [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (**расширяет / частично-суперсидит для `/v1/agent/*`**), [ADR-005](ADR-005-idempotency-ledger.md) (idempotency-ledger), [ADR-002](ADR-002-access-policy-state-machine.md) (policy state machine), [ADR-004](ADR-004-blocked-http-200.md) (blocked HTTP 200), [ADR-045](ADR-045-hermes-as-agent-proxy.md) (agent-proxy), [03-data-model.md](../03-data-model.md), [modules/wallet-ledger/](../modules/wallet-ledger/README.md), [modules/agent-proxy/](../modules/agent-proxy/README.md)

## Context

[ADR-006](ADR-006-credit-billing-and-subscription-grant.md) зафиксировал модель «1 кредит = 1 сообщение», идемпотентность debit по `messageStepId`. Эта модель привязана к per-turn-семантике `/v1/chat/*` (один завершённый assistant_message = 1 debit).

Агентный путь ([ADR-045](ADR-045-hermes-as-agent-proxy.md)) принципиально другой: один `POST /v1/agent/run` запускает автономный прогон Hermes с собственным tool-loop переменной длины. «1 сообщение = 1 кредит» здесь не отражает реальную стоимость (прогон может потребить кардинально разный объём токенов). Hermes отдаёт реальный `usage:{input_tokens,output_tokens,total_tokens}` в событии `run.completed`. Решение пользователя — **биллинг по реальному usage** для агентного пути.

Это **изменение модели биллинга для агентного пути**. Нужно явно зафиксировать отношение к [ADR-006](ADR-006-credit-billing-and-subscription-grant.md), формулу конвертации, идемпотентность, policy-gate, и обработать неоднозначности тарификации.

## Decision

### 1. Сосуществование двух биллинг-моделей (расширение / частичный supersede [ADR-006](ADR-006-credit-billing-and-subscription-grant.md))

- **`/v1/chat/*`** — **без изменений**: «1 кредит = 1 сообщение», idempotency по `messageStepId` ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md) остаётся Accepted и действующим для этого контура).
- **`/v1/agent/*`** — **новая модель**: списание по реальному `usage` (токены), idempotency по `runId`. Для агентного пути ADR-006 **частично-суперсидится** (формула стоимости заменяется); механика идемпотентности/атомарности ([ADR-005](ADR-005-idempotency-ledger.md)) и начисление кредитов при подписке ([ADR-006 §2](ADR-006-credit-billing-and-subscription-grant.md)) — **сохраняются** общими для обоих путей.

### 2. Формула конвертации токенов в кредиты

- Конфигурируемые коэффициенты (env, [02-tech-stack.md](../02-tech-stack.md) / [07-deployment.md](../07-deployment.md)):
  - `CREDITS_PER_1K_INPUT` — кредитов за 1000 input-токенов.
  - `CREDITS_PER_1K_OUTPUT` — кредитов за 1000 output-токенов.
- Списание за прогон:
  ```
  amount = ceil( input_tokens  / 1000 * CREDITS_PER_1K_INPUT
               + output_tokens / 1000 * CREDITS_PER_1K_OUTPUT )
  ```
- **Кредиты — целые** (инвариант [03-data-model.md](../03-data-model.md): деньги/кредиты целочисленные, без float). Округление дробного результата — **вверх (`ceil`)** до целого кредита, минимум `1` при ненулевом usage (прогон с реальным потреблением не может стоить 0). `usage` (`input_tokens`/`output_tokens`/`total_tokens`/`model`) сохраняется в `ledger_transactions.meta` для аудита.
- Точные дефолты коэффициентов и политика округления зафиксированы дефолтом, остаточная неоднозначность тарификации — [Q-047-1](../99-open-questions.md).

### 3. Policy-gate перед прогоном ([ADR-002](ADR-002-access-policy-state-machine.md), [ADR-004](ADR-004-blocked-http-200.md))

- Перед запуском прогона (`POST /v1/agent/run`, [ADR-045 §2](ADR-045-hermes-as-agent-proxy.md)) вызывается `PolicyEngine.evaluate` (BR-2/BR-3/BR-5): **нет активной подписки** или **0 кредитов** → `blocked` **до** прогона (контейнер не будится напрасно).
- Blocked — **бизнес-ответ HTTP 200** ([ADR-004](ADR-004-blocked-http-200.md)): `200 {status:"blocked", blockReason}`. НЕ 4xx. Достижимый набор `blockReason` определяется Policy Engine ([ADR-002](ADR-002-access-policy-state-machine.md)) и зафиксирован в [agent-proxy/02-api-contracts.md §Достижимый набор](../modules/agent-proxy/02-api-contracts.md): `credits_empty | subscription_expired | trial_used`. `subscription_required` на этом пути **недостижим** (его возвращает только byok-ветка; агентный путь работает строго в `credits`-режиме).
- `mode=byok` агентного пути на старте не вводится (агент использует СВОЙ `LLM_PROVIDER`/ключ внутри инстанса, [ADR-046](ADR-046-per-user-hermes-runtime.md)); policy для `/v1/agent/*` работает в `credits`-ветке. BYOK для агента — [Q-047-3](../99-open-questions.md).
- Policy state machine ([ADR-002](ADR-002-access-policy-state-machine.md)) **не меняется** — переиспользуется как есть.

### 4. Списание после прогона — идемпотентность по `runId`

- На событии `run.completed` ([ADR-045 §3](ADR-045-hermes-as-agent-proxy.md)) control plane вызывает:
  ```
  WalletService.consume(user_id, amount, idempotency_key=runId, meta={usage, model, source:"agent_run"})
  ```
- **Идемпотентность по `runId`** (вместо `messageStepId`): повторная подписка на `/events` того же `runId`, ретрай, дублированный `run.completed` → **один** debit (unique index `ux_ledger_idempotency (user_id, idempotency_key)`, [ADR-005](ADR-005-idempotency-ledger.md)). `runId` — стабильный идентификатор прогона Hermes.
- `run.failed` → `usage` отсутствует → **debit не выполняется** (неуспешный прогон не тарифицируется).
- Источник credit-tx и идемпотентность-ключи (инвариант анти-double-grant, [03-data-model.md](../03-data-model.md) §Источники credit-tx) расширяется: для агентного debit `idempotency_key=runId` (`meta.source=agent_run`) — отдельное пространство ключей, не пересекается с `messageStepId` (chat-debit) и grant-ключами.
- Возможный «pre-debit hold» (резерв кредита до прогона) — **не вводится** на старте: policy-gate отсекает нулевой баланс, фактический debit — по факту usage после `run.completed`. Риск «прогон при близком к нулю балансе уводит баланс в минус» исключён CHECK `balance >= 0` ([03-data-model.md](../03-data-model.md)) — debit, превышающий баланс, **не списывается** (savepoint-откат INSERT+UPDATE, без orphan-строки) и фиксируется как audit-запись несписанной дельты `billing_debit_insufficient` (§6); реконсиляция долга — [Q-047-2](../99-open-questions.md) / [TD-029](../100-known-tech-debt.md).

### 5. Audit

- Переиспользуется `AuditService`: событие прогона и списания (`agent_run` / `billing_debit`), `meta` с `runId`/`usage`/`model`, без секретов (`API_SERVER_KEY`, user-content — под redaction).

### 6. Поведение при `InsufficientCredits` на агентном `run.completed` (data-integrity)

**Контекст дефекта.** `WalletService.consume` ([ADR-005](ADR-005-idempotency-ledger.md)) выполнен как `INSERT ... RETURNING` debit-строки → условный `UPDATE wallets ... WHERE balance >= amount`. При недостатке баланса `UPDATE` затрагивает 0 строк и поднимается `InsufficientCreditsError`. Корректность опиралась на то, что исключение долетит до внешнего `session_scope` и тот сделает ROLLBACK (так работает chat-путь: `/wallet/consume` отдаёт `409`, транзакция HTTP-запроса откатывается целиком).

На **агентном** пути это нарушается: SSE-ретранслятор (`_bill_completed`) обязан НЕ рвать стрим (run уже завершён upstream, [ADR-045 §3](ADR-045-hermes-as-agent-proxy.md), [Q-047-2](../99-open-questions.md)), поэтому он **глотает** `InsufficientCreditsError` и продолжает отдачу. Внешний `session_scope` SSE-генератора делает **commit** (исключение не дошло до него) → INSERT debit-строки фиксируется БЕЗ уменьшения баланса (`UPDATE` с 0 строк не отравляет сессию). Итог — **orphan debit-строка** `type='debit'`, `amount=N` в `ledger_transactions`, не отражённая в `balance`. Это нарушает инвариант сверки **balance == Σ(credit) − Σ(debit)** ([03-data-model.md](../03-data-model.md)), попадает в `GET /v1/wallet` (фантомное списание у пользователя) и в usage-аналитику.

**Решение (комбинация (b) + явный non-financial audit):**

1. **`consume` становится самодостаточно-атомарным (вариант (b) reviewer'а).** Блок `INSERT debit + условный UPDATE` оборачивается во вложённую транзакцию (SAVEPOINT, SQLAlchemy `session.begin_nested()`). При недостатке баланса (UPDATE = 0 строк) **откатывается savepoint** — INSERT debit-строки отменяется, баланс не меняется — и затем поднимается `InsufficientCreditsError`. Корректность ledger **больше НЕ зависит** от внешнего ROLLBACK: даже если вызывающий проглотит исключение и внешняя транзакция закоммитится, orphan-строки не возникает. Это распространяется на оба пути (chat и agent) — chat-семантика (`409`, повтор не списывает) сохраняется.

2. **Usage не теряется молча — фиксируется в audit (НЕ в ledger).** На агентном пути при `InsufficientCreditsError` ретранслятор вместо «только лог» пишет **audit-событие** `billing_debit_insufficient` (`AuditService`, append-only `audit_logs`, [03-data-model.md](../03-data-model.md)) с `runId`/`usage`/`model`/требуемым `amount`/текущим балансом. Это **аудит-запись, а не финансовая ledger-строка**: финансовый ledger остаётся чистым и сверяемым, реальное потребление зафиксировано для последующей реконсиляции, SSE не рвётся. Списание при этом **не выполнено** (несписанная дельта). **Redaction-инвариант ([ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)):** token-каунты usage (`input_tokens`/`output_tokens`/`total_tokens`) в этом payload (и в `ledger_transactions.meta.usage`/audit `billing_debit`/`agent_run`) — **НЕ секрет** и проходят redaction-allowlist, поэтому сохраняются как есть; денилист `*token*` по-прежнему редактирует реальные токен-секреты. Без этого allowlist usage был бы заменён на `***REDACTED***` и реконсиляция стала бы невозможна.

3. **Вариант (c) (orphan-строка как намеренный audit-флаг в ledger) — отвергнут.** `ledger_tx_type` = {`debit`,`credit`}, `amount > 0`; debit-строка, не уменьшающая баланс, ломает сверку balance↔ledger и засоряет `GET /v1/wallet`. Чтобы (c) был корректным, потребовался бы новый tx-тип + исключение из расчёта баланса + из user-facing wallet-view — лишняя схемно-контрактная сложность ради того, что append-only `audit_logs` уже даёт без загрязнения денег.

4. **Реконсиляция/clawback несписанной дельты** (как именно «добрать» долг: отрицательный баланс / hold / последующее списание / блок следующего прогона) — **не вводится на старте**, расширяет [Q-047-2](../99-open-questions.md) и заведён долг [TD-029](../100-known-tech-debt.md). На старте: audit-запись + `balance >= 0` CHECK + policy-gate (отсекает нулевой баланс ДО прогона) — достаточны; единственный остаточный кейс — прогон, стартовавший при положительном, но недостаточном для итогового usage балансе (нет pre-debit hold).

## Consequences

**Положительные:**
- Стоимость агентного прогона отражает реальное потребление токенов (справедливее для дорогих автономных прогонов).
- `/v1/chat/*` биллинг не затронут ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md) для него в силе).
- Идемпотентность/атомарность переиспользуют готовый ledger ([ADR-005](ADR-005-idempotency-ledger.md)); policy state machine ([ADR-002](ADR-002-access-policy-state-machine.md)) не меняется.
- Анти-double-debit by construction (`runId` + unique index).

**Отрицательные / ограничения:**
- Стоимость для пользователя теперь **непредсказуема до прогона** (зависит от usage) — обратная цена «1 кредит = 1 сообщение». Принято осознанно для агентного пути.
- Биллинг постфактум (на `run.completed`): при обрыве SSL до события списание не произойдёт на этом соединении — митигация idempotency по `runId` + реконсиляция ([Q-047-2](../99-open-questions.md)).
- Возможен прогон при близком к нулю балансе с фактическим usage больше остатка (нет pre-debit hold) — несписанная дельта фиксируется в audit (`billing_debit_insufficient`, §6), не теряется молча; реконсиляция долга отложена ([Q-047-2](../99-open-questions.md), [TD-029](../100-known-tech-debt.md)).
- `consume` теперь самодостаточно-атомарен (SAVEPOINT, §6): корректность ledger не зависит от внешнего ROLLBACK вызывающего; устранён класс дефектов «orphan debit-строки при проглатывании `InsufficientCreditsError`».
- Две биллинг-модели в одной кодовой базе — выше когнитивная сложность; разведены по контурам (`/chat` vs `/agent`).

## Alternatives

1. **Распространить «1 кредит = 1 сообщение» ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md)) на агентный путь.** Отвергнуто: один agent-run = автономный прогон переменной длины; фикс-цена не отражает стоимость, легко злоупотребить (один run = огромный tool-loop за 1 кредит).
2. **Фиксированная цена за run (N кредитов).** Отвергнуто: всё ещё не отражает реальное потребление; usage-based точнее при доступном `usage` из `run.completed`.
3. **Pre-debit hold (резерв кредита до прогона) + reconcile.** Отложено ([Q-047-2](../99-open-questions.md)): усложняет ledger (hold/capture/release); на старте policy-gate + post-debit достаточно, отрицательный баланс отсечён CHECK.
4. **Идемпотентность по `messageStepId` (как chat).** Неприменимо: агентный прогон не имеет per-message-step семантики; `runId` — естественный стабильный ключ прогона.
