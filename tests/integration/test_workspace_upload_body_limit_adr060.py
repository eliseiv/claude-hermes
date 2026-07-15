"""Integration: end-to-end repro of the ADR-060 workspace-upload transport-limit fix.

Prod bug: POST /v1/workspaces/{id}/files with a ~484KB knowledge file base64-inflated to ~645KB of
request body was rejected with 413 by the general ≤512KB size_limit_body transport guard BEFORE it
ever reached the ADR-036 file validation. ADR-060 scopes a raised 12MB transport limit to exactly
the workspace files COLLECTION path so such uploads now reach the handler.

These tests exercise the FULL stack (real PostgreSQL container, real workspace row, real base64
decode + extraction) to prove the body now traverses transport → auth → ADR-036 validation → 201,
NOT a 413 from the middleware. This complements the transport-only chunked tests in
test_streaming_size_guard_adr017 (which assert the middleware verdict without a DB).
"""

from __future__ import annotations

import base64
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user

_GENERAL_LIMIT = 512 * 1024  # SIZE_LIMIT_BODY default — the cap that used to reject the ~645KB body.


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _create_workspace(client: AsyncClient, uid: uuid.UUID) -> str:
    r = await client.post("/v1/workspaces", json={"name": "Proj"}, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _upload_text(client: AsyncClient, uid: uuid.UUID, wid: str, decoded: bytes) -> AsyncClient:
    return await client.post(
        f"/v1/workspaces/{wid}/files",
        json={"type": "text", "mediaType": "text/plain", "filename": "notes.txt", "data": _b64(decoded)},
        headers=auth_headers(uid),
    )


# ============================================================================
# 1. The exact prod-bug repro: a ~484KB file (base64 body ~645KB > general 512KB) now reaches the
#    handler and returns 201 — it is NOT rejected 413 by the transport middleware.
# ============================================================================
@pytest.mark.asyncio
async def test_upload_484kb_file_reaches_handler_and_succeeds(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    wid = await _create_workspace(client, uid)

    decoded = b"a" * (484 * 1024)  # ~484KB real file content
    body_b64 = _b64(decoded)
    # Sanity: the base64 request body is genuinely above the general cap that caused the prod 413.
    assert len(body_b64) > _GENERAL_LIMIT

    r = await _upload_text(client, uid, wid, decoded)
    assert r.status_code == 201, r.text  # was 413 before ADR-060
    meta = r.json()
    assert meta["size"] == len(decoded)
    assert meta["hasExtractedText"] is True

    # Persisted for real.
    async with db_sessionmaker() as s:
        et = await s.scalar(
            text("SELECT extracted_text FROM workspace_files WHERE id=:i"), {"i": meta["fileId"]}
        )
    assert et == decoded.decode()


# ============================================================================
# 2. A file at the ADR-036 contract cap (~8MB decoded → base64 body ~10.67MB) passes the 12MB
#    transport limit and reaches ADR-036 validation, which accepts it (≤8MB) → 201. This proves the
#    raised limit spans the whole contract-legal range, not just small files.
# ============================================================================
@pytest.mark.asyncio
async def test_upload_near_8mb_cap_passes_transport_and_validation(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    wid = await _create_workspace(client, uid)

    decoded = b"a" * (8 * 1024 * 1024 - 1024)  # ~8MB, just under WORKSPACE_FILE_MAX_BYTES
    body_b64 = _b64(decoded)
    # The request body is well above the general cap and above 12MB would 413 — assert it is under.
    assert len(body_b64) > _GENERAL_LIMIT
    assert len(body_b64) < 12 * 1024 * 1024

    r = await _upload_text(client, uid, wid, decoded)
    # Reached ADR-036 validation and passed (NOT a transport 413): a ≤8MB file is accepted.
    assert r.status_code == 201, r.text
    assert r.json()["size"] == len(decoded)


# ============================================================================
# 3. Non-regression: the GET list on the same collection path is unaffected by the raised limit
#    (it carries no body). Listing after a large upload still returns 200 with metadata only.
# ============================================================================
@pytest.mark.asyncio
async def test_get_files_list_unaffected_by_raised_limit(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    wid = await _create_workspace(client, uid)
    r = await _upload_text(client, uid, wid, b"a" * (484 * 1024))
    assert r.status_code == 201, r.text

    lst = await client.get(f"/v1/workspaces/{wid}/files", headers=auth_headers(uid))
    assert lst.status_code == 200, lst.text
    items = lst.json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "notes.txt"
    assert "content" not in items[0]  # body never leaks on the list
