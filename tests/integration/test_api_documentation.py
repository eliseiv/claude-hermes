"""Integration: OpenAPI/Swagger documentation convention (08-api-documentation.md, ADR-044).

Rewritten for the client-API-KEY auth model (ADR-044): the client contour /v1/* is authorized by
TWO apiKey-in-header schemes that are required TOGETHER (AND) — `clientApiKey` (X-API-Key) +
`userId` (X-User-Id) — NOT the former single `bearerAuth` (JWT). Covered here:
1. DOCS_ENABLED toggles /docs, /redoc, /openapi.json (404 when off, 200 when on/default).
2. openapi.json: client /v1/* require [{clientApiKey:[], userId:[]}] (AND, one object), admin →
   [{adminToken:[]}], adapty → [{adaptyWebhook:[]}], public → no security; the merged AND form is
   NOT the OR form [{clientApiKey:[]},{userId:[]}]. Scheme declarations: clientApiKey/userId/
   adminToken/adaptyWebhook; bearerAuth stays DECLARED (dormant) but is attached to NO operation.
3. Real auth verification unchanged (auto_error=False did NOT short-circuit get_current_user):
   no headers / wrong key / missing X-User-Id on /v1/* still 401; valid pair passes (regression).
4. No auth header leaks as an operation `parameter` (X-API-Key/X-User-Id/Authorization/X-Admin).
5. Named request/response examples (visible byok/wallet contour); the 8-value blockReason
   completeness guard lives on the VISIBLE EffectivePolicyResponse.reasons[]; tags/grouping.
6. Swagger /docs builds without error.
7. ADR-059 Hermes-only surface: legacy routers (chat/tools/models/presets/workspaces/chats/
   profile/preferences/preview) and the dormant /v1/auth/* issuer are ABSENT from the schema, yet
   the legacy routes stay live (hide-only, include_in_schema=False); title == "claude-hermes".

The documentation layer is reflection-only: these tests use the OpenAPI schema produced by
create_app() and (for the regression guard) the live ASGI client from conftest. DOCS_ENABLED
variants build dedicated apps with the flag overridden via the lru_cached settings.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import _TEST_CLIENT_API_KEY, auth_headers, seed_user

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.config import Settings

# IMPORTANT: do NOT import app.main / call get_settings at module top.
# `app.main` runs `create_app()` (→ get_settings()) at import time; importing it during
# collection would cache the default localhost DATABASE_URL before the testcontainer env
# is set (conftest sets DATABASE_URL inside the session-scoped pg_url fixture), poisoning
# the lru_cached settings for alembic migrations and the app.db engine. All app imports
# below are deferred into fixtures/helpers, mirroring conftest's lazy-import pattern.

# ADR-004 canonical blockReason values (docs/adr/ADR-004-blocked-http-200.md).
_BLOCK_REASONS = (
    "trial_used",
    "subscription_required",
    "subscription_expired",
    "credits_empty",
    "byok_disabled",
    "byok_invalid",
    "rate_limited",
    "policy_denied",
)

# Expected tag order per ADR-059 (Hermes-only public surface): only visible-router tags remain,
# ordered by the Hermes user scenario. Legacy/dormant tags (Auth, Chat, Tools, Models, Presets,
# Preview, Chats, Workspaces, Profile, Preferences) are REMOVED — their routers are hidden
# (include_in_schema=False) and carry no visible operations. app.main `_OPENAPI_TAGS` must match.
_TAG_ORDER = [
    "Agent",
    "Policy",
    "Wallet",
    "Tokens",
    "BYOK",
    "Billing (Adapty)",
    "Admin",
    "Health",
]

# Endpoint -> expected single tag (ADR-059 visible surface only). Hidden legacy routes
# (chat, tools, models, presets, workspaces, chats, profile, preferences, preview) and the dormant
# /v1/auth/* issuer are ABSENT from the schema and therefore not listed here.
_ENDPOINT_TAG = {
    # Agent (ADR-045/047): 4 client-contour /v1/agent/* endpoints, all tag=Agent. The route path
    # params are declared as {run_id} (FastAPI emits the function param name verbatim), so the
    # OpenAPI path strings are /v1/agent/runs/{run_id}/* — the docs' {runId} is the
    # external-narrative spelling (agent-proxy/02). Security is the client-contour AND form
    # (clientApiKey+userId), asserted via _CLIENT_V1_OPERATIONS below.
    ("/v1/agent/run", "post"): "Agent",
    ("/v1/agent/runs/{run_id}/events", "get"): "Agent",
    ("/v1/agent/runs/{run_id}/approval", "post"): "Agent",
    ("/v1/agent/runs/{run_id}/stop", "post"): "Agent",
    ("/v1/policy/effective", "get"): "Policy",
    ("/v1/wallet", "get"): "Wallet",
    ("/v1/wallet/consume", "post"): "Wallet",
    ("/v1/tokens/purchase", "post"): "Tokens",
    ("/v1/tokens/products", "get"): "Tokens",
    ("/v1/byok/set", "post"): "BYOK",
    ("/v1/byok/toggle", "post"): "BYOK",
    ("/v1/byok/delete", "post"): "BYOK",
    # Adapty webhook (ADR-029): visible under its own contour (adaptyWebhook), NOT the client
    # contour — excluded from _CLIENT_V1_OPERATIONS below and asserted by its own security test.
    ("/v1/billing/adapty/webhook", "post"): "Billing (Adapty)",
    # Admin (ADR-009/048): 4 endpoints, all tag=Admin, authorized by adminToken only (see
    # _ADMIN_PATHS) — excluded from the client-contour AND checks.
    ("/v1/admin/credits/grant", "post"): "Admin",
    ("/v1/admin/wallet/grant", "post"): "Admin",
    ("/v1/admin/subscription/grant", "post"): "Admin",
    ("/v1/admin/wallet/{userId}", "get"): "Admin",
    ("/health", "get"): "Health",
    ("/ready", "get"): "Health",
    ("/metrics", "get"): "Health",
}

# Public service endpoints that must NOT carry a security requirement.
_PUBLIC_PATHS = {"/health", "/ready", "/metrics"}

# The Adapty webhook is visible but authorized by adaptyWebhook (not the client contour); it is
# excluded from the client-contour AND assertions and checked by test_adapty_webhook_* instead.
_ADAPTY_WEBHOOK = ("/v1/billing/adapty/webhook", "post")

# Admin endpoints (ADR-009): authorized by the isolated adminToken scheme, NOT the client contour.
# ADR-048: credits/grant (canonical) + subscription/grant (new) join the wallet/grant alias.
# Each NEW route is enumerated DIRECTLY so its adminToken-only security is asserted per-endpoint
# (enumerated-contour guard) — not merely inferred from sharing the same router dependency.
_ADMIN_PATHS = {
    ("/v1/admin/wallet/grant", "post"),
    ("/v1/admin/credits/grant", "post"),
    ("/v1/admin/subscription/grant", "post"),
    ("/v1/admin/wallet/{userId}", "get"),
}

# ADR-044 R2.4: the client-contour AND requirement, exactly one object with BOTH keys.
_CLIENT_CONTOUR_SECURITY = [{"clientApiKey": [], "userId": []}]
# The OR form that must NEVER appear for a client operation (two separate objects).
_OR_FORM_KEYSETS = ({"clientApiKey"}, {"userId"})


# --------------------------- app/openapi builders ---------------------------
def _build_app(*, docs_enabled: bool) -> FastAPI:
    """Build a fresh app with DOCS_ENABLED overridden (see comment in fixture below)."""
    import app.config as config_mod
    import app.main as main_mod

    overridden = config_mod.get_settings().model_copy(update={"docs_enabled": docs_enabled})

    def _override() -> Settings:
        return overridden

    config_get = config_mod.get_settings
    main_get = main_mod.get_settings
    config_mod.get_settings = _override  # type: ignore[assignment]
    main_mod.get_settings = _override  # type: ignore[assignment]
    try:
        return main_mod.create_app()
    finally:
        config_mod.get_settings = config_get  # type: ignore[assignment]
        main_mod.get_settings = main_get  # type: ignore[assignment]


@pytest.fixture(scope="module")
def openapi_schema(pg_url: str) -> dict[str, Any]:
    """OpenAPI schema from a docs-enabled app (default state).

    Depends on pg_url so the testcontainer DATABASE_URL is in env before any settings
    read, keeping the shared lru_cached Settings (and app.db engine) consistent.
    """
    app = _build_app(docs_enabled=True)
    return app.openapi()


def _operation(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return schema["paths"][path][method]


# Client-contour /v1/* operations from the tag table. Excludes the admin contour (adminToken) and
# the Adapty webhook (adaptyWebhook), which are authorized by their own isolated schemes, not by
# clientApiKey+userId (ADR-009/ADR-029). Yields agent(4)+policy+wallet(2)+tokens(2)+byok(3) = 12.
_CLIENT_V1_OPERATIONS = [
    (p, m)
    for (p, m) in _ENDPOINT_TAG
    if p.startswith("/v1/") and (p, m) not in _ADMIN_PATHS and (p, m) != _ADAPTY_WEBHOOK
]


# ============================================================================
# 1. DOCS_ENABLED toggle (R7)
# ============================================================================
@pytest.mark.asyncio
async def test_docs_enabled_default_true_serves_docs(pg_url: str) -> None:
    from app.config import get_settings

    assert get_settings().docs_enabled is True


@pytest.mark.asyncio
async def test_docs_enabled_true_endpoints_return_200(pg_url: str) -> None:
    # R2: Swagger /docs (and /redoc, /openapi.json) build WITHOUT error under the AND post-process.
    app = _build_app(docs_enabled=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = await ac.get(path)
            assert r.status_code == 200, f"{path} expected 200, got {r.status_code}"


@pytest.mark.asyncio
async def test_docs_enabled_false_endpoints_return_404(pg_url: str) -> None:
    app = _build_app(docs_enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = await ac.get(path)
            assert r.status_code == 404, f"{path} expected 404, got {r.status_code}"


@pytest.mark.asyncio
async def test_docs_disabled_does_not_break_functional_endpoints(pg_url: str) -> None:
    app = _build_app(docs_enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ============================================================================
# 2. Security scheme declarations (R2.1 / R2.2)
# ============================================================================
def test_client_contour_schemes_declared(openapi_schema: dict[str, Any]) -> None:
    # ADR-044 R2.1: clientApiKey + userId are apiKey-in-header schemes (X-API-Key / X-User-Id).
    schemes = openapi_schema["components"]["securitySchemes"]
    assert "clientApiKey" in schemes
    assert "userId" in schemes
    ck = schemes["clientApiKey"]
    assert ck["type"] == "apiKey" and ck["in"] == "header" and ck["name"] == "X-API-Key"
    uid = schemes["userId"]
    assert uid["type"] == "apiKey" and uid["in"] == "header" and uid["name"] == "X-User-Id"


def test_client_contour_schemes_have_russian_descriptions(openapi_schema: dict[str, Any]) -> None:
    schemes = openapi_schema["components"]["securitySchemes"]
    assert "X-API-Key" in schemes["clientApiKey"].get("description", "")
    assert "X-User-Id" in schemes["userId"].get("description", "")


def test_bearer_auth_dormant_attached_to_no_operation(openapi_schema: dict[str, Any]) -> None:
    # ADR-044 §4/§5: the JWT contour is DORMANT — bearerAuth must be attached to NO client (or any)
    # operation. The scheme declaration "может оставаться в коде" but is not navigated onto /v1/*;
    # FastAPI only emits referenced schemes, so its absence from components is acceptable. The
    # binding contract is: no operation's `security` references bearerAuth.
    if "bearerAuth" in openapi_schema["components"].get("securitySchemes", {}):
        bearer = openapi_schema["components"]["securitySchemes"]["bearerAuth"]
        assert bearer["type"] == "http" and bearer["scheme"] == "bearer"
    for path, item in openapi_schema.get("paths", {}).items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            for req in op.get("security", []) or []:
                assert "bearerAuth" not in req, f"bearerAuth attached to {method.upper()} {path}"


def test_admin_token_security_scheme_declared(openapi_schema: dict[str, Any]) -> None:
    schemes = openapi_schema["components"]["securitySchemes"]
    assert "adminToken" in schemes
    admin = schemes["adminToken"]
    assert admin["type"] == "apiKey"
    assert admin["in"] == "header"
    assert admin["name"] == "X-Admin-Token"


# ============================================================================
# 2b. Per-operation security by contour (R2.4 acceptance table)
# ============================================================================
@pytest.mark.parametrize(("path", "method"), _CLIENT_V1_OPERATIONS)
def test_client_v1_endpoints_require_and_form(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    # ADR-044 R2.4: each client /v1/* carries EXACTLY one requirement object with BOTH keys (AND).
    op = _operation(openapi_schema, path, method)
    sec = op.get("security")
    assert (
        sec == _CLIENT_CONTOUR_SECURITY
    ), f"{method.upper()} {path} security != {_CLIENT_CONTOUR_SECURITY}: {sec}"


@pytest.mark.parametrize(("path", "method"), _CLIENT_V1_OPERATIONS)
def test_client_v1_endpoints_not_or_form(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    # The OR form [{clientApiKey:[]},{userId:[]}] is a defect (ADR-044 §5): two separate
    # single-key objects must NOT appear for a client operation.
    op = _operation(openapi_schema, path, method)
    sec = op.get("security") or []
    keysets = [set(req.keys()) for req in sec]
    for forbidden in _OR_FORM_KEYSETS:
        assert (
            keysets.count(forbidden) == 0
        ), f"{method.upper()} {path} uses OR form (separate {forbidden}): {sec}"
    # And there is exactly one requirement object total (the merged AND object).
    assert len(sec) == 1, f"{method.upper()} {path} should have 1 requirement object: {sec}"


# NOTE (ADR-059): test_tools_endpoint_requires_client_contour REMOVED — /v1/tools is now hidden
# (include_in_schema=False) and absent from the schema. The client-contour AND requirement is still
# asserted for every VISIBLE client operation via the parametrized _CLIENT_V1_OPERATIONS tests
# above, and directly per-endpoint for the Agent headline contour below.


# Enumerated-contour coverage for the Agent headline contour (08-api-documentation R2.1/R4,
# agent-proxy/02): each /v1/agent/* operation MUST be present in openapi.json, carry tag `Agent`,
# and require the client-contour AND form. These DIRECT per-endpoint asserts guard the contour even
# if the generic parametrized maps were to drift; they fail if tag/security deviate from docs
# (masking-regression guard: a deviation cannot silently pass as "covered elsewhere").
_AGENT_OPERATIONS = [
    ("/v1/agent/run", "post"),
    ("/v1/agent/runs/{run_id}/events", "get"),
    ("/v1/agent/runs/{run_id}/approval", "post"),
    ("/v1/agent/runs/{run_id}/stop", "post"),
]


@pytest.mark.parametrize(("path", "method"), _AGENT_OPERATIONS)
def test_agent_endpoint_present_in_openapi(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    paths = openapi_schema.get("paths", {})
    assert path in paths, f"{path} missing from /openapi.json"
    assert method in paths[path], f"{method.upper()} {path} missing from /openapi.json"


@pytest.mark.parametrize(("path", "method"), _AGENT_OPERATIONS)
def test_agent_endpoint_tag_is_agent(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    op = _operation(openapi_schema, path, method)
    assert op.get("tags") == ["Agent"], f"{method.upper()} {path} tags={op.get('tags')}"


@pytest.mark.parametrize(("path", "method"), _AGENT_OPERATIONS)
def test_agent_endpoint_requires_client_contour_and_form(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    # AND form: exactly one requirement object with BOTH clientApiKey + userId (NOT the OR form).
    op = _operation(openapi_schema, path, method)
    sec = op.get("security")
    assert (
        sec == _CLIENT_CONTOUR_SECURITY
    ), f"{method.upper()} {path} security != {_CLIENT_CONTOUR_SECURITY}: {sec}"
    keysets = [set(req.keys()) for req in (sec or [])]
    for forbidden in _OR_FORM_KEYSETS:
        assert keysets.count(forbidden) == 0, f"{method.upper()} {path} uses OR form: {sec}"


# NOTE (ADR-059): test_auth_endpoints_have_no_security REMOVED — the /v1/auth/* issuer router is
# not registered in create_app() (dormant, ADR-044 §4), so those paths are absent from the schema
# and there is no operation to assert on. The dormant JWT import contour is still guarded by
# test_dormant_jwt_modules_importable in test_client_api_key_auth_adr044.py.


@pytest.mark.parametrize(("path", "method"), sorted(_ADMIN_PATHS))
def test_admin_endpoints_require_admin_token_only(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    # ADR-009: /v1/admin/* authorize via adminToken ONLY; the client contour is not an auth factor.
    op = _operation(openapi_schema, path, method)
    assert op.get("security") == [
        {"adminToken": []}
    ], f"{method.upper()} {path} security != [{{'adminToken': []}}]: {op.get('security')}"


def test_adapty_webhook_requires_adapty_scheme(openapi_schema: dict[str, Any]) -> None:
    # ADR-029/ADR-044 R2.4: the Adapty webhook carries [{adaptyWebhook:[]}] only.
    op = _operation(openapi_schema, "/v1/billing/adapty/webhook", "post")
    assert op.get("security") == [{"adaptyWebhook": []}], op.get("security")


@pytest.mark.parametrize("path", sorted(_PUBLIC_PATHS))
def test_public_endpoints_have_no_security(openapi_schema: dict[str, Any], path: str) -> None:
    op = _operation(openapi_schema, path, "get")
    assert not op.get("security"), f"{path} must not require auth, got {op.get('security')}"


def test_no_global_security_applied(openapi_schema: dict[str, Any]) -> None:
    assert not openapi_schema.get("security")


# ============================================================================
# 2c. No auth header leaks as an operation PARAMETER.
#     Auth headers must surface ONLY as securitySchemes, NEVER as `parameters`.
# ============================================================================
_FORBIDDEN_AUTH_PARAM_NAMES = {"authorization", "x-admin-token", "x-api-key", "x-user-id"}


def _all_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    ops: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete", "options", "head"}:
                ops.append((path, method, operation))
    return ops


def _header_param_names(operation: dict[str, Any]) -> list[str]:
    return [
        str(p.get("name", "")).lower()
        for p in operation.get("parameters", [])
        if p.get("in") == "header"
    ]


def test_no_operation_declares_auth_header_as_parameter(openapi_schema: dict[str, Any]) -> None:
    offenders: list[str] = []
    for path, method, operation in _all_operations(openapi_schema):
        dup = set(_header_param_names(operation)) & _FORBIDDEN_AUTH_PARAM_NAMES
        if dup:
            offenders.append(f"{method.upper()} {path}: {sorted(dup)}")
    assert not offenders, "auth headers leaked into operation parameters: " + ("; ".join(offenders))


# ============================================================================
# 2d. ADR-059 hide-only: legacy routes are ABSENT from the schema but STAY routed & functional.
#     Removing them from /openapi.json (include_in_schema=False) must NOT unregister the route.
# ============================================================================
@pytest.mark.parametrize(
    "path",
    ["/v1/chat/run", "/v1/chat/tool-result", "/v1/tools", "/v1/workspaces", "/v1/models"],
)
def test_hidden_legacy_paths_absent_from_openapi(openapi_schema: dict[str, Any], path: str) -> None:
    # The legacy contour is documentation-hidden (ADR-059 §1/§3): no path item in the schema.
    assert path not in openapi_schema.get("paths", {}), f"hidden legacy path {path} leaked into schema"


@pytest.mark.asyncio
async def test_hidden_chat_run_route_still_live_not_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-059 §1/§6: hide-only, not disable. Though '/v1/chat/run' is absent from /openapi.json, the
    # route is still registered and answers (same code/auth). A valid client pair reaches the
    # handler → NOT 404 (business-blocked 200 for a trial-spent user). This is the behavioural
    # counterpart to the schema-absence assertion above (masking guard: "hidden" must not mean gone).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code != 404, f"hidden route must stay live, got {r.status_code}: {r.text}"
    assert r.status_code == 200


# ============================================================================
# 3. Real auth verification regression (R2.4) — CRITICAL.
#    auto_error=False on the apiKey schemes must NOT short-circuit get_current_user.
# ============================================================================
@pytest.mark.asyncio
async def test_regression_no_headers_still_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_regression_wrong_key_still_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
        headers={"X-API-Key": "wrong", "X-User-Id": str(uuid.uuid4())},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_regression_missing_user_id_401_across_v1_endpoints(client: AsyncClient) -> None:
    # A valid client key but NO X-User-Id has no subject → 401 everywhere it's declared.
    probes = [
        ("post", "/v1/chat/tool-result"),
        ("get", "/v1/policy/effective"),
        ("get", "/v1/wallet"),
        ("post", "/v1/wallet/consume"),
        ("post", "/v1/byok/set"),
        ("post", "/v1/byok/toggle"),
        ("post", "/v1/byok/delete"),
    ]
    key_only = {"X-API-Key": _TEST_CLIENT_API_KEY}
    for method, path in probes:
        if method == "get":
            r = await client.get(path, headers=key_only)
        else:
            r = await client.post(path, json={}, headers=key_only)
        assert r.status_code == 401, f"{method.upper()} {path} expected 401, got {r.status_code}"


@pytest.mark.asyncio
async def test_regression_valid_pair_passes_auth_not_401(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A valid (X-API-Key, X-User-Id) pair must NOT be rejected. The seeded user has trial used and
    # no subscription, so the orchestrator blocks business-side (200) — the point is it's not 401.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code not in (401, 403)
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"


# ============================================================================
# 4. Named examples (R5)
# ============================================================================
def _request_example_names(op: dict[str, Any]) -> set[str]:
    body = op.get("requestBody", {})
    content = body.get("content", {}).get("application/json", {})
    return set(content.get("examples", {}).keys())


def _response_example_names(op: dict[str, Any], status: str = "200") -> set[str]:
    resp = op.get("responses", {}).get(status, {})
    content = resp.get("content", {}).get("application/json", {})
    return set(content.get("examples", {}).keys())


# NOTE (ADR-059): the chat request/response example tests (test_chat_run_*, test_chat_tool_result_*)
# were REMOVED — /v1/chat/* is hidden from the schema (include_in_schema=False), so its named
# examples are no longer part of the documented surface. The named-example convention is still
# asserted on the VISIBLE contour below (byok/set request+response, wallet/consume request).


def test_byok_set_examples_valid_and_invalid(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/byok/set", "post")
    resp_names = _response_example_names(op)
    assert {"valid", "invalid"} <= resp_names, resp_names
    assert _request_example_names(op), "byok/set must have a request example"


def test_byok_set_request_example_marks_redaction(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/byok/set", "post")
    body = op["requestBody"]["content"]["application/json"]
    examples = body.get("examples", {})
    blob = str(examples)
    assert "sk-ant-" in blob
    assert "логир" in blob or "redact" in blob.lower()


def test_wallet_consume_example_debit_one(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/wallet/consume", "post")
    assert "debit_one" in _request_example_names(op)


# ============================================================================
# 5. blockReason / reasons documentation (R3)
#    ADR-059: the completeness guard for all 8 blockReason values moved from the hidden ChatResponse
#    schema onto the VISIBLE EffectivePolicyResponse.reasons[] surface (test below). The
#    chat-schema tests (test_chat_response_blockreason_documents_all_8,
#    test_chat_response_status_invariant_documented) were REMOVED — ChatResponse is no longer part
#    of the documented surface (its owning /v1/chat/* router is include_in_schema=False).
# ============================================================================
def test_policy_reasons_documents_all_8_block_reasons(openapi_schema: dict[str, Any]) -> None:
    # Completeness guard (moved from ChatResponse.blockReason per ADR-059): the visible
    # EffectivePolicyResponse.reasons[] field documents the FULL set of 8 canonical blockReason
    # values. Losing any value fails here — the "all 8 documented" invariant is preserved on the
    # Hermes-visible contour.
    schema = openapi_schema["components"]["schemas"]["EffectivePolicyResponse"]
    reasons_field = schema["properties"]["reasons"]
    desc = reasons_field.get("description", "")
    assert len(_BLOCK_REASONS) == 8
    for reason in _BLOCK_REASONS:
        assert reason in desc, f"policy reasons[] description missing '{reason}'"


# ============================================================================
# 6. Tags & grouping (R4)
# ============================================================================
def test_tag_order(openapi_schema: dict[str, Any]) -> None:
    declared = [t["name"] for t in openapi_schema.get("tags", [])]
    assert declared == _TAG_ORDER, declared


def test_tags_have_russian_descriptions(openapi_schema: dict[str, Any]) -> None:
    for tag in openapi_schema.get("tags", []):
        assert tag.get("description"), f"tag {tag['name']} has no description"


@pytest.mark.parametrize(
    ("path", "method", "expected_tag"), [(p, m, tag) for (p, m), tag in _ENDPOINT_TAG.items()]
)
def test_each_endpoint_has_exactly_one_correct_tag(
    openapi_schema: dict[str, Any], path: str, method: str, expected_tag: str
) -> None:
    op = _operation(openapi_schema, path, method)
    tags = op.get("tags", [])
    assert tags == [expected_tag], f"{method.upper()} {path} tags={tags}, expected [{expected_tag}]"


def test_all_documented_paths_have_summary_and_description(openapi_schema: dict[str, Any]) -> None:
    for path, method in _ENDPOINT_TAG:
        op = _operation(openapi_schema, path, method)
        assert op.get("summary"), f"{method.upper()} {path} missing summary"
        assert op.get("description"), f"{method.upper()} {path} missing description"


# ============================================================================
# R6. API metadata
# ============================================================================
def test_api_metadata(openapi_schema: dict[str, Any]) -> None:
    info = openapi_schema["info"]
    # ADR-059 §5: the Hermes-service title.
    assert info["title"] == "claude-hermes"
    assert info["version"] == "0.1.0"
    desc = info.get("description", "")
    # ADR-044: description references the client-contour headers and the blocked=200 rule (R6).
    assert "X-API-Key" in desc
    assert "X-User-Id" in desc
    assert "200" in desc


# ============================================================================
# TD-021 / ADR-029 revision: retired POST /v1/subscription/sync MUST be absent from the schema
# ============================================================================
def test_retired_subscription_sync_absent_from_openapi(openapi_schema: dict[str, Any]) -> None:
    # Direct, enumerated-contour assertion that the retired route is gone from the documented API
    # (not merely "not asserted"): neither the path nor the `Subscription` tag may appear.
    paths = openapi_schema.get("paths", {})
    assert "/v1/subscription/sync" not in paths, "retired route still in OpenAPI (TD-021)"
    declared_tags = {t["name"] for t in openapi_schema.get("tags", [])}
    assert "Subscription" not in declared_tags, "retired Subscription tag still declared (TD-021)"
