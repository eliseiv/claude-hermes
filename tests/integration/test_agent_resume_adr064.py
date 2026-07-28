"""Integration: agent-run resume (continuation) — ADR-064 §5. Real PostgreSQL + mocked Hermes.

Resume launches a NEW child run in the SAME Hermes session (memory/transcript preserved). The single
race arbiter is the atomic CAS ``paused → resumed`` (``AgentRunsRepository.cas_resume``), executed
on the REAL container DB so the Postgres row lock genuinely serialises concurrent resumes. Hermes
instance is mocked at the HTTP boundary (respx): ``POST /v1/runs`` (launch) and
``GET /api/sessions/{sid}/messages`` (hydrate); ``HermesInstanceManager`` is a fake endpoint (no
Docker). ``WalletService`` / ``AuditService`` / ``AgentRunsRepository`` are REAL.

Covers ADR-064 §5:
- happy path: CAS flip, child chained (continued_from==old, same session_id), 202 {new, old};
- concurrent-resume idempotency (CRITICAL): two parallel resumes of one paused run → EXACTLY ONE
  child, a SINGLE Hermes launch, loser resolves to the child (202) or 409 resume_in_progress;
- RBAC 404 (foreign / unknown run); status pre-guard 409 run_not_resumable; policy-gate 200 blocked
  (balance still 0, status NOT flipped); hydrate 404/empty → 409 session_expired (CAS reverted);
  launch failure after CAS → 502 (CAS reverted, run stays resumable);
- hydrate maps GET /api/sessions/{sid}/messages ({data:[{role,content}]}) into conversation_history.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.service import AgentProxyService, RunResumeResult
from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository
from app.audit.service import AuditService
from app.config import Settings
from app.errors import (
    NotFoundError,
    ResumeInProgressError,
    RunNotResumableError,
    SessionExpiredError,
    UpstreamError,
)
from app.hermes_runtime.manager import InstanceEndpoint
from app.wallet.service import WalletService
from tests.conftest import seed_user

_BASE_URL = "http://hermes-user-test:8642"
_API_KEY = "super-secret-instance-bearer-key-do-not-leak"


class _FakeManager:
    def __init__(self) -> None:
        self.endpoint = InstanceEndpoint(base_url=_BASE_URL, api_key=_API_KEY)

    async def ensure_running(self, user_id: uuid.UUID) -> InstanceEndpoint:
        return self.endpoint


def _proxy(session: AsyncSession) -> AgentProxyService:
    audit = AuditService(session)
    return AgentProxyService(
        session=session,
        manager=_FakeManager(),  # type: ignore[arg-type]
        wallet=WalletService(session, audit),
        audit=audit,
        settings=Settings(**{"AGENT_INCREMENTAL_BILLING_ENABLED": True}),  # type: ignore[arg-type]
        runs=AgentRunsRepository(session),
        # ADR-066: relay-side snapshot writer (real repo — production wiring).
        snapshots=AgentRunSnapshotsRepository(session),
    )


def _hydrate_route(
    session_id: str, messages: list[dict[str, Any]] | None = None, status: int = 200
):
    payload = {"data": messages if messages is not None else [{"role": "user", "content": "hi"}]}
    return respx.get(f"{_BASE_URL}/api/sessions/{session_id}/messages").mock(
        return_value=httpx.Response(status, json=payload)
    )


def _launch_route(child_id: str = "run_child"):
    return respx.post(f"{_BASE_URL}/v1/runs").mock(
        return_value=httpx.Response(202, json={"run_id": child_id, "status": "running"})
    )


async def _seed_paused_user(
    m: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    session_id: str = "sess-1",
    balance: int = 100,
    subscription: str | None = "active",
    status: str = "paused",
) -> uuid.UUID:
    """Seed a user (active subscription + credits so policy allows) with an agent_runs row."""
    uid = uuid.uuid4()
    async with m() as s:
        await seed_user(s, user_id=uid, subscription=subscription, balance=balance)
        repo = AgentRunsRepository(s)
        await repo.create_running(run_id, uid, session_id, "m", status="running")
        if status == "paused":
            await repo.mark_paused(run_id, "credits_exhausted")
        elif status != "running":
            await repo.mark_status(run_id, status)
        await s.commit()
    return uid


async def _run_row(m: async_sessionmaker[AsyncSession], run_id: str) -> Any:
    async with m() as s:
        return (
            await s.execute(
                text(
                    "SELECT status, session_id, continued_from_run_id "
                    "FROM agent_runs WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one_or_none()


async def _children(m: async_sessionmaker[AsyncSession], parent: str) -> list[Any]:
    async with m() as s:
        rows = await s.execute(
            text(
                "SELECT run_id, session_id, status FROM agent_runs WHERE continued_from_run_id=:p"
            ),
            {"p": parent},
        )
        return list(rows)


# ============================================================================
# Happy path: CAS flip → child chained (same session, continued_from==old) → 202 {new, old}.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_resume_happy_path_chains_child_in_same_session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sid = "run_old", "sess-1"
    uid = await _seed_paused_user(db_sessionmaker, run_id=run_id, session_id=sid)
    hydrate = _hydrate_route(sid, [{"role": "user", "content": "earlier"}])
    launch = _launch_route("run_new")

    async with db_sessionmaker() as s:
        result = await _proxy(s).resume(user_id=uid, run_id=run_id, message="continue")

    assert isinstance(result, RunResumeResult)
    assert result.blocked is False
    assert result.run_id == "run_new"
    assert result.continued_from == run_id
    # Old run flipped paused→resumed; child created with continued_from==old, SAME session_id.
    old = await _run_row(db_sessionmaker, run_id)
    assert str(old.status) == "resumed"
    children = await _children(db_sessionmaker, run_id)
    assert len(children) == 1
    assert children[0].run_id == "run_new"
    assert children[0].session_id == sid  # continuation shares the Hermes session
    # Hydrate was called and its transcript was injected as conversation_history in the launch body.
    assert hydrate.called
    assert launch.called
    launch_body = json.loads(launch.calls.last.request.content)
    assert launch_body["session_id"] == sid
    assert launch_body["input"] == "continue"
    assert launch_body["conversation_history"] == [{"role": "user", "content": "earlier"}]
    # Launch carried the instance Bearer, never leaked to the client.
    assert launch.calls.last.request.headers["authorization"] == f"Bearer {_API_KEY}"


# ============================================================================
# Concurrent resume (CRITICAL): two parallel resumes → EXACTLY ONE child, ONE launch.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_concurrent_resume_yields_exactly_one_child(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sid = "run_old", "sess-1"
    uid = await _seed_paused_user(db_sessionmaker, run_id=run_id, session_id=sid)
    _hydrate_route(sid)
    # Distinct child id per launch so a double-launch would create TWO distinct children.
    counter = {"n": 0}

    def _launch(_request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(
            202, json={"run_id": f"run_child_{counter['n']}", "status": "running"}
        )

    launch = respx.post(f"{_BASE_URL}/v1/runs").mock(side_effect=_launch)

    async def _do_resume() -> Any:
        async with db_sessionmaker() as s:
            try:
                return await _proxy(s).resume(user_id=uid, run_id=run_id, message="go")
            except (ResumeInProgressError, RunNotResumableError) as exc:
                return exc

    results = await asyncio.gather(_do_resume(), _do_resume(), return_exceptions=True)

    # EXACTLY ONE Hermes launch (single-flight CAS) and EXACTLY ONE child row.
    assert launch.call_count == 1, "CAS must serialise: only the winner launches a child"
    children = await _children(db_sessionmaker, run_id)
    assert len(children) == 1, f"exactly one continuation child; got {children}"
    child_id = children[0].run_id
    # Winner returned the child (202); loser returned the same child (202) or 409-in-progress.
    successes = [r for r in results if isinstance(r, RunResumeResult)]
    assert any(r.run_id == child_id for r in successes)
    for r in results:
        if isinstance(r, RunResumeResult):
            assert r.blocked is False
            assert r.run_id == child_id  # never a second child
        else:
            assert isinstance(r, ResumeInProgressError)
    # Old run is resumed (terminal marker), never double-flipped.
    assert str((await _run_row(db_sessionmaker, run_id)).status) == "resumed"


# ============================================================================
# RBAC 404: foreign user / unknown run.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_resume_foreign_run_is_404(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _seed_paused_user(db_sessionmaker, run_id="run_old")
    other = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=other, subscription="active", balance=100)
    async with db_sessionmaker() as s:
        with pytest.raises(NotFoundError):
            await _proxy(s).resume(user_id=other, run_id="run_old", message=None)  # not owner
    async with db_sessionmaker() as s:
        with pytest.raises(NotFoundError):
            await _proxy(s).resume(user_id=uid, run_id="does_not_exist", message=None)


# ============================================================================
# Status pre-guard: a non-{paused,resumed} run → 409 run_not_resumable.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled"])
async def test_resume_non_resumable_status_is_409(
    db_sessionmaker: async_sessionmaker[AsyncSession], status: str
) -> None:
    uid = await _seed_paused_user(db_sessionmaker, run_id="run_old", status=status)
    async with db_sessionmaker() as s:
        with pytest.raises(RunNotResumableError):
            await _proxy(s).resume(user_id=uid, run_id="run_old", message=None)
    # Status untouched (no CAS on a non-resumable run).
    assert str((await _run_row(db_sessionmaker, "run_old")).status) == status


# ============================================================================
# Policy-gate: balance still 0 → 200 blocked, status NOT flipped (no CAS, no launch).
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_resume_still_blocked_when_balance_zero_does_not_flip(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Active subscription but ZERO balance → credits_empty → resume returns 200 blocked before CAS.
    uid = await _seed_paused_user(db_sessionmaker, run_id="run_old", balance=0)
    launch = _launch_route()
    async with db_sessionmaker() as s:
        result = await _proxy(s).resume(user_id=uid, run_id="run_old", message=None)
    assert result.blocked is True
    assert result.block_reason == "credits_empty"
    assert result.run_id is None
    # Status NOT flipped (still paused); no child; no launch.
    assert str((await _run_row(db_sessionmaker, "run_old")).status) == "paused"
    assert await _children(db_sessionmaker, "run_old") == []
    assert not launch.called


# ============================================================================
# Hydrate 404 / empty transcript → 409 session_expired, CAS reverted (run stays paused).
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_resume_hydrate_404_is_session_expired_and_reverts(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sid = "run_old", "sess-1"
    uid = await _seed_paused_user(db_sessionmaker, run_id=run_id, session_id=sid)
    _hydrate_route(sid, status=404)
    launch = _launch_route()
    async with db_sessionmaker() as s:
        with pytest.raises(SessionExpiredError):
            await _proxy(s).resume(user_id=uid, run_id=run_id, message=None)
    # CAS reverted resumed→paused (run stays resumable); no child; launch never happened.
    assert str((await _run_row(db_sessionmaker, run_id)).status) == "paused"
    assert await _children(db_sessionmaker, run_id) == []
    assert not launch.called


@respx.mock
@pytest.mark.asyncio
async def test_resume_hydrate_empty_history_is_session_expired(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sid = "run_old", "sess-1"
    uid = await _seed_paused_user(db_sessionmaker, run_id=run_id, session_id=sid)
    _hydrate_route(sid, messages=[])  # empty transcript → cannot continue
    _launch_route()
    async with db_sessionmaker() as s:
        with pytest.raises(SessionExpiredError):
            await _proxy(s).resume(user_id=uid, run_id=run_id, message=None)
    assert str((await _run_row(db_sessionmaker, run_id)).status) == "paused"


# ============================================================================
# Launch failure AFTER a won CAS → 502, CAS reverted (run stays resumable).
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_resume_launch_failure_reverts_cas_and_502(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sid = "run_old", "sess-1"
    uid = await _seed_paused_user(db_sessionmaker, run_id=run_id, session_id=sid)
    _hydrate_route(sid)
    # Launch returns non-2xx → _launch_run raises UpstreamError → resume reverts the CAS → 502.
    respx.post(f"{_BASE_URL}/v1/runs").mock(return_value=httpx.Response(500, json={"e": "boom"}))
    async with db_sessionmaker() as s:
        with pytest.raises(UpstreamError):
            await _proxy(s).resume(user_id=uid, run_id=run_id, message=None)
    # Reverted resumed→paused; no child; run stays resumable.
    assert str((await _run_row(db_sessionmaker, run_id)).status) == "paused"
    assert await _children(db_sessionmaker, run_id) == []


# ============================================================================
# Idempotent sequential re-resume after completion (status='resumed') → the SAME child.
# ============================================================================
@respx.mock
@pytest.mark.asyncio
async def test_resume_after_resumed_returns_same_child(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    run_id, sid = "run_old", "sess-1"
    uid = await _seed_paused_user(db_sessionmaker, run_id=run_id, session_id=sid)
    _hydrate_route(sid)
    _launch_route("run_new")
    async with db_sessionmaker() as s:
        first = await _proxy(s).resume(user_id=uid, run_id=run_id, message=None)
    # A second resume of the now-'resumed' run resolves idempotently to the SAME child (no 2nd CAS).
    async with db_sessionmaker() as s:
        second = await _proxy(s).resume(user_id=uid, run_id=run_id, message=None)
    assert first.run_id == second.run_id == "run_new"
    assert second.continued_from == run_id
    assert len(await _children(db_sessionmaker, run_id)) == 1
