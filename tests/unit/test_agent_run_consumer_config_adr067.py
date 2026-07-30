"""Unit: the ADR-067 settings that are only correct in RELATION to one another (stage 1a).

Why a whole module for a validator. Every invariant here has a failure mode that is silent at
runtime: a misconfiguration does not raise, it quietly finalizes live runs, charges them from a
stale cumulative, or removes a guarantee while the config still reads as if the feature were on.
Startup is the only place these knobs are visible together — 07-deployment.md documents them one
row at a time, and each row looks perfectly reasonable alone.

Two of them (heartbeat < orphan_timeout, redis_grace > lease_renew) are NOT stated in the original
ADR text. They were derived from what breaks, and both break money: a heartbeat slower than the
orphan window makes the sweep finalize runs whose consumer is alive — charging them and marking
them failed, after which the real terminal transition is a no-op conditional UPDATE. Nothing logs
an error. Those two get the most explicit tests here.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _rejects(**overrides: Any) -> str:
    with pytest.raises(ValidationError) as excinfo:
        _settings(**overrides)
    return str(excinfo.value)


# ==================================================================================================
# The defaults must themselves satisfy every invariant — otherwise the product ships misconfigured.
# ==================================================================================================
def test_defaults_satisfy_every_invariant() -> None:
    s = _settings()
    assert s.agent_run_max_duration_seconds > 0
    assert s.agent_run_consumer_lease_renew_seconds < s.agent_run_consumer_lease_ttl_seconds
    assert s.agent_run_consumer_heartbeat_seconds < s.agent_run_orphan_timeout_seconds
    assert s.agent_run_orphan_redis_grace_seconds > s.agent_run_consumer_lease_renew_seconds
    assert s.agent_run_handshake_timeout_seconds > 0
    assert s.agent_run_shutdown_drain_seconds > 0
    assert s.agent_run_redis_db >= 0


# ==================================================================================================
# (1) MAX_DURATION — after the idle timeout was retracted it is the ONLY bound on `running`.
# ==================================================================================================
@pytest.mark.parametrize("value", [0, -1])
def test_max_duration_must_be_positive(value: int) -> None:
    """``0`` would silently mean "no limit" — admissible only as a deliberate Q-067-8 decision."""
    assert "AGENT_RUN_MAX_DURATION_SECONDS" in _rejects(AGENT_RUN_MAX_DURATION_SECONDS=value)


# ==================================================================================================
# (2) Every period / size / count: non-positive DISABLES the named mechanism rather than degrading.
# ==================================================================================================
@pytest.mark.parametrize(
    "name",
    [
        "AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS",
        "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS",
        "AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS",
        "AGENT_RUN_UPSTREAM_TCP_KEEPIDLE_SECONDS",
        "AGENT_RUN_UPSTREAM_TCP_KEEPINTVL_SECONDS",
        "AGENT_RUN_UPSTREAM_TCP_KEEPCNT",
        "AGENT_RUN_PROCESSING_STALL_SECONDS",
        "AGENT_RUN_FIRST_BYTE_STALL_SECONDS",
        "AGENT_RUN_EVENT_BUFFER_MAX",
        "AGENT_RUN_EVENT_BUFFER_MAX_BYTES",
        "AGENT_RUN_EVENT_BUFFER_TTL_SECONDS",
        "AGENT_RUN_SUBSCRIBER_QUEUE_MAX",
        "AGENT_RUN_DOWNSTREAM_IDLE_TIMEOUT_SECONDS",
        "AGENT_RUN_ORPHAN_TIMEOUT_SECONDS",
        "AGENT_RUN_ORPHAN_MAX_PER_TICK",
        "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS",
    ],
)
def test_every_period_size_and_count_must_be_positive(name: str) -> None:
    """A 0 ring cap keeps no events, a 0 heartbeat spins, a 0 per-tick cap sweeps nothing — and in
    each case the config still reads as if the feature were configured."""
    message = _rejects(**{name: 0})
    assert name in message, f"{name}=0 was accepted or reported under another name"


@pytest.mark.parametrize(
    "name",
    ["AGENT_RUN_HANDSHAKE_TIMEOUT_SECONDS", "AGENT_RUN_SHUTDOWN_DRAIN_SECONDS"],
)
def test_the_two_float_budgets_must_be_positive(name: str) -> None:
    """A zero drain budget does not "skip waiting" — it closes the DB pool under the §6.4 shutdown
    procedures of live consumers, so no run gets a final flush, a released lease or an audit row on
    an ORDERLY restart. Deploys are routine; this must not be a silent no-op."""
    assert name in _rejects(**{name: 0.0})


# ==================================================================================================
# (3) A lease renewed no sooner than it expires is never held.
# ==================================================================================================
@pytest.mark.parametrize(
    ("renew", "ttl"),
    [(30, 30), (31, 30)],
    ids=["renew_equals_ttl", "renew_above_ttl"],
)
def test_lease_renew_must_be_strictly_below_the_ttl(renew: int, ttl: int) -> None:
    """At or above the TTL the lease lapses between renewals, so the run is perpetually up for
    takeover — and the "single upstream subscriber" property the ONE-SHOT Hermes stream depends on
    is gone. The boundary case (equal) is the one a human would most plausibly configure."""
    message = _rejects(
        AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=renew,
        AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS=ttl,
    )
    assert "AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS" in message


def test_lease_renew_just_below_the_ttl_is_accepted() -> None:
    """The bound is strict-less-than, not a ratio: the validator must not invent a policy."""
    s = _settings(
        AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=29,
        AGENT_RUN_CONSUMER_LEASE_TTL_SECONDS=30,
    )
    assert s.agent_run_consumer_lease_renew_seconds == 29


# ==================================================================================================
# (4) heartbeat < orphan_timeout — NOT in the original ADR text, and it costs money.
# ==================================================================================================
@pytest.mark.parametrize(
    ("heartbeat", "orphan"),
    [(900, 900), (901, 900)],
    ids=["heartbeat_equals_orphan", "heartbeat_above_orphan"],
)
def test_heartbeat_must_fit_inside_the_orphan_window(heartbeat: int, orphan: int) -> None:
    """The silent-money invariant. A live consumer must be able to stamp SEVERAL heartbeats within
    the orphan window; otherwise the sweep finalizes runs that are working — it charges them from
    the snapshot cumulative and marks them failed, and the genuine terminal transition afterwards is
    a no-op (conditional UPDATE). Money and status both wrong, nothing logged as an error.
    """
    message = _rejects(
        AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS=heartbeat,
        AGENT_RUN_ORPHAN_TIMEOUT_SECONDS=orphan,
    )
    assert "AGENT_RUN_CONSUMER_HEARTBEAT_SECONDS" in message
    assert "AGENT_RUN_ORPHAN_TIMEOUT_SECONDS" in message


def test_default_heartbeat_leaves_room_for_several_beats_per_orphan_window() -> None:
    """The invariant is only strict-less-than, but the DEFAULTS should be comfortable — a single
    missed beat must not be enough to be declared orphaned."""
    s = _settings()
    beats = s.agent_run_orphan_timeout_seconds / s.agent_run_consumer_heartbeat_seconds
    assert beats >= 3, f"only {beats:.1f} heartbeats fit in the orphan window"


# ==================================================================================================
# (5) redis_grace > lease_renew — also absent from the ADR, also a mass-finalization guard.
# ==================================================================================================
@pytest.mark.parametrize(
    ("grace", "renew"),
    [(10, 10), (9, 10)],
    ids=["grace_equals_renew", "grace_below_renew"],
)
def test_redis_grace_must_exceed_one_renew_period(grace: int, renew: int) -> None:
    """After a Redis restart EVERY lease is gone at once and live consumers re-take theirs within
    one renew period. A grace no longer than that lets the sweep run in exactly the window where
    nobody holds a lease yet — the mass false finalization the grace exists to prevent."""
    message = _rejects(
        AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS=grace,
        AGENT_RUN_CONSUMER_LEASE_RENEW_SECONDS=renew,
    )
    assert "AGENT_RUN_ORPHAN_REDIS_GRACE_SECONDS" in message


# ==================================================================================================
# (6) The agent-run keys must not share a logical DB with rate limiting / idempotency.
# ==================================================================================================
def test_agent_run_redis_db_must_not_collide_with_the_main_url_db() -> None:
    """Equal values fail NOWHERE at runtime — they just mean a FLUSHDB or SCAN sweep of one contour
    silently takes the other with it."""
    message = _rejects(REDIS_URL="redis://127.0.0.1:6379/3", AGENT_RUN_REDIS_DB=3)
    assert "AGENT_RUN_REDIS_DB" in message


def test_agent_run_redis_db_must_be_non_negative() -> None:
    assert "AGENT_RUN_REDIS_DB" in _rejects(AGENT_RUN_REDIS_DB=-1)


def test_a_distinct_logical_db_is_accepted() -> None:
    s = _settings(REDIS_URL="redis://127.0.0.1:6379/0", AGENT_RUN_REDIS_DB=1)
    assert s.agent_run_redis_db == 1


def test_a_url_without_a_db_path_does_not_block_startup() -> None:
    """``redis://host:6379`` names no logical DB, so there is nothing to collide with. Rejecting it
    would make a perfectly ordinary URL un-deployable."""
    s = _settings(REDIS_URL="redis://127.0.0.1:6379", AGENT_RUN_REDIS_DB=1)
    assert s.agent_run_redis_db == 1
