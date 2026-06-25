"""Integration: hermes_instances registry race-safety + manager flow on REAL Postgres (ADR-046).

The PK + ``ON CONFLICT DO NOTHING`` + ``SELECT ... FOR UPDATE`` guarantees (follow_up #2) only
hold against a real database, so these run on the shared testcontainers Postgres. The DockerBackend
is replaced by an in-process fake (no real socket); KMS is the real LocalKmsClient (ADR-003).

Cleanup: the conftest db_sessionmaker fixture TRUNCATEs ``users RESTART IDENTITY CASCADE`` between
tests; ``hermes_instances.user_id`` FK is ON DELETE CASCADE, so its rows are wiped with the user.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.byok.kms import LocalKmsClient
from app.config import Settings
from app.hermes_runtime.docker_backend import HERMES_API_PORT, ContainerRef, ProvisionSpec
from app.hermes_runtime.manager import HermesInstanceManager
from app.hermes_runtime.registry import HermesInstanceRegistry

_MASTER_KEY = bytes(range(32))


def _settings() -> Settings:
    return Settings(
        HERMES_IMAGE="hermes:test-1.0",
        HERMES_LLM_API_KEY="service-llm-key",
        HERMES_LLM_PROVIDER="anthropic",
        HERMES_MODEL="claude-sonnet-4-5",
        HERMES_API_KEY_BYTES=32,
    )


class _CountingBackend:
    """RuntimeBackend fake that counts provision calls (thread-safe-ish via asyncio single loop)."""

    def __init__(self) -> None:
        self.provision_count = 0
        self._n = 0

    async def provision(self, spec: ProvisionSpec) -> ContainerRef:
        self.provision_count += 1
        self._n += 1
        # Yield control so concurrent ensure_running calls can interleave (stress the lock).
        await asyncio.sleep(0)
        return ContainerRef(
            container_id=f"cid-{self._n}",
            name=spec.name,
            endpoint=f"http://{spec.name}:{HERMES_API_PORT}",
        )

    async def start(self, container_ref: ContainerRef) -> None:
        return None

    async def stop(self, container_ref: ContainerRef) -> None:
        return None

    async def remove(self, container_ref: ContainerRef) -> None:
        return None

    async def health(self, endpoint: str, api_key: str) -> bool:
        return True


async def _seed_user(session: AsyncSession, uid: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
    )
    await session.commit()


def _manager(session: AsyncSession, backend: Any) -> HermesInstanceManager:
    return HermesInstanceManager(
        session=session,
        registry=HermesInstanceRegistry(session),
        backend=backend,
        kms=LocalKmsClient(_MASTER_KEY),
        settings=_settings(),
    )


# ============================ basic CRUD round-trip on real PG ============================
async def test_provision_persists_row_and_round_trips_key(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = uuid.uuid4()
    backend = _CountingBackend()
    async with db_sessionmaker() as session:
        await _seed_user(session, uid)
        mgr = _manager(session, backend)
        ep = await mgr.ensure_running(uid)
        await session.commit()

    # Re-read from a fresh session: status running, encrypted material present, key decrypts back.
    async with db_sessionmaker() as session:
        mgr = _manager(session, backend)
        row = await HermesInstanceRegistry(session).get(uid)
        assert row is not None
        assert row.status == "running"
        assert bytes(row.api_key_enc) and bytes(row.encrypted_dek) and bytes(row.nonce)
        # Plaintext key never stored.
        assert ep.api_key.encode() not in bytes(row.api_key_enc)
        # Decryption restores the exact key handed to the caller.
        assert mgr._decrypt_key(row) == ep.api_key


async def test_stop_idle_selects_only_running_idle_rows(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    old_uid, fresh_uid = uuid.uuid4(), uuid.uuid4()
    backend = _CountingBackend()
    async with db_sessionmaker() as session:
        await _seed_user(session, old_uid)
        await _seed_user(session, fresh_uid)
        mgr = _manager(session, backend)
        await mgr.ensure_running(old_uid)
        await mgr.ensure_running(fresh_uid)
        await session.commit()
        # Backdate old_uid's last_active_at beyond the threshold.
        await session.execute(
            text(
                "UPDATE hermes_instances SET last_active_at = now() - interval '2 hours' "
                "WHERE user_id = :uid"
            ),
            {"uid": str(old_uid)},
        )
        await session.commit()

    async with db_sessionmaker() as session:
        mgr = _manager(session, backend)
        stopped = await mgr.stop_idle(threshold_seconds=600)
        await session.commit()
    assert stopped == 1

    async with db_sessionmaker() as session:
        reg = HermesInstanceRegistry(session)
        assert (await reg.get(old_uid)).status == "stopped"
        assert (await reg.get(fresh_uid)).status == "running"


# ============================ race: concurrent ensure_running (follow_up #2) ============================
async def test_concurrent_ensure_running_provisions_single_container(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent ensure_running for the same user must create exactly ONE container.

    Each call uses its OWN session/transaction (as in production, one session per request). The PK
    + ON CONFLICT DO NOTHING + FOR UPDATE serialize them: only one INSERT wins; the loser observes
    the winner's row and does not call backend.provision a second time.
    """
    uid = uuid.uuid4()
    backend = _CountingBackend()
    async with db_sessionmaker() as session:
        await _seed_user(session, uid)

    async def _call() -> str:
        async with db_sessionmaker() as session:
            mgr = _manager(session, backend)
            ep = await mgr.ensure_running(uid)
            await session.commit()
            return ep.api_key

    keys = await asyncio.gather(_call(), _call())

    # Exactly one container provisioned despite two concurrent callers.
    assert backend.provision_count == 1
    # Exactly one row exists.
    async with db_sessionmaker() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM hermes_instances WHERE user_id = :uid"), {"uid": str(uid)}
        )
    assert count == 1
    # Both callers see the SAME (winner's) key.
    assert keys[0] == keys[1]


# ============================ FK ON DELETE CASCADE (follow_up #11) ============================
async def test_fk_cascade_deletes_instance_on_user_delete(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = uuid.uuid4()
    backend = _CountingBackend()
    async with db_sessionmaker() as session:
        await _seed_user(session, uid)
        mgr = _manager(session, backend)
        await mgr.ensure_running(uid)
        await session.commit()

    async with db_sessionmaker() as session:
        await session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(uid)})
        await session.commit()

    async with db_sessionmaker() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM hermes_instances WHERE user_id = :uid"), {"uid": str(uid)}
        )
    assert count == 0  # CASCADE removed the instance row with the user
