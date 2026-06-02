# Auth — API Contracts

Все эндпоинты под префиксом `/v1/auth`. **Без** пользовательского JWT (это точка его получения). `Content-Type: application/json` для POST. Тело — строгая Pydantic-схема (`extra='forbid'`). Защита — rate-limit per IP ([05-security.md](05-security.md)).

## `POST /v1/auth/register`
Создать/найти идентичность устройства и выдать токены.

**Request**
```json
{ "deviceId": "A1B2C3D4-..." }
```
- `deviceId` — опционально, строка `1..128`, charset `[A-Za-z0-9._:-]`. Если отсутствует/пусто — backend генерирует UUIDv4 и возвращает его.

**Response 200**
```json
{
  "userId": "550e8400-e29b-41d4-a716-446655440000",
  "deviceId": "A1B2C3D4-...",
  "accessToken": "<RS256 JWT>",
  "tokenType": "Bearer",
  "expiresIn": 3600,
  "refreshToken": "<opaque>",
  "refreshExpiresIn": 2592000
}
```
- Если `deviceId` уже известен (`auth_devices`) — возвращается **существующий** `userId` (идемпотентно); новый `userId` не создаётся.
- Новое устройство → новый `userId = uuid4()`, provisioning `users`, строка `auth_devices`.
- `accessToken` claims: `sub=userId`, `device_id=deviceId`, `iss`, `aud`, `iat`, `exp`, `kid` (заголовок). Верифицируется собственным `JwtVerifier`.

**Коды:** `200` ok; `422` невалидный `deviceId`; `429` rate-limit; `503` issuer не сконфигурирован (нет приватного ключа).

## `POST /v1/auth/token`
Повторно получить токены для **известного** устройства (без создания новой идентичности).

**Request**
```json
{ "deviceId": "A1B2C3D4-..." }
```
- `deviceId` — **обязателен**.

**Response 200** — как у `register` (та же схема).
- Если устройство неизвестно → создаётся как в `register` (find-or-create; `token` и `register` различаются лишь обязательностью `deviceId` и семантическим намерением «у меня уже есть устройство»).

**Коды:** `200`; `422` отсутствует/невалидный `deviceId`; `429`; `503`.

## `POST /v1/auth/refresh`
Обменять refresh-token на новую пару (rotation, single-use).

**Request**
```json
{ "refreshToken": "<opaque>" }
```

**Response 200** — новая пара (как `register`, тот же `userId`/`deviceId` из связанной записи).

**Поведение:**
- Предъявленный refresh-token **инвалидируется** (single-use); выдаётся новый.
- Reuse уже использованного/неизвестного/истёкшего refresh → `401`; при детекте reuse валидной-но-использованной записи — ревокация **всей цепочки** устройства (анти-кража).
- Хранение — `token_hash` (SHA-256), не plaintext ([04-data-model.md](04-data-model.md)).

**Коды:** `200`; `401` невалидный/истёкший/reused refresh; `422` отсутствует поле; `429`.

## `GET /v1/auth/jwks`
JWKS с публичным ключом (для самопроверки/отладки/будущих верификаторов). **Опционально-публичный** (не содержит секретов). Управляется тем же `DOCS_ENABLED`-классом видимости — конкретно env `AUTH_JWKS_ENABLED` (дефолт `true`).

**Response 200**
```json
{ "keys": [ { "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "<JWT_KID>", "n": "...", "e": "AQAB" } ] }
```
- Только публичный ключ. Приватный никогда не отдаётся.

**Коды:** `200`; `404` если `AUTH_JWKS_ENABLED=false` или публичный ключ не сконфигурирован.

## Ошибки
Стандартный формат ([api-gateway/02-api-contracts.md](../api-gateway/02-api-contracts.md)): `{ "error": { "code", "message", "requestId" } }`. `code` ∈ { `validation_error`, `unauthorized`, `rate_limited`, `internal_error` } + `503` (`code=service_unavailable`) при несконфигурированном issuer.

## Карта (для api-gateway)
| Метод | Путь | Auth | Назначение |
|---|---|---|---|
| POST | /v1/auth/register | нет (rate-limit per IP) | find-or-create identity + токены |
| POST | /v1/auth/token | нет (rate-limit per IP) | токены для известного устройства |
| POST | /v1/auth/refresh | нет (rate-limit per IP) | rotation refresh → новая пара |
| GET | /v1/auth/jwks | нет (опц. публичный) | публичный ключ (JWKS) |
