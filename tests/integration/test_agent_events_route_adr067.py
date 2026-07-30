"""Integration: GET /v1/agent/runs/{runId}/events THROUGH THE ROUTE (ADR-067 §3).

PRIORITY 1 — the security defect this module exists for.

Before ADR-067 the RBAC of ``/events`` was IMPLICIT. The handler called ``ensure_running(user_id)``,
which resolves the CALLER's own Hermes instance, so a foreign ``runId`` was unreachable by
construction — nobody had to check anything and nothing could go wrong. The broker has no such
property: it reads Redis by the ``run_id`` taken straight from the URL path. The implicit guard did
not weaken, it CEASED TO EXIST, and the switch-over would have shipped one user reading another
user's event stream.

Nothing in the suite would have noticed: there were no API-level tests for this route at all — the
existing ones call ``stream_events`` directly, below the router, so they exercise neither the path
parameter nor the authenticated identity. That is the gap; these tests are written through the HTTP
route deliberately.

The route-level 404 tests below were once withheld, and why is worth keeping. The ownership check
used to raise ``NotFoundError`` from INSIDE the streaming generator, and Starlette sends
``http.response.start`` (status 200) BEFORE the first ``__anext__`` — so the exception arrived after
the headers were committed and could never become a 404. It surfaced as
``RuntimeError: Caught handled exception, but response already started.`` and, under
``BaseHTTPMiddleware``, wedged the connection so every following request hung. The check now runs in
the HANDLER, before the response object exists; the generator keeps its own copy as defence in
depth, which ``test_the_broker_path_refuses_a_foreign_run_before_yielding_anything`` still covers.

The status-code asymmetry in cursor handling is also deliberate and is pinned here: an invalid
``?afterSeq=`` is a 400 (the client wrote it), while a malformed ``Last-Event-ID`` silently means
"replay everything" (an SSE library wrote it, and the client cannot fix what it did not author).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user

_EVENTS = "/v1/agent/runs/{run_id}/events"


def _service_with_broker(session: AsyncSession, broker: Any) -> Any:
    """The real service on a real session, with the broker replaced by a double.

    Only the broker is faked: the ownership check reads ``agent_runs`` through the real repository
    against the real database, which is the part under test.
    """
    from app.deps import get_agent_proxy_service_for

    service = get_agent_proxy_service_for(session)
    service._broker = broker
    return service


async def _insert_run(
    session: AsyncSession, *, run_id: str, user_id: uuid.UUID, status: str = "running"
) -> None:
    await session.execute(
        text(
            "INSERT INTO agent_runs (run_id, user_id, session_id, status, model) "
            "VALUES (:r, :u, 'sess-1', CAST(:st AS agent_run_status), 'm')"
        ),
        {"r": run_id, "u": str(user_id), "st": status},
    )


@pytest.fixture
def consumer_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the ADR-067 contour on for a test, bypassing the settings cache."""
    monkeypatch.setenv("AGENT_RUN_CONSUMER_ENABLED", "true")
    # Any stream this module opens must end quickly: the production idle timeout is 300s and would
    # hang the suite (it did, on the first run of this file).
    monkeypatch.setenv("AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


@pytest.fixture
def consumer_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the kill-switch OFF — the pre-ADR-067 path must still work unchanged."""
    monkeypatch.setenv("AGENT_RUN_CONSUMER_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


# ==================================================================================================
# (1) THE DEFECT: ownership must be asserted explicitly now that the implicit guard is gone.
# ==================================================================================================
@pytest.mark.asyncio
async def test_unauthenticated_events_request_is_401(client: AsyncClient) -> None:
    """The route carries the same auth contour as the rest of /v1 (ADR-044)."""
    response = await client.get(_EVENTS.format(run_id="run_x"))
    assert response.status_code == 401


# ==================================================================================================
# (2)/(3) Cursor handling — a deliberate asymmetry between what the CLIENT wrote and what its
# SSE library wrote.
# ==================================================================================================
@pytest.mark.parametrize("after_seq", ["-1", "1.5", "abc", "", " ", "1e3", "0x10"])
@pytest.mark.asyncio
async def test_invalid_after_seq_is_400_before_the_stream_opens(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    consumer_on: None,
    after_seq: str,
) -> None:
    """The client authored this value, so it gets a status code.

    It must arrive as 400 BEFORE the response starts: inside an already-open ``text/event-stream``
    the client would see a truncated stream instead of an error. 400 rather than FastAPI's default
    422 for a bad int is the stated contract, which is why the parameter is typed as a string and
    validated by hand.
    """
    async with db_sessionmaker() as session:
        user_id = await seed_user(session, subscription="active", balance=100)
        await _insert_run(session, run_id="run_cursor", user_id=user_id)
        await session.commit()

    response = await client.get(
        _EVENTS.format(run_id="run_cursor"),
        headers=auth_headers(user_id),
        params={"afterSeq": after_seq},
    )
    assert response.status_code == 400, f"afterSeq={after_seq!r} → {response.status_code}"
    assert response.headers.get("content-type", "").startswith("application/json")


@pytest.mark.parametrize("last_event_id", ["garbage", "-", "abc-xyz", "", "-5", "epoch-"])
def test_a_malformed_last_event_id_falls_back_to_a_full_replay(last_event_id: str) -> None:
    """(3) The asymmetry, asserted at the parsing boundary.

    The client did not author this header — its SSE library did — so answering 400 would strand a
    reconnecting client with no way to recover. The contract is a FULL replay instead: an EMPTY
    cursor, never an exception. Asserted here rather than through the route because proving a
    negative about the status code would require opening a real stream, and therefore a live Redis,
    for a rule that has nothing to do with Redis.
    """
    from app.agent_proxy.broker import parse_cursor

    cursor = parse_cursor(last_event_id=last_event_id, after_seq=None)
    assert cursor.empty, f"{last_event_id!r} produced a non-empty cursor {cursor!r}"
    assert cursor.seq == 0


def test_an_unrecognised_epoch_is_not_the_parser_s_business() -> None:
    """``"1.5-2"`` parses fine — and should. The epoch is an OPAQUE token (a uuid4 hex in practice),
    so the parser only requires it to be non-empty with a digit sequence after it. Whether the epoch
    is the CURRENT one is decided downstream by the broker, which replays from the start on any
    mismatch. Pinning this stops a future "tighten the parser" change from turning a resumable
    reconnect into a 400 the client cannot act on.
    """
    from app.agent_proxy.broker import parse_cursor

    cursor = parse_cursor(last_event_id="1.5-2", after_seq=None)
    assert (cursor.seq, cursor.epoch) == (2, "1.5")


def test_a_valid_after_seq_is_accepted_by_the_parser() -> None:
    """Paired negative: a parser that rejected everything would satisfy the 400 matrix above."""
    from app.agent_proxy.broker import parse_cursor

    assert parse_cursor(last_event_id=None, after_seq="0").seq == 0
    assert parse_cursor(last_event_id=None, after_seq="41").seq == 41


@pytest.mark.parametrize("after_seq", ["-1", "1.5", "abc", "", " ", "1e3", "0x10"])
def test_the_parser_rejects_every_non_integer_after_seq(after_seq: str) -> None:
    """The same matrix as the route test, at the level where the rule is implemented."""
    from app.agent_proxy.broker import parse_cursor
    from app.errors import BadRequestError

    with pytest.raises(BadRequestError):
        parse_cursor(last_event_id=None, after_seq=after_seq)


def test_last_event_id_wins_over_after_seq() -> None:
    """Both present: the header is the live reconnect signal, the query is the manual override.

    Asserted at the parsing boundary because the precedence is invisible in a response status.
    """
    from app.agent_proxy.broker import parse_cursor

    cursor = parse_cursor(last_event_id="abcdef-7", after_seq="99")
    assert (cursor.seq, cursor.epoch) == (7, "abcdef")


# ==================================================================================================
# (4) The kill-switch decides WHO does the domain work — one or the other, never both.
# ==================================================================================================
@pytest.mark.asyncio
async def test_the_broker_path_performs_no_domain_work(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """(4) The client stream must be READ-ONLY: no billing, no snapshot, no status transition.

    Two subscribers doing the domain work would double-charge every run. Asserted on the DATABASE —
    a ledger row, a snapshot row and a status change are all observable facts — and driven at the
    service level rather than through the route, because the route needs a live Redis to open the
    stream at all while this property does not depend on Redis in the slightest.
    """
    from app.agent_proxy.broker import Cursor

    run_id = "run_readonly"
    async with db_sessionmaker() as session:
        user_id = await seed_user(session, subscription="active", balance=1000)
        await _insert_run(session, run_id=run_id, user_id=user_id)
        await session.commit()

    class _EmptyBroker:
        """Stands in for the real broker: the run exists, it simply has no events yet."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def stream(self, *, run_id: str, cursor: Any) -> Any:
            self.calls.append(run_id)
            return
            yield b""  # pragma: no cover - marks this as an async generator

    broker = _EmptyBroker()
    async with db_sessionmaker() as session:
        service = _service_with_broker(session, broker)
        chunks = [
            chunk
            async for chunk in service.stream_events(
                user_id=user_id, run_id=run_id, cursor=Cursor()
            )
        ]

    assert chunks == []
    assert broker.calls == [run_id], "the request must be served from the broker, not from Hermes"

    async with db_sessionmaker() as session:
        debits = (
            await session.execute(
                text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
                {"u": str(user_id)},
            )
        ).scalar_one()
        status = (
            await session.execute(
                text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()
        snapshots = (
            await session.execute(
                text("SELECT count(*) FROM agent_run_snapshots WHERE run_id=:r"), {"r": run_id}
            )
        ).scalar_one()

    assert debits == 0, "the client path billed the run — the consumer is the only biller"
    assert str(status) == "running", "the client path moved the lifecycle status"
    assert snapshots == 0, "the client path wrote a snapshot"


@pytest.mark.asyncio
async def test_the_broker_path_refuses_a_foreign_run_before_yielding_anything(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """(1) The SECURITY half of the defect, stated independently of the status code.

    Whatever the transport ends up reporting, the guarantee that matters is that not one byte of
    another user's stream is produced and the broker is never even consulted. This passes today; the
    route-level 404 above does not (see the QA report) — keeping them separate makes the difference
    between "leaks data" and "reports the wrong status" explicit.
    """
    from app.agent_proxy.broker import Cursor
    from app.errors import NotFoundError

    async with db_sessionmaker() as session:
        owner = await seed_user(session, subscription="active", balance=100)
        intruder = await seed_user(session, subscription="active", balance=100)
        await _insert_run(session, run_id="run_a", user_id=owner)
        await session.commit()

    class _TrackingBroker:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def stream(self, *, run_id: str, cursor: Any) -> Any:
            self.calls.append(run_id)  # pragma: no cover - must never be reached
            yield b"leaked"

    broker = _TrackingBroker()
    async with db_sessionmaker() as session:
        service = _service_with_broker(session, broker)
        produced: list[bytes] = []
        with pytest.raises(NotFoundError):
            async for chunk in service.stream_events(
                user_id=intruder, run_id="run_a", cursor=Cursor()
            ):
                produced.append(chunk)  # pragma: no cover

    assert produced == [], "a foreign subscriber received stream bytes"
    assert broker.calls == [], "the broker was consulted for a run the caller does not own"


# ==================================================================================================
# (5) The `id:` field — the whole reconnect contract rests on it.
# ==================================================================================================
def test_every_emitted_block_carries_an_id_of_epoch_and_seq() -> None:
    """``id: {epoch}-{seq}`` prefixed to the VERBATIM upstream bytes.

    Two properties in one: the client gets a resumable cursor, and the payload below it is
    untouched — the broker relays what the image sent, it does not re-encode it.
    """
    from app.agent_proxy.broker import sse_block

    raw = b'data: {"event": "message.delta", "delta": "hi"}\n\n'
    block = sse_block(epoch="deadbeef", seq=42, data=raw)

    assert block.startswith(b"id: deadbeef-42\n")
    assert block.endswith(raw), "the upstream bytes must be relayed verbatim under the id line"


def test_the_id_round_trips_through_the_cursor_parser() -> None:
    """A reconnect must be INCREMENTAL: the id the client last saw has to parse back to that seq.

    An epoch containing the separator is the interesting case — the split is a right-partition, so a
    hyphenated epoch still yields the right sequence number.
    """
    from app.agent_proxy.broker import parse_cursor, sse_block

    for epoch, seq in (("deadbeef", 42), ("epoch-with-dashes", 7), ("e", 0)):
        block = sse_block(epoch=epoch, seq=seq, data=b"data: {}\n\n")
        emitted_id = block.split(b"\n", 1)[0].removeprefix(b"id: ").decode()
        cursor = parse_cursor(last_event_id=emitted_id, after_seq=None)
        assert (cursor.seq, cursor.epoch) == (seq, epoch), emitted_id


def test_truncation_marker_is_a_well_formed_event_with_an_id() -> None:
    """The gap signal is itself a normal, resumable SSE event — not a side channel."""
    import json

    from app.agent_proxy.broker import EVENT_RUN_TRUNCATED, truncation_marker

    block = truncation_marker(epoch="abc", seq=5, run_id="run_1", from_seq=99)
    assert block.startswith(b"id: abc-5\n")
    payload = json.loads(block.split(b"data: ", 1)[1].decode())
    assert payload["event"] == EVENT_RUN_TRUNCATED
    assert payload["run_id"] == "run_1"
    assert payload["from_seq"] == 99


# ==================================================================================================
# (1, extended) Ownership is now asserted on ALL FIVE runId routes, not only /events.
#
# The /events defect was a symptom of a general rule: the guard was never written down anywhere,
# it was a side effect of ensure_running resolving the CALLER's instance. That side effect vanished
# the moment one route changed executor, and nothing failed — which is precisely the argument for
# asserting the property directly on every route that takes a runId from the path.
# ==================================================================================================
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/agent/runs/{run_id}/stop", None),
        ("POST", "/v1/agent/runs/{run_id}/approval", {"choice": "once"}),
        ("POST", "/v1/agent/runs/{run_id}/resume", {}),
        ("GET", "/v1/agent/runs/{run_id}/state", None),
    ],
)
@pytest.mark.asyncio
async def test_every_run_id_route_hides_a_foreign_run(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    method: str,
    path: str,
    body: Any,
) -> None:
    """404 on someone else's run — and BEFORE any upstream side effect.

    The check runs ahead of ``ensure_running`` on purpose: rejecting later would let a foreign
    ``runId`` wake our instance and reach Hermes — a state change on another user's run — before the
    request was refused. There is no Hermes in this contour, so a route that checked ownership too
    late would surface as a 502 rather than a 404, which is exactly what this distinguishes.
    """
    async with db_sessionmaker() as session:
        owner = await seed_user(session, subscription="active", balance=1000)
        intruder = await seed_user(session, subscription="active", balance=1000)
        await _insert_run(session, run_id="run_of_a", user_id=owner, status="paused")
        await session.commit()

    url = path.format(run_id="run_of_a")
    headers = auth_headers(intruder)
    response = (
        await client.get(url, headers=headers)
        if method == "GET"
        else await client.post(url, headers=headers, json=body)
    )
    assert response.status_code == 404, (
        f"{method} {url} answered {response.status_code}; a foreign runId must be invisible "
        "and must not reach upstream first"
    )


@pytest.mark.parametrize(
    ("method", "path", "valid_body"),
    [
        ("POST", "/v1/agent/runs/{run_id}/stop", {}),
        ("POST", "/v1/agent/runs/{run_id}/approval", {"choice": "once"}),
        ("GET", "/v1/agent/runs/{run_id}/state", None),
    ],
)
@pytest.mark.asyncio
async def test_a_valid_request_cannot_distinguish_foreign_from_nonexistent(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    method: str,
    path: str,
    valid_body: Any,
) -> None:
    """Indistinguishability, stated over a FIXED request shape.

    The invariant is NOT "404 outranks 422" — that was a plausible-sounding consequence, and pinning
    it would have frozen a requirement nobody actually holds. What must hold is that for one and the
    same request shape, a run the caller does not own is indistinguishable from a run that never
    existed. Body validation runs before the handler, so it can only ever answer identically for
    both classes; it therefore reveals nothing and needs no ordering rule.

    Here the body is VALID, so the request reaches the ownership check and both classes must answer
    the same way, byte for byte apart from the request id.
    """
    async with db_sessionmaker() as session:
        owner = await seed_user(session, subscription="active", balance=1000)
        caller = await seed_user(session, subscription="active", balance=1000)
        await _insert_run(session, run_id="run_of_someone_else", user_id=owner, status="paused")
        await session.commit()

    async def _call(run_id: str) -> Any:
        url = path.format(run_id=run_id)
        headers = auth_headers(caller)
        if method == "GET":
            return await client.get(url, headers=headers)
        return await client.post(url, headers=headers, json=valid_body)

    foreign = await _call("run_of_someone_else")
    missing = await _call("run_that_never_existed")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert (
        foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    ), "the two classes must not be distinguishable by the message either"


@pytest.mark.parametrize(
    "run_id_kind", ["own", "foreign", "missing"], ids=["own", "foreign", "missing"]
)
@pytest.mark.asyncio
async def test_an_invalid_body_answers_422_for_every_ownership_class(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    run_id_kind: str,
) -> None:
    """The other half: with an INVALID body the answer is 422 whoever the run belongs to.

    That uniformity is the whole reason the ordering question is moot. A 422 is produced by schema
    validation, which never looks at the run, so it cannot leak whether the run exists or who owns
    it — including for the caller's OWN run, which is the case that proves the 422 is about the body
    and not about the resource.
    """
    async with db_sessionmaker() as session:
        caller = await seed_user(session, subscription="active", balance=1000)
        owner = await seed_user(session, subscription="active", balance=1000)
        await _insert_run(session, run_id="run_mine", user_id=caller, status="paused")
        await _insert_run(session, run_id="run_theirs", user_id=owner, status="paused")
        await session.commit()

    run_id = {"own": "run_mine", "foreign": "run_theirs", "missing": "run_absent"}[run_id_kind]
    response = await client.post(
        f"/v1/agent/runs/{run_id}/approval",
        headers=auth_headers(caller),
        json={"choice": "not-a-valid-choice"},
    )
    assert response.status_code == 422, (
        f"{run_id_kind} run answered {response.status_code} to an invalid body — the response to a "
        "malformed body must not depend on the run at all"
    )


# ==================================================================================================
# The restored regression, plus a tripwire for the CLASS of defect it belonged to.
# ==================================================================================================
@pytest.mark.asyncio
async def test_foreign_run_is_404_through_the_route_and_the_connection_survives(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    consumer_on: None,
) -> None:
    """Owner A's run must be invisible to caller B — as a STATUS, on a connection that stays usable.

    Two assertions, because the defect had two symptoms and only one of them was about RBAC:
    the client must receive 404 (not a 200 with a truncated body), and the NEXT request over the
    same connection must still work. The second half is what used to take the whole suite down, so
    it is asserted explicitly rather than assumed.
    """
    async with db_sessionmaker() as session:
        owner = await seed_user(session, subscription="active", balance=100)
        intruder = await seed_user(session, subscription="active", balance=100)
        await _insert_run(session, run_id="run_owned_by_a", user_id=owner)
        await session.commit()

    response = await client.get(
        _EVENTS.format(run_id="run_owned_by_a"), headers=auth_headers(intruder)
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"
    assert (
        "run_owned_by_a" not in response.text
    ), "the 404 body must not confirm the run id back to a caller who does not own it"

    # THE SECOND SYMPTOM: the old failure mode left the connection wedged.
    follow_up = await client.get(
        _EVENTS.format(run_id="run_never_created"), headers=auth_headers(intruder)
    )
    assert (
        follow_up.status_code == 404
    ), "the connection did not survive the rejection — this is the symptom that hung the suite"


@pytest.mark.asyncio
async def test_unknown_run_is_404_through_the_route(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    consumer_on: None,
) -> None:
    """A run that does not exist is indistinguishable from one the caller may not see."""
    async with db_sessionmaker() as session:
        user_id = await seed_user(session, subscription="active", balance=100)
        await session.commit()

    response = await client.get(
        _EVENTS.format(run_id="run_never_created"), headers=auth_headers(user_id)
    )
    assert response.status_code == 404


def test_a_rejection_raised_inside_a_streaming_body_cannot_become_a_status() -> None:
    """The FRAMEWORK fact that makes the rule necessary — pinned so the rule has a reason on file.

    This is not a test of our code; it is a test of the constraint our code has to live with. If a
    future Starlette ever delayed ``http.response.start`` until the first chunk, this test would
    fail and the rule below could be relaxed deliberately rather than by accident.
    """
    import asyncio
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse
    from httpx import ASGITransport
    from httpx import AsyncClient as RawClient

    from app.errors import NotFoundError

    app = FastAPI()

    @app.exception_handler(NotFoundError)
    async def _handler(_request: Any, _exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    async def body() -> AsyncIterator[bytes]:
        raise NotFoundError("run not found")
        yield b""  # pragma: no cover - marks this as a generator

    # No return annotation on purpose: FastAPI resolves annotations against MODULE globals, and
    # StreamingResponse is imported inside this function.
    @app.get("/stream")
    async def _route():  # noqa: ANN202
        return StreamingResponse(body(), media_type="text/event-stream")

    async def _probe() -> str:
        async with RawClient(transport=ASGITransport(app=app), base_url="http://t") as raw:
            try:
                response = await raw.get("/stream")
            except RuntimeError as exc:
                return f"RuntimeError: {exc}"
            return f"status {response.status_code}"

    outcome = asyncio.run(_probe())
    assert outcome.startswith("RuntimeError"), (
        "Starlette no longer commits the response before the body is pulled — the "
        "check-before-the-response rule can be revisited deliberately"
    )
    assert "response already started" in outcome


def test_the_set_of_streaming_routes_is_known() -> None:
    """TRIPWIRE for the class of defect, not for this one instance.

    Every streaming route must perform its status-bearing checks BEFORE constructing the response,
    because inside the body they cannot produce a status (see the test above). That rule cannot be
    verified generically — only the author knows what a given route must reject — so this test
    freezes the SET instead: adding a streaming route fails here and forces whoever adds it to
    state, in this file, that the ownership/validation checks run in the handler.
    """
    import inspect

    from fastapi.responses import StreamingResponse

    from app.main import create_app

    streaming: set[str] = set()
    for route in create_app().routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        annotation = inspect.signature(endpoint).return_annotation
        if annotation is StreamingResponse or "StreamingResponse" in str(annotation):
            streaming.add(f"{sorted(getattr(route, 'methods', []) or [])} {route.path}")

    assert streaming == {"['GET'] /v1/agent/runs/{run_id}/events"}, (
        "a streaming route was added or removed. Every status-bearing check on it (RBAC, cursor "
        "validation, body validation) MUST run in the handler before StreamingResponse is built — "
        "inside the generator it becomes a RuntimeError over an already-started response. Add the "
        "route here together with its own before-the-response coverage."
    )
