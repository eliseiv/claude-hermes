"""Integration: wake readiness-gate on REAL Postgres (ADR-062 §1/§1a/§1b).

ADR-062 distributes the ADR-056 readiness-gate onto the wake path (``stopped → running``). The
short-transaction arbiter (commit the ``provisioning`` row BEFORE polling health, release the
row-lock), the identity-guarded conditional re-hibernate (``mark_stopped_if_provisioning``), and the
``provisioning_started_at`` stale-anchor all depend on REAL row/transaction semantics, so these run
against the shared testcontainers Postgres (registry SQL is exercised for real). Docker is a fake
(no socket); KMS is the real LocalKmsClient (ADR-003).

Covered (task ADR-062 wake-gate):
- W1 happy path: start → mark_provisioning(commit arbiter) → health 200 → mark_running (STRICTLY
  after 200); endpoint returned; provisioning_started_at re-stamped; container reused.
- W2 readiness timeout, rowcount=1: mark_stopped_if_provisioning returns 1 → backend.stop + 502;
  container NOT removed (a valid hibernated instance, unlike provision-cleanup which removes).
- W3 readiness timeout, rowcount=0: a concurrent actor took the row (new running, different anchor)
  → row+container untouched → rollback → 502 (anti-clobber of a healthy concurrent running).
- W4 CRITICAL stale-anchor: a woken `provisioning` row with an OLD created_at but a FRESH
  provisioning_started_at is NOT stale → a concurrent ensure_running goes to _await_concurrent_ready
  (no replay: no remove/provision/delete). Contrast: an OLD anchor IS replayed.
- W5 transactionality: the commit after mark_provisioning releases the row-lock BEFORE the poll, so
  a second same-user get_for_update is NOT blocked for the whole ready budget.
- create_provisioning stamps provisioning_started_at = now(); cold-start stale-replay (TD-031)
  still correct when anchored on provisioning_started_at.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.byok.kms import LocalKmsClient
from app.config import Settings
from app.errors import UpstreamError
from app.hermes_runtime.docker_backend import HERMES_API_PORT, ContainerRef, ProvisionSpec
from app.hermes_runtime.manager import HermesInstanceManager
from app.hermes_runtime.registry import HermesInstanceRegistry

_MASTER_KEY = bytes(range(32))


def _settings(ready_timeout: int = 4, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "HERMES_IMAGE": "hermes:test-1.0",
        "HERMES_LLM_API_KEY": "service-llm-key",
        "HERMES_LLM_PROVIDER": "anthropic",
        "HERMES_MODEL": "claude-sonnet-4-5",
        "HERMES_API_KEY_BYTES": 32,
        # Small readiness budget so timeout paths do not sleep the 90s default (fake clock also
        # applied on the timeout tests). stale (120) stays > ready (ADR-056 §3 invariant).
        "HERMES_PROVISION_READY_TIMEOUT_SECONDS": ready_timeout,
        "HERMES_PROVISION_READY_INTERVAL_SECONDS": 1,
        "HERMES_PROVISIONING_STALE_SECONDS": 120,
    }
    base.update(overrides)
    return Settings(**base)


class _Backend:
    """RuntimeBackend fake for the wake path. Records lifecycle calls; drives health.

    ``health_results`` is a per-call list (bool); once exhausted, ``health_default`` is returned.
    ``on_health`` (async) is invoked with the 0-based call index BEFORE the result — used to assert
    ordering (row still `provisioning` at poll time) or to inject a concurrent takeover.
    ``stop_fail`` makes ``stop`` raise (cleanup must survive it).
    """

    def __init__(self) -> None:
        self.provision_calls: list[ProvisionSpec] = []
        self.start_calls: list[ContainerRef] = []
        self.stop_calls: list[ContainerRef] = []
        self.remove_calls: list[ContainerRef] = []
        self.health_calls = 0
        self.health_results: list[bool] | None = None
        self.health_default = True
        self.on_health: Any = None
        self.stop_fail = False
        self._n = 0

    async def provision(self, spec: ProvisionSpec) -> ContainerRef:
        self.provision_calls.append(spec)
        self._n += 1
        return ContainerRef(
            container_id=f"cid-{self._n}",
            name=spec.name,
            endpoint=f"http://{spec.name}:{HERMES_API_PORT}",
        )

    async def start(self, container_ref: ContainerRef) -> None:
        self.start_calls.append(container_ref)

    async def stop(self, container_ref: ContainerRef) -> None:
        self.stop_calls.append(container_ref)
        if self.stop_fail:
            raise UpstreamError("stop failed")

    async def remove(self, container_ref: ContainerRef) -> None:
        self.remove_calls.append(container_ref)

    async def health(self, endpoint: str, api_key: str) -> bool:
        idx = self.health_calls
        self.health_calls += 1
        if self.on_health is not None:
            await self.on_health(idx)
        if self.health_results is not None and idx < len(self.health_results):
            return self.health_results[idx]
        return self.health_default


def _manager(session: AsyncSession, backend: Any, settings: Settings | None = None) -> Any:
    return HermesInstanceManager(
        session=session,
        registry=HermesInstanceRegistry(session),
        backend=backend,
        kms=LocalKmsClient(_MASTER_KEY),
        settings=settings or _settings(),
    )


async def _seed_user(session: AsyncSession, uid: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO users (id, trial_used) VALUES (:id, false)"), {"id": str(uid)}
    )
    await session.commit()


async def _seed_stopped_instance(
    db_sessionmaker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> None:
    """Provision (ready backend) then hibernate → a `stopped` row with real encrypted key material.

    Uses the manager's own provision path so api_key_enc/dek/nonce decrypt correctly on wake; then
    registry.mark_stopped flips it to `stopped` (keeping container_id/endpoint) — the wake state.
    """
    ready = _Backend()  # health_default True → provision readiness-gate passes on the first poll
    async with db_sessionmaker() as s:
        await _seed_user(s, uid)
        mgr = _manager(s, ready)
        await mgr.ensure_running(uid)
        await s.commit()
        await HermesInstanceRegistry(s).mark_stopped(uid)
        await s.commit()


@pytest.fixture
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Virtual monotonic clock for the readiness-timeout paths (no real wall-clock wait).

    ``asyncio.sleep`` advances the virtual clock; ``time.monotonic`` reads it. Applied only where a
    poll loop would otherwise burn the real budget; the concurrency test (W5) must NOT use it.
    """
    import app.hermes_runtime.manager as manager_mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(manager_mod.time, "monotonic", lambda: clock["now"])

    async def _sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(manager_mod.asyncio, "sleep", _sleep)


# ============================================================================
# W1 — wake happy path: gated wake, mark_running STRICTLY after health 200
# ============================================================================
async def test_wake_happy_path_marks_running_only_after_health_200(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)

    backend = _Backend()
    backend.health_results = [True]  # ready on the first poll
    statuses_at_poll: list[str] = []

    async def _on_health(_idx: int) -> None:
        # At poll time the provisioning arbiter is already committed but mark_running has NOT fired.
        async with db_sessionmaker() as probe:
            row = await HermesInstanceRegistry(probe).get(uid)
            assert row is not None
            statuses_at_poll.append(row.status)

    backend.on_health = _on_health

    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        ep = await mgr.ensure_running(uid)
        await s.commit()

    # Woken (started), NOT re-provisioned.
    assert len(backend.start_calls) == 1
    assert len(backend.provision_calls) == 0
    assert ep.base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"
    # Ordering: row was `provisioning` at probe time → mark_running is strictly after health 200.
    assert statuses_at_poll == ["provisioning"]

    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None
        assert row.status == "running"
        assert row.endpoint == f"http://hermes-user-{uid}:{HERMES_API_PORT}"  # container reused
        assert row.provisioning_started_at is not None  # anchor re-stamped for THIS wake attempt


# ============================================================================
# W2 — readiness timeout, rowcount=1: re-hibernate (stop, NO remove) + 502
# ============================================================================
async def test_wake_timeout_rowcount1_rehibernates_stops_no_remove_raises(
    _fake_clock: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)

    backend = _Backend()
    backend.health_default = False  # never ready → readiness timeout

    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        with pytest.raises(UpstreamError):
            await mgr.ensure_running(uid)
        await s.commit()

    # We still owned the attempt (anchor unchanged) → honest re-hibernate: stop the container, but
    # do NOT remove it (a valid hibernated instance — volume/memory kept; unlike provision-cleanup).
    assert len(backend.start_calls) == 1
    assert len(backend.stop_calls) == 1
    assert len(backend.remove_calls) == 0
    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None
        assert row.status == "stopped"  # honest re-hibernate committed


async def test_wake_timeout_rowcount1_survives_stop_failure(
    _fake_clock: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A backend.stop failure during re-hibernate must not mask the readiness timeout — still 502,
    # the row is still flipped to stopped (the mark_stopped_if_provisioning UPDATE ran before stop).
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)

    backend = _Backend()
    backend.health_default = False
    backend.stop_fail = True

    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        with pytest.raises(UpstreamError):
            await mgr.ensure_running(uid)
        await s.commit()

    assert len(backend.stop_calls) == 1  # stop attempted
    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None and row.status == "stopped"


# ============================================================================
# W3 — readiness timeout, rowcount=0: concurrent takeover → row/container UNTOUCHED + 502
# ============================================================================
async def test_wake_timeout_rowcount0_concurrent_takeover_untouched_raises(
    _fake_clock: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)

    backend = _Backend()
    backend.health_default = False  # our poller never sees ready
    takeover_endpoint = "http://concurrent-takeover:8642"

    async def _on_health(idx: int) -> None:
        # On the FIRST poll a concurrent replay/provision promotes a NEW running container with a
        # DIFFERENT provisioning_started_at (committed in its own session). Our later
        # mark_stopped_if_provisioning(user, T_orig) then matches 0 rows (running now, anchor T').
        if idx == 0:
            async with db_sessionmaker() as other:
                await other.execute(
                    text(
                        "UPDATE hermes_instances SET status='running', endpoint=:ep, "
                        "provisioning_started_at = now() + interval '1 second' WHERE user_id=:uid"
                    ),
                    {"ep": takeover_endpoint, "uid": str(uid)},
                )
                await other.commit()

    backend.on_health = _on_health

    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        with pytest.raises(UpstreamError):
            await mgr.ensure_running(uid)
        await s.commit()

    # Anti-clobber (§1b MAJOR): the healthy concurrent `running` row is NOT reverted to stopped, and
    # our wake does NOT stop/remove the concurrent container.
    assert len(backend.stop_calls) == 0
    assert len(backend.remove_calls) == 0
    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None
        assert row.status == "running"  # concurrent running preserved
        assert row.endpoint == takeover_endpoint  # untouched by the timed-out wake


# ============================================================================
# W4 — CRITICAL stale-anchor: fresh provisioning_started_at over old created_at → NOT stale
# ============================================================================
async def test_wake_fresh_anchor_over_old_created_at_not_stale_awaits_not_replay(
    _fake_clock: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A woken instance: created_at DAYS old (original provision) but provisioning_started_at FRESH
    # (this wake attempt). A concurrent ensure_running must NOT treat it as stale — it goes to
    # _await_concurrent_ready (no remove/provision/delete) and, since the row never reaches running
    # here, times out → 502. Had it anchored on created_at (pre-ADR-062), it would have replayed
    # (remove + provision) and SUCCEEDED — the absence of those calls proves the anchor is correct.
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)
    async with db_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE hermes_instances SET status='provisioning', "
                "created_at = now() - interval '2 days', provisioning_started_at = now() "
                "WHERE user_id=:uid"
            ),
            {"uid": str(uid)},
        )
        await s.commit()

    backend = _Backend()
    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        with pytest.raises(
            UpstreamError
        ):  # await_concurrent_ready times out (row stays provisioning)
            await mgr.ensure_running(uid)
        await s.commit()

    # NOT stale → concurrent-wait branch, NOT replay:
    assert len(backend.remove_calls) == 0  # the in-flight container is NOT torn down
    assert len(backend.provision_calls) == 0  # NOT re-provisioned from scratch
    assert len(backend.start_calls) == 0  # await branch does not start
    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert (
            row is not None and row.status == "provisioning"
        )  # row untouched (not deleted/replayed)


async def test_wake_old_anchor_is_stale_replays(
    _fake_clock: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Contrast to W4: the SAME old created_at but an OLD provisioning_started_at (beyond stale) IS a
    # genuine crash residue → replayed (remove the half-created container + provision afresh).
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)
    async with db_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE hermes_instances SET status='provisioning', "
                "created_at = now() - interval '2 days', "
                "provisioning_started_at = now() - interval '2 days' WHERE user_id=:uid"
            ),
            {"uid": str(uid)},
        )
        await s.commit()

    backend = _Backend()
    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        ep = await mgr.ensure_running(uid)  # stale → replay → fresh provision succeeds
        await s.commit()

    assert len(backend.remove_calls) == 1  # half-created container torn down
    assert len(backend.provision_calls) == 1  # provisioned afresh
    assert ep.base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"


# ============================================================================
# W5 — transactionality: commit after mark_provisioning releases the row-lock BEFORE the poll
# ============================================================================
async def test_wake_commits_arbiter_before_poll_second_request_not_blocked(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = uuid.uuid4()
    await _seed_stopped_instance(db_sessionmaker, uid)

    backend = _Backend()
    backend.health_results = [True]
    provisioned = asyncio.Event()
    release = asyncio.Event()

    async def _on_health(_idx: int) -> None:
        # Reached only AFTER mark_provisioning + commit (the arbiter). Hold here so we can prove a
        # concurrent get_for_update is not blocked while this wake is mid-poll.
        provisioned.set()
        await release.wait()

    backend.on_health = _on_health

    async def _wake() -> str:
        async with db_sessionmaker() as s:
            mgr = _manager(s, backend)
            ep = await mgr.ensure_running(uid)
            await s.commit()
            return ep.base_url

    task = asyncio.create_task(_wake())
    try:
        await asyncio.wait_for(provisioned.wait(), timeout=10)
        # The wake has committed the `provisioning` arbiter and is now polling health WITHOUT the
        # row-lock. A second same-user FOR UPDATE must return promptly (NOT block the ready budget).
        async with db_sessionmaker() as s2:
            row = await asyncio.wait_for(HermesInstanceRegistry(s2).get_for_update(uid), timeout=5)
            assert row is not None
            assert row.status == "provisioning"  # observes the committed arbiter
            await s2.commit()
    finally:
        release.set()

    base_url = await asyncio.wait_for(task, timeout=10)
    assert base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"
    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None and row.status == "running"


# ============================================================================
# create_provisioning stamps the anchor = now(); cold-start stale-replay still correct
# ============================================================================
async def test_create_provisioning_stamps_provisioning_started_at_now(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    uid = uuid.uuid4()
    async with db_sessionmaker() as s:
        await _seed_user(s, uid)
        reg = HermesInstanceRegistry(s)
        row = await reg.create_provisioning(uid, api_key_enc=b"e", encrypted_dek=b"d", nonce=b"n")
        await s.commit()
        assert row is not None

    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None
        assert row.provisioning_started_at is not None  # cold-start stamped the anchor
        # It is a recent timestamp (within the last minute), i.e. now() at insert.
        now = datetime.datetime.now(datetime.UTC)
        assert (now - row.provisioning_started_at) < datetime.timedelta(minutes=1)


async def test_cold_start_stale_replay_anchored_on_provisioning_started_at(
    _fake_clock: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A cold-start crash residue: a `provisioning` row whose provisioning_started_at is older than
    # the stale threshold IS replayed (TD-031 preserved, now anchored on provisioning_started_at).
    uid = uuid.uuid4()
    async with db_sessionmaker() as s:
        await _seed_user(s, uid)
        reg = HermesInstanceRegistry(s)
        await reg.create_provisioning(uid, api_key_enc=b"e", encrypted_dek=b"d", nonce=b"n")
        await s.commit()
        # Backdate the anchor beyond the stale threshold (created_at stays recent → proves anchor
        # is provisioning_started_at, not created_at).
        await s.execute(
            text(
                "UPDATE hermes_instances "
                "SET provisioning_started_at = now() - interval '10 minutes' WHERE user_id=:uid"
            ),
            {"uid": str(uid)},
        )
        await s.commit()

    backend = _Backend()
    async with db_sessionmaker() as s:
        mgr = _manager(s, backend)
        ep = await mgr.ensure_running(uid)  # stale (by anchor) → replay → fresh provision
        await s.commit()

    assert len(backend.provision_calls) == 1
    assert ep.base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"
    async with db_sessionmaker() as s:
        row = await HermesInstanceRegistry(s).get(uid)
        assert row is not None and row.status == "running"
