"""Unit: the consumer liveness rule and the heartbeat's shape (ADR-067 §6.1/§6.4, stage 2б).

THE ASYMMETRY THAT SHAPES EVERY TEST HERE. A false negative — a wedged consumer we fail to notice —
costs a run that lingers until ``MAX_DURATION`` and is then finalized by the reaper. A false
positive — a WORKING consumer declared stalled — cancels a live run, and the reaper then charges it
from a partial cumulative and records it ``failed`` irreversibly. The two are not comparable, and an
idle timeout that made exactly this mistake was already retracted once (revision 2). So the bulk of
this module asserts that the rule does NOT fire.

``is_stalled`` is a pure function of a beacon, a progress window and settings, which is what makes
the hard cases testable at all: "an agent thinking for twenty minutes inside one tool call" is a
beacon state plus a clock, not a scenario that has to be acted out against a live upstream.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.agent_proxy.consumer import (
    BEACON_AWAITING_UPSTREAM,
    BEACON_CONNECTING,
    BEACON_PROCESSING,
    ConsumerBeacon,
    _ProgressWindow,
    is_stalled,
)
from app.config import Settings

_PROCESSING_STALL = 120
_FIRST_BYTE_STALL = 180


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "AGENT_RUN_PROCESSING_STALL_SECONDS": _PROCESSING_STALL,
        "AGENT_RUN_FIRST_BYTE_STALL_SECONDS": _FIRST_BYTE_STALL,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _beacon(
    *,
    state: str,
    age_seconds: float,
    bytes_read: int = 0,
    published_seq: int = 0,
    transitions: int = 0,
) -> ConsumerBeacon:
    beacon = ConsumerBeacon()
    beacon.state = state
    beacon.since = time.monotonic() - age_seconds
    beacon.bytes_read = bytes_read
    beacon.last_published_seq = published_seq
    beacon.transitions = transitions
    return beacon


def _window(beacon: ConsumerBeacon, *, age_seconds: float = 0.0) -> _ProgressWindow:
    window = _ProgressWindow.of(beacon)
    return _ProgressWindow(
        bytes_read=window.bytes_read,
        published_seq=window.published_seq,
        transitions=window.transitions,
        at=time.monotonic() - age_seconds,
    )


# ==================================================================================================
# THE protection: waiting on the outside world is not a hang, however long it lasts.
# ==================================================================================================
@pytest.mark.parametrize("waited", [60, 600, 3600, 7000])
def test_a_long_tool_call_is_never_a_stall(waited: float) -> None:
    """``awaiting_upstream`` is alive UNCONDITIONALLY — an agent may think for an hour.

    This is the retracted idle timeout, restated as a property. Bounding this state is precisely
    what killed working runs before, and the cost of getting it wrong is asymmetric: the run is
    cancelled, then charged from an incomplete cumulative and marked failed for ever.
    """
    beacon = _beacon(state=BEACON_AWAITING_UPSTREAM, age_seconds=waited, bytes_read=4096)
    window = _window(beacon, age_seconds=waited)
    assert is_stalled(beacon, window, _settings()) is False


def test_a_quiet_stream_that_keeps_reading_bytes_is_alive() -> None:
    """Progress, not the loop: bytes arriving is enough even with no published event."""
    beacon = _beacon(
        state=BEACON_AWAITING_UPSTREAM, age_seconds=3600, bytes_read=10, transitions=99
    )
    window = _window(beacon, age_seconds=3600)
    beacon.bytes_read += 1  # a single byte since the window opened
    assert is_stalled(beacon, window, _settings()) is False


def test_a_publishing_stream_is_alive_even_with_many_transitions() -> None:
    """A busy stream turns the loop constantly; that must never look like spinning."""
    beacon = _beacon(
        state=BEACON_AWAITING_UPSTREAM, age_seconds=3600, published_seq=5, transitions=500
    )
    window = _window(beacon, age_seconds=3600)
    beacon.last_published_seq += 1
    assert is_stalled(beacon, window, _settings()) is False


# ==================================================================================================
# The cases that MUST fire — otherwise the rule is just "always alive".
# ==================================================================================================
def test_a_spinning_loop_with_no_progress_is_stalled() -> None:
    """The one failure a STATE cannot express: the loop keeps turning and nothing is read.

    A fast reconnect loop re-declares ``awaiting_upstream`` on every iteration and would otherwise
    look alive for ever, which is why liveness is tied to observable progress instead.
    """
    beacon = _beacon(state=BEACON_AWAITING_UPSTREAM, age_seconds=1, bytes_read=100, transitions=0)
    window = _window(beacon, age_seconds=_PROCESSING_STALL + 1)
    beacon.transitions = 50  # many turns, no new bytes and no new seq
    assert is_stalled(beacon, window, _settings()) is True


def test_a_wedged_handler_is_stalled() -> None:
    """``processing`` is NOT exempt: our own code does not legitimately take minutes.

    The beacon is set to ``processing`` BEFORE the handler runs, precisely so a handler wedged on a
    DB write is visible here rather than hidden behind ``awaiting_upstream``.
    """
    beacon = _beacon(state=BEACON_PROCESSING, age_seconds=_PROCESSING_STALL + 1, bytes_read=4096)
    assert is_stalled(beacon, _window(beacon), _settings()) is True


def test_a_handler_just_under_the_threshold_is_not_stalled() -> None:
    """Paired boundary — the rule must be a threshold, not "processing is suspicious"."""
    beacon = _beacon(state=BEACON_PROCESSING, age_seconds=_PROCESSING_STALL - 5, bytes_read=4096)
    assert is_stalled(beacon, _window(beacon), _settings()) is False


def test_an_inert_subscription_gets_the_first_byte_threshold() -> None:
    """``connecting`` with no byte ever read is the inert-subscription guard (§6.4.2).

    Its threshold is deliberately the larger one: the worst case "subscription to first content
    event" was never measured (Q-067-14), so the bound errs high — a false positive here cancels a
    working run AND wedges the user's instance.
    """
    settings = _settings()
    just_under = _beacon(state=BEACON_CONNECTING, age_seconds=_FIRST_BYTE_STALL - 5)
    assert is_stalled(just_under, _window(just_under), settings) is False
    over = _beacon(state=BEACON_CONNECTING, age_seconds=_FIRST_BYTE_STALL + 5)
    assert is_stalled(over, _window(over), settings) is True
    # Between the two thresholds a connection that has ALREADY read something is judged by the
    # shorter one — the inert-subscription case no longer applies.
    read_something = _beacon(
        state=BEACON_CONNECTING, age_seconds=_PROCESSING_STALL + 5, bytes_read=1
    )
    assert is_stalled(read_something, _window(read_something), settings) is True


# ==================================================================================================
# The beacon's own contract, on which every rule above rests.
# ==================================================================================================
def test_re_declaring_a_state_does_not_refresh_its_deadline() -> None:
    """Otherwise a handler that keeps re-entering ``processing`` renews its own deadline for ever.

    The rule measures how long we have BEEN in a state, not how recently we said so — which is the
    difference between a liveness signal and a claim.
    """
    beacon = ConsumerBeacon()
    beacon.set_state(BEACON_PROCESSING)
    first_since, first_transitions = beacon.since, beacon.transitions
    beacon.set_state(BEACON_PROCESSING)
    assert beacon.since == first_since, "a repeated state reset its own deadline"
    assert beacon.transitions == first_transitions, "a no-op transition was counted"

    beacon.set_state(BEACON_AWAITING_UPSTREAM)
    assert beacon.transitions == first_transitions + 1
    assert beacon.since > first_since


def test_progress_is_bytes_or_seq_never_a_state_change() -> None:
    """``advanced`` must ignore transitions: "the loop turned" is not "the stream moved"."""
    beacon = _beacon(state=BEACON_AWAITING_UPSTREAM, age_seconds=0, bytes_read=10, published_seq=2)
    window = _window(beacon)

    beacon.transitions += 100
    assert window.advanced(beacon) is False, "a state change counted as progress"

    beacon.bytes_read += 1
    assert window.advanced(beacon) is True

    fresh = _window(beacon)
    beacon.last_published_seq += 1
    assert fresh.advanced(beacon) is True


def test_the_first_byte_ends_the_inert_subscription_guard() -> None:
    beacon = ConsumerBeacon()
    assert beacon.saw_first_byte is False
    beacon.note_bytes(1)
    assert beacon.saw_first_byte is True
    beacon.note_bytes(4095)
    assert beacon.bytes_read == 4096
