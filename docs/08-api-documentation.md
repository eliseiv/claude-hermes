# 08 — API Documentation (OpenAPI / Swagger)

Сквозной стандарт оформления автогенерируемой OpenAPI-документации FastAPI (`/docs`, `/redoc`, `/openapi.json`). Цель — документация **на русском языке**, читаемая как нормальное руководство по API, с **полностью рабочей авторизацией в Swagger UI для тестировщика** (все способы auth покрыты security schemes), **лаконичными** user-facing текстами и явно описанными бизнес-блокировками.

Это convention поверх уже реализованного backend (все endpoint — см. [modules/api-gateway/02-api-contracts.md](modules/api-gateway/02-api-contracts.md)). Бизнес-контракты не меняются — меняется только их представление в OpenAPI. Реализация — `backend` в `src/app/` (app factory `create_app()`, схемы `src/app/schemas/`, роутеры `src/app/api_gateway/routers/`).

> Это не ADR: оформление документации не является значимым архитектурным решением (не меняет границы компонентов, контракты, модель данных, безопасность по существу). Единственный аспект с эффектом на поверхность атаки — отключение `/docs` в prod — зафиксирован здесь и в [05-security.md](05-security.md), отдельный ADR не требуется.

## R1. Язык

| Что | Язык |
|---|---|
| `FastAPI(description=...)` — общее описание API | русский |
| `summary` каждого endpoint | русский |
| `description` каждого endpoint | русский |
| `description` тегов (`openapi_tags`) | русский |
| Описания полей схем (`Field(description=...)`) | русский |
| Описания response-моделей и примеров | русский |
| Сообщения об ошибках валидации (`detail`) | как есть (генерит FastAPI/Pydantic) |

Остаются **в оригинале** (не переводятся): имена endpoint-путей (`/v1/chat/run`), имена полей схем (`sessionId`, `blockReason`), enum-значения (`assistant_message`, `credits`, `trial_used`), коды ошибок (`validation_error`, `unauthorized`), имена tools (`files.write`), HTTP-методы и коды, имена заголовков (`Authorization`, `X-Device-Id`, `X-Request-Id`).

Правило: **описываем по-русски, идентифицируем по-английски**. Описание поля `sessionId` — на русском («Идентификатор сессии…»), само имя поля — `sessionId`.

## R2. Security schemes (покрывают ВСЕ способы auth)

Swagger UI должен позволять тестировщику авторизоваться **любым** способом, которым реально защищён endpoint. **С Hermes-интеграцией ([ADR-044](adr/ADR-044-client-api-key-auth.md)) клиентский контур переведён с `bearerAuth` (JWT) на `APIKeyHeader`.** В OpenAPI объявляются security schemes для клиентского (`clientApiKey`+`userId`), admin (`adminToken`) и adapty (`adaptyWebhook`) контуров; **каждый** endpoint помечается корректной. Кнопка **Authorize** в Swagger UI показывает все варианты.

### R2.1. `clientApiKey` + `userId` — клиентская auth (Hermes-интеграция, [ADR-044](adr/ADR-044-client-api-key-auth.md))
- **Две `APIKeyHeader`-схемы** (заменяют прежний `bearerAuth` для клиентского контура; механизм FastAPI — `fastapi.security.APIKeyHeader`, объявлено в `src/app/api_gateway/openapi_security.py`):
  - `clientApiKey` — `type: apiKey`, `in: header`, `name: X-API-Key`. Единый клиентский ключ. `description` (RU): «Клиентский API-ключ. Вставьте `CLIENT_API_KEY` в заголовок `X-API-Key` через Authorize — применится ко всем `/v1/*` клиентского контура».
  - `userId` — `type: apiKey`, `in: header`, `name: X-User-Id`. Идентичность субъекта (UUID). `description` (RU): «UUID пользователя. Идентичность доверяется (ключ доверенный). Обязателен вместе с `X-API-Key`».
- **Требуется** (обе схемы) для пользовательских `/v1/*`: `agent` (`/v1/agent/run`, `/v1/agent/runs/{runId}/events|approval|stop`), `chat` (`/v1/chat/run`, `/v1/chat/tool-result`), `GET /v1/tools`, `policy`, `wallet`, `subscription`, `byok`, `chats`, `profile`, `preferences`, `workspaces`, `snippets`, `attachments`, `tokens`, `notifications`. У них в Swagger UI значок замка.
- **`bearerAuth` (JWT) — «спящий»:** объявление scheme может оставаться в коде (JWT/Apple не удалены, [ADR-044](adr/ADR-044-client-api-key-auth.md)), но **не навешивается** на клиентские операции `/v1/*`. На горячем клиентском пути используется `clientApiKey`+`userId`.
- **AND-семантика (обе схемы обязательны вместе), НЕ OR ([ADR-044 §5](adr/ADR-044-client-api-key-auth.md)).** Каждый клиентский `/v1/*` требует **одновременно** `X-API-Key` **и** `X-User-Id`; ни один в отдельности доступа не даёт. В OpenAPI это — **один** security-requirement-объект с обоими ключами: `security: [{ "clientApiKey": [], "userId": [] }]` (ключи внутри одного объекта = логическое AND). Форма `[{ "clientApiKey": [] }, { "userId": [] }]` означает OR (достаточно одной схемы) и **противоречит контракту** — это дефект.
- **Post-process `app.openapi()` обязателен.** Дефолт FastAPI из двух `SecurityBase`-зависимостей на операции публикует их **раздельными** requirement-объектами (`[{clientApiKey:[]},{userId:[]}]` = OR). Реализация в `custom_openapi()` (`src/app/main.py`) пост-обрабатывает сгенерированную схему: для каждой клиентской операции `/v1/*` **сливает** пару раздельных объектов в один AND-объект `[{ "clientApiKey": [], "userId": [] }]`. Объекты `adminToken`/`adaptyWebhook` и public-операции не трогаются.

### R2.2. `adminToken` — admin-auth (X-Admin-Token)
- apiKey scheme: `type: apiKey`, `in: header`, `name: X-Admin-Token`, `scheme_name = adminToken`. Изолированный admin-секрет ([контракт admin](modules/admin/02-api-contracts.md), [05-security.md](05-security.md)).
- В `description` scheme кратко (по-русски): «Изолированный admin-токен. Вставьте секрет в заголовок `X-Admin-Token` через Authorize. Пользовательский JWT admin-действия не авторизует».
- **Требуется** для **всех** `/v1/admin/*` (`POST /v1/admin/credits/grant`, `POST /v1/admin/subscription/grant` ([ADR-048](adr/ADR-048-admin-credits-and-subscription-grant.md)), `GET /v1/admin/wallet/{userId}`; `POST /v1/admin/wallet/grant` — переходный алиас `credits/grant`). До этой фичи admin-эндпоинты не имели объявленной scheme в OpenAPI → тестировщик не мог авторизоваться в Swagger UI; теперь у них значок замка `adminToken`.
- Механизм объявления — добавить scheme в OpenAPI-кастомизацию (рядом с дремлющим `bearerAuth` в `src/app/api_gateway/openapi_security.py` или в `custom_openapi()`); реальная проверка остаётся в `require_admin` ([ADR-009](adr/ADR-009-admin-token-auth.md), неизменна).

### R2.3. Публичные endpoint (без security, без замка)
- Служебные: `GET /health`, `GET /healthz` (алиас `/health`, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)), `GET /ready`, `GET /metrics` (защищён сетью/scrape-токеном, не входит в Swagger Authorize — см. [07-deployment.md](07-deployment.md#health--readiness)).
- **Auth-issuer:** `POST /v1/auth/register`, `POST /v1/auth/token`, `POST /v1/auth/refresh`, `GET /v1/auth/jwks` — точка получения токена, защищены per-IP rate-limit ([контракт](modules/api-gateway/02-api-contracts.md)). Без security в OpenAPI — тестировщик вызывает их без авторизации, чтобы получить токен.
- Preview: `GET /v1/preview/*` — авторизуются signed URL, не JWT. Без security scheme.

### R2.4. Общее
- Объявление security scheme в OpenAPI **не подменяет** реальную проверку (клиентский ключ — `verify_client_api_key()`; идентичность — `get_current_user` из `X-User-Id`, [ADR-044](adr/ADR-044-client-api-key-auth.md); admin — `require_admin`). Это только описание для клиента и Swagger UI. Источник истины аутентификации не меняется.
- Каждый клиентский `/v1/*` обязан иметь привязку **(`clientApiKey` + `userId`)** (обе схемы), admin-эндпоинты — `adminToken`, public — none. Привязки не смешиваются (admin-эндпоинты не принимают клиентскую auth, и наоборот). `bearerAuth` (JWT) **спящий** — на клиентских операциях не навешивается ([ADR-044](adr/ADR-044-client-api-key-auth.md)).
- **Ожидаемый `security`-объект по контурам (acceptance для reviewer/qa, проверять в `/openapi.json`):**

| Контур | Операции | `security` в OpenAPI |
|---|---|---|
| Клиентский | `/v1/*` (кроме admin/auth/preview) | `[{ "clientApiKey": [], "userId": [] }]` (AND — оба ключа в одном объекте) |
| Admin | `/v1/admin/*` | `[{ "adminToken": [] }]` |
| Adapty webhook | `POST /v1/billing/adapty/webhook` | `[{ "adaptyWebhook": [] }]` |
| Public | `/health`, `/healthz`, `/ready`, `/metrics`, `/v1/auth/*`, `/v1/preview/*` | отсутствует (`security` не задан / пустой) |

  OR-форма для клиентских операций (`[{clientApiKey:[]},{userId:[]}]`) — **нарушение контракта** ([ADR-044 §5](adr/ADR-044-client-api-key-auth.md)); требует исправления через post-process `custom_openapi()` (R2.1).

## R2bis. Как тестировать через Swagger (флоу тестировщика)

Swagger UI обязан быть самодостаточным для ручного тестирования всех эндпоинтов — без внешних инструментов и без ручной выпечки токенов. Зафиксированный флоу:

**Пользовательские эндпоинты (`clientApiKey` + `userId`, [ADR-044](adr/ADR-044-client-api-key-auth.md)):**
1. Открыть `/docs`.
2. Нажать **Authorize** → заполнить **обе** схемы: `clientApiKey` (вставить `CLIENT_API_KEY` в `X-API-Key`) и `userId` (вставить UUID субъекта в `X-User-Id`). Оба значения тестировщик получает из секрет-менеджера/env (ключ) и выбирает/создаёт сам (`userId` — идентичность доверяется, ключ доверенный).
3. Тестировать любые `/v1/*` (agent, chat, wallet, chats, profile, preferences, byok, subscription, tokens, `/v1/tools` и др.) — замок закрыт, оба заголовка подставляются автоматически.
4. **`bearerAuth` (JWT) — спящий** ([ADR-044](adr/ADR-044-client-api-key-auth.md)): на клиентских `/v1/*` не навешан; `/v1/auth/*` остаются public (получение токена), но клиентом при Hermes-интеграции не используются.

**Admin-эндпоинты (`adminToken`):**
1. Нажать **Authorize** → выбрать `adminToken` → вставить значение `ADMIN_API_SECRET` (его получает тестировщик из секрет-менеджера/env, не из API).
2. Тестировать `/v1/admin/*` — заголовок `X-Admin-Token` подставляется автоматически.

**Acceptance флоу:** тестировщик проходит весь путь Authorize(`clientApiKey`+`userId`) → защищённый вызов `/v1/*` (в т.ч. `/v1/agent/run`) **и** Authorize(adminToken) → admin-вызов (`/v1/admin/credits/grant`, `/v1/admin/subscription/grant`), не покидая Swagger UI. Если хотя бы одна группа эндпоинтов не авторизуется через Authorize — нарушение R2.

## R2ter. Лаконичность user-facing текстов (для тестировщиков)

OpenAPI-тексты (`summary`, `description` эндпоинтов, `Field(description=...)` в схемах) пишутся **лаконично и для тестировщика**, а не как внутренняя архитектурная документация.

**Правила:**
- `summary` — **одна строка**: что делает endpoint (повелительно/назывательно, ≤ ~60 символов). Например: «Покупка пакета токенов», «Сохранить свой ключ Anthropic».
- `description` — **только существенное для тестировщика**: что отправить, что вернётся, ключевые коды/состояния. Без пересказа внутренней механики.
- **Запрещено** в user-facing OpenAPI-текстах:
  - ссылки на ADR (например `(ADR-015)`, `(ADR-002)`), на `Q-NNN-N`, на `TD-NNN`;
  - избыточные скобки-пояснения и расшифровки аббревиатур ради аббревиатуры (например `(Bring Your Own Key)`);
  - многословные описания серверной механики (детальный маппинг, «отдельный путь от подписки», внутренние сервисы/таблицы).
- **Технические нюансы** (идемпотентность, redaction и т.п.) — упоминаются **кратко** одной фразой или показываются в примере (R5), без перегруза основного текста.
- **ADR/Q/TD-ссылки остаются ТОЛЬКО в `docs/`** (модульные контракты, ADR) — это источник истины для разработчиков. В OpenAPI их быть не должно.

**Примеры приведения к стилю (обязательны к исправлению backend):**

| Где | БЫЛО (многословно) | СТАЛО (лаконично) |
|---|---|---|
| `POST /v1/tokens/purchase` description | «Подписанная consumable-транзакция верифицируется и идемпотентно начисляет кредиты по серверному маппингу productId → credits; отдельный путь от подписки (ADR-015).» | «Покупка пакета токенов через StoreKit. Начисляет кредиты по `productId`. Повторная отправка той же транзакции не начисляет повторно.» |
| Тег / summary `BYOK` | «Свой ключ Anthropic (Bring Your Own Key)» | «Свой ключ Anthropic» |

Правило применяется и к новым эндпоинтам (`/v1/auth/*`, `GET /v1/tools`): их summary/description — в этом же лаконичном стиле.

## R3. Бизнес-блокировки (status=blocked, HTTP 200)

Документация обязана явно объяснить нестандартное правило [ADR-004](adr/ADR-004-blocked-http-200.md): бизнес-блокировка — это **успешный** ответ `200 OK` с телом `{status:"blocked", blockReason}`, а не 4xx.

- В `description` endpoint `/v1/chat/run` и `/v1/chat/tool-result` — абзац на русском: «Блокировка по бизнес-правилам возвращается с HTTP 200 и полем `blockReason` (машиночитаемо). Технические ошибки — 4xx/5xx (см. таблицу кодов)».
- В общем `description` API (R6) — короткая ссылка на это правило, чтобы интегратор не искал 4xx там, где приходит 200.
- Поле `blockReason` (в `ChatResponse`; в `reasons[]` `/policy/effective` — подмножество policy-причин, без `rate_limited`/`max_tokens`) описать как enum с расшифровкой **каждого** из 9 значений: что означает и что должен сделать UI. Канонический источник значений — [ADR-004](adr/ADR-004-blocked-http-200.md) (расширен `max_tokens` в [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); документация не вводит новых значений.

### Расшифровка blockReason (для описаний полей и общего раздела)

| Значение | Что означает | Что делает UI |
|---|---|---|
| `trial_used` | Бесплатная пробная генерация уже использована, подписки нет. | Предложить оформить подписку. |
| `subscription_required` | Действие требует активной подписки, её нет. | Экран оформления подписки. |
| `subscription_expired` | Подписка была, но истекла/отозвана. | Предложить продлить подписку. |
| `credits_empty` | Баланс кредитов исчерпан (режим `credits`). | Показать баланс, предложить пополнение/подписку. |
| `byok_disabled` | Режим `byok` выбран, но BYOK выключен пользователем. | Включить BYOK в настройках. |
| `byok_invalid` | Ключ BYOK отсутствует или невалиден (`keyStatus=invalid`, либо ключ не задан при `mode=byok` и активной подписке — `byok=missing`). | Добавить/исправить ключ в настройках. |
| `rate_limited` | Транспортное превышение rate limit; всегда возвращается как HTTP `429` (gateway-concern). НЕ приходит как `status=blocked` body и НЕ входит в `/policy/effective.reasons[]`. Значение enum сохранено только для HTTP-слоя (см. [ADR-004](adr/ADR-004-blocked-http-200.md), BLK-7b в [09-e2e-testing.md](09-e2e-testing.md)). | Показать «слишком часто», предложить повторить позже. |
| `policy_denied` | Общий fallback для непредвиденного состояния Policy Engine. | Generic-сообщение «недоступно», лог/ретрай. |

### Дискриминация ответа `/chat/run` и `/chat/tool-result`

Ответ — одна модель `ChatResponse` (`src/app/schemas/chat.py`) с тремя взаимоисключающими состояниями по полю `status`. Документация обязана сделать варианты очевидными:
- `status=assistant_message`: присутствуют `assistantMessage`, `usage`, `messageStepId`, `stepId`; нет `toolCall`/`toolCalls`, `blockReason`.
- `status=tool_call`: присутствуют **`toolCalls[]`** (все client-side tool_use хода) и `toolCall` (= `toolCalls[0]`, deprecated, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), `usage`, `messageStepId`, `stepId`; нет `blockReason`. **`assistantMessage` — опционально присутствует**, если Claude выдал текст вместе с `tool_use` (текст того же assistant-шага `stepId`); `null`/опущено, если текста не было ([Q-024-1](99-open-questions.md) / [ADR-024](adr/ADR-024-history-payload-domain-normalization.md)).
- `status=blocked` (**policy**, `blockReason ≠ max_tokens`): присутствует `blockReason`; `messageStepId`/`stepId` = `null`; нет `assistantMessage`, `toolCall`/`toolCalls`, `usage`.
- `status=blocked` + **`blockReason=max_tokens`** (обрезка, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)): присутствуют `usage`, `messageStepId`, `stepId` (НЕ null), опционально `assistantMessage` (частичный текст); нет `toolCall`/`toolCalls` (обрезанные tool_use не отдаются). Кредит не списан.

`messageStepId`/`stepId` ([ADR-023](adr/ADR-023-sync-ids-in-chat-response.md), nullable) — идентификаторы синхронизации с историей чата: дословно совпадают с `steps[].messageStepId` / `steps[].id` из `GET /v1/chats/{id}` (модуль chats). В `description` поля `stepId` указать: «id шага этого ответа, совпадает со `steps[].id` в истории»; `messageStepId`: «ключ хода (стабилен через tool-loop), совпадает со `steps[].messageStepId`». При `blocked` оба `null` (шаг/ход не создаются — блок до генерации).

Способ (на усмотрение `backend`, любой даёт читаемый результат): либо описать инвариант в `description` модели + три именованных примера `openapi_examples` (`assistant_message`, `tool_call`, `blocked`), либо ввести discriminated-union response с тремя под-моделями. **Обязательный минимум** — три именованных примера ответа на каждый из двух chat-endpoint; примеры `assistant_message`/`tool_call` должны нести непустые `messageStepId`/`stepId`, пример `blocked` — `null`. Пример `tool_call` рекомендуется показать с непустым `assistantMessage` (Claude сказал текст + вызвал инструмент, [Q-024-1](99-open-questions.md)/[ADR-024](adr/ADR-024-history-payload-domain-normalization.md)); вариант без текста (`assistantMessage` отсутствует/`null`) — также валиден. Менять wire-формат существующих полей (имена/типы) запрещено; `messageStepId`/`stepId` — **аддитивные** nullable-поля (обратносовместимо).

## R4. Теги и группировка

Сгруппировать endpoint по модулям через `tags` + объявить порядок и русские описания в `openapi_tags`. Порядок тегов = порядок пользовательского сценария.

Колонка **Security** ниже отражает Hermes-интеграцию ([ADR-044](adr/ADR-044-client-api-key-auth.md)): клиентский контур — **`clientApiKey` + `userId`** (обе схемы, заголовки `X-API-Key` + `X-User-Id`); admin — `adminToken`; public — none. Запись `clientApiKey+userId` означает привязку обеих клиентских схем (R2.1). `bearerAuth` (JWT) — спящий, в таблице не фигурирует.

| Тег | Endpoint | Security | Описание тега (русский, кратко) |
|---|---|---|---|
| `Auth` | `POST /v1/auth/register`, `POST /v1/auth/token`, `POST /v1/auth/refresh`, `GET /v1/auth/jwks` | none | Получение и обновление токена доступа (спящий контур, [ADR-044](adr/ADR-044-client-api-key-auth.md)). Клиентом при Hermes-интеграции не используется; оставлен публичным для совместимости/тестов. |
| `Agent` | `POST /v1/agent/run`, `GET /v1/agent/runs/{runId}/events`, `POST /v1/agent/runs/{runId}/approval`, `POST /v1/agent/runs/{runId}/stop` | `clientApiKey+userId` | Автономный ИИ-агент пользователя (Hermes). Запуск прогона, поток событий (SSE), подтверждение инструмента, остановка. Headline-контур ([ADR-045](adr/ADR-045-hermes-as-agent-proxy.md)). |
| `Chat` | `POST /v1/chat/run`, `POST /v1/chat/tool-result` | `clientApiKey+userId` | Простой диалог с ассистентом и tool-loop (вызовы инструментов на устройстве). Опциональный контур рядом с `Agent`. |
| `Tools` | `GET /v1/tools` | `clientApiKey+userId` | Каталог инструментов, доступных в tool-loop. |
| `Models` | `GET /v1/models` | `clientApiKey+userId` | Каталог доступных моделей активного провайдера для селектора модели ([ADR-034](adr/ADR-034-user-model-selection.md)). |
| `Presets` | `GET /v1/presets` | `clientApiKey+userId` | Пресеты промтов для чипов на главном экране чата ([ADR-035](adr/ADR-035-prompt-presets-endpoint.md)). |
| `Policy` | `GET /v1/policy/effective` | `clientApiKey+userId` | Эффективные права пользователя для UI (можно ли генерировать и почему нет). |
| `Wallet` | `GET /v1/wallet`, `POST /v1/wallet/consume` | `clientApiKey+userId` | Баланс кредитов и списание. |
| `Tokens` | `POST /v1/tokens/purchase`, `GET /v1/tokens/products` | `clientApiKey+userId` | Покупка пакетов токенов и каталог продуктов. |
| `BYOK` | `POST /v1/byok/set`, `POST /v1/byok/toggle`, `POST /v1/byok/delete` | `clientApiKey+userId` | Свой ключ Anthropic: сохранение, включение, удаление. |
| `Admin` | `POST /v1/admin/credits/grant`, `POST /v1/admin/subscription/grant`, `GET /v1/admin/wallet/{userId}`, `POST /v1/admin/wallet/grant` (переходный алиас `credits/grant`) | `adminToken` | Операторские действия: начисление кредитов, ручная активация подписки, просмотр кошелька ([ADR-048](adr/ADR-048-admin-credits-and-subscription-grant.md)). Авторизация — `X-Admin-Token`. |
| `Preview` | `GET /v1/preview/{projectId}/{token}/{path}` | none | Публичная отдача сгенерированных сайтов по подписанной ссылке (авторизация в подписи, без JWT, [ADR-010](adr/ADR-010-backend-hosted-preview.md)). |
| `Chats` | `GET/PATCH/DELETE /v1/chats[/{id}]` (+ `/{id}/steps`) | `clientApiKey+userId` | История чатов: список, переименование, удаление, шаги. |
| `Profile` | `GET/PATCH /v1/profile` | `clientApiKey+userId` | Профиль пользователя. |
| `Preferences` | `GET/PATCH /v1/preferences` | `clientApiKey+userId` | Пользовательские настройки. |
| `Health` | `GET /health`, `GET /healthz`, `GET /ready`, `GET /metrics` | none | Служебные проверки и метрики (без auth). `/healthz` — алиас `/health` ([ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)). |

Каждый endpoint имеет ровно один тег из таблицы и security согласно колонке (R2). Порядок в `openapi_tags` = фактический порядок в `_OPENAPI_TAGS` (`src/app/main.py`): `Auth`, `Agent`, `Chat`, `Tools`, `Models`, `Presets`, `Policy`, `Wallet`, `Tokens`, `BYOK`, `Admin`, `Preview`, `Chats`, `Workspaces`, `Profile`, `Preferences`, `Health`. Тег `Agent` ставится перед `Chat` — он headline-контур пользовательского сценария ([ADR-045](adr/ADR-045-hermes-as-agent-proxy.md)). **Тег `Subscription` удалён** ([TD-021](100-known-tech-debt.md)/ревизия [ADR-029](adr/ADR-029-adapty-subscription-webhook.md)): после ретирования `POST /v1/subscription/sync` под ним нет роутов → пустую группу убираем из `_OPENAPI_TAGS` (синхронно code↔docs; подписки идут через Adapty-вебхук `POST /v1/billing/adapty/webhook`, который под своим контуром/тегом). needs_code_sync: `_OPENAPI_TAGS` в `src/app/main.py`.

> Прочие модули расширения (workspaces, snippets, attachments, notifications — см. [карту маршрутов](modules/api-gateway/02-api-contracts.md)) получают собственные теги по тому же принципу: клиентская auth (`clientApiKey`+`userId`, [ADR-044](adr/ADR-044-client-api-key-auth.md)), лаконичные тексты (R2ter), один тег на endpoint.

## R5. Примеры (request/response)

Для ключевых endpoint — осмысленные примеры на русском (значения-плейсхолдеры реалистичны: UUID-подобные id, осмысленный текст сообщения по-русски). Минимум:

| Endpoint | Обязательные примеры |
|---|---|
| `POST /v1/agent/run` | request (`message`, опц. `sessionId`, `model`); response `202` (`runId`, `status=queued`); response `blocked` (`status=blocked`, `blockReason` ∈ `credits_empty\|subscription_expired\|trial_used` — достижимый набор credits-ветки, HTTP 200 по [ADR-004](adr/ADR-004-blocked-http-200.md)). См. контракт [modules/agent-proxy/02-api-contracts.md](modules/agent-proxy/02-api-contracts.md). |
| `GET /v1/agent/runs/{runId}/events` | описание SSE-потока (passthrough Hermes): последовательность `run.queued` → `run.running` → `message.delta` → `tool.started`/`tool.completed` → (`approval.request`?) → `run.completed{usage}` либо `run.failed`. Формат событий — как у Hermes ([ADR-045](adr/ADR-045-hermes-as-agent-proxy.md)); пример одного `message.delta` и одного `run.completed{usage:{input_tokens,output_tokens,total_tokens}}`. |
| `POST /v1/agent/runs/{runId}/approval` | request (`choice` ∈ `once|session|always|deny`); response `202`/`200`. |
| `POST /v1/agent/runs/{runId}/stop` | request (пустое тело); response `202`/`200`. |
| `POST /v1/chat/run` | request (`mode=credits`); response `assistant_message`; response `tool_call` с `toolCalls[]` (≥1, напр. `files.read`); response `blocked` (`credits_empty`); response `blocked` (`max_tokens`, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) — с `usage`/`stepId`, без `toolCalls`). |
| `POST /v1/chat/tool-result` | request батч `results[]` (один и несколько результатов хода); request с `error` в элементе; response `assistant_message` (финал loop); response `tool_call` с оставшимися `toolCalls[]` (барьер хода не закрыт, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). |
| `POST /v1/wallet/consume` | request (`amount=1`, `requestId`); response (`newBalance`, `ledgerTxId`). |
| `POST /v1/byok/set` | request (`apiKey` — плейсхолдер, помечен «не логируется»); response `keyStatus=valid` и `keyStatus=invalid`. |
| `POST /v1/admin/credits/grant` | request (`userId`, `amount`, `idempotencyKey`); response (`newBalance`, `ledgerTxId`); response `404` (`user_not_found`). См. [modules/admin/02-api-contracts.md](modules/admin/02-api-contracts.md). |
| `POST /v1/admin/subscription/grant` | request (`userId`, период/план, опц. кредиты, `idempotencyKey`); response (статус подписки); response `404` (`user_not_found`). ([ADR-048](adr/ADR-048-admin-credits-and-subscription-grant.md)). |

Tool-loop сценарий описать связно (в `description` тега `Chat` или endpoint `/chat/run`): `run` → `tool_call` (`toolCalls[]`) → клиент исполняет **все** tool → `tool-result` (батч `results[]`) → `assistant_message`. Использовать согласованные id между примерами `toolCalls[].id` и `tool-result.results[].toolCallId`, чтобы сценарий читался end-to-end. **Parallel tool use ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** пример `tool_call` рекомендуется показать с ≥2 элементами `toolCalls[]` и соответствующим батч `tool-result` (барьер хода: backend продолжает только когда собраны все результаты). Поле `toolCall` (одиночное) — deprecated, помечать в `description` как «= `toolCalls[0]`, читайте `toolCalls`».

Запрещено в примерах: реальные секреты, реальные JWT, реальные ключи Anthropic, реальные StoreKit payload. Только очевидные плейсхолдеры. Для `apiKey`, `transaction`, `Authorization` — плейсхолдер + пометка о redaction (R7 [05-security.md](05-security.md)).

## R6. Метаданные API

В `create_app()` (`src/app/main.py`) при создании `FastAPI(...)` задать:

| Параметр | Значение |
|---|---|
| `title` | `claude-ios-backend` (без изменений). |
| `version` | текущая версия приложения (на момент фичи `0.1.0`; источник версии не меняется). |
| `description` | русский multiline-текст: назначение сервиса (backend-оркестратор Claude для iOS-приложения), кратко бизнес-правила доступа (trial → подписка/кредиты → BYOK), правило blocked=HTTP 200 (R3) с отсылкой к перечню `blockReason`, требование JWT (R2). Без раскрытия секретов и внутренних деталей реализации. |
| `contact` / `license` / `terms_of_service` | опционально, на усмотрение `backend`. Если заданы — без выдуманных URL/email; иначе не задавать. |

`description` должен дать интегратору контекст за один экран: что это, как авторизоваться, как читать `blocked`.

## R7. Доступность /docs в prod (env-флаг)

Документационные endpoint должны отключаться в production.

- Новая env-переменная **`DOCS_ENABLED`** (bool). Дефолт — `true` (удобно для dev/CI/staging).
- При `DOCS_ENABLED=false`: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` — `/docs`, `/redoc`, `/openapi.json` возвращают `404`.
- В prod значение задаётся через секрет-менеджер/env согласно [07-deployment.md](07-deployment.md#конфигурация-env). Рекомендация prod — `false` (схему API не раскрывать публично).
- Флаг добавляется в `Settings` (`src/app/config.py`, alias `DOCS_ENABLED`) рядом с прочими тумблерами; `create_app()` читает его при инициализации.
- Поведение зафиксировано как security-мера (снижение раскрытия API surface) — см. [05-security.md](05-security.md#документация-api-в-prod).

## R8. Читаемость (acceptance)

Документация считается «удобной для чтения и тестирования», если:
- Каждый endpoint имеет короткий `summary` (одна строка, ≤ ~60 символов) и лаконичный `description` (R2ter): что отправить, что вернётся, ключевые коды; без ADR/Q/TD-ссылок и без избыточных скобок-пояснений.
- В user-facing OpenAPI-текстах нет вхождений `ADR-`, `Q-`, `TD-` и расшифровок-аббревиатур в скобках (например `(Bring Your Own Key)`).
- Объявлены security schemes клиентского контура **`clientApiKey` + `userId`** (`X-API-Key` + `X-User-Id`, [ADR-044](adr/ADR-044-client-api-key-auth.md)) и `adminToken` (`X-Admin-Token`); каждый endpoint помечен корректно (R2). `bearerAuth` (JWT) — спящий, на клиентские операции не навешивается.
- У пользовательских `/v1/*` (включая `/v1/agent/*`) виден замок (обе клиентские схемы), у `/v1/admin/*` — замок `adminToken`, у `Auth`/`Health`/`preview` — замка нет.
- Тестировщик проходит флоу R2bis целиком в Swagger UI: Authorize(`clientApiKey`+`userId`) → защищённый вызов (в т.ч. `/v1/agent/run`); Authorize(adminToken) → admin-вызов (`/v1/admin/credits/grant`, `/v1/admin/subscription/grant`).
- Endpoint сгруппированы тегами в порядке сценария (R4); внутри Swagger UI читаются как разделы руководства.
- `blockReason` раскрыт (R3): интегратор понимает каждое значение без чтения исходников.
- В ключевых endpoint есть примеры request/response (R5), включая tool-loop и blocked.
- `description` API (R6) даёт стартовый контекст.

## Scope / Out-of-scope

**В scope:**
- `src/app/api_gateway/openapi_security.py` (или `custom_openapi()` в `src/app/main.py`): **объявить клиентские схемы `clientApiKey` (`X-API-Key`) + `userId` (`X-User-Id`) и схему `adminToken` (`X-Admin-Token`)** ([ADR-044](adr/ADR-044-client-api-key-auth.md), R2.1/R2.2). Пометить пользовательские `/v1/*` (включая `/v1/agent/*`) обеими клиентскими схемами; `/v1/admin/*` — `adminToken`; `/v1/auth/*`, `/v1/preview/*`, `Health` — без security (R2.3). `bearerAuth` (JWT) может оставаться объявленным, но спящим — на клиентские операции не навешивается.
- `src/app/api_gateway/routers/*.py` — **переписать `summary`/`description` во ВСЕХ роутерах** в лаконичный стиль (R2ter): убрать ADR/Q/TD-ссылки и избыточные скобки-пояснения. Прицельно: `token_purchase.py` (многословие token-purchase, убрать `(ADR-015)`), `byok.py` (убрать `(Bring Your Own Key)`). Проставить корректные `tags`/`security` (R4). Новые роутеры `agent` (`clientApiKey`+`userId`), `auth` (public), `GET /v1/tools` (`clientApiKey`+`userId`) — в том же стиле и security.
- `src/app/schemas/*.py` — `Field(description=...)`: лаконично, убрать ADR/Q/TD-ссылки и расшифровки-аббревиатуры в скобках.
- `src/app/main.py` — метаданные API (R6), теги (R4), docs-флаг (R7); `src/app/config.py` — `DOCS_ENABLED` (R7).
- `src/app/api_gateway/routers/health.py` — служебные без security.

**Out-of-scope:** изменение wire-формата (имена/типы полей, пути, методы, коды, состав security-механизмов) — запрещено, иначе ломается контракт [modules/api-gateway/02-api-contracts.md](modules/api-gateway/02-api-contracts.md). Меняются только **тексты** (`summary`/`description`/`Field.description`) и **объявление** security schemes в OpenAPI, не сама проверка auth, бизнес-логика, rate limit. Новые endpoint не вводятся; технические идентификаторы не переводятся.

## Открытые вопросы
Нет. Все решения зафиксированы выше; дефолты явны.
