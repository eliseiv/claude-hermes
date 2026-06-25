"""TD-014 (second client): structured logging + metric on OpenAI upstream errors (ADR-033 §10).

The Anthropic side is covered by test_anthropic_upstream_error_logging.py; TD-014 explicitly
requires the SAME contract on the OpenAI client (event ``llm_upstream_error``, camelCase keys, level
matrix WARNING(4xx)/ERROR(5xx/network), no secrets/user-content, bounded-label metric). These tests
drive ``_log_upstream_error`` directly AND the real ``OpenAIClient.create_message`` with the SDK
raising genuine ``openai`` exceptions, so the production logging path is exercised end to end.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.chat.openai_client import OpenAIAuthError, OpenAIClient, _log_upstream_error
from app.errors import UpstreamError
from app.observability.logging import JsonFormatter
from app.observability.metrics import llm_upstream_errors_total

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(status: int) -> openai.APIStatusError:
    return openai.APIStatusError("boom", response=httpx.Response(status, request=_REQ), body=None)


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "unauthorized", response=httpx.Response(401, request=_REQ), body=None
    )


# ----------------------------- log capture -----------------------------
class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured_logs() -> Iterator[_Capture]:
    logger = logging.getLogger("app.chat.openai")
    handler = _Capture()
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    llm_upstream_errors_total.clear()
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled


def _event(handler: _Capture) -> tuple[logging.LogRecord, dict[str, Any]]:
    recs = [r for r in handler.records if r.getMessage() == "llm_upstream_error"]
    assert len(recs) == 1, f"expected exactly one event, got {len(recs)}"
    rec = recs[0]
    fields = getattr(rec, "extra_fields", None)
    assert isinstance(fields, dict)
    return rec, fields


def _metric(provider: str, status_code: str, error_type: str) -> float:
    return llm_upstream_errors_total.labels(
        provider=provider, status_code=status_code, error_type=error_type
    )._value.get()


# ============================= 4xx → WARNING =============================
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_4xx_logs_warning(captured_logs: _Capture, status: int) -> None:
    _log_upstream_error(_status_error(status), model="gpt-4o", status_code=status)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.WARNING
    assert fields["event"] == "llm_upstream_error"
    assert fields["provider"] == "openai"
    assert fields["model"] == "gpt-4o"
    assert fields["status_code"] == status
    assert fields["exceptionClass"] == "APIStatusError"


# ============================= 5xx → ERROR =============================
@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_logs_error(captured_logs: _Capture, status: int) -> None:
    _log_upstream_error(_status_error(status), model="gpt-4o", status_code=status)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.ERROR
    assert fields["status_code"] == status
    assert fields["provider"] == "openai"


# ============= network errors (timeout/connection) → ERROR, no status =============
@pytest.mark.parametrize(
    ("exc", "cls_name"),
    [
        (openai.APITimeoutError(request=_REQ), "APITimeoutError"),
        (openai.APIConnectionError(request=_REQ), "APIConnectionError"),
    ],
)
def test_network_errors_log_error_without_status(
    captured_logs: _Capture, exc: Exception, cls_name: str
) -> None:
    _log_upstream_error(exc, model="gpt-4o", status_code=None)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.ERROR
    assert "status_code" not in fields
    assert fields["exceptionClass"] == cls_name
    assert fields["model"] == "gpt-4o"
    # Metric: status_code='none'.
    assert _metric("openai", "none", cls_name) == 1.0


# ============= redaction / security =============
def test_no_secret_field_keys_in_logged_event(captured_logs: _Capture) -> None:
    _log_upstream_error(_status_error(403), model="gpt-4o", status_code=403)
    _rec, fields = _event(captured_logs)
    sensitive = ("key", "token", "secret", "password", "authorization", "credential")
    for field_name in fields:
        low = field_name.lower()
        assert not any(s in low for s in sensitive), f"sensitive-looking field logged: {field_name}"


def test_hypothetical_secret_field_redacted_by_formatter(captured_logs: _Capture) -> None:
    from app.observability.redaction import REDACTED

    logger = logging.getLogger("app.chat.openai")
    logger.warning(
        "llm_upstream_error",
        extra={
            "extra_fields": {
                "event": "llm_upstream_error",
                "provider": "openai",
                "apiKey": "sk-openai-should-never-appear",
                "openai_api_key": "sk-svc",
            }
        },
    )
    rec = [r for r in captured_logs.records if r.getMessage() == "llm_upstream_error"][-1]
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["apiKey"] == REDACTED
    assert payload["openai_api_key"] == REDACTED
    assert "sk-openai-should-never-appear" not in json.dumps(payload)
    assert "sk-svc" not in json.dumps(payload)


# ============= metric bounded labels =============
def test_metric_status_error_labels(captured_logs: _Capture) -> None:
    _log_upstream_error(_status_error(429), model="gpt-4o", status_code=429)
    assert _metric("openai", "429", "APIStatusError") == 1.0


# ============= real create_message path: maps + logs exactly once =============
def _client_raising(exc: Exception) -> OpenAIClient:
    client = OpenAIClient()

    class _FakeCompletions:
        async def create(self, **_kwargs: Any) -> Any:
            raise exc

    fake = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    client._client = fake  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_status_error_maps_to_upstream_and_logs(captured_logs: _Capture) -> None:
    client = _client_raising(_status_error(503))
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.ERROR
    assert fields["status_code"] == 503
    assert fields["provider"] == "openai"


@pytest.mark.asyncio
async def test_timeout_maps_to_upstream_and_logs(captured_logs: _Capture) -> None:
    client = _client_raising(openai.APITimeoutError(request=_REQ))
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.ERROR
    assert "status_code" not in fields


@pytest.mark.asyncio
async def test_auth_error_maps_and_logs_warning(captured_logs: _Capture) -> None:
    client = _client_raising(_auth_error())
    with pytest.raises(OpenAIAuthError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.WARNING
    assert fields["status_code"] == 401
    assert fields["exceptionClass"] == "AuthenticationError"


@pytest.mark.asyncio
async def test_upstream_error_message_is_generic_no_leak(captured_logs: _Capture) -> None:
    # Outward contract: the raised UpstreamError is generic — no provider status leaks out.
    client = _client_raising(_status_error(403))
    with pytest.raises(UpstreamError) as ei:
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    assert "403" not in str(ei.value)
    assert "sk-" not in str(ei.value)


@pytest.mark.asyncio
async def test_log_emitted_exactly_once(captured_logs: _Capture) -> None:
    client = _client_raising(_status_error(500))
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="s", messages=[], tools=[], attachments=None)
    events = [r for r in captured_logs.records if r.getMessage() == "llm_upstream_error"]
    assert len(events) == 1
