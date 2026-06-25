"""Registry repository over the ``hermes_instances`` table (ADR-046 §3, hermes-runtime/04).

CRUD + race-safe upsert for one Hermes instance per user (``user_id`` PK). Stores only metadata
and the envelope-encrypted ``API_SERVER_KEY`` (``api_key_enc``/``encrypted_dek``/``nonce``,
ADR-003); plaintext is never persisted here. No secret is ever logged from this layer.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HermesInstance


class HermesInstanceRegistry:
    """Async repository over ``hermes_instances`` (one row per user, ``user_id`` PK)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> HermesInstance | None:
        """Return the user's instance row, or None if not provisioned."""
        row: HermesInstance | None = await self._session.scalar(
            select(HermesInstance).where(HermesInstance.user_id == user_id)
        )
        return row

    async def get_for_update(self, user_id: uuid.UUID) -> HermesInstance | None:
        """Row-locked read (``SELECT ... FOR UPDATE``) for race-safe ensure_running.

        Concurrent ``ensure_running`` calls for the same ``user_id`` serialize on the locked row
        so a second caller observes the first's state instead of double-provisioning.
        ``skip_locked`` is intentionally NOT used — the second caller must wait and re-read.
        """
        row: HermesInstance | None = await self._session.scalar(
            select(HermesInstance).where(HermesInstance.user_id == user_id).with_for_update()
        )
        return row

    async def create_provisioning(
        self,
        user_id: uuid.UUID,
        *,
        api_key_enc: bytes,
        encrypted_dek: bytes,
        nonce: bytes,
    ) -> HermesInstance | None:
        """Insert a ``provisioning`` row, race-safe via ``ON CONFLICT (user_id) DO NOTHING``.

        Returns the inserted row, or None if a row already existed (a concurrent provisioner won
        the race) — the caller then re-reads with :meth:`get_for_update`. The encrypted key
        material is mandatory (NOT NULL); plaintext is never accepted here.
        """
        stmt = (
            pg_insert(HermesInstance)
            .values(
                user_id=user_id,
                api_key_enc=api_key_enc,
                encrypted_dek=encrypted_dek,
                nonce=nonce,
                status="provisioning",
            )
            .on_conflict_do_nothing(index_elements=[HermesInstance.user_id])
            .returning(HermesInstance)
        )
        result: HermesInstance | None = await self._session.scalar(stmt)
        return result

    async def mark_running(
        self,
        user_id: uuid.UUID,
        *,
        container_id: str,
        endpoint: str,
        port: int | None = None,
    ) -> None:
        """Promote a row to ``running`` after the container is up, recording its address."""
        await self._session.execute(
            update(HermesInstance)
            .where(HermesInstance.user_id == user_id)
            .values(
                container_id=container_id,
                endpoint=endpoint,
                port=port,
                status="running",
                last_active_at=_utcnow(),
            )
        )

    async def mark_stopped(self, user_id: uuid.UUID) -> None:
        """Mark a row ``stopped`` (hibernation). The volume is preserved."""
        await self._session.execute(
            update(HermesInstance).where(HermesInstance.user_id == user_id).values(status="stopped")
        )

    async def touch_active(self, user_id: uuid.UUID) -> None:
        """Bump ``last_active_at`` to now (keeps a hot instance out of the reaper's window)."""
        await self._session.execute(
            update(HermesInstance)
            .where(HermesInstance.user_id == user_id)
            .values(last_active_at=_utcnow())
        )

    async def list_idle_running(self, threshold_seconds: int, limit: int) -> list[HermesInstance]:
        """Return running instances idle longer than the threshold (reaper input).

        Uses ``ix_hermes_instances_status_active`` (status, last_active_at). ``limit`` bounds the
        batch so a single reaper tick cannot stall on a huge backlog.
        """
        cutoff = _utcnow() - datetime.timedelta(seconds=threshold_seconds)
        rows = await self._session.scalars(
            select(HermesInstance)
            .where(HermesInstance.status == "running")
            .where(HermesInstance.last_active_at < cutoff)
            .order_by(HermesInstance.last_active_at)
            .limit(limit)
        )
        return list(rows)

    async def delete(self, user_id: uuid.UUID) -> None:
        """Remove the registry row (deprovision). The host volume is handled by the caller."""
        await self._session.execute(delete(HermesInstance).where(HermesInstance.user_id == user_id))


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)
