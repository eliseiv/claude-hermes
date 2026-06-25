# ADR-049 — Redaction-allowlist для usage-token-каунтов (`*_tokens` ≠ секрет)

- Статус: Accepted
- Дата: 2026-06-23
- Связан с: [ADR-047](ADR-047-usage-based-billing-for-agent.md) (usage-based billing, §5/§6 — usage обязан сохраняться в audit/ledger), [ADR-009](ADR-009-admin-token-auth.md) (`x-admin-token`), [ADR-043](ADR-043-sign-in-with-apple.md) (`identityToken`), [05-security.md §Логирование](../05-security.md#логирование-безопасное), [modules/agent-proxy/05-events.md](../modules/agent-proxy/05-events.md), [modules/wallet-ledger/02-api-contracts.md](../modules/wallet-ledger/02-api-contracts.md), [modules/notifications/00-overview.md](../modules/notifications/00-overview.md) (`push_token`)

## Context

Два контура `docs/` вступили в прямое противоречие, материализовавшееся в коде (выявлено qa-тестом):

1. **[05-security.md §Логирование](../05-security.md#логирование-безопасное)** задаёт secret-denylist redaction по подстрокам, включая `*token*`. Реализация — `src/app/observability/redaction.py`: `_DENY_SUBSTRINGS` содержит `"token"`; `_is_sensitive_key` помечает чувствительным **любой** ключ, содержащий подстроку `token`.
2. **[ADR-047 §6 п.2](ADR-047-usage-based-billing-for-agent.md)** и **[modules/agent-proxy/05-events.md](../modules/agent-proxy/05-events.md)** требуют, чтобы `usage` (token-каунты `input_tokens`/`output_tokens`/`total_tokens`) **обязательно** сохранялся в audit-событии `billing_debit_insufficient` (а также в `ledger_transactions.meta.usage` и audit `billing_debit`/`agent_run`) — для реконсиляции несписанной дельты ([Q-047-2](../99-open-questions.md) / [TD-029](../100-known-tech-debt.md)): «usage не теряется молча».

Следствие конфликта: подстрока `token` ⊂ `input_tokens`/`output_tokens`/`total_tokens`, поэтому redaction заменял эти ключи на `***REDACTED***` в payload audit-события `billing_debit_insufficient` (и потенциально в `billing_debit.meta.usage`, `agent_run`). Это **прямо нарушает** требование ADR-047 §6 / agent-proxy/05-events.md — реконсиляция становится невозможной (usage теряется).

**Природа данных.** Token-каунты usage — целочисленная биллинг-аналитика реального потребления модели, а **НЕ секрет** (в отличие от `api_key`/`Authorization`/`API_SERVER_KEY`/Apple `identityToken`/user-content). Денилист по подстроке `token` слишком широк и захватывает легитимную usage-аналитику.

**Ограничение, которое нельзя нарушать.** В системе есть **реальные** токен-секреты, чьё имя содержит `token` и которые ОБЯЗАНЫ оставаться под redaction: `identityToken` (Apple OIDC, credential-equivalent — [ADR-043](ADR-043-sign-in-with-apple.md)), `push_token` (чувствительный device-идентификатор APNs — [modules/notifications](../modules/notifications/00-overview.md)), `x-admin-token` ([ADR-009](ADR-009-admin-token-auth.md)), плюс возможные `access_token`/`refresh_token`/`bearer_token`/`api_token`. Решение НЕ должно их раскрыть.

## Decision

**Приоритет:** требование сохранять usage (ADR-047 §6 / agent-proxy/05-events.md) первично для usage-каунтов; денилист `*token*` (05-security.md) **уточняется**, а не отменяется.

**Развязка — точечный closed-set allowlist usage-каунтов в redaction:**

1. **Allowlist (исключение из redaction) — ровно три имени каунта, в обоих casings:**
   - snake_case (агентный путь, Hermes wire / ADR-047 §6): `input_tokens`, `output_tokens`, `total_tokens`;
   - camelCase (chat-путь, `ledger_transactions.meta.usage` / [wallet-ledger/02-api-contracts.md](../modules/wallet-ledger/02-api-contracts.md)): `inputTokens`, `outputTokens`, `totalTokens`.

   Поскольку `_is_sensitive_key` приводит ключ к нижнему регистру, allowlist задаётся как lowercased-набор: `{input_tokens, output_tokens, total_tokens, inputtokens, outputtokens, totaltokens}`. Эти ключи проходят в логи/audit/ledger **как есть** (целые числа).

2. **Allowlist проверяется ПЕРВЫМ** в `_is_sensitive_key` — до подстрочного `*token*`/`*key*`/`*secret*`-матча. Это тот же паттерн, что уже существующий carve-out `lowered.endswith("status")` для `keyStatus` (метаданные, не секрет).

3. **Денилист сужен по семантике, НЕ ослаблен.** `*token*` после allowlist по-прежнему редактирует **все** реальные токен-секреты. Allowlist — **закрытый набор из 3 имён каунтов** (6 строк с учётом casing); по построению ни один реальный секрет не называется `input_tokens`/`output_tokens`/`total_tokens`-каунтом usage, поэтому carve-out не открывает ни одного секрета.

## Consequences

**Положительные:**
- Снят конфликт docs↔docs (05-security.md ↔ ADR-047 §6 / agent-proxy/05-events.md); оба раздела самосогласованы.
- `usage` (token-каунты) корректно сохраняется в audit (`billing_debit_insufficient`, `billing_debit`, `agent_run`) и `ledger_transactions.meta.usage` → реконсиляция [TD-029](../100-known-tech-debt.md) / [Q-047-2](../99-open-questions.md) реализуема.
- Security не ослаблена: allowlist закрытый и явный; реальные токен-секреты (`identityToken`/`push_token`/`x-admin-token`/`access_token`/`bearer`/`api_token`) редактируются как прежде.

**Отрицательные / ограничения:**
- Redaction-правило перестало быть «чисто подстрочным» — добавлено явное исключение. Митигация: исключение минимально (3 имени, 2 casings), задокументировано здесь и в [05-security.md](../05-security.md#usage-token-каунты-не-редактируются-allowlist-adr-049), покрыто тестом (qa).
- Если в будущем появится новый usage-каунт с `token` в имени (например провайдер добавит `cache_read_input_tokens`), его придётся явно внести в allowlist — иначе он будет отредактирован (fail-safe в сторону redaction; не утечка). Зафиксировать при появлении.

## Alternatives

1. **Ослабить денилист — убрать `token` из `_DENY_SUBSTRINGS`.** Отвергнуто: раскроет `identityToken`/`push_token`/`x-admin-token`/`access_token` — недопустимая утечка credential-equivalent данных.
2. **Широкое правило «не редактировать любой `*_tokens`».** Отвергнуто: слишком широко — гипотетический `secret_tokens`/`api_tokens` прошёл бы. Closed-set из 3 явных имён безопаснее.
3. **Переименовать usage-каунты, чтобы не содержали `token` (например `input_units`).** Отвергнуто: `input_tokens`/`output_tokens` — внешний wire-контракт Hermes ([agent-proxy/05-events.md](../modules/agent-proxy/05-events.md)) и устоявшаяся usage-номенклатура провайдеров; переименование ломает контракт и аналитику.
4. **Хранить usage вне redaction-обрабатываемого payload (отдельная не-redacted колонка).** Отвергнуто: усложняет схему/контракты ради того, что точечный allowlist решает в одной функции; usage логически часть `meta`/audit-payload.

## needs_code_sync (backend, `src/app/observability/redaction.py`)

Точная семантика для исполнителя:

- В модуль добавить closed-set allowlist usage-каунтов (lowercased):
  `_USAGE_COUNT_ALLOWLIST = ("input_tokens", "output_tokens", "total_tokens", "inputtokens", "outputtokens", "totaltokens")`.
- В `_is_sensitive_key(key)` **первой** проверкой (до `endswith("status")` или сразу рядом с ним, в любом случае ДО `_DENY_EXACT`/`_DENY_SUBSTRINGS`-проверок) вернуть `False`, если `lowered in _USAGE_COUNT_ALLOWLIST`.
- НЕ трогать `_DENY_SUBSTRINGS` (оставить `"token"`), НЕ трогать `_DENY_EXACT`. Денилист сохраняется целиком.
- Инвариант, который должен подтвердить тест: после правки `redact({"input_tokens":1,"output_tokens":2,"total_tokens":3})` возвращает значения без изменений; `redact({"identityToken":"x","push_token":"y","x-admin-token":"z","access_token":"a","api_key":"b","authorization":"c"})` → все `***REDACTED***`.
