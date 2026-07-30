"""Repository over ``agent_run_snapshots`` — agent-run state snapshot (ADR-066 §6/§7).

Owns the three statements the snapshot contour needs: the relay upsert (with the per-column
replay-guard), the ``pending_approval`` clear after a client approval answer, and the idempotent
retention sweep run by the reaper. Reads for ``GET /v1/agent/runs/{runId}/state`` go through
:meth:`AgentRunSnapshotsRepository.get`.

This repository is the ONLY writer of ``result_text``/``pending_approval`` — user-facing model
content that must never reach logs or audit (ADR-066 §5, redaction contract ADR-049 unchanged).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRunSnapshot


@dataclass(frozen=True)
class SnapshotUpsertResult:
    """Outcome of one snapshot upsert — both refusal modes are observable by the caller.

    A bare rowcount (the shape of :meth:`AgentRunSnapshotsRepository.clear_pending_approval` and
    ``sweep_expired``) cannot carry this: the write has TWO independent ways of not doing what the
    caller intended, and only one of them shows up as an affected-row count.

    * ``applied=False`` — the ``ON CONFLICT`` branch was rejected by the TENANCY guard (0 rows):
      a ``run_id`` collision across tenants (Q-066-2). Nothing was written at all.
    * ``applied=True`` but ``stored_text_length`` below the length that was submitted — the row was
      written, yet ``result_text`` did NOT advance: the per-column guard kept the stored value
      because the incoming text does not continue it (Q-066-1 — the relay is either replaying from
      the start, or accumulating a diverging text after a reconnect). Tools/approval/tokens/
      ``updated_at`` were still updated.
    """

    applied: bool
    stored_text_length: int


class AgentRunSnapshotsRepository:
    """Async repository over ``agent_run_snapshots`` (one row per run, ``run_id`` TEXT PK)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: str, user_id: uuid.UUID) -> AgentRunSnapshot | None:
        """Return the OWNER's snapshot row, or None when nothing has been flushed yet.

        A missing snapshot is NOT an error: ``/state`` answers 200 with defaults (ADR-066 §5).

        The ``user_id`` predicate is defense-in-depth, not the primary RBAC gate (the caller has
        already matched ``agent_runs.user_id``): Hermes ``run_id`` values are generated per instance
        and their GLOBAL uniqueness across tenants is not guaranteed (Q-064-4), so a collision must
        never let one user's snapshot content surface under another user's run. On a mismatch this
        degrades to the empty-snapshot answer rather than leaking text.
        """
        row: AgentRunSnapshot | None = await self._session.scalar(
            select(AgentRunSnapshot).where(
                AgentRunSnapshot.run_id == run_id,
                AgentRunSnapshot.user_id == user_id,
            )
        )
        return row

    async def upsert(
        self,
        *,
        run_id: str,
        user_id: uuid.UUID,
        result_text: str,
        last_tool: str | None,
        pending_approval: dict[str, Any] | None,
        input_tokens: int,
        output_tokens: int,
        assert_pending_approval: bool = True,
    ) -> SnapshotUpsertResult:
        """Upsert the snapshot with the PER-COLUMN replay-guard (ADR-066 §6.2).

        A re-subscription to ``/events`` restarts the relay's text accumulation, so a second
        consumer may hold a shorter/other text than what is already stored. Monotonicity is applied
        to INDIVIDUAL columns:

        * ``result_text`` — accepted only when the incoming text CONTINUES the stored one: it must
          be at least as long AND have the stored value as its exact prefix;
        * ``input_tokens``/``output_tokens`` — ``GREATEST`` (cumulative counters, monotonic);
        * ``last_tool``/``pending_approval``/``updated_at`` — ``EXCLUDED.*``, NOT guarded by
          monotonicity (the tool changes, an approval is withdrawn — they must reflect the latest
          relay state).

        **Why the prefix check and not length alone.** A length-only guard is safe only under the
        assumption that Hermes replays its buffer FROM THE START on re-subscription — which the live
        image does not confirm. Under "new events only" semantics a reconnecting relay accumulates
        from zero and, once it merely OUTGROWS the stored value, would overwrite a full text with a
        fragment that is missing its beginning. Requiring ``left(new, len(old)) = old`` makes the
        two cases behave differently in the right direction: with replay-from-start it is exactly
        equivalent to the length comparison (the prefix always matches), and without replay the text
        honestly freezes at its fullest known value instead of being silently replaced. The
        head-preserving truncation keeps the prefix stable, so the cap never breaks the check.

        **Staleness gating is per-column.** A row-level ``WHERE`` used for STALENESS is FORBIDDEN
        (ADR-066 §6.2): it gates the whole row, so during a replay window neither
        ``pending_approval`` (the client would never see ``waiting_approval``), nor ``last_tool``,
        nor ``updated_at`` (the staleness detector) would be written. The ``WHERE`` present below is
        a TENANCY guard, not a staleness one — see the next paragraph.

        **Tenancy guard** ``WHERE t.user_id = EXCLUDED.user_id``: defense-in-depth against a Hermes
        ``run_id`` colliding across tenants (per-instance ids, global uniqueness unconfirmed,
        Q-064-4). It can never block a legitimate write — the owner's own upsert always compares
        equal — and it fails closed: a foreign writer updates nothing instead of overwriting another
        user's snapshot.

        ``assert_pending_approval=False`` is the ONE narrow staleness exception, and it is still
        per-column (never row-level): the caller is telling us it has NO information about the
        approval state, so the stored value is preserved instead of being overwritten with a stale
        belief. It is used by the throttled ``message.delta`` flush: a client may have answered
        ``POST …/approval`` out of band (another request, another session), and re-asserting the
        cached ``{tool, preview}`` would resurrect a false ``waiting_approval`` until the next
        ``tool.*`` event. Every flush that IS triggered by an approval-relevant event (the request
        itself, ``tool.*``, terminal events) asserts normally.

        Returns a :class:`SnapshotUpsertResult` so BOTH silent refusals stay observable to the
        caller: a tenancy rejection (no row) and a text that did not advance (the prefix guard kept
        the stored value). ``RETURNING char_length(result_text)`` reports the post-write length
        without ever pulling the text itself into the application — the content stays out of logs
        and metrics by construction.
        """
        result = await self._session.execute(
            text(
                "INSERT INTO agent_run_snapshots AS t "
                "(run_id, user_id, result_text, last_tool, pending_approval, "
                "input_tokens, output_tokens, updated_at) "
                "VALUES (:run_id, :uid, :result_text, :last_tool, "
                "CAST(:pending_approval AS JSONB), :input_tokens, :output_tokens, now()) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "result_text = CASE "
                "WHEN char_length(EXCLUDED.result_text) >= char_length(t.result_text) "
                "AND left(EXCLUDED.result_text, char_length(t.result_text)) = t.result_text "
                "THEN EXCLUDED.result_text ELSE t.result_text END, "
                "input_tokens = GREATEST(t.input_tokens, EXCLUDED.input_tokens), "
                "output_tokens = GREATEST(t.output_tokens, EXCLUDED.output_tokens), "
                "last_tool = EXCLUDED.last_tool, "
                "pending_approval = CASE WHEN :assert_approval "
                "THEN EXCLUDED.pending_approval ELSE t.pending_approval END, "
                "updated_at = EXCLUDED.updated_at "
                # Tenancy guard (NOT staleness): a colliding run_id from another tenant updates 0
                # rows instead of overwriting the owner's snapshot. Never blocks the owner.
                "WHERE t.user_id = EXCLUDED.user_id "
                # Length only — the text itself must never leave the DB for observability purposes.
                "RETURNING char_length(result_text)"
            ),
            {
                "run_id": run_id,
                "uid": str(user_id),
                "result_text": result_text,
                "last_tool": last_tool,
                # NULL stays SQL NULL (not the JSON literal 'null') so `IS NOT NULL` keeps meaning
                # "the run is waiting for an approval answer".
                "pending_approval": (
                    json.dumps(pending_approval) if pending_approval is not None else None
                ),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "assert_approval": assert_pending_approval,
            },
        )
        row = result.first()
        if row is None:
            # No row came back => the DO UPDATE was filtered out by the tenancy guard.
            return SnapshotUpsertResult(applied=False, stored_text_length=0)
        return SnapshotUpsertResult(applied=True, stored_text_length=int(row[0]))

    async def touch_consumer_heartbeat(self, run_id: str) -> int:
        """Stamp ``consumer_heartbeat_at = now()`` — ONE column, nothing else (ADR-067 §4).

        ⚠️ The snapshot upsert MUST NOT be used for this, without exception. That statement writes
        ``updated_at = EXCLUDED.updated_at`` unconditionally, and a heartbeat is written precisely
        when the run state did NOT change (a long tool call, a run with no ``usage.delta``). Using
        it would move the client's staleness detector (``/state.updatedAt``, ADR-066 §5) on a write
        that carries no new state — telling the client the run just progressed when it did not.
        The symmetric invariant of the retention sweep ("the sweep does not touch ``updated_at``")
        exists for the same reason.

        Not owner-scoped: this writes NO user content and reveals none — the caller is the consumer
        that already owns the run's lease, and adding a ``user_id`` predicate would only make a
        heartbeat depend on data the supervisor has no other reason to carry. Never INSERTs; the row
        is created when the consumer starts (see :meth:`ensure_row`), so a 0 rowcount means the run
        has no snapshot row at all and is reported by the caller rather than silently ignored.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_run_snapshots SET consumer_heartbeat_at = now() "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            ),
        )
        return result.rowcount or 0

    async def ensure_row(self, run_id: str, user_id: uuid.UUID) -> None:
        """Create the snapshot row at consumer start if absent. No-op when it already exists.

        Created EAGERLY rather than on the first flush (ADR-067 §6.1): the heartbeat is a bare
        ``UPDATE`` that matches nothing until a row exists, so a run whose first event is minutes
        away — or never arrives — would have no heartbeat to go stale, and the orphan sweep would
        judge it by ``agent_runs.created_at`` instead. That still works, but it means a run cannot
        be distinguished from one whose consumer never started, which is exactly the distinction
        §5 needs. ``ON CONFLICT DO NOTHING`` keeps a takeover by another worker harmless, and every
        content column keeps its schema default (empty text, no approval, zero tokens), so this can
        never overwrite state a previous consumer already wrote.
        """
        await self._session.execute(
            text(
                "INSERT INTO agent_run_snapshots (run_id, user_id) VALUES (:run_id, :uid) "
                "ON CONFLICT (run_id) DO NOTHING"
            ),
            {"run_id": run_id, "uid": str(user_id)},
        )

    async def clear_pending_approval(self, run_id: str, user_id: uuid.UUID) -> int:
        """Drop ``pending_approval`` after a successful ``POST …/approval`` passthrough (§6).

        Third clearing point besides ``tool.*`` and the terminal events: without it the derived
        ``waiting_approval`` would stick after the user has already answered. Owner-scoped
        (``user_id``) and never INSERTs — a run without a snapshot has nothing to clear. Moves
        ``updated_at``: unlike the retention sweep this IS a state change.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_run_snapshots SET pending_approval = NULL, updated_at = now() "
                    "WHERE run_id = :run_id AND user_id = :uid AND pending_approval IS NOT NULL"
                ),
                {"run_id": run_id, "uid": str(user_id)},
            ),
        )
        return result.rowcount or 0

    async def sweep_expired(self, ttl_days: int) -> int:
        """Clear user content of TERMINAL runs older than ``ttl_days`` (ADR-066 §7). Idempotent.

        The row is NOT deleted: ``/state`` keeps answering 200 with status/usage/``updatedAt``
        ("the run happened, here is its outcome") while the model text stops being stored.

        Two mandatory invariants:

        1. **Idempotency** — the guard ``AND (result_text <> '' OR pending_approval IS NOT NULL)``
           skips already-cleared rows. Without it every reaper tick would rewrite ALL old terminal
           snapshots forever (no-op UPDATEs → MVCC bloat, WAL and autovacuum churn growing with
           history). A second pass over a cleared run MUST report ``rowcount = 0``.
        2. **``updated_at`` is NOT touched** — it means "time of the last STATE write" and backs the
           client staleness detector; moving it on a content wipe would tell the client the state
           had just been refreshed, which is a lie. The UPDATE lists ONLY the two content columns.

        Active runs (``running``/``resumed``) are excluded at any age. Returns the rowcount.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    "UPDATE agent_run_snapshots SET result_text = '', pending_approval = NULL "
                    "WHERE updated_at < now() - make_interval(days => :ttl_days) "
                    "AND (result_text <> '' OR pending_approval IS NOT NULL) "
                    # Terminal statuses only — active runs are never swept, at any age. The list is
                    # a fixed enum literal (no user input), inlined exactly as in ADR-066 §7.
                    "AND run_id IN (SELECT run_id FROM agent_runs "
                    "WHERE status IN ('completed', 'failed', 'cancelled', 'paused'))"
                ),
                {"ttl_days": ttl_days},
            ),
        )
        return result.rowcount or 0
