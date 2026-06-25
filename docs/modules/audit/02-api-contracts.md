# Audit — API Contracts

Нет публичного HTTP API на старте (admin/просмотр аудита — out of scope bootstrap).

## Внутренний контракт
```
record(event: AuditEvent) -> None
AuditEvent = {
  userId: uuid,
  sessionId: uuid | None,
  eventType: str,          # tool_mutation | billing_debit | billing_credit |
                           # billing_debit_insufficient | billing_debt_repaid |
                           # policy_decision | byok_change | subscription_change |
                           # adapty_subscription | chat_step |
                           # tool_call_initiated | tool_call_completed |
                           # admin_grant | admin_subscription_grant
  payload: dict            # без секретов
}
```
- Запись синхронная в рамках той же бизнес-транзакции, где это уместно (например, billing_debit — в транзакции списания), иначе сразу после.
- `payload` проходит redaction-проверку: запрещены ключи `*key*`, `*token*`, `*secret*`, raw StoreKit/BYOK. **Исключения (allowlist, проверяется первым):** usage-каунты `input_tokens`/`output_tokens`/`total_tokens` (+ camelCase) — [ADR-049](../../adr/ADR-049-redaction-usage-token-counts-allowlist.md); `idempotencyKey` (клиентский дедуп-ключ для трассируемости admin-операций, НЕ секрет) — [ADR-050](../../adr/ADR-050-redaction-idempotencykey-allowlist.md). Реальные секреты (`api_key`/`*_token`/`*_secret`) редактируются по-прежнему.

## Каталог eventType
| eventType | Источник | Обязателен для AC |
|---|---|---|
| `tool_mutation` | Orchestrator (files.write/mkdir, calendar.create_events, reminders.create; server-side site.write_file/site.delete) | AC-7 |
| `billing_debit` | Wallet | AC-7 |
| `billing_credit` | Wallet | — |
| `billing_debit_insufficient` | Wallet / Agent Proxy (недобор баланса на агентном `run.completed`, [ADR-047 §6](../../adr/ADR-047-usage-based-billing-for-agent.md)). Поля payload: `runId`, `usage` (token-каунты — allowlist, не редактируются, [ADR-049](../../adr/ADR-049-redaction-usage-token-counts-allowlist.md)), `model`, `requiredAmount`, `partialDebited` (списанная часть = бывший balance), `debtAdded` (= недобор в `wallets.debt`, [ADR-051 §2.1](../../adr/ADR-051-agent-debt-reconciliation.md)). | — |
| `billing_debt_repaid` | Wallet (**НОВОЕ**, clawback долга при пополнении, [ADR-051 §3](../../adr/ADR-051-agent-debt-reconciliation.md)). Поля payload: `userId`, `repaid` (погашено из grant), `debtRemaining` (остаток `wallets.debt` после погашения), `grantLedgerTxId` (id credit-tx начисления). Пишется только при `repaid > 0`. | — |
| `policy_decision` | Orchestrator | — |
| `byok_change` | BYOK | — |
| `subscription_change` | Subscription | — |
| `adapty_subscription` | Billing-Adapty (вебхук `POST /v1/billing/adapty/webhook`, пишется **только** на `applied`, [ADR-029 §7](../../adr/ADR-029-adapty-subscription-webhook.md)). Поля payload: `adaptyEventId`, `eventType`, `status`, `plan`, `expiresAt`, `customerId`. Bearer-секрет не пишется. | — |
| `chat_step` | Orchestrator | — |
| `tool_call_initiated` / `tool_call_completed` | Orchestrator | — |
| `admin_grant` | Admin (начисление кредитов оператором; actor=admin, reason, без секрета) | — |
| `admin_subscription_grant` | Admin (ручная выдача подписки оператором, [ADR-048 §2](../../adr/ADR-048-admin-credits-and-subscription-grant.md); код-константа `EVENT_ADMIN_SUBSCRIPTION_GRANT`). Поля payload: `actor=admin`, `userId`, `plan`, `expiresAt`, `reason`, `grantCredits`, `ledgerTxId?` (присутствует только при `grantCredits=true`), `idempotencyKey` (дедуп-ключ для трассируемости — НЕ секрет, исключён из redaction, [ADR-050](../../adr/ADR-050-redaction-idempotencykey-allowlist.md)). Секрет `X-Admin-Token` не пишется. | — |
