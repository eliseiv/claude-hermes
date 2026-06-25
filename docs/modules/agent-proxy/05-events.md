# Agent Proxy — Events (SSE)

Ретранслируемые события Hermes API-сервера (`GET /v1/runs/{id}/events`, бери как есть). Контракт событий — внешний (Hermes); прокси проксирует as-is, кроме биллинг-обработки `run.completed`.

| Событие | Полезная нагрузка | Обработка прокси |
|---|---|---|
| `run.queued` | `{run_id, status}` | ретрансляция |
| `run.running` | `{run_id}` | ретрансляция |
| `message.delta` | инкрементальный текст ответа | ретрансляция |
| `tool.started` | `{tool, ...}` | ретрансляция |
| `tool.completed` | `{tool, result?}` | ретрансляция |
| `approval.request` | запрос подтверждения опасного действия | ретрансляция; клиент отвечает `POST /v1/agent/runs/{runId}/approval` |
| `run.completed` | `{usage:{input_tokens, output_tokens, total_tokens}, ...}` | **биллинг:** `WalletService.consume(idempotency_key=runId)` ([ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)) + ретрансляция |
| `run.failed` | `{error, ...}` | ретрансляция; **без debit** (нет usage) |

## Биллинг на `run.completed`
- `amount = ceil(input_tokens/1000*CREDITS_PER_1K_INPUT + output_tokens/1000*CREDITS_PER_1K_OUTPUT)`; минимум `1` при ненулевом usage; кредиты целые.
- Идемпотентность по `runId` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)) — повторная подписка/ретрай/дубль события → один debit.
- `usage` сохраняется в `ledger_transactions.meta` (аудит/аналитика), не содержит секретов.
- `audit`-событие `agent_run` + `billing_debit` (без `API_SERVER_KEY`/user-content).
- **Недостаток баланса ([ADR-047 §6](../../adr/ADR-047-usage-based-billing-for-agent.md)):** ретранслятор НЕ рвёт стрим. `consume` сам откатывает savepoint → **debit не записан, баланс не тронут, orphan-строки нет**. Несписанная дельта фиксируется audit-событием **`billing_debit_insufficient`** (`runId`/`usage`/`model`/`amount`/`balance`, без секретов) — usage не теряется молча. Реконсиляция долга — [Q-047-2](../../99-open-questions.md) / [TD-029](../../100-known-tech-debt.md).
- **Usage-каунты НЕ редактируются ([ADR-049](../../adr/ADR-049-redaction-usage-token-counts-allowlist.md)):** `input_tokens`/`output_tokens`/`total_tokens` в payload `billing_debit_insufficient` (и в `agent_run`/`billing_debit`/`ledger.meta.usage`) — целочисленная биллинг-аналитика, НЕ секрет; redaction-allowlist исключает их из `*token*`-денилиста, поэтому usage сохраняется для реконсиляции. Реальные токен-секреты (`API_SERVER_KEY`, `identityToken`, `x-admin-token`, bearer) редактируются как прежде.

## Замечания
- Событийный контракт привязан к Hermes API-серверу; изменение его событий/полей — внешний breaking change (зафиксировано как зависимость, [01-context.md](01-context.md)).
- Approvals по умолчанию настроены безопасно (deny опасных без подтверждения) на уровне инстанса ([Hermes Runtime / 05-security.md](../hermes-runtime/05-security.md)).
