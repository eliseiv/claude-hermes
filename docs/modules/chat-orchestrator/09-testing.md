# Chat Orchestrator — Testing

## Unit
- Tool-схемы: валидные/невалидные args/result для всех 8 tools → 422 на нарушение.
- `path` traversal (`..`) отклоняется.
- Маппинг ответа Anthropic (end_turn/tool_use) → status.
- usage parsing включая cache_read/cache_creation.
- **tool_use.id (BUG-4, ADR-008):** разбор `tool_use` с реалистичным anthropic id (`toolu_01...`, **не** UUID) → `tool_calls.provider_tool_use_id` = raw id; `tool_calls.id` = свежий UUID (не выведен из anthropic id); наружу `toolCall.id` = доменный UUID.
- **Нормализация payload (BUG-5, ADR-021):** assistant `tool_use`-блок из ответа SDK со служебным полем `caller` (`block.model_dump()`) → в `chat_steps.payload` сохранены только wire-валидные поля (`type`/`id`/`name`/`input`), `caller` отсутствует; raw `tool_use.id` сохранён дословно. Реконструированные `messages` к Anthropic не содержат `caller`.

> **Требование к fake/мокам Anthropic-клиента:** во ВСЕХ тестах (unit/integration/e2e) fake `messages.create` обязан возвращать `tool_use.id` в **реалистичном** формате `toolu_<...>` (НЕ UUID-образный). Старый fake отдавал UUID-образный id и маскировал BUG-4. Запрет UUID-образного provider id в fake — нормативное требование тестовой инфраструктуры.

## Integration (respx для Anthropic)
- `/chat/run` blocked: для каждого blockReason возвращается 200 + reason, генерация не вызвана.
- `/chat/run` allow → assistant_message; chat_steps записан; audit chat_step.
- tool_use → status=tool_call, tool_calls(pending) создан, audit tool_call_initiated.
- `/chat/tool-result` чужой/несуществующий toolCallId → 404/403.
- Повторный tool-result с completed → идемпотентно, Anthropic не вызван повторно.
- mode=byok → используется ключ пользователя (проверка через мок BYOK), ключ не в логах/steps.

## Integration — порядок шагов server-side tool-loop (BUG-5, ADR-021)
- **Детерминированный порядок при равном `created_at`:** server-side tool (`site.*`) пишет `tool_use`-шаг и `tool_result`-шаг в **одной транзакции** (равный `created_at`). Реконструкция (`_build_messages` через `list_steps`) должна давать `messages` в порядке `assistant(tool_use) → user(tool_result)` **независимо** от значений `id`/`created_at`. Тест должен ставить такой `id`, при котором старая `(created_at, id)`-сортировка инвертировала бы порядок (UUID `tool_result` < UUID `tool_use`) → на старой реализации orphan tool_result/400, на новой (`ORDER BY seq`) — корректно.
- `next_step_after` возвращает следующий шаг по `seq`, не по `created_at`.

## Integration — sync ids в `ChatResponse` (ADR-023)

Нормативное покрытие инварианта синка `messageStepId` / `stepId` ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).

- **Непустые id при `assistant_message` / `tool_call`:** ответы `/v1/chat/run` и `/v1/chat/tool-result` со `status=assistant_message` либо `status=tool_call` несут **НЕПУСТЫЕ** `messageStepId` и `stepId` (оба не `null`).
- **`stepId` точно совпадает с историей:** `ChatResponse.stepId` **дословно равен** `ChatStepSchema.id` соответствующего шага в `steps[]` ответа `GET /v1/chats/{id}` (точное совпадение UUID — шаг-носитель: финальный assistant-шаг при `assistant_message`, assistant-шаг с `tool_use`-блоком при `tool_call`).
- **`messageStepId` стабилен в пределах хода:** `messageStepId`, выданный в `/v1/chat/run`, **равен** `messageStepId` в ответе последующего `/v1/chat/tool-result` того же хода (run → tool-result одного хода дают равный `messageStepId`).
- **`blocked` → оба `null`:** при `status=blocked` `messageStepId` = `null` и `stepId` = `null` (шаг/ход не создаются — блок до генерации, [ADR-004](../../adr/ADR-004-blocked-http-200.md)).
- **`stepId`/`messageStepId` ≠ `toolCall.id`:** при `status=tool_call` ни `stepId`, ни `messageStepId` **не равны** `toolCall.id` — это разные идентификаторы (id шага/хода vs доменный `tool_calls.id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).

## E2E (AC-4)
- Полный tool-loop: run → tool_call → tool-result → tool_call → ... → assistant_message (≥2 итерации).
- **Server-side tool-loop continuation (BUG-5 регресс, live):** website-builder `site.*` multi-round tool-loop с реальным Claude → реконструкция диалога корректна (нет orphan tool_result, нет Anthropic 400/502). Покрывается live e2e website-builder после восстановления org Anthropic (см. memory/deployment-state).
- **Continuation с реалистичным anthropic id (BUG-4 регресс):** fake возвращает `tool_use.id = "toolu_..."`; на раунде continuation проверить, что отправленный в Anthropic `tool_result.tool_use_id` **точно равен** этому raw id (а не доменному UUID), и реплеенный assistant `tool_use.id` совпадает с ним → второй `messages.create` не падает с 400. Тест должен падать на старой реализации (`uuid4`-подмена).
