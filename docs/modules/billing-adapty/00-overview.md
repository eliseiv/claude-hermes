# billing-adapty / 00 — Overview

## Назначение
Приём серверного вебхука платформы подписок Adapty и приведение состояния биллинга в соответствие событию: обновление `subscriptions` + идемпотентный грант кредитов по тиру продукта. Это **основной путь биллинга по подпискам** ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)).

## In scope
- Эндпоинт `POST /v1/billing/adapty/webhook`.
- Статическая bearer-авторизация (constant-time), изолированный per-instance секрет.
- Дефенсивный приём сырого тела + ручной парсинг (без Pydantic-валидации тела).
- 4 типа событий: `subscription_started`, `subscription_renewed`, `subscription_cancelled`, `subscription_expired`.
- Идемпотентность через таблицу `adapty_webhook_events` (UNIQUE `event_id`) + ledger idempotency-key.
- Тир `vendor_product_id → tokens` (config-карта + fallback).
- Audit `adapty_subscription`.

## Out of scope (этой итерации)
- **Consumable-пакеты токенов через Adapty.** Остаются на `/v1/tokens/purchase` ([ADR-015](../../adr/ADR-015-consumable-token-iap.md)). Перенос — [Q-029-1](../../99-open-questions.md), [TD-020](../../100-known-tech-debt.md).
- Webhook на нашей стороне → Adapty (исходящие вызовы Adapty API). Не требуется.

## Ретирование `/v1/subscription/sync` (prod-harden, [TD-021](../../100-known-tech-debt.md)/[Q-029-2](../../99-open-questions.md))
StoreKit-путь подписок `POST /v1/subscription/sync` **ретируется** (ревизия [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)): роут + подписочная ветка кода удаляются backend'ом. Adapty-вебхук — **единственный** путь подписок claude-hermes → анти-double-grant **by construction** (второго пути нет). `/v1/tokens/purchase` (consumable) сохраняется.

## Ключевой инвариант (анти-double-grant)
После ретирования `/v1/subscription/sync` путь подписок один (Adapty) → двойное начисление невозможно конструктивно. В пределах Adapty-пути — двойная UNIQUE-граница (`adapty_webhook_events.event_id` + ledger `adapty-event:{event_id}`).
