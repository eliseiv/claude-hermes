"""Repository over ``agent_runs`` — agent-run lifecycle + resume chain (ADR-064 §6).

Owns the small set of statements the incremental-billing / pause-resume contour needs against the
``agent_runs`` table (ADR-064): create the root/child row, record a per-step billing mirror, flip
status, and the two race-arbiter statements ``cas_resume`` / ``revert_cas`` (ADR-064 §5). The table
is a denormalised mirror of the ledger (source of truth is ``ledger_transactions``); on divergence
the ledger wins (``WalletService.charged_for_run``). No secret is ever read/written here.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, Row, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun


class AgentRunsRepository:
    """Async repository over ``agent_runs`` (one row per Hermes run, ``run_id`` TEXT PK)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: str) -> AgentRun | None:
        """Return the run row, or None if unknown (post-hoc runs never write a row)."""
        row: AgentRun | None = await self._session.scalar(
            select(AgentRun).where(AgentRun.run_id == run_id)
        )
        return row

    async def create_running(
        self,
        run_id: str,
        user_id: uuid.UUID,
        session_id: str,
        model: str | None,
        *,
        continued_from_run_id: str | None = None,
        status: str = "running",
    ) -> None:
        """Insert a lifecycle row (root run or resume child), idempotent on the ``run_id`` PK.

        ``ON CONFLICT (run_id) DO NOTHING`` makes a replayed create (reconnect / duplicate launch)
        a no-op rather than a PK violation. ``continued_from_run_id`` links a resume child to its
        paused parent (root run = NULL).
        """
        await self._session.execute(
            text(
                "INSERT INTO agent_runs "
                "(run_id, user_id, session_id, status, model, continued_from_run_id) "
                "VALUES (:run_id, :uid, :session_id, CAST(:status AS agent_run_status), "
                ":model, :parent) "
                "ON CONFLICT (run_id) DO NOTHING"
            ),
            {
                "run_id": run_id,
                "uid": str(user_id),
                "session_id": session_id,
                "status": status,
                "model": model,
                "parent": continued_from_run_id,
            },
        )

    async def record_step(self, run_id: str, step_index: int, credits: int) -> None:
        """Advance the per-step billing mirror (ADR-064 §6): last_billed_step and cumulative spend.

        ``last_billed_step = GREATEST(last_billed_step, :step)`` is monotonic under out-of-order /
        replayed ``usage.delta``; ``cumulative_credits_spent += :credits`` mirrors the ledger debit.
        The ledger stays the source of truth; a missing row (post-hoc) updates 0 rows harmlessly.
        """
        await self._session.execute(
            text(
                "UPDATE agent_runs SET "
                "last_billed_step = GREATEST(last_billed_step, :step), "
                "cumulative_credits_spent = cumulative_credits_spent + :credits, "
                "updated_at = now() "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id, "step": step_index, "credits": credits},
        )

    async def mark_paused(self, run_id: str, reason: str) -> None:
        """Mark a run ``paused`` with a reason (ADR-064 §3, e.g. ``credits_exhausted``)."""
        await self._session.execute(
            text(
                "UPDATE agent_runs SET status = 'paused', paused_reason = :reason, "
                "updated_at = now() WHERE run_id = :run_id"
            ),
            {"run_id": run_id, "reason": reason},
        )

    async def mark_status(self, run_id: str, status: str) -> None:
        """Set the run status (ADR-064 §2/§6), e.g. ``completed`` on finalization."""
        await self._session.execute(
            text(
                "UPDATE agent_runs SET status = CAST(:status AS agent_run_status), "
                "updated_at = now() WHERE run_id = :run_id"
            ),
            {"run_id": run_id, "status": status},
        )

    async def active_child(self, parent_run_id: str) -> AgentRun | None:
        """Return the continuation child of a paused run, or None (ADR-064 §5 idempotent)."""
        row: AgentRun | None = await self._session.scalar(
            select(AgentRun).where(AgentRun.continued_from_run_id == parent_run_id)
        )
        return row

    async def cas_resume(self, run_id: str) -> Row[Any] | None:
        """Atomic ``paused → resumed`` compare-and-set — the single resume race arbiter (§5).

        ``UPDATE ... WHERE run_id AND status='paused' RETURNING session_id, model``. Exactly one
        concurrent caller flips the row (the Postgres row lock serialises them); the winner gets a
        Row (``session_id``/``model``), every loser gets None (WHERE matched 0 rows → already
        resumed). The caller COMMITs immediately after a win to release the row lock (short txn).
        """
        result = await self._session.execute(
            text(
                "UPDATE agent_runs SET status = 'resumed', updated_at = now() "
                "WHERE run_id = :run_id AND status = 'paused' "
                "RETURNING session_id, model"
            ),
            {"run_id": run_id},
        )
        return result.first()

    async def revert_cas(self, run_id: str) -> int:
        """Roll back a won CAS (``resumed → paused``) when launch failed (ADR-064 §5 reconcile).

        Guarded ``... WHERE status='resumed' AND NOT EXISTS(child)``: reverts ONLY when no
        continuation child was created (so a successfully-chained resume is never undone). Returns
        the affected rowcount. Keeps the run resumable after a launch failure (502 to the client).
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_runs SET status = 'paused', updated_at = now() "
                    "WHERE run_id = :run_id AND status = 'resumed' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM agent_runs c WHERE c.continued_from_run_id = :run_id"
                    ")"
                ),
                {"run_id": run_id},
            ),
        )
        return result.rowcount or 0
