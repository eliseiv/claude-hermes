"""Integration: GET /v1/agent/runs/{runId}/state — HTTP contract of the run snapshot (ADR-066 §5).

Real PostgreSQL (testcontainers) through the shared ``client`` fixture. NO Hermes and NO Docker are
needed by design: the route is strictly read-only and only ``SELECT``s from ``agent_runs`` +
``agent_run_snapshots`` — which is itself one of the invariants asserted here.

Covers agent-proxy/09-testing.md §"Снапшот состояния прогона" / Integration:
- 401 matrix (the shared auth parametrize in ``test_agent_proxy_api.py`` carries the new path too);
- 404 for an unknown run AND for another user's run (RBAC-404, never 403);
- 200 for every status mapping of the ADR-066 §4 table (running / waiting_approval / paused
  +blockReason / completed / failed / stopped);
- 200 with an ``agent_runs`` row but NO snapshot → documented defaults;
- the exact camelCase response surface;
- read-only invariants (no ``ensure_running``, no HTTP to Hermes, wallet/ledger untouched);
- retention: content-cleared runs still answer 200 with status/usage/updatedAt.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user

_STATE_FIELDS = {
    "runId",
    "sessionId",
    "status",
    "resultText",
    "lastTool",
    "pendingApproval",
    "blockReason",
    "usage",
    "updatedAt",
    "continuedFrom",
}


async def _insert_run(
    session: AsyncSession,
    *,
    run_id: str,
    user_id: uuid.UUID,
    status: str = "running",
    session_id: str = "sess-1",
    paused_reason: str | None = None,
    continued_from: str | None = None,
) -> None:
    """Insert an ``agent_runs`` lifecycle row directly (the launch path needs a live Hermes)."""
    await session.execute(
        text(
            "INSERT INTO agent_runs (run_id, user_id, session_id, status, paused_reason, "
            "continued_from_run_id, model) VALUES (:r, :u, :s, CAST(:st AS agent_run_status), "
            ":pr, :cf, 'm')"
        ),
        {
            "r": run_id,
            "u": str(user_id),
            "s": session_id,
            "st": status,
            "pr": paused_reason,
            "cf": continued_from,
        },
    )


async def _insert_snapshot(
    session: AsyncSession,
    *,
    run_id: str,
    user_id: uuid.UUID,
    result_text: str = "",
    last_tool: str | None = None,
    pending_approval: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    age_days: float = 0.0,
) -> None:
    """Insert an ``agent_run_snapshots`` row; ``age_days`` back-dates ``updated_at`` (retention)."""
    await session.execute(
        text(
            "INSERT INTO agent_run_snapshots (run_id, user_id, result_text, last_tool, "
            "pending_approval, input_tokens, output_tokens, updated_at) "
            "VALUES (:r, :u, :txt, :tool, CAST(:pa AS JSONB), :i, :o, "
            "now() - make_interval(secs => :age))"
        ),
        {
            "r": run_id,
            "u": str(user_id),
            "txt": result_text,
            "tool": last_tool,
            "pa": pending_approval,
            "i": input_tokens,
            "o": output_tokens,
            "age": age_days * 86400.0,
        },
    )


async def _seed(
    m: async_sessionmaker[AsyncSession],
    *,
    run_id: str = "run_1",
    status: str = "running",
    balance: int = 100,
    **snapshot: Any,
) -> uuid.UUID:
    """Seed a subscribed user + one lifecycle row (+ optional snapshot). Returns the user id."""
    async with m() as s:
        uid = await seed_user(s, subscription="active", balance=balance)
        await _insert_run(
            s,
            run_id=run_id,
            user_id=uid,
            status=status,
            paused_reason=snapshot.pop("paused_reason", None),
            continued_from=snapshot.pop("continued_from", None),
        )
        if snapshot.pop("with_snapshot", False):
            await _insert_snapshot(s, run_id=run_id, user_id=uid, **snapshot)
        await s.commit()
    return uid


def _url(run_id: str) -> str:
    return f"/v1/agent/runs/{run_id}/state"


# ============================================================================
# 404 — unknown run and foreign run (RBAC-404, never 403)
# ============================================================================
@pytest.mark.asyncio
async def test_state_unknown_run_returns_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Runs launched before ADR-066 was deployed have no agent_runs row either and land here.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    r = await client.get(_url("run_does_not_exist"), headers=auth_headers(uid))
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_state_foreign_run_returns_404_not_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # agent-proxy/06-rbac.md: a foreign run is INVISIBLE. Answering 403 would confirm it exists.
    owner = await _seed(db_sessionmaker, run_id="run_owned", with_snapshot=True, result_text="x")
    async with db_sessionmaker() as s:
        intruder = await seed_user(s, subscription="active", balance=10)
    assert owner != intruder
    r = await client.get(_url("run_owned"), headers=auth_headers(intruder))
    assert r.status_code == 404, r.text
    # Assert the CODE, never a substring of the body: the error envelope carries a random
    # `requestId` (main._error_response), so `"403" not in r.text` would flake whenever that hex
    # id happens to contain "403".
    assert r.json()["error"]["code"] == "not_found", r.text
    # The owner still sees it (the row was really there — the 404 is authorization, not absence).
    r_owner = await client.get(_url("run_owned"), headers=auth_headers(owner))
    assert r_owner.status_code == 200, r_owner.text


# ============================================================================
# 200 — every status mapping of ADR-066 §4
# ============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("db_status", "pending_approval", "expected"),
    [
        pytest.param("running", None, "running", id="running"),
        pytest.param("resumed", None, "running", id="resumed-maps-to-running"),
        pytest.param(
            "running",
            '{"tool": "shell", "preview": "rm -rf /"}',
            "waiting_approval",
            id="running+pending-approval",
        ),
        pytest.param("completed", None, "completed", id="completed"),
        pytest.param("failed", None, "failed", id="failed"),
        pytest.param("cancelled", None, "stopped", id="cancelled-maps-to-stopped"),
    ],
)
async def test_state_status_mapping(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    db_status: str,
    pending_approval: str | None,
    expected: str,
) -> None:
    uid = await _seed(
        db_sessionmaker,
        run_id="run_map",
        status=db_status,
        with_snapshot=True,
        result_text="накопленный текст",
        pending_approval=pending_approval,
    )
    r = await client.get(_url("run_map"), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == expected
    # blockReason is filled ONLY while paused (see the dedicated test below).
    assert body["blockReason"] is None
    if pending_approval is None:
        assert body["pendingApproval"] is None
    else:
        assert body["pendingApproval"] == {"tool": "shell", "preview": "rm -rf /"}


@pytest.mark.asyncio
async def test_state_paused_carries_block_reason(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-066 §5: blockReason here carries agent_runs.paused_reason (v1: only credits_exhausted) —
    # deliberately NOT the ADR-004 policy blockReason enum. The value sets do not overlap.
    uid = await _seed(
        db_sessionmaker,
        run_id="run_paused",
        status="paused",
        paused_reason="credits_exhausted",
        with_snapshot=True,
        result_text="до паузы",
        input_tokens=1200,
        output_tokens=340,
    )
    r = await client.get(_url("run_paused"), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paused"
    assert body["blockReason"] == "credits_exhausted"
    # NOT a policy blockReason (ADR-004) — guard against anyone validating it with that enum.
    assert body["blockReason"] not in {
        "credits_empty",
        "subscription_expired",
        "trial_used",
        "debt_outstanding",
    }
    assert body["usage"] == {"inputTokens": 1200, "outputTokens": 340}


@pytest.mark.asyncio
async def test_state_paused_reason_not_leaked_on_non_paused_status(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A resumed run keeps its historical paused_reason in the row; blockReason must stay null once
    # the run is no longer paused (the client shows "why we are standing still" only when we are).
    uid = await _seed(
        db_sessionmaker,
        run_id="run_resumed",
        status="resumed",
        paused_reason="credits_exhausted",
        with_snapshot=True,
    )
    r = await client.get(_url("run_resumed"), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    assert r.json()["blockReason"] is None


# ============================================================================
# 200 — agent_runs row WITHOUT a snapshot → documented defaults
# ============================================================================
@pytest.mark.asyncio
async def test_state_without_snapshot_returns_defaults(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-066 §5: a missing snapshot is NOT an error — the relay writer simply has not flushed a
    # single event yet. updatedAt falls back to agent_runs.updated_at.
    uid = await _seed(db_sessionmaker, run_id="run_bare", status="running")
    async with db_sessionmaker() as s:
        row_updated_at = await s.scalar(
            text("SELECT updated_at FROM agent_runs WHERE run_id='run_bare'")
        )
        snap_count = await s.scalar(
            text("SELECT count(*) FROM agent_run_snapshots WHERE run_id='run_bare'")
        )
    assert snap_count == 0

    r = await client.get(_url("run_bare"), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["resultText"] == ""
    assert body["lastTool"] is None
    assert body["pendingApproval"] is None
    assert body["usage"] == {"inputTokens": 0, "outputTokens": 0}
    assert body["continuedFrom"] is None
    returned = datetime.datetime.fromisoformat(body["updatedAt"].replace("Z", "+00:00"))
    assert abs((returned - row_updated_at).total_seconds()) < 1.0


# ============================================================================
# Response surface: exact camelCase field set + types
# ============================================================================
@pytest.mark.asyncio
async def test_state_response_surface_is_camel_case_and_complete(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _seed(
        db_sessionmaker,
        run_id="run_child",
        status="running",
        with_snapshot=True,
        result_text="привет",
        last_tool="files.write",
        pending_approval='{"tool": "shell", "preview": "ls"}',
        input_tokens=7,
        output_tokens=11,
    )
    # Chain the child to a parent so continuedFrom is a real value, not just null.
    async with db_sessionmaker() as s:
        await _insert_run(s, run_id="run_parent", user_id=uid, status="resumed")
        await s.execute(
            text(
                "UPDATE agent_runs SET continued_from_run_id='run_parent' WHERE run_id='run_child'"
            )
        )
        await s.commit()

    r = await client.get(_url("run_child"), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    # Exactly the documented field set — no snake_case leakage, no extra fields.
    assert set(body) == _STATE_FIELDS, set(body) ^ _STATE_FIELDS
    assert body["runId"] == "run_child"
    assert body["sessionId"] == "sess-1"
    assert body["status"] == "waiting_approval"
    assert body["resultText"] == "привет"
    assert body["lastTool"] == "files.write"
    assert set(body["pendingApproval"]) == {"tool", "preview"}
    assert body["usage"] == {"inputTokens": 7, "outputTokens": 11}
    assert body["continuedFrom"] == "run_parent"
    # Never leak the DB-side status vocabulary (`cancelled`) nor a snake_case duplicate.
    assert "result_text" not in r.text
    assert "pending_approval" not in r.text


# ============================================================================
# Read-only invariants (MAJOR, ADR-066 §5)
# ============================================================================
@pytest.mark.asyncio
async def test_state_is_read_only_no_wake_no_hermes_no_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ensure_running would cost a ~30-40s cold start on every background polling tick (ADR-056), so
    # the route must never touch it. Poison it: any call fails the test loudly.
    from app.hermes_runtime.manager import HermesInstanceManager

    async def _boom(self: Any, user_id: uuid.UUID) -> Any:
        raise AssertionError("GET /state must NOT call ensure_running (ADR-066 §5)")

    monkeypatch.setattr(HermesInstanceManager, "ensure_running", _boom)

    uid = await _seed(
        db_sessionmaker,
        run_id="run_ro",
        status="completed",
        balance=250,
        with_snapshot=True,
        result_text="готово",
        input_tokens=9000,
        output_tokens=1000,
    )

    async def _wallet_state() -> tuple[int, int]:
        async with db_sessionmaker() as s:
            balance = await s.scalar(
                text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
            )
            ledger = await s.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"), {"u": str(uid)}
            )
        return int(balance or 0), int(ledger or 0)

    before = await _wallet_state()
    for _ in range(3):  # polling-like repetition: still free, still no side effect
        r = await client.get(_url("run_ro"), headers=auth_headers(uid))
        assert r.status_code == 200, r.text
    assert await _wallet_state() == before
    # And no policy-gate: a 200 {status:blocked} cannot occur on this route.
    assert r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_state_zero_balance_user_still_reads_state(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # No policy-gate (ADR-066 §5): a credits block applies to STARTING a generation, not to reading
    # what already happened. The same user would get 200 {blocked} from POST /v1/agent/run.
    uid = await _seed(
        db_sessionmaker,
        run_id="run_broke",
        status="completed",
        balance=0,
        with_snapshot=True,
        result_text="done",
    )
    launch = await client.post("/v1/agent/run", json={"message": "hi"}, headers=auth_headers(uid))
    assert launch.status_code == 200 and launch.json()["status"] == "blocked"

    r = await client.get(_url("run_broke"), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert r.json()["resultText"] == "done"


# ============================================================================
# Retention: a swept run keeps answering 200 with status/usage/updatedAt
# ============================================================================
@pytest.mark.asyncio
async def test_state_after_retention_sweep_keeps_status_and_usage(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository

    uid = await _seed(
        db_sessionmaker,
        run_id="run_old",
        status="completed",
        with_snapshot=True,
        result_text="секретный ответ модели",
        pending_approval='{"tool": "shell", "preview": "p"}',
        input_tokens=4321,
        output_tokens=1234,
        age_days=30.0,
    )
    before = (await client.get(_url("run_old"), headers=auth_headers(uid))).json()
    assert before["resultText"] == "секретный ответ модели"

    async with db_sessionmaker() as s:
        cleared = await AgentRunSnapshotsRepository(s).sweep_expired(14)
        await s.commit()
    assert cleared == 1

    after = (await client.get(_url("run_old"), headers=auth_headers(uid))).json()
    # Content gone, everything else intact — "the run happened, here is its outcome" is forever.
    assert after["resultText"] == ""
    assert after["pendingApproval"] is None
    assert after["status"] == "completed"
    assert after["usage"] == {"inputTokens": 4321, "outputTokens": 1234}
    # ADR-066 §7: clearing content is NOT a state update — updatedAt must not move.
    assert after["updatedAt"] == before["updatedAt"]
