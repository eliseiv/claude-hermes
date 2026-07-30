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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.agent_proxy.consumer import sweep_orphan_runs
from app.agent_proxy.service import AgentProxyService
from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository
from app.agent_proxy.transport import AgentRunEventBus, get_event_bus_redis
from app.config import Settings
from app.db import get_sessionmaker, session_scope
from app.deps import get_agent_proxy_service_for, get_hermes_backend
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
    # ADR-067 §5: finalize runs whose background consumer disappeared (restart/deploy, crashed
    # worker, a subscription that never came up). Runs OUTSIDE the session above because it opens
    # one short session per candidate — a long-lived session would hold a connection across the
    # Redis probes between candidates.
    #
    # Deliberately NOT gated on hermes_image like the hibernation reaper: orphaned runs are rows in
    # OUR database, and they must be finalized on any instance that serves the agent contour.
    if settings.agent_run_consumer_enabled:
        finalized = await sweep_orphan_runs(
            services=_background_services,
            bus=AgentRunEventBus(get_event_bus_redis(settings), settings),
            settings=settings,
        )
        if finalized:
            logger.info("agent runs finalized as orphaned count=%d", finalized)


@asynccontextmanager
async def _background_services() -> AsyncIterator[AgentProxyService]:
    """One short session per orphan-sweep operation (ADR-067 §6.1.1 — same rule as the consumer)."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield get_agent_proxy_service_for(session)
        except Exception:
            await session.rollback()
            raise


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
