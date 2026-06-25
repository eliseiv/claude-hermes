"""Integration: auth refresh-token cleanup reaper (TD-013). Real PostgreSQL.

cleanup_refresh_tokens deletes stale auth_refresh_tokens:
- expired (expires_at < now()) → deleted REGARDLESS of grace;
- used/revoked → deleted only when COALESCE(used_at, revoked_at) is OLDER than the grace window;
- still-valid (not expired, not used/revoked) → kept;
- idempotent (a second pass deletes nothing new).

The production function runs in its OWN committed session via ``app.db.session_scope`` (global
engine/sessionmaker). We point that global sessionmaker at the test container for the duration of
the test so the real function runs against the same DB the fixtures seed. The lifespan reaper loop
(start/cancel) is asserted separately at the loop level (cancellation-safe).
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.cleanup_reaper import cleanup_refresh_tokens, run_cleanup_reaper
from app.config import Settings
from tests.conftest import seed_user

_GRACE = 604800  # 7 days (default AUTH_REFRESH_CLEANUP_GRACE_SECONDS)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        AUTH_REFRESH_CLEANUP_GRACE_SECONDS=_GRACE,
        AUTH_REFRESH_CLEANUP_INTERVAL_SECONDS=3600,
    )


@pytest.fixture
async def _patch_global_sessionmaker(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Point app.db.session_scope at the test container sessionmaker for this test."""
    import app.db as db_mod

    saved_engine = db_mod._engine
    saved_maker = db_mod._sessionmaker
    db_mod._sessionmaker = db_sessionmaker  # type: ignore[assignment]
    try:
        yield
    finally:
        db_mod._sessionmaker = saved_maker  # type: ignore[assignment]
        db_mod._engine = saved_engine


async def _seed_device(s: AsyncSession, uid: uuid.UUID, device_id: str) -> None:
    await s.execute(
        text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
        {"d": device_id, "u": str(uid)},
    )


async def _insert_token(
    s: AsyncSession,
    uid: uuid.UUID,
    device_id: str,
    *,
    token_hash: str,
    expires_at: datetime.datetime,
    used_at: datetime.datetime | None = None,
    revoked_at: datetime.datetime | None = None,
) -> None:
    await s.execute(
        text(
            "INSERT INTO auth_refresh_tokens "
            "(user_id, device_id, token_hash, expires_at, used_at, revoked_at) "
            "VALUES (:u, :d, :h, :e, :ua, :ra)"
        ),
        {
            "u": str(uid),
            "d": device_id,
            "h": token_hash,
            "e": expires_at,
            "ua": used_at,
            "ra": revoked_at,
        },
    )


async def _token_hashes(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> set[str]:
    async with maker() as s:
        rows = await s.scalars(
            text("SELECT token_hash FROM auth_refresh_tokens WHERE user_id=:u"), {"u": str(uid)}
        )
        return set(rows)


def _ago(seconds: float) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=seconds)


def _ahead(seconds: float) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=seconds)


# ============================================================================
# Deletion predicate: expired (any grace), used/revoked older than grace, valid kept
# ============================================================================
@pytest.mark.asyncio
async def test_cleanup_deletes_expired_keeps_valid_and_recent_used(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    _patch_global_sessionmaker: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_device(s, uid, "dev-1")
        # (a) expired token, NOT used/revoked → deleted regardless of grace.
        await _insert_token(s, uid, "dev-1", token_hash="h-expired", expires_at=_ago(10))
        # (b) used recently (within grace) but NOT expired → KEPT.
        await _insert_token(
            s,
            uid,
            "dev-1",
            token_hash="h-used-recent",
            expires_at=_ahead(3600),
            used_at=_ago(60),
        )
        # (c) used long ago (older than grace) but NOT expired → deleted.
        await _insert_token(
            s,
            uid,
            "dev-1",
            token_hash="h-used-old",
            expires_at=_ahead(3600),
            used_at=_ago(_GRACE + 3600),
        )
        # (d) revoked long ago (older than grace), not expired → deleted.
        await _insert_token(
            s,
            uid,
            "dev-1",
            token_hash="h-revoked-old",
            expires_at=_ahead(3600),
            revoked_at=_ago(_GRACE + 3600),
        )
        # (e) still valid (not expired, not used/revoked) → KEPT.
        await _insert_token(s, uid, "dev-1", token_hash="h-valid", expires_at=_ahead(7200))
        await s.commit()

    deleted = await cleanup_refresh_tokens(_settings())
    assert deleted == 3  # expired + used-old + revoked-old

    remaining = await _token_hashes(db_sessionmaker, uid)
    assert remaining == {"h-used-recent", "h-valid"}


@pytest.mark.asyncio
async def test_cleanup_is_idempotent(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    _patch_global_sessionmaker: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_device(s, uid, "dev-2")
        await _insert_token(s, uid, "dev-2", token_hash="h-exp", expires_at=_ago(5))
        await _insert_token(s, uid, "dev-2", token_hash="h-ok", expires_at=_ahead(3600))
        await s.commit()

    first = await cleanup_refresh_tokens(_settings())
    second = await cleanup_refresh_tokens(_settings())
    assert first == 1
    assert second == 0  # nothing new to delete on the second pass
    assert await _token_hashes(db_sessionmaker, uid) == {"h-ok"}


@pytest.mark.asyncio
async def test_cleanup_used_exactly_at_grace_boundary_kept(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    _patch_global_sessionmaker: None,
) -> None:
    # Used JUST inside the grace window (not yet older than grace) → kept. The predicate is strict
    # "< now() - grace", so a token used grace-minus-epsilon ago is retained for reuse-detection.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_device(s, uid, "dev-3")
        await _insert_token(
            s,
            uid,
            "dev-3",
            token_hash="h-edge",
            expires_at=_ahead(3600),
            used_at=_ago(_GRACE - 120),
        )
        await s.commit()
    deleted = await cleanup_refresh_tokens(_settings())
    assert deleted == 0
    assert await _token_hashes(db_sessionmaker, uid) == {"h-edge"}


# ============================================================================
# Reaper loop: starts, runs at least one tick, cancels cleanly (lifespan shutdown)
# ============================================================================
@pytest.mark.asyncio
async def test_reaper_loop_runs_tick_then_cancels_cleanly(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    _patch_global_sessionmaker: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_device(s, uid, "dev-4")
        await _insert_token(s, uid, "dev-4", token_hash="h-loop-exp", expires_at=_ago(5))
        await _insert_token(s, uid, "dev-4", token_hash="h-loop-ok", expires_at=_ahead(3600))
        await s.commit()

    # Tiny interval so the first tick fires immediately; cancel after it has had a chance to run.
    settings = Settings(  # type: ignore[call-arg]
        AUTH_REFRESH_CLEANUP_GRACE_SECONDS=_GRACE,
        AUTH_REFRESH_CLEANUP_INTERVAL_SECONDS=1,
    )
    task = asyncio.create_task(run_cleanup_reaper(settings))
    # Yield control so the loop body (one cleanup pass) runs before we cancel.
    for _ in range(20):
        await asyncio.sleep(0)
        if "h-loop-exp" not in await _token_hashes(db_sessionmaker, uid):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The expired token was reaped by the loop's first tick; the valid one survived.
    assert await _token_hashes(db_sessionmaker, uid) == {"h-loop-ok"}
