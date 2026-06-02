# Auth — Security

## Ключевая пара RSA (RS256)
- **Приватный ключ — СЕКРЕТ.** Только через env / secret manager / mounted-файл. Никогда в репозитории, образе, логах. В redaction allowlist (`JWT_PRIVATE_KEY`, плюс покрытие `*key*`/`*secret*`).
- **Публичный ключ** — не секрет; используется `JwtVerifier` (verify) и `GET /v1/auth/jwks` (отдача).
- Issuer (подпись) и Verifier (проверка) разделяют **одну** пару из config — self-consistent loop ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md) §3).
- `kid` (`JWT_KID`) проставляется в заголовок — задел под ротацию ключей (несколько активных kid в JWKS) — future, не MVP.

## PEM-в-env (решение)
Многострочный PEM плохо переносится через `.env`. Поддержаны **оба** механизма, приоритет у файла:
| Переменная | Назначение | Приоритет |
|---|---|---|
| `JWT_PRIVATE_KEY_PATH` | путь к PEM-файлу приватного ключа (prod-рекомендация: mount секрета) | выше |
| `JWT_PRIVATE_KEY` | PEM-строка приватного ключа с **`\n`-экранированием** (литералы `\n` → переводы строк при загрузке) | ниже |
| `JWT_PUBLIC_KEY_PATH` | путь к PEM-файлу публичного ключа | выше |
| `JWT_PUBLIC_KEY` | PEM-строка публичного ключа (`\n`-экранирование); **уже существует** в config | ниже |
- Резолв: `*_PATH` читается из файла; иначе строковое значение разэкранируется (`value.replace("\\n", "\n")`).
- Нет ни пути, ни приватной строки → issuer-эндпоинты `503` (`service_unavailable`); verify-only режим продолжает работать на публичном ключе/`JWT_JWKS_URL`.
- `.env` в `.gitignore`; в prod — секрет-менеджер ([Q-002-1](../../99-open-questions.md) дефолт).

## Issuer / audience (self-consistent)
- `JWT_ISSUER = https://broadnova.shop` (= `SERVICE_DOMAIN`, [Q-017-1](../../99-open-questions.md)).
- `JWT_AUDIENCE = claude-ios`.
- Verifier проверяет `iss`/`aud` против тех же значений (тот же config) — токен, выпущенный backend'ом, проходит собственную верификацию.

## Refresh-token
- Opaque (не JWT), высокоэнтропийный (`secrets.token_urlsafe(32)`). В БД — **только** `sha256`-хэш ([04-data-model.md](04-data-model.md)).
- Single-use rotation; reuse использованного → `401` + ревокация всей цепочки устройства (детект кражи). Серверная ревокация (logout/кража) возможна только потому, что refresh — stateful (а не JWT).
- TTL 30 дней (`AUTH_REFRESH_TTL_SECONDS`), access-token TTL 1ч (`AUTH_ACCESS_TTL_SECONDS`) — короткое окно при утечке access.

## Rate-limit и anti-abuse
- `/v1/auth/*` — **без** JWT (точка его получения), поэтому защищены **per-IP** rate-limit'ом: `AUTH_RATE_LIMIT_PER_IP` (дефолт `10 req/min per IP`). Использует существующий per-IP лимитер gateway (Redis), client-IP определяется через trusted-proxy логику ([05-security.md](../../05-security.md#доверенный-reverse-proxy-и-определение-client-ip-anti-spoofing)).
- **Массовая генерация identity (Sybil)** — [Q-018-1](../../99-open-questions.md): дефолт — per-IP rate-limit; усиление App Attest / DeviceCheck — post-MVP (не закрывается путь).
- `deviceId` валидируется (строка `1..128`, charset `[A-Za-z0-9._:-]`, `extra='forbid'`) — защита от инъекций/мусора.

## Что НЕ логируется
Приватный ключ (`JWT_PRIVATE_KEY`/файл-содержимое), выпущенный access-token (JWT), refresh-token (plaintext и хэш не выводятся в ответных логах), любые `*key*`/`*token*`/`*secret*`. `deviceId` — нечувствителен (не PII), логируется как correlation-атрибут.

## Модель угроз (дополнение к [05-security.md](../../05-security.md))
| Угроза | Митигирование |
|---|---|
| Утечка приватного ключа подписи | Секрет-менеджер/mounted-файл, redaction, не в образе; ротация через `kid`/JWKS (future). |
| Массовая анонимная регистрация (Sybil/abuse) | Per-IP rate-limit; App Attest усиление — post-MVP ([Q-018-1](../../99-open-questions.md)). |
| Кража refresh-token | Single-use rotation + reuse-детект + ревокация цепочки; hashed-store. |
| Подмена чужого `userId` через register | `userId` задаёт backend (uuid4 / find-by-device), не клиент; `register` не принимает `userId` в теле. |
| Долгоживущий access при утечке | Короткий TTL (1ч); компрометация ограничена окном. |
| Issuer не сконфигурирован (нет приватного ключа) в prod | `503` на issuer-эндпоинтах + prod-checklist пункт ([07-deployment.md](../../07-deployment.md)). |
