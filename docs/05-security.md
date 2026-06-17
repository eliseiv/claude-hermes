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

## Adapty webhook-авторизация (изолированная, без HMAC-подписи payload, ADR-029)
См. [ADR-029](adr/ADR-029-adapty-subscription-webhook.md), [modules/billing-adapty](modules/billing-adapty/README.md).
- `POST /v1/billing/adapty/webhook` авторизуется **отдельным статическим секретом** `ADAPTY_WEBHOOK_SECRET` через заголовок `Authorization: Bearer <...>`. Это **machine-to-machine** контур (вызывает только сервис Adapty), per-route dependency, **изолированный** от пользовательского JWT, admin-токена и preview-секрета.
- **Adapty НЕ подписывает payload (нет HMAC).** Аутентичность запроса = знание bearer-секрета (shared secret, заданный оператором в Adapty UI). Это ограничение платформы, принято осознанно. Митигация: высокоэнтропийный секрет (≥ 32 байта), TLS на edge, per-instance значение, ротация, audit.
- Сравнение токена — **constant-time** (`hmac.compare_digest`, образец `require_admin`). Несовпадение/отсутствие → `401` (причина не раскрывается). `ADAPTY_WEBHOOK_SECRET` не задан → `500` (мис-конфигурация); пустой секрет никогда не матчит.
- **2xx на кривой payload — анти-абуз бесконечных ретраев (а не «проглатывание ошибок»):** Adapty ретраит любой не-2xx бесконечно и не сохранит вебхук без 2xx на проверочный пинг. Поэтому **после успешной авторизации** любое нераспознанное/неполное/мусорное тело → `200 ignored/<reason>`. `5xx` — только при реальном внутреннем сбое (БД недоступна), где ретрай Adapty желателен и безопасен (откат транзакции → чистая переобработка). Тело читается сырым (`request.body()`), **без Pydantic-валидации** (иначе `422` на пинг/дрейф payload).
- `customer_user_id` из тела **не является авторизацией действий** — это лишь адресат гранта; несуществующий → `200 ignored/user_not_found` (без провижининга `users`). Эскалация невозможна: контур не даёт ни пользовательских, ни admin-привилегий.
- `Authorization` (и весь bearer) — уже в redaction-денилисте (`authorization` ∈ `_DENY_SUBSTRINGS`); секрет не логируется и не пишется в `adapty_webhook_events.payload` (он в заголовке, не в теле).

## Секреты и ключи
- Сервисный Anthropic API key, **сервисный OpenAI API key (`OPENAI_API_KEY`, инстансы `LLM_PROVIDER=openai`, [ADR-033](adr/ADR-033-llm-provider-abstraction.md))**, KMS credentials, **JWT signing keys (приватный RS256-ключ — секрет; публичный — для verify, не секрет)**, App Store credentials, **`ADMIN_API_SECRET`** (+ опц. `ADMIN_API_SECRET_PREV`), **`PREVIEW_URL_SECRET`**, **`ADAPTY_WEBHOOK_SECRET`** — только через **env / secret manager**, никогда в коде/репозитории/образе.
- Все перечисленные секреты **взаимно не пересекаются** (отдельные значения): JWT signing key, KMS, Anthropic, `ADMIN_API_SECRET`, `PREVIEW_URL_SECRET`, `ADAPTY_WEBHOOK_SECRET` — независимы; компрометация одного не даёт доступа к домену другого. `ADAPTY_WEBHOOK_SECRET` — per-instance (мульти-инстанс, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)).
- `.env` в `.gitignore`; в prod — секрет-менеджер (конкретный — [Q-002-1](99-open-questions.md), дефолт: облачный KMS + Secrets Manager того же провайдера).
- Запрет логировать любые секреты, BYOK plaintext, JWT (выпущенный access-token), refresh-token, приватный ключ подписи, StoreKit payload целиком.

## Провайдер LLM: OpenAI (ADR-033)
См. [ADR-033](adr/ADR-033-llm-provider-abstraction.md), [chat-orchestrator/03-architecture.md §Провайдер-абстракция LLM](modules/chat-orchestrator/03-architecture.md#провайдер-абстракция-llm-anthropic--openai-adr-033).
- **`OPENAI_API_KEY` (сервисный, mode=credits) и BYOK OpenAI-ключ пользователя (mode=byok) — секреты под redaction.** Уже покрыты денилистом подстрок `key`/`secret` (`redaction.py` `_DENY_SUBSTRINGS`); `OPENAI_API_KEY` содержит `key`. Отдельной правки redaction не требуется. BYOK plaintext OpenAI-ключ — только in-memory на время вызова, как Anthropic-ключ ([chat-orchestrator/03 §Безопасность](modules/chat-orchestrator/03-architecture.md#безопасность)).
- **Upstream-ошибки OpenAI** логируются по тому же контракту, что Anthropic ([§Логирование](#логирование-безопасное), [chat-orchestrator/03 §Логирование upstream-ошибок](modules/chat-orchestrator/03-architecture.md#логирование-upstream-ошибок-anthropic-td-014)): тело ошибки провайдера — да, api-key и user-content — нет.
- **PDF-вложения при `LLM_PROVIDER=openai` отклоняются `422` (`unsupported_media_type`)** — Chat Completions vision не принимает PDF ([TD-023](100-known-tech-debt.md)). Это чистая валидационная ошибка (не `500`); прочая валидация вложений (allowlist/magic-bytes/лимиты/PDF page-guard) и модель угроз ([§Вложения](#вложения-attachments-adr-020), если применимо) не меняются. На anthropic-инстансах PDF принимается как прежде ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)).
- **Один провайдер на инстанс:** `chat_steps.payload` инстанса хранит wire-формат только своего провайдера; смешения форматов в одной БД нет (инвариант [ADR-033](adr/ADR-033-llm-provider-abstraction.md)).

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

1. **Transport-уровень — общий размер тела запроса (`413`).** Enforced `SizeLimitMiddleware` на API Gateway **до парсинга** тела, по заголовку `Content-Length`. Превышение → `413 Payload Too Large`. Это защита от приёма крупного payload как такового. **Ограничение (на 2026-06-03):** проверка опирается на `Content-Length`; при его отсутствии (chunked-запрос без заголовка) transport-guard пропускается — streaming-устойчивая проверка по фактическому объёму потока **не реализована** ([TD-017](100-known-tech-debt.md); на MVP за внешним Traefik не эксплуатируется).
   - Общий request body: ≤ 512 KB. Повышенный лимит только роута `/v1/chat/run` (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB) под inline base64-вложения (ADR-020).

2. **Schema-уровень — лимиты отдельных полей (`422`).** Enforced Pydantic v2 валидаторами (`max_length`) после парсинга. Нарушение лимита конкретного поля при допустимом размере тела → `422 Unprocessable Entity` (стандартная семантика per-field schema violation, согласована с прочей валидацией ввода ниже). Это **не** `413`: тело прошло transport-лимит, отклонено уже на валидации схемы.
   - `message`: ≤ 32 KB.
   - `context` object: ≤ 64 KB сериализованного JSON.
   - `tool-result` `result`: ≤ 256 KB.

### `context` (per-message conversation settings, ADR-037)
Поле `context` ([ADR-037](adr/ADR-037-chatrunrequest-context-allowlist-injection.md)) — доп-настройки текущего хода (язык кода/стиль/тон/локаль), инъектируемые в промт. Модель угроз и контроли:

- **Инъекция в user-сообщение, НЕ в system-prompt.** context = собственные данные пользователя и не должен получать авторитет системного промта (нет privilege-escalation «через настройки»). Блок добавляется лидирующей строкой к содержимому user-сообщения turn0, перед текстом `message`. System+tools от context не зависят → prompt-кэш (`cache_control: ephemeral` на system) не инвалидируется.
- **Allowlist + per-key валидация.** Известный набор ключей (`codeLanguage`/`responseStyle`/`verbosity`/`tone`/`locale`); неизвестные ключи и невалидные значения игнорируются (lenient, forward-compat). Свободные строки ограничены длиной (`strip`, ≤35–40 символов), `locale` — символьным классом `[A-Za-z0-9_-]`, `responseStyle`/`verbosity` — закрытыми enum. Это ограничивает поверхность инъекции произвольного текста под видом «настроек».
- **Экранирование разделителей** служебного блока (`; `, `=`, переводы строк → пробел в значениях) — чтобы значение пользователя не нарушало структуру блока.
- **Size-guard** `size_limit_context` (≤64KB, `422`) сохраняется как защита от раздутого тела (схема-уровень, см. выше).
- **Не логировать** содержимое `context` (как и `message`); в логи/метрики — максимум имена применённых ключей, не значения.

Дефолты конкретных значений — [Q-003-2](99-open-questions.md). Оба пути — валидный технический reject 4xx; различие 413 vs 422 отражает уровень, на котором сработал лимит (transport до парсинга vs schema поля).

### Повышенный transport-лимит для `/v1/chat/run` (inline base64-вложения, ADR-020)
Inline base64-вложения ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)) превышают общий `≤512KB`. Поэтому `SizeLimitMiddleware` применяет **повышенный лимит только к роуту `/v1/chat/run`** (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB); все прочие роуты сохраняют общий `≤512KB`. Превышение per-route лимита → `413` (до парсинга). Повышение НЕ глобальное — поверхность приёма крупного payload ограничена одним роутом.

## Мультимодальные вложения — валидация и модель угроз (ADR-020)
Критично: `/v1/chat/run` принимает в `attachments[]` загруженный пользователем бинарный контент (фото/PDF/текст) в base64 и передаёт его Claude ([ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md)). Правила валидации (фокус ревью):

- **Allowlist `mediaType` (не denylist).** `image/jpeg|png|gif|webp`, `application/pdf`, `text/plain|markdown|csv`, `application/json`. Вне allowlist → `422 unsupported_media_type` ([Q-020-1](99-open-questions.md) — расширение).
- **Соответствие заявленного MIME содержимому (magic bytes).** `type`/`mediaType` из запроса сверяются с реальной сигнатурой декодированного содержимого (JPEG/PNG/GIF/WEBP/PDF magic bytes; для `text/*`/`json` — успешная UTF-8-декодировка и при необходимости JSON-парс). Рассогласование → `422`. Нельзя доверять заявленному клиентом `mediaType`.
- **Лимиты — ДО декодирования base64.** Размер base64-строки проверяется до `b64decode` (decoded ≈ 3/4 от base64-длины): одно вложение ≤ `ATTACHMENT_MAX_BYTES_IMAGE` (дефолт 5 MB) / `ATTACHMENT_MAX_BYTES_DOCUMENT` (дефолт 8 MB), суммарно ≤ `ATTACHMENT_TOTAL_BYTES` (10 MB), число ≤ `ATTACHMENT_MAX_COUNT` (10). Превышение → `413`/`422`. Это защита от раздувания памяти декодированием.
- **Валидность base64.** Невалидный/обрезанный base64 → `422` (не 500).
- **Анти-decompression/zip-bomb для PDF.** Guard числа страниц PDF (`ATTACHMENT_PDF_MAX_PAGES`, дефолт 100) через `pypdf` (только подсчёт страниц/структуры, без полного рендера); превышение → `422`. PDF с подозрительной структурой/паролем → `422`. Защищает от «маленький файл → гигантский разворот».
- **Никакого URL-fetch (анти-SSRF).** URL-вложения (`source.type=url`) запрещены: backend НЕ выполняет исходящих запросов за содержимым вложения. Только inline base64 — SSRF-вектор устранён by construction.
- **Redaction.** Содержимое вложений (`attachments[].data`, декодированные байты, текст файлов) **никогда не логируется** — попадает в redaction-allowlist наравне с user-content промпта. В логах — только метаданные (класс, `mediaType`, размер, число). См. [§ Логирование](#логирование-безопасное).
- **Хранение.** Сырой base64 НЕ персистится в `chat_steps.payload` (только текстовый плейсхолдер, [ADR-020 §3](adr/ADR-020-inline-base64-attachments-mvp.md)) — снижает поверхность утечки данных пользователя из БД.

Полный набор `ATTACHMENT_*` settings — [02-tech-stack.md](02-tech-stack.md) / backend config; дефолты конфигурируемы ([Q-020-2](99-open-questions.md)).

## Workspaces — изоляция, лимиты файлов, инъекция контента ([ADR-036](adr/ADR-036-workspaces-implementation.md))
Критично: workspace хранит пользовательский контент (`instructions` — кастомный system-prompt; файлы-знания — байты + извлечённый текст), который подаётся модели как промт/контекст для **всех** чатов проекта.

- **Изоляция по владельцу.** Все операции `/v1/workspaces/*` и привязка `workspaceProjectId` скоупятся `WHERE user_id = :sub`. Чужой/несуществующий workspace → `404` (никогда не раскрывать чужое существование, BR-WS-1). Привязка чата к чужому workspace в `/chat/run` → `404 workspace_not_found`. Фильтр `GET /v1/chats?workspaceProjectId=` чужого → пустой список. **Чужой контекст не может попасть в чужой промт** — инъекция читает только workspace, принадлежащий `sub` сессии.
- **Файлы-знания: те же правила валидации, что у вложений ([§ Мультимодальные вложения](#мультимодальные-вложения--валидация-и-модель-угроз-adr-020))** — allowlist `mediaType`, magic-bytes-сверка, лимиты до декодирования base64, валидность base64, анти-zip-bomb PDF (`pypdf`-guard, [TD-004a](100-known-tech-debt.md)), никакого URL-fetch (анти-SSRF). Плюс лимиты workspace: `WORKSPACE_FILE_MAX_COUNT` (20), `WORKSPACE_FILE_MAX_BYTES` (8 MB), `WORKSPACE_FILES_TOTAL_BYTES` (32 MB). Превышение → `413`/`422`.
- **Хранение в БД (BYTEA).** В отличие от inline-attachments (не персистятся), файлы-знания **персистятся** в `workspace_files.content` (долгоживущий контекст). Это расширяет поверхность хранения пользовательских данных в БД (как `site_files`) — учтено в модели данных; миграция в object storage — [TD-027](100-known-tech-debt.md).
- **Инъекция извлечённого текста (не нативный PDF).** Файлы document/text подаются модели как `extracted_text` (текст), не нативный бинарный PDF — поэтому ограничение PDF→422 на OpenAI ([TD-023](100-known-tech-debt.md)) **не применяется** к workspace-файлам (работает на обоих провайдерах). Лимит суммарного инжектируемого текста — `WORKSPACE_CONTEXT_MAX_CHARS` (усечение, защита от раздувания промта).
- **`instructions` — данные, не код.** Кастомный system-prompt — пользовательский текст, добавляемый к base-промту; backend не исполняет его как инструкцию инфраструктуре, только передаёт модели. Prompt-injection в пределах **собственного** диалога пользователя — его ответственность (не cross-user угроза при строгой изоляции `sub`).
- **Redaction.** `instructions`, байты и `extracted_text` файлов **никогда не логируются** (наравне с user-content и вложениями). В логах — только метаданные (id, `filename`, `mediaType`, `size`, число). См. [§ Логирование](#логирование-безопасное).
- **Биллинг не обходится.** CRUD/файлы бесплатны (нет генерации); генерация в чате workspace — обычные 1 кредит ([ADR-006](adr/ADR-006-credit-billing-and-subscription-grant.md)); инъекция контекста на amount не влияет.

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
- Allowlist полей в логах; redaction middleware вырезает заголовок `Authorization`, любые поля `*key*`, `*token*`, `*secret*`, BYOK/StoreKit payload, **содержимое вложений (`attachments[].data` и декодированные байты/текст, [ADR-020](adr/ADR-020-inline-base64-attachments-mvp.md))**.
- Policy decision log и billing decision log не содержат секретов.
- **Upstream-ошибки Anthropic** ([TD-014](100-known-tech-debt.md), [modules/chat-orchestrator/03-architecture.md §Логирование upstream-ошибок Anthropic](modules/chat-orchestrator/03-architecture.md#логирование-upstream-ошибок-anthropic-td-014) — **канонический контракт ключей лог-записи**): логировать **разрешено** тело ошибки апстрима. Запись — структурированный JSON, событие `anthropic_upstream_error`, с camelCase-ключами лог-записи `status_code`, `errorType`, `errorMessage`, `anthropicRequestId`, `model`, `exceptionClass`. Значения `errorType`/`errorMessage`/`anthropicRequestId` берутся из ТЕЛА ошибки Anthropic — соответственно `error.type`/`error.message` (источник) и `request_id` SDK; это поля провайдера-источника, а не имена ключей лог-записи. Содержимое тела ошибки — сообщение провайдера, не user-content. **Запрещено** логировать `ANTHROPIC_API_KEY`, BYOK-ключ пользователя и содержимое пользовательских сообщений/тело промпта — даже когда ошибка апстрима связана с ключом (логируется сообщение Anthropic, не сам ключ). Запись проходит через ту же redaction-middleware. Поведение наружу не меняется (502).

## Модель угроз (кратко)
| Угроза | Митигирование |
|---|---|
| Утечка BYOK ключа | Envelope encryption, no-log redaction, ключ только in-memory на время вызова. |
| Утечка api-key/BYOK при логировании upstream-ошибки | Логируется только тело ошибки Anthropic (лог-ключи `status_code`/`errorType`/`errorMessage`/`anthropicRequestId`, из полей-источника `error.type`/`error.message`/`request_id`), но не сам ключ и не user-content; redaction-middleware ([TD-014](100-known-tech-debt.md)). |
| Обход trial / двойной trial | Атомарный `UPDATE users SET trial_used=TRUE WHERE trial_used=FALSE`. |
| Двойное списание кредитов | Idempotency key + unique index + транзакция БД. |
| Спуфинг чужого userId | Сверка `userId` с `sub` JWT. |
| Создание «фантомного» пользователя без аутентификации | Lazy provisioning срабатывает только после полной JWT-верификации; невалидный токен → `401` до upsert (ADR-007). |
| Replay tool-result | Идемпотентность по `toolCallId` + статус `tool_calls`. |
| Abuse / DoS | Rate limits per user/device/IP + size-лимиты. |
| Подделка подписки | Server-side verification StoreKit транзакции через App Store Server API. |
| Форж Adapty-вебхука (нет HMAC) | Статический bearer-секрет `ADAPTY_WEBHOOK_SECRET` (constant-time compare), TLS на edge, per-instance ротация, audit `adapty_subscription`. Знание секрета = условие приёма; idempotency (`event_id` UNIQUE) ограничивает повтор. Ограничение платформы Adapty (нет подписи payload) — [ADR-029](adr/ADR-029-adapty-subscription-webhook.md). |
| Абуз ретраями Adapty (шторм не-2xx) | После авторизации любой кривой payload → `2xx ignored`; `5xx` только при реальном сбое. Сырое тело без Pydantic (нет `422` на пинг/дрейф). |
| Двойное начисление подписки (Adapty + StoreKit sync) | Разные idempotency-ключи (`adapty-event:*` vs `sub-grant:*`) НЕ защищают между путями. Митигация контрактом: клиент использует ОДИН путь подписок ([ADR-029](adr/ADR-029-adapty-subscription-webhook.md)). В пределах Adapty-пути двойная UNIQUE-граница (`event_id` + ledger key). |
| Эскалация до admin через пользовательский JWT | Изолированный `ADMIN_API_SECRET`/`X-Admin-Token`, отдельная `require_admin`; нет роли admin в JWT; разные секреты (ADR-009). |
| Утечка/подделка admin-секрета | Constant-time compare, ротация (PREV-секрет), redaction `X-Admin-Token`, отдельный rate limit, audit `admin_grant`. |
| Начисление на «фантомный» userId (опечатка) | Admin-grant не создаёт пользователей: несуществующий `userId` → `404` (ADR-009 / [Q-009-2](99-open-questions.md)). |
| Подделка/истечение preview URL | HMAC под `PREVIEW_URL_SECRET` + TTL, constant-time проверка → `403` (ADR-010). |
| Доступ к чужому проекту через превью | `ownerUserId` в подписи + сверка с `projects.user_id`; чужой → `404`. |
| XSS / доступ к API-origin из preview-контента | Sandbox CSP, `nosniff`, без cookies/credentials, рекомендация отдельного поддомена ([Q-010-3](99-open-questions.md)). |
| Path-traversal в превью | Нормализация `path`, запрет `..`/абсолютных, lookup по `(project_id, path)` в БД. |
| Запись в чужой проект моделью | `userId`/`external_project_id` server-side tools берут из контекста сессии, не из args (ADR-011). Дополнительно: при сессии **без `projectId`** (`chat_sessions.project_id IS NULL`, [ADR-022](adr/ADR-022-optional-project-and-tool-gating.md)) `site.*` Claude **не предлагаются** и не исполняются → резолв проекта не происходит, поверхность IDOR по проекту отсутствует по построению. |
| Утечка приватного ключа подписи JWT | Секрет-менеджер/mounted-файл, redaction, не в образе; ротация через `kid`/JWKS (future) (ADR-018). |
| Массовая анонимная регистрация (Sybil/abuse) | Per-IP rate-limit на `/v1/auth/*`; App Attest/DeviceCheck — post-MVP ([Q-018-1](99-open-questions.md)). |
| Кража refresh-token | Single-use rotation + reuse-детект → ревокация цепочки устройства; hashed-store, не plaintext (ADR-018). |
| Подмена чужого `userId` при register | `userId` назначает backend (uuid4/find-by-device); `register`/`token` не принимают `userId` в теле (ADR-018). |
| Подделка MIME вложения (выдать бинарь за image) | Сверка `type`/`mediaType` с magic bytes декодированного содержимого; рассогласование → `422` (ADR-020). |
| Memory-DoS через гигантское base64-вложение | Лимиты размера/числа проверяются ДО `b64decode`; повышенный body-лимит только на `/v1/chat/run`; `413`/`422` (ADR-020). Остаточный риск: transport-guard опирается на `Content-Length` ([TD-017](100-known-tech-debt.md)). |
| PDF decompression/structure bomb | Guard числа страниц PDF (`pypdf`, без полного рендера); подозрительный/защищённый PDF → `422` (ADR-020). Остаточный риск: CPU-spike при парсинге злонамеренного PDF в рамках 8 MB-cap ([TD-004](100-known-tech-debt.md) §TD-004a). |
| SSRF через URL-вложение | URL-вложения запрещены; backend не фетчит внешний контент, только inline base64 (ADR-020). |
| Утечка содержимого вложений в логах | Redaction `attachments[].data` и декодированных байт/текста; в логах только метаданные (ADR-020). |
| Раздувание/утечка байтов вложений из БД | Сырой base64 не персистится в `chat_steps.payload` — только текстовый плейсхолдер (ADR-020 §3). |
| Утечка путей/URL/signed-token через `serverTools[].summary` ответа `/chat/run` | `summary` несёт **только** `"ok"` (при `completed`) либо короткий машинный `error_code` (при `errored`) — raw-результат, `error_message`, пути, URL и signed-token превью **никогда** не читаются и не попадают в ответ; лимит `_SUMMARY_MAX_CHARS=120`. Полный результат server-side инструмента доступен только в истории `GET /v1/chats/{id}` ([ADR-028](adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) §Решение 2, [Q-028-1](99-open-questions.md), нормализация [ADR-024](adr/ADR-024-history-payload-domain-normalization.md)). |
| Чтение/подача контекста чужого workspace в свой промт | Все операции workspace и привязка `workspaceProjectId` скоупятся `user_id = sub`; чужой/несуществующий → `404`; инъекция instructions/файлов читает только workspace владельца сессии ([ADR-036](adr/ADR-036-workspaces-implementation.md)). |
| Memory/zip-bomb через файл-знание workspace | Те же лимиты/валидации, что у вложений (allowlist, magic-bytes, лимиты до b64decode, pypdf-guard) + `WORKSPACE_FILE_MAX_*`/`WORKSPACE_FILES_TOTAL_BYTES`/`WORKSPACE_CONTEXT_MAX_CHARS`; URL-fetch запрещён ([ADR-036 §4,§6](adr/ADR-036-workspaces-implementation.md)). |
| Утечка `instructions`/содержимого файлов workspace в логах | Redaction: `instructions`, байты и `extracted_text` не логируются; только метаданные (id/`filename`/`mediaType`/`size`) ([ADR-036](adr/ADR-036-workspaces-implementation.md)). |
