# ADR-065 — Патченый образ Hermes-runtime из ghcr.io (`usage.delta`), digest-pinned, off-server сборка/публикация

- **Статус:** Accepted (2026-07-15)
- **Связан с:** [ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md) (**зависимость образа**: incremental-биллинг требует `usage.delta` + hydrate, §7 ADR-064), [ADR-046](ADR-046-per-user-hermes-runtime.md) (**ревизует источник `HERMES_IMAGE`**: upstream → наш патченый образ), [ADR-057](ADR-057-dedicated-server-self-hosted-traefik-deploy.md) (deploy `.156`: pull образа, доступ к registry), [ADR-055](ADR-055-hermes-instance-llm-config-contract.md)/[ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md) (runtime-контракт инстанса — **без изменений**), [07-deployment.md §Hermes runtime](../07-deployment.md#hermes-runtime--деплой-per-user-инстансов-adr-046-adr-045), [modules/hermes-runtime/](../modules/hermes-runtime/README.md)
- **Контракт данных:** нет (миграций нет)
- **Контракт API:** нет нового claude-hermes-endpoint; фиксируется внешний контракт **образа** Hermes (событие `usage.delta`, hydrate `GET /api/sessions/{id}/messages` — определены в [ADR-064 §7](ADR-064-incremental-agent-run-billing-and-pause-resume.md))
- **Инфра-инвариант:** `HERMES_IMAGE = ghcr.io/eliseiv/hermes-agent@sha256:<digest>` (digest-pin, НЕ `:latest`, НЕ плавающий тег)

## Context

[ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md) (пошаговый биллинг agent-run, pause/resume) **не исполним на upstream-образе**: он требует, чтобы Hermes-инстанс

1. эмитил per-step событие `usage.delta` наружу в SSE (кумулятивные `cumulative_*_tokens` — источник incremental-тарификации, [ADR-064 §7](ADR-064-incremental-agent-run-billing-and-pause-resume.md)); upstream отдаёт `usage` **только** в терминальном `run.completed`;
2. отдавал историю сессии `GET /api/sessions/{session_id}/messages` (hydrate для continuation-resume, [ADR-064 §5](ADR-064-incremental-agent-run-billing-and-pause-resume.md)).

Публичный upstream **`nousresearch/hermes-agent`** (Docker Hub) этих возможностей **не имеет**. Патч реализован в рабочей копии Hermes (`D:\BA\hermes`), **факты верифицированы по коду**:
- `agent/conversation_loop.py` (эмиссия `usage.delta` per LLM-вызов внутри tool-loop — `agent/conversation_loop.py:2061`, вызов `tool_progress_callback("usage.delta", …)`);
- `gateway/platforms/api_server.py` (проброс `usage.delta` в SSE-очередь — `gateway/platforms/api_server.py:4485`, ветка `elif event_type == "usage.delta": _push({…})`).

**Push в чужой namespace `nousresearch/*` невозможен** (не наш аккаунт registry). Требуется **наш** registry для патченого образа. **Пользователь выбрал** GitHub Container Registry **`ghcr.io`**, namespace **`eliseiv`** (совпадает с репозиторием сервиса `github.com:eliseiv/claude-hermes`).

**Действующий инвариант расходится с требованием ADR-064.** [ADR-046](ADR-046-per-user-hermes-runtime.md) / [07-deployment.md](../07-deployment.md) / [`.env.prod.example`](../../.env.prod.example) фиксировали: «`HERMES_IMAGE` — **публичный upstream** `nousresearch/hermes-agent`, тянется из registry; сборки из внешних исходников Hermes на сервере нет (самодостаточность `.156`)». В части **источника** образа это теперь неверно — upstream не несёт патч. Настоящий ADR устраняет расхождение docs↔ADR-064.

**Лицензия Hermes — MIT** (verified `D:\BA\hermes\LICENSE`, «Copyright (c) 2025 Nous Research», MIT). MIT явно разрешает **модификацию и распространение** (в т.ч. публикацию собственного образа) при сохранении copyright/permission notice. Значит публичный ghcr-пакет патченого образа **лицензионно допустим** (notice сохраняется — LICENSE в дереве, из которого собирается образ).

## Decision

### 1. `HERMES_IMAGE` = наш патченый образ `ghcr.io/eliseiv/hermes-agent`, digest-pinned

- Источник runtime-образа Hermes-инстансов меняется с upstream `nousresearch/hermes-agent` (Docker Hub) на **наш** `ghcr.io/eliseiv/hermes-agent`.
- **Инвариант:** `HERMES_IMAGE = ghcr.io/eliseiv/hermes-agent@sha256:<digest>` — **digest-pin** (`@sha256:…`), **НЕ** `:latest`, **НЕ** плавающий тег. Digest (а не только читаемый тег вроде `:usage-delta-v1`) обязателен именно потому, что образ теперь **наш и мутабелен** (мы можем перетегировать `usage-delta-v1`): digest фиксирует **точные биты**, прошедшие e2e, — воспроизводимость provision. Тег `:usage-delta-v<N>` служит человекочитаемой меткой публикации, но в `HERMES_IMAGE` записывается **digest**.
- `docker-py` `containers.run(image="ghcr.io/eliseiv/hermes-agent@sha256:…")` авто-pull'ит по digest при отсутствии локально (как и раньше). Дефолт `HERMES_IMAGE=''` → provision **fail-fast** (инвариант [ADR-046](ADR-046-per-user-hermes-runtime.md)/[07-deployment.md](../07-deployment.md), без изменений).

### 2. Инвариант «no build on server» СОХРАНЁН и усилен: off-server сборка

- Патченый образ собирается **OFF-server** — на dev-машине или в CI (не на `.156`), **публикуется в ghcr**, а `.156` только `docker pull`'ит его по digest.
- На `.156` по-прежнему **нет** сборки из исходников Hermes и **нет** самого дерева `D:\BA\hermes`: на prod-сервер деплоится **только** репозиторий `claude-hermes`. Самодостаточность деплоя `.156` ([07-deployment.md §Артефакт](../07-deployment.md#артефакт)) **не нарушена** — меняется лишь **registry-источник** (ghcr/eliseiv вместо Docker Hub/nousresearch) и **природа** runtime-образа (наш патч-форк вместо upstream-образа). Инварианты runtime-контракта инстанса ([ADR-055](ADR-055-hermes-instance-llm-config-contract.md) config.yaml, [ADR-056](ADR-056-provision-readiness-gate-and-volume-ownership.md) readiness/владение томом, toolset [ADR-046 §6](ADR-046-per-user-hermes-runtime.md)) — **без изменений** (патч аддитивен: добавляет SSE-событие + read-эндпоинт, ничего в контракте provision/env/config.yaml не трогает).

### 3. Версионирование патча — Вариант **B**: diff-патч в репо claude-hermes

Рекомендация: **B — хранить diff-патч `infra/hermes-patches/usage-delta.patch` в репозитории `claude-hermes`**, применяемый на **pinned upstream-базе** при сборке.

Обоснование:
- Патч — **минимальная ревьюабельная дельта** (2 хунка: `conversation_loop.py` + `api_server.py`). Diff — самое компактное и точное представление именно того, что мы изменили; в code review виден ровно наш вклад, не 5-GB зеркало.
- **Ко-локация** патча с единственным потребителем (`claude-hermes`) и с [ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md)/ADR-065 → один источник истины docs↔патч↔build-recipe в одном репо.
- **Воспроизводимость:** pin upstream-базы (commit SHA / release-тег upstream) + патч + build-recipe (`clone upstream@<base>` → `git apply infra/hermes-patches/usage-delta.patch` → `docker build --platform linux/amd64` → push ghcr) → итоговый **digest** пиннится в `HERMES_IMAGE`.
- **Дешёвый upstream-sync:** обновление = бампнуть pinned-базу + ре-применить патч; конфликт `git apply` — явный сигнал, что патч разошёлся с upstream (обрабатывается как maintenance-долг, [TD-036](../100-known-tech-debt.md)).

**Caveat (bootstrap-условие Варианта B):** патч должен генерироваться против **известной** upstream-базы. `D:\BA\hermes` сейчас **без git**, поэтому нужно (1) определить, какому upstream-commit/release соответствует рабочая копия, (2) `diff` двух патченных файлов против upstream@base → `usage-delta.patch`, применяющийся **чисто**. Если провенанс upstream-базы неоднозначен и чистое применение не гарантируется — используется **фолбэк A** (см. Alternatives): snapshot текущего дерева `D:\BA\hermes` как самостоятельный build-source (git init → отдельный репо `github.com:eliseiv/hermes-agent-fork`). Подтверждение базы — [Q-046-4](../99-open-questions.md) (не блокер: при неопределённости берётся фолбэк A).

### 4. Модель доступа `.156` к ghcr — публичный пакет (default; MIT позволяет)

- **Default — публичный ghcr-пакет.** `ghcr.io/eliseiv/hermes-agent` публикуется как **public** → Docker daemon `.156` (и docker-py auto-pull при provision) тянет **анонимно, без креденшелов на сервере**. Это паритет с прежней public-upstream-моделью (минимум операционных секретов на prod) и лицензионно допустимо (MIT, §Context).
- **Альтернатива (если образ должен остаться приватным):** private-пакет + `docker login ghcr.io -u eliseiv` под **root** на `.156` токеном с областью **`read:packages`** → креды персистятся в `/root/.docker/config.json`, docker-py auto-pull при provision подхватывает их из daemon-конфига. Минус — дополнительный секрет на `.156` (хранение/ротация); плюс — образ не публичен.
- Финальный выбор visibility — [Q-046-4](../99-open-questions.md) (**default = public**, реализация не блокируется).

### 5. Процедура сборки+публикации (спека для devops)

Выполняется **off-server** (dev/CI), НЕ на `.156`:
1. `git apply` `infra/hermes-patches/usage-delta.patch` на upstream@`<pinned-base>` (Вариант B) **или** сборка из snapshot-форка (фолбэк A).
2. `docker build --platform linux/amd64 -t ghcr.io/eliseiv/hermes-agent:usage-delta-v<N> .` (обязательно `linux/amd64` — целевой daemon `.156` amd64).
3. `docker login ghcr.io -u eliseiv` токеном с областью **`write:packages`** (`GITHUB_TOKEN`/PAT; хранить off-server, не в репо, не на `.156`).
4. `docker push ghcr.io/eliseiv/hermes-agent:usage-delta-v<N>` → зафиксировать **digest** из вывода push (`ghcr.io/eliseiv/hermes-agent@sha256:<digest>`).
5. (Вариант B) сделать пакет public в настройках ghcr (§4 default) — либо оставить private + подготовить `read:packages`-токен для `.156`.

### 6. Доставка на `.156` + rollout (спека для devops; последовательность обязательна)

1. `docker pull ghcr.io/eliseiv/hermes-agent@sha256:<digest>` на `.156` (предзагрузка; при public-пакете — анонимно, при private — после `docker login`).
2. Записать `HERMES_IMAGE=ghcr.io/eliseiv/hermes-agent@sha256:<digest>` в `/opt/claude-hermes/.env`.
3. Пересоздать `api` (split-up `--force-recreate api`, [07-deployment.md §Процедура деплоя](../07-deployment.md#процедура-деплоя-github-actions--ssh-156)) — control plane подхватывает новый `HERMES_IMAGE`.
4. **Re-provision per-user инстансов** под новый образ: `deprovision` → `provision` (том `HERMES_HOME` **сохраняется**, [ADR-046 §1](ADR-046-per-user-hermes-runtime.md) — память/навыки не теряются). Инстансы, не пересозданные, продолжат работать на старом образе до следующего wake/provision.
5. **Включение `agent_incremental_billing_enabled` — ТОЛЬКО ПОСЛЕ** (а) образа на `.156` + (б) зелёного e2e агентного пути, где `usage.delta` реально наблюдается в SSE. Флаг OFF безопасен (постфактум-биллинг [ADR-047](ADR-047-usage-based-billing-for-agent.md), [ADR-064 §Decision](ADR-064-incremental-agent-run-billing-and-pause-resume.md)) и остаётся дефолтом до подтверждения.

## Consequences

**Положительные:**
- [ADR-064](ADR-064-incremental-agent-run-billing-and-pause-resume.md) становится **исполнимым**: `usage.delta` + hydrate присутствуют в runtime-образе.
- **Digest-pin** → воспроизводимость provision (точные протестированные биты), устойчивость к перетегированию нашего мутабельного образа.
- **Off-server сборка** сохраняет самодостаточность `.156` (дерево Hermes на prod не попадает; deploy тянет только `claude-hermes`-репо).
- Публичный ghcr-пакет (MIT) — паритет с прежней моделью: **нет нового секрета на `.156`**.

**Отрицательные / ограничения:**
- **Maintenance-долг**: наш патч нужно ре-применять/ресинхронизировать при обновлениях upstream Hermes — [TD-036](../100-known-tech-debt.md).
- **Ручная (на старте) сборка/публикация** образа — новый операционный шаг **вне** CI `claude-hermes` (авто-сборка образа в CI — задел, не старт; кандидат в отдельный workflow).
- **ghcr — новая внешняя зависимость доступности** pull на `.156` (при недоступности registry provision нового инстанса упадёт на pull; уже предзагруженные образы кэшируются в daemon).
- Двойной источник документации ссылок на образ (upstream в immutable-телах ADR-046/057 vs ghcr в ревизиях/playbook) — снят ревизиями + INDEX-пометками.

## Alternatives

1. **A — snapshot-форк (`git init D:\BA\hermes` → push `github.com:eliseiv/hermes-agent-fork`).** Гарантирует точные биты, служит фолбэком Варианта B при неопределённой upstream-базе. Отклонён как **основной**: тяжёлый репо (полное дерево/контекст ~ГБ), теряет крисп-вью «только наш delta», дублирует upstream. **Оставлен как фолбэк** к §3 B.
2. **C — полный поддерживаемый форк-зеркало upstream.** То же, что A, плюс бремя поддержки полного зеркала и ручного мерджа upstream. Избыточно для 2-хунк-патча.
3. **Оставить upstream + рантайм-shim в control plane (без патча образа).** Невозможно: per-step usage существует только **внутри** loop Hermes и наружу не эмитится; без патча образа `usage.delta` неоткуда взять ([ADR-064 §7](ADR-064-incremental-agent-run-billing-and-pause-resume.md)). Control plane не может синтезировать точный per-step usage.
4. **Build патченого образа на `.156` из исходников Hermes.** Отклонён: нарушает самодостаточность (пришлось бы тащить дерево Hermes на prod-сервер), медленно (~5 ГБ образ), расширяет поверхность prod-сервера. Off-server build + pull проще и безопаснее.
5. **Другой registry (Docker Hub `eliseiv/*`, приватный self-hosted).** Отклонён: пользователь выбрал ghcr.io/eliseiv (единый namespace с репо сервиса, интеграция с GitHub-токенами/CI).
