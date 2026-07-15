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
| `usage.delta` ⓘ | `{step_index, input_tokens, output_tokens, cumulative_input_tokens, cumulative_output_tokens, cumulative_total_tokens, model}` | ретрансляция + **incremental-биллинг** ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md), под флагом `agent_incremental_billing_enabled`); при OFF — только ретрансляция |
| `run.completed` | `{usage:{input_tokens, output_tokens, total_tokens}, ...}` | **биллинг:** постфактум `consume(idempotency_key=runId)` ([ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)) ИЛИ финализация остатка при incremental ([ADR-064 §2](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) + ретрансляция |
| `run.failed` | `{error, ...}` | ретрансляция; **без debit** (нет usage) |
| `run.paused` ⚙ | `{event:"run.paused", run_id, reason:"credits_exhausted", status:"paused", output:"<промежуточный текст>", steps, billed:<charged>, balance:0, usage:{cumulative_input_tokens, cumulative_output_tokens}}` | **синтетическое, генерирует control plane** ([ADR-064 §3](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)); терминальное для клиента (стрим закроется БЕЗ `run.completed`) |

ⓘ `usage.delta` — **внешний контракт Hermes** (патч образа, per LLM API-вызов loop). `cumulative_*_tokens` — источник тарификации (анти-двойной-счёт). Точки эмиссии (ориентир, проверить при патче): `agent/conversation_loop.py` ~2059, `gateway/platforms/api_server.py` ~4478.
⚙ `run.paused` — **НЕ** событие Hermes: control plane эмитит его при исчерпании баланса (после `stop`→Hermes interrupt), промежуточный результат — из локального relay-буфера.

## Wire-формат и диспетчеризация клиента
- Все события — SSE-строки вида `data: {json}` (единый транспорт Hermes, [ADR-045 §3](../../adr/ADR-045-hermes-as-agent-proxy.md)); тип события несёт **JSON-поле `"event"` внутри тела** (напр. `data: {"event":"usage.delta", ...}`, `data: {"event":"run.paused", ...}`), а не SSE-заголовок `event:`. **Клиент диспетчеризует по JSON-полю `"event"`** — включая синтетическое `run.paused` (control plane эмитит его тем же wire-форматом, что и любое Hermes-событие).
- `run.paused` — **терминальное**: после него стрим закрывается **без** `run.completed`. Промежуточный текст ответа — в ключе **`output`** (склеенный буфер `message.delta`; **НЕ `message`**), детализация — в `steps`; `billed` = списанные кредиты, `balance:0`, `usage.cumulative_*` — накопленный расход к точке паузы. Клиент показывает `output`/`steps` и предлагает пополнение → `POST /v1/agent/runs/{runId}/resume`.

## Redaction usage-полей (инвариант + follow-up)
- **Дельта-каунты `input_tokens`/`output_tokens`/`total_tokens`** (обе казинга) — в `_USAGE_COUNT_ALLOWLIST` ([ADR-049](../../adr/ADR-049-redaction-usage-token-counts-allowlist.md), `src/app/observability/redaction.py`): **переживают** redaction (билинг-аналитика, не секрет) везде, где `meta.usage` проходит через `redact()`.
- **Кумулятивные `cumulative_input_tokens`/`cumulative_output_tokens`/`cumulative_total_tokens`** ([ADR-064 §7](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) — матчат substring-денилист `token`, но **НЕ** входят в `_USAGE_COUNT_ALLOWLIST`. **Текущий инвариант:** на incremental-пути `meta.usage.cumulative_*` попадают только в `ledger_transactions.meta` (сырой, минуя `redact()`), в тело SSE `usage.delta` (relay as-is) и в admin wallet-view (сырой ledger.meta) — **ни один из этих путей не пропускает их через `redact()`**, поэтому сейчас они не редактируются и сохраняются для реконсиляции. ⚠️ **Латентный риск:** если будущий audit-путь начнёт нести `meta.usage` через `redact()`, `cumulative_*` будут ошибочно вычищены (billing-аналитика потеряна, противоречит принципу [ADR-049](../../adr/ADR-049-redaction-usage-token-counts-allowlist.md)). Defense-in-depth-фикс (расширить allowlist на `cumulative_*`) зафиксирован как **[TD-035](../../100-known-tech-debt.md)** (owner backend). Реальные токен-секреты (`API_SERVER_KEY`/bearer/`x-admin-token`) редактируются как прежде.

## Incremental-биллинг ([ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md), флаг `agent_incremental_billing_enabled`)
- На каждом `usage.delta`: `owed = usage_to_credits(cumulative_input, cumulative_output)`; `want = owed − charged`; при `want>0` списать `charge = min(want, balance)` — idempotency `f"{runId}:{stepIndex}"`, `meta.source='agent_run'`+`incremental=true`, commit каждый шаг. Сумма per-step charges **телескопирует точно** в `usage_to_credits(final)` (per-step `ceil` дельт запрещён — инфляция).
- **Seed `charged` из ledger** (reconnect-safe): `charged_for_run` = `SUM(amount)` по debit-строкам `idempotency_key = runId OR LIKE 'runId:%'`, читается в начале `stream_events`.
- **Stop-at-0:** при `charge < want` (баланса не хватило) → `stop(runId)` (Hermes interrupt) + синтетическое `run.paused` + `agent_runs.status='paused'`. **БЕЗ долга** (`charge ≤ balance` → debt-ветка [ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md) недостижима; `netBalance==balance` [ADR-063](../../adr/ADR-063-client-facing-debt-and-net-balance.md)). Сервис поглощает недобор ≤ одного шага ([Q-064-2](../../99-open-questions.md)).
- **Финализация на `run.completed`:** `remainder = usage_to_credits(final) − charged`, при `>0` — debit idempotency `runId` (голый). Paused-прогон финализации не имеет (нет `run.completed`). Флаг OFF: `charged=0` → `remainder=full` = постфактум [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md).

## Биллинг на `run.completed`
- `amount = ceil(input_tokens/1000*CREDITS_PER_1K_INPUT + output_tokens/1000*CREDITS_PER_1K_OUTPUT)`; минимум `1` при ненулевом usage; кредиты целые.
- Идемпотентность по `runId` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)) — повторная подписка/ретрай/дубль события → один debit.
- `usage` сохраняется в `ledger_transactions.meta` (аудит/аналитика), не содержит секретов.
- `audit`-событие `agent_run` + `billing_debit` (без `API_SERVER_KEY`/user-content).
- **Недостаток баланса на финализации `run.completed` ([ADR-047 §6](../../adr/ADR-047-usage-based-billing-for-agent.md) / [ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md)):** ретранслятор НЕ рвёт стрим. Поведение зависит от `AGENT_DEBT_RECONCILE_ENABLED` (дефолт **`true`**):
  - **Дефолт (reconcile ON) — основная ветка ([ADR-051](../../adr/ADR-051-agent-debt-reconciliation.md), `_consume_agent_with_debt`, `wallet/service.py`):** `consume` списывает доступный `balance` (**частичный** ledger-debit) и недобор `delta = amount − balance` кладёт в **`wallets.debt`** (НЕ ledger-строка); audit **`billing_debit_insufficient`** (+ `partialDebited`/`debtAdded`/`debt`). Долг гасится clawback'ом при пополнении; следующий прогон блокируется policy `debt_outstanding` до погашения ([02-api-contracts.md](02-api-contracts.md)). `InsufficientCreditsError` **не** поднимается.
  - **Flag-off / гоночный fallback:** при выключенном флаге (или редкой гонке, когда partial-путь вернул `None` и defer в обычный `_debit_in_savepoint`) `consume` поднимает `InsufficientCreditsError` → savepoint откатывается (**debit не записан, баланс не тронут, orphan-строки нет**), несписанная дельта фиксируется тем же audit `billing_debit_insufficient` — usage не теряется молча.
  - Реконсиляция долга закрыта **[TD-029](../../100-known-tech-debt.md) (ADR-051)**; открытым остаётся только обрыв SSE до `run.completed` — [Q-047-2](../../99-open-questions.md).
  - **NB (incremental-путь, [ADR-064](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)):** per-step debit долг **не** копит (self-clamp `_consume_incremental_clamp`, `charge≤balance`) → при 0 идёт `run.paused` без долга. Debt-ветка выше применима только к **финализации остатка** на `run.completed` (meta без `incremental`).
- **Usage-каунты НЕ редактируются ([ADR-049](../../adr/ADR-049-redaction-usage-token-counts-allowlist.md)):** `input_tokens`/`output_tokens`/`total_tokens` в payload `billing_debit_insufficient` (и в `agent_run`/`billing_debit`/`ledger.meta.usage`) — целочисленная биллинг-аналитика, НЕ секрет; redaction-allowlist исключает их из `*token*`-денилиста, поэтому usage сохраняется для реконсиляции. Реальные токен-секреты (`API_SERVER_KEY`, `identityToken`, `x-admin-token`, bearer) редактируются как прежде.

## Замечания
- Событийный контракт привязан к Hermes API-серверу; изменение его событий/полей — внешний breaking change (зафиксировано как зависимость, [01-context.md](01-context.md)).
- Approvals по умолчанию настроены безопасно (deny опасных без подтверждения) на уровне инстанса ([Hermes Runtime / 05-security.md](../hermes-runtime/05-security.md)).
