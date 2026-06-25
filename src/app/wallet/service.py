"""Wallet service: atomic, idempotent consume/grant (ADR-005, ADR-006, ADR-047 §6; AC-3).

consume: INSERT ledger ON CONFLICT DO NOTHING + conditional UPDATE balance >= amount, wrapped in a
SAVEPOINT (session.begin_nested()) + DB CHECK (balance >= 0). Self-contained atomicity (ADR-047 §6):
on insufficient balance the savepoint is rolled back inside consume, undoing the just-inserted debit
row WITHOUT relying on the caller's outer ROLLBACK — so even if the caller swallows
InsufficientCreditsError (agent SSE path, which must not break the stream) no orphan debit row is
committed and balance == Σ(credit) − Σ(debit) holds. Idempotency by (user_id, idempotency_key); for
chat-debit the idempotency_key is messageStepId (NOT gateway requestId), for agent-debit it is runId
(ADR-047 §4). grant: same shape, type=credit, idempotency by transactionId of the subscription
period.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_BILLING_CREDIT,
    EVENT_BILLING_DEBIT,
    EVENT_BILLING_DEBIT_INSUFFICIENT,
    EVENT_BILLING_DEBT_REPAID,
    AuditEvent,
    AuditService,
)
from app.config import get_settings
from app.errors import (
    ConflictError,
    ForbiddenError,
    InsufficientCreditsError,
    SessionNotFoundError,
)
from app.models import LedgerTransaction, Wallet
from app.observability.metrics import wallet_debit_total

# meta.source marker of an agent-run debit (ADR-047 §4). The ADR-051 partial-debit + debt
# reconciliation path applies ONLY to this source (chat-debit keeps the full savepoint rollback →
# InsufficientCreditsError → 409 semantics).
_AGENT_RUN_SOURCE = "agent_run"


class _RetryNormalPath(Exception):
    """Internal signal: the agent-reconcile savepoint hit a concurrent-debit race; roll it back and
    fall through to the normal full-debit path. Never escapes WalletService."""


@dataclass(frozen=True)
class ConsumeResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


@dataclass(frozen=True)
class GrantResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


class WalletService:
    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def _ensure_wallet(self, user_id: uuid.UUID) -> None:
        """Idempotent auto-provisioning of the wallet row (wallet-ledger/03)."""
        await self._session.execute(
            text(
                "INSERT INTO wallets (user_id, balance) VALUES (:uid, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"uid": str(user_id)},
        )

    async def _existing_tx(
        self, user_id: uuid.UUID, idempotency_key: str
    ) -> LedgerTransaction | None:
        row: LedgerTransaction | None = await self._session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.user_id == user_id,
                LedgerTransaction.idempotency_key == idempotency_key,
            )
        )
        return row

    async def _current_balance(self, user_id: uuid.UUID) -> int:
        wallet = await self._session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        return int(wallet.balance) if wallet is not None else 0

    async def current_balance(self, user_id: uuid.UUID) -> int:
        """Read the current wallet balance (0 if no row). Public accessor over the existing query;
        used e.g. by the agent path to record the balance in the billing_debit_insufficient audit
        event without pulling the full wallet view (ADR-047 §6)."""
        return await self._current_balance(user_id)

    async def _current_debt(self, user_id: uuid.UUID) -> int:
        wallet = await self._session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        return int(wallet.debt) if wallet is not None else 0

    async def current_debt(self, user_id: uuid.UUID) -> int:
        """Read the current wallet debt (0 if no row). ADR-051: accumulated uncharged agent-run
        delta in credits. Used by the policy-gate (debt_outstanding) and the admin wallet view."""
        return await self._current_debt(user_id)

    async def _validate_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """Validate sessionId before any FK-dependent op (wallet-ledger/02; robustness vs 500).

        A bogus sessionId would otherwise hit a FK violation on audit_logs.session_id and surface
        as a 500. We resolve the owning user_id up front: missing → 404 session_not_found; owned by
        another user → 403. Parameterized query; runs before idempotency/balance checks.
        """
        owner = await self._session.scalar(
            text("SELECT user_id FROM chat_sessions WHERE id = :sid"),
            {"sid": str(session_id)},
        )
        if owner is None:
            raise SessionNotFoundError("session not found")
        if uuid.UUID(str(owner)) != user_id:
            raise ForbiddenError("session does not belong to user")

    async def _debit_in_savepoint(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
    ) -> tuple[uuid.UUID | None, int | None]:
        """INSERT debit + conditional UPDATE inside a SAVEPOINT (ADR-047 §6, wallet-ledger/03).

        Returns ``(inserted_id, new_balance)``:
        - ``(None, None)`` — ON CONFLICT (idempotent replay): savepoint released, no balance change.
        - ``(id, balance)`` — new debit applied: savepoint released, balance decremented.

        Raises ``InsufficientCreditsError`` when the conditional UPDATE matches 0 rows (balance <
        amount): raising inside ``begin_nested()`` rolls the savepoint back, so the just-inserted
        debit row is undone and the balance is untouched — no orphan row, independent of the
        caller's outer transaction outcome.
        """
        async with self._session.begin_nested():
            inserted_id = await self._session.scalar(
                text(
                    "INSERT INTO ledger_transactions "
                    "(user_id, type, amount, meta, idempotency_key) "
                    "VALUES (:uid, 'debit', :amount, CAST(:meta AS JSONB), :key) "
                    "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "uid": str(user_id),
                    "amount": amount,
                    "meta": _json(meta),
                    "key": idempotency_key,
                },
            )
            if inserted_id is None:
                # Idempotent replay: nothing inserted. Release the savepoint (no-op) and let the
                # caller resolve the existing tx / current balance outside the savepoint.
                return None, None

            # New debit: conditional balance update (double guard against negative balance).
            updated = await self._session.scalar(
                text(
                    "UPDATE wallets SET balance = balance - :amount, updated_at = now() "
                    "WHERE user_id = :uid AND balance >= :amount "
                    "RETURNING balance"
                ),
                {"uid": str(user_id), "amount": amount},
            )
            if updated is None:
                # Insufficient credits: raise to roll back to the savepoint — the just-inserted
                # debit row is undone and the balance is untouched (no orphan row), regardless of
                # whether the caller swallows this error and the outer tx commits (ADR-047 §6).
                wallet_debit_total.labels(result="fail").inc()
                raise InsufficientCreditsError("insufficient_credits")

            return inserted_id, int(updated)

    async def consume(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
        session_id: uuid.UUID | None = None,
    ) -> ConsumeResult:
        """Atomic, idempotent debit. amount > 0. See ADR-005."""
        if amount <= 0:
            raise ConflictError("amount must be positive")
        # wallet-ledger/02: validate sessionId BEFORE idempotency/balance and any FK-dependent
        # write (debit + audit billing_debit). Prevents a 500 from a FK violation on a bogus id.
        if session_id is not None:
            await self._validate_session(user_id, session_id)
        await self._ensure_wallet(user_id)

        # ADR-051 §2.1: on the AGENT path with reconciliation enabled, a shortfall (amount >
        # balance) does NOT roll back fully — consume debits the available balance (partial ledger
        # debit) and accrues the remainder into wallets.debt (NOT a ledger row). This keeps the SSE
        # relay alive and the user pays for what could be charged; the rest is reconciled by
        # clawback on the next grant. chat-debit and the flag-off case keep full-rollback → 409.
        if self._agent_reconcile_applies(meta):
            reconciled = await self._consume_agent_with_debt(
                user_id=user_id,
                amount=amount,
                idempotency_key=idempotency_key,
                meta=meta,
            )
            if reconciled is not None:
                return reconciled

        # Self-contained atomicity (ADR-047 §6, wallet-ledger/03 §Самодостаточная атомарность):
        # the INSERT debit + conditional UPDATE for a NEW row run inside a SAVEPOINT
        # (session.begin_nested()). On insufficient balance the savepoint is rolled back, so the
        # just-inserted debit row is undone WITHOUT relying on the caller's outer ROLLBACK. This
        # keeps the ledger free of orphan debit rows even when the caller swallows
        # InsufficientCreditsError (agent SSE path, which must not break the stream) and the outer
        # session_scope commits — the invariant balance == Σ(credit) − Σ(debit) is preserved.
        #
        # The idempotent-replay branch (ON CONFLICT DO NOTHING returns no id) and the amount/meta
        # equality check live OUTSIDE the savepoint's failure path: a replay releases the savepoint
        # (no balance change) and returns the existing tx; only a NEW row's insufficient-balance
        # case rolls back to the savepoint.
        inserted_id, new_balance = await self._debit_in_savepoint(
            user_id=user_id, amount=amount, idempotency_key=idempotency_key, meta=meta
        )

        if inserted_id is None:
            # Idempotent replay: same key already exists. Verify payload matches.
            existing = await self._existing_tx(user_id, idempotency_key)
            if existing is None:  # pragma: no cover - defensive
                raise ConflictError("idempotency conflict")
            if existing.type != "debit" or int(existing.amount) != amount:
                raise ConflictError("idempotency key reused with different payload")
            balance = await self._current_balance(user_id)
            return ConsumeResult(
                new_balance=balance, ledger_tx_id=existing.id, idempotent_replay=True
            )

        assert (
            new_balance is not None
        )  # invariant: a new debit always yields the post-debit balance
        wallet_debit_total.labels(result="success").inc()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_BILLING_DEBIT,
                payload={
                    "ledgerTxId": str(inserted_id),
                    "amount": amount,
                    "newBalance": new_balance,
                    "sessionId": str(session_id) if session_id else None,
                    "model": meta.get("model"),
                },
            )
        )
        return ConsumeResult(
            new_balance=new_balance, ledger_tx_id=inserted_id, idempotent_replay=False
        )

    @staticmethod
    def _agent_reconcile_applies(meta: dict[str, Any]) -> bool:
        """True when the ADR-051 partial-debit + debt path applies to this debit.

        Only the agent-run source (``meta.source == "agent_run"``) with the
        ``AGENT_DEBT_RECONCILE_ENABLED`` flag on. chat-debit and the flag-off case keep the legacy
        full-savepoint-rollback → InsufficientCreditsError → 409 behaviour (ADR-047 §6).
        """
        if meta.get("source") != _AGENT_RUN_SOURCE:
            return False
        return get_settings().agent_debt_reconcile_enabled

    async def _consume_agent_with_debt(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
    ) -> ConsumeResult | None:
        """Agent-path shortfall reconciliation (ADR-051 §2.1, wallet-ledger/03).

        Returns ``None`` when the normal full-debit path should handle the debit (balance is
        sufficient, or a defensive balance==0 edge that the normal path raises on). On a shortfall
        with ``balance > 0`` it debits the available balance (partial ledger debit, idempotent by
        runId) and accrues the remainder ``delta = amount - balance`` into ``wallets.debt`` (NOT a
        ledger row), records ``billing_debit_insufficient`` (+ partialDebited/debtAdded), and
        returns the ConsumeResult — never raising, so the SSE relay is not broken. A replayed run
        hits ON CONFLICT and increments neither the ledger nor the debt.
        """
        # Idempotent replay FIRST (ADR-051 §2.1, idempotency by runId): a prior shortfall already
        # wrote a PARTIAL debit row (ledger.amount = the then-available balance, which is LESS than
        # `amount`) AND accrued the remainder into debt. On replay the balance is 0, so the
        # balance-branches below would fall through to the normal full-debit path and its replay
        # check would compare the FULL `amount` against the partial ledger amount → a false
        # ConflictError. Detect the existing row here and return it verbatim — no debt growth, no
        # amount comparison (the partial-debit amount intentionally differs from the run `amount`).
        existing = await self._existing_tx(user_id, idempotency_key)
        if existing is not None:
            current = await self._current_balance(user_id)
            return ConsumeResult(
                new_balance=current, ledger_tx_id=existing.id, idempotent_replay=True
            )

        balance = await self._current_balance(user_id)
        if amount <= balance:
            # Sufficient balance: defer to the normal full-debit path (idempotent, billing_debit).
            return None
        if balance <= 0:
            # Defensive: a zero-balance agent run should have been blocked pre-run by the
            # policy-gate (credits_empty / debt_outstanding). With no partial amount to debit
            # (amount > 0 CHECK), defer to the normal path which raises InsufficientCreditsError.
            return None

        partial = balance
        delta = amount - balance
        try:
            async with self._session.begin_nested():
                inserted_id = await self._session.scalar(
                    text(
                        "INSERT INTO ledger_transactions "
                        "(user_id, type, amount, meta, idempotency_key) "
                        "VALUES (:uid, 'debit', :amount, CAST(:meta AS JSONB), :key) "
                        "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                        "RETURNING id"
                    ),
                    {
                        "uid": str(user_id),
                        "amount": partial,
                        "meta": _json(meta),
                        "key": idempotency_key,
                    },
                )
                if inserted_id is None:
                    # Idempotent replay of run.completed: the partial debit already exists. Release
                    # the savepoint (no balance/debt change) and resolve the existing tx + balance.
                    existing = await self._existing_tx(user_id, idempotency_key)
                    if existing is None:  # pragma: no cover - defensive
                        raise ConflictError("idempotency conflict")
                    current = await self._current_balance(user_id)
                    return ConsumeResult(
                        new_balance=current, ledger_tx_id=existing.id, idempotent_replay=True
                    )

                # New partial debit: drain the available balance to 0 and accrue the shortfall into
                # debt, atomically. The conditional `balance >= :partial` guards a concurrent debit
                # that reduced the balance after our read; 0 rows → raise to roll back the savepoint
                # (and the just-inserted partial row), then fall back to the normal path.
                updated = await self._session.scalar(
                    text(
                        "UPDATE wallets "
                        "SET balance = balance - :partial, debt = debt + :delta, "
                        "    updated_at = now() "
                        "WHERE user_id = :uid AND balance >= :partial "
                        "RETURNING debt"
                    ),
                    {"uid": str(user_id), "partial": partial, "delta": delta},
                )
                if updated is None:  # pragma: no cover - concurrent-debit race
                    raise _RetryNormalPath
                new_debt = int(updated)
        except _RetryNormalPath:  # pragma: no cover - concurrent-debit race
            # The savepoint rolled back (partial row undone, balance untouched): defer to the normal
            # path, which re-reads the balance under the conditional UPDATE.
            return None

        wallet_debit_total.labels(result="partial").inc()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BILLING_DEBIT_INSUFFICIENT,
                payload={
                    "runId": meta.get("runId"),
                    "usage": meta.get("usage"),
                    "model": meta.get("model"),
                    "requiredAmount": amount,
                    "partialDebited": partial,
                    "debtAdded": delta,
                    "debt": new_debt,
                },
            )
        )
        return ConsumeResult(new_balance=0, ledger_tx_id=inserted_id, idempotent_replay=False)

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        meta: dict[str, Any],
        reason: str,
    ) -> GrantResult:
        """Atomic, idempotent credit grant (ADR-006). amount > 0.

        ADR-051 §3 clawback: on a real (non-replay) grant, the debt is repaid out of the granted
        amount BEFORE the balance is increased — ``repaid = min(amount, debt)``, ``debt -= repaid``,
        ``balance += amount - repaid`` — atomically with the credit ledger INSERT. The ledger keeps
        the FULL grant amount (the grant is not "lost"); the debt is settled at the wallets level.
        """
        if amount <= 0:
            raise ConflictError("amount must be positive")
        await self._ensure_wallet(user_id)

        inserted_id = await self._session.scalar(
            text(
                "INSERT INTO ledger_transactions (user_id, type, amount, meta, idempotency_key) "
                "VALUES (:uid, 'credit', :amount, CAST(:meta AS JSONB), :key) "
                "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "uid": str(user_id),
                "amount": amount,
                "meta": _json(meta),
                "key": idempotency_key,
            },
        )

        if inserted_id is None:
            existing = await self._existing_tx(user_id, idempotency_key)
            if existing is None:  # pragma: no cover - defensive
                raise ConflictError("idempotency conflict")
            if existing.type != "credit" or int(existing.amount) != amount:
                raise ConflictError("idempotency key reused with different payload")
            balance = await self._current_balance(user_id)
            return GrantResult(
                new_balance=balance, ledger_tx_id=existing.id, idempotent_replay=True
            )

        # Clawback (ADR-051 §3, gated by AGENT_DEBT_RECONCILE_ENABLED): settle debt out of the grant
        # before increasing balance. repaid = min(amount, debt) computed from the ORIGINAL debt;
        # debt -= repaid; balance += (amount - repaid). Flag off (or debt 0) → repaid 0, grant as
        # before. A snapshot subquery (prev.debt) captures the pre-update debt so RETURNING does not
        # see the already-updated value (PostgreSQL RETURNING reflects the NEW row); this is the
        # bug-fix vs computing LEAST against the new debt.
        clawback = get_settings().agent_debt_reconcile_enabled
        if clawback:
            row = await self._session.execute(
                text(
                    "UPDATE wallets w "
                    "SET debt = w.debt - LEAST(:amount, prev.debt), "
                    "    balance = w.balance + (:amount - LEAST(:amount, prev.debt)), "
                    "    updated_at = now() "
                    "FROM (SELECT debt FROM wallets WHERE user_id = :uid) AS prev "
                    "WHERE w.user_id = :uid "
                    "RETURNING w.balance, w.debt, LEAST(:amount, prev.debt) AS repaid"
                ),
                {"uid": str(user_id), "amount": amount},
            )
            updated = row.one_or_none()
            new_balance = int(updated.balance) if updated is not None else amount
            repaid = int(updated.repaid) if updated is not None else 0
            debt_remaining = int(updated.debt) if updated is not None else 0
        else:
            balance_row = await self._session.scalar(
                text(
                    "UPDATE wallets SET balance = balance + :amount, updated_at = now() "
                    "WHERE user_id = :uid RETURNING balance"
                ),
                {"uid": str(user_id), "amount": amount},
            )
            new_balance = int(balance_row) if balance_row is not None else amount
            repaid = 0
            debt_remaining = 0

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BILLING_CREDIT,
                payload={
                    "ledgerTxId": str(inserted_id),
                    "amount": amount,
                    "newBalance": new_balance,
                    "reason": reason,
                },
            )
        )
        if repaid > 0:
            await self._audit.record(
                AuditEvent(
                    user_id=user_id,
                    event_type=EVENT_BILLING_DEBT_REPAID,
                    payload={
                        "userId": str(user_id),
                        "repaid": repaid,
                        "debtRemaining": debt_remaining,
                        "grantLedgerTxId": str(inserted_id),
                    },
                )
            )
        return GrantResult(
            new_balance=new_balance, ledger_tx_id=inserted_id, idempotent_replay=False
        )

    async def get_wallet_view(
        self, user_id: uuid.UUID, last_n: int
    ) -> tuple[int, int, list[LedgerTransaction]]:
        """Return ``(balance, debt, last_transactions)`` for the wallet view (admin/02).

        ``debt`` (ADR-051) is the current ``wallets.debt`` (0 with no debt or flag off); additive
        for the admin wallet view (GET /v1/admin/wallet/{userId}).
        """
        await self._ensure_wallet(user_id)
        wallet = await self._session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        balance = int(wallet.balance) if wallet is not None else 0
        debt = int(wallet.debt) if wallet is not None else 0
        txs = list(
            await self._session.scalars(
                select(LedgerTransaction)
                .where(LedgerTransaction.user_id == user_id)
                .order_by(LedgerTransaction.created_at.desc())
                .limit(last_n)
            )
        )
        return balance, debt, txs


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)
