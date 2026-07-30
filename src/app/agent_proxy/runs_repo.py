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

    async def mark_paused(self, run_id: str, reason: str) -> int:
        """Mark a run ``paused`` with a reason (ADR-064 §3, e.g. ``credits_exhausted``).

        Conditional like every other status transition (ADR-066 §3): a run that is already terminal
        (e.g. the client sent ``POST …/stop`` a moment earlier) must not be flipped back to
        ``paused``. Returns the affected rowcount.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_runs SET status = 'paused', paused_reason = :reason, "
                    "updated_at = now() "
                    "WHERE run_id = :run_id AND status IN ('running', 'resumed')"
                ),
                {"run_id": run_id, "reason": reason},
            ),
        )
        return result.rowcount or 0

    async def mark_status(self, run_id: str, status: str) -> int:
        """Set a TERMINAL run status — conditional on the run still being active (ADR-066 §3).

        ``WHERE status IN ('running','resumed')`` is MANDATORY for every terminal transition
        (``completed``/``failed``/``cancelled``), not only for ``cancelled``. An unconditional write
        would create a last-writer-wins race: after ``POST …/stop`` Hermes keeps flushing buffered
        events into the still-open relay, and a late ``run.completed``/``run.failed`` would
        overwrite the recorded ``cancelled`` — the client would see ``completed`` for a run it
        stopped itself, and the history would lose it. The condition makes the FIRST terminal
        status the winner and protects ``paused`` from being overwritten as well.

        A missing row (a run started before this row became unconditional) updates 0 rows
        harmlessly. Returns the affected rowcount.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_runs SET status = CAST(:status AS agent_run_status), "
                    "updated_at = now() "
                    "WHERE run_id = :run_id AND status IN ('running', 'resumed')"
                ),
                {"run_id": run_id, "status": status},
            ),
        )
        return result.rowcount or 0

    async def list_orphan_candidates(self, *, timeout_seconds: int, limit: int) -> list[Any]:
        """Active runs whose consumer heartbeat has gone stale (ADR-067 §5, condition 2 of three).

        Starts from ``agent_runs`` under the partial index ``ix_agent_runs_active``
        (``(created_at) WHERE status IN ('running','resumed')``) and joins the snapshot by PK: the
        working set is defined by STATUS, and that predicate self-cleans as runs finish.

        Age is ``COALESCE(snapshot.consumer_heartbeat_at, agent_runs.created_at)``. The fallback to
        ``created_at`` is what covers a run whose consumer never started at all — it has no
        heartbeat to go stale, and without the fallback it would never become a candidate.
        ``updated_at`` deliberately does NOT participate (03-data-model.md §25): it moves on billing
        and status writes, which are not evidence that anyone is still CONSUMING the run.

        ``snapshot_present`` (``s.run_id IS NOT NULL``) is returned ALONGSIDE the coalesced token
        counts and is not redundant with them (ADR-067 §5.2): ``COALESCE(...,0)`` renders "no
        snapshot row at all" and "a row that observed zero usage" as the same zero, and the two are
        entirely different events. The first means the consumer never reached its first DB call, so
        the run was never observed and the zero is the ABSENCE of a measurement — a revenue incident
        to investigate. The second is a measurement: a run that ended before its first
        ``usage.delta``. Only this flag can tell them apart afterwards, and the sweep reports which
        one it acted on (``billingBasis``).

        ``heartbeat_age_seconds`` is the very quantity the ``WHERE`` clause tests, returned so the
        finalization can record it (ADR-067 §5 step 3 requires the age in the audit trail). Computed
        IN SQL from the same ``now()`` as the predicate rather than by handing the caller two
        timestamps to subtract: the age would otherwise be measured against a different clock at a
        later moment, and an audit trail that disagrees with the decision it documents is worse than
        none. It is a float — Postgres ``EXTRACT(EPOCH …)`` is fractional.

        Oldest-first under the same index, capped per tick. Returns candidates only — the caller
        still applies conditions 1 (no live lease) and 3 (Redis uptime).
        """
        result = await self._session.execute(
            text(
                "SELECT r.run_id, r.user_id, "
                "(s.run_id IS NOT NULL) AS snapshot_present, "
                "EXTRACT(EPOCH FROM (now() - COALESCE(s.consumer_heartbeat_at, r.created_at))) "
                "    AS heartbeat_age_seconds, "
                "COALESCE(s.input_tokens, 0) AS input_tokens, "
                "COALESCE(s.output_tokens, 0) AS output_tokens "
                "FROM agent_runs r "
                "LEFT JOIN agent_run_snapshots s ON s.run_id = r.run_id "
                "WHERE r.status IN ('running', 'resumed') "
                "AND COALESCE(s.consumer_heartbeat_at, r.created_at) "
                "    < now() - make_interval(secs => :timeout) "
                "ORDER BY r.created_at "
                "LIMIT :limit"
            ),
            {"timeout": timeout_seconds, "limit": limit},
        )
        return list(result.mappings().all())

    async def mark_stopped(self, run_id: str, user_id: uuid.UUID) -> int:
        """Mark a run ``cancelled`` after a 2xx CLIENT ``POST …/stop`` (ADR-066 §3). Owner-scoped.

        Named separately from :meth:`mark_status` because the CALL SITE is the invariant: this must
        be invoked ONLY on the client stop path, never on the internal Hermes interrupt that
        pause-at-zero performs (ADR-064 §3). Marking the status inside the shared interrupt would
        make a credits-exhausted run transiently ``cancelled`` instead of ``paused`` — ``/state``
        would report ``stopped`` (so the client would not offer a top-up) and ``POST …/resume``
        would answer ``409 run_not_resumable`` inside that window.

        ``AND user_id = :uid`` is MANDATORY here (unlike the relay-driven transitions, whose run id
        comes from the stream the caller is already authorised for): the stop path takes ``run_id``
        straight from the request path and Hermes may answer 2xx for an unknown/foreign run
        (idempotent-stop semantics), so an unscoped UPDATE would let user A cancel user B's run.
        A foreign id simply updates 0 rows — no 403 is surfaced (RBAC-404 contract).
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_runs SET status = 'cancelled', updated_at = now() "
                    "WHERE run_id = :run_id AND user_id = :uid "
                    "AND status IN ('running', 'resumed')"
                ),
                {"run_id": run_id, "uid": str(user_id)},
            ),
        )
        return result.rowcount or 0

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
