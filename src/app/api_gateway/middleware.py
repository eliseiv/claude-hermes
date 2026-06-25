"""Gateway middleware: size limit, correlation id, security headers (api-gateway/03)."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.observability.context import set_request_id, set_session_id, set_user_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates/propagates X-Request-Id (HTTP correlation id, NOT a billing key, ADR-005)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        set_request_id(request_id)
        set_session_id(None)
        set_user_id(None)
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class SizeLimitMiddleware:
    """Rejects bodies exceeding the limit with 413 (TD-017: streaming-safe, pure ASGI).

    The general limit applies to all routes. /v1/chat/run gets a RAISED transport limit for
    inline base64 attachments (ADR-020, 05-security.md): heavy multimodal payloads exceed the
    general ≤512KB cap, so the raise is scoped to exactly that one route — the attack surface for
    accepting a large payload is not widened globally.

    Two guards (TD-017):
    1. Content-Length fast-path — an early reject BEFORE reading any body (unchanged HTTP-413
       semantics). Skipped when the header is absent (chunked / client omitted it).
    2. Streaming byte-count — when Content-Length is absent we read the body chunks ourselves,
       accumulating their actual lengths independent of any declared length. The moment the running
       total exceeds the applicable limit we stop, send a 413, and NEVER invoke the application —
       the handler does not see the over-limit body. Otherwise the fully-counted body is replayed
       to the app verbatim so request handling is transparent. This closes the chunked /
       missing-Content-Length bypass of guard 1.

    Implemented as pure ASGI (not BaseHTTPMiddleware) so we can intercept body chunks before the
    application is invoked — the guard stays streaming-устойчив. All client routes here are
    JSON-request endpoints whose handlers buffer the whole body anyway (no request-body streaming),
    so reading the body up to the limit before dispatch does not change observable behaviour.
    """

    _CHAT_RUN_PATH = "/v1/chat/run"

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        settings = get_settings()
        self._limit = settings.size_limit_body
        self._chat_run_limit = settings.attachment_request_body_limit

    def _limit_for(self, path: str) -> int:
        if path == self._CHAT_RUN_PATH:
            return self._chat_run_limit
        return self._limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._limit_for(scope.get("path", ""))

        # Guard 1 — Content-Length fast-path: reject before reading any body. Header keys in the
        # ASGI scope are lowercased bytes. A present, valid, over-limit value rejects immediately;
        # a present in-limit value lets the request through WITHOUT the streaming read (fast-path,
        # current behaviour). A missing/invalid value falls through to the streaming guard.
        content_length = self._content_length(scope)
        if content_length is not None:
            if content_length > limit:
                await self._send_too_large(scope, send)
                return
            await self._app(scope, receive, send)
            return

        # Guard 2 — no Content-Length (chunked / omitted): count body bytes as we read them and
        # reject BEFORE invoking the app once the running total exceeds the limit. Buffered chunks
        # are replayed to the app verbatim on the in-limit path.
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnect (or any non-body message) before the body completes: hand the stream
                # to the app, replaying what we buffered then deferring to the real receive (which
                # re-delivers this message).
                break
            body = message.get("body", b"")
            received += len(body)
            if received > limit:
                await self._send_too_large(scope, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        await self._app(scope, self._make_replay(chunks, receive), send)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _make_replay(chunks: list[bytes], receive: Receive) -> Receive:
        """Build a receive() that replays the buffered body, then defers to the real receive."""
        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay

    async def _send_too_large(self, scope: Scope, send: Send) -> None:
        request_id = self._request_id_from_scope(scope)
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": "request body exceeds limit",
                    "requestId": request_id,
                }
            },
        )
        await response(scope, self._noop_receive, send)

    @staticmethod
    async def _noop_receive() -> Message:  # pragma: no cover - Response never reads the body
        return {"type": "http.disconnect"}

    @staticmethod
    def _request_id_from_scope(scope: Scope) -> str | None:
        # SizeLimitMiddleware is the outermost middleware (runs before CorrelationIdMiddleware), so
        # request.state.request_id is not set yet on a rejected request; the only correlation id
        # available is the client-supplied X-Request-Id header (echoed if present). When absent the
        # 413 body carries requestId=null — acceptable for an over-limit transport reject.
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
            if name == b"x-request-id":
                return value.decode("latin-1")
        return None


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Default API security headers.

    The preview endpoint (/v1/preview/*) serves user (Claude-generated) HTML/JS and needs its own
    sandbox headers (CSP sandbox, X-Frame-Options: SAMEORIGIN, no-store; ADR-010) which differ from
    the API defaults (notably X-Frame-Options: DENY). The middleware therefore does NOT set its
    defaults on preview paths — the preview route owns its complete header set.
    """

    _PREVIEW_PREFIX = "/v1/preview/"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith(self._PREVIEW_PREFIX):
            return response
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response
