"""Unit tests for SizeLimitMiddleware._limit_for path→limit mapping (ADR-060, TD-017).

Pure and deterministic: no HTTP, no DB. Verifies the first-match path routing that scopes the
RAISED transport body limit to exactly the routes that need it (/v1/chat/run for inline base64
attachments; /v1/workspaces/{id}/files collection path for inline base64 knowledge files) while
every other route — including the per-file item path /v1/workspaces/{id}/files/{file_id} and the
workspace collection/detail paths — stays on the general size_limit_body cap.
"""

from __future__ import annotations

import uuid

import pytest

from app.api_gateway.middleware import SizeLimitMiddleware
from app.config import get_settings


@pytest.fixture
def mw() -> SizeLimitMiddleware:
    async def _dummy_app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        raise AssertionError("_limit_for must never invoke the wrapped app")

    return SizeLimitMiddleware(_dummy_app)  # type: ignore[arg-type]


def test_chat_run_gets_attachment_limit(mw: SizeLimitMiddleware) -> None:
    s = get_settings()
    assert mw._limit_for("/v1/chat/run") == s.attachment_request_body_limit


def test_workspace_files_collection_gets_workspace_limit(mw: SizeLimitMiddleware) -> None:
    s = get_settings()
    wid = uuid.uuid4()
    assert mw._limit_for(f"/v1/workspaces/{wid}/files") == s.workspace_request_body_limit


def test_workspace_file_item_path_keeps_general_limit(mw: SizeLimitMiddleware) -> None:
    # /v1/workspaces/{id}/files/{file_id} does NOT end with "/files" → general cap (ADR-060).
    s = get_settings()
    wid, fid = uuid.uuid4(), uuid.uuid4()
    assert mw._limit_for(f"/v1/workspaces/{wid}/files/{fid}") == s.size_limit_body


def test_workspace_collection_and_detail_keep_general_limit(mw: SizeLimitMiddleware) -> None:
    s = get_settings()
    wid = uuid.uuid4()
    assert mw._limit_for("/v1/workspaces") == s.size_limit_body
    assert mw._limit_for(f"/v1/workspaces/{wid}") == s.size_limit_body


def test_unrelated_route_keeps_general_limit(mw: SizeLimitMiddleware) -> None:
    s = get_settings()
    assert mw._limit_for("/v1/wallet/me") == s.size_limit_body
    assert mw._limit_for("/") == s.size_limit_body


def test_raised_limits_actually_exceed_general(mw: SizeLimitMiddleware) -> None:
    # If a raised limit were <= the general cap the scoping would be a silent no-op. Guard
    # against it.
    s = get_settings()
    assert s.workspace_request_body_limit > s.size_limit_body
    assert s.attachment_request_body_limit > s.size_limit_body


def test_adr060_default_limit_values(mw: SizeLimitMiddleware) -> None:
    # ADR-060 fixes the workspace upload transport limit at 12MB (covers base64(8MB)≈10.67MB + the
    # JSON envelope). chat/run keeps its own independent 12MB; the general cap stays 512KB.
    s = get_settings()
    assert s.workspace_request_body_limit == 12 * 1024 * 1024
    assert s.attachment_request_body_limit == 12 * 1024 * 1024
    assert s.size_limit_body == 512 * 1024
