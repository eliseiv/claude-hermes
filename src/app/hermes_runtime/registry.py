"""Registry repository over the ``hermes_instances`` table (ADR-046 §3, hermes-runtime/04).

CRUD + race-safe upsert for one Hermes instance per user (``user_id`` PK). Stores only metadata
and the envelope-encrypted ``API_SERVER_KEY`` (``api_key_enc``/``encrypted_dek``/``nonce``,
ADR-003); plaintext is never persisted here. No secret is ever logged from this layer.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, text, update
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

    async def get_for_update(
        self, user_id: uuid.UUID, *, lock_timeout_ms: int | None = None
    ) -> HermesInstance | None:
        """Row-locked read (``SELECT ... FOR UPDATE``) for race-safe ensure_running.

        Concurrent ``ensure_running`` calls for the same ``user_id`` serialize on the locked row
        so a second caller observes the first's state instead of double-provisioning.
        ``skip_locked`` is intentionally NOT used — the second caller must wait and re-read.

        ``lock_timeout_ms`` bounds THAT wait. Without it the wait is unbounded (Postgres defaults
        ``lock_timeout`` to 0 = forever, and this project sets no global one), so a caller queued
        behind a slow lock holder hangs no matter what timeout its own HTTP client has — the wait
        happens before a single byte is sent. ``SET LOCAL`` scopes it to the current transaction
        (reverted on commit/rollback, never leaking to the next user of the pooled connection) and
        must be issued on this same session, which is why it lives here next to the ``FOR UPDATE``
        it guards rather than in the caller. Exhaustion surfaces as a ``55P03`` DBAPI error.

        The setting is restored to ``DEFAULT`` immediately after the lock is taken, narrowing it to
        the ONE statement it is meant for. Left in place it would outlive its handler: every later
        statement of the same transaction — notably the ``ON CONFLICT`` insert of
        ``create_provisioning``, which can wait on a concurrent speculative insert — could also
        raise ``55P03``, far from the caller's ``except`` and therefore surface as a 500 instead of
        a 502. Restoring is done only on the success path: if the lock DID time out, the transaction
        is already aborted (no further statement would run) and the caller's rollback clears the
        setting anyway.
        """
        if lock_timeout_ms is not None:
            # Integer-formatted into the statement, NOT a bind parameter: SET LOCAL takes a literal
            # (Postgres rejects a placeholder here). The value is an int by construction — coerced
            # here, never a caller-supplied string — so no injection surface exists.
            await self._session.execute(text(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}"))
        row: HermesInstance | None = await self._session.scalar(
            select(HermesInstance).where(HermesInstance.user_id == user_id).with_for_update()
        )
        if lock_timeout_ms is not None:
            # SET LOCAL … DEFAULT, not RESET: RESET would persist past this transaction's commit.
            await self._session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
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
                # ADR-062 §1a: anchor the stale check on the START of THIS provisioning attempt
                # (not the immutable created_at). Cold-start create → now().
                provisioning_started_at=func.now(),
            )
            .on_conflict_do_nothing(index_elements=[HermesInstance.user_id])
            .returning(HermesInstance)
        )
        result: HermesInstance | None = await self._session.scalar(stmt)
        return result

    async def mark_provisioning(
        self,
        user_id: uuid.UUID,
        *,
        provisioning_started_at: datetime.datetime,
    ) -> None:
        """Move a ``stopped`` row to ``provisioning`` for a wake attempt (ADR-062 §1 step 3).

        Sets ``provisioning_started_at`` to the caller-supplied attempt timestamp ``T`` (the
        stale-anchor for THIS wake attempt, §1a) and re-stamps ``last_active_at``. Preserves
        ``container_id``/``endpoint``/``port`` (the hibernated container is reused, NOT recreated),
        unlike a cold-start insert. The committed ``provisioning`` row is then the race arbiter for
        a concurrent ``ensure_running`` (ADR-062 §1 step 4).
        """
        await self._session.execute(
            update(HermesInstance)
            .where(HermesInstance.user_id == user_id)
            .values(
                status="provisioning",
                provisioning_started_at=provisioning_started_at,
                last_active_at=_utcnow(),
            )
        )

    async def mark_stopped_if_provisioning(
        self,
        user_id: uuid.UUID,
        *,
        provisioning_started_at: datetime.datetime,
    ) -> int:
        """Conditionally re-hibernate a timed-out wake attempt (ADR-062 §1 step 7, §1b).

        Guarded ``UPDATE … SET status='stopped' WHERE user_id AND status='provisioning' AND
        provisioning_started_at=:T``: writes ONLY when the row is still THIS wake attempt (identity
        guard by ``provisioning_started_at=T``). Returns the affected rowcount so the caller can
        decide (1 → we still own the attempt: stop the container; 0 → a concurrent replay/provision
        already took ownership and may have promoted a NEW ``running`` container: do NOT touch it,
        else a healthy instance is clobbered — §1b MAJOR). ``container_id``/``endpoint`` are kept
        (the container is valid; only the status reverts to ``stopped``).
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(HermesInstance)
                .where(HermesInstance.user_id == user_id)
                .where(HermesInstance.status == "provisioning")
                .where(HermesInstance.provisioning_started_at == provisioning_started_at)
                .values(status="stopped")
            ),
        )
        return result.rowcount or 0

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
