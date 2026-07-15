"""Integration: streaming-safe SizeLimitMiddleware (TD-017).

Closes the chunked / missing-Content-Length bypass of the transport size guard. The middleware has
two guards: (1) a Content-Length fast-path reject BEFORE reading the body; (2) a streaming
byte-count that, when Content-Length is ABSENT (Transfer-Encoding: chunked or the client omitted
the header), reads the body chunks and rejects with 413 the moment the running total exceeds the
applicable limit — BEFORE invoking the handler.

Per-route limits (ADR-020): general ≤512KB; /v1/chat/run gets a raised 12MB transport limit.

httpx's ASGITransport sends a request body WITHOUT a Content-Length header (chunked-style) when the
``content`` is an (async) iterator — exactly the case guard 2 targets. A bytes ``content`` carries a
Content-Length and exercises guard 1.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from tests.conftest import auth_headers

_GENERAL_LIMIT = 512 * 1024  # SIZE_LIMIT_BODY default
_JSON_CT = "application/json"


def _hdrs(uid: uuid.UUID, *, content_type: bool = True) -> dict[str, str]:
    h = auth_headers(uid)
    if content_type:
        h["content-type"] = _JSON_CT
    return h


async def _chunked(total: int, chunk: int = 64 * 1024) -> AsyncIterator[bytes]:
    """Yield ``total`` bytes in chunks → httpx omits Content-Length (chunked transport)."""
    sent = 0
    while sent < total:
        n = min(chunk, total - sent)
        yield b"x" * n
        sent += n


# ============================================================================
# Guard 2 — chunked (no Content-Length) OVER the limit → 413 BEFORE the handler
# ============================================================================
@pytest.mark.asyncio
async def test_chunked_over_limit_returns_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    over = _GENERAL_LIMIT + 200 * 1024  # > 512KB
    r = await client.post("/v1/wallet/me", content=_chunked(over), headers=_hdrs(uid))
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"


# ============================================================================
# Guard 2 — chunked UNDER the limit → NOT 413 (the body reaches the handler)
# ============================================================================
@pytest.mark.asyncio
async def test_chunked_under_limit_not_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    # A small chunked body well under the general limit. It is replayed to the handler verbatim; the
    # handler then rejects it as invalid JSON/route (NOT 413). The transport guard must not fire.
    r = await client.post("/v1/wallet/me", content=_chunked(8 * 1024), headers=_hdrs(uid))
    assert r.status_code != 413, r.text


# ============================================================================
# Guard 1 — Content-Length fast-path: OVER → 413, UNDER → not 413
# ============================================================================
@pytest.mark.asyncio
async def test_content_length_over_limit_returns_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    big = b"x" * (_GENERAL_LIMIT + 100 * 1024)  # bytes → Content-Length present
    r = await client.post("/v1/wallet/me", content=big, headers=_hdrs(uid))
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_content_length_under_limit_not_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    small = b"x" * (16 * 1024)
    r = await client.post("/v1/wallet/me", content=small, headers=_hdrs(uid))
    assert r.status_code != 413, r.text


# ============================================================================
# Per-route raised limit: /v1/chat/run accepts a chunked body above the GENERAL cap (≤512KB) but
# under its own 12MB limit — must NOT be 413 (it is rejected later as 422/auth, not at transport).
# ============================================================================
@pytest.mark.asyncio
async def test_chat_run_raised_limit_chunked_above_general_not_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    # 700KB > general 512KB but << chat/run 12MB → the raised per-route limit applies.
    body = _GENERAL_LIMIT + 200 * 1024
    r = await client.post("/v1/chat/run", content=_chunked(body), headers=_hdrs(uid))
    assert r.status_code != 413, r.text


@pytest.mark.asyncio
async def test_chat_run_chunked_over_raised_limit_returns_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    # Above the 12MB chat/run limit → 413 even on the raised route.
    over = 12 * 1024 * 1024 + 256 * 1024
    r = await client.post("/v1/chat/run", content=_chunked(over), headers=_hdrs(uid))
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_chat_run_chunked_just_under_raised_limit_not_413(client: AsyncClient) -> None:
    # A body just under the 12MB chat/run cap must still pass transport (non-regression of ADR-060,
    # which only touched the workspace path; the chat/run limit is unchanged).
    uid = uuid.uuid4()
    body = 12 * 1024 * 1024 - 256 * 1024  # ~11.75MB < 12MB
    r = await client.post("/v1/chat/run", content=_chunked(body), headers=_hdrs(uid))
    assert r.status_code != 413, r.text


# ============================================================================
# ADR-060 — workspace files COLLECTION path /v1/workspaces/{id}/files gets the raised 12MB transport
# limit (inline base64 of a ≤8MB knowledge file exceeds the general ≤512KB cap once
# base64-inflated).
# These transport-layer tests use chunked bodies (no Content-Length) and a random workspace id: they
# assert only the middleware verdict (413 vs NOT-413) BEFORE the handler, so no DB/workspace is
# needed. End-to-end 201 repro of the fixed prod bug lives in
# test_workspace_upload_body_limit_adr060.
# ============================================================================
def _ws_files_path(wid: uuid.UUID | None = None) -> str:
    return f"/v1/workspaces/{wid or uuid.uuid4()}/files"


@pytest.mark.asyncio
async def test_workspace_files_chunked_above_general_not_413(client: AsyncClient) -> None:
    # ~645KB > general 512KB but << workspace 12MB → the raised per-route limit applies, so the
    # transport guard must NOT fire (the request reaches auth/handler and is rejected there, ≠413).
    # This is the direct transport-level repro of the fixed prod bug (413 on a ~484KB file).
    uid = uuid.uuid4()
    body = 645 * 1024
    r = await client.post(_ws_files_path(), content=_chunked(body), headers=_hdrs(uid))
    assert r.status_code != 413, r.text


@pytest.mark.asyncio
async def test_workspace_files_chunked_near_8mb_cap_not_413(client: AsyncClient) -> None:
    # base64(8MB) ≈ 10.67MB body: above general AND above chat-run-only concerns, still < 12MB
    # workspace limit → passes transport (reaches ADR-036 validation, ≠413 at the middleware).
    uid = uuid.uuid4()
    body = 10_670 * 1024  # ~10.42MB, comfortably < 12MB
    r = await client.post(_ws_files_path(), content=_chunked(body), headers=_hdrs(uid))
    assert r.status_code != 413, r.text


@pytest.mark.asyncio
async def test_workspace_files_chunked_over_12mb_returns_413(client: AsyncClient) -> None:
    # Above the 12MB workspace limit → 413 payload_too_large at the transport middleware.
    uid = uuid.uuid4()
    over = 12 * 1024 * 1024 + 256 * 1024
    r = await client.post(_ws_files_path(), content=_chunked(over), headers=_hdrs(uid))
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_workspace_files_over_12mb_rejected_before_auth(client: AsyncClient) -> None:
    # The transport guard runs OUTERMOST (before auth). An over-12MB body with NO auth headers must
    # be 413 (middleware), never 401 — proving the reject is at transport, not the endpoint.
    over = 12 * 1024 * 1024 + 256 * 1024
    r = await client.post(
        _ws_files_path(), content=_chunked(over), headers={"content-type": _JSON_CT}
    )
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"


# ============================================================================
# ADR-060 — the raise is scoped to the COLLECTION path only. The per-file ITEM path
# /v1/workspaces/{id}/files/{file_id} and the workspace collection/detail paths keep the general
# ≤512KB cap (first-match rule in _limit_for). A ~600KB body on those paths → 413.
# ============================================================================
@pytest.mark.asyncio
async def test_workspace_file_item_path_keeps_general_limit_413(client: AsyncClient) -> None:
    uid = uuid.uuid4()
    over_general = _GENERAL_LIMIT + 100 * 1024  # ~612KB > 512KB, << 12MB
    path = f"/v1/workspaces/{uuid.uuid4()}/files/{uuid.uuid4()}"
    r = await client.post(path, content=_chunked(over_general), headers=_hdrs(uid))
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_non_workspace_post_600kb_still_413(client: AsyncClient) -> None:
    # Non-regression: the general size_limit_body is UNCHANGED by ADR-060. A ~600KB body on an
    # unrelated POST route still trips the general ≤512KB cap → 413.
    uid = uuid.uuid4()
    body = 600 * 1024  # > 512KB, < 12MB
    r = await client.post("/v1/wallet/me", content=_chunked(body), headers=_hdrs(uid))
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"
