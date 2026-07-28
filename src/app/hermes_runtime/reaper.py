"""Background reaper: idle Hermes hibernation (ADR-046 §5) + snapshot retention (ADR-066 §7).

A periodic ``lifespan`` task that, on every tick, calls ``HermesInstanceManager.stop_idle`` and then
sweeps expired agent-run snapshots. State lives in the DB (``hermes_instances`` /
``agent_run_snapshots``), not process memory, so the reaper resumes cleanly after an ``api``
restart. Each tick uses its own DB session and never raises into the loop — a tick failure is logged
and the next tick proceeds.
"""

from __future__ import annotations

import asyncio
import logging

from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository
from app.config import Settings
from app.db import session_scope
from app.deps import get_hermes_backend
from app.hermes_runtime.manager import HermesInstanceManager
from app.hermes_runtime.registry import HermesInstanceRegistry

logger = logging.getLogger("app.hermes_runtime.reaper")


async def _run_one_tick(settings: Settings) -> None:
    """Run a single stop_idle pass + snapshot retention sweep in one committed transaction."""
    from app.byok.kms import get_kms_client

    async for session in session_scope():
        manager = HermesInstanceManager(
            session=session,
            registry=HermesInstanceRegistry(session),
            backend=get_hermes_backend(),
            kms=get_kms_client(),
            settings=settings,
        )
        await manager.stop_idle(settings.hermes_idle_timeout_seconds)
        # ADR-066 §7: clear user content (result_text/pending_approval) of TERMINAL agent runs
        # older than the TTL. The rows survive — /state keeps returning status/usage/updatedAt —
        # and the statement is idempotent (an already-cleared row matches nothing), so a steady
        # state costs 0 rows per tick instead of rewriting the whole history forever.
        cleared = await AgentRunSnapshotsRepository(session).sweep_expired(
            settings.agent_run_snapshot_ttl_days
        )
        if cleared:
            logger.info("agent run snapshots swept rows=%d", cleared)


async def run_reaper(settings: Settings) -> None:
    """Loop: every ``HERMES_REAPER_INTERVAL_SECONDS`` stop idle instances. Cancellation-safe.

    Swallows per-tick exceptions (logged) so a transient DB/Docker error does not kill the loop.
    Exits cleanly on :class:`asyncio.CancelledError` (lifespan shutdown).
    """
    interval = max(settings.hermes_reaper_interval_seconds, 1)
    logger.info("hermes reaper started interval=%ds", interval)
    try:
        while True:
            try:
                await _run_one_tick(settings)
            except Exception:  # noqa: BLE001 - a tick must never kill the reaper loop
                logger.exception("hermes reaper tick failed")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("hermes reaper stopped")
        raise
