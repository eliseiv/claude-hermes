# ADR-068 — RU-оплата CloudPayments (broadapps) + живой каталог продуктов

- **Статус:** Accepted
- **Дата:** 2026-09-18
- **Связано:** порт зрелого контура из claude-ios (ADR-050/051/053/054/057), [ADR-015](ADR-015-consumable-token-iap.md), [ADR-029](ADR-029-adapty-subscription-webhook.md), [ADR-018](ADR-018-embedded-auth-issuer.md)

## Контекст

`avorelio.shop` — единственный prod-инстанс `claude-hermes`. iOS-клиент должен уметь принимать оплату в рублях. В `claude-ios` это уже сделано через агрегатор broadapps (YooKassa, проводной формат CloudPayments): исходящая ссылка на оплату + публичный вебхук, который **не** начисляет по телу колбэка, а верифицирует платежи через API провайдера.

`GET /v1/tokens/products` в hermes отдавал только карту `TOKEN_PRODUCTS` (StoreKit-пакеты без цены). Клиенту нужен каталог RU-продуктов с `title`/`price`/`currency`/`kind`.

## Решение

Портирован зрелый контур claude-ios **без** CRM-оверлеев, экспериментов пейволла и `legacy_user_ids` (этих таблиц/модулей в hermes нет).

1. **`POST /v1/billing/cloudpayments/checkout`** (клиентский контур `X-API-Key` + `X-User-Id`). `userId` берётся **только** из авторизованного субъекта и уходит в broadapps как `user_id`/`AccountId`. Тело: `productId` + необязательный `customerEmail`. Не сконфигурировано (`CLOUDPAYMENTS_APP_ID` / `CLOUDPAYMENTS_API_TOKEN` пусты) → `503`. Неизвестный продукт → `422`. Исходящий вызов — `POST {CLOUDPAYMENTS_API_BASE}/payments/link` (multipart).

2. **`POST /v1/billing/cloudpayments/webhook`** — публичный триггер. Авторизация колбэка **не блокирует** (broadapps шлёт без auth). Начисление — только после `GET /users/{id}/payments` с нашим `CLOUDPAYMENTS_API_TOKEN`. Резолв пользователя: `users.id`, иначе `auth_devices.device_id` (case-insensitive). Запрос платежей идёт и по внутреннему `userId`, и по присланному идентификатору. Идемпотентность по broadapps `payment_id` (`cloudpayments_webhook_events` + ledger `cp-txn:{payment_id}`). Сумма — только из серверных карт (`TOKEN_PRODUCTS` / `CLOUDPAYMENTS_PRODUCT_TOKENS`), никогда из тела/amount.

3. **`GET /v1/tokens/products`** — три ветки: живой каталог broadapps → статичный `PRODUCTS_CATALOG` → `TOKEN_PRODUCTS`. Поля `title`/`kind`/`period`/`price`/`currency`/`credits`/`isSpecialOffer`/`isDefault` аддитивны.

Миграция `0021_cp_webhook_events`. Активно на инстансе, где заданы `CLOUDPAYMENTS_APP_ID` + `CLOUDPAYMENTS_API_TOKEN`.
