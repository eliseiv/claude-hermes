"""Integration: /v1/agent/* HTTP contract — auth, validation, policy-blocked (ADR-044/045/047).

Real PostgreSQL (testcontainers) via the shared ``client`` fixture. These paths do NOT require a
Hermes instance / Docker:
- auth (#4): every /v1/agent/* route is 401 without a valid X-API-Key + X-User-Id pair.
- request validation (#2/#3 sad path): empty/missing message → 422; bad approval choice → 422.
- policy-blocked (#1): with no subscription/credits the launch returns 200 {status:blocked,
  blockReason} and never wakes an instance (the service returns BEFORE ensure_running, so no
  Docker is touched). The contract is verified end-to-end through the router (ADR-004).

The allowed launch / SSE relay / approval / stop happy paths require a live Hermes instance and are
covered at the service layer with a mocked instance (tests/unit/test_agent_proxy_service.py) and in
e2e (agent-proxy/09-testing.md §E2E).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import _TEST_CLIENT_API_KEY, auth_headers, seed_user


def _key_only() -> dict[str, str]:
    return {"X-API-Key": _TEST_CLIENT_API_KEY}


# ============================================================================
# 4. Auth: 401 on every /v1/agent/* without a valid X-API-Key + X-User-Id pair
# ============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("POST", "/v1/agent/run", {"message": "hi"}),
        ("GET", "/v1/agent/runs/run_1/events", None),
        ("POST", "/v1/agent/runs/run_1/approval", {"choice": "once"}),
        ("POST", "/v1/agent/runs/run_1/stop", None),
    ],
)
@pytest.mark.parametrize(
    "headers_kind",
    ["no_headers", "key_only", "bad_key", "no_user_id", "bad_user_id"],
)
async def test_agent_routes_require_auth(
    client: AsyncClient,
    method: str,
    path: str,
    json_body: dict | None,
    headers_kind: str,
) -> None:
    headers: dict[str, str] = {}
    if headers_kind == "key_only":
        headers = _key_only()
    elif headers_kind == "bad_key":
        headers = {"X-API-Key": "wrong-key", "X-User-Id": str(uuid.uuid4())}
    elif headers_kind == "no_user_id":
        headers = _key_only()
    elif headers_kind == "bad_user_id":
        headers = {**_key_only(), "X-User-Id": "not-a-uuid"}
    r = await client.request(method, path, json=json_body, headers=headers)
    assert r.status_code == 401, f"{headers_kind} {method} {path} -> {r.status_code}: {r.text}"


# ============================================================================
# 1. POST /v1/agent/run — policy blocked → 200 {status:blocked, blockReason}
# ============================================================================
@pytest.mark.asyncio
async def test_run_blocked_no_subscription_returns_200_blocked(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # No subscription AND the one-time trial already used → credits-branch policy blocks. A
    # brand-new user (trial_used=False) would instead be ALLOWED via the single lifetime trial
    # (ADR-002) — that path needs a live instance and is covered in e2e.
    #
    # trial_used is a legitimate, documented blockReason of the agent path (02-api-contracts.md
    # §Достижимый набор: {credits_empty, subscription_expired, trial_used}); the contract and code
    # are in sync, so this is the real, expected blocked response.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)  # no subscription, no wallet, trial spent.
    r = await client.post(
        "/v1/agent/run", json={"message": "do something"}, headers=auth_headers(uid)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "blocked"
    # Actual credits-branch reason for trial-spent, no-subscription (ADR-002 state machine).
    assert body["blockReason"] == "trial_used"
    assert body["runId"] is None


@pytest.mark.asyncio
async def test_run_blocked_zero_credits_returns_200_blocked(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Active subscription but 0 credits → credits_empty (ADR-004 200 blocked).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    r = await client.post(
        "/v1/agent/run", json={"message": "do something"}, headers=auth_headers(uid)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "credits_empty"
    assert body["runId"] is None


@pytest.mark.asyncio
async def test_run_blocked_expired_subscription_returns_200_blocked(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="expired", balance=100)
    r = await client.post(
        "/v1/agent/run", json={"message": "do something"}, headers=auth_headers(uid)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "subscription_expired"


# ============================================================================
# 2/3. Request validation (sad path) — 422
# ============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-message"),
        pytest.param({"message": ""}, id="empty-message"),
        pytest.param({"message": "hi", "extra": "x"}, id="extra-forbidden"),
    ],
)
async def test_run_invalid_body_returns_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], body: dict
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post("/v1/agent/run", json=body, headers=auth_headers(uid))
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"choice": "yes"}, id="bad-choice"),
        pytest.param({}, id="missing-choice"),
        pytest.param({"choice": "once", "extra": 1}, id="extra-forbidden"),
    ],
)
async def test_approval_invalid_body_returns_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], body: dict
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    r = await client.post("/v1/agent/runs/run_1/approval", json=body, headers=auth_headers(uid))
    assert r.status_code == 422, r.text


# ============================================================================
# 12. Regression: /v1/chat/* and other routes are unaffected by the agent router.
# ============================================================================
@pytest.mark.asyncio
async def test_chat_route_independent_of_agent(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A narrow regression: /v1/policy/effective (a cheap authenticated route) still serves 200,
    # confirming the agent router did not disturb the existing app wiring.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    r = await client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
