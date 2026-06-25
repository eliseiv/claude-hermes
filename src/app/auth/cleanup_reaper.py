"""Background cleanup reaper for stale ``auth_refresh_tokens`` (TD-013, ADR-046 §5 pattern).

A periodic ``lifespan`` task that deletes expired / used / revoked refresh-token rows on the
configured interval. State lives in ``auth_refresh_tokens`` (not process memory), so the reaper
resumes cleanly after an ``api`` restart. Each tick uses its own committed DB session and never
raises into the loop — a tick failure is logged and the next tick proceeds.

Mirrors ``app.hermes_runtime.reaper`` (the established reaper pattern). The auth module persists
refresh tokens via raw SQL (no ORM model), so the deletion uses raw parameterized SQL too.

Deletion predicate (auth/04-data-model.md, TD-013):
- ``expires_at < now()`` — expired tokens are deleted regardless of grace; OR
- ``(used_at IS NOT NULL OR revoked_at IS NOT NULL) AND COALESCE(used_at, revoked_at) < now() -
  grace`` — used/revoked tokens are kept for ``grace`` seconds so recently-rotated tokens remain
  available to reuse-detection before removal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from sqlalchemy import CursorResult, text

from app.config import Settings
from app.db import session_scope

logger = logging.getLogger("app.auth.cleanup_reaper")

# Parameterized DELETE (never f-string SQL). `grace` is an integer second count bound at call time.
_CLEANUP_SQL = text(
    "DELETE FROM auth_refresh_tokens "
    "WHERE expires_at < now() "
    "OR ((used_at IS NOT NULL OR revoked_at IS NOT NULL) "
    "AND COALESCE(used_at, revoked_at) < now() - make_interval(secs => :grace))"
)


async def cleanup_refresh_tokens(settings: Settings) -> int:
    """Run a single cleanup pass in its own committed transaction. Returns the deleted-row count."""
    grace = max(settings.auth_refresh_cleanup_grace_seconds, 0)
    deleted = 0
    async for session in session_scope():
        # session.execute() is typed Result; a DML statement yields a CursorResult with rowcount.
        result = cast("CursorResult[object]", await session.execute(_CLEANUP_SQL, {"grace": grace}))
        deleted = result.rowcount or 0
    if deleted:
        logger.info("auth refresh cleanup deleted %d stale token(s)", deleted)
    return deleted


async def run_cleanup_reaper(settings: Settings) -> None:
    """Loop: every ``AUTH_REFRESH_CLEANUP_INTERVAL_SECONDS`` delete stale refresh tokens.

    Swallows per-tick exceptions (logged) so a transient DB error does not kill the loop. Exits
    cleanly on :class:`asyncio.CancelledError` (lifespan shutdown). Cancellation-safe.
    """
    interval = max(settings.auth_refresh_cleanup_interval_seconds, 1)
    logger.info("auth refresh cleanup reaper started interval=%ds", interval)
    try:
        while True:
            try:
                await cleanup_refresh_tokens(settings)
            except Exception:  # noqa: BLE001 - a tick must never kill the cleanup loop
                logger.exception("auth refresh cleanup tick failed")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("auth refresh cleanup reaper stopped")
        raise
