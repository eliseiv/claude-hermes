# 05 — Security

## Аутентификация
- Все endpoint (кроме health/metrics, `/v1/auth/*`, `/v1/preview/*`) требуют **JWT Bearer** в заголовке `Authorization`.
- Алгоритм подписи: **RS256** (асимметрия; публичный ключ в сервисе для verify, приватный — секрет для подписи).
- Claims (минимум): `sub` = userId (UUID), `exp`, `iat`, `device_id`, `iss`, `aud`; заголовок `kid`.
- Проверка: подпись, `exp`, `iss`, `aud`. Просроченный/невалидный → `401`.
- `userId` в теле запроса должен совпадать с `sub` токена; иначе `403` (запрет действий за другого пользователя).
- **Issuer — встроенный в backend ([ADR-018](adr/ADR-018-embedded-auth-issuer.md), закрывает [Q-005-1](99-open-questions.md)):** backend САМ выпускает токены (`/v1/auth/*`, модуль [auth](modules/auth/README.md)) и САМ их верифицирует существующим `JwtVerifier` — self-consistent (один config-набор ключей; `iss=https://broadnova.shop`, `aud=claude-ios`). Первичная аутентификация — **device-based** (анонимная: `deviceId` → `userId`); email/пароль и Apple Sign-In — опциональное расширение, не MVP ([Q-018-2](99-open-questions.md)). Verify-only режим внешнего issuer (`JWT_JWKS_URL`) сохраняется как опция для будущего апгрейда. **Приватный ключ подписи должен быть сконфигурирован до публичного запуска (must-configure-before-launch, [07-deployment.md prod-checklist](07-deployment.md#prod-readiness-checklist-must-configure-before-launch));** без него issuer-эндпоинты отдают `503`.

### Выпуск токенов (встроенный issuer, [ADR-018](adr/ADR-018-embedded-auth-issuer.md))
- `/v1/auth/register|token|refresh|jwks` — **без** пользовательского JWT (точка его получения); защита — per-IP rate-limit (`AUTH_RATE_LIMIT_PER_IP`, дефолт 10/min).
- Access-token: RS256 JWT, TTL 1ч (`AUTH_ACCESS_TTL_SECONDS`). Refresh-token: opaque, TTL 30д (`AUTH_REFRESH_TTL_SECONDS`), хранится как `sha256`-хэш, single-use rotation + reuse-детект → ревокация цепочки.
- `register` создаёт `users` явно (eager provisioning); lazy-provisioning ([ADR-007](adr/ADR-007-lazy-user-provisioning.md)) остаётся fallback; `trial_used`/policy не затронуты. Детали — [modules/auth/05-security.md](modules/auth/05-security.md).

## Модель идентичности и провижининг пользователей
См. [ADR-007](adr/ADR-007-lazy-user-provisioning.md).
- **Источник истины идентичности — доверенный JWT issuer.** `users.id` ≡ JWT `sub` (UUID, выдаёт issuer). Endpoint регистрации отсутствует и не предусмотрен.
- **Ленивый провижининг (lazy provisioning).** Строка `users` создаётся при первом аутентифицированном запросе — централизованно в API Gateway (`get_current_user`), **после** успешной верификации JWT и **до** любой FK-зависимой операции. Идемпотентный атомарный upsert: `INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING` (race-free: `DO NOTHING` атомарен в PostgreSQL).
- Поддельный/невалидный `sub` строку не создаёт: провижининг идёт только после прохождения JWT-проверки (иначе `401` раньше). Сверка `userId` тела с `sub` (`403`) сохраняется.
- Все FK-зависимые таблицы (`subscriptions`, `wallets`, `byok_keys`, `ledger_transactions`, `chat_sessions`/`chat_steps`/`tool_calls`, `audit_logs`) гарантированно имеют родительскую строку `users` к моменту вставки.

## Авторизация (RBAC)
Пользовательская роль — `user` (владелец своих ресурсов): каждый пользовательский запрос ограничен ресурсами `sub`.
Дополнительно — **изолированный admin-принципал** (`admin`) для операторских действий, см. ниже. Детали per-module — в `modules/<M>/06-rbac.md`.

## Admin-авторизация (изолированная, ADR-009)
См. [ADR-009](adr/ADR-009-admin-token-auth.md), [modules/admin](modules/admin/README.md).
- Admin-API (`/v1/admin/*`) авторизуется **отдельным секретом** `ADMIN_API_SECRET` через заголовок **`X-Admin-Token`**,
  зависимость `require_admin` — **полностью отдельная** от пользовательской `get_current_user`.
- **Изоляция от пользовательской auth:** разные секреты, заголовки, зависимости. Пользовательский JWT **не** авторизует
  admin-действия; admin-токен **не** даёт доступа к пользовательским ресурсам через пользовательские эндпоинты.
  Эскалация невозможна by construction. Роли `admin` в пользовательском JWT **нет**.
- `require_admin` **не** запускает lazy-provisioning ([ADR-007](adr/ADR-007-lazy-user-provisioning.md)), **не** трогает
  `users.trial_used`, **не** создаёт строку `users` для actor (admin — не пользователь системы).
- Сравнение токена — **constant-time** (`hmac.compare_digest`). Несовпадение/отсутствие → `401`.
- **Ротация:** два активных секрета на grace-период — `ADMIN_API_SECRET` (основной) + опц. `ADMIN_API_SECRET_PREV`.
- Защита admin-API: отдельный rate limit (дефолт 10 req/min per source IP), `extra='forbid'`, тело ≤ 8 KB.
- `X-Admin-Token` — в redaction allowlist (никогда не логируется).

## Секреты и ключи
- Сервисный Anthropic API key, KMS credentials, **JWT signing keys (приватный RS256-ключ — секрет; публичный — для verify, не секрет)**, App Store credentials, **`ADMIN_API_SECRET`** (+ опц. `ADMIN_API_SECRET_PREV`), **`PREVIEW_URL_SECRET`** — только через **env / secret manager**, никогда в коде/репозитории/образе.
- Все перечисленные секреты **взаимно не пересекаются** (отдельные значения): JWT signing key, KMS, Anthropic, `ADMIN_API_SECRET`, `PREVIEW_URL_SECRET` — независимы; компрометация одного не даёт доступа к домену другого.
- `.env` в `.gitignore`; в prod — секрет-менеджер (конкретный — [Q-002-1](99-open-questions.md), дефолт: облачный KMS + Secrets Manager того же провайдера).
- Запрет логировать любые секреты, BYOK plaintext, JWT (выпущенный access-token), refresh-token, приватный ключ подписи, StoreKit payload целиком.

### JWT-ключи: PEM-в-env (встроенный issuer, [ADR-018](adr/ADR-018-embedded-auth-issuer.md))
Многострочный PEM плохо переносится через `.env`. Поддержаны **оба** механизма, приоритет у файла-пути:
| Переменная | Назначение | Приоритет |
|---|---|---|
| `JWT_PRIVATE_KEY_PATH` / `JWT_PUBLIC_KEY_PATH` | путь к PEM-файлу (prod-рекомендация: mount секрета) | выше |
| `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` | PEM-строка в env с **`\n`-экранированием** (литералы `\n` → переводы строк при загрузке) | ниже |
- Резолв: `*_PATH` (read file) > строковое значение (разэкранирование `\\n`→`\n`). `JWT_PUBLIC_KEY` уже существовал; добавляются `JWT_PRIVATE_KEY`, `*_PATH`-варианты, `JWT_KID`.
- Приватный ключ — **секрет** (redaction, не в образе); публичный — для verify и `GET /v1/auth/jwks`. Issuer и `JwtVerifier` берут пару из одного config (self-consistent). Нет приватного → issuer-эндпоинты `503`, verify-only режим продолжает работать.

## BYOK — шифрование at-rest (envelope encryption)
См. [ADR-003](adr/ADR-003-byok-envelope-encryption.md).
1. На `POST /v1/byok/set`: генерируется случайный **DEK** (32 байта).
2. Пользовательский ключ шифруется **AES-256-GCM** с DEK → `encrypted_key` + `nonce`.
3. DEK шифруется через **KMS** (`Encrypt` под master key) → `encrypted_dek`.
4. В БД хранятся `encrypted_key`, `encrypted_dek`, `nonce`. Plaintext ключ и plaintext DEK — никогда.
5. На использование: KMS `Decrypt(encrypted_dek)` → DEK in-memory → расшифровка ключа → передача только Chat Orchestrator на время вызова. После — обнуление из памяти.
6. Валидация ключа при `set` (лёгкий вызов Anthropic) → `key_status = valid|invalid`.
- **На MVP** (решение пользователя 2026-06-02): используется `LocalKmsClient` — реальный AES-256-GCM wrap DEK под мастер-ключом `KMS_LOCAL_MASTER_KEY` (через secret manager/`.env` на сервере). Это рабочая envelope-схема, не заглушка. Облачный KMS-провайдер ([Q-002-1](99-open-questions.md)) — **post-MVP**: подключается в тот же интерфейс `KmsClient` без изменения контрактов. Q-002-1 отвязан от deploy-target ([TD-005](100-known-tech-debt.md) закрыт независимо).

## Защита от abuse / rate limiting
- Ограничения per **user**, per **device_id**, per **IP**. Реализация — Redis (sliding window / token bucket).

### Доверенный reverse-proxy и определение client IP (anti-spoofing)
Приложение работает за reverse-proxy / LB (TLS termination). В prod-топологии ([ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)) это **внешний Traefik** (`/opt/edge`), который проставляет `X-Forwarded-For` с реальным клиентским IP. Заголовок `X-Forwarded-For` (XFF) клиентом подделываем, поэтому доверять ему можно только при контролируемой цепочке прокси:
- `TRUSTED_PROXY_IPS` (env, comma-separated IP/CIDR) задаёт доверенные source-range прокси. Дефолт `""` → XFF **не доверяется**, per-IP лимит использует socket peer IP (безопасный дефолт для развёртывания без прокси).
- **Правило prod:** `TRUSTED_PROXY_IPS` **ОБЯЗАН** указывать source-range прокси — в текущей схеме **подсеть docker-сети `web`**, через которую Traefik проксирует на `api` (`docker network inspect web` → `IPAM.Config.Subnet`, обычно bridge `172.x.0.0/16`). Иначе `client_ip` определяется как IP **Traefik** (один и тот же для всех клиентов) — per-IP rate limit фактически не работает: легко вызвать ложные общие `429` или, наоборот, потерять защиту по IP. См. [07-deployment.md](07-deployment.md#биндинг-и-доступ).
- Anti-spoofing: client IP определяется как **rightmost-non-trusted hop** в XFF (берётся `(TRUSTED_PROXY_HOP_COUNT + 1)`-я запись справа), что отсекает значения, инжектированные клиентом слева от доверенных прокси.
- При превышении → HTTP `429` (стандартный error-формат с `code=rate_limited`). `rate_limited` — **gateway-concern**: он НЕ отражается в `/policy/effective.reasons[]` (policy engine не знает rate-limit состояния, BLK-7b). `rate_limited` остаётся значением blockReason enum для HTTP-слоя — см. [ADR-004](adr/ADR-004-blocked-http-200.md).
- Конкретные значения лимитов — [Q-003-1](99-open-questions.md). Дефолты на старте:
  - `/v1/chat/run`: 30 req/min per user, 60 req/min per device, 120 req/min per IP.
  - Прочие POST: 60 req/min per user.

## Size-лимиты (защита payload)
Два разных уровня контроля размера с **разной HTTP-семантикой** (не путать):

1. **Transport-уровень — общий размер тела запроса (`413`).** Enforced `SizeLimitMiddleware` на API Gateway **до парсинга** тела, по `Content-Length`/объёму потока. Превышение → `413 Payload Too Large`. Это защита от приёма крупного payload как такового.
   - Общий request body: ≤ 512 KB.

2. **Schema-уровень — лимиты отдельных полей (`422`).** Enforced Pydantic v2 валидаторами (`max_length`) после парсинга. Нарушение лимита конкретного поля при допустимом размере тела → `422 Unprocessable Entity` (стандартная семантика per-field schema violation, согласована с прочей валидацией ввода ниже). Это **не** `413`: тело прошло transport-лимит, отклонено уже на валидации схемы.
   - `message`: ≤ 32 KB.
   - `context` object: ≤ 64 KB сериализованного JSON.
   - `tool-result` `result`: ≤ 256 KB.

Дефолты конкретных значений — [Q-003-2](99-open-questions.md). Оба пути — валидный технический reject 4xx; различие 413 vs 422 отражает уровень, на котором сработал лимит (transport до парсинга vs schema поля).

## Валидация ввода
- Строгие Pydantic v2 схемы на всех endpoint; `extra='forbid'`.
- `mode` ∈ {`credits`, `byok`}; `toolName` ∈ зафиксированном списке tools; иначе `422`.
- Tool args/result валидируются по строго типизированным схемам (см. chat-orchestrator).

## Backend-hosted preview (отдача пользовательского HTML/JS, ADR-010)
См. [ADR-010](adr/ADR-010-backend-hosted-preview.md), [modules/website-builder/05-security.md](modules/website-builder/05-security.md).
Критично: эндпоинт `GET /v1/preview/{projectId}/{token}/{path:path}` отдаёт **пользовательский (Claude-сгенерированный) HTML/JS**.
- **Signed URL:** `token = base64url(exp).base64url(HMAC_SHA256(PREVIEW_URL_SECRET, "projectId|ownerUserId|exp"))`.
  HMAC + TTL (дефолт 15 мин) проверяются constant-time; подделка/истечение → `403`.
- **Изоляция владельца:** `ownerUserId` запечён в подпись; сверка с `projects.user_id`. Чужой/несуществующий → `404`.
- **Path-traversal guard:** нормализация `path` (запрет `..`/абсолютных/`\`/NUL); lookup по `(project_id, path)` в БД, не по ФС.
- **Content-type allowlist:** тип строго из `site_files.content_type` (html/css/js/json/png/jpeg/svg/gif/webp/woff2/plain), не из расширения/заголовков; вне allowlist — не принимается на запись.
- **Sandbox-заголовки:** `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'self'; frame-ancestors 'self'`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Cache-Control: private, no-store`. Без cookies/credentials.
- **Изоляция origin:** старт — выделенный путь `/v1/preview/*` + sandbox-заголовки; prod-рекомендация — отдельный поддомен ([Q-010-3](99-open-questions.md)).
- **Лимиты:** файл ≤ 1 MB, проект ≤ 10 MB, ≤ 200 файлов (конфигурируемо).
- Авторизация — в signed URL (не пользовательский JWT): превью открывается прямой ссылкой в браузере.

## Транспорт
- Только HTTPS (TLS терминируется на reverse-proxy / LB).
- HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` на ответах. **Исключение:** превью-ответы
  (`/v1/preview/*`) используют sandbox-CSP и `X-Frame-Options: SAMEORIGIN` (см. ADR-010) — отдельная политика для пользовательского контента.

## Документация API в prod
- OpenAPI-документация (`/docs`, `/redoc`, `/openapi.json`) управляется env `DOCS_ENABLED` (дефолт `true`). В prod рекомендуется `false`: схема API не раскрывается публично (снижение API surface для разведки). При `false` пути отдают `404`. Стандарт оформления и поведение флага — [08-api-documentation.md](08-api-documentation.md).
- Примеры в документации не содержат реальных секретов: `apiKey` (BYOK), `transaction` (StoreKit), `Authorization`/JWT — только плейсхолдеры; распространяется то же правило redaction, что и для логов.

## Логирование (безопасное)
- Структурированный JSON, correlation id (`requestId`, `sessionId`).
- Allowlist полей в логах; redaction middleware вырезает заголовок `Authorization`, любые поля `*key*`, `*token*`, `*secret*`, BYOK/StoreKit payload.
- Policy decision log и billing decision log не содержат секретов.

## Модель угроз (кратко)
| Угроза | Митигирование |
|---|---|
| Утечка BYOK ключа | Envelope encryption, no-log redaction, ключ только in-memory на время вызова. |
| Обход trial / двойной trial | Атомарный `UPDATE users SET trial_used=TRUE WHERE trial_used=FALSE`. |
| Двойное списание кредитов | Idempotency key + unique index + транзакция БД. |
| Спуфинг чужого userId | Сверка `userId` с `sub` JWT. |
| Создание «фантомного» пользователя без аутентификации | Lazy provisioning срабатывает только после полной JWT-верификации; невалидный токен → `401` до upsert (ADR-007). |
| Replay tool-result | Идемпотентность по `toolCallId` + статус `tool_calls`. |
| Abuse / DoS | Rate limits per user/device/IP + size-лимиты. |
| Подделка подписки | Server-side verification StoreKit транзакции через App Store Server API. |
| Эскалация до admin через пользовательский JWT | Изолированный `ADMIN_API_SECRET`/`X-Admin-Token`, отдельная `require_admin`; нет роли admin в JWT; разные секреты (ADR-009). |
| Утечка/подделка admin-секрета | Constant-time compare, ротация (PREV-секрет), redaction `X-Admin-Token`, отдельный rate limit, audit `admin_grant`. |
| Начисление на «фантомный» userId (опечатка) | Admin-grant не создаёт пользователей: несуществующий `userId` → `404` (ADR-009 / [Q-009-2](99-open-questions.md)). |
| Подделка/истечение preview URL | HMAC под `PREVIEW_URL_SECRET` + TTL, constant-time проверка → `403` (ADR-010). |
| Доступ к чужому проекту через превью | `ownerUserId` в подписи + сверка с `projects.user_id`; чужой → `404`. |
| XSS / доступ к API-origin из preview-контента | Sandbox CSP, `nosniff`, без cookies/credentials, рекомендация отдельного поддомена ([Q-010-3](99-open-questions.md)). |
| Path-traversal в превью | Нормализация `path`, запрет `..`/абсолютных, lookup по `(project_id, path)` в БД. |
| Запись в чужой проект моделью | `userId`/`external_project_id` server-side tools берут из контекста сессии, не из args (ADR-011). |
| Утечка приватного ключа подписи JWT | Секрет-менеджер/mounted-файл, redaction, не в образе; ротация через `kid`/JWKS (future) (ADR-018). |
| Массовая анонимная регистрация (Sybil/abuse) | Per-IP rate-limit на `/v1/auth/*`; App Attest/DeviceCheck — post-MVP ([Q-018-1](99-open-questions.md)). |
| Кража refresh-token | Single-use rotation + reuse-детект → ревокация цепочки устройства; hashed-store, не plaintext (ADR-018). |
| Подмена чужого `userId` при register | `userId` назначает backend (uuid4/find-by-device); `register`/`token` не принимают `userId` в теле (ADR-018). |
