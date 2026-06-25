"""Integration: trial claim-before-generation + reconcile-on-failure (TD-006 / ADR-054).

Real PostgreSQL container; Anthropic faked at the client boundary. Covers BOTH execution paths of a
trial turn (qa continuation/tool-loop rule):
- direct /chat/run: race of two concurrent first runs → EXACTLY one free generation (the loser is
  blocked/trial_used WITHOUT a generation); reconcile rolls the claim back on a non-success
  (max_tokens / UpstreamError) so the trial survives; a successful turn keeps the claim (no double
  flip);
- continuation /chat/tool-result (§2a): a trial turn that returns tool_call must NOT be falsely
  blocked by its OWN claim on continuation (re-evaluate would otherwise see trial_used=TRUE and
  block) — it reaches assistant_message and the trial stays burned; a failed continuation rolls the
  claim back.

A trial-eligible user is subscription=none + trial_used=false + mode=credits (ADR-002): policy
allows via the single lifetime trial. ``_billing_plan`` sets mark_trial for exactly this state.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_WRITE = {"path": "index.html", "content": "<h1>x</h1>", "encoding": "utf8", "overwrite": True}


async def _trial_used(maker: async_sessionmaker[AsyncSession], uid: object) -> bool:
    async with maker() as s:
        return bool(
            await s.scalar(text("SELECT trial_used FROM users WHERE id=:u"), {"u": str(uid)})
        )


async def _debit_count(maker: async_sessionmaker[AsyncSession], uid: object) -> int:
    async with maker() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"),
                {"u": str(uid)},
            )
            or 0
        )


def _run_body(uid: uuid.UUID) -> dict:
    # subscription=none, no project (chat), credits mode → trial-allow branch.
    return {"userId": str(uid), "message": "hi", "mode": "credits"}


# ============================================================================
# §1 success path: trial claimed before generation, burns on success, NO double flip, no debit
# ============================================================================
@pytest.mark.asyncio
async def test_trial_success_burns_once_no_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)  # subscription=none, trial-eligible
    fake_anthropic.responses = [fake_anthropic.text_result("hello")]

    r = await client.post("/v1/chat/run", json=_run_body(uid), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message", r.json()
    # Trial burned exactly once; trial generation is FREE (no debit, ADR-006).
    assert await _trial_used(db_sessionmaker, uid) is True
    assert await _debit_count(db_sessionmaker, uid) == 0


# ============================================================================
# §1 race: two concurrent first /chat/run → exactly ONE free generation, loser blocked/trial_used
# ============================================================================
@pytest.mark.asyncio
async def test_concurrent_first_runs_yield_exactly_one_free_generation(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    # Script enough successful responses that BOTH would generate if the claim did not serialize.
    fake_anthropic.responses = [
        fake_anthropic.text_result("a"),
        fake_anthropic.text_result("b"),
    ]

    body = _run_body(uid)
    r1, r2 = await asyncio.gather(
        client.post("/v1/chat/run", json=body, headers=auth_headers(uid)),
        client.post("/v1/chat/run", json=body, headers=auth_headers(uid)),
    )
    statuses = sorted([r1.json()["status"], r2.json()["status"]])
    # Exactly one assistant_message (the trial winner) and one blocked (the loser).
    assert statuses == ["assistant_message", "blocked"], (r1.json(), r2.json())
    blocked = r1.json() if r1.json()["status"] == "blocked" else r2.json()
    assert blocked["blockReason"] == "trial_used"
    # The claim is the single arbiter: exactly ONE generation reached Anthropic.
    assert len(fake_anthropic.calls) == 1
    # Trial is burned, and no credit was debited (trial is free).
    assert await _trial_used(db_sessionmaker, uid) is True
    assert await _debit_count(db_sessionmaker, uid) == 0


# ============================================================================
# §2 reconcile: max_tokens (not a successful final) → claim rolled back, trial survives
# ============================================================================
@pytest.mark.asyncio
async def test_trial_max_tokens_rolls_back_claim(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    fake_anthropic.responses = [fake_anthropic.max_tokens_result(text="partial")]

    r = await client.post("/v1/chat/run", json=_run_body(uid), headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "blocked"
    assert body["blockReason"] == "max_tokens"
    # ADR-054 §2: a claimed trial burns ONLY on success → max_tokens reconciles it back to FALSE.
    assert await _trial_used(db_sessionmaker, uid) is False


# ============================================================================
# §2 reconcile: UpstreamError (502) → claim rolled back, trial survives, error contract unchanged
# ============================================================================
@pytest.mark.asyncio
async def test_trial_upstream_error_rolls_back_claim_and_502(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    fake_anthropic.raise_upstream = True  # create_message raises UpstreamError → 502

    r = await client.post("/v1/chat/run", json=_run_body(uid), headers=auth_headers(uid))
    assert r.status_code == 502, r.text
    # Trial restored despite the upstream failure (user keeps their single trial).
    assert await _trial_used(db_sessionmaker, uid) is False


# ============================================================================
# §2a continuation: trial turn returns tool_call → /chat/tool-result NOT blocked by own claim;
#      reaches assistant_message; trial stays burned (no false trial_used block, no double flip)
# ============================================================================
@pytest.mark.asyncio
async def test_trial_tool_loop_continuation_not_blocked_by_own_claim(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    # Turn 0: a client-side tool_call (files.write). Continuation: final assistant_message.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.write", _WRITE, tool_id="toolu_01TrialA"),
        fake_anthropic.text_result("done"),
    ]
    # A project is required for the session to allow site.* — but files.* is client-side and offered
    # regardless; provide a projectId so the chat session is created normally.
    run = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert run.status_code == 200, run.text
    run_body = run.json()
    assert run_body["status"] == "tool_call", run_body
    sid = run_body["sessionId"]
    tool_call_id = run_body["toolCall"]["id"]
    # Trial already claimed on /chat/run (mid-turn): trial_used=TRUE while the tool-loop is open.
    assert await _trial_used(db_sessionmaker, uid) is True

    # Continuation: re-evaluate would see trial_used=TRUE + subscription=none and WOULD block with
    # trial_used — §2a must un-block the turn's OWN claim. Expect a normal assistant_message.
    tr = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "toolCallId": tool_call_id,
            "result": {"ok": 1},
        },
        headers=auth_headers(uid),
    )
    assert tr.status_code == 200, tr.text
    assert tr.json()["status"] == "assistant_message", tr.json()
    # Trial stays burned (success); no debit (trial generation is free even across the tool-loop).
    assert await _trial_used(db_sessionmaker, uid) is True
    assert await _debit_count(db_sessionmaker, uid) == 0


# ============================================================================
# §2a continuation failure: tool-loop continuation hits UpstreamError → claim rolled back
# ============================================================================
@pytest.mark.asyncio
async def test_trial_tool_loop_continuation_failure_rolls_back_claim(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=False)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.write", _WRITE, tool_id="toolu_01TrialB"),
    ]
    run = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "build", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert run.status_code == 200, run.text
    sid = run.json()["sessionId"]
    tool_call_id = run.json()["toolCall"]["id"]
    assert await _trial_used(db_sessionmaker, uid) is True  # claimed mid-turn

    # Make the continuation generation fail at the upstream boundary.
    fake_anthropic.raise_upstream = True
    tr = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "toolCallId": tool_call_id,
            "result": {"ok": 1},
        },
        headers=auth_headers(uid),
    )
    assert tr.status_code == 502, tr.text
    # §2a reconcile: the in-flight trial turn failed on continuation → claim rolled back to FALSE.
    assert await _trial_used(db_sessionmaker, uid) is False
