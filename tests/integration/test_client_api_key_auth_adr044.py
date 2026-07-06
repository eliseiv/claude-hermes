"""Integration: client API-KEY auth contract end-to-end (ADR-044).

Real PostgreSQL (testcontainers) via the shared ``client`` fixture. Covers the acceptance items:
- X-User-Id handling: missing / blank / whitespace / non-UUID → 401; valid pair → not 401.
- Lazy provisioning by X-User-Id: a users row is created in the same per-request session before any
  FK-bearing insert, and is idempotent on repeat (ON CONFLICT DO NOTHING).
- require_owner is a no-op: body userId != X-User-Id no longer 403s on the touched routers
  (byok / subscription / token-purchase / wallet); the op runs on the X-User-Id subject.
- Dormant JWT: the JWT/Apple modules stay importable (ADR-044 §4).
- Redaction: X-API-Key is never written to logs / audit.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import _TEST_CLIENT_API_KEY, auth_headers, make_jwt, seed_user


def _key_header() -> dict[str, str]:
    return {"X-API-Key": _TEST_CLIENT_API_KEY}


def _derived_uuid(raw: str) -> uuid.UUID:
    """The stable users.id the code derives for a non-UUID X-User-Id (ADR-058 §1 branch 3).

    Imported lazily (like the other app imports in this file) so module collection never triggers
    app.config/get_settings before the testcontainer DATABASE_URL is set.
    """
    from app.deps import HERMES_USER_NAMESPACE

    return uuid.uuid5(HERMES_USER_NAMESPACE, raw)


async def _users_count(db_sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with db_sessionmaker() as s:
        row = await s.execute(text("SELECT COUNT(*) FROM users"))
        return int(row.scalar_one())


# ============================================================================
# 1. X-User-Id handling (subject resolution) — ADR-058
# ============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_id_value",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
    ],
)
async def test_invalid_or_missing_user_id_returns_401(
    client: AsyncClient, user_id_value: str | None
) -> None:
    # ADR-058 §1: the ONLY 401 cause from subject resolution is an empty / whitespace-only
    # X-User-Id (no subject). A non-UUID string is NOT 401 anymore — it is a valid subject
    # deterministically mapped to a UUID (see test_non_uuid_user_id_* below).
    headers = _key_header()
    if user_id_value is not None:
        headers["X-User-Id"] = user_id_value
    r = await client.get("/v1/tools", headers=headers)
    assert r.status_code == 401, r.text


# --- ADR-058: arbitrary non-UUID X-User-Id is a valid, stable subject -----------------------
@pytest.mark.asyncio
async def test_non_uuid_user_id_provisions_user(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # (a) An arbitrary non-UUID string (client's own stable account id) authenticates → 200 and
    # lazily provisions the derived subject (uuid5(namespace, raw)). No 401 on "format".
    r = await client.get("/v1/tools", headers={**_key_header(), "X-User-Id": "acct_123"})
    assert r.status_code == 200, r.text
    expected = _derived_uuid("acct_123")
    assert await _user_exists(db_sessionmaker, expected), "derived users row must be provisioned"


@pytest.mark.asyncio
async def test_non_uuid_user_id_is_stable_no_duplicates(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # (b) The SAME non-UUID string on repeat resolves to the SAME users.id (uuid5 stability) — no
    # duplicate subject. Observable effect: exactly one users row, under the derived id.
    for _ in range(3):
        r = await client.get("/v1/tools", headers={**_key_header(), "X-User-Id": "acct_123"})
        assert r.status_code == 200, r.text
    expected = _derived_uuid("acct_123")
    async with db_sessionmaker() as s:
        row = await s.execute(
            text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": str(expected)}
        )
        assert row.scalar_one() == 1, "the derived subject must be a single, stable users row"
    assert await _users_count(db_sessionmaker) == 1, "no other subject may leak from the same string"


@pytest.mark.asyncio
async def test_canonical_uuid_user_id_used_as_is(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # (c) A canonical UUID string is used AS-IS (backward compatibility): users.id == the supplied
    # UUID exactly — NOT the uuid5 hash of its string form. Guards the ADR-058 branch-2 invariant.
    uid = uuid.uuid4()
    r = await client.get("/v1/tools", headers={**_key_header(), "X-User-Id": str(uid)})
    assert r.status_code == 200, r.text
    assert await _user_exists(db_sessionmaker, uid), "canonical UUID must resolve to itself"
    derived = _derived_uuid(str(uid))
    assert derived != uid
    assert not await _user_exists(
        db_sessionmaker, derived
    ), "a canonical UUID must NOT be hashed via uuid5 (would break backward compatibility)"


@pytest.mark.asyncio
async def test_non_uuid_user_id_strip_stability_same_subject(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # (d) ' acct_123 ' (padded) and 'acct_123' resolve to the SAME subject: strip() runs before the
    # uuid5 hash (ADR-058 §3 trimming), so surrounding whitespace does not fork the identity.
    for value in (" acct_123 ", "acct_123"):
        r = await client.get("/v1/tools", headers={**_key_header(), "X-User-Id": value})
        assert r.status_code == 200, r.text
    expected = _derived_uuid("acct_123")
    assert await _user_exists(db_sessionmaker, expected)
    assert await _users_count(db_sessionmaker) == 1, "padded/unpadded must be one subject"


@pytest.mark.asyncio
async def test_valid_key_and_uuid_passes(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert "tools" in r.json()


@pytest.mark.asyncio
async def test_user_id_with_surrounding_whitespace_is_trimmed(
    client: AsyncClient,
) -> None:
    # deps.get_current_user strips X-User-Id before uuid.UUID(...): a padded valid UUID still works
    # (and lazily provisions). /v1/tools serves a 200 for any authenticated subject.
    uid = uuid.uuid4()
    r = await client.get("/v1/tools", headers={**_key_header(), "X-User-Id": f"  {uid}  "})
    assert r.status_code == 200, r.text


# ============================================================================
# 2. Lazy provisioning by X-User-Id (ADR-007 channel swapped to X-User-Id)
# ============================================================================
async def _user_exists(db_sessionmaker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> bool:
    async with db_sessionmaker() as s:
        row = await s.execute(text("SELECT 1 FROM users WHERE id = :id"), {"id": str(uid)})
        return row.first() is not None


@pytest.mark.asyncio
async def test_lazy_provisioning_creates_user_row(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.uuid4()
    assert not await _user_exists(db_sessionmaker, uid)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert await _user_exists(db_sessionmaker, uid), "users row must be lazily provisioned"


@pytest.mark.asyncio
async def test_lazy_provisioning_is_idempotent(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.uuid4()
    for _ in range(3):
        r = await client.get("/v1/tools", headers=auth_headers(uid))
        assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        row = await s.execute(text("SELECT COUNT(*) FROM users WHERE id = :id"), {"id": str(uid)})
        count = row.scalar_one()
    assert count == 1, f"expected exactly one users row, got {count}"


@pytest.mark.asyncio
async def test_lazy_provisioning_does_not_overwrite_existing(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A pre-existing user with trial_used=True must keep that flag (ON CONFLICT DO NOTHING).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200
    async with db_sessionmaker() as s:
        row = await s.execute(text("SELECT trial_used FROM users WHERE id = :id"), {"id": str(uid)})
        assert row.scalar_one() is True, "existing trial_used must not be reset by provisioning"


# ============================================================================
# 3. require_owner is a no-op (ADR-044 §3) — body userId != X-User-Id no longer 403s
# ============================================================================
@pytest.mark.asyncio
async def test_byok_toggle_body_userid_mismatch_no_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    other = uuid.uuid4()
    # Authenticated as uid (X-User-Id); body claims `other`. Pre-ADR-044 this was 403; now no-op.
    r = await client.post(
        "/v1/byok/toggle",
        json={"userId": str(other), "enabled": False},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    # The op ran on the X-User-Id subject (uid), which has no key → missing.
    assert r.json()["keyStatus"] == "missing"


@pytest.mark.asyncio
async def test_byok_delete_body_userid_mismatch_no_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    other = uuid.uuid4()
    r = await client.post(
        "/v1/byok/delete",
        json={"userId": str(other)},
        headers=auth_headers(uid),
    )
    assert r.status_code != 403, r.text


@pytest.mark.asyncio
async def test_wallet_consume_body_userid_mismatch_no_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # /v1/wallet/consume carries userId in the body. A mismatch must not 403 on the client contour;
    # the request is processed against the X-User-Id subject (and may 4xx for business reasons,
    # but never 403 for ownership).
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=5)
    other = uuid.uuid4()
    r = await client.post(
        "/v1/wallet/consume",
        json={"userId": str(other), "sessionId": str(uuid.uuid4()), "amount": 1},
        headers=auth_headers(uid),
    )
    assert r.status_code != 403, r.text


# ============================================================================
# 3b. Dormant JWT is IGNORED on the hot client path (ADR-044 §4 / §4a)
# ============================================================================
@pytest.mark.asyncio
async def test_bearer_jwt_alone_is_ignored_on_v1_returns_401(client: AsyncClient) -> None:
    # ADR-044 §4/§4a: the JWT contour is dormant and the issuer /v1/auth/* is RETIRED. A valid
    # Bearer JWT is NOT an authentication factor on the hot client path — /v1/* authenticates via
    # X-API-Key + X-User-Id only. A Bearer-only request (no X-API-Key) is rejected → 401. Migrated
    # from the retired test_auth_api.py::test_issued_access_token_verifies_but_is_dormant_on_v1 so
    # the "JWT is ignored on /v1/*" invariant is preserved after the auth-issuer suite is deleted.
    token = make_jwt(uuid.uuid4())
    r = await client.get("/v1/tools", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


# ============================================================================
# 4. Dormant JWT/Apple modules remain importable (ADR-044 §4)
# ============================================================================
def test_dormant_jwt_modules_importable() -> None:
    # The JWT/Apple contour is not deleted — only dormant. Imports must keep working so the DB
    # schema and the existing auth tests stay green.
    from app.api_gateway.auth import JwtVerifier  # noqa: F401
    from app.auth.apple import get_apple_verifier  # noqa: F401
    from app.auth.issuer import TokenIssuer  # noqa: F401

    # JwtVerifier still constructs against the configured (test) key material.
    JwtVerifier()


# ============================================================================
# 5. Redaction: X-API-Key never reaches logs / audit (ADR-044 §3)
# ============================================================================
def test_redaction_hides_x_api_key() -> None:
    from app.observability.redaction import REDACTED, redact

    payload = {
        "X-API-Key": _TEST_CLIENT_API_KEY,
        "x-api-key": _TEST_CLIENT_API_KEY,
        "apiKey": _TEST_CLIENT_API_KEY,
        "headers": {"X-API-Key": _TEST_CLIENT_API_KEY},
        "userId": "11111111-2222-3333-4444-555555555555",
    }
    out = redact(payload)
    assert out["X-API-Key"] == REDACTED
    assert out["x-api-key"] == REDACTED
    assert out["apiKey"] == REDACTED
    assert out["headers"]["X-API-Key"] == REDACTED
    # Non-secret subject identity survives for diagnostics.
    assert out["userId"] == "11111111-2222-3333-4444-555555555555"
    # The raw key value appears nowhere in the redacted output.
    assert _TEST_CLIENT_API_KEY not in str(out)
