"""Unit: AgentProxyService._launch_run connect-only retry (ADR-062 §2, agent-proxy).

The wake-gap fix's belt-and-suspenders: POST /v1/runs is retried ONLY on a connect-phase transport
error (the request is guaranteed NOT to have reached the server), because the endpoint is NOT
idempotent (no client key) — any post-send error may have created a run and must never be retried
(double-run / double-billing risk). The classification is by an EXPLICIT tuple
``(httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)`` — NOT a base class — because in
the pinned httpx (0.27.2) ``ConnectTimeout`` is NOT a subclass of ``ConnectError`` (both connect,
but split under ``TimeoutException``/``NetworkError``), while ``ReadTimeout``/``WriteError``
(post-send) DO share those bases; catching a base class would swallow the post-send set → dupe risk.

Hermes is mocked at the HTTP boundary (respx). ``asyncio.sleep`` (the retry backoff) is patched to a
recorder so the tests neither wait the real 2s backoff nor lose the ability to assert it fired the
right number of times.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app.agent_proxy.service import AgentProxyService
from app.config import Settings
from app.errors import UpstreamError
from app.hermes_runtime.manager import InstanceEndpoint

_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"
_RUNS_URL = f"{_BASE_URL}/v1/runs"
_BODY: dict[str, Any] = {"input": "build me a site"}


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        # Keep the backoff explicit/observable; it is patched out (no real wait) but the VALUE is
        # what the recorder asserts was requested.
        "HERMES_LAUNCH_RETRY_ATTEMPTS": 3,
        "HERMES_LAUNCH_RETRY_BACKOFF_SECONDS": 2.0,
        "HERMES_PROXY_TIMEOUT_SECONDS": 5.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _service(settings: Settings) -> AgentProxyService:
    # _launch_run only touches self._settings + self._bearer_headers, so the DB/manager/wallet/
    # audit/runs collaborators are irrelevant here and passed as None (never dereferenced here).
    # ADR-064: the constructor now requires ``runs``; None is safe on this path.
    return AgentProxyService(
        session=None,  # type: ignore[arg-type]
        manager=None,  # type: ignore[arg-type]
        wallet=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        settings=settings,
        runs=None,  # type: ignore[arg-type]
    )


def _endpoint() -> InstanceEndpoint:
    return InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch the backoff sleep to a recorder: no real wait, but the calls are asserted on."""
    import app.agent_proxy.service as service_mod

    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(service_mod.asyncio, "sleep", _fake_sleep)
    return recorded


def _ok_response() -> httpx.Response:
    return httpx.Response(202, json={"run_id": "run_abc", "status": "running"})


# ============================================================================
# Retry (safe, connect-phase, body NOT sent) — success after a transient connect error
# ============================================================================
@respx.mock
async def test_connect_error_first_then_success_returns_run_no_502(no_sleep: list[float]) -> None:
    svc = _service(_settings())
    route = respx.post(_RUNS_URL).mock(
        side_effect=[httpx.ConnectError("conn refused"), _ok_response()]
    )
    run_id, status = await svc._launch_run(_endpoint(), _BODY)
    assert (run_id, status) == ("run_abc", "running")  # succeeded on the retry, NOT a 502
    assert route.call_count == 2  # one failed connect + one successful retry
    assert no_sleep == [2.0]  # exactly one backoff between the two attempts


@respx.mock
async def test_connect_timeout_retried_even_though_not_a_connect_error_subclass(
    no_sleep: list[float],
) -> None:
    # Regression guard for the EXPLICIT-tuple classification: ConnectTimeout is a connect error
    # but is NOT a subclass of ConnectError in the pinned httpx, so `except ConnectError` would MISS
    # it. It must still be retried (the safe set is matched by the explicit tuple).
    assert not issubclass(httpx.ConnectTimeout, httpx.ConnectError)  # documents the hierarchy trap
    svc = _service(_settings())
    route = respx.post(_RUNS_URL).mock(
        side_effect=[httpx.ConnectTimeout("connect timed out"), _ok_response()]
    )
    run_id, _status = await svc._launch_run(_endpoint(), _BODY)
    assert run_id == "run_abc"
    assert route.call_count == 2  # retried the connect timeout → succeeded
    assert no_sleep == [2.0]


@respx.mock
async def test_pool_timeout_retried(no_sleep: list[float]) -> None:
    # PoolTimeout is the third member of the safe connect-phase tuple (no connection acquired → the
    # request never left the pool → safe to retry).
    svc = _service(_settings())
    route = respx.post(_RUNS_URL).mock(
        side_effect=[httpx.PoolTimeout("pool exhausted"), _ok_response()]
    )
    run_id, _status = await svc._launch_run(_endpoint(), _BODY)
    assert run_id == "run_abc"
    assert route.call_count == 2


# ============================================================================
# NO retry (post-send: the run MAY have been created) → exactly ONE POST → 502 (anti double-run)
# ============================================================================
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteError("write failed"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=["read_timeout", "write_error", "remote_protocol_error"],
)
@respx.mock
async def test_post_send_error_is_not_retried_single_post_then_502(
    no_sleep: list[float], exc: Exception
) -> None:
    # CRITICAL anti-dupe invariant: a post-send transport error may have created a run server-side,
    # so it must NOT be retried — exactly ONE POST reaches Hermes and the caller gets a 502.
    svc = _service(_settings())
    route = respx.post(_RUNS_URL).mock(side_effect=exc)
    with pytest.raises(UpstreamError):
        await svc._launch_run(_endpoint(), _BODY)
    assert route.call_count == 1  # NO second POST → no risk of a duplicate run
    assert no_sleep == []  # backoff never fired (no retry attempted)


# ============================================================================
# Retry exhaustion — all attempts connect-fail → 502, POST count == attempts
# ============================================================================
@respx.mock
async def test_all_attempts_connect_error_raises_502_call_count_equals_attempts(
    no_sleep: list[float],
) -> None:
    svc = _service(_settings(HERMES_LAUNCH_RETRY_ATTEMPTS=3))
    route = respx.post(_RUNS_URL).mock(side_effect=httpx.ConnectError("still down"))
    with pytest.raises(UpstreamError):
        await svc._launch_run(_endpoint(), _BODY)
    assert route.call_count == 3  # all three attempts made
    assert no_sleep == [2.0, 2.0]  # a backoff between each pair of attempts (attempts-1)


# ============================================================================
# Non-2xx response (the server ANSWERED — deterministic) → no retry → 502, one POST
# ============================================================================
@respx.mock
async def test_non_2xx_response_no_retry_single_post_then_502(no_sleep: list[float]) -> None:
    svc = _service(_settings(HERMES_LAUNCH_RETRY_ATTEMPTS=3))
    route = respx.post(_RUNS_URL).mock(return_value=httpx.Response(500, json={"e": "boom"}))
    with pytest.raises(UpstreamError):
        await svc._launch_run(_endpoint(), _BODY)
    assert route.call_count == 1  # a server response is deterministic → not retried
    assert no_sleep == []


# ============================================================================
# attempts=1 disables retry — a connect error is an immediate 502, backoff never fires
# ============================================================================
@respx.mock
async def test_attempts_one_disables_retry_no_backoff(no_sleep: list[float]) -> None:
    svc = _service(_settings(HERMES_LAUNCH_RETRY_ATTEMPTS=1))
    route = respx.post(_RUNS_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(UpstreamError):
        await svc._launch_run(_endpoint(), _BODY)
    assert route.call_count == 1  # single attempt, no retry
    assert no_sleep == []  # backoff not called when attempts == 1


@respx.mock
async def test_successful_retry_sends_bearer_and_body_on_each_attempt(
    no_sleep: list[float],
) -> None:
    # The retried request carries the SAME instance Bearer + body (the run body is re-sent verbatim
    # on the connect-phase retry; this is safe precisely because the prior attempt never reached the
    # server).
    svc = _service(_settings())
    route = respx.post(_RUNS_URL).mock(side_effect=[httpx.ConnectError("x"), _ok_response()])
    await svc._launch_run(_endpoint(), _BODY)
    assert route.call_count == 2
    last = route.calls.last.request
    assert last.headers["authorization"] == f"Bearer {_API_KEY}"
    import json as _json

    assert _json.loads(last.content) == _BODY
