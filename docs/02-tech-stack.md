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
| anthropic | **0.39.x** (Python SDK) | Claude messages API + prompt caching. ⚠️ **Не типизирует `document`-блок** (есть `ImageBlockParam`, нет `DocumentBlockParam` — проверено эмпирически). Backend передаёт `messages` как сырые dict (`cast(Any, ...)`), поэтому PDF-`document`-блок проходит без отказа SDK; wire-совместимость для `claude-sonnet-4-5/4-6` подтверждается e2e ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)). Bump SDK для типобезопасности document-блока — [TD-016](100-known-tech-debt.md). |
| httpx | **0.27.x** | App Store Server API, прочие HTTP |
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

> **Дата/время — stdlib `datetime` + `zoneinfo` (инструмент `time.now`, [ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md)).** UTC-набор результата (`utc`/`unix`/`weekday`) — чистый stdlib `datetime` от `datetime.UTC`, без внешних зависимостей. Локальное время по IANA-зоне (`tz` → `local`/`timezone`) — stdlib `zoneinfo` (`ZoneInfo`), которому нужна **база таймзон**. Базовый образ `python:3.12-slim-bookworm` ([Dockerfile](Dockerfile)) — slim; системная tz-база может отсутствовать. Пакет **`tzdata`** добавлен в зависимости проекта (`pyproject.toml` — `tzdata>=2024.1`; `uv.lock` — `tzdata 2026.2`; pure-Python, предпочтительно для slim, без правки Dockerfile) → в prod-образе tz-база гарантирована, `ZoneInfo("Europe/Moscow")` резолвится, `tz` работает — **[TD-019](100-known-tech-debt.md) Resolved (2026-06-10, вариант A)**. UTC-набор доступен всегда независимо; невалидная/мусорная зона штатно деградирует к tool-result error `invalid_timezone`. Время в `time.now` берётся через инъектируемый `Clock`-провайдер (детерминизм qa, [ADR-026 §8](adr/ADR-026-global-server-side-tools-and-time-now.md)), не прямой `datetime.now()`.

### Модели Claude (значения по умолчанию)
- Оркестрация: `claude-sonnet-4-5` (значение конфигурируемо через env `ANTHROPIC_MODEL`).
- Prompt caching: включён через `cache_control` на системном промте и стабильном контексте.
- Точную модель/версию pin фиксирует Chat Orchestrator config; пользователь BYOK использует ту же модель, но со своим ключом.
- **`ANTHROPIC_MAX_TOKENS` (output budget на вызов, [ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** дефолт **`16000`** (`src/app/config.py`). Прежний дефолт `4096` был мал для генерации кода/файлов (несколько `files.write` с полным содержимым) → ответ обрезался (`stop_reason="max_tokens"`). `16000` покрывает типовой генеративный ход; вызов остаётся **non-streaming** (`create_message`) — `16000` ниже порога SDK non-streaming-гарда. **Per-instance** в `.env`-контракте (применяется к каждому инстансу мульти-инстанс-деплоя, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)). Обрезку по `max_tokens` оркестратор обрабатывает явно (`status=blocked`, `blockReason=max_tokens`, кредит не списывается) — [chat-orchestrator/03-architecture.md §Обработка обрезки](modules/chat-orchestrator/03-architecture.md#обработка-обрезки-по-max_tokens-adr-025). Переход на streaming при дальнейшем росте `max_tokens` — [TD-018](100-known-tech-debt.md).
- **`ANTHROPIC_TIMEOUT_SECONDS`:** дефолт поднят до **`120`** (с 60) — страховка от ложного `502` по таймауту на длинной non-streaming-генерации при `max_tokens=16000` ([ADR-025](adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)). Конфигурируемо, существенно ниже SDK non-streaming-гарда.
- `ANTHROPIC_MAX_RETRIES` (дефолт `2`) — без изменений.

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
  models/                 # base, tables. На MVP активны 14 таблиц (миграции 0001=9 базовых, 0003=projects/site_files, 0004=user_preferences, 0005=auth_devices/auth_refresh_tokens). Спроектированы, миграцией ещё не созданы: workspace_projects/snippets/device_push_tokens (Спринт 2/3) + ОТЛОЖЕНЫ workspace_files/attachments (TD-015, ADR-020). Полный статус — 03-data-model.md.
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
