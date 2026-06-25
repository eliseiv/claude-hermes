# ADR-050 — Redaction-allowlist для `idempotencyKey` (дедуп-ключ ≠ секрет)

- Статус: Accepted
- Дата: 2026-06-23
- Связан с: [ADR-048](ADR-048-admin-credits-and-subscription-grant.md) (admin credits/subscription grant — audit обязан нести `idempotencyKey`), [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md) (тот же приём carve-out для usage-каунтов — образец), [ADR-005](ADR-005-idempotency-ledger.md) (idempotency ledger), [ADR-009](ADR-009-admin-token-auth.md) (`x-admin-token` — остаётся под redaction), [05-security.md §Логирование](../05-security.md#логирование-безопасное), [modules/admin/02-api-contracts.md](../modules/admin/02-api-contracts.md), [modules/admin/06-rbac.md](../modules/admin/06-rbac.md), [modules/audit/02-api-contracts.md](../modules/audit/02-api-contracts.md)

## Context

Прямое противоречие двух контуров `docs/`, материализующееся в коде (аналог [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)):

1. **[05-security.md §Логирование](../05-security.md#логирование-безопасное)** задаёт secret-denylist redaction по подстрокам, включая `*key*`. Реализация — `src/app/observability/redaction.py`: `_DENY_SUBSTRINGS` содержит `"key"`; `_is_sensitive_key` помечает чувствительным **любой** ключ, содержащий подстроку `key`.
2. **[modules/admin/02-api-contracts.md](../modules/admin/02-api-contracts.md), [06-rbac.md §Аудит](../modules/admin/06-rbac.md)** предписывают, что audit-события `admin_grant` и `admin_subscription_grant` (код-константа `EVENT_ADMIN_SUBSCRIPTION_GRANT`, [ADR-048 §2](ADR-048-admin-credits-and-subscription-grant.md)) **несут** поле `idempotencyKey` — для трассируемости admin-операции (связать audit-запись с конкретным запросом оператора и его дедуп-ключом).

Следствие конфликта: подстрока `key` ⊂ `idempotencyKey` (lowercased `idempotencykey`), поэтому redaction заменяет значение на `***REDACTED***` в `payload` audit-событий `admin_grant`/`admin_subscription_grant`. Это **нарушает** контракт audit — трассируемость по дедуп-ключу теряется.

**Природа данных.** `idempotencyKey` — **клиентский дедуп-ключ** (произвольная строка, которую оператор/клиент задаёт, чтобы повтор запроса не выполнил операцию дважды, [ADR-005](ADR-005-idempotency-ledger.md)). Это **НЕ секрет**: знание ключа не даёт доступа, не является credential, не аутентифицирует. Денилист по подстроке `key` слишком широк и захватывает легитимный дедуп-идентификатор (как `*token*` захватывал usage-каунты в [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)).

**Ограничение, которое нельзя нарушать.** В системе есть **реальные** секреты, чьё имя содержит `key` и которые ОБЯЗАНЫ оставаться под redaction: `api_key` (BYOK/Anthropic), `API_SERVER_KEY` (Hermes-инстанс, [ADR-046](ADR-046-per-user-hermes-runtime.md)), `CLIENT_API_KEY` ([ADR-044](ADR-044-client-api-key-auth.md)), `encrypted_key`/`encrypted_dek` (envelope, [ADR-003](ADR-003-byok-envelope-encryption.md)). Решение НЕ должно их раскрыть. **Примечание:** `encrypted_dek` до этого ADR фактически НЕ редактировался (gap: его нет в `_DENY_EXACT`, подстроки `key` в имени нет) — этот ADR попутно закрывает gap defense-in-depth (см. §needs_code_sync), приводя код к security-инварианту.

## Decision

**Приоритет:** требование audit нести `idempotencyKey` (admin-контракт / ADR-048) первично для этого ключа; денилист `*key*` (05-security.md) **уточняется**, а не отменяется.

**Развязка — точечный closed-set allowlist по образцу [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md):**

1. **Allowlist (исключение из redaction) — ровно одно имя ключа, в обоих casings:**
   - camelCase (wire/audit-payload, [admin/02-api-contracts.md](../modules/admin/02-api-contracts.md)): `idempotencyKey`;
   - snake_case (внутренние meta/поля, [ADR-005](ADR-005-idempotency-ledger.md)): `idempotency_key`.

   Поскольку `_is_sensitive_key` приводит ключ к нижнему регистру, allowlist задаётся как lowercased-набор: `{idempotencykey, idempotency_key}`. Эти ключи проходят в логи/audit как есть (строка дедуп-ключа).

2. **Allowlist проверяется ПЕРВЫМ** в `_is_sensitive_key` — до подстрочного `*key*`/`*token*`/`*secret*`-матча. Тот же паттерн, что carve-out usage-каунтов ([ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)) и `endswith("status")` для `keyStatus`. Реализационно объединяется с allowlist ADR-049 в один проверяемый-первым набор.

3. **Денилист сужен по семантике, НЕ ослаблен.** `*key*` после allowlist по-прежнему редактирует **все** реальные key-секреты (`api_key`/`API_SERVER_KEY`/`CLIENT_API_KEY`/`encrypted_key`/`encrypted_dek`/…). Allowlist — **закрытый набор из 1 имени** (2 строки с учётом casing); по построению ни один реальный секрет не называется `idempotencyKey`/`idempotency_key`, поэтому carve-out не открывает ни одного секрета.

## Consequences

**Положительные:**
- Снят конфликт docs↔docs (05-security.md §Логирование ↔ admin-контракт audit / ADR-048); оба раздела самосогласованы.
- `idempotencyKey` корректно сохраняется в audit (`admin_grant`, `admin_subscription_grant`) → трассируемость admin-операций по дедуп-ключу реализуема.
- Security не ослаблена: allowlist закрытый и явный; реальные key-секреты редактируются как прежде. Согласован по приёму с [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md).
- **Усиление:** попутно закрыт пре-существующий gap `encrypted_dek` (добавлен в `_DENY_EXACT`) — зашифрованный DEK теперь редактируется by construction, ADR↔код по security-инварианту [ADR-003](ADR-003-byok-envelope-encryption.md) согласованы.

**Отрицательные / ограничения:**
- Redaction-правило получает ещё одно явное исключение. Митигация: исключение минимально (1 имя, 2 casings), задокументировано здесь и в [05-security.md](../05-security.md#дедуп-ключ-idempotencykey-не-редактируется-allowlist-adr-050), покрывается тестом (qa).
- Если появится секрет с буквальным именем `idempotency_key` — он пройдёт в лог (fail-open для этого единственного имени). Оценено как невозможное: имя зарезервировано за дедуп-ключом по всей системе ([ADR-005](ADR-005-idempotency-ledger.md)).

## Alternatives

1. **Оставить `idempotencyKey` под redaction (`***REDACTED***`), убрать требование нести его в audit.** Отвергнуто: ломает трассируемость admin-операций (нельзя связать audit с конкретным запросом оператора); admin-контракт ([06-rbac.md §Аудит](../modules/admin/06-rbac.md)) явно требует ключ в payload.
2. **Ослабить денилист — убрать `key` из `_DENY_SUBSTRINGS`.** Отвергнуто: раскроет `api_key`/`API_SERVER_KEY`/`CLIENT_API_KEY` — недопустимая утечка credential.
3. **Хэшировать `idempotencyKey` в audit (писать дайджест вместо значения).** Отвергнуто: усложняет трассировку (оператор не сопоставит хэш со своим ключом без отдельного инструмента) ради сокрытия не-секрета; closed-set allowlist проще и честнее отражает природу данных.
4. **Переименовать поле, чтобы не содержало `key` (напр. `dedupId`).** Отвергнуто: `idempotencyKey`/`idempotency_key` — устоявшаяся номенклатура контрактов и ledger ([ADR-005](ADR-005-idempotency-ledger.md), wallet/admin API); переименование ломает контракт.

## needs_code_sync (backend, `src/app/observability/redaction.py`)

Точная семантика для исполнителя (тот же файл и приём, что [ADR-049](ADR-049-redaction-usage-token-counts-allowlist.md)):

- Расширить closed-set allowlist (lowercased) именами дедуп-ключа: добавить `"idempotencykey"` и `"idempotency_key"` к уже существующему usage-allowlist ADR-049 (`input_tokens`/…). Допустимо как отдельный набор `_IDEMPOTENCY_KEY_ALLOWLIST = ("idempotencykey", "idempotency_key")`, проверяемый рядом с usage-allowlist.
- В `_is_sensitive_key(key)` проверка allowlist (usage-каунты ADR-049 + idempotency-ключ) выполняется **первой** — до `_DENY_EXACT`/`_DENY_SUBSTRINGS`-проверок: вернуть `False`, если `lowered` в объединённом allowlist.
- НЕ трогать `_DENY_SUBSTRINGS` (оставить `"key"`/`"token"`/`"secret"`). Денилист сохраняется целиком и **дополнительно усиливается** (см. следующий пункт).
- **Закрытие пре-существующего redaction-gap `encrypted_dek` (defense-in-depth, выявлено backend при code-sync).** В `_DENY_EXACT` присутствуют `dek` (exact) и `encrypted_key`, но **НЕ** `encrypted_dek`, и подстроки `key`/`token`/`secret` в имени `encrypted_dek` нет → `redact({"encrypted_dek": ...})` сейчас возвращает значение как есть. Это расхождение ADR↔код против security-инварианта «зашифрованные ключи редактируются». Не активная утечка (в audit/log payload BYOK идёт `keyStatus`, а `encrypted_dek` — bytes-колонка БД, в payload не попадает — см. [05-security.md §BYOK envelope](../05-security.md), [ADR-003](ADR-003-byok-envelope-encryption.md)), но инвариант должен выполняться **by construction**. **Добавить `"encrypted_dek"` в `_DENY_EXACT`** (рядом с `encrypted_key`/`dek`). Это **расширение** денилиста (усиление), не ослабление.
- Инвариант, который должен подтвердить тест: `redact({"idempotencyKey":"sub-grant-42"})` → значение без изменений; `redact({"api_key":"x","API_SERVER_KEY":"y","CLIENT_API_KEY":"z","encrypted_key":"v","encrypted_dek":"w","dek":"d"})` → все `***REDACTED***`.
