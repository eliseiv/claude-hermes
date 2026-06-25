"""Unit: background hibernation reaper (ADR-046 §5, follow_up #10).

The reaper loop must: call stop_idle periodically (via _run_one_tick), survive a tick exception
(log + continue), and exit cleanly on cancellation (lifespan shutdown). The DB/backend wiring of a
single tick is integration-tested elsewhere; here _run_one_tick is patched to isolate the loop.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.hermes_runtime import reaper as reaper_mod

# Real sleep captured before any monkeypatch so fakes can yield without recursing into themselves.
_REAL_SLEEP = asyncio.sleep


def _settings(interval: int = 1) -> Settings:
    return Settings(
        HERMES_REAPER_INTERVAL_SECONDS=interval,
        HERMES_IDLE_TIMEOUT_SECONDS=1800,
        HERMES_IMAGE="hermes:test",
    )


async def test_reaper_runs_ticks_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _fake_tick(settings: Settings) -> None:
        calls["n"] += 1

    # Zero out the sleep so several ticks run quickly, then cancel.
    async def _fast_sleep(_seconds: float) -> None:
        await _REAL_SLEEP(0)

    monkeypatch.setattr(reaper_mod, "_run_one_tick", _fake_tick)
    monkeypatch.setattr(reaper_mod.asyncio, "sleep", _fast_sleep)

    task = asyncio.create_task(reaper_mod.run_reaper(_settings()))
    # Let the loop spin a few iterations.
    for _ in range(5):
        await _REAL_SLEEP(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["n"] >= 1  # at least one stop_idle pass executed


async def test_reaper_survives_tick_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _flaky_tick(settings: Settings) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")

    async def _fast_sleep(_seconds: float) -> None:
        await _REAL_SLEEP(0)

    monkeypatch.setattr(reaper_mod, "_run_one_tick", _flaky_tick)
    monkeypatch.setattr(reaper_mod.asyncio, "sleep", _fast_sleep)

    task = asyncio.create_task(reaper_mod.run_reaper(_settings()))
    for _ in range(6):
        await _REAL_SLEEP(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The first tick raised; the loop kept going (n advanced past 1).
    assert calls["n"] >= 2


async def test_reaper_cancels_cleanly_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def _slow_tick(settings: Settings) -> None:
        started.set()

    monkeypatch.setattr(reaper_mod, "_run_one_tick", _slow_tick)

    task = asyncio.create_task(reaper_mod.run_reaper(_settings(interval=3600)))
    await asyncio.wait_for(started.wait(), timeout=1.0)  # one tick ran, then it sleeps long
    task.cancel()
    # Cancellation must re-raise CancelledError (clean lifespan shutdown).
    with pytest.raises(asyncio.CancelledError):
        await task


def test_reaper_gated_on_hermes_image_configured() -> None:
    """The lifespan only starts the reaper when HERMES_IMAGE is set (ADR-046 §5, main.py).

    Mirrors the exact predicate ``settings.hermes_image.strip()`` used in app.main.lifespan: empty
    image ⇒ no reaper (no Docker-socket dependency on non-Hermes instances); configured ⇒ start.
    """
    assert not Settings(HERMES_IMAGE="").hermes_image.strip()
    assert not Settings(HERMES_IMAGE="   ").hermes_image.strip()
    assert Settings(HERMES_IMAGE="hermes:1.0").hermes_image.strip()


async def test_reaper_interval_floor_is_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_intervals: list[float] = []

    async def _noop_tick(settings: Settings) -> None:
        return None

    async def _capture_sleep(seconds: float) -> None:
        seen_intervals.append(seconds)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(reaper_mod, "_run_one_tick", _noop_tick)
    monkeypatch.setattr(reaper_mod.asyncio, "sleep", _capture_sleep)

    # Interval 0 must be clamped to >= 1 so the loop cannot busy-spin.
    task = asyncio.create_task(reaper_mod.run_reaper(_settings(interval=0)))
    for _ in range(3):
        await _REAL_SLEEP(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen_intervals and all(s >= 1 for s in seen_intervals)
