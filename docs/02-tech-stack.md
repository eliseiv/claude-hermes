# 02 — Tech Stack

Единственное место, где фиксируются язык, библиотеки, версии и команды. Все остальные агенты берут команды отсюда. Если чего-то нет здесь — это блокер, не угадывай.

## Язык и runtime
| Компонент | Версия | Примечание |
|---|---|---|
| Python | **3.12.x** | целевой `3.12`, CI на `3.12` |
| FastAPI | **0.115.x** | ASGI framework |
| Uvicorn | **0.32.x** (`uvicorn[standard]`) | ASGI server (dev); в prod за Gunicorn |
| Gunicorn | **23.x** | process manager, worker class `uvicorn.workers.UvicornWorker` |
| Pydantic | **2.9.x** | v2, строгая валидация схем |
| pydantic-settings | **2.5.x** | конфиг из env |

## Данные
| Компонент | Версия | Примечание |
|---|---|---|
| PostgreSQL | **16.x** | единственное хранилище состояния |
| SQLAlchemy | **2.0.x** | async ORM (`AsyncSession`) |
| asyncpg | **0.29.x** | async драйвер PostgreSQL |
| Alembic | **1.13.x** | миграции |
| Redis | **7.x** | rate limiting, idempotency-метки, policy cache |
| redis-py | **5.x** | async client (`redis.asyncio`) |

## Внешние SDK / интеграции
| Компонент | Версия | Назначение |
|---|---|---|
| openai | **>=1.51,<2** (Python SDK; зарезолвлено **`1.109.1`** в `uv.lock`) | **OpenAI Chat Completions API** (function-calling + vision, non-streaming) — провайдер-абстракция LLM ([ADR-033](adr/ADR-033-llm-provider-abstraction.md)). Подключается на инстансах с `LLM_PROVIDER=openai`; на anthropic-инстансах (дефолт) не используется в рантайме, но зависимость в стеке общая (один код, не форк). Async-клиент `openai.AsyncOpenAI`; per-call key override (BYOK). PDF-вложения **поддержаны** ([ADR-041](adr/ADR-041-openai-native-pdf-attachment.md), закрывает [TD-023](100-known-tech-debt.md)): content-часть `file` (data-URI) — основной путь, `pypdf`-text-extraction — фолбэк. OpenAI Responses API — отложен ([Q-033-2](99-open-questions.md)). |
| anthropic | **0.39.x** (Python SDK) | Claude messages API + prompt caching. ⚠️ **Не типизирует `document`-блок** (есть `ImageBlockParam`, нет `DocumentBlockParam` — проверено эмпирически). Backend передаёт `messages` как сырые dict (`cast(Any, ...)`), поэтому PDF-`document`-блок проходит без отказа SDK; wire-совместимость для `claude-sonnet-4-5/4-6` подтверждается e2e ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)). Bump SDK для типобезопасности document-блока — [TD-016](100-known-tech-debt.md). |
| httpx | **0.27.x** | App Store Server API, прочие HTTP. **Hermes-интеграция ([ADR-045](adr/ADR-045-hermes-as-agent-proxy.md)):** `httpx.AsyncClient` (+ `.stream`) — прокси `POST /v1/runs` и SSE-ретрансляция `GET /v1/runs/{id}/events` к Hermes-инстансам. Новой зависимости не требует (httpx уже в стеке). |
| docker (docker-py) | **кандидат `>=7,<8`** (зарезолвить в `uv.lock` при реализации) | **Hermes-интеграция ([ADR-046](adr/ADR-046-per-user-hermes-runtime.md)):** Docker SDK для управления per-user Hermes-инстансами (`docker run/start/stop/remove/inspect`) в `src/app/hermes_runtime/docker_backend.py` (реализация `RuntimeBackend`). **Новая зависимость** — добавляется backend'ом при реализации модуля `hermes-runtime`; точный пин — в `pyproject.toml`/`uv.lock`. Требует доступа control plane к Docker socket ([07-deployment.md](07-deployment.md), [05-security.md](05-security.md)). Альтернативный путь (HTTP к Docker API напрямую через httpx) — резерв; на старте — docker-py. Расширяемый `RuntimeBackend` оставляет задел под Modal/Daytona SDK (post-MVP). |
| pyjwt | **2.9.x** | JWT verify (или App Store JWS) |
| cryptography | **43.x** | AES-GCM envelope encryption (data layer); StoreKit JWS x5c chain/signature verify |

> **Admin-auth и preview signed URL новых внешних зависимостей не требуют.** HMAC-SHA256 для preview signed URL и
> constant-time сравнение admin/preview-токенов — stdlib `hmac`/`hashlib`/`secrets` (Python 3.12). `cryptography` (уже в стеке)
> при необходимости. См. [ADR-009](adr/ADR-009-admin-token-auth.md), [ADR-010](adr/ADR-010-backend-hosted-preview.md).
| prometheus-client | **0.21.x** (`>=0.21,<0.22`) | Prometheus exposition для `GET /metrics` (observability cross-cutting) |
| pypdf | **5.x** (`>=5,<6`; зарезолвлено `5.9.0` в `uv.lock`) | **MVP ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)): guard числа страниц PDF** (анти-decompression-bomb) — только подсчёт страниц/структуры, без полного рендера (`src/app/chat/attachments.py::_check_pdf_pages`). PDF отдаётся Claude нативным `document`-блоком, текст НЕ извлекается (Claude разбирает сам). Pure-Python, без системных зависимостей. Фактически добавлен в `pyproject.toml`/`uv.lock` при реализации ADR-020 (2026-06-03); до этого числился в стеке только в docs (см. примечание ниже). (Извлечение `extracted_text` по [ADR-014](adr/ADR-014-multimodal-attachments.md) — отложено вместе с двухшаговой моделью, [TD-015](100-known-tech-debt.md).) |
| python-multipart | **0.0.x** (актуальная) | парсинг `multipart/form-data`. **Для MVP-вложений ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) НЕ требуется** (inline base64 в JSON, без multipart-upload). Остаётся в стеке (был добавлен под двухшаговый `POST /v1/attachments` [ADR-014](adr/ADR-014-multimodal-attachments.md) → [TD-015](100-known-tech-debt.md)); удаление — отдельное решение, безвредно. |

> **MVP-вложения (2026-06-03, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)):** мультимодальный ввод реализуется inline base64 в `/v1/chat/run` (base64 — stdlib; magic-bytes — stdlib-сигнатуры). Единственная требующаяся внешняя зависимость — `pypdf` (PDF page-guard). Важно: `pypdf` числился в стеке (в этом документе) уже с расширения Figma-gap 2026-06-02, но **физически в `pyproject.toml`/`uv.lock` отсутствовал** — реальная зависимость `pypdf>=5,<6` добавлена backend'ом именно при реализации ADR-020 (2026-06-03). Пин в коде совпадает с задокументированным. Прочих новых внешних зависимостей фича не добавляет. Новые config-настройки (env, конфигурируемы, [Q-020-2](99-open-questions.md)): `ATTACHMENT_MAX_COUNT` (дефолт 10), `ATTACHMENT_MAX_BYTES_IMAGE` (5 MB) и `ATTACHMENT_MAX_BYTES_DOCUMENT` (8 MB — отдельные env-ключи в коде, `src/app/config.py`), `ATTACHMENT_TOTAL_BYTES` (10 MB), `ATTACHMENT_PDF_MAX_PAGES` (100), `ATTACHMENT_REQUEST_BODY_LIMIT` (12 MB — повышенный transport-лимит только роута `/v1/chat/run`). Allowlist `mediaType` — фиксирован в коде ([Q-020-1](99-open-questions.md) — расширение).

> **Расширение Figma-gap (2026-06-02):** в стек (на уровне этого документа) были внесены `pypdf` (тогда — под извлечение `extracted_text` из PDF, модуль attachments) и `python-multipart` (multipart-upload вложений). Уточнение (2026-06-03): на тот момент это была только docs-фиксация намерения — `pypdf` физически в `pyproject.toml` отсутствовал и был реально добавлен лишь при реализации [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md) (с назначением «guard числа страниц», а не извлечение текста — извлечение отложено в [TD-015](100-known-tech-debt.md)). Прочие новые модули (chats/profile/preferences/workspaces/snippets/token-purchase/notifications) **новых внешних зависимостей не требуют** — используют существующий стек (FastAPI/SQLAlchemy/Pydantic, stdlib `hmac`/`hashlib`/`secrets` для accountId-производной). Определение media_type вложений по magic bytes — stdlib (`mimetypes`/собственная сигнатурная проверка), без новой зависимости. APNs-отправка push отложена ([TD-011](100-known-tech-debt.md)) — APNs-зависимость добавится при её реализации, не в этом проходе.

> **Adapty subscription webhook ([ADR-029](adr/ADR-029-adapty-subscription-webhook.md)) — новых внешних зависимостей не требует.** Bearer constant-time compare — stdlib `hmac.compare_digest` (как admin/preview). Дефенсивный парсинг тела — stdlib `json` (сырое тело `request.body()`, без Pydantic-валидации тела — иначе `422` на проверочный пинг Adapty). UUID-разбор `customer_user_id` — stdlib `uuid`. ISO8601 `expires_at` — stdlib `datetime`. Идемпотентность — PostgreSQL `ON CONFLICT` (уже в стеке). Новые config-настройки (env, `src/app/config.py`): `ADAPTY_WEBHOOK_SECRET` (секрет, дефолт пуст → эндпоинт `500`), `ADAPTY_PRODUCT_TOKENS` (JSON `vendor_product_id→tokens`, дефолт `{}`, хелпер `adapty_product_tokens()` по образцу `token_products()`), `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (int, дефолт `1000`, fallback-грант). Точные значения/назначение — [07-deployment.md §Конфигурация (env)](07-deployment.md#конфигурация-env), [modules/billing-adapty/07-implementation-phases.md](modules/billing-adapty/07-implementation-phases.md).

> **Дата/время — stdlib `datetime` + `zoneinfo` (инструмент `time.now`, [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md)).** UTC-набор результата (`utc`/`unix`/`weekday`) — чистый stdlib `datetime` от `datetime.UTC`, без внешних зависимостей. Локальное время по IANA-зоне (`tz` → `local`/`timezone`) — stdlib `zoneinfo` (`ZoneInfo`), которому нужна **база таймзон**. Базовый образ `python:3.12-slim-bookworm` ([Dockerfile](Dockerfile)) — slim; системная tz-база может отсутствовать. Пакет **`tzdata`** добавлен в зависимости проекта (`pyproject.toml` — `tzdata>=2024.1`; `uv.lock` — `tzdata 2026.2`; pure-Python, предпочтительно для slim, без правки Dockerfile) → в prod-образе tz-база гарантирована, `ZoneInfo("Europe/Moscow")` резолвится, `tz` работает — **[TD-019](100-known-tech-debt.md) Resolved (2026-06-10, вариант A)**. UTC-набор доступен всегда независимо; невалидная/мусорная зона штатно деградирует к tool-result error `invalid_timezone`. Время в `time.now` берётся через инъектируемый `Clock`-провайдер (детерминизм qa, [ADR-026 §8](adr/ADR-026-global-server-side-tools-and-time-now.md)), не прямой `datetime.now()`.

> **Hermes-интеграция (control plane для автономного агента, [ADR-044](adr/ADR-044-client-api-key-auth.md)–[ADR-048](adr/ADR-048-admin-credits-and-subscription-grant.md)).** Новые внешние зависимости: **docker (docker-py)** — управление per-user инстансами ([ADR-046](adr/ADR-046-per-user-hermes-runtime.md)); **httpx** (уже в стеке) — прокси/SSE к Hermes ([ADR-045](adr/ADR-045-hermes-as-agent-proxy.md)). Шифрование `API_SERVER_KEY` — существующий `byok.kms` (новой крипто-зависимости нет, [ADR-003](adr/ADR-003-byok-envelope-encryption.md)). Constant-time сравнение `X-API-Key` — stdlib `hmac.compare_digest`. Новые config-настройки (env, `src/app/config.py`; точные значения/дефолты — [07-deployment.md §Конфигурация (env)](07-deployment.md#конфигурация-env)):
> - `CLIENT_API_KEY` (секрет, под redaction; клиентская auth [ADR-044](adr/ADR-044-client-api-key-auth.md)) + опц. `CLIENT_API_KEY_PREV` (ротация).
> - `HERMES_IMAGE` (Docker-образ Hermes + тег), `HERMES_DOCKER_NETWORK` (выделенная docker-сеть control plane↔инстансы), `HERMES_VOLUME_ROOT` (корень томов `HERMES_HOME`), `HERMES_DEFAULT_TOOLSET` (безопасный набор инструментов для `config.yaml`), `HERMES_IDLE_TIMEOUT_SECONDS` (порог гибернации), `HERMES_MODEL` («голое» имя модели инстанса → `config.yaml model.default`, env `LLM_MODEL` образ игнорирует — [ADR-055](adr/ADR-055-hermes-instance-llm-config-contract.md)), `HERMES_LLM_PROVIDER` (→ `config.yaml model.provider`, валидный провайдер allowlist образа, НЕ `openai`/`auto`) + соответствующий ключ провайдера (сервисный LLM-ключ инстанса; секрет), опц. `HERMES_LLM_BASE_URL` (`custom`/`azure-foundry`).
> - `CREDITS_PER_1K_INPUT` / `CREDITS_PER_1K_OUTPUT` (конвертация usage→кредиты для `/v1/agent/*`, [ADR-047](adr/ADR-047-usage-based-billing-for-agent.md)).
> Прим.: `HERMES_LLM_PROVIDER`/`HERMES_MODEL` — это провайдер/модель **внутри Hermes-инстанса** (его собственный LLM, [ADR-046](adr/ADR-046-per-user-hermes-runtime.md)); они **независимы** от нашего `LLM_PROVIDER`/`LLMClient` ([ADR-033](adr/ADR-033-llm-provider-abstraction.md), `/v1/chat/*`). `DOCS_ENABLED` (Swagger) — без изменений.

> **Провайдер-абстракция LLM ([ADR-033](adr/ADR-033-llm-provider-abstraction.md)).** LLM-клиент выбирается env **`LLM_PROVIDER ∈ {anthropic, openai}`, дефолт `anthropic`** (существующие инстансы `claude-ios`/`avelyra` поведение не меняют). Нейтральный интерфейс `LLMClient` (`src/app/chat/llm_client.py`) — контракт `create_message`/`validate_key`; `AnthropicClient` (как есть) и новый `OpenAIClient` — его реализации; factory `get_llm_client()`. **Один провайдер на инстанс** — БД хранит wire-формат своего провайдера (кросс-провайдерный реплей в одной БД не требуется). Нормализованный внутренний `stop_reason ∈ {tool_use, max_tokens, end_turn}` (каждый клиент мапит свой wire: OpenAI `tool_calls→tool_use`, `length→max_tokens`, `stop→end_turn`). Провайдер-специфичная (де)сериализация — **внутри клиента**; orchestrator/персист провайдер-агностичны. Контракт границы, маппинги имён tools, attachments — [chat-orchestrator/03-architecture.md §Провайдер-абстракция LLM](modules/chat-orchestrator/03-architecture.md#провайдер-абстракция-llm-anthropic--openai-adr-033). Config-имена (`OPENAI_*`) — [07-deployment.md §Конфигурация (env)](07-deployment.md#конфигурация-env).

### Модели Claude (значения по умолчанию)
- Оркестрация: `claude-sonnet-4-5` (значение конфигурируемо через env `ANTHROPIC_MODEL`).
- Prompt caching: включён через `cache_control` на системном промте и стабильном контексте.
- Точную модель/версию pin фиксирует Chat Orchestrator config; пользователь BYOK использует ту же модель, но со своим ключом.
- **`ANTHROPIC_MAX_TOKENS` (output budget на вызов, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** дефолт **`16000`** (`src/app/config.py`). Прежний дефолт `4096` был мал для генерации кода/файлов (несколько `files.write` с полным содержимым) → ответ обрезался (`stop_reason="max_tokens"`). `16000` покрывает типовой генеративный ход; вызов остаётся **non-streaming** (`create_message`) — `16000` ниже порога SDK non-streaming-гарда. **Per-instance** в `.env`-контракте (применяется к каждому инстансу мульти-инстанс-деплоя, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)). Обрезку по `max_tokens` оркестратор обрабатывает явно (`status=blocked`, `blockReason=max_tokens`, кредит не списывается) — [chat-orchestrator/03-architecture.md §Обработка обрезки](modules/chat-orchestrator/03-architecture.md#обработка-обрезки-по-max_tokens-adr-025). Переход на streaming при дальнейшем росте `max_tokens` — [TD-018](100-known-tech-debt.md).
- **`ANTHROPIC_TIMEOUT_SECONDS`:** дефолт поднят до **`120`** (с 60) — страховка от ложного `502` по таймауту на длинной non-streaming-генерации при `max_tokens=16000` ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). Конфигурируемо, существенно ниже SDK non-streaming-гарда.
- `ANTHROPIC_MAX_RETRIES` (дефолт `2`) — без изменений.

### Модель OpenAI (инстансы `LLM_PROVIDER=openai`, [ADR-033](adr/ADR-033-llm-provider-abstraction.md))
- Оркестрация: **`gpt-4o`** (env `OPENAI_MODEL`). Chat Completions API, non-streaming (паритет с anthropic-путём).
- `OPENAI_MAX_TOKENS` (output-бюджет, дефолт `16000` — паритет), `OPENAI_TIMEOUT_SECONDS` (дефолт `120`), `OPENAI_MAX_RETRIES` (дефолт `2`).
  - **Параметр Chat Completions:** `OPENAI_MAX_TOKENS` передаётся в Chat Completions как `max_tokens` — валидный параметр для `gpt-4o` (MVP-модель). При переходе на reasoning-модели (семейство `o*`/новые модели, требующие `max_completion_tokens`) этот параметр в API называется `max_completion_tokens`; для текущей MVP-модели `gpt-4o` `max_tokens` корректен. Смена — отдельным решением при выборе reasoning-модели.
- BYOK активная модель OpenAI: `OPENAI_BYOK_DEFAULT_MODEL` (дефолт `gpt-4o`) — отдельно от anthropic `BYOK_DEFAULT_MODEL`.
- Кэширование: `cache_control` не применяется (Anthropic-only); у OpenAI авто-кэш промпт-префикса, спец-логики в коде нет. `usage.cache_read_tokens` берётся из `prompt_tokens_details.cached_tokens` (если есть), `cache_write_tokens` = 0.

## Инструменты разработки
| Инструмент | Версия | Роль |
|---|---|---|
| uv | **0.4.30** (CI pin) | менеджер зависимостей и venv (`uv.lock`). CI запинен на `0.4.30` (`.github/workflows/ci.yml`, `UV_VERSION`); локальное dev-окружение может быть новее (наблюдалось `0.11.6`) — `uv.lock` обеспечивает идентичность зависимостей независимо от версии uv. Каноничен CI-pin `0.4.30`. |
| Ruff | **0.7.x** | linter + formatter |
| mypy | **1.11.x** | статическая типизация (strict для бизнес-логики) |
| pytest | **8.x** | тесты |
| pytest-asyncio | **0.24.x** | async тесты |
| pytest-cov | **5.x** | coverage |
| testcontainers | **4.x** | реальный PostgreSQL/Redis в интеграционных тестах |
| respx | **0.21.x** | мок HTTP (Anthropic/Apple) в unit-тестах |

## Команды (канонические — использовать именно их)
> Запускаются из корня репозитория. Менеджер — `uv`.

```bash
# install deps
uv sync

# format (пишет изменения)
uv run ruff format .

# format check (CI, без записи)
uv run ruff format --check .

# lint
uv run ruff check .

# lint с автофиксом
uv run ruff check --fix .

# type-check
uv run mypy src

# тесты + покрытие
# Coverage core = sys.monitoring (sysmon), НЕ legacy C-tracer: [tool.coverage.run] core="sysmon"
# в pyproject.toml + COVERAGE_CORE=sysmon в Makefile/CI. Windows-обязательно: legacy settrace
# per-thread на нативных C-frames (cryptography/asyncpg/pydantic-core в asyncio.to_thread worker'ах)
# давал access-violation segfault; concurrency=["thread"] намеренно удалён (sysmon покрывает
# to_thread/корутины без него). Детали — [06-testing-strategy.md §Coverage measurement core](06-testing-strategy.md).
uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=80

# миграции
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "<message>"

# запуск приложения (dev)
uv run uvicorn app.main:app --reload

# запуск (prod)
uv run gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4
```

## Структура проекта (фактическая)
```
src/app/
  main.py                 # FastAPI app factory: middleware, routers, exception handlers
  config.py               # pydantic-settings (Settings, get_settings)
  db.py                   # async engine, sessionmaker, dispose_engine
  deps.py                 # FastAPI dependencies (auth/JWT, db session, correlation id)
  errors.py               # AppError иерархия (ValidationFailedError и пр.)
  api_gateway/
    auth.py               # JWT (RS256) verify, JWKS; require_admin (X-Admin-Token, изолирован от get_current_user — ADR-009)
    middleware.py         # CorrelationId, SecurityHeaders, SizeLimit
    rate_limit.py         # Redis rate limiting, redis_ping, close_redis
    routers/              # chat, policy, wallet, subscription, byok, health, admin (/v1/admin/*), preview (/v1/preview/*)
  chat/                   # orchestrator, anthropic_client, repository, tools (client-side files.*/calendar.*/reminders.* + server-side site.* dispatch)
  admin/                  # service-обёртка над Wallet.grant/get_wallet_view (ADR-009)
  website/                # projects/site_files service, signed URL (HMAC), site.* tool-хэндлеры (server-side, ADR-010/011)
  policy/                 # engine (pure), loader (state из репозиториев)
  wallet/                 # service (ledger, consume, grant)
  subscription/           # service, storekit (JWS x5c chain/signature verify)
  byok/                   # service (envelope encrypt/toggle/delete), kms (KmsClient + LocalKmsClient)
  audit/                  # service (append-only logging)
  observability/          # metrics (prometheus), logging, context (request id), redaction
  models/                 # base, tables. На MVP активны 14 таблиц (миграции 0001=9 базовых, 0003=projects/site_files, 0004=user_preferences, 0005=auth_devices/auth_refresh_tokens). Поставка 3 (миграция 0011, ADR-036): workspace_projects + workspace_files (BYTEA, самодостаточно, НЕ через attachments) + chat_sessions.workspace_project_id. Спроектированы, миграцией ещё не созданы: snippets/device_push_tokens; ОТЛОЖЕНЫ: attachments (TD-015, ADR-020). Полный статус — 03-data-model.md.
  schemas/                # Pydantic request/response (chat, policy, wallet, subscription, byok, common)
  # Расширение Figma-gap (новые пакеты — каждый со своим router/service/repository):
  chats/                  # CRUD/список/поиск/steps-view поверх chat_sessions (модуль chats)
  profile/                # displayName + производный accountId
  preferences/            # default_assistant_mode, notif toggle, code defaults
  workspaces/             # рабочие пространства чатов (≠ website projects, ADR-013)
  snippets/               # сохранённые код-фрагменты
  attachments/            # двухшаговый upload/extract_text/таблица — ОТЛОЖЕН (TD-015, transport ADR-014 Superseded); MVP — inline base64 в /chat/run (ADR-020, реализует chat-orchestrator)
  token_purchase/         # consumable IAP → grant кредитов (reuse storekit verifier + Wallet, ADR-015)
  notifications/          # device push-token CRUD (отправка push → TD-011)
  # Hermes-интеграция (control plane для автономного агента, ADR-044..048):
  hermes_runtime/         # HermesInstanceManager + docker_backend (RuntimeBackend) + registry (таблица hermes_instances), reaper (ADR-046)
  #   api_gateway/routers/agent.py — контур /v1/agent/* (прокси к Hermes /v1/runs + SSE), schemas/agent.py (ADR-045)
  #   auth: verify_client_api_key (X-API-Key) + get_current_user из X-User-Id (ADR-044); admin: credits/grant + subscription/grant (ADR-048)
migrations/               # alembic
tests/
  unit/  integration/  e2e/
pyproject.toml  uv.lock
```

> Служебные маршруты `/health`, `/healthz` (алиас `/health`, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)), `/ready`, `/metrics` — в `api_gateway/routers/health.py` (см. [07-deployment.md](07-deployment.md#health--readiness)).

## Соглашения по коду
- Форматирование и линт — Ruff (line length 100). Конфиг в `pyproject.toml`.
- mypy `strict = true` для пакетов `policy`, `wallet`, `byok`, `chat`. Остальное — `disallow_untyped_defs`.
- Pydantic v2 модели для всех request/response; запрет «голых» dict на границе API.
- Async везде, где есть I/O (БД, Redis, HTTP).
- Идемпотентность через ключи в Redis + уникальные индексы в PostgreSQL (не полагаться только на одно из двух).
