# ADR-060: Согласование transport-лимита `SizeLimitMiddleware` с contract-лимитом workspace-upload

- **Статус:** Accepted (2026-07-15)
- **Контекст модулей:** `api-gateway` (SizeLimitMiddleware), `workspaces` (knowledge-files upload)
- **Связано с:** [ADR-020](ADR-020-inline-base64-attachments-mvp.md) (raised transport-лимит `/v1/chat/run`), [ADR-036](ADR-036-workspaces-implementation.md) (workspaces knowledge-files, inline base64), [TD-017](../100-known-tech-debt.md) (streaming-safe guard), [TD-033](../100-known-tech-debt.md) (chat/run aggregate mismatch)

## Context

`POST /v1/workspaces/{workspace_id}/files` ([ADR-036 §4](ADR-036-workspaces-implementation.md)) принимает файл-знание как **inline base64** в JSON-теле (`WorkspaceFileUploadRequest`: `type`/`mediaType`/`filename`/`data`), симметрично chat-вложениям ([ADR-020](ADR-020-inline-base64-attachments-mvp.md)). Contract-лимит одного файла — `WORKSPACE_FILE_MAX_BYTES` = **8 MB** (decoded).

`SizeLimitMiddleware` (`src/app/api_gateway/middleware.py`, `_limit_for`) применяет **общий транспортный лимит** `size_limit_body` = **512 KB** (`config.py`) ко всем роутам, повышая его **только** для точного пути `/v1/chat/run` до `attachment_request_body_limit` = 12 MB (`_CHAT_RUN_PATH`, ADR-020).

**Дефект (верифицирован на проде, файл 484 KB → `413`).** base64 раздувает тело в ~4/3 раза, плюс JSON-envelope (`type`/`mediaType`/`filename`≤512 + имена полей/кавычки). Файл 484 KB → тело ≈ 645 KB > 512 KB → middleware режет `413` **до** endpoint-валидации ADR-036. Фактический потолок реального файла ≈ **380 KB** (512 KB / 1.333 − envelope) вместо заявленных 8 MB. Явное рассогласование: transport-лимит middleware (512 KB) < base64(contract-лимит 8 MB) ≈ 10.67 MB. Заявленный контракт ADR-036 (8 MB/файл) на практике недостижим.

`GET /v1/workspaces/{workspace_id}/files` (list) делит тот же collection-путь, но безвреден (нет тела).

## Decision

### 1. Отдельный config-константа `workspace_request_body_limit` (вариант A)

Вводится независимый транспортный лимит:

```
workspace_request_body_limit: int = Field(default=12 * 1024 * 1024, alias="WORKSPACE_REQUEST_BODY_LIMIT")
```

- **Значение — 12 MB** (12 582 912 байт). Обоснование: base64(8 MB) = 8 388 608 × 4/3 ≈ 10.67 MB + JSON-envelope (несколько KB). 12 MB даёт запас ≈ 1.3 MB над base64-раздутым максимумом → 8 MB-файл гарантированно проходит транспорт. Workspace-upload — **один** файл на запрос (`WorkspaceFileUploadRequest` — не список), поэтому агрегатный кейс chat/run ([TD-033](../100-known-tech-debt.md)) здесь не возникает.
- **Вариант A предпочтён варианту B** (переиспользовать `attachment_request_body_limit`): независимый контроль поверхности приёма крупного payload. chat/run и workspace-upload — разные роуты с разными контрактами (10 MB total attachments vs 8 MB single file) и потенциально разной калибровкой оператором; связывать их одной константой означает, что изменение лимита одного роута молча меняет второй. Стоимость варианта A — одна config-строка; выгода — изоляция поверхностей (симметрично изоляции секретов ADR-017 и грантов ADR-029).

### 2. `_limit_for` — набор правил вместо хардкода одного пути

`SizeLimitMiddleware._limit_for(path)` переводится с единичного равенства на **упорядоченный набор правил** (первое совпадение — победитель, иначе общий лимит):

1. `path == "/v1/chat/run"` → `attachment_request_body_limit` (ADR-020, без изменений).
2. `path.startswith("/v1/workspaces/") and path.endswith("/files")` → `workspace_request_body_limit` (новое).
3. иначе → `size_limit_body` (общий 512 KB).

**Верификация path-matching (по фактическому коду `routers/workspaces.py`):**

| Путь | Метод(ы) | Правило 2 матчит? | Корректно? |
|---|---|---|---|
| `/v1/workspaces/{id}/files` | POST upload, GET list | да (`startswith` ∧ `endswith("/files")`) | да — upload нуждается в raise; GET безвреден (нет тела) |
| `/v1/workspaces/{id}/files/{file_id}` | GET, DELETE | нет (`endswith("/{file_id}")`, не `/files`) | да — не матчит, не нужен raise |
| `/v1/workspaces` | POST create, GET list | нет (`"/v1/workspaces".startswith("/v1/workspaces/")` = False) | да — маленькое JSON-тело, общий 512 KB |
| `/v1/workspaces/{id}` | GET, PATCH, DELETE | нет (`endswith` ≠ `/files`) | да — маленькое JSON-тело |

Правило 1 (`/v1/chat/run`) остаётся **точным** матчем. `_CHAT_RUN_PATH` не заменяется на префикс/суффикс.

### 3. Инвариант согласованности

Для каждого роута с inline-base64-контрактом должно выполняться:

> transport-лимит ≥ base64(max decoded contract size) × 4/3 + запас на JSON-envelope.

- `/v1/chat/run`: single-file 8 MB (document-cap) → 10.67 MB < 12 MB ✅. **Агрегатный** кейс (`attachment_total_bytes` = 10 MB total → 13.33 MB base64 > 12 MB) — нарушение инварианта, вынесено в [TD-033](../100-known-tech-debt.md) (single-file работает; затронут только multi-attachment у потолка).
- `/v1/workspaces/{id}/files`: single-file 8 MB → 10.67 MB < 12 MB ✅ (настоящий ADR).

## Consequences

- **Положительно:** заявленный контракт ADR-036 (8 MB/файл) достижим; прод-баг `413` на 484 KB устранён; поверхность raise ограничена ровно collection-путём workspace-files (не глобально); правило `_limit_for` расширяемо для будущих upload-роутов без хардкода.
- **Нейтрально:** общий лимит `512 KB` для всех прочих роутов **не изменён**; `/v1/chat/run` **без регресса** (правило 1 идентично). Поверхность приёма крупного payload расширена на один роут (POST workspace-upload) — приемлемо: тот же threat-model, что chat/run (лимиты размера/числа/magic-bytes/anti-zip-bomb проверяются в сервисе до `b64decode`, [ADR-036 §4](ADR-036-workspaces-implementation.md), [05-security.md](../05-security.md)).
- **Остаточно:** transport-guard по-прежнему опирается на `Content-Length` при его наличии + streaming byte-count при отсутствии ([TD-017](../100-known-tech-debt.md), реализован); настоящий ADR не меняет механику guard, только applicable-лимит.
- **Осознанно НЕ поднят транспортный лимит `/v1/chat/tool-result`.** Роут получает общий 512 KB (точный raise-матч — только `/v1/chat/run`). Батч-форма `results[]` (`ChatToolResultRequest`, `src/app/schemas/chat.py:207`) несёт по `SIZE_LIMIT_TOOL_RESULT` = 256 KB на элемент (per-item guard `src/app/schemas/chat.py:186,261`), поэтому 2+ параллельных tool-result у потолка → тело > 512 KB → возможный ложный `413` **до** schema-валидации. Это тот же **агрегатный** класс, что [TD-033](../100-known-tech-debt.md) (N × per-item-cap vs общий транспорт), но НЕ base64/upload, предсуществующий и низковероятный (типичный tool-result мал). Общий 512 KB для tool-result оставлен намеренно (не расширять транспортную поверхность ради редкого multi-256KB-батча); зафиксировано как известное ограничение в [TD-033](../100-known-tech-debt.md). Правка контракта — вне scope настоящего ADR.

## Alternatives

- **B: переиспользовать `attachment_request_body_limit` для workspace-роута.** Отвергнут: связывает калибровку двух независимых поверхностей; оператор не может поднять/опустить лимит chat/run без побочного эффекта на workspace-upload.
- **Multipart-upload вместо inline base64.** Вне scope (уже отвергнут [ADR-036 §Альтернативы](ADR-036-workspaces-implementation.md) / ADR-020): новый код парсинга, асимметрия с inline-путём.
- **Глобально поднять `size_limit_body`.** Отвергнут: расширяет DoS-поверхность на все роуты (auth/byok/wallet/admin, где тела маленькие); нарушает принцип «raise только там, где контракт требует».
