# ADR-057 — Deploy-топология: ВЫДЕЛЕННЫЙ сервер с self-hosted Traefik-контейнером в нашем docker-compose

- **Статус:** Accepted (2026-06-25)
- **Контекст:** второй deploy-target для сервиса `claude-hermes` — **отдельный** Linux-сервер `87.239.135.156` под доменом `avorelio.shop`, где reverse-proxy/TLS — **наш собственный Traefik-контейнер** в составе `docker-compose.prod.yml` (НЕ внешний edge-Traefik из `/opt/edge`).
- **Дополняет, не отменяет:** [ADR-017](ADR-017-shared-server-traefik-deploy.md) (общий сервер `87.239.135.154` за **внешним** Traefik `/opt/edge`, домен `broadnova.shop` и со-инстансы) остаётся в силе для broadnova-сервера и его инстансов (`claude-ios`/`avelyra`/`orvianix`/`veltrio` и co-located `claude-hermes` на `.154`). ADR-057 вводит **второй, независимый** deploy-target для `claude-hermes` на `.156` — выбор target операционный (один и тот же код/репозиторий).
- **Совместимость инвариантов Hermes runtime:** [ADR-046](ADR-046-per-user-hermes-runtime.md) / [ADR-045](ADR-045-hermes-as-agent-proxy.md) / [ADR-055](ADR-055-hermes-instance-llm-config-contract.md) / [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md) **не меняются**: `hermes-net` external, `docker.sock` в `api` (`:ro`), `HERMES_UID/GID=10001`, readiness-gate, токенный LLM-биллинг — без правок (см. §Совместимость ниже).

## Контекст

[ADR-017](ADR-017-shared-server-traefik-deploy.md) зафиксировал deploy на **общем** сервере `87.239.135.154`, где владелец инфраструктуры держит **внешний** edge-Traefik (`/opt/edge`): он владеет портами 80/443, терминирует TLS, авто-выпускает Let's Encrypt-сертификаты и роутит по доменам **для всех** сервисов сервера. Наш стек туда **не** добавлял reverse-proxy — `api` встраивался в чужой Traefik через docker-labels + внешнюю сеть `web`.

Новое требование пользователя: развернуть `claude-hermes` на **выделенном** сервере `87.239.135.156` (Linux, root, ОТДЕЛЬНЫЙ инстанс, **не** shared broadnova) под доменом `avorelio.shop` (DNS A → `87.239.135.156`). На этом сервере **нет** внешнего edge-Traefik — reverse-proxy и TLS становятся **нашей** ответственностью. Поскольку сервер выделенный и единственный потребитель портов 80/443 — мы запускаем **собственный Traefik-контейнер** в нашем `docker-compose.prod.yml` (self-hosted), с авто-TLS Let's Encrypt.

Различие двух target-ов:

| | ADR-017 (broadnova `.154`) | ADR-057 (avorelio `.156`) |
|---|---|---|
| Сервер | общий (shared, music-backend и др.) | выделенный (только claude-hermes) |
| Reverse-proxy / TLS | **внешний** Traefik `/opt/edge` (владельца) | **наш** Traefik-контейнер в compose |
| Порты 80/443 на хост | держит внешний Traefik; наш стек НЕ публикует | держит **наш** Traefik-контейнер |
| Сеть `web` | external (создаётся вручную, общая с чужим Traefik) | **внутренняя** сеть compose (потребитель один — наш Traefik) |
| ACME/сертификаты | выпускает внешний Traefik | выпускает **наш** Traefik (acme.json в томе) |
| Домен | `broadnova.shop` и др. | `avorelio.shop` |
| `TRUSTED_PROXY_IPS` | подсеть внешней `web` | подсеть нашей proxy-сети |

## Решение

**Deploy-target №2 для claude-hermes = выделенный сервер `87.239.135.156` + self-hosted Traefik-контейнер в нашем `docker-compose.prod.yml` + GitHub Actions SSH-деплой. Каталог `/opt/claude-hermes`, домен `avorelio.shop`.**

### 1. Топология стека `/opt/claude-hermes` на `.156`

```mermaid
graph TD
    Internet["Интернет<br/>80/443"] --> Traefik["traefik (НАШ контейнер)<br/>ports 80→80, 443→443 на хост<br/>provider docker (exposedbydefault=false)<br/>ACME Let's Encrypt → acme.json (vol, 600)<br/>watch labels на api"]
    Traefik -->|HTTP по сети web| API["api: Gunicorn + UvicornWorker<br/>expose 8000, БЕЗ ports на хост<br/>docker.sock :ro (provision Hermes)"]
    API --> PG[("PostgreSQL 16<br/>сеть default, без портов")]
    API --> Redis[("Redis 7<br/>сеть default, без портов")]
    Migrate["migrate job<br/>alembic upgrade head"] -.pre-deploy.-> PG
    API -. provision docker.sock .-> Hermes["per-user Hermes<br/>hermes-user-&lt;id&gt;:8642<br/>сеть hermes-net (external)<br/>порт НЕ на хост"]
    subgraph stack["docker-compose.prod.yml (/opt/claude-hermes на .156)"]
        Traefik
        API
        PG
        Redis
        Migrate
    end
```

Состав стека:
- **traefik** — **наш** контейнер (`traefik:v3.x` pinned). Публикует `80:80` и `443:443` на хост (единственный сервис с `ports:`). Provider Docker (`--providers.docker --providers.docker.exposeddefault=false`), watch labels сервиса `api`. ACME Let's Encrypt (см. §2). `docker.sock` смонтирован `:ro` (provider читает Docker API). В сети `web` (для маршрута к `api`). Dashboard выключен (`--api=false`, §4).
- **api** — переиспользует **существующие** Traefik-labels (тот же контракт, что ADR-017: `Host(avorelio.shop)`, `entrypoints=websecure`, `tls.certresolver`, `loadbalancer.server.port=8000`), **без** публикации портов на хост (`expose: 8000`). `docker.sock` `:ro` для provision Hermes (ADR-046). Сети `web` + `default` + `hermes-net`.
- **postgres** / **redis** — только внутренняя сеть `default`, без публикации портов (как ADR-017).
- **migrate** — одноразовый job `alembic upgrade head` (как ADR-017).
- **Hermes per-user инстансы** — провижинятся control plane через `docker.sock` в `hermes-net` (ADR-046, без изменений), порт `8642` не на хост.

### 2. TLS — Traefik ACME Let's Encrypt (наш Traefik)

- **certResolver — `le`** (имя сохранено единым с ADR-017 для единообразия label-контракта; значение env `TRAEFIK_CERTRESOLVER=le`). Резолвер объявляется в **нашей** static-конфигурации Traefik (CLI-флаги в `command:`, см. ниже).
- **Challenge-тип — HTTP-01 (дефолт), обоснование:** для **одиночного** домена `avorelio.shop` HTTP-01 проще всего — требует лишь доступного порта 80 (наш Traefik его держит) и публичной A-записи; не нужны DNS-API-креды провайдера. TLS-ALPN-01 — рабочая альтернатива (challenge на 443, не требует 80), но даёт меньше выгод для одиночного домена и не нужен (мы и так держим 80 для HTTP→HTTPS redirect). DNS-01 не выбран (нужны API-креды DNS-провайдера; оправдан только для wildcard/закрытого 80). Выбор: **HTTP-01**.
- **acme.json** — хранится в **persistent named volume** `traefik-acme` (монтируется в `/letsencrypt`), путь резолвера `--certificatesresolvers.le.acme.storage=/letsencrypt/acme.json`. Traefik сам создаёт файл с правами `600` при первом выпуске (внутри тома, владелец — root внутри traefik-контейнера). Named volume (не bind-mount) выбран для переживания пересоздания контейнера без ручного `chmod` на хосте. Авто-обновление сертификата — встроено в Traefik (фоновое, до истечения).
- **ACME email** — env `ACME_EMAIL` (для уведомлений Let's Encrypt об истечении). Обязателен; пусто → fail-fast на старте Traefik (флаг `--certificatesresolvers.le.acme.email=${ACME_EMAIL}` с обязательной подстановкой `:?`).
- **Static-конфиг Traefik — CLI-флаги в `command:` сервиса (НЕ файл `traefik.yml`), обоснование:** для маленького одно-сервисного стека набор флагов мал и обозрим; держать его прямо в `docker-compose.prod.yml` (а не в отдельном файле + дополнительном bind-mount) — меньше артефактов, вся топология self-contained в одном файле, нет рассинхрона compose↔traefik.yml. Файловый `traefik.yml` оправдан при разрастании middlewares/провайдеров — это будущее упрощение, не нужно на старте. Выбор: **CLI-флаги в `command:`**. Динамическая конфигурация (маршруты/middlewares) — через docker-labels на `api` (provider docker), не в static-конфиге.

Минимальный набор static-флагов (спека для devops):
```
--providers.docker=true
--providers.docker.exposedbydefault=false
--entrypoints.web.address=:80
--entrypoints.websecure.address=:443
# HTTP→HTTPS redirect на entrypoint web (глобально), кроме pass-through ниже — см. §4:
--entrypoints.web.http.redirections.entrypoint.to=websecure
--entrypoints.web.http.redirections.entrypoint.scheme=https
--certificatesresolvers.le.acme.email=${ACME_EMAIL}
--certificatesresolvers.le.acme.storage=/letsencrypt/acme.json
--certificatesresolvers.le.acme.httpchallenge=true
--certificatesresolvers.le.acme.httpchallenge.entrypoint=web
--api=false        # dashboard выключен (§4)
# опц. наблюдаемость: --accesslog=true --log.level=INFO
```

### 3. Сети

- **`web`** — на выделенном сервере объявляется **внутри compose** (НЕ external), обоснование: единственный потребитель этой сети — **наш** Traefik-контейнер (нет внешнего edge-Traefik, который должен был бы заранее существовать и шарить сеть, как в ADR-017). Compose сам создаёт `<project>_web` и подключает к ней `traefik` + `api`. Это устраняет ручной предзапусковый `docker network create web` и привязку к чужой сети. (В ADR-017 `web` был `external: true` именно потому, что его создавал/владел чужой Traefik; здесь такого совладельца нет.)
  - **Механизм переопределения (зафиксировано devops, Docker Compose v5.0.2, [Q-057-1](../99-open-questions.md) Closed).** Базовый `docker-compose.prod.yml` объявляет `web` как `external: true` (топология №1 broadnova `.154`). Overlay-файл `.156` ([Q-057-1](../99-open-questions.md): отдельный overlay, напр. `docker-compose.traefik.yml`) переопределяет сеть на внутреннюю **только** явным `external: false`:
    ```yaml
    networks:
      web:
        external: false
    ```
    **Пустого блока `web: {}` НЕДОСТАТОЧНО** — при merge нескольких compose-файлов он **наследует** `external: true` из базового файла (пустой mapping не сбрасывает ранее заданный ключ). Требуется именно явный `external: false`, чтобы compose создал `<project>_web` локально. Это не трогает базовый файл → broadnova `.154` (деплой без overlay) сохраняет `external: true` (backward-compat, инвариант ADR-017 §12).
- **`default`** — внутренняя сеть стека для `api ↔ postgres/redis` (как ADR-017).
- **`hermes-net`** — **остаётся `external: true`** (инвариант [ADR-046](ADR-046-per-user-hermes-runtime.md)/[ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md)): control plane провижинит per-user Hermes-контейнеры **вне** compose-проекта (через docker-py), поэтому сеть обязана пред-существовать и быть видимой под плоским именем (без `<project>_`-префикса). Создаётся на сервере однократно: `docker network create hermes-net` (или per-instance имя из `HERMES_DOCKER_NETWORK`). Менять на внутреннюю **нельзя** — docker-py из control plane не увидит project-prefixed-сеть.
- **Размещение:** `traefik` — в `web` (+ для provider читает все контейнеры через `docker.sock`). `api` — в `web` + `default` + `hermes-net`. `postgres`/`redis` — только `default`. Hermes-инстансы — только `hermes-net`. На хост публикует порты **только** `traefik` (80/443).

### 4. Безопасность (детали — [05-security.md](../05-security.md), [07-deployment.md](../07-deployment.md))

- **Публикация портов — только traefik (80/443).** `api`/`postgres`/`redis`/Hermes — без `ports:` на хост; снаружи доступен только домен через наш Traefik.
- **`docker.sock` читают ДВА сервиса, оба `:ro`:** (1) `traefik` — Docker provider (service discovery по labels); (2) `api` — provision Hermes (ADR-046). **Риск docker.sock в traefik:** Docker provider читает Docker API ≈ **root на хосте даже при `:ro`** (read-only ограничивает запись в файл сокета, но не сам Docker API). Это та же повышенная привилегия, что у `api` (ADR-046/[05-security.md](../05-security.md#multi-tenant-изоляция-hermes-инстансов-adr-046-adr-045)). Митигация: Traefik читает только для discovery, не запускает контейнеры по своей логике; на выделенном сервере поверхность меньше (нет соседних сервисов); усиление — **socket-proxy** (напр. `tecnativa/docker-socket-proxy` с allowlist Docker API: только `CONTAINERS=1`/`NETWORKS=1` read-only для Traefik) зафиксировано как **задел** (не дефолт на старте; добавляется без изменения контракта при ужесточении).
- **Security-headers middleware Traefik (HSTS и пр.) — с обязательным исключением `/v1/preview/*`.** Глобальные security-заголовки (HSTS, `X-Frame-Options: DENY`, CSP) можно навесить через Traefik middleware **только на не-preview маршруты**. Контракт pass-through [ADR-010](ADR-010-backend-hosted-preview.md)/[07-deployment.md](../07-deployment.md) сохраняется: на префиксе `/v1/preview/*` Traefik **не навешивает** глобальные header-/cookie-middleware (не перетирать sandbox-`CSP: sandbox`, `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options`, `Cache-Control` приложения; не инжектить `Set-Cookie`). Реализация — отдельный router с более высоким приоритетом для `PathPrefix(/v1/preview/)` **без** security-middleware, либо вовсе не вешать глобальный headers-middleware (приложение само ставит HSTS/заголовки — [05-security.md §Транспорт](../05-security.md#транспорт)). Дефолт на старте: middleware **не** добавляется (приложение уже ставит HSTS/`nosniff`/`X-Frame-Options`), что by construction не трогает preview; HTTP→HTTPS redirect на entrypoint безопасен для preview (только смена схемы, не заголовки ответа).
- **Dashboard Traefik — выключен (`--api=false`).** Не публикуется. При необходимости включения позже — только за auth-middleware + не на публичном entrypoint (задел, не старт).
- **`TRUSTED_PROXY_IPS`** теперь = **подсеть нашей `web`-сети** (через неё наш Traefik проксирует на `api`). `docker network inspect <project>_web` → `IPAM.Config.Subnet`. Без этого `client_ip` = IP нашего Traefik → per-IP rate limit неработоспособен ([05-security.md](../05-security.md#доверенный-reverse-proxy-и-определение-client-ip-anti-spoofing)).
- **`root`-доступ по SSH из CI** — тот же риск, что ADR-017 (компрометация `SSH_PRIVATE_KEY`); ключ только в GitHub Secrets, ротация при подозрении.

### 5. CI/CD — GitHub Actions push main → SSH (`.156`)

- Workflow деплоя на `.156`: SSH (`appleboy/ssh-action`, `script_stop: false`) → `cd /opt/claude-hermes` → `git pull --ff-only` → `docker compose -f docker-compose.prod.yml --env-file .env build api migrate` → `run --rm migrate` → `up -d --no-build` → readiness-gate на health `claude-hermes-api-1` (compose healthcheck = `GET /ready`: db+redis; фактический health-путь подтверждён в [07-deployment.md §Health/readiness](../07-deployment.md#health--readiness): liveness `/health`/`/healthz`, readiness `/ready`) → NON-FATAL public smoke `https://avorelio.shop/healthz`. Тот же hardened-паттерн `set -uo pipefail` без `-e`, что ADR-017 (см. [07-deployment.md §Процедура деплоя](../07-deployment.md#процедура-деплоя-github-actions--ssh)).
- **GitHub Secrets:** `SSH_HOST_AVORELIO=87.239.135.156`, `SSH_USER=root`, `SSH_PRIVATE_KEY` (deploy-ключ; публичная половина — в `~/.ssh/authorized_keys` на `.156`). Имя секрета хоста **отдельное** от broadnova `SSH_HOST=87.239.135.154`, т.к. это другой сервер (не путать инстансы broadnova-loop с этим target-ом).
- **Где живёт `.env` — на сервере вручную (рекомендация), обоснование:** секреты кладутся в `/opt/claude-hermes/.env` на `.156` оператором из secret manager (как все инстансы ADR-017). Это проще (нет необходимости пробрасывать весь набор секретов через GitHub Secrets и материализовать `.env` на каждом деплое), безопаснее (секреты не проходят через CI-логи/окружение runner) и согласуется с действующим контрактом ([07-deployment.md §Конфигурация](../07-deployment.md#конфигурация-env): «Все секреты — из secret manager, не из plaintext `.env` в prod», на сервере). `.env` — в `.gitignore`, переживает `git pull`.
- **Rollback** — как ADR-017 (нет registry/immutable-tag): `cd /opt/claude-hermes` → `git checkout <prev-commit>` → `build api migrate` → (при необходимости) `run --rm migrate` → `up -d --no-build`. Traefik-контейнер при откате не меняется (его образ pinned, конфиг в compose).
- **Проверка `COVERAGE_CORE=sysmon` (вывод):** `env: COVERAGE_CORE: sysmon` в `ci.yml` job `test` (PEP 669 sys.monitoring) — мотивирован Windows-сегфолтом `--cov` settrace, но на Linux-runner GitHub Actions он **harmless и быстрее** (комментарий `ci.yml:67-72`), и авторитетным источником является `pyproject [tool.coverage.run] core="sysmon"` (job-env — belt-and-suspenders). **Изменение CI под `.156` для него НЕ требуется** — CI-jobs (`quality`/`test`/`build-image`) платформенно-нейтральны (Linux runner), `sysmon` не мешает Linux-прогону. Удалять его не нужно (не вредит); это операционный no-op для нового target-а.

### 6. Процедура первого деплоя (prod-checklist для `.156`)

Предзапусковые шаги на сервере `87.239.135.156` (root), ДО первого деплоя:
1. **DNS:** A-запись `avorelio.shop` → `87.239.135.156` существует (нужна для ACME HTTP-01 — порт 80 нашего Traefik должен быть достижим публично при выпуске).
2. **Сети:** `docker network create hermes-net` (external для control plane↔Hermes; `web` создаётся самим compose — НЕ создавать вручную).
3. **Том Hermes:** `mkdir -p /opt/data/hermes` (или значение `HERMES_VOLUME_ROOT`).
4. **Образ Hermes:** `docker pull nousresearch/hermes-agent:<pinned-tag>` (pinned, не `latest`).
5. **Код + `.env`:** клон репозитория в `/opt/claude-hermes`; `.env` из `.env.prod.example` со значениями (ниже); свежий RSA JWT keypair в `/opt/claude-hermes/.secrets/` (`chown 10001:10001`, приватный `640`, каталог `750`).
6. **Деплой:** CI push main (или ручной workflow) → build → migrate → up → readiness; первичный ACME-выпуск Traefik по `avorelio.shop` (HTTP-01, требует доступного 80).

`.env`-ключи (минимум; полный перечень — [07-deployment.md §Конфигурация](../07-deployment.md#конфигурация-env)):
- **Identity/routing:** `COMPOSE_PROJECT_NAME=claude-hermes`, `SERVICE_DOMAIN=avorelio.shop`, `TRAEFIK_CERTRESOLVER=le`, `ACME_EMAIL=<email>`, `JWT_ISSUER=https://avorelio.shop`, `JWT_AUDIENCE=claude-hermes`, `TRUSTED_PROXY_IPS=<подсеть нашей web>`.
- **Секреты:** `CLIENT_API_KEY`, `ADMIN_API_SECRET`, `PREVIEW_URL_SECRET`, `KMS_LOCAL_MASTER_KEY`, `METRICS_SCRAPE_TOKEN`, `ADAPTY_WEBHOOK_SECRET`; БД-роли `POSTGRES_USER/PASSWORD/DB` + `APP_RW_PASSWORD`/`APP_MIGRATE_PASSWORD` + `DATABASE_URL`(роль `app_rw`)/`DATABASE_URL_MIGRATE`(роль `app_migrate`) ([ADR-053](ADR-053-audit-logs-db-append-only.md)).
- **LLM:** `HERMES_LLM_PROVIDER` (валидный из allowlist образа, НЕ `openai`/`auto`) + `HERMES_MODEL` («голое» имя, непусто) + `HERMES_LLM_API_KEY` (секрет; соответствует провайдеру) [+ `HERMES_LLM_BASE_URL` для `custom`/`azure-foundry`] ([ADR-055](ADR-055-hermes-instance-llm-config-contract.md)); сервисный `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` по `LLM_PROVIDER` ([ADR-033](ADR-033-llm-provider-abstraction.md)).
- **Hermes runtime:** `HERMES_IMAGE=<pinned>`, `HERMES_DOCKER_NETWORK=hermes-net`, `HERMES_VOLUME_ROOT=/opt/data/hermes`, `HERMES_UID=10001`/`HERMES_GID=10001` ([ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md)).

### 7. Совпадение `COMPOSE_PROJECT_NAME` и пути с co-located `claude-hermes` на broadnova `.154` — НАМЕРЕННО (Вариант A)

INSTANCES-loop broadnova `.154` ([ADR-017 §Мульти-инстанс](ADR-017-shared-server-traefik-deploy.md), [07-deployment.md §CI/CD INSTANCES-loop](../07-deployment.md#cicd-контракт-instances-loop-мульти-инстанс)) уже содержит запись `claude-hermes:claude-hermes` (project-name `claude-hermes`, каталог `/opt/claude-hermes`). Новый deploy-target `.156` использует **те же** `COMPOSE_PROJECT_NAME=claude-hermes` и путь `/opt/claude-hermes`.

**Решение: оставить идентичные имя project и путь (Вариант A). Инстанс различается ХОСТОМ (`.154` vs `.156`), а не именем/путём.** Обоснование:
- **Физической коллизии нет.** Docker project-name изолирует ресурсы (сети/тома/контейнеры) **в пределах одного Docker daemon**; `.154` и `.156` — **разные хосты с разными daemon**, поэтому `claude-hermes_pgdata`/`claude-hermes_web`/контейнеры `claude-hermes-*` на каждом сервере свои и не пересекаются. Деплой-job-ы тоже не пересекаются: broadnova-loop ходит на `SSH_HOST=87.239.135.154`, target `.156` — на отдельный `SSH_HOST_AVORELIO=87.239.135.156` (§5). Один и тот же `git pull`/`build`/`up` на разных хостах независимы.
- **Семантика `COMPOSE_PROJECT_NAME` ([ADR-017 §Мульти-инстанс](ADR-017-shared-server-traefik-deploy.md)) сохранена.** На `.156` это **единственный** инстанс claude-hermes на хосте → `-p claude-ios`-инвариант обратной совместимости broadnova не затрагивается (другой хост). Имя project детерминированно соответствует каталогу basename (`/opt/claude-hermes`), что согласуется с приоритетом Compose (CLI `-p` > env > basename).
- **Инвариант Q-046-3 ([ADR-046](ADR-046-per-user-hermes-runtime.md)) соблюдён.** `HERMES_VOLUME_ROOT` и `HERMES_DOCKER_NETWORK` коллизируют только **на одном** daemon. На `.156` — единственный control plane → дефолты `/opt/data/hermes` + `hermes-net` корректны (как для одиночного инстанса). Per-instance-префикс пути/сети нужен только при **нескольких** control plane на **одном** daemon — не наш случай (`.156` выделен под claude-hermes). Тома/имена Hermes-контейнеров (`hermes-user-<id>`) на `.156` свои, с `.154` не пересекаются.
- **Вариант B (отдельный префикс `claude-hermes-avorelio` / путь `/opt/claude-hermes-avorelio`) отклонён.** Он не устраняет реального риска (его нет — разные хосты), но **добавляет** дивергенцию: потребовал бы правок `COMPOSE_PROJECT_NAME`, каталога на сервере, cd-пути в deploy-workflow и `.env.prod.example`, увеличивая поверхность рассинхрона docs↔инфра и риск регрессии. Цена выше выгоды.

**Остаточный риск — операционный footgun (перепутать инстансы оператору / в логах / в реестре INSTANCES), а не техническая коллизия.** Митигация — **дисциплина и явная маркировка**:
- Оператор различает инстансы **по хосту/домену**: broadnova co-located `claude-hermes` → `.154` (за внешним Traefik), dedicated `claude-hermes` → `.156` / `avorelio.shop` (наш Traefik).
- В deploy-логах/реестре INSTANCES `.156` идентифицируется доменом `avorelio.shop` (smoke-URL) и секретом `SSH_HOST_AVORELIO`; broadnova-loop — доменом co-located инстанса и `SSH_HOST=...154`.
- **Запись `.156` НЕ добавляется в broadnova INSTANCES-loop** (тот ходит на `.154`) — это разные deploy-job-ы/хосты.

**Доп. правка devops для Варианта A НЕ требуется** (имя/путь/`.env`-ключи уже зафиксированы в §6; меняется только хост-секрет `SSH_HOST_AVORELIO`, уже в §5 и prod-checklist).

## Совместимость с действующими ADR (инварианты не нарушены)

- **ADR-017 не отменён:** он остаётся каноном для broadnova-сервера `.154` (внешний Traefik). ADR-057 — **другой** deploy-target (`.156`, наш Traefik). Существующий label-контракт `api` (`Host(${SERVICE_DOMAIN})`/`entrypoints=websecure`/`tls.certresolver=${TRAEFIK_CERTRESOLVER}`/`loadbalancer.server.port=8000`) **переиспользуется без изменений** — наш Traefik на `.156` его читает так же, как внешний на `.154`. Тот же базовый `docker-compose.prod.yml` обслуживает оба target-а; различие в наличии сервиса `traefik` и природе сети `web` (internal vs external) разрешено overlay-файлом `.156` с явным `networks.web.external: false` (§3, [Q-057-1](../99-open-questions.md) Closed) — базовый файл не меняется, `.154` не регрессирует. Имя project/путь `claude-hermes` совпадает с co-located инстансом `.154` намеренно (§7).
- **ADR-046 / ADR-056:** `hermes-net` остаётся `external: true`; `docker.sock` в `api` `:ro`; `HERMES_UID/GID=10001`; readiness-gate (`HERMES_PROVISION_READY_TIMEOUT_SECONDS`<`HERMES_PROVISIONING_STALE_SECONDS`); idempotent `config.yaml` — **без изменений**. Транзакционные/lifecycle-инварианты control plane (короткие registry-транзакции, readiness-poll после commit `provisioning`-строки, [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md)) не затрагиваются deploy-топологией.
- **ADR-053:** раздельные роли БД `app_rw`/`app_migrate` и два DSN — как на любом инстансе (init-скрипт на свежем томе либо ручная процедура).
- **ADR-010 preview pass-through:** сохранён — §4 явно исключает `/v1/preview/*` из глобальных Traefik header/cookie-middleware.

## Последствия

**Плюсы:**
- Полная автономия `claude-hermes` на выделенном сервере: нет зависимости от чужого edge-Traefik и его конфигурации/доступности.
- TLS/ACME под нашим контролем (резолвер, email, обновление) — в одном compose-файле.
- `web` как внутренняя сеть compose устраняет ручной `docker network create web` (на выделенном сервере он не нужен).
- Тот же код/репозиторий/CI-паттерн, что и для broadnova — повторное использование.

**Минусы / риски:**
- Теперь **мы** держим 80/443 и отвечаем за TLS — выпуск/обновление сертификата (HTTP-01 требует доступного 80 и валидной A-записи; первый выпуск может «устаканиваться» — smoke NON-FATAL это учитывает).
- **`docker.sock` теперь читают ДВА сервиса** (traefik + api) — расширение поверхности привилегии; митигация — `:ro`, выделенный сервер, socket-proxy как задел.
- Сборка образа на сервере при каждом деплое (нет registry/immutable-tag) — как ADR-017.
- Два разных `SSH_HOST`-секрета (broadnova `.154` vs avorelio `.156`) — операционная дисциплина, чтобы не задеплоить не на тот сервер.
- **Двойственность `docker-compose.prod.yml`** (со `traefik`-сервисом и internal-`web` для `.156` vs без него и external-`web` для `.154`): **разрешена** ([Q-057-1](../99-open-questions.md) Closed) — отдельный overlay-файл `.156` (напр. `docker-compose.traefik.yml`) добавляет сервис `traefik` + acme volume и переопределяет сеть `web` явным `external: false` (§3; пустой `web: {}` недостаточно — наследует `external:true` базового файла). Базовый файл не трогается → broadnova `.154` (деплой без overlay) не регрессирует. Остаётся операционная сложность поддержки двух наборов файлов.
- **Совпадение имени project/пути `claude-hermes` с co-located инстансом broadnova `.154`** — намеренно (§7, Вариант A): техническая коллизия отсутствует (разные хосты/daemon, отдельные SSH-секреты), но остаётся операционный footgun — митигация дисциплиной (различать инстансы по хосту/домену, не путать в реестре INSTANCES).

## Альтернативы

- **Внешний Traefik на `.156` (как `.154`/ADR-017).** Отклонено: на выделенном сервере нет чужого edge-Traefik и нет других сервисов, ради которых стоило бы выносить прокси наружу нашего стека; self-hosted в compose проще операционно (один артефакт, один деплой).
- **Caddy/nginx self-hosted вместо Traefik.** Отклонено: Traefik уже наш label-контракт (ADR-017) — переиспользуем те же labels на `api` без изменения; авто-ACME из коробки; единообразие с broadnova.
- **TLS-ALPN-01 / DNS-01 challenge.** TLS-ALPN-01 — рабочая альтернатива (не требует 80), но для одиночного домена выгод не даёт (80 и так держим под redirect). DNS-01 — нужен только для wildcard/закрытого 80; требует DNS-API-кредов. Выбран HTTP-01.
- **Файловый `traefik.yml` static-конфиг.** Отклонено на старте: малый набор флагов компактнее держать в `command:`; файл оправдан при разрастании конфигурации (будущее).
- **acme.json bind-mount на хост.** Отклонено в пользу named volume (переживает пересоздание контейнера без ручного `chmod 600` на хосте).
