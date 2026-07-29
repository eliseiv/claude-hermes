"""Contract + regression: the REAL Hermes SSE wire shape of the patched image (ADR-065/ADR-066).

WHY THIS MODULE EXISTS (the test gap that let a prod defect through).
Every ``message.delta`` fixture in the suite was written as ``{"text": …}`` / ``{"delta": {"text":
…}}`` — shapes invented together with the parser, never taken from the image. The patched
production image actually emits ``"delta"`` as a **bare string**, so ``_extract_delta_text`` missed
on EVERY delta and ``resultText`` was identically empty on prod (ADR-066 defect) while the whole
suite stayed green: the fixtures asserted the code against its own assumption.

FIRST SOURCE (06-testing-strategy.md / shared-block v2 — an external-integration fixture is taken
from the source, never from our code or docs). ``tests/fixtures/hermes_prod_run_adr065.sse`` is a
**raw capture of a live production run** (devops, 2026-07-29, run
``run_d931839587a64e3885b4d096cf7440d0``, credits-exhausted). It is byte-verbatim from the capture;
the only edit is DELETION of whole ``message.delta`` blocks from the middle of the answer to shorten
the user content. No string value, key, escape or separator was rewritten, so every structural fact
under test comes from the image:

* wire format: blocks of ``data: {json}`` separated by a blank LINE FEED line — **no ``event:``
  header line at all**; the type travels in the JSON field ``"event"`` (dispatch must not depend on
  the SSE header);
* ``message.delta``: ``{"event", "run_id", "timestamp", "delta"}`` with **``delta`` a bare string**;
* ``usage.delta``: flat ``step_index`` / ``input_tokens`` / ``output_tokens`` / ``cumulative_*`` /
  ``model`` (NOT nested under ``usage``);
* ``run.paused``: our own synthetic terminal block as it reached the client on prod — with
  ``"output": ""``, i.e. the defect itself, frozen as evidence.

NOT OBSERVED in the capture (unreachable at zero balance): ``tool.started`` / ``tool.completed`` /
``run.completed`` / ``approval.request``. No fixture for them is invented here; the existing tests
that use them assert against an ASSUMED shape (see ``unverified_external_assumptions`` in the QA
report) — the ``run.completed{usage}`` nesting in particular is NOT confirmed by any capture.
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.agent_proxy import service as service_module
from app.agent_proxy.service import (
    _DELTA_SILENT_WARN_AFTER,
    _SHAPE_SUMMARY_MAX_KEYS,
    _USAGE_ANCHOR_STALL_WARN_AFTER,
    _USAGE_GATE_MAX_DEPTH,
    _as_int,
    _delta_shape_looks_unknown,
    _event_name,
    _extract_delta_text,
    _extract_tool_name,
    _extract_usage_counts,
    _has_token_like_field,
    _is_provably_usage_free,
    _iter_sse_blocks,
    _parse_sse_block,
    _shape_summary,
    _SseEvent,
)
from tests.unit.test_agent_proxy_service import _BASE_URL, _capture_service_logs, _collect
from tests.unit.test_agent_run_state_adr066 import _events_route, _settings, _wire

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hermes_prod_run_adr065.sse"
_RUN_ID = "run_d931839587a64e3885b4d096cf7440d0"
# The answer text carried by the retained delta blocks of the capture (ground truth for the
# accumulator: this is what /state.resultText and run.paused.output must contain).
_EXPECTED_TEXT = "Я не могу сейчас выходить в интернет из этой среды 2024?"


def _dump_bytes() -> bytes:
    return _FIXTURE.read_bytes()


def _dump_blocks() -> list[bytes]:
    """The raw capture split into SSE blocks (no re-encoding: bytes straight from the file)."""
    return [b for b in _dump_bytes().split(b"\n\n") if b.strip()]


def _dump_events() -> list[_SseEvent]:
    return [_parse_sse_block(b) for b in _dump_blocks()]


def _block(payload: dict[str, Any]) -> bytes:
    """One SSE block in the PRODUCTION wire format: ``data: {json}`` + blank LF line, no header."""
    return f"data: {json.dumps(payload)}\n\n".encode()


def _stream_response(body: bytes, *, chunk_size: int | None = None) -> httpx.Response:
    """An httpx response over ``body``; ``chunk_size`` fragments it like a real socket would."""
    if chunk_size is None:
        return httpx.Response(200, content=body)

    async def _chunks() -> Any:
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]

    return httpx.Response(200, content=_chunks())


# ==================================================================================================
# A. Wire format of the capture — dispatch must read the JSON field, not the SSE header.
# ==================================================================================================
def test_prod_capture_has_no_sse_event_header_line() -> None:
    body = _dump_bytes()
    assert b"\r" not in body, "the capture is LF-separated; a CRLF fixture would be a rewrite"
    assert b"\nevent:" not in body and not body.startswith(
        b"event:"
    ), "the patched image emits NO `event:` header line — a fixture with one is invented"
    for block in _dump_blocks():
        for line in block.split(b"\n"):
            assert line.startswith(b"data: "), line
        # The parser must therefore resolve no header name at all.
        assert _parse_sse_block(block).name is None


def test_prod_capture_dispatches_by_the_json_event_field() -> None:
    names = [_event_name(e) for e in _dump_events()]
    # Every block resolves — via `data.event`, since `_SseEvent.name` is None throughout.
    assert None not in names
    assert set(names) == {"message.delta", "usage.delta", "run.paused"}
    # Terminal ordering of the real run: usage anchor, then the synthetic pause. No run.completed.
    assert names[-2:] == ["usage.delta", "run.paused"]
    assert "run.completed" not in names


@pytest.mark.parametrize("chunk_size", [None, 1, 7, 64, 4096])
async def test_iter_sse_blocks_reassembles_the_prod_capture(chunk_size: int | None) -> None:
    """The real stream survives arbitrary socket fragmentation and is relayed byte-for-byte."""
    body = _dump_bytes()
    seen: list[tuple[_SseEvent, bytes]] = []
    async for block, raw in _iter_sse_blocks(_stream_response(body, chunk_size=chunk_size)):
        seen.append((block, raw))
    assert len(seen) == len(_dump_blocks())
    assert b"".join(raw for _, raw in seen) == body, "relay mutated the wire bytes"
    assert [_event_name(b) for b, _ in seen] == [_event_name(e) for e in _dump_events()]


# ==================================================================================================
# B. message.delta — bare string is THE production shape (the defect this module guards).
# ==================================================================================================
def test_prod_capture_message_delta_carries_a_bare_string_delta() -> None:
    deltas = [e for e in _dump_events() if _event_name(e) == "message.delta"]
    assert deltas, "the capture must carry message.delta blocks"
    for event in deltas:
        assert isinstance(event.data["delta"], str), event.data
        # The shapes the suite used to assume are simply absent from the real payload.
        assert "text" not in event.data and "content" not in event.data
        assert set(event.data) == {"event", "run_id", "timestamp", "delta"}


def test_extract_delta_text_reads_every_delta_of_the_prod_capture() -> None:
    deltas = [e for e in _dump_events() if _event_name(e) == "message.delta"]
    pieces = [_extract_delta_text(e) for e in deltas]
    assert all(pieces), f"{pieces.count('')} of {len(pieces)} real deltas resolved to empty text"
    assert "".join(pieces) == _EXPECTED_TEXT


def _pre_fix_extract_delta_text(event: _SseEvent) -> str:
    """VERBATIM copy of ``_extract_delta_text`` as it stood BEFORE the ADR-066 parser fix.

    Kept as an executable oracle of the shipped defect: it makes the regression above provably
    fail-on-old-code instead of merely asserting today's behaviour (the new fixture alone would be
    indistinguishable from a test that always passed).
    """
    data = event.data
    value = data.get("text")
    if isinstance(value, str):
        return value
    delta = data.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        return str(delta["text"])
    content = data.get("content")
    if isinstance(content, str):
        return content
    return ""


def test_pre_fix_extractor_returns_empty_on_the_whole_prod_capture() -> None:
    """Proof that the regression above FAILS on the pre-fix parser (ADR-066 defect reproduced)."""
    deltas = [e for e in _dump_events() if _event_name(e) == "message.delta"]
    assert [_pre_fix_extract_delta_text(e) for e in deltas] == [""] * len(deltas)
    # ... while the invented fixture shape kept the old parser (and the whole suite) green — this is
    # exactly why the defect was invisible.
    assert _pre_fix_extract_delta_text(_SseEvent(None, {"delta": {"text": "x"}})) == "x"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"delta": "прод"}, "прод"),  # the production shape
        ({"delta": {"text": "wrapped"}}, "wrapped"),  # the nested build — must not regress
        ({"text": "top"}, "top"),
        ({"content": "c"}, "c"),
        ({"content": {"text": "cw"}}, "cw"),
        ({"delta": " "}, " "),  # whitespace-only fragment (present in the capture) is real text
        ({"delta": ""}, ""),  # legitimately empty
        ({"delta": {"blocks": []}}, ""),  # structured, no text carrier
        ({"delta": 42}, ""),
    ],
)
def test_extract_delta_text_shape_matrix(payload: dict[str, Any], expected: str) -> None:
    assert _extract_delta_text(_SseEvent(None, payload)) == expected


# ==================================================================================================
# C. usage.delta — the fields the biller/snapshot actually read, checked against the capture.
# ==================================================================================================
def test_prod_capture_usage_delta_carries_every_field_the_relay_reads() -> None:
    (usage,) = (e for e in _dump_events() if _event_name(e) == "usage.delta")
    # Read by _bill_step / the snapshot token anchors. A missing key here would be a code defect.
    for key in (
        "step_index",
        "input_tokens",
        "output_tokens",
        "cumulative_input_tokens",
        "cumulative_output_tokens",
        "model",
    ):
        assert key in usage.data, f"{key} missing from the real usage.delta"
    # Flat, NOT nested under "usage". NB: the nesting on run.completed is ASSUMED, not confirmed —
    # no capture reaches run.completed (every one so far ends at run.paused), so the relay accepts
    # both layouts (_extract_usage_counts) instead of betting on one.
    assert "usage" not in usage.data
    assert _as_int(usage.data["cumulative_input_tokens"]) == 6313
    assert _as_int(usage.data["cumulative_output_tokens"]) == 658
    assert _as_int(usage.data["step_index"]) == 1
    assert usage.data["model"] == "gpt-5-mini"


def test_prod_capture_run_paused_output_was_empty_the_defect_evidence() -> None:
    """The synthetic terminal block AS DELIVERED ON PROD: ``output`` empty — the ADR-066 defect."""
    (paused,) = (e for e in _dump_events() if _event_name(e) == "run.paused")
    assert paused.data["reason"] == "credits_exhausted"
    assert paused.data["output"] == "", "capture no longer shows the defect it was taken to record"
    assert paused.data["usage"] == {
        "cumulative_input_tokens": 6313,
        "cumulative_output_tokens": 658,
    }


# ==================================================================================================
# D. Relay end-to-end over the real capture (fakes for the DB, respx for the Hermes boundary).
# ==================================================================================================
@respx.mock
async def test_relay_over_prod_capture_fills_result_text_and_pauses_with_nonempty_output() -> None:
    """The whole defect, end to end: real bytes in → non-empty snapshot AND non-empty pause body."""
    uid = uuid.uuid4()
    # Flush every delta (throttling is asserted elsewhere); incremental billing ON with a zero
    # balance reproduces the captured run: usage.delta → depleted → synthetic run.paused.
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=True)
    )
    w.wallet.current_balance_value = 0
    _events_route(_dump_bytes(), run_id=_RUN_ID)
    respx.post(f"{_BASE_URL}/v1/runs/{_RUN_ID}/stop").mock(
        return_value=httpx.Response(200, json={})
    )

    with _capture_service_logs() as logs:
        out = await _collect(w.svc.stream_events(user_id=uid, run_id=_RUN_ID))

    # 1. Every captured byte up to the depleting usage.delta is relayed verbatim, then the stream
    #    terminates with OUR synthetic run.paused (the capture's own trailing run.paused block is
    #    ours too and is never reached: the relay returns at depletion).
    upstream = b"".join(
        b + b"\n\n" for b in _dump_blocks() if _event_name(_parse_sse_block(b)) != "run.paused"
    )
    assert out.startswith(upstream), "relay mutated or dropped upstream bytes"
    emitted = json.loads(out[len(upstream) :].split(b"data: ", 1)[1])
    assert emitted["event"] == "run.paused"
    assert emitted["reason"] == "credits_exhausted"
    # 2. THE REGRESSION: the pause body carries the answer instead of "" (prod behaviour above).
    assert emitted["output"] == _EXPECTED_TEXT
    assert emitted["usage"]["cumulative_input_tokens"] == 6313
    # 3. ... and so does the snapshot feeding GET /state.resultText.
    assert w.snapshots.upserts, "no snapshot write at all"
    assert w.snapshots.upserts[-1]["result_text"] == _EXPECTED_TEXT
    assert w.snapshots.stored[_RUN_ID]["result_text"] == _EXPECTED_TEXT
    assert w.snapshots.upserts[-1]["input_tokens"] == 6313
    assert w.snapshots.upserts[-1]["output_tokens"] == 658
    # 4. The shape is RECOGNISED, so the drift warning must stay silent on a healthy stream.
    assert "carrier unknown" not in "\n".join(logs.messages)
    assert w.runs.mark_paused_calls == [(_RUN_ID, "credits_exhausted")]
    assert w.runs.mark_stopped_calls == [], "pause-at-zero must not route through the stop path"


@respx.mock
async def test_relay_still_accepts_the_nested_delta_build() -> None:
    """Builds that nest the fragment (``{"delta": {"text": …}}``) must not regress to empty."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = b"".join(
        _block({"event": "message.delta", "delta": {"text": p}}) for p in ("nes", "ted")
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert w.snapshots.upserts[-1]["result_text"] == "nested"
    assert "carrier unknown" not in "\n".join(logs.messages)


@respx.mock
async def test_legitimately_empty_delta_is_silent() -> None:
    """A carrier that is present but empty is normal traffic — it must NOT raise the drift alarm."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": d}) for d in ("", "", "")
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert "carrier unknown" not in "\n".join(
        logs.messages
    ), "an empty fragment must not be reported as drift"


@respx.mock
async def test_unknown_delta_shape_logs_exactly_one_warning_without_payload_values() -> None:
    """The observability half of the fix: a future rename is LOUD, once, and leaks no user text."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    secret = "PRIVATE-USER-ANSWER-TEXT"
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "fragment": secret}) for _ in range(5)
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    lines = [m for m in logs.messages if "message.delta text carrier unknown" in m]
    assert len(lines) == 1, f"latch broken: {lines}"
    line = lines[0]
    assert "run_1" in line
    assert "'fragment'" in line, "the key NAME must be reported (that is the actionable part)"
    assert secret not in line, "payload VALUES are user content and must never be logged"


# ==================================================================================================
# E. THE AGGREGATE SILENT-MISS LATCH — the guard that would have caught the ADR-066 defect.
#
# Everything above proves the parser reads today's wire shape. This section proves the relay
# NOTICES the next shape it cannot read, which is the part that actually failed on prod: the defect
# ran for a whole run, on every delta, and emitted not one log line.
# ==================================================================================================
def _replay_capture_through_pre_fix_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the historical defect into the live relay: the pre-fix extractor, nothing else.

    This is DELIBERATE defect injection, not mocking-our-own-code for convenience: the property
    under test is "the guard fires on the failure that actually shipped", and the only faithful way
    to state it is to run the real relay, over the real captured bytes, with the one function that
    was wrong. Substituting a hand-made broken payload instead would prove a different (easier)
    thing — see ``test_aggregate_latch_fires_on_a_dict_shaped_delta_without_any_injection`` for the
    injection-free half of the pair.
    """
    monkeypatch.setattr(service_module, "_extract_delta_text", _pre_fix_extract_delta_text)


@respx.mock
async def test_aggregate_latch_fires_once_on_the_real_capture_read_by_the_pre_fix_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A. The prod defect replayed end to end: ONE aggregate warning, and it is the only line.

    Note what does NOT fire: the per-event shape probe stays silent for the whole run, because the
    payload was never malformed — ``"delta": "<text>"`` is a perfectly normal non-empty string that
    the old extractor simply refused to read. That is precisely why the aggregate latch exists, and
    this assertion is the executable statement of that gap.
    """
    _replay_capture_through_pre_fix_extractor(monkeypatch)
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    n_deltas = sum(1 for e in _dump_events() if _event_name(e) == "message.delta")
    assert n_deltas >= _DELTA_SILENT_WARN_AFTER, (
        f"the capture fixture carries {n_deltas} deltas, below the {_DELTA_SILENT_WARN_AFTER} "
        "threshold — it can no longer prove the latch fires"
    )
    _events_route(_dump_bytes(), run_id=_RUN_ID)

    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id=_RUN_ID))

    silent = [m for m in logs.messages if "yielded no text" in m]
    assert len(silent) == 1, f"latch must fire exactly once per relay: {silent}"
    # The latch reports the count at which it gave up (the threshold), not the run total.
    assert f"no text for {_DELTA_SILENT_WARN_AFTER} events" in silent[0]
    assert _RUN_ID in silent[0]
    # The per-event heuristic is structurally blind to this defect (documented gap, not a bug).
    assert [m for m in logs.messages if "carrier unknown" in m] == []
    # F. Key names only — not one character of the captured answer reaches the log.
    assert "'delta'" in silent[0] and "'run_id'" in silent[0]
    for word in ("могу", "интернет", "среды", "2024"):
        assert word not in silent[0], "payload VALUES leaked into the log line"
    # The defect's observable signature is intact: nothing accumulated for the whole run.
    assert all(u["result_text"] == "" for u in w.snapshots.upserts)


@respx.mock
async def test_aggregate_latch_is_silent_on_the_healthy_capture() -> None:
    """A (paired negative). The same bytes on the FIXED path: no aggregate warning at all.

    Without this half the test above would also pass on a latch wired to fire unconditionally.
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_dump_bytes(), run_id=_RUN_ID)
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id=_RUN_ID))
    assert [m for m in logs.messages if "yielded no text" in m] == []
    assert w.snapshots.upserts[-1]["result_text"] == _EXPECTED_TEXT


@respx.mock
async def test_aggregate_latch_fires_on_a_dict_shaped_delta_without_any_injection() -> None:
    """A (injection-free half). 61 text-less deltas from a shape the parser genuinely cannot read.

    61 is an arbitrary "short run" order of magnitude, NOT a property of any capture: the committed
    one (``tests/fixtures/hermes_prod_run_adr065.sse``) holds 15 message.delta blocks. What matters
    here is only that it exceeds ``_DELTA_SILENT_WARN_AFTER``. Both latches are asserted to fire
    exactly once each over the whole stream: the aggregate one and the per-event shape probe are
    independent, and neither may repeat per event.
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    secret = "PRIVATE-USER-ANSWER-TEXT"
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": {"chunk": secret}})
        for _ in range(61)
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    silent = [m for m in logs.messages if "yielded no text" in m]
    shape = [m for m in logs.messages if "carrier unknown" in m]
    assert len(silent) == 1, silent
    assert len(shape) == 1, shape
    # The latch reports WHEN it gave up (the threshold), not the whole stream length: it fires on
    # the 10th event and never re-arms.
    assert f"no text for {_DELTA_SILENT_WARN_AFTER} events" in silent[0]
    # F. Neither line carries payload values.
    assert secret not in silent[0] and secret not in shape[0]


# Upper bound on how many SSE blocks a threshold test may synthesise. The counts below are
# deliberately CONSTANTS, not arithmetic on _DELTA_SILENT_WARN_AFTER: deriving them coupled the
# runtime of this file to a production constant, and raising that constant to 10**9 (while probing
# whether these tests really depend on the latch) made the generator try to build a billion blocks
# and hang the run. A test must fail loudly on an unexpected constant, never grow without bound.
_MAX_SYNTHESISED_BLOCKS = 1000


def _bounded(count: int, *, derived_from: str = "") -> int:
    """Fail fast instead of synthesising an unbounded stream.

    Every count in this file that is DERIVED from a production constant passes through here. Twice
    now, probing whether these tests really depend on a guard (by inflating its constant) turned a
    derived count into millions of SSE blocks and hung the run — the second time it also skipped the
    restore step of the probe, leaving mutated source in the tree. A loud assertion is the fix: a
    changed constant must break the test with a message, never with a wall clock.
    """
    assert 0 <= count <= _MAX_SYNTHESISED_BLOCKS, (
        f"refusing to synthesise {count} SSE blocks (from {derived_from or 'a literal'}); raise "
        "_MAX_SYNTHESISED_BLOCKS deliberately or rethink the constant"
    )
    return count


def _require_testable_threshold() -> None:
    _bounded(_DELTA_SILENT_WARN_AFTER, derived_from="_DELTA_SILENT_WARN_AFTER")


@pytest.mark.parametrize("n_deltas", [0, 1, 9])
@respx.mock
async def test_aggregate_latch_stays_silent_below_the_threshold(n_deltas: int) -> None:
    """B. Below ``_DELTA_SILENT_WARN_AFTER`` a fully text-less run is NOT reported.

    Recorded as a DELIBERATE LIMITATION, not an oversight: a run that produces fewer than
    ``_DELTA_SILENT_WARN_AFTER`` deltas and no text at all — a run stopped/paused/failed within the
    first instants — would go unflagged, and a shape defect visible only on such runs would stay
    invisible here. The threshold buys silence on runs whose opening deltas are legitimately empty;
    this test pins the cost of that trade so a future change of the constant is a conscious one.
    """
    assert n_deltas < _DELTA_SILENT_WARN_AFTER, (
        "the threshold moved below the counts this test calls 'below threshold' — update both "
        "together, deliberately"
    )
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": ""}) for _ in range(n_deltas)
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "yielded no text" in m] == []


@respx.mock
async def test_aggregate_latch_fires_exactly_at_the_threshold() -> None:
    """B (boundary). The ``>=`` edge: the Nth text-less delta is the one that warns."""
    _require_testable_threshold()
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": ""})
        for _ in range(_bounded(_DELTA_SILENT_WARN_AFTER))
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    silent = [m for m in logs.messages if "yielded no text" in m]
    assert len(silent) == 1
    assert f"no text for {_DELTA_SILENT_WARN_AFTER} events" in silent[0]


@respx.mock
async def test_one_late_readable_delta_disarms_the_aggregate_latch_for_the_whole_run() -> None:
    """B (the other edge of the trade-off): the guard is about a TOTALLY silent run, per relay.

    ``delta_text_seen`` is sticky, so a run that emits 50 unreadable deltas and then one readable
    one never warns. Deliberate — the guard's claim is "this relay extracted nothing at all" — but
    it means a PARTIAL shape drift (one carrier among several renamed) is out of its reach.
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = _block({"event": "message.delta", "run_id": "run_1", "delta": "ok"}) + b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": {"chunk": "x"}})
        for _ in range(50)
    )
    _events_route(body, run_id="run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "yielded no text" in m] == []
    # The per-event probe is what still covers this case (once).
    assert len([m for m in logs.messages if "carrier unknown" in m]) == 1


# ==================================================================================================
# F. Per-event shape probe — a TYPE change under a known key now counts as drift.
# ==================================================================================================
@pytest.mark.parametrize(
    ("payload", "expected", "why"),
    [
        ({"delta": {"chunk": "hi"}}, True, "known key, dict without any text carrier"),
        ({"delta": [{"text": "hi"}]}, True, "known key, list — text hidden one level down"),
        ({"delta": 42}, True, "known key, numeric type"),
        ({"delta": {"text": 5}}, True, "wrapper present, its text field is not a string"),
        ({"delta": ""}, False, "present and empty = a legitimately empty delta"),
        ({"delta": " x"}, False, "readable text (whitespace-prefixed is still text)"),
        ({"event": "message.delta", "run_id": "r", "delta": ""}, False, "envelope + empty carrier"),
        (
            {"event": "message.delta", "run_id": "r", "status": "hello"},
            True,
            "prose under `status`",
        ),
        (
            {"event": "message.delta", "run_id": "r", "reason": "hello"},
            True,
            "prose under `reason`",
        ),
        ({"event": "message.delta", "run_id": "r", "message": "hi"}, True, "renamed carrier"),
        ({"event": "message.delta", "run_id": "r", "timestamp": 1.0}, False, "envelope only"),
        ({"event": "message.delta", "run_id": "r", "model": "gpt-5-mini"}, False, "envelope only"),
    ],
)
def test_delta_shape_probe_matrix(payload: dict[str, Any], expected: bool, why: str) -> None:
    assert _delta_shape_looks_unknown(_SseEvent(None, payload)) is expected, why


def test_delta_shape_probe_flags_status_and_reason_as_plausible_carriers() -> None:
    """Regression: ``status``/``reason`` were removed from the envelope allowlist on purpose.

    They never occur on a real ``message.delta`` (they are ``run.paused`` fields) and are among the
    likeliest names a renamed prose carrier would take, so keeping them excluded would have widened
    the blind spot the whole section exists to close.
    """
    for key in ("status", "reason"):
        assert _delta_shape_looks_unknown(_SseEvent(None, {"event": "message.delta", key: "text"}))


# ==================================================================================================
# G. _extract_usage_counts — THE MONEY PATH. A miss here is indistinguishable from a free run.
# ==================================================================================================
@pytest.mark.parametrize(
    ("payload", "expected", "why"),
    [
        # Flat top level — the shape the real usage.delta of the capture uses.
        (
            {"cumulative_input_tokens": 6313, "cumulative_output_tokens": 658},
            (6313, 658, 0, True),
            "flat cumulative (the captured usage.delta)",
        ),
        (
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            (1, 2, 3, True),
            "flat per-step",
        ),
        # Nested carrier — the assumed run.completed shape and our own synthetic run.paused.
        (
            {"usage": {"input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000}},
            (2000, 1000, 3000, True),
            "nested per-step (the ADR-047 assumption)",
        ),
        (
            {"usage": {"cumulative_input_tokens": 10, "cumulative_output_tokens": 20}},
            (10, 20, 0, True),
            "nested cumulative (our synthetic run.paused)",
        ),
        # BOTH sets inside one carrier: cumulative wins. Reading per-step deltas as run totals
        # would under-bill the run — a silent revenue loss, which is why the order is asserted.
        (
            {
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 9,
                    "cumulative_input_tokens": 700,
                    "cumulative_output_tokens": 900,
                    "cumulative_total_tokens": 1600,
                }
            },
            (700, 900, 1600, True),
            "cumulative beats per-step INSIDE a carrier",
        ),
        # Nested carrier beats the flat top level.
        (
            {"usage": {"input_tokens": 5, "output_tokens": 6}, "input_tokens": 999},
            (5, 6, 0, True),
            "nested carrier probed before the top level",
        ),
        # Partial presence still counts as recognised (either key is enough).
        ({"usage": {"output_tokens": 4}}, (0, 4, 0, True), "output-only carrier"),
        # Drift: the counts are there, under names nothing probes.
        (
            {"usage": {"prompt_tokens": 10, "completion_tokens": 20}},
            (0, 0, 0, False),
            "OpenAI-style names — NOT recognised",
        ),
        ({"prompt_tokens": 10, "completion_tokens": 20}, (0, 0, 0, False), "flat drifted names"),
        # Legitimately no usage at all.
        ({"event": "run.completed"}, (0, 0, 0, False), "no usage carrier of any kind"),
        ({"usage": {}}, (0, 0, 0, False), "empty carrier"),
        ({"usage": "n/a"}, (0, 0, 0, False), "carrier is not an object"),
    ],
)
def test_extract_usage_counts_matrix(
    payload: dict[str, Any], expected: tuple[int, int, int, bool], why: str
) -> None:
    counts = _extract_usage_counts(_SseEvent(None, payload))
    actual = (counts.input_tokens, counts.output_tokens, counts.total_tokens, counts.recognised)
    assert actual == expected, why


def test_extract_usage_counts_reads_the_real_captured_usage_delta() -> None:
    """The matrix above is hand-written; this pins the same function to the FIRST SOURCE."""
    (usage,) = (e for e in _dump_events() if _event_name(e) == "usage.delta")
    counts = _extract_usage_counts(usage)
    # cumulative_* win over the per-step input_tokens/output_tokens present in the same block —
    # here they happen to be equal (step 1), so the field ORDER is asserted on the matrix above.
    assert (counts.input_tokens, counts.output_tokens, counts.recognised) == (6313, 658, True)
    assert counts.total_tokens == 6971


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"usage": {"prompt_tokens": 10}}, True),
        ({"prompt_tokens": 10}, True),
        ({"usage": {"total_tokens": 5}}, True),
        ({"usage": {"prompt_tokens": "10"}}, False),  # a string is not a count
        ({"usage": {"prompt_tokens": True}}, False),  # bool is not a count
        ({"event": "run.completed"}, False),
        ({"usage": {"cost": 3}}, False),  # an int, but the name says nothing about tokens
    ],
)
def test_has_token_like_field(payload: dict[str, Any], expected: bool) -> None:
    assert _has_token_like_field(_SseEvent(None, payload)) is expected


# ==================================================================================================
# H. run.completed — warn when usage drifted, stay quiet when there is legitimately none.
# ==================================================================================================
def _completed_block(payload: dict[str, Any]) -> bytes:
    return _block({"event": "run.completed", "run_id": "run_1", **payload})


@respx.mock
async def test_run_completed_with_drifted_usage_names_warns_once_with_keys_only() -> None:
    """C/F. Counts present under unknown names => a warning, not a silent zero-credit run."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_completed_block({"prompt_tokens": 6313, "completion_tokens": 658}), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    warned = [m for m in logs.messages if "run.completed usage shape unknown" in m]
    assert len(warned) == 1, warned
    assert "'prompt_tokens'" in warned[0] and "'completion_tokens'" in warned[0]
    assert "6313" not in warned[0] and "658" not in warned[0], "counts are values, not key names"
    # Nothing was billed (owed=0) — the warning is the ONLY signal that this is not a free run.
    assert w.wallet.calls == []


@respx.mock
async def test_run_completed_without_any_usage_does_not_warn() -> None:
    """C. A legitimately usage-less terminal event must not train operators to ignore the line."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_completed_block({"status": "completed"}), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "usage shape unknown" in m] == []
    assert w.runs.mark_status_calls == [("run_1", "completed")]


@respx.mock
async def test_run_completed_with_a_recognised_carrier_does_not_warn_and_bills() -> None:
    """C. The healthy path stays healthy: recognised counts → a real debit, no warning."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block({"usage": {"input_tokens": 2000, "output_tokens": 1000}, "model": "m"}),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "usage shape unknown" in m] == []
    # 2000/1000 tokens at the default 1.0/5.0 credits per 1k = 2 + 5 = 7 credits, key = bare run_id.
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(7, "run_1")]


@respx.mock
async def test_run_completed_nested_cumulative_names_are_now_billable() -> None:
    """C. New capability of the union carrier: cumulative names inside ``usage`` also bill.

    Before ``_extract_usage_counts`` only ``usage.input_tokens``/``output_tokens`` were read, so a
    terminal payload that spoke the cumulative dialect billed 0 credits in silence.
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block(
            {"usage": {"cumulative_input_tokens": 2000, "cumulative_output_tokens": 1000}}
        ),
        "run_1",
    )
    await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(7, "run_1")]


@respx.mock
async def test_run_completed_nested_drift_names_the_drifted_key_paths() -> None:
    """C/F. The diagnostic must name what did not match — key PATHS, never values.

    This test used to pin the opposite (a line listing only ``['event','run_id','usage']``) as a
    known gap, with an assertion written to fail once the gap was closed. It did, so the requirement
    is now stated directly: with no capture of a completed run available, this line is the only
    route from production to the real field names.
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block({"usage": {"prompt_tokens": 6313, "completion_tokens": 658}}), "run_1"
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    warned = [m for m in logs.messages if "run.completed usage shape unknown" in m]
    assert len(warned) == 1, warned
    assert "usage.prompt_tokens" in warned[0] and "usage.completion_tokens" in warned[0]
    # H. The names arrive; the counts do not.
    assert "6313" not in warned[0] and "658" not in warned[0]
    assert w.wallet.calls == []


# ==================================================================================================
# J. The billing INVARIANT gate — "owed == 0 on a block that is not provably usage-free".
#
# Stated on the OUTCOME rather than on a list of distrusted shapes, so it catches drifts nobody
# taught it. These tests are written the same way: they assert the gate's promise, not its
# implementation, and each case names the real-world payload it stands for.
# ==================================================================================================
@pytest.mark.parametrize(
    ("payload", "provably_free", "why"),
    [
        # --- NOT provably free: something mentions usage/tokens somewhere. --------------------
        ({"result": {"usage": {"input_tokens": 1}}}, False, "nested one level under `result`"),
        (
            {"message": {"usage": {"input_tokens": 1}}},
            False,
            "the literal Anthropic envelope — Hermes proxies Claude",
        ),
        ({"data": {"usage": {"input_tokens": 1}}}, False, "nested under `data`"),
        ({"metrics": {"input_tokens": 1}}, False, "no `usage` key at all, but a *token* name"),
        ({"steps": [{"usage": {"input_tokens": 1}}]}, False, "inside a LIST of dicts"),
        ({"usages": [{"input_tokens": 1}]}, False, "plural key — substring match, not exact"),
        ({"usage_summary": {"input_tokens": 1}}, False, "suffixed key"),
        ({"token_usage": {"in": 1}}, False, "the *token* half of the marker pair"),
        ({"usage": "6313/658"}, False, "carrier present as a STRING — unreadable, not absent"),
        ({"usage": [{"input_tokens": 1}]}, False, "carrier present as a LIST — unreadable"),
        ({"cache_read_input_tokens": 500}, False, "cache counters still mention tokens"),
        # E. camelCase is this project's own API convention — a case-sensitive gate missed it.
        (
            {"totalTokens": 900, "inputTokens": 800, "outputTokens": 100},
            False,
            "camelCase counts must not read as 'no usage here'",
        ),
        ({"Usage": {"InputTokens": 1}}, False, "capitalised carrier"),
        # --- Provably free: nothing here can be usage. ----------------------------------------
        ({"usage": {}}, True, "empty carrier asserts 'nothing', not 'unreadable'"),
        ({"usage": None}, True, "null carrier — a run interrupted before its first step"),
        ({"usage": []}, True, "empty list carrier"),
        ({"event": "run.completed", "run_id": "r", "status": "ok"}, True, "no usage of any kind"),
        (
            {"result": {"cost_usd": 0.12, "in": 6313, "out": 658}},
            True,
            "SEMANTIC rename — no name-based marker can see this (see the text-based signal)",
        ),
    ],
)
def test_is_provably_usage_free_matrix(
    payload: dict[str, Any], provably_free: bool, why: str
) -> None:
    assert _is_provably_usage_free(_SseEvent(None, payload)) is provably_free, why


def test_usage_gate_depth_is_a_deliberate_bound() -> None:
    """A. The scan reaches ``_USAGE_GATE_MAX_DEPTH`` levels and stops — pinned as a known edge.

    Deeper than that, a usage carrier is NOT seen and a zero-credit run passes the name-based gate
    silently. Accepted: no observed layout nests usage that deep, and an unbounded walk on an
    attacker-shaped payload is worse. The text-based signal still covers such a run whenever it
    produced assistant text, which is the case that costs money.
    """
    assert _USAGE_GATE_MAX_DEPTH == 4
    reachable: Any = {"usage": {"input_tokens": 1}}
    for _ in range(_USAGE_GATE_MAX_DEPTH - 1):
        reachable = {"wrap": reachable}
    assert not _is_provably_usage_free(_SseEvent(None, reachable)), "the bound shrank"
    assert _is_provably_usage_free(_SseEvent(None, {"wrap": reachable})), (
        "one level deeper must fall outside the scan — if this now fails the bound GREW, which is "
        "an improvement: raise _USAGE_GATE_MAX_DEPTH here deliberately"
    )


@pytest.mark.parametrize(
    ("payload", "expect_warning", "why"),
    [
        ({"result": {"usage": {"input_tokens": 1}}}, True, "nested carrier, billed zero"),
        ({"steps": [{"usage": {"input_tokens": 1}}]}, True, "carrier inside a list"),
        ({"usages": [{"input_tokens": 1}]}, True, "plural key"),
        ({"token_usage": {"in": 1}}, True, "*token* marker"),
        ({"usage": "6313/658"}, True, "string carrier"),
        ({"totalTokens": 900, "inputTokens": 800}, True, "E. camelCase counts, billed zero"),
        ({"usage": {}}, False, "empty carrier — silence is correct"),
        ({"usage": None}, False, "null carrier — silence is correct"),
        ({"status": "ok"}, False, "no usage at all — silence is correct"),
    ],
)
@respx.mock
async def test_run_completed_zero_credit_gate_end_to_end(
    payload: dict[str, Any], expect_warning: bool, why: str
) -> None:
    """A/E. The same matrix through the REAL relay: a zero-credit block warns exactly once."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_completed_block(payload), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    warned = [m for m in logs.messages if "usage shape unknown" in m]
    assert len(warned) == (1 if expect_warning else 0), f"{why}: {warned}"
    assert w.wallet.calls == [], "nothing is billable in any of these blocks"
    for line in warned:
        assert "6313" not in line and "900" not in line, "payload VALUES must never be logged"


# ==================================================================================================
# K. The union fold — element-wise MAXIMUM, with divergence scoped to ONE field set.
# ==================================================================================================
@pytest.mark.parametrize(
    ("payload", "expected", "why"),
    [
        # THE REGRESSION the fold exists for: a zeroed carrier used to win by rank and bill 0.
        # The anomaly it raises is now NAMED precisely: per-step above cumulative is a broken
        # ORDER (non_monotonic), not two carriers claiming the same quantity (divergent).
        (
            {
                "usage": {"input_tokens": 6313, "output_tokens": 658},
                "cumulative_input_tokens": 0,
                "cumulative_output_tokens": 0,
            },
            (6313, 658, True, False, False, True),
            "zeroed cumulative anchors must NOT shadow a populated per-step carrier",
        ),
        # The ORDINARY shape of any step > 1 (per-step delta next to the running total). Flagging it
        # was the false positive the reviewer caught: it would fire on every multi-step run.
        (
            {
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "cumulative_input_tokens": 6313,
                "cumulative_output_tokens": 658,
            },
            (6313, 658, True, False, False, False),
            "a per-step delta below the cumulative total is the contract, not a conflict",
        ),
        # A. The same regression with the numbers a real step 2 carries.
        (
            {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cumulative_input_tokens": 7513,
                "cumulative_output_tokens": 998,
            },
            (7513, 998, True, False, False, False),
            "step 2 of a healthy run: billed from the cumulative anchors, silently",
        ),
        # B. The order broken: a per-step count ABOVE the cumulative total it belongs to.
        (
            {
                "input_tokens": 9000,
                "output_tokens": 100,
                "cumulative_input_tokens": 1000,
                "cumulative_output_tokens": 50,
            },
            (9000, 100, True, False, False, True),
            "per-step above cumulative contradicts monotonic counters",
        ),
        # Divergence proper: two carriers claiming the SAME quantity with different values.
        (
            {
                "usage": {"cumulative_input_tokens": 2000, "cumulative_output_tokens": 1000},
                "cumulative_input_tokens": 12,
                "cumulative_output_tokens": 3,
            },
            (2000, 1000, True, False, True, False),
            "same field set, two carriers, two answers",
        ),
        # Nested carrier beats a stray top-level half.
        (
            {"usage": {"input_tokens": 5, "output_tokens": 6}, "input_tokens": 999},
            (5, 6, True, False, False, False),
            "a lone top-level input_tokens is not a full match and cannot inflate the fold",
        ),
        # Partial: half a carrier is better than none, but must announce itself.
        ({"usage": {"output_tokens": 4}}, (0, 4, True, True, False, False), "half-read carrier"),
        # Cache counters are NOT counts of billable usage and are not probed.
        (
            {"usage": {"cache_read_input_tokens": 500, "cache_creation_input_tokens": 700}},
            (0, 0, False, False, False, False),
            "cache fields must not enter the fold",
        ),
        # Non-numeric counts do not make a carrier recognised (they would bill as 0).
        (
            {"usage": {"input_tokens": "6313", "output_tokens": "658"}},
            (0, 0, False, False, False, False),
            "string counts are not counts",
        ),
        (
            {"usage": {"input_tokens": True, "output_tokens": False}},
            (0, 0, False, False, False, False),
            "bools are not counts",
        ),
        # Agreement between carriers is not a divergence.
        (
            {
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "input_tokens": 10,
                "output_tokens": 20,
            },
            (10, 20, True, False, False, False),
            "identical full matches agree",
        ),
    ],
)
def test_usage_fold_matrix(
    payload: dict[str, Any], expected: tuple[int, int, bool, bool, bool, bool], why: str
) -> None:
    counts = _extract_usage_counts(_SseEvent(None, payload))
    actual = (
        counts.input_tokens,
        counts.output_tokens,
        counts.recognised,
        counts.partial,
        counts.divergent,
        counts.non_monotonic,
    )
    assert actual == expected, why


def test_usage_fold_labels_every_carrier_it_read() -> None:
    """The audit trail of the fold: which carrier×field-set actually contributed."""
    counts = _extract_usage_counts(
        _SseEvent(
            None,
            {
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "cumulative_input_tokens": 6313,
                "cumulative_output_tokens": 658,
            },
        )
    )
    assert set(counts.sources) == {"top.cumulative", "usage.per_step"}


def test_real_captured_usage_delta_folds_without_any_anomaly() -> None:
    """C. No false positive on the FIRST SOURCE: both carriers agree, so nothing is reported.

    The captured usage.delta carries the per-step and cumulative pairs side by side with identical
    values (step 1) — the shape a naive check would flag on every run.
    """
    (usage_event,) = (e for e in _dump_events() if _event_name(e) == "usage.delta")
    counts = _extract_usage_counts(usage_event)
    assert (counts.input_tokens, counts.output_tokens, counts.total_tokens) == (6313, 658, 6971)
    assert counts.recognised and not counts.partial
    assert not counts.divergent, "the real capture must not trip the divergence alarm"
    assert not counts.non_monotonic, "step 1 has per-step == cumulative, which is monotonic"
    assert set(counts.sources) == {"top.cumulative", "top.per_step"}


@respx.mock
async def test_divergent_carriers_warn_once_with_labels_and_still_bill_the_maximum() -> None:
    """C. Disagreement is surfaced, not absorbed — and the line names carriers, not values.

    Divergence means two carriers answering for the SAME quantity, so the conflict must live INSIDE
    one field set. The earlier version of this test used a per-step pair next to a cumulative one,
    which is the ordinary shape of any multi-step run — it asserted a false positive.
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block(
            {
                "usage": {"cumulative_input_tokens": 2000, "cumulative_output_tokens": 1000},
                "cumulative_input_tokens": 12,
                "cumulative_output_tokens": 3,
            }
        ),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    diverged = [m for m in logs.messages if "carriers disagree" in m]
    assert len(diverged) == 1, diverged
    assert "top.cumulative" in diverged[0] and "usage.cumulative" in diverged[0]
    assert "run.completed" in diverged[0]
    # Billed from the MAXIMUM: 2000 in + 1000 out = 2 + 5 = 7 credits.
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(7, "run_1")]


@respx.mock
async def test_a_multi_step_shaped_terminal_block_does_not_warn() -> None:
    """A/C. The false positive itself, pinned: per-step next to cumulative is silent."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block(
            {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cumulative_input_tokens": 7513,
                "cumulative_output_tokens": 998,
            }
        ),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "disagree" in m or "exceeds" in m] == []
    # Billed from the cumulative anchors: 7.513 + 4.99 → 13 credits.
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(13, "run_1")]


@respx.mock
async def test_non_monotonic_usage_warns_with_its_own_message() -> None:
    """B. A per-step count above the cumulative total gets its OWN line, not the divergence one."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block(
            {
                "input_tokens": 9000,
                "output_tokens": 100,
                "cumulative_input_tokens": 1000,
                "cumulative_output_tokens": 50,
            }
        ),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    broken = [m for m in logs.messages if "per-step usage exceeds the cumulative total" in m]
    assert len(broken) == 1, broken
    assert "run.completed" in broken[0]
    assert [
        m for m in logs.messages if "carriers disagree" in m
    ] == [], "an order breach is not a carrier disagreement — the two signals must stay distinct"


@respx.mock
async def test_agreeing_carriers_do_not_warn() -> None:
    """C (paired negative): without this, an alarm wired 'always' would also pass."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _completed_block(
            {
                "usage": {"input_tokens": 2000, "output_tokens": 1000},
                "cumulative_input_tokens": 2000,
                "cumulative_output_tokens": 1000,
            }
        ),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "disagree" in m or "exceeds" in m] == []


# ==================================================================================================
# L. Per-kind latches — the same anomaly on two paths is two facts, not a repeat.
# ==================================================================================================
@respx.mock
async def test_same_anomaly_on_both_paths_yields_one_line_each() -> None:
    """D. usage.delta and run.completed latch INDEPENDENTLY: a step drift and a terminal drift are
    different findings, and collapsing them would hide whichever arrived second."""
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=True)
    )
    w.wallet.current_balance_value = 10_000
    # Counts of 0 keep `want <= 0`, so _bill_step returns before any debit and the stream is not
    # paused at zero balance — leaving the terminal block reachable. The half-read anomaly is
    # reported before that early return, which is the property under test.
    half_read = _block(
        {
            "event": "usage.delta",
            "run_id": "run_1",
            "step_index": 1,
            "cumulative_input_tokens": 0,
        }
    )
    _events_route(
        half_read + half_read + _completed_block({"usage": {"input_tokens": 2000}}), "run_1"
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    half = [m for m in logs.messages if "usage half-read" in m]
    assert len(half) == 2, f"one line per (kind, anomaly), not one per relay: {half}"
    assert len([m for m in half if "usage.delta" in m]) == 1, "the step path repeated itself"
    assert len([m for m in half if "run.completed" in m]) == 1, "the terminal path repeated itself"


# ==================================================================================================
# M. The text-based signal — the one class of drift NO name can catch.
# ==================================================================================================
@respx.mock
async def test_semantic_rename_with_assistant_text_warns_although_no_name_matches() -> None:
    """E. ``{'result': {'cost_usd': …, 'in': …, 'out': …}}``: provably usage-free BY NAME, yet the
    run demonstrably produced text — so tokens were spent and the zero debit is wrong."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = _block(
        {"event": "message.delta", "run_id": "run_1", "delta": "ответ"}
    ) + _completed_block({"result": {"cost_usd": 0.12, "in": 6313, "out": 658}})
    _events_route(body, "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    text_signal = [m for m in logs.messages if "billed zero for a run that produced" in m]
    assert len(text_signal) == 1, text_signal
    # The name-based gate is correctly silent here — that is exactly why this signal exists.
    assert [m for m in logs.messages if "usage shape unknown" in m] == []
    assert w.wallet.calls == []
    # H. deltas/recognised/key paths only — no payload values, no assistant text.
    assert "ответ" not in text_signal[0]
    assert "6313" not in text_signal[0] and "0.12" not in text_signal[0]
    # ... and the key PATHS are what makes such a line actionable.
    assert "result.cost_usd" in text_signal[0]


@respx.mock
async def test_same_block_without_assistant_text_is_silent() -> None:
    """E (paired negative). No text => no evidence tokens were spent => no claim."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_completed_block({"result": {"cost_usd": 0.12, "in": 6313, "out": 658}}), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "billed zero for a run that produced" in m] == []


@respx.mock
async def test_a_normally_billed_run_with_text_is_silent() -> None:
    """E (paired negative). owed > 0 => nothing to explain, whatever the text."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = _block(
        {"event": "message.delta", "run_id": "run_1", "delta": "ответ"}
    ) + _completed_block({"usage": {"input_tokens": 2000, "output_tokens": 1000}})
    _events_route(body, "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "billed zero" in m] == []
    assert [m for m in logs.messages if "usage shape unknown" in m] == []
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(7, "run_1")]


@respx.mock
async def test_both_signals_fire_independently_when_both_apply() -> None:
    """E. Different claims: 'tokens were certainly spent' vs 'a carrier we cannot read'."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = _block(
        {"event": "message.delta", "run_id": "run_1", "delta": "ответ"}
    ) + _completed_block({"result": {"usage": {"prompt_tokens": 6313}}})
    _events_route(body, "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert len([m for m in logs.messages if "billed zero for a run that produced" in m]) == 1
    assert len([m for m in logs.messages if "usage shape unknown" in m]) == 1


# ==================================================================================================
# N. Key paths in diagnostics — names, bounded, never values.
# ==================================================================================================
def test_shape_summary_reports_dotted_paths_not_top_level_names() -> None:
    """F. The whole point: ``usage.prompt_tokens``, not ``usage``."""
    paths = _shape_summary(
        _SseEvent(None, {"event": "run.completed", "usage": {"prompt_tokens": 1, "cost": 2}})
    )
    assert "usage.prompt_tokens" in paths
    assert "usage.cost" in paths
    assert "usage" not in paths, "a leaf-less parent name adds nothing"


def test_shape_summary_is_bounded_and_deduplicated() -> None:
    """F. A log line must stay a line: 60 distinct nested keys render as exactly 40 paths."""
    payload = {"usage": {f"k{i:02d}_tokens": i for i in range(60)}}
    paths = _shape_summary(_SseEvent(None, payload))
    assert len(paths) == _SHAPE_SUMMARY_MAX_KEYS == 40
    assert paths == sorted(set(paths)), "paths must be sorted and unique"
    assert all(p.startswith("usage.k") for p in paths)


def test_shape_summary_never_collects_values() -> None:
    """H. Values are user content and billing analytics — the summary must not carry them."""
    payload = {
        "usage": {"prompt_tokens": 6313, "note": "PRIVATE-USER-TEXT"},
        "steps": [{"tool": "shell", "arg": "rm -rf /"}],
    }
    rendered = str(_shape_summary(_SseEvent(None, payload)))
    for value in ("6313", "PRIVATE-USER-TEXT", "shell", "rm -rf /"):
        assert value not in rendered, f"{value} leaked into the shape summary"


@respx.mock
async def test_shape_summary_bound_holds_in_a_real_log_line() -> None:
    """F. End to end: the bounded summary is what actually reaches the log."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_completed_block({"usage": {f"k{i:02d}_tokens": i for i in range(60)}}), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    warned = [m for m in logs.messages if "usage shape unknown" in m]
    assert len(warned) == 1
    rendered = ast.literal_eval(warned[0].split("keys=", 1)[1])
    assert len(rendered) == _SHAPE_SUMMARY_MAX_KEYS == 40, "the log line grew past its bound"
    # The envelope names occupy two of the forty slots (they sort before `usage.*`), so the bound
    # applies to the WHOLE summary, not to the drifted keys alone.
    assert rendered[:2] == ["event", "run_id"]
    assert all(p.startswith("usage.k") for p in rendered[2:])


# ==================================================================================================
# O. The terminal half of the ADR-065 aggregate guard.
# ==================================================================================================
@respx.mock
async def test_terminal_event_closes_the_below_threshold_window() -> None:
    """B (revised). A SHORT text-less run now warns once it ends — the window is conclusive there.

    This narrows the limitation pinned by ``test_aggregate_latch_stays_silent_below_the_threshold``:
    below the threshold the counting latch still says nothing MID-run, but any terminal event turns
    "not enough evidence yet" into "the run is over and produced nothing".
    """
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": {"chunk": "x"}})
        for _ in range(3)
    ) + _completed_block({"usage": {"input_tokens": 2000, "output_tokens": 1000}})
    _events_route(body, "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    silent = [m for m in logs.messages if "yielded no text" in m]
    assert len(silent) == 1, silent
    assert "for the whole run" in silent[0] and "deltas=3" in silent[0]


@respx.mock
async def test_terminal_half_shares_the_latch_and_never_doubles_the_line() -> None:
    """B. Counting latch + terminal half must yield ONE line, not two, on a long silent run."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    body = b"".join(
        _block({"event": "message.delta", "run_id": "run_1", "delta": {"chunk": "x"}})
        for _ in range(_bounded(_DELTA_SILENT_WARN_AFTER + 5))
    ) + _completed_block({"usage": {"input_tokens": 2000, "output_tokens": 1000}})
    _events_route(body, "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert len([m for m in logs.messages if "yielded no text" in m]) == 1


@respx.mock
async def test_a_run_that_produced_text_never_reports_a_silent_relay() -> None:
    """B (paired negative), on the real capture: healthy text ⇒ neither half fires."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_dump_bytes() + _completed_block({"usage": {"input_tokens": 1}}), _RUN_ID)
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id=_RUN_ID))
    assert [m for m in logs.messages if "yielded no text" in m] == []


# ==================================================================================================
# P. Usage anomalies are reported INDEPENDENTLY of the billing flag.
#
# Why this section is load-bearing. The reporting call now sits in the usage.delta branch, above
# `if incremental` — and nothing at the call site says so. Move it three lines down, back inside
# _bill_step, and every other test in this file still passes: they all run with the flag ON. The
# regression would be invisible precisely in the DEFAULT configuration, where the anchors still feed
# /state but run.completed is the run's only debit, so a step-level shape problem shows up (if at
# all) as a wrong final charge with no line explaining it. Each case below is therefore stated
# twice, once per flag value, and the OFF half is the one that matters.
# ==================================================================================================
_ANOMALOUS_USAGE_DELTAS: dict[str, dict[str, Any]] = {
    # A. Only one of the two counters is present: the other half is billed as an invented zero.
    "half_read": {"step_index": 1, "cumulative_input_tokens": 6313},
    # B. Two carriers answering for the SAME quantity (both `cumulative`) with different values.
    "divergent": {
        "step_index": 1,
        "usage": {"cumulative_input_tokens": 2000, "cumulative_output_tokens": 1000},
        "cumulative_input_tokens": 12,
        "cumulative_output_tokens": 3,
    },
    # C. A per-step count above the cumulative total it belongs to — monotonicity broken.
    "non_monotonic": {
        "step_index": 1,
        "input_tokens": 9000,
        "output_tokens": 100,
        "cumulative_input_tokens": 1000,
        "cumulative_output_tokens": 50,
    },
}
_ANOMALY_MARKERS = {
    "half_read": "usage half-read",
    "divergent": "carriers disagree",
    "non_monotonic": "per-step usage exceeds the cumulative total",
}


def _usage_delta_block(payload: dict[str, Any]) -> bytes:
    return _block({"event": "usage.delta", "run_id": "run_1", **payload})


@pytest.mark.parametrize("anomaly", sorted(_ANOMALOUS_USAGE_DELTAS))
@pytest.mark.parametrize("incremental", [False, True], ids=["flag_off", "flag_on"])
@respx.mock
async def test_usage_delta_anomaly_is_reported_whatever_the_billing_flag(
    anomaly: str, incremental: bool
) -> None:
    """A/B/C. One line per anomalous stream in BOTH modes — the OFF half is the regression guard."""
    uid = uuid.uuid4()
    w = _wire(
        _settings(
            AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0,
            AGENT_INCREMENTAL_BILLING_ENABLED=incremental,
        )
    )
    w.wallet.current_balance_value = 10_000
    # With the flag ON these anchors are billable, and the wallet double reports no credits actually
    # decremented, so the relay pauses at zero and interrupts the run. Irrelevant to what is under
    # test, but it has to be reachable.
    respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(return_value=httpx.Response(200, json={}))
    _events_route(_usage_delta_block(_ANOMALOUS_USAGE_DELTAS[anomaly]), "run_1")

    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    marker = _ANOMALY_MARKERS[anomaly]
    reported = [m for m in logs.messages if marker in m]
    assert len(reported) == 1, (
        f"{anomaly} went unreported with the billing flag "
        f"{'ON' if incremental else 'OFF'}: {logs.messages}"
    )
    assert "usage.delta" in reported[0], "the line must name the path that read badly"
    # H. Counts of the FOLD and carrier labels are diagnostics; raw payload values are not logged.
    assert "PRIVATE" not in reported[0]
    # The other two anomalies must not be dragged along by this one.
    for other, other_marker in _ANOMALY_MARKERS.items():
        if other != anomaly:
            assert [
                m for m in logs.messages if other_marker in m
            ] == [], f"{anomaly} also raised {other}"


@pytest.mark.parametrize("incremental", [False, True], ids=["flag_off", "flag_on"])
@respx.mock
async def test_repeated_anomalous_usage_deltas_latch_once_in_both_modes(incremental: bool) -> None:
    """D. The (kind, anomaly) latch survives the move out of the billing path.

    A step-level anomaly is systematic by nature — it repeats on every usage event of the run — so
    the latch is what keeps the signal readable. Asserted in both modes because the call now runs
    for every stream, not only for billed ones: an unlatched line would newly spam the DEFAULT
    configuration, which is a fresh regression the flag-ON tests could not see.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(
            AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0,
            AGENT_INCREMENTAL_BILLING_ENABLED=incremental,
        )
    )
    w.wallet.current_balance_value = 10_000
    respx.post(f"{_BASE_URL}/v1/runs/run_1/stop").mock(return_value=httpx.Response(200, json={}))
    step = _ANOMALOUS_USAGE_DELTAS["half_read"]
    _events_route(
        _usage_delta_block({**step, "step_index": 1})
        + _usage_delta_block({**step, "step_index": 2})
        + _usage_delta_block({**step, "step_index": 3}),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert len([m for m in logs.messages if "usage half-read" in m]) == 1


@respx.mock
async def test_step_and_terminal_anomalies_are_both_reported_with_the_flag_off() -> None:
    """D. Per-kind latching with billing OFF: the step path no longer depends on _bill_step running.

    With the flag off the ONLY debit of the run comes from run.completed, so this is the exact
    configuration in which a step-level anomaly used to be invisible — and the terminal line alone
    cannot stand in for it: it describes a different payload.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(
        _usage_delta_block(_ANOMALOUS_USAGE_DELTAS["half_read"])
        + _completed_block({"usage": {"input_tokens": 2000}}),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    half = [m for m in logs.messages if "usage half-read" in m]
    assert len(half) == 2, f"one line per (kind, anomaly): {half}"
    assert len([m for m in half if "usage.delta" in m]) == 1
    assert len([m for m in half if "run.completed" in m]) == 1
    # The run WAS billed from the terminal block despite the flag being off (ADR-047 post-hoc).
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(2, "run_1")]


@respx.mock
async def test_healthy_usage_delta_stays_silent_with_the_flag_off() -> None:
    """A/B/C (paired negative). Moving the call must not make the default configuration noisy.

    The real capture is the payload here: per-step and cumulative side by side, agreeing. If the
    reporting call had been moved without scoping divergence to a single field set, THIS is the
    stream that would now warn on every ordinary run.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(_dump_bytes(), _RUN_ID)
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id=_RUN_ID))
    for marker in _ANOMALY_MARKERS.values():
        assert [m for m in logs.messages if marker in m] == [], f"false positive: {marker}"


# ==================================================================================================
# Q. The two classes a SINGLE well-formed event cannot reveal.
#
# Everything above judges one block at a time. These two judge the SEQUENCE, which is the only place
# they exist: a carrier nothing recognises bills 0 on every step in silence, and a carrier that is
# perfectly readable but FROZEN bills once and then rides free — `want <= 0` forever, so even
# pause-at-zero never fires. Neither shows up in any per-event assertion.
# ==================================================================================================
_UNRECOGNISED_USAGE_DELTAS: dict[str, dict[str, Any]] = {
    # Right names, wrong TYPE — the ADR-065 defect class itself, on the money path.
    "string_counts": {"cumulative_input_tokens": "6313", "cumulative_output_tokens": "658"},
    # Right types, renamed carrier (the OpenAI dialect).
    "renamed": {"prompt_tokens": 6313, "completion_tokens": 658},
    # The carrier itself is not an object.
    "list_carrier": {"usage": [{"input_tokens": 6313}]},
}


@respx.mock
async def test_unrecognised_usage_delta_carrier_warns_once_across_drift_classes() -> None:
    """A. Three ways of being unreadable in one stream produce exactly ONE line, with key paths.

    The step path needs this invariant of its own: ``run.completed`` has the stronger ``owed == 0``
    gate, but under TD-037 the terminal event is often never processed at all, so a run can consist
    entirely of steps nothing could read.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=True)
    )
    w.wallet.current_balance_value = 10_000
    _events_route(
        b"".join(
            _usage_delta_block({"step_index": i, **payload})
            for i, payload in enumerate(_UNRECOGNISED_USAGE_DELTAS.values(), start=1)
        ),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    unrecognised = [m for m in logs.messages if "no carrier recognised" in m]
    assert len(unrecognised) == 1, f"latched per (kind, anomaly): {unrecognised}"
    assert "usage.delta" in unrecognised[0]
    # The key PATHS are the whole value of the line — they are the only route to the real names.
    assert "cumulative_input_tokens" in unrecognised[0]
    # H. Names, never values.
    assert "6313" not in unrecognised[0] and "658" not in unrecognised[0]
    # Nothing was billable and nothing was worth persisting: no debit, no snapshot write.
    assert w.wallet.calls == []
    assert w.snapshots.upserts == [], "an unreadable step must not write empty anchors"


@respx.mock
async def test_the_real_captured_usage_delta_is_recognised_and_silent() -> None:
    """A (paired negative). The FIRST SOURCE must not trip the new step-level invariant."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_dump_bytes(), _RUN_ID)
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id=_RUN_ID))
    assert [m for m in logs.messages if "no carrier recognised" in m] == []


def _frozen_anchor_stream(count: int, *, cum_in: int = 6313, cum_out: int = 658) -> bytes:
    """``count`` usage.delta events whose per-step counts grow while the anchors stand still.

    The per-step values stay BELOW the anchors so the monotonicity check has nothing to say — the
    only thing wrong here is that the run total never moves.
    """
    _bounded(count, derived_from="the anchor-stall threshold")
    return b"".join(
        _usage_delta_block(
            {
                "step_index": step,
                "input_tokens": 10 * step,
                "output_tokens": step,
                "cumulative_input_tokens": cum_in,
                "cumulative_output_tokens": cum_out,
            }
        )
        for step in range(1, count + 1)
    )


@respx.mock
async def test_frozen_cumulative_anchors_warn_once() -> None:
    """B. Readable, self-consistent, and stuck: only the sequence shows it.

    The first event advances the anchors from zero; every one after it leaves them where they are,
    so the stall counter reaches ``_USAGE_ANCHOR_STALL_WARN_AFTER`` and the relay says so once.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(_frozen_anchor_stream(1 + _USAGE_ANCHOR_STALL_WARN_AFTER), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    frozen = [m for m in logs.messages if "cumulative anchors have not advanced" in m]
    assert len(frozen) == 1, f"one line per run, not per stuck event: {frozen}"
    assert f"events={_USAGE_ANCHOR_STALL_WARN_AFTER}" in frozen[0]
    # No other alarm should be dragged along: the payload is well formed and monotonic.
    for marker in ("no carrier recognised", "half-read", "disagree", "exceeds"):
        assert [m for m in logs.messages if marker in m] == [], f"false positive: {marker}"


@respx.mock
async def test_anchors_below_the_stall_threshold_stay_silent() -> None:
    """B. The threshold tolerates a benign reason one step may not advance (a duplicate)."""
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(_frozen_anchor_stream(_USAGE_ANCHOR_STALL_WARN_AFTER - 1), "run_1")
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "cumulative anchors have not advanced" in m] == []


@respx.mock
async def test_advancing_anchors_never_report_a_stall() -> None:
    """B (paired negative). A healthy multi-step run must stay silent however long it runs."""
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(
        b"".join(
            _usage_delta_block(
                {
                    "step_index": step,
                    "cumulative_input_tokens": 1000 * step,
                    "cumulative_output_tokens": 100 * step,
                }
            )
            for step in range(1, _bounded(4 * _USAGE_ANCHOR_STALL_WARN_AFTER))
        ),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "cumulative anchors have not advanced" in m] == []


@respx.mock
async def test_stall_counter_resets_when_the_anchors_move_again() -> None:
    """B. A brief stall that recovers is not reported — the counter resets on progress."""
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    stalled = _USAGE_ANCHOR_STALL_WARN_AFTER - 1
    _events_route(
        _frozen_anchor_stream(1 + stalled)
        + _usage_delta_block(
            {"step_index": 99, "cumulative_input_tokens": 9999, "cumulative_output_tokens": 999}
        )
        + _frozen_anchor_stream(stalled, cum_in=9999, cum_out=999),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert [m for m in logs.messages if "cumulative anchors have not advanced" in m] == []


# ==================================================================================================
# R. Flush cadence — writes follow real progress, not event volume.
# ==================================================================================================
@respx.mock
async def test_repeated_identical_anchors_write_the_snapshot_once() -> None:
    """C. Ten usage events, one advance, ONE write. The gate keeps the unthrottled write affordable.

    The once-per-step cadence that makes an unthrottled snapshot write acceptable rests on the
    capture (one usage.delta per ~15 message.delta). If the image ever emits per chunk, this gate is
    what keeps the write rate tied to progress instead of to event count.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(_frozen_anchor_stream(10), "run_1")
    await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert len(w.snapshots.upserts) == 1, w.snapshots.upserts
    assert (w.snapshots.upserts[0]["input_tokens"], w.snapshots.upserts[0]["output_tokens"]) == (
        6313,
        658,
    )


@respx.mock
async def test_advancing_anchors_write_on_every_step() -> None:
    """C (paired negative). Real progress must still reach the DB immediately, step by step."""
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    steps = 4
    _events_route(
        b"".join(
            _usage_delta_block(
                {
                    "step_index": step,
                    "cumulative_input_tokens": 1000 * step,
                    "cumulative_output_tokens": 100 * step,
                }
            )
            for step in range(1, steps + 1)
        ),
        "run_1",
    )
    await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert len(w.snapshots.upserts) == steps
    assert [u["input_tokens"] for u in w.snapshots.upserts] == [1000, 2000, 3000, 4000]


@respx.mock
async def test_step_level_unrecognised_latch_does_not_silence_the_terminal_gate() -> None:
    """D. Two paths, two facts: the step latch is keyed on its own kind and must not absorb the
    terminal one, which describes a different payload and a different consequence (the run's only
    debit with the billing flag off)."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(
        _usage_delta_block({"step_index": 1, **_UNRECOGNISED_USAGE_DELTAS["renamed"]})
        + _usage_delta_block({"step_index": 2, **_UNRECOGNISED_USAGE_DELTAS["renamed"]})
        + _completed_block({"usage": {"prompt_tokens": 6313}}),
        "run_1",
    )
    with _capture_service_logs() as logs:
        await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    assert len([m for m in logs.messages if "no carrier recognised" in m]) == 1
    assert len([m for m in logs.messages if "billed zero from a non-empty usage block" in m]) == 1


# ==================================================================================================
# S. Mutation-resistant properties of the usage.delta flush (backend-reviewer, MAJOR).
#
# Both properties survived a mutation run against the whole unit suite: removing `assert_approval=
# False` (M1) and downgrading `immediate=True` to `immediate=False` (M2) left every test green. The
# gap was structural, not accidental — no test combined usage.delta with an approval, and almost
# every test runs with the throttle disabled, which makes "immediate" unobservable by construction.
# Each test below is verified to FAIL under its mutation; the proof is in the QA report.
# ==================================================================================================
@respx.mock
async def test_usage_delta_flush_does_not_assert_the_approval_state() -> None:
    """M1. The anchor write must not speak for an approval it knows nothing about.

    Mirrors ``test_throttled_delta_flush_does_not_assert_pending_approval``: a ``usage.delta`` may
    legitimately arrive after the client answered ``POST …/approval``, and the relay still holds the
    stale ``{tool, preview}`` in memory. Asserting it here restores a state the user already
    resolved, so /state falls back to ``waiting_approval`` on a run that is working fine.

    MUTATION: dropping ``assert_approval=False`` (it then defaults to ``immediate``, i.e. True)
    makes this test fail — the whole suite passed that mutation before this test existed.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(
        _block(
            {"event": "approval.request", "run_id": "run_1", "tool": "shell", "preview": "rm -rf"}
        )
        + _usage_delta_block(
            {"step_index": 1, "cumulative_input_tokens": 6313, "cumulative_output_tokens": 658}
        ),
        "run_1",
    )
    await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    approval_flush, anchor_flush = w.snapshots.upserts
    # The approval write DOES assert it — that is the event that knows.
    assert approval_flush["assert_pending_approval"] is True
    assert approval_flush["pending_approval"] == {"tool": "shell", "preview": "rm -rf"}
    # The anchor write carries the anchors and stays out of the approval question entirely.
    assert (
        anchor_flush["assert_pending_approval"] is False
    ), "the usage.delta flush re-asserted a cached approval — an answered request would come back"
    assert (anchor_flush["input_tokens"], anchor_flush["output_tokens"]) == (6313, 658)


@respx.mock
async def test_anchors_reach_the_snapshot_even_under_an_active_throttle() -> None:
    """M2. "Immediate" is only meaningful when the throttle is actually on.

    Almost every test in the suite runs with ``AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0``, where
    throttled and immediate writes are indistinguishable — which is exactly why downgrading this
    flush to ``immediate=False`` passed the whole suite. With the production default (3.0s) the
    difference is decisive: the text delta opens the throttle window, and a throttled anchor write
    would be dropped inside it. The stream then ends WITHOUT a terminal event — the ordinary shape
    of a dropped SSE connection — so nothing would ever flush those anchors again (Q-047-2), and
    /state would report usage {0,0} for a run whose tokens were known.

    MUTATION: ``immediate=False`` on that flush makes this test fail.
    """
    uid = uuid.uuid4()
    w = _wire(
        _settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=3.0, AGENT_INCREMENTAL_BILLING_ENABLED=False)
    )
    _events_route(
        _block({"event": "message.delta", "run_id": "run_1", "delta": "ответ"})
        + _usage_delta_block(
            {"step_index": 1, "cumulative_input_tokens": 6313, "cumulative_output_tokens": 658}
        ),
        "run_1",
    )
    await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    assert len(w.snapshots.upserts) == 2, (
        "the first delta opens the throttle window and the anchor write must still get through: "
        f"{w.snapshots.upserts}"
    )
    last = w.snapshots.upserts[-1]
    assert (last["input_tokens"], last["output_tokens"]) == (
        6313,
        658,
    ), "the anchors were swallowed by the throttle; no terminal event will ever flush them"
    assert last["result_text"] == "ответ", "the text accumulated so far rides along with the write"


# ==================================================================================================
# T. The ADR-067 captures — a run that actually REACHES run.completed.
#
# Provenance (2026-07-30, patched Hermes image of ADR-065, model gpt-5-mini). Every capture below is
# byte-verbatim from a live production run; the ONLY edit is DELETION of whole message.delta blocks
# to shorten the answer text. No value, key, escape or separator was rewritten, and the retained
# blocks keep their original order and trailing bytes (asserted in
# ``test_adr067_fixtures_are_wire_faithful``). Registry: docs/06-testing-strategy.md.
#
#   hermes_prod_completed_run_adr067.sse          run_3b3b1253e0974b24b594b7452bc7095d — normal
#     (+ .state.json)                             completion, 6/6 blocks VERBATIM (nothing to
#                                                 shorten: the whole answer is "DONE.")
#   hermes_prod_tool_run_adr067.sse               2×tool.started/tool.completed (write_file,
#                                                 read_file), 13/27 blocks
#   hermes_prod_no_approval_run_adr067.sse        a deliberate attempt to provoke approval.request:
#                                                 the agent asked for confirmation in PROSE instead,
#                                                 10/144 blocks — evidence of ABSENCE
#   hermes_prod_no_terminal_event_adr067.sse      a jammed instance: the stream stops after
#     (+ _2nd_sample)                             usage.delta with no terminal event, 4/120 blocks
#
# WHAT THIS CLOSES. The shape of ``run.completed`` was the last unverified external assumption of
# this whole effort — nine iterations of detectors were built around a payload nobody had ever seen.
# The capture settles it: usage IS nested, under the per-step names. The tests below pin that to the
# first source rather than to the assumption the code was written from.
# ==================================================================================================
_COMPLETED_FIXTURE = _FIXTURE.parent / "hermes_prod_completed_run_adr067.sse"
_COMPLETED_STATE = _FIXTURE.parent / "hermes_prod_completed_run_adr067.state.json"
_TOOL_FIXTURE = _FIXTURE.parent / "hermes_prod_tool_run_adr067.sse"
_NO_APPROVAL_FIXTURE = _FIXTURE.parent / "hermes_prod_no_approval_run_adr067.sse"
_NO_TERMINAL_FIXTURE = _FIXTURE.parent / "hermes_prod_no_terminal_event_adr067.sse"
_NO_TERMINAL_FIXTURE_2 = _FIXTURE.parent / "hermes_prod_no_terminal_event_2nd_sample_adr067.sse"
_STREAM_CLOSED_MARKER = b": stream closed"


def _events_of(path: Path) -> list[_SseEvent]:
    return [_parse_sse_block(b) for b in path.read_bytes().split(b"\n\n") if b.strip()]


def _only(path: Path, event_name: str) -> _SseEvent:
    (found,) = (e for e in _events_of(path) if _event_name(e) == event_name)
    return found


@pytest.mark.parametrize(
    "path",
    [
        _COMPLETED_FIXTURE,
        _TOOL_FIXTURE,
        _NO_APPROVAL_FIXTURE,
        _NO_TERMINAL_FIXTURE,
        _NO_TERMINAL_FIXTURE_2,
    ],
    ids=lambda p: p.name,
)
def test_adr067_fixtures_are_wire_faithful(path: Path) -> None:
    """The shortening rule, enforced: whole blocks may go, nothing may be rewritten."""
    body = path.read_bytes()
    assert b"\r" not in body, "LF separators only — a CRLF fixture would be a rewrite"
    assert body.endswith(b"\n\n"), "trailing separator must match the capture"
    for block in (b for b in body.split(b"\n\n") if b.strip()):
        # Every block is either a `data:` line or the server's own comment/keepalive line.
        assert block.startswith(b"data: ") or block.startswith(b":"), block
    # Dispatch never depends on an SSE header line: the image does not emit one.
    assert b"\nevent:" not in body and not body.startswith(b"event:")
    for event in _events_of(path):
        assert event.name is None


def test_captured_run_completed_carries_usage_nested_under_per_step_names() -> None:
    """(a) THE assumption, finally measured: ``{"usage": {"input_tokens", "output_tokens", ...}}``.

    Every guard on the money path was designed for a payload that had never been observed. This is
    the observation — and it agrees with the guess, which is worth stating explicitly: the union
    fold reads it from a single carrier, with no divergence and no order breach to report.
    """
    completed = _only(_COMPLETED_FIXTURE, "run.completed")
    assert isinstance(completed.data["usage"], dict), "usage is nested, not flat"
    assert set(completed.data["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}

    counts = _extract_usage_counts(completed)
    assert (counts.input_tokens, counts.output_tokens, counts.total_tokens) == (6302, 586, 6888)
    assert counts.sources == ("usage.per_step",)
    assert counts.recognised and not counts.partial
    assert not counts.divergent and not counts.non_monotonic
    # The gate must therefore stay silent on a real terminal block.
    assert not _is_provably_usage_free(completed)


def test_captured_run_completed_agrees_with_the_state_endpoint() -> None:
    """The capture and the /state body of the SAME run must tell the same story."""
    state = json.loads(_COMPLETED_STATE.read_text())
    completed = _only(_COMPLETED_FIXTURE, "run.completed")
    assert state["runId"] == completed.data["run_id"] == "run_3b3b1253e0974b24b594b7452bc7095d"
    assert state["status"] == "completed"
    assert state["resultText"] == completed.data["output"] == "DONE."
    assert state["usage"] == {"inputTokens": 6302, "outputTokens": 586}


def test_reasoning_available_is_not_a_text_delta() -> None:
    """(d) The duplication trap, pinned before anyone falls into it.

    ``reasoning.available`` carries the finished answer AGAIN under a top-level ``text`` key — the
    very first carrier ``_extract_delta_text`` probes. Nothing routes it there today, because
    dispatch is by event NAME. But a future "any block with a text field is a delta" shortcut would
    look reasonable and would silently double every answer, and /state is where it would show.
    """
    reasoning = _only(_COMPLETED_FIXTURE, "reasoning.available")
    completed = _only(_COMPLETED_FIXTURE, "run.completed")
    # The hazard is real: the field exists, it is a string, and it duplicates the final output.
    assert reasoning.data["text"] == completed.data["output"] == "DONE."
    assert (
        _extract_delta_text(reasoning) == "DONE."
    ), "the trap is live if this block is ever routed"
    # The deltas alone already spell the answer, so appending the reasoning text would double it.
    deltas = [e for e in _events_of(_COMPLETED_FIXTURE) if _event_name(e) == "message.delta"]
    assert "".join(_extract_delta_text(e) for e in deltas) == "DONE."


@respx.mock
async def test_relay_over_the_completed_capture_keeps_the_answer_undoubled() -> None:
    """(d) End to end: the snapshot holds the answer ONCE, and the terminal status is recorded."""
    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_COMPLETED_FIXTURE.read_bytes(), "run_1")
    with _capture_service_logs() as logs:
        out = await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))

    assert out == _COMPLETED_FIXTURE.read_bytes(), "relay mutated the captured bytes"
    assert w.snapshots.upserts[-1]["result_text"] == "DONE."
    assert w.runs.mark_status_calls == [("run_1", "completed")]
    assert (
        w.snapshots.upserts[-1]["input_tokens"],
        w.snapshots.upserts[-1]["output_tokens"],
    ) == (6302, 586)
    # 6302 in + 586 out at the default 1.0 / 5.0 per 1k = 6.302 + 2.93 → 10 credits.
    assert [(c["amount"], c["idempotency_key"]) for c in w.wallet.calls] == [(10, "run_1")]
    # A healthy real run must not trip a single one of the nine iterations of alarms.
    for marker in (
        "usage shape unknown",
        "no carrier recognised",
        "half-read",
        "disagree",
        "exceeds",
        "yielded no text",
        "billed zero",
        "anchors have not advanced",
    ):
        assert [m for m in logs.messages if marker in m] == [], f"false positive: {marker}"


@respx.mock
async def test_relay_over_the_tool_capture_records_the_last_tool() -> None:
    """(c) ``tool.started``/``tool.completed`` — the shape is now measured, not assumed.

    Both carry ``{tool: "<name>"}`` at the top level, which is what ``_extract_tool_name`` reads;
    ``tool.started`` also carries a ``preview`` and ``tool.completed`` a ``duration``/``error``.
    """
    for event_name in ("tool.started", "tool.completed"):
        events = [e for e in _events_of(_TOOL_FIXTURE) if _event_name(e) == event_name]
        assert len(events) == 2
        assert [_extract_tool_name(e) for e in events] == ["write_file", "read_file"]

    uid = uuid.uuid4()
    w = _wire(_settings(AGENT_STATE_FLUSH_INTERVAL_SECONDS=0.0))
    _events_route(_TOOL_FIXTURE.read_bytes(), "run_1")
    await _collect(w.svc.stream_events(user_id=uid, run_id="run_1"))
    assert w.snapshots.upserts[-1]["last_tool"] == "read_file"
    assert w.snapshots.upserts[-1]["pending_approval"] is None


def test_the_current_toolset_never_emitted_an_approval_request() -> None:
    """Evidence of ABSENCE, which is why the capture is worth keeping.

    A deliberate attempt to provoke ``approval.request`` (asking the agent to delete files) produced
    no such event: the agent asked for confirmation in PROSE and completed the run. So the payload
    shape of ``approval.request`` remains unmeasured — the relay's handling of it is still written
    against an assumption, and this fixture is the record of why.
    """
    names = {_event_name(e) for e in _events_of(_NO_APPROVAL_FIXTURE)}
    assert "approval.request" not in names
    assert "run.completed" in names
    completed = _only(_NO_APPROVAL_FIXTURE, "run.completed")
    assert "confirm" in completed.data["output"].lower()


@pytest.mark.parametrize(
    ("path", "closed_normally"),
    [
        (_COMPLETED_FIXTURE, True),
        (_TOOL_FIXTURE, True),
        (_NO_APPROVAL_FIXTURE, True),
        (_NO_TERMINAL_FIXTURE, False),
        (_NO_TERMINAL_FIXTURE_2, False),
    ],
    ids=lambda v: getattr(v, "name", v),
)
def test_stream_closed_marker_discriminates_a_normal_close(
    path: Path, closed_normally: bool
) -> None:
    """(e) ``: stream closed`` is present exactly when a terminal event was delivered.

    NOTE ON THE PAIR USED. The task named ``q6b_1`` vs ``q6b_2`` as the positive/negative pair, but
    on disk those two files are the SAME case — two independent runs of a jammed instance, both
    ending at ``usage.delta``, NEITHER carrying ``run.paused`` nor the marker. Asserting a
    difference between them would have meant inventing one. The real discriminator runs between the
    completion captures and the jammed ones, which is what is asserted here; the second jammed
    sample is kept precisely because it shows the phenomenon reproduced twice.
    """
    body = path.read_bytes()
    assert (_STREAM_CLOSED_MARKER in body) is closed_normally
    names = {_event_name(e) for e in _events_of(path)}
    assert ("run.completed" in names) is closed_normally
    # The marker is an SSE COMMENT line, so it must never parse as an event.
    if closed_normally:
        assert None in {_event_name(e) for e in _events_of(path)}


def test_the_jammed_samples_are_two_runs_of_the_same_phenomenon() -> None:
    """The negative case, reproduced: different runs, identical failure mode."""
    a, b = _events_of(_NO_TERMINAL_FIXTURE), _events_of(_NO_TERMINAL_FIXTURE_2)
    assert a[0].data["run_id"] != b[0].data["run_id"], "two independent runs"
    for events in (a, b):
        assert _event_name(events[-1]) == "usage.delta", "the stream stops on the usage anchor"
        assert {"run.completed", "run.failed", "run.paused"}.isdisjoint(
            {_event_name(e) for e in events}
        )
