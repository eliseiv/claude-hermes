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
