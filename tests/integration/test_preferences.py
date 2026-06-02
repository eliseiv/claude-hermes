"""Integration tests for /v1/preferences (preferences module, 09-testing.md).

GET without a stored row returns in-memory defaults (chat/true/{}) and does NOT write the DB.
PATCH upserts and updates only the provided fields. codeDefaults is bounded (<= 8KB) and must
not contain secrets. Data is scoped to the JWT sub.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


async def _row_count(maker: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with maker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM user_preferences WHERE user_id=:u"), {"u": uid}
        )
        return int(n or 0)


@pytest.mark.asyncio
async def test_get_without_row_returns_defaults_and_does_not_write(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/preferences", headers=auth_headers(uid))
    assert r.status_code == 200
    assert r.json() == {
        "defaultAssistantMode": "chat",
        "notificationsEnabled": True,
        "codeDefaults": {},
    }
    # GET must NOT create a row (lazy defaults only).
    assert await _row_count(db_sessionmaker, str(uid)) == 0


@pytest.mark.asyncio
async def test_patch_upsert_partial_preserves_other_fields(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    # First partial PATCH: only assistant mode → row created (upsert), others keep defaults.
    r1 = await client.patch(
        "/v1/preferences",
        json={"defaultAssistantMode": "code"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200
    assert r1.json() == {
        "defaultAssistantMode": "code",
        "notificationsEnabled": True,
        "codeDefaults": {},
    }
    assert await _row_count(db_sessionmaker, str(uid)) == 1

    # Second partial PATCH: only notifications → must NOT reset defaultAssistantMode.
    r2 = await client.patch(
        "/v1/preferences",
        json={"notificationsEnabled": False},
        headers=auth_headers(uid),
    )
    assert r2.json()["defaultAssistantMode"] == "code"  # preserved
    assert r2.json()["notificationsEnabled"] is False

    # GET reflects the merged state.
    g = await client.get("/v1/preferences", headers=auth_headers(uid))
    assert g.json() == {
        "defaultAssistantMode": "code",
        "notificationsEnabled": False,
        "codeDefaults": {},
    }


@pytest.mark.asyncio
async def test_patch_code_defaults_stored(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch(
        "/v1/preferences",
        json={"codeDefaults": {"language": "python", "indent": 4}},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200
    assert r.json()["codeDefaults"] == {"language": "python", "indent": 4}


@pytest.mark.asyncio
async def test_patch_invalid_assistant_mode_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch(
        "/v1/preferences",
        json={"defaultAssistantMode": "wizard"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_code_defaults_too_large_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    big = {"blob": "x" * (9 * 1024)}  # serialized > 8KB
    r = await client.patch("/v1/preferences", json={"codeDefaults": big}, headers=auth_headers(uid))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_code_defaults_with_secret_key_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch(
        "/v1/preferences",
        json={"codeDefaults": {"apiKey": "sk-ant-should-not-be-here"}},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_empty_body_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch("/v1/preferences", json={}, headers=auth_headers(uid))
    assert r.status_code == 422  # at least one field required


@pytest.mark.asyncio
async def test_preferences_scoped_to_sub(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        a = await seed_user(s)
        b = await seed_user(s)
    await client.patch(
        "/v1/preferences", json={"defaultAssistantMode": "code"}, headers=auth_headers(a)
    )
    # b never set anything → still defaults; a's change must not leak.
    rb = await client.get("/v1/preferences", headers=auth_headers(b))
    assert rb.json()["defaultAssistantMode"] == "chat"


@pytest.mark.asyncio
async def test_get_preferences_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/preferences")
    assert r.status_code == 401
