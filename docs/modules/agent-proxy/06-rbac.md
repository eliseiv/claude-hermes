# Agent Proxy — RBAC

Авторизация — клиентский контур ([ADR-044](../../adr/ADR-044-client-api-key-auth.md)): `X-API-Key` (единый клиентский ключ, constant-time) + `X-User-Id` (UUID субъекта). Пользовательская роль — `user` (владелец своих ресурсов).

- **Субъект** — `X-User-Id`; все операции (`ensure_running`, `consume`, прокси) скоупятся этим `userId`. Прогон/инстанс/баланс другого пользователя недоступны.
- **Идентичность доверяется** (ключ доверенный, [ADR-044 §3](../../adr/ADR-044-client-api-key-auth.md)); `require_owner` не применяется (субъект = `X-User-Id` по определению).
- **`runId`-операции** (`/events`, `/approval`, `/stop`) — прогон должен принадлежать инстансу субъекта (`X-User-Id`); адресация инстанса по `userId` исключает доступ к чужому прогону.
- **`POST /v1/agent/runs/{runId}/resume`** ([ADR-064 §5](../../adr/ADR-064-incremental-agent-run-billing-and-pause-resume.md)) — RBAC по строке `agent_runs`: `agent_runs[runId].user_id == X-User-Id`, иначе **404** (чужой/несуществующий прогон невидим — не раскрываем существование, как namespaced `runId`). Проверяется первым шагом (`service.py::resume`, до пред-гварда статуса и CAS).
- **Изоляция от admin** — admin-контур (`X-Admin-Token`, [ADR-009](../../adr/ADR-009-admin-token-auth.md)) к `/v1/agent/*` отношения не имеет; клиентский ключ admin-действий не авторизует и наоборот.
- **Без security scheme** — нет: оба заголовка (`clientApiKey`+`userId`) обязательны на всех `/v1/agent/*`.
