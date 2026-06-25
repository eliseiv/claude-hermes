# ADR-055 — Контракт конфигурации LLM Hermes-инстанса (config.yaml `model`-секция, валидация провайдера, env-набор)

- Статус: Accepted
- Дата: 2026-06-24 (ревизия §6 — закрытие [Q-055-1](../99-open-questions.md): 2026-06-25)
- Расширяет / частично ревизует: [ADR-046](ADR-046-per-user-hermes-runtime.md) §1 (env-проброс), §5/§6 (рендер `config.yaml` тома). Тело ADR-046 не переписано (immutability) — пометка добавлена в шапку ADR-046, контракт `config.yaml`/env уточняется здесь.
- Связан с: [ADR-033](ADR-033-llm-provider-abstraction.md) (LLM-абстракция control plane — НЕ путать с провайдером инстанса), [ADR-045](ADR-045-hermes-as-agent-proxy.md) (agent-proxy), [ADR-003](ADR-003-byok-envelope-encryption.md), [05-security.md](../05-security.md), [07-deployment.md](../07-deployment.md), [modules/hermes-runtime/](../modules/hermes-runtime/README.md)
- needs_code_sync: `src/app/hermes_runtime/config_yaml.py` (новая сигнатура `render_instance_config` + `model`-секция + валидация значений + **§6: параметр `api_key`, эмиссия `model.api_key="${HERMES_INSTANCE_LLM_KEY}"` для провайдеров из `HERMES_PROVIDERS_CONFIG_API_KEY`**), `src/app/hermes_runtime/manager.py::_container_env` / `_provision_locked` (передача provider/model/base_url/**api_key** в рендер, удаление `LLM_MODEL` env, fail-fast валидация провайдера, **§6: для config-api-key провайдеров — env `HERMES_INSTANCE_LLM_KEY` вместо `<PROVIDER>_API_KEY`**), `src/app/config.py` (дефолт `hermes_llm_provider`, новое поле `hermes_llm_base_url`, валидатор провайдера, **§6: набор `HERMES_PROVIDERS_CONFIG_API_KEY = {"custom"}`**)

## Context

Полный локальный e2e подтвердил: control plane корректно провижинит per-user Hermes-контейнеры (изоляция/auth/admin/subscription/billing работают, БД пишется), но **реальный Hermes-инстанс падает с HTTP 401 от LLM**: «Provider: openrouter, Model: (пусто), Missing Authentication header».

### Корневая причина (подтверждена чтением кода control plane и образа Hermes)

1. **`render_instance_config(toolset)` не задаёт секцию `model`.** Текущий рендер (`config_yaml.py`) пишет в `config.yaml` инстанса ТОЛЬКО `platform_toolsets.api_server` + `approvals.mode: deny`. Без секции `model` инстанс берёт **встроенный дефолт** cli-config (`D:\BA\hermes\cli-config.yaml.example`, строки 8–46): `model.default: "anthropic/claude-opus-4.6"`, `model.provider: "auto"`, `model.base_url: "https://openrouter.ai/api/v1"`. `provider: "auto"` + openrouter `base_url` → инстанс дефолтит на OpenRouter и требует `OPENROUTER_API_KEY` (которого нет) → 401.

2. **Hermes НЕ читает модель из env `LLM_MODEL`.** `D:\BA\hermes\.env.example` (строки 12–15): *«LLM_MODEL is no longer read from .env»* — модель/провайдер берутся из `config.yaml` (`model.*`). Текущий `manager.py::_container_env` прокидывает `LLM_MODEL=<HERMES_MODEL>`, который инстанс **игнорирует** → модель остаётся дефолтной из cli-config.

3. **Дефолт `HERMES_LLM_PROVIDER=openai` невалиден.** У Hermes НЕТ direct-провайдера `"openai"` (см. валидный набор ниже). Прокидывание `LLM_PROVIDER=openai` + `OPENAI_API_KEY=<key>` не даёт рабочей конфигурации модели (модель всё равно из `config.yaml` дефолта). До этой ревизии `src/app/config.py::hermes_llm_provider` имел дефолт `anthropic` в коде, но e2e-`.env` выставлял `openai` — что и привёл бы к невалидному `model.provider` после фикса. Фиксируем валидный набор и fail-fast.

### Валидные провайдеры Hermes (`D:\BA\hermes\cli-config.yaml.example`, строки 13–39)

`auto, openrouter, nous, nous-api, anthropic, openai-codex, copilot, gemini, zai, kimi-coding, minimax, minimax-cn, huggingface, nvidia, xiaomi, arcee, ollama-cloud, kilocode, azure-foundry, lmstudio, custom`.

- direct-провайдера `"openai"` **НЕТ**. OpenAI-совместимый Chat API доступен через `openrouter` (`OPENROUTER_API_KEY`/`OPENAI_API_KEY`) ИЛИ `custom` (OpenAI-compatible `base_url` + ключ).
- `anthropic` → `ANTHROPIC_API_KEY` (direct).
- Формат `config.yaml`: `model.default: "<provider>/<model>"`, `model.provider: "<provider>"`, `model.base_url` (**обязателен** для `custom`/`azure-foundry` — нет дефолтного endpoint; **опционален** для `lmstudio` — дефолт образа `http://127.0.0.1:1234/v1` — и прочих провайдеров с дефолтным endpoint).

## Decision

### 1. `render_instance_config` задаёт секцию `model` в `config.yaml` инстанса

Новая сигнатура (заменяет `render_instance_config(toolset)`):

```python
def render_instance_config(
    *,
    toolset: list[str],
    provider: str,
    model: str,
    base_url: str = "",
) -> str:
```

Рендерит дополнительно к существующим `platform_toolsets.api_server` + `approvals.mode` (НЕ ослабляются) секцию:

```yaml
model:
  default: "<provider>/<model>"
  provider: "<provider>"
  base_url: "<base_url>"   # строка эмитится ТОЛЬКО если base_url непустой
```

- `provider` — **КОНКРЕТНЫЙ** провайдер (НЕ `"auto"`): `auto` вернул бы openrouter `base_url` из дефолта → 401.
- `default` собирается control plane как `"<provider>/<model>"` (см. §3).
- `base_url` строка эмитится только когда `base_url` непустой (его задают провайдеры без дефолтного endpoint — `custom`/`azure-foundry`, а также опционально `lmstudio`, если оператор переопределяет дефолт образа) — пусто → строка отсутствует (Hermes подставит провайдер-дефолт base_url, напр. `lmstudio` → `http://127.0.0.1:1234/v1`).

**Safe-инъекция YAML (как у toolset).** Значения `provider`/`model`/`base_url` валидируются к консервативному charset перед эмиссией (см. §2): `provider` — по allowlist; `model` — по `^[A-Za-z0-9._/\-]+$` (точки/слэш для `provider/model`-id и дат-суффиксов вроде `-latest`); `base_url` — `^https?://[A-Za-z0-9._:/\-]+$`. Невалидное значение → провижининг падает (fail-fast, §2), а не эмитит сломанный/инъектированный YAML. Рендер остаётся hand-rendered (без YAML-либы), детерминированным и тестируемым.

### 2. Валидный набор `HERMES_LLM_PROVIDER` + fail-fast валидация

Closed-set allowlist (в коде, источник — `cli-config.yaml.example` образа):

```
auto, openrouter, nous, nous-api, anthropic, openai-codex, copilot, gemini,
zai, kimi-coding, minimax, minimax-cn, huggingface, nvidia, xiaomi, arcee,
ollama-cloud, kilocode, azure-foundry, lmstudio, custom
```

- `"openai"` — **невалиден** (нет direct-провайдера); попытка задать → fail-fast с понятной ошибкой.
- `"auto"` формально входит в набор образа, но control plane его **запрещает для провижининга** (он реанимирует баг: openrouter base_url по умолчанию). Запрещаем `auto` на нашей стороне → требуем конкретный провайдер.
- **Fail-fast при провижининге** (выбран против маппинга): `_require_provision_config` (`manager.py`) дополнительно проверяет, что `hermes_llm_provider` ∈ allowlist (и ≠ `auto`). Невалидно → `UpstreamError("hermes runtime is not configured (HERMES_LLM_PROVIDER=<v> invalid; allowed: ...)")` — **до** `docker run`. Обоснование: провижининг падает рано с понятной ошибкой в логах control plane, а не отдаёт непрозрачный 401 в рантайме инстанса. Маппинг (`openai → openrouter`) отвергнут: скрывает неверную конфигурацию, неоднозначен по ключу (`OPENAI_API_KEY` vs `OPENROUTER_API_KEY`).
- Для провайдеров, требующих `base_url` (`custom`/`azure-foundry` — у них **нет** дефолтного endpoint), при пустом `HERMES_LLM_BASE_URL` — также fail-fast (иначе инстанс не знает endpoint). Набор в коде: `HERMES_PROVIDERS_REQUIRING_BASE_URL = {custom, azure-foundry}`. **`lmstudio` сюда НЕ входит:** у образа есть дефолтный endpoint `http://127.0.0.1:1234/v1`, поэтому base_url для `lmstudio` **опционален** (см. §4 таблица). (Согласовано: §2/§4 и module-02 едины — required-base_url только для провайдеров без дефолтного endpoint.)

### 3. Формат `HERMES_MODEL` — «голое» имя модели

Выбран **формат «bare model»**: `HERMES_MODEL` содержит ТОЛЬКО имя модели (напр. `claude-3-5-haiku-latest`), БЕЗ префикса провайдера. Control plane собирает `model.default = "<HERMES_LLM_PROVIDER>/<HERMES_MODEL>"`.

Обоснование (против «`provider/model` в одном поле»):
- Провайдер уже задан отдельно (`HERMES_LLM_PROVIDER` → `model.provider` + ключ-env). Хранить провайдер дважды (в `HERMES_LLM_PROVIDER` и внутри `HERMES_MODEL`) → риск рассинхрона (`model.provider: anthropic`, а `default: openrouter/...`).
- Hermes ждёт `model.default` в формате `"provider/model"` (cli-config пример: `anthropic/claude-opus-4.6`) — control plane собирает его детерминированно из двух согласованных источников.
- Если `HERMES_MODEL` пуст — провижининг fail-fast (модель обязательна; пустой `default` = текущий баг «Model: (пусто)»). `HERMES_MODEL` перестаёт иметь дефолт `''` как «валидный» (см. §5 deployment).

Edge: если оператор всё же впишет `HERMES_MODEL="<provider>/<model>"` со слэшем — charset `model` (§1) слэш допускает, но сборка даст `default: "<provider>/<provider>/<model>"` (двойной префикс). Поэтому в docs (07-deployment) явно фиксируется: **`HERMES_MODEL` — без префикса провайдера**.

### 4. Финальный env-набор контейнера (`manager._container_env`)

```
API_SERVER_ENABLED = "true"
API_SERVER_KEY     = <CSPRNG, per-instance>
API_SERVER_HOST    = "0.0.0.0"
API_SERVER_PORT    = "8642"
# Провайдер с env-ключом (anthropic, openrouter, …):
<PROVIDER>_API_KEY      = <HERMES_LLM_API_KEY>     # имя по провайдеру, см. таблицу
# Провайдер без env-ключа (custom ∈ HERMES_PROVIDERS_CONFIG_API_KEY):
HERMES_INSTANCE_LLM_KEY = <HERMES_LLM_API_KEY>     # env-ref для config.yaml model.api_key (§6); <PROVIDER>_API_KEY НЕ передаётся
```

Изменения относительно прежнего набора:
- **`LLM_MODEL` — УДАЛЁН** (Hermes его игнорирует; модель — только через `config.yaml` `model.default`).
- **`LLM_PROVIDER` env — УДАЛЁН как канал выбора провайдера.** Основной и единственный канал провайдера — `config.yaml` `model.provider`. Hermes резолвит провайдер из `config.yaml`; дублировать в env незачем и рискованно (рассинхрон с `model.provider`). (`.env.example` образа не документирует `LLM_PROVIDER` как читаемый источник модели; провайдер берётся из config.yaml `model.provider`.)
- **Ключ — через `<PROVIDER>_API_KEY`**, имя env-переменной ключа определяется провайдером:

| `HERMES_LLM_PROVIDER` | env-переменная ключа | `model.base_url` |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | — (direct) |
| `openrouter` | `OPENROUTER_API_KEY` | — (дефолт образа) |
| `gemini` | `GOOGLE_API_KEY` | — |
| `nous-api` | `NOUS_API_KEY` | — |
| `zai` | `GLM_API_KEY` | — |
| `kimi-coding` | `KIMI_API_KEY` | — |
| `huggingface` | `HF_TOKEN` | — |
| `nvidia` | `NVIDIA_API_KEY` | — |
| `custom` | **нет env-ключа** → ключ в `config.yaml model.api_key` (§6); env-ref `HERMES_INSTANCE_LLM_KEY` | **обязателен** `HERMES_LLM_BASE_URL` |
| `lmstudio` | `LM_API_KEY` (опц.) | опц. (дефолт `http://127.0.0.1:1234/v1`) |
| `azure-foundry` | по auth-mode (key или Entra) | **обязателен** |

Примечание по имени ключа: для большинства провайдеров оно НЕ совпадает с `<PROVIDER.upper()>_API_KEY` (напр. `gemini → GOOGLE_API_KEY`, `huggingface → HF_TOKEN`). Поэтому имя env-переменной ключа берётся из **явной map провайдер→key-env** (в коде, источник — `cli-config.yaml.example`/`.env.example` образа), а НЕ из `f"{provider.upper()}_API_KEY"`.

**Провайдеры без env-ключа (`custom`) — ключ через `config.yaml`, НЕ через env.** Закрыто [§6](#6-провайдеры-без-env-ключа-custom--ключ-через-configyaml-закрытие-q-055-1) ([Q-055-1](../99-open-questions.md) — Resolved). Подтверждено чтением образа (`D:\BA\hermes\plugins\model-providers\custom\__init__.py:65` — `env_vars=()`; `D:\BA\hermes\hermes_cli\config.py:3991` — «`model.api_key` valid only for explicit custom endpoint … Built-in providers resolve credentials from env vars»): провайдер `custom` **НЕ** читает ключ из env (`CUSTOM_API_KEY` образ не вводит). Для таких провайдеров (`HERMES_PROVIDERS_CONFIG_API_KEY = {custom}`) `<PROVIDER>_API_KEY` env **бесполезен** и НЕ передаётся; ключ эмитится в `config.yaml` как `model.api_key` (детали и безопасность — §6).

`HERMES_LLM_BASE_URL` → `model.base_url` в `config.yaml` (НЕ env). Никакое значение env здесь не логируется (redaction `*key*`/`*token*`).

### 5. Итоговый поток провижининга

```
provision(user_id)
  → _require_provision_config():
       HERMES_IMAGE непуст
       HERMES_LLM_API_KEY непуст
       HERMES_LLM_PROVIDER ∈ allowlist ∧ ≠ auto          ← НОВОЕ (fail-fast)
       HERMES_MODEL непуст                                ← НОВОЕ (fail-fast)
       provider требует base_url ⇒ HERMES_LLM_BASE_URL непуст  ← НОВОЕ
  → render_instance_config(toolset=…, provider=…, model=…, base_url=…, api_key=…)
       эмитит model.default="<provider>/<model>", model.provider="<provider>" [, base_url]
       provider ∈ HERMES_PROVIDERS_CONFIG_API_KEY ⇒ доп. эмитит model.api_key="${HERMES_INSTANCE_LLM_KEY}"  ← §6
  → _container_env(api_key): API_SERVER_*
       provider с env-ключом ⇒ <PROVIDER>_API_KEY=<key>
       provider ∈ HERMES_PROVIDERS_CONFIG_API_KEY ⇒ HERMES_INSTANCE_LLM_KEY=<key> (НЕ <PROVIDER>_API_KEY)
       (без LLM_MODEL/LLM_PROVIDER)
  → docker run → mark_running
```

### 6. Провайдеры без env-ключа (`custom`) — ключ через `config.yaml` (закрытие Q-055-1)

**Проблема.** ADR-055 (до этой ревизии) прокидывал ключ ТОЛЬКО через env `<PROVIDER>_API_KEY`. Для `custom` это не работает: подтверждено чтением образа —

- `D:\BA\hermes\plugins\model-providers\custom\__init__.py:65` — профиль `custom` объявлен `env_vars=()` (нет фиксированного key-env; образ **не** вводит `CUSTOM_API_KEY`);
- `D:\BA\hermes\hermes_cli\config.py:3991` (`clear_model_endpoint_credentials`) — «`model.api_key` is valid **only for explicit custom endpoint** assignments. Built-in providers resolve credentials from env vars». То есть для `custom` единственный канал ключа — `model.api_key` в `config.yaml`;
- резолвер модели читает `model.provider`/`model.api_key`/`model.base_url` напрямую из `config.yaml` (`D:\BA\hermes\hermes_cli\models.py:2429-2433`; `D:\BA\hermes\agent\agent_init.py:110` — ветка `provider == "custom"`).

Прокинутый `CUSTOM_API_KEY` env образ **игнорирует** → ключа нет → upstream-`401`. Это и есть [Q-055-1](../99-open-questions.md).

**Решение.** Вводим closed-set провайдеров, передающих ключ через `config.yaml` (а не env):

```
HERMES_PROVIDERS_CONFIG_API_KEY = {custom}
```

(`lmstudio` сюда **не** входит: его auth-режим опционален и читается из env `LM_API_KEY` — остаётся env-каналом; при необходимости расширения набор дополняется backend'ом по сверке с образом, см. [Q-055-2](../99-open-questions.md).)

Для `provider ∈ HERMES_PROVIDERS_CONFIG_API_KEY`:

1. `render_instance_config` получает доп. параметр `api_key: str` и эмитит в секцию `model` строку:

   ```yaml
   model:
     default: "custom/<model>"
     provider: "custom"
     base_url: "<base_url>"
     api_key: "${HERMES_INSTANCE_LLM_KEY}"   # env-ref, НЕ плейнтекст ключа
   ```

   **Плейнтекст ключа в файл тома НЕ пишется.** Эмитится **env-ссылка** `${HERMES_INSTANCE_LLM_KEY}`; образ раскрывает её при загрузке config (`_expand_env_vars`, `D:\BA\hermes\hermes_cli\config.py:5531-5548` — `${VAR}` → `os.environ[VAR]`; нераскрытые ссылки сохраняются verbatim → не «утекают» как пустой ключ молча, дают явный fail). Это строго безопаснее литерала: секрет остаётся только в env контейнера (как `API_SERVER_KEY`/прочие), не лежит на диске тома, не попадает в бэкап/снапшот тома.

2. `manager._container_env` для таких провайдеров передаёт ключ в env под фиксированным именем `HERMES_INSTANCE_LLM_KEY=<HERMES_LLM_API_KEY>` (имя выбрано нейтральным, не пересекается с key-env реальных провайдеров) и **НЕ** передаёт бесполезный `<PROVIDER>_API_KEY`.

3. Для провайдеров с env-ключом (`anthropic`, `openrouter`, …) — поведение **без изменений**: ключ только через `<PROVIDER>_API_KEY` env, `model.api_key` в config **НЕ** эмитится (не дублировать секрет; `clear_model_endpoint_credentials` подтверждает, что для built-in провайдеров inline-ключ в config — мусор/риск).

**Anti-injection ключа.** Значение `api_key` в `render_instance_config` эмитится как **env-ссылка-константа** `"${HERMES_INSTANCE_LLM_KEY}"` (литерал в шаблоне, сам ключ в YAML не подставляется) → YAML-инъекция через содержимое ключа **структурно невозможна** (ключ в файл не пишется). Если backend всё же выберет fallback-режим «литерал в файле» (например, образ старой версии без `${}`-экспансии — что НЕ относится к pin-тегу `HERMES_IMAGE`), значение ключа обязано пройти safe-charset валидацию (`^[A-Za-z0-9._\-]+$`, как для прочих `model.*`) с fail-fast при нарушении; режим env-ref — основной и default.

**Резолюция `model.default` для `custom`.** Подтверждено: образ принимает `model.provider: "custom"` как авторитетный (резолвер читает его напрямую, `models.py:2429-2433`); `model.default` — в формате `"<provider>/<model>"`, т.е. для custom — **`"custom/<model>"`** (напр. `custom/gpt-4o-mini`). Формат единый с §3 (control plane собирает `default = "<HERMES_LLM_PROVIDER>/<HERMES_MODEL>"`; для custom `HERMES_LLM_PROVIDER=custom`). `model.base_url` обязателен (custom ∈ `HERMES_PROVIDERS_REQUIRING_BASE_URL`, §2).

## Consequences

**Положительные:**
- Инстанс получает явные `model.default`/`model.provider` → нет дефолта на openrouter → 401 устранён.
- Неверная конфигурация (невалидный провайдер, пустая модель, отсутствующий base_url) падает **рано** с понятной ошибкой в логах control plane, а не непрозрачным 401 в рантайме.
- `config.yaml` — единственный источник модели/провайдера (согласовано с контрактом образа); env очищен от игнорируемых ключей.

**Отрицательные / ограничения:**
- Расширенная сигнатура `render_instance_config` + новые поля настройки (`HERMES_LLM_BASE_URL`) — небольшое усложнение конфигурации деплоя.
- ~~Точное имя key-env для `custom` зависит от версии образа~~ → **закрыто §6** ([Q-055-1](../99-open-questions.md) Resolved): `custom` не имеет env-ключа, ключ идёт через `config.yaml model.api_key` env-ссылкой. Цена: `render_instance_config` получает доп. параметр `api_key` + набор `HERMES_PROVIDERS_CONFIG_API_KEY` нужно держать в синхроне с образом ([Q-055-2](../99-open-questions.md)).
- Map провайдер→key-env нужно поддерживать в синхроне с образом при апгрейде Hermes ([Q-055-2](../99-open-questions.md)).

## Alternatives

1. **Маппинг `openai → openrouter` вместо fail-fast.** Отвергнуто: скрывает неверную конфигурацию, неоднозначен по ключу, оставляет «магию».
2. **Оставить `provider: "auto"` + задать только `base_url`/ключ.** Отвергнуто: `auto`-резолюция образа недетерминирована и уже привела к багу; конкретный провайдер надёжнее.
3. **`HERMES_MODEL = "provider/model"` (один токен).** Отвергнуто: дублирует провайдер, риск рассинхрона с `model.provider`/ключом.
4. **Передавать модель через env `LLM_MODEL`.** Невозможно: образ Hermes её игнорирует (`.env.example` строки 12–15).
