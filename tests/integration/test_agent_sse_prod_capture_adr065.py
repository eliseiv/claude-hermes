"""Integration: the raw production SSE capture driven through the REAL relay + PostgreSQL.

The unit twin (``tests/unit/test_agent_sse_delta_contract_adr065.py``) proves the parser reads the
captured wire shape; this module proves the value survives all the way into the column
``GET /v1/agent/runs/{runId}/state`` reads — the surface the ADR-066 prod defect was reported on
(``resultText`` identically empty). Real repositories, real wallet, real ``agent_run_snapshots``
SQL (per-column guard included); only the Hermes boundary is mocked (respx), per
06-testing-strategy.md §Политика моков.

FIRST SOURCE: ``tests/fixtures/hermes_prod_run_adr065.sse`` — byte-verbatim capture of a live prod
run (see the unit module docstring for provenance and for what the capture does NOT contain).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.service import AgentProxyService
from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository
from app.audit.service import AuditService
from app.config import Settings
from app.observability.redaction import redact
from app.wallet.service import WalletService
from tests.conftest import seed_user
from tests.unit.test_agent_proxy_service import _capture_service_logs

_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hermes_prod_run_adr065.sse"
_RUN_ID = "run_d931839587a64e3885b4d096cf7440d0"
_EXPECTED_TEXT = "Я не могу сейчас выходить в интернет из этой среды 2024?"


class _FakeManager:
    """Stand-in for HermesInstanceManager: a fixed endpoint, no Docker."""

    def __init__(self) -> None:
        from app.hermes_runtime.manager import InstanceEndpoint

        self.endpoint = InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)

    async def ensure_running(self, user_id: uuid.UUID, *, deadline: float | None = None) -> Any:
        return self.endpoint


def _proxy(session: AsyncSession, **overrides: Any) -> AgentProxyService:
    base: dict[str, Any] = {"AGENT_STATE_FLUSH_INTERVAL_SECONDS": 0.0}
    base.update(overrides)
    audit = AuditService(session)
    return AgentProxyService(
        session=session,
        manager=_FakeManager(),  # type: ignore[arg-type]
        wallet=WalletService(session, audit),
        audit=audit,
        settings=Settings(**base),  # type: ignore[arg-type]
        runs=AgentRunsRepository(session),
        snapshots=AgentRunSnapshotsRepository(session),
    )


async def _collect(stream: Any) -> bytes:
    out = b""
    async for chunk in stream:
        out += chunk
    return out


async def _seed_run(m: async_sessionmaker[AsyncSession], run_id: str, *, balance: int) -> uuid.UUID:
    async with m() as s:
        uid = await seed_user(s, subscription="active", balance=balance)
        await AgentRunsRepository(s).create_running(run_id, uid, "sess-1", "m")
        await s.commit()
    return uid


async def _snapshot(m: async_sessionmaker[AsyncSession], run_id: str) -> Any:
    async with m() as s:
        return (
            await s.execute(
                text(
                    "SELECT result_text, input_tokens, output_tokens FROM agent_run_snapshots "
                    "WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one_or_none()


@respx.mock
@pytest.mark.asyncio
async def test_prod_capture_lands_in_result_text_and_pauses_with_the_answer(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The reported defect, reproduced end to end on the real bytes and now green.

    Zero balance + incremental billing reproduces the captured run exactly: the ``usage.delta``
    anchor depletes the wallet, the relay interrupts and emits its synthetic ``run.paused``. Before
    the parser fix BOTH observable outputs of this run were empty strings.
    """
    uid = await _seed_run(db_sessionmaker, _RUN_ID, balance=0)
    body = _FIXTURE.read_bytes()
    respx.get(f"{_BASE_URL}/v1/runs/{_RUN_ID}/events").mock(
        return_value=httpx.Response(200, content=body)
    )
    respx.post(f"{_BASE_URL}/v1/runs/{_RUN_ID}/stop").mock(
        return_value=httpx.Response(200, json={})
    )

    async with db_sessionmaker() as s:
        out = await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                user_id=uid, run_id=_RUN_ID
            )
        )

    # 1. The synthetic terminal block carries the assistant answer (prod delivered "").
    paused = json.loads(out.rsplit(b"data: ", 1)[1])
    assert paused["event"] == "run.paused"
    assert paused["reason"] == "credits_exhausted"
    assert paused["output"] == _EXPECTED_TEXT

    # 2. The snapshot column behind GET …/state — written through the real ON CONFLICT statement.
    row = await _snapshot(db_sessionmaker, _RUN_ID)
    assert row is not None, "no snapshot row was written at all"
    assert row.result_text == _EXPECTED_TEXT
    # Token anchors come from the flat cumulative_* fields of the real usage.delta.
    assert (row.input_tokens, row.output_tokens) == (6313, 658)

    # 3. The client-facing view: non-empty resultText on a paused run (the defect's report surface).
    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=uid, run_id=_RUN_ID)
    assert view.status == "paused"
    assert view.result_text == _EXPECTED_TEXT


@respx.mock
@pytest.mark.asyncio
async def test_prod_capture_fills_result_text_with_billing_flag_off(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Text accumulation is independent of the billing flag: the whole capture relays, text lands.

    With billing OFF nothing depletes, so the capture is replayed to its end — including its own
    trailing ``run.paused`` block, which the relay forwards without interpreting it.
    """
    run_id = "run_capture_flag_off"
    uid = await _seed_run(db_sessionmaker, run_id, balance=1000)
    body = _FIXTURE.read_bytes()
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=body)
    )

    async with db_sessionmaker() as s:
        out = await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=False).stream_events(
                user_id=uid, run_id=run_id
            )
        )
    assert out == body, "with nothing to bill the capture must relay byte-for-byte"

    row = await _snapshot(db_sessionmaker, run_id)
    assert row is not None
    assert row.result_text == _EXPECTED_TEXT
    # The usage.delta anchors are persisted by their OWN immediate flush, so they no longer depend
    # on a later text flush or on a terminal event arriving at all. This assertion previously read
    # (0, 0) and documented that gap as expected behaviour — which was the wrong thing for a test to
    # do: /state reported usage {0,0} for a run whose tokens were known. Anchors land regardless of
    # the billing flag (05-events.md §snapshot table).
    assert (row.input_tokens, row.output_tokens) == (6313, 658)

    # No debit happened on this path (flag OFF, no run.completed in the capture).
    async with db_sessionmaker() as s:
        spent = (
            await s.execute(
                text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"), {"u": str(uid)}
            )
        ).scalar_one()
    assert spent == 0


# ==================================================================================================
# ADR-064 incremental path vs the _UsageCounts refactor — REAL wallet, REAL ledger.
#
# The unit doubles cannot carry these: FakeWallet returns charged_amount=0, which the per-step
# biller correctly reads as "the balance did not move" and pauses on. Money assertions belong on
# the real ledger anyway (06-testing-strategy.md §Политика моков).
# ==================================================================================================
def _block(payload: dict[str, Any]) -> bytes:
    """One SSE block in the production wire format: ``data: {json}`` + blank LF line, no header."""
    return f"data: {json.dumps(payload)}\n\n".encode()


def _usage_delta(run_id: str, step: int, cum_in: int, cum_out: int) -> bytes:
    """A ``usage.delta`` in the FLAT captured shape (per-step and cumulative side by side)."""
    return _block(
        {
            "event": "usage.delta",
            "run_id": run_id,
            "step_index": step,
            "input_tokens": cum_in,
            "output_tokens": cum_out,
            "cumulative_input_tokens": cum_in,
            "cumulative_output_tokens": cum_out,
            "model": "gpt-5-mini",
        }
    )


def _completed(run_id: str, usage: dict[str, Any]) -> bytes:
    return _block({"event": "run.completed", "run_id": run_id, "usage": usage, "model": "m"})


async def _debits(m: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[tuple[str, int]]:
    """(idempotency_key, amount) of every debit of this user, in ledger order."""
    async with m() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT idempotency_key, amount FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit' ORDER BY created_at, id"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [(r.idempotency_key, r.amount) for r in rows]


@respx.mock
@pytest.mark.asyncio
async def test_incremental_telescoping_and_final_remainder_survive_the_usage_counts_refactor(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """E. Per-step charges still telescope and the terminal remainder is still owed_final - charged.

    Steps at cumulative 1000/1000 (6 credits) and 2000/2000 (12) charge 6 then 6; the terminal
    payload owes 12 in total, so the remainder is 0 and NO third debit is written. A regression in
    which carrier ``_bill_completed`` reads would surface here as a duplicate or inflated charge.
    """
    run_id = "run_incr_telescope"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(
            200,
            content=_usage_delta(run_id, 1, 1000, 1000)
            + _usage_delta(run_id, 2, 2000, 2000)
            + _completed(run_id, {"input_tokens": 2000, "output_tokens": 2000}),
        )
    )
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                user_id=uid, run_id=run_id
            )
        )

    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:1", 6), (f"{run_id}:2", 6)]
    async with db_sessionmaker() as s:
        balance = (
            await s.execute(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        ).scalar_one()
    assert balance == 10_000 - 12, "Σ per-step != usage_to_credits(final)"


@respx.mock
@pytest.mark.asyncio
async def test_incremental_remainder_is_billed_when_completed_exceeds_the_steps(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """E. The finalisation path itself: one top-up under the BARE run_id key."""
    run_id = "run_incr_remainder"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(
            200,
            content=_usage_delta(run_id, 1, 1000, 1000)
            + _completed(run_id, {"input_tokens": 2000, "output_tokens": 2000}),
        )
    )
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                user_id=uid, run_id=run_id
            )
        )
    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:1", 6), (run_id, 6)]


@respx.mock
@pytest.mark.asyncio
async def test_incremental_replayed_usage_delta_is_still_idempotent(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """E. A replayed step keeps its ``run_id:step`` key: one ledger row, no spurious pause."""
    run_id = "run_incr_replay"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=_usage_delta(run_id, 1, 1000, 1000) * 2)
    )
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                user_id=uid, run_id=run_id
            )
        )
    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:1", 6)]
    async with db_sessionmaker() as s:
        status = (
            await s.execute(text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id})
        ).scalar_one()
    assert str(status) == "running", "an idempotent replay must not look like depletion"


@respx.mock
@pytest.mark.asyncio
async def test_run_completed_with_cumulative_names_is_now_billable(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """E/C. New capability of the union carrier: the cumulative dialect inside ``usage`` bills.

    Before ``_extract_usage_counts`` only ``usage.input_tokens``/``output_tokens`` were probed, so a
    terminal payload speaking the cumulative dialect billed 0 credits in silence — the same class of
    defect as ADR-066, on the money path.
    """
    run_id = "run_completed_cumulative"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(
            200,
            content=_completed(
                run_id, {"cumulative_input_tokens": 2000, "cumulative_output_tokens": 1000}
            ),
        )
    )
    async with db_sessionmaker() as s:
        await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))
    # 2000 in + 1000 out at 1.0/5.0 credits per 1k = 2 + 5 = 7.
    assert await _debits(db_sessionmaker, uid) == [(run_id, 7)]


# ==================================================================================================
# Usage anomalies on the PER-STEP path, and what the ledger actually records.
#
# These live here rather than beside the unit matrices because both claims are about persisted
# money: "one warning per run, not per step" is only meaningful when several steps really bill, and
# the ledger meta must be asserted on the row Postgres stored, not on the dict we passed.
# ==================================================================================================
def _half_read_usage_delta(run_id: str, step: int, cum_in: int) -> bytes:
    """A ``usage.delta`` carrying only the INPUT anchor — the other half is missing entirely."""
    return _block(
        {
            "event": "usage.delta",
            "run_id": run_id,
            "step_index": step,
            "cumulative_input_tokens": cum_in,
            "model": "gpt-5-mini",
        }
    )


@respx.mock
@pytest.mark.asyncio
async def test_half_read_usage_delta_warns_once_per_run_not_once_per_step(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """D. A systematically half-read stream reports ONE line, and still bills what it could read.

    The anomaly is a property of the run, not of each step: an unlatched line would repeat for every
    usage event all run long and stop being read — which is how the original defect survived.
    """
    run_id = "run_half_read"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(
            200,
            content=_half_read_usage_delta(run_id, 1, 1000)
            + _half_read_usage_delta(run_id, 2, 2000)
            + _half_read_usage_delta(run_id, 3, 3000),
        )
    )
    with _capture_service_logs() as logs:
        async with db_sessionmaker() as s:
            await _collect(
                _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                    user_id=uid, run_id=run_id
                )
            )

    half = [m for m in logs.messages if "usage half-read" in m]
    assert len(half) == 1, f"latched per run, not per step: {half}"
    assert "usage.delta" in half[0], "the line must say WHICH path read badly"
    assert "top.cumulative" in half[0], "and which carrier it read"
    # The readable half is still billed — losing it would turn an observability gap into revenue
    # loss. 1000/2000/3000 input tokens at 1.0 per 1k = 1, 1, 1 credits.
    assert await _debits(db_sessionmaker, uid) == [
        (f"{run_id}:1", 1),
        (f"{run_id}:2", 1),
        (f"{run_id}:3", 1),
    ]


@respx.mock
@pytest.mark.asyncio
async def test_incremental_debit_meta_records_raw_event_values_and_the_billed_anchors(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """F. The ledger row states BOTH what arrived and what the debit was computed from.

    They can legitimately differ (the fold may bill from another carrier, or from a partial match
    whose other half is an invented zero), so recording only one of them would make the audit trail
    assert something the event never said.
    """
    run_id = "run_meta_shape"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=_usage_delta(run_id, 1, 6313, 658))
    )
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                user_id=uid, run_id=run_id
            )
        )

    async with db_sessionmaker() as s:
        meta = (
            await s.execute(
                text("SELECT meta FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
                {"u": str(uid)},
            )
        ).scalar_one()

    usage_meta = meta["usage"]
    # RAW, under the names the event used.
    assert usage_meta["input_tokens"] == 6313
    assert usage_meta["output_tokens"] == 658
    assert usage_meta["cumulative_input_tokens"] == 6313
    assert usage_meta["cumulative_output_tokens"] == 658
    # DERIVED, kept separate and labelled with the carriers it came from.
    assert usage_meta["billed_input"] == 6313
    assert usage_meta["billed_output"] == 658
    assert set(usage_meta["billed_from"]) == {"top.cumulative", "top.per_step"}
    assert meta["incremental"] is True and meta["runId"] == run_id


def test_ledger_usage_meta_survives_adr049_redaction() -> None:
    """F. The billing fields must not be collateral damage of the ``*token*`` denylist.

    ``meta.usage`` reaches ``ledger_transactions.meta`` raw today, but any future audit path that
    routes it through ``redact()`` must keep every count: they are billing analytics, not secrets
    (ADR-049). The ``billed_*`` names avoid the ``*_tokens`` suffix deliberately — this test is what
    makes that reasoning executable rather than a comment.
    """
    meta = {
        "source": "agent_run",
        "incremental": True,
        "runId": "run_1",
        "stepIndex": 1,
        "usage": {
            "input_tokens": 6313,
            "output_tokens": 658,
            "cumulative_input_tokens": 6313,
            "cumulative_output_tokens": 658,
            "billed_input": 6313,
            "billed_output": 658,
            "billed_from": ["top.cumulative", "top.per_step"],
        },
        "model": "gpt-5-mini",
    }
    redacted = redact(meta)
    assert redacted["usage"] == meta["usage"], "redaction ate a billing count"
    # ... while a real secret sharing the denylist substring is still removed.
    assert redact({"api_key": "sk-secret"})["api_key"] != "sk-secret"


@respx.mock
@pytest.mark.asyncio
async def test_real_capture_bills_the_amount_its_own_numbers_imply(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """G. The money path end to end on the FIRST SOURCE, with enough balance to complete.

    6313 input + 658 output at the default 1.0 / 5.0 credits per 1k = 6.313 + 3.29 → 10 credits.
    No divergence and no half-read warning: the captured event's two carriers agree.
    """
    run_id = _RUN_ID + "_paid"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    # The captured bytes carry the original run id inside the JSON; only the route id matters here.
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=_FIXTURE.read_bytes())
    )
    with _capture_service_logs() as logs:
        async with db_sessionmaker() as s:
            await _collect(
                _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                    user_id=uid, run_id=run_id
                )
            )
    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:1", 10)]
    assert [m for m in logs.messages if "half-read" in m or "disagree" in m] == []

    row = await _snapshot(db_sessionmaker, run_id)
    assert row is not None
    assert row.result_text == _EXPECTED_TEXT
    # Ledger and snapshot now agree: the same anchors that produced the 10-credit debit are in the
    # snapshot, committed in the same beat as the debit. This previously asserted (0, 0) — i.e. a
    # run billed 10 credits while /state reported usage {0,0}, which is the discrepancy a client
    # would file a bug about.
    assert (row.input_tokens, row.output_tokens) == (6313, 658)


# ==================================================================================================
# The usage.delta flush: anchors land immediately, approval state is NOT spoken for.
#
# Both halves of one change, and they pull in opposite directions — which is why they are tested
# together: bypassing the throttle is what makes the anchors durable, and asserting approval on that
# same write is what would resurrect an answered approval.
# ==================================================================================================
@respx.mock
@pytest.mark.asyncio
async def test_stream_dropping_right_after_usage_delta_still_persists_the_anchors(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """C (MAJOR 2). No terminal event ever arrives — the anchors must already be in the snapshot.

    This is the ordinary shape of a dropped SSE connection, and it is not recoverable later: the
    Hermes buffer replays to the FIRST subscriber only (Q-066-1), so a run whose anchors were still
    buffered in relay memory would report usage {0,0} through /state forever while the ledger had
    already been charged for that step.
    """
    run_id = "run_drop_after_usage"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=_usage_delta(run_id, 1, 6313, 658))
    )
    async with db_sessionmaker() as s:
        await _collect(
            _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True).stream_events(
                user_id=uid, run_id=run_id
            )
        )

    row = await _snapshot(db_sessionmaker, run_id)
    assert row is not None, "no snapshot row at all — the anchors never reached the DB"
    assert (row.input_tokens, row.output_tokens) == (6313, 658)
    # The ledger and the snapshot were written in the same beat: neither may be ahead of the other.
    assert await _debits(db_sessionmaker, uid) == [(f"{run_id}:1", 10)]


@respx.mock
@pytest.mark.asyncio
async def test_usage_delta_flush_does_not_resurrect_an_answered_approval(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """C (MAJOR 2, the other half). The client answers mid-run; the next anchor flush stays quiet.

    The relay still holds ``{tool, preview}`` in memory when the ``usage.delta`` arrives, so an
    approval-asserting write here would restore a state the user already resolved and /state would
    fall back to ``waiting_approval`` on a run that is happily working. The clear is applied from a
    SEPARATE session mid-stream, exactly as ``POST …/approval`` does out of band.
    """
    run_id = "run_approval_not_resurrected"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(
            200,
            content=_block(
                {
                    "event": "approval.request",
                    "run_id": run_id,
                    "tool": "shell",
                    "preview": "rm -rf",
                }
            )
            + _usage_delta(run_id, 1, 6313, 658),
        )
    )

    async with db_sessionmaker() as s:
        proxy = _proxy(s, AGENT_INCREMENTAL_BILLING_ENABLED=True)
        answered = False
        async for chunk in proxy.stream_events(user_id=uid, run_id=run_id):
            # The relay yields each block's raw bytes BEFORE handling it, so seeing the usage.delta
            # chunk is exactly the window in which the approval flush has already landed and the
            # anchor flush has not yet run. Answering here is what a client does out of band.
            if b"usage.delta" in chunk and not answered:
                answered = True
                async with db_sessionmaker() as other:
                    cleared = await AgentRunSnapshotsRepository(other).clear_pending_approval(
                        run_id, uid
                    )
                    await other.commit()
                assert cleared == 1, "the approval was never recorded to begin with"
    assert answered, "the usage.delta block never reached the relay"

    async with db_sessionmaker() as s:
        pending = (
            await s.execute(
                text("SELECT pending_approval FROM agent_run_snapshots WHERE run_id=:r"),
                {"r": run_id},
            )
        ).scalar_one()
    assert pending is None, "an answered approval was resurrected by the usage.delta flush"

    # ... and the anchors of that very flush still landed: the two properties are independent.
    row = await _snapshot(db_sessionmaker, run_id)
    assert row is not None
    assert (row.input_tokens, row.output_tokens) == (6313, 658)


# ==================================================================================================
# The ADR-067 completion capture, end to end against a real database.
#
# Provenance and the shortening rule: see the module docstring of
# tests/unit/test_agent_sse_delta_contract_adr065.py and the registry in
# docs/06-testing-strategy.md.
# This capture is the one that settles the money path — a run that actually reaches run.completed.
# ==================================================================================================
_COMPLETED_FIXTURE = _FIXTURE.parent / "hermes_prod_completed_run_adr067.sse"
_COMPLETED_STATE = _FIXTURE.parent / "hermes_prod_completed_run_adr067.state.json"


@respx.mock
@pytest.mark.asyncio
async def test_completed_capture_agrees_with_the_ledger_and_the_state_endpoint(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """(b) One real run, three records that must agree: capture, ledger, /state.

    The production /state body of this very run was captured alongside the stream, so the assertion
    is not "what our code computes" but "what production actually answered".
    """
    run_id = "run_3b3b1253e0974b24b594b7452bc7095d"
    uid = await _seed_run(db_sessionmaker, run_id, balance=10_000)
    respx.get(f"{_BASE_URL}/v1/runs/{run_id}/events").mock(
        return_value=httpx.Response(200, content=_COMPLETED_FIXTURE.read_bytes())
    )
    with _capture_service_logs() as logs:
        async with db_sessionmaker() as s:
            await _collect(_proxy(s).stream_events(user_id=uid, run_id=run_id))

    expected = json.loads(_COMPLETED_STATE.read_text())

    # 1. Lifecycle status recorded from the terminal event.
    async with db_sessionmaker() as s:
        status = (
            await s.execute(text("SELECT status FROM agent_runs WHERE run_id=:r"), {"r": run_id})
        ).scalar_one()
    assert str(status) == expected["status"] == "completed"

    # 2. The snapshot behind GET …/state — text present exactly once, usage from the terminal block.
    row = await _snapshot(db_sessionmaker, run_id)
    assert row is not None
    assert row.result_text == expected["resultText"] == "DONE."
    assert (
        (row.input_tokens, row.output_tokens)
        == (
            expected["usage"]["inputTokens"],
            expected["usage"]["outputTokens"],
        )
        == (6302, 586)
    )

    # 3. The ledger: 6302 in + 586 out at 1.0 / 5.0 per 1k = 6.302 + 2.93 → 10 credits, once.
    assert await _debits(db_sessionmaker, uid) == [(run_id, 10)]

    # 4. The client view derived from all of it.
    async with db_sessionmaker() as s:
        view = await _proxy(s).get_state(user_id=uid, run_id=run_id)
    assert view.status == "completed"
    assert view.result_text == "DONE."

    # 5. Nine iterations of alarms, silent on a healthy production run.
    assert [m for m in logs.messages if "usage" in m or "no text" in m] == []
