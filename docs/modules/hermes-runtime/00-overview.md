# Hermes Runtime — Overview

## Назначение
Модуль управляет жизненным циклом персональных автономных агентов Hermes. Каждый подписчик получает собственный Hermes-инстанс: Docker-контейнер с приватным томом `HERMES_HOME` (память, навыки, сессии), ограниченным набором инструментов и уникальным `API_SERVER_KEY`. Модуль — внутренний control-plane-компонент монолита (`src/app/hermes_runtime/`), не имеет собственных публичных HTTP-эндпоинтов (его потребитель — [Agent Proxy](../agent-proxy/README.md)).

## In scope
- Провижининг инстанса (`docker run` образа Hermes с томом, env, `config.yaml`, в выделенной docker-сети, без проброса host-порта).
- `ensure_running` — резолв/пробуждение/обновление активности инстанса перед прокси-вызовом.
- Гибернация (`stop_idle`) простаивающих инстансов + фоновый reaper в `lifespan`.
- Деинициализация (`deprovision`) и health-пробинг.
- Registry поверх таблицы `hermes_instances`.
- Расширяемый `RuntimeBackend` (MVP — Docker; задел под Modal/Daytona).
- Шифрование per-instance `API_SERVER_KEY` at-rest через `byok.kms`.

## Out of scope
- Прокси чата к инстансу, SSE-ретрансляция, биллинг — [Agent Proxy](../agent-proxy/README.md) ([ADR-045](../../adr/ADR-045-hermes-as-agent-proxy.md), [ADR-047](../../adr/ADR-047-usage-based-billing-for-agent.md)).
- Внутренняя логика агента Hermes (его tool-loop, память, навыки) — это Hermes, не наш код.
- Авторизация клиента — [API Gateway](../api-gateway/README.md) / [ADR-044](../../adr/ADR-044-client-api-key-auth.md).

## Ключевые решения
- [ADR-046](../../adr/ADR-046-per-user-hermes-runtime.md) — per-user runtime, гибернация, registry, `RuntimeBackend`, таблица `hermes_instances`.
- [ADR-003](../../adr/ADR-003-byok-envelope-encryption.md) — KMS для шифрования `API_SERVER_KEY`.
- [ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md) — deploy-топология (Docker socket, выделенная сеть, том-рут).

## Открытые вопросы
- [Q-046-1](../../99-open-questions.md) — масштабирование per-user runtime (контейнеры/пулы/serverless).
- [Q-046-2](../../99-open-questions.md) — политика хранения/удаления тома `HERMES_HOME` неактивного пользователя.
