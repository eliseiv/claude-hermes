# ADR Index

Реестр архитектурных решений. Статусы: Proposed / Accepted / Superseded.

| ADR | Заголовок | Статус | Дата |
|---|---|---|---|
| [ADR-001](ADR-001-stack-choice.md) | Выбор стека: Python + FastAPI + PostgreSQL, модульный монолит | Accepted | 2026-05-21 |
| [ADR-002](ADR-002-access-policy-state-machine.md) | Политика доступа как state machine (trial → subscription → credits/byok) | Accepted | 2026-05-21 |
| [ADR-003](ADR-003-byok-envelope-encryption.md) | BYOK: envelope encryption (AES-256-GCM + KMS) | Accepted (ревизия 2026-06-02) | 2026-05-21 |
| [ADR-004](ADR-004-blocked-http-200.md) | HTTP 200 для бизнес-blocked + стандартизированный blockReason enum | Accepted | 2026-05-21 |
| [ADR-005](ADR-005-idempotency-ledger.md) | Атомарность и идемпотентность ledger и tool-result | Accepted | 2026-05-21 |
| [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) | Биллинг: 1 кредит = 1 сообщение; фикс. пакет кредитов на период подписки | Accepted | 2026-05-21 |
| [ADR-007](ADR-007-lazy-user-provisioning.md) | Ленивый провижининг users из доверенного JWT subject (upsert в gateway) | Accepted | 2026-05-25 |
| [ADR-008](ADR-008-provider-tool-use-id.md) | Раздельное хранение provider tool_use.id (`toolu_...`) для согласованности continuation | Accepted | 2026-05-25 |
| [ADR-009](ADR-009-admin-token-auth.md) | Admin-авторизация: изолированный admin-токен (`X-Admin-Token`, статический secret), ротация, невозможность эскалации | Accepted | 2026-06-01 |
| [ADR-010](ADR-010-backend-hosted-preview.md) | Backend-hosted preview сайтов: signed URL (HMAC+TTL) + threat model отдачи пользовательского HTML/JS | Accepted | 2026-06-01 |
| [ADR-011](ADR-011-server-side-tools.md) | Server-side tools (`site.*`): backend исполняет в tool-loop, не отдаёт клиенту | Accepted | 2026-06-01 |
| [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) | Разведение терминологии: `assistant_mode` (chat/code, тип ассистента) vs `billing_mode` (credits/byok, способ оплаты) | Accepted | 2026-06-02 |
| [ADR-013](ADR-013-workspace-projects-vs-website-builder.md) | Workspace-проекты (рабочие пространства чатов) — отдельный модуль, не website-builder `projects` | Accepted | 2026-06-02 |
| [ADR-014](ADR-014-multimodal-attachments.md) | Мультимодальный ввод: двухшаговые вложения (upload `/v1/attachments` → ссылка в `/chat/run`) | **Superseded (транспорт) → [ADR-020](ADR-020-inline-base64-attachments-mvp.md)** | 2026-06-02 |
| [ADR-015](ADR-015-consumable-token-iap.md) | Покупка токенов: consumable StoreKit IAP → идемпотентный grant кредитов (отдельно от подписки) | Accepted | 2026-06-02 |
| [ADR-016](ADR-016-extended-byok-statuses.md) | Расширенные BYOK-статусы (`validating`/`offline`/`expired`) + активная модель в ответе, обратная совместимость | Accepted | 2026-06-02 |
| [ADR-017](ADR-017-shared-server-traefik-deploy.md) | Deploy-топология: общий сервер за внешним Traefik + GitHub Actions SSH (ревизует TD-005 VPS+Caddy); **расширен 2026-06-10: мульти-инстанс / клонирование** (`COMPOSE_PROJECT_NAME`) | Accepted (расширен 2026-06-10) | 2026-06-02 |
| [ADR-018](ADR-018-embedded-auth-issuer.md) | Встроенный auth-issuer в backend (device-based identity, RS256, refresh-rotation) — закрывает Q-005-1 | Accepted | 2026-06-02 |
| [ADR-019](ADR-019-tools-catalog-endpoint.md) | Каталог инструментов `GET /v1/tools` (JWT-protected, источник — chat/tools.py) | Accepted | 2026-06-02 |
| [ADR-020](ADR-020-inline-base64-attachments-mvp.md) | Мультимодальный ввод: inline base64-вложения в `/chat/run` (MVP); заменяет транспорт ADR-014 | Accepted | 2026-06-03 |
| [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) | Детерминированный порядок шагов сессии (монотонный `chat_steps.seq`) + нормализация content-блоков перед персистом (BUG-5) | Accepted | 2026-06-04 |
| [ADR-022](ADR-022-optional-project-and-tool-gating.md) | Опциональный `projectId` в `/v1/chat/run` + гейтинг `site.*` по наличию проекта (website-builder — опциональная фича; «чистый чат» без проекта) | Accepted | 2026-06-04 |
| [ADR-023](ADR-023-sync-ids-in-chat-response.md) | Идентификаторы синхронизации в `ChatResponse` (`messageStepId` + `stepId`, nullable при blocked) — склейка ответа генерации с историей чата | Accepted | 2026-06-05 |
| [ADR-024](ADR-024-history-payload-domain-normalization.md) | Нормализация content-блоков истории `GET /v1/chats/{id}` `steps[].payload` к доменному виду (dot-имя + domain id) при отдаче — хранение/реплей не меняются | Accepted | 2026-06-10 |
| [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) | Параллельные client-side tool-вызовы (`toolCalls[]` в `ChatResponse` + батч `/chat/tool-result`, барьер хода) + обработка обрезки по `max_tokens` (`blockReason=max_tokens`, дефолт `ANTHROPIC_MAX_TOKENS=16000`) | Accepted | 2026-06-10 |
| [ADR-026](ADR-026-global-server-side-tools-and-time-now.md) | Project-independent (global) server-side tools (`GLOBAL_SERVER_SIDE_TOOLS`, отдельно от project-scoped `site.*`) + инструмент `time.now` (UTC всегда + локальное по опц. IANA `tz`); работает без проекта, не мутирует, без новых списаний; статичная generic-инструкция в промт (кэш не ломается) | Accepted | 2026-06-10 |

## Ревизии

- **ADR-017 (расширение 2026-06-10):** добавлен раздел «Мульти-инстанс / клонирование сервиса» — паттерн запуска нескольких изолированных инстансов одного кода claude-ios за общим edge-Traefik под разными доменами (первый `broadnova.shop`, второй `avelyraweb.shop`). Параметризация `docker-compose.prod.yml` через `COMPOSE_PROJECT_NAME` (дефолт `claude-ios`) для image-тегов и Traefik router/service-имён с **инвариантом обратной совместимости** (дефолт=текущее значение, `compose config` идентичен для существующего `.env`). Per-instance JWT keypair в `/opt/<inst>/.secrets/`, свежие секреты, INSTANCES-loop в CI. Само deploy-решение не изменено — добавлен playbook горизонтального клонирования. Playbook — [07-deployment.md §Мульти-инстанс](../07-deployment.md#мульти-инстанс--клонирование-сервиса). Связано с [Q-017-3](../99-open-questions.md).
- **ADR-003 (ревизия 2026-06-02):** в рамках MVP-решения зафиксирован `LocalKmsClient` как KMS-реализация для MVP (реальный AES-256-GCM wrap DEK под `KMS_LOCAL_MASTER_KEY`, тот же интерфейс `KmsClient`); миграция на облачный KMS — post-MVP ([Q-002-1](../99-open-questions.md)). Само решение envelope encryption не изменено — уточнена реализация на MVP. Пометка добавлена для трассируемости (запрос architect-reviewer).
