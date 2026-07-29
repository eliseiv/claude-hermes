"""Integration: the row-lock half of TD-040, against a REAL PostgreSQL.

Why these three cases cannot be unit tests. The defect they cover is not a branch — it is what
Postgres does with a lock that is held too long, and what ``SET LOCAL lock_timeout`` does to
statements that follow it in the same transaction. An in-memory registry double has no locks at all,
so it can only record that a number was passed; whether that number actually bounds the wait, and
whether it leaks onto the NEXT statement, is a property of the database.

The user-visible symptom this closes: one wedged instance did not merely hang its own request, it
hung EVERY subsequent request of the same user, because the ``running`` fast path held its
``SELECT … FOR UPDATE`` across the caller's whole HTTP call.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.byok.kms import LocalKmsClient
from app.config import Settings
from app.hermes_runtime.manager import HermesInstanceManager, _is_lock_timeout
from app.hermes_runtime.registry import HermesInstanceRegistry
from tests.conftest import seed_user

# Loose on purpose. What these tests prove is BOUNDED vs FOREVER, and the configured bound is a
# fraction of a second; anything under this ceiling is a pass, so a loaded CI box cannot turn a
# correct lock timeout into a red build. A tighter number measures the machine, not the code.
_UNBOUNDED_WAIT_SANITY_SECONDS = 30.0
_MASTER_KEY = bytes(range(32))
_API_KEY = "instance-bearer-key-for-td040"


def _kms() -> LocalKmsClient:
    return LocalKmsClient(_MASTER_KEY)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        HERMES_IMAGE="hermes:test",
        HERMES_LLM_API_KEY="k",
        HERMES_LLM_PROVIDER="anthropic",
        HERMES_MODEL="claude-sonnet-4-5",
    )


def _manager_for(session: AsyncSession) -> HermesInstanceManager:
    """The real manager on a real session; the backend is never reached on the fast path."""
    return HermesInstanceManager(
        session=session,
        registry=HermesInstanceRegistry(session),
        backend=None,  # type: ignore[arg-type]
        kms=_kms(),
        settings=_settings(),
    )


def _encrypted_key() -> tuple[bytes, bytes, bytes]:
    """Envelope-encrypt _API_KEY the same way the manager does (ADR-003)."""
    kms = _kms()
    dek = os.urandom(32)
    nonce = os.urandom(12)
    api_key_enc = AESGCM(dek).encrypt(nonce, _API_KEY.encode(), None)
    return api_key_enc, kms.encrypt_dek(dek), nonce


async def _seed_instance(
    maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, *, status: str = "running"
) -> None:
    """Insert a hermes_instances row directly (provisioning it for real needs Docker)."""
    async with maker() as session:
        await session.execute(
            text(
                "INSERT INTO hermes_instances (user_id, status, container_id, endpoint, port, "
                "api_key_enc, encrypted_dek, nonce) VALUES (:u, CAST(:s AS hermes_instance_status),"
                " 'c1', 'http://hermes-user:8642', 8642, :k, :d, :n)"
            ),
            dict(zip(("k", "d", "n"), _encrypted_key(), strict=True), u=str(user_id), s=status),
        )
        await session.commit()


@pytest.mark.asyncio
async def test_lock_timeout_actually_bounds_the_wait_and_raises_55p03(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Case 4, for real: a second locker gives up on Postgres' clock, not on ours.

    The unit test can only assert that a millisecond value was handed to the registry. This asserts
    the thing that matters — the wait ENDS, with the SQLSTATE the manager classifies on.
    """
    async with db_sessionmaker() as seed:
        user_id = await seed_user(seed, subscription="active", balance=100)
    await _seed_instance(db_sessionmaker, user_id)

    async with db_sessionmaker() as holder, db_sessionmaker() as waiter:
        # Holder takes the row lock and keeps the transaction open.
        await HermesInstanceRegistry(holder).get_for_update(user_id)

        started = time.monotonic()
        with pytest.raises(DBAPIError) as excinfo:
            await HermesInstanceRegistry(waiter).get_for_update(user_id, lock_timeout_ms=300)
        elapsed = time.monotonic() - started
        await waiter.rollback()
        await holder.rollback()

    assert _is_lock_timeout(excinfo.value) is True
    assert elapsed < _UNBOUNDED_WAIT_SANITY_SECONDS, f"the bounded lock wait took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_lock_timeout_does_not_leak_onto_later_statements_of_the_same_transaction(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Case 13: ``SET LOCAL lock_timeout`` is restored to DEFAULT after the FOR UPDATE.

    Otherwise the tiny per-request lock budget would silently govern ``create_provisioning`` — a
    different statement with a different risk profile — in the SAME transaction, and a cold start
    under a nearly-spent budget would fail on a lock wait nobody asked to bound.
    """
    async with db_sessionmaker() as seed:
        user_id = await seed_user(seed, subscription="active", balance=100)
    await _seed_instance(db_sessionmaker, user_id)

    async with db_sessionmaker() as session:
        registry = HermesInstanceRegistry(session)
        await registry.get_for_update(user_id, lock_timeout_ms=250)
        # The setting must be back to the session default for whatever runs next.
        current = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
        await session.rollback()

    assert current in ("0", "0ms"), f"lock_timeout leaked to a later statement: {current!r}"


@pytest.mark.asyncio
async def test_the_running_fast_path_releases_the_lock_before_the_http_call(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Case 3: the whole point of TD-040's second half — driven through the REAL manager.

    Two sessions, as two concurrent requests of ONE user. The first goes through
    ``ensure_running``'s ``running`` fast path; the second then tries to take the same row while the
    first request is still "in flight" upstream. It must acquire it immediately.

    Deliberately exercises ``HermesInstanceManager.ensure_running`` rather than hand-rolling the
    commit: the property under test is that THAT method releases the lock, so a version which only
    flushes (the pre-fix code) has to fail this test. Docker is never touched — the fast path does
    not call the backend at all.
    """
    async with db_sessionmaker() as seed:
        user_id = await seed_user(seed, subscription="active", balance=100)
    await _seed_instance(db_sessionmaker, user_id)

    async with db_sessionmaker() as first:
        manager = _manager_for(first)
        endpoint = await manager.ensure_running(user_id, deadline=time.monotonic() + 30)
        assert endpoint.api_key == _API_KEY, "the fast path must still decrypt the instance key"

        # The first request is now waiting on a wedged instance; its session stays open, exactly as
        # it would while the HTTP call is in flight.
        async def _wedged_upstream_call() -> None:
            await asyncio.sleep(1.0)

        upstream = asyncio.create_task(_wedged_upstream_call())

        # A second request of the SAME user arrives inside that window.
        async with db_sessionmaker() as second:
            started = time.monotonic()
            second_row = await HermesInstanceRegistry(second).get_for_update(
                user_id, lock_timeout_ms=2_000
            )
            elapsed = time.monotonic() - started
            await second.rollback()

        await upstream

    assert second_row is not None, "the second request could not read the row at all"
    assert elapsed < 0.5, (
        f"the second request waited {elapsed:.2f}s on the row lock — the fast path is holding it "
        "across the upstream call, which is the TD-040 pile-up"
    )


@pytest.mark.asyncio
async def test_a_held_lock_still_bounds_a_second_request_at_its_budget(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The complement: when a lock IS legitimately held (a cold start), the queued request still
    gets an answer instead of waiting forever, and it is the answer the manager classifies on.

    Postgres defaults ``lock_timeout`` to 0 = wait forever and this project sets no global one, so
    without the per-request value the second caller blocks for as long as the first holds the row.
    The proof of boundedness is therefore that the statement RAISES at all — the wall-clock bound
    below is only a sanity net against a pathological wait and is deliberately loose: a tight one
    measures the load on the box, not the behaviour of the lock (it flaked at 3s inside the full
    suite while passing standalone).
    """
    async with db_sessionmaker() as seed:
        user_id = await seed_user(seed, subscription="active", balance=100)
    await _seed_instance(db_sessionmaker, user_id, status="provisioning")

    async with db_sessionmaker() as holder, db_sessionmaker() as waiter:
        await HermesInstanceRegistry(holder).get_for_update(user_id)

        started = time.monotonic()
        with pytest.raises(DBAPIError) as excinfo:
            await HermesInstanceRegistry(waiter).get_for_update(user_id, lock_timeout_ms=200)
        elapsed = time.monotonic() - started
        await waiter.rollback()
        await holder.rollback()

    # The manager maps exactly this error to the client-visible upstream_timeout.
    assert (
        _is_lock_timeout(excinfo.value) is True
    ), "the real 55P03 must be what the manager classifies as upstream_timeout"
    assert elapsed < _UNBOUNDED_WAIT_SANITY_SECONDS, (
        f"the bounded lock wait took {elapsed:.2f}s — a lock_timeout that does not bind is "
        "indistinguishable from the unbounded wait this closes"
    )
