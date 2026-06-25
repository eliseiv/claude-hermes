"""Unit: stale-provisioning replay in ensure_running (TD-031).

A crash between create_provisioning and mark_running leaves a `provisioning` row with endpoint=NULL
(→ DNS fallback to a container that may not exist → a clean 502). ADR/TD-031: ensure_running treats
a `provisioning` row OLDER than HERMES_PROVISIONING_STALE_SECONDS as stale and replays the full
lifecycle (deprovision the half-created container + drop the row, then provision afresh) under the
held row-lock. A FRESH provisioning row (younger than the threshold) is left as a live concurrent
provisioning (touch only — unchanged). Threshold boundary is exercised.

Docker is faked at the RuntimeBackend boundary; KMS is the real LocalKmsClient (round-trip).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.byok.kms import LocalKmsClient
from app.config import Settings
from app.hermes_runtime.docker_backend import HERMES_API_PORT, ContainerRef, ProvisionSpec
from app.hermes_runtime.manager import HermesInstanceManager

_MASTER_KEY = bytes(range(32))


def _kms() -> LocalKmsClient:
    return LocalKmsClient(_MASTER_KEY)


def _settings(stale_seconds: int = 120, ready_seconds: int = 1, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "HERMES_IMAGE": "hermes:test-1.0",
        "HERMES_LLM_API_KEY": "service-llm-key-xyz",
        "HERMES_LLM_PROVIDER": "anthropic",
        "HERMES_MODEL": "claude-sonnet-4-5",
        "HERMES_DOCKER_NETWORK": "hermes-net",
        "HERMES_VOLUME_ROOT": "/opt/data/hermes",
        "HERMES_API_KEY_BYTES": 32,
        "HERMES_PROVISIONING_STALE_SECONDS": stale_seconds,
        # ADR-056 §3 config invariant: stale MUST be > ready. Keep ready tiny so small stale values
        # used by the boundary tests (e.g. 60) still satisfy stale > ready and construct cleanly,
        # and so the readiness poll on the replay path does not sleep a long budget.
        "HERMES_PROVISION_READY_TIMEOUT_SECONDS": ready_seconds,
        "HERMES_PROVISION_READY_INTERVAL_SECONDS": 1,
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class _Row:
    user_id: uuid.UUID
    api_key_enc: bytes
    encrypted_dek: bytes
    nonce: bytes
    status: str = "provisioning"
    container_id: str | None = None
    endpoint: str | None = None
    port: int | None = None
    created_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    last_active_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


class FakeRegistry:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _Row] = {}
        self.create_calls = 0
        self.touch_calls = 0
        self.delete_calls = 0
        self.mark_running_calls = 0

    async def get(self, user_id: uuid.UUID) -> _Row | None:
        return self.rows.get(user_id)

    async def get_for_update(self, user_id: uuid.UUID) -> _Row | None:
        return self.rows.get(user_id)

    async def create_provisioning(
        self, user_id: uuid.UUID, *, api_key_enc: bytes, encrypted_dek: bytes, nonce: bytes
    ) -> _Row | None:
        self.create_calls += 1
        if user_id in self.rows:
            return None
        row = _Row(
            user_id=user_id,
            api_key_enc=api_key_enc,
            encrypted_dek=encrypted_dek,
            nonce=nonce,
            status="provisioning",
        )
        self.rows[user_id] = row
        return row

    async def mark_running(
        self, user_id: uuid.UUID, *, container_id: str, endpoint: str, port: int | None = None
    ) -> None:
        self.mark_running_calls += 1
        row = self.rows[user_id]
        row.container_id = container_id
        row.endpoint = endpoint
        row.port = port
        row.status = "running"

    async def touch_active(self, user_id: uuid.UUID) -> None:
        self.touch_calls += 1

    async def delete(self, user_id: uuid.UUID) -> None:
        self.delete_calls += 1
        self.rows.pop(user_id, None)


class FakeRuntimeBackend:
    def __init__(self) -> None:
        self.provision_calls: list[ProvisionSpec] = []
        self.remove_calls: list[ContainerRef] = []
        self._counter = 0

    async def provision(self, spec: ProvisionSpec) -> ContainerRef:
        self.provision_calls.append(spec)
        self._counter += 1
        return ContainerRef(
            container_id=f"cid-{self._counter}",
            name=spec.name,
            endpoint=f"http://{spec.name}:{HERMES_API_PORT}",
        )

    async def start(self, container_ref: ContainerRef) -> None:  # pragma: no cover - unused here
        return None

    async def stop(self, container_ref: ContainerRef) -> None:  # pragma: no cover - unused here
        return None

    async def remove(self, container_ref: ContainerRef) -> None:
        self.remove_calls.append(container_ref)

    async def health(self, endpoint: str, api_key: str) -> bool:  # pragma: no cover - unused here
        return True


class _FakeSession:
    """AsyncSession double. ADR-056 added commit (provisioning row before docker run; mark_running
    later) and rollback (release the row-lock before a concurrent-wait) alongside the legacy flush.
    """

    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    def expire_all(self) -> None:
        # SQLAlchemy Session.expire_all is SYNC; no-op on the in-memory FakeRegistry (no
        # identity-map cache to drop). ADR-056 concurrent-loser staleness is covered by integration.
        return None


def _manager(reg: FakeRegistry, backend: FakeRuntimeBackend, settings: Settings) -> Any:
    return HermesInstanceManager(
        session=_FakeSession(),  # type: ignore[arg-type]
        registry=reg,  # type: ignore[arg-type]
        backend=backend,
        kms=_kms(),
        settings=settings,
    )


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Virtual monotonic clock so concurrent-wait polls (ADR-056 §2) terminate instantly.

    ``asyncio.sleep`` advances the virtual clock by the requested interval; ``time.monotonic`` reads
    it. The budget arithmetic in the manager is exercised exactly, with no real wall-clock latency.
    """
    import app.hermes_runtime.manager as manager_mod

    clock = {"now": 1000.0}

    monkeypatch.setattr(manager_mod.time, "monotonic", lambda: clock["now"])

    async def _sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(manager_mod.asyncio, "sleep", _sleep)


def _seed_provisioning(
    reg: FakeRegistry, mgr: Any, *, age_seconds: float, container_id: str | None
) -> uuid.UUID:
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("seed-key-abcdef0123")
    reg.rows[uid] = _Row(
        user_id=uid,
        api_key_enc=enc,
        encrypted_dek=dek,
        nonce=nonce,
        status="provisioning",
        container_id=container_id,
        endpoint=None,  # crash residue: endpoint never set
        created_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age_seconds),
    )
    return uid


# ============================================================================
# Stale provisioning (older than threshold) → deprovision half-created container + fresh provision
# ============================================================================
@pytest.mark.asyncio
async def test_stale_provisioning_with_container_replays_deprovision_then_provision() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(stale_seconds=120))
    uid = _seed_provisioning(reg, mgr, age_seconds=300, container_id="cid-halfbaked")

    ep = await mgr.ensure_running(uid)

    # Half-created container removed, then a fresh provision.
    assert len(backend.remove_calls) == 1
    assert backend.remove_calls[0].container_id == "cid-halfbaked"
    assert len(backend.provision_calls) == 1
    assert reg.rows[uid].status == "running"
    assert reg.rows[uid].endpoint is not None  # endpoint now resolved (no DNS-NULL fallback)
    assert ep.base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"
    assert reg.touch_calls == 0  # NOT treated as a live provisioning


@pytest.mark.asyncio
async def test_stale_provisioning_without_container_replays_provision_no_remove() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(stale_seconds=120))
    uid = _seed_provisioning(reg, mgr, age_seconds=300, container_id=None)

    await mgr.ensure_running(uid)

    # No container to remove (crash before provision), but a fresh provision happens.
    assert len(backend.remove_calls) == 0
    assert len(backend.provision_calls) == 1
    assert reg.rows[uid].status == "running"


# ============================================================================
# Fresh provisioning (younger than threshold) → live concurrent provisioning (ADR-056 §2)
# ============================================================================
# masking-regression: the OLD contract treated a fresh provisioning row like running (touch-only,
# touch_calls==1). ADR-056 §2 changed this — a fresh row is a concurrent provisioner in its
# readiness-poll; ensure_running releases the row-lock (rollback) and WAITS for `running`. The old
# `..._touch_only` assertion would now pass for the wrong reason, so it is replaced below.
@pytest.mark.asyncio
async def test_fresh_provisioning_not_replayed_waits_for_concurrent() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(stale_seconds=120))
    uid = _seed_provisioning(reg, mgr, age_seconds=10, container_id=None)

    # Simulate the concurrent provisioner finishing: flip the row to running on the first wait-read.
    original_get = reg.get

    async def _get_then_running(u: uuid.UUID) -> _Row | None:
        row = await original_get(u)
        if row is not None and row.status == "provisioning":
            row.status = "running"
            row.endpoint = f"http://hermes-user-{u}:{HERMES_API_PORT}"
        return row

    reg.get = _get_then_running  # type: ignore[method-assign]

    await mgr.ensure_running(uid)

    # Younger than the threshold → NOT replayed (no provision, no remove). No touch (not running).
    assert len(backend.provision_calls) == 0
    assert len(backend.remove_calls) == 0
    assert reg.touch_calls == 0
    assert reg.rows[uid].status == "running"  # the concurrent provisioner finished
    assert mgr._session.rollback_calls >= 1  # type: ignore[attr-defined]  # row-lock released


# ============================================================================
# Threshold boundary: just-over → replayed; just-under → not replayed
# ============================================================================
@pytest.mark.asyncio
async def test_threshold_boundary_just_over_replays() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(stale_seconds=60))
    uid = _seed_provisioning(reg, mgr, age_seconds=61, container_id=None)
    await mgr.ensure_running(uid)
    assert len(backend.provision_calls) == 1  # age > threshold → stale → replay


@pytest.mark.asyncio
async def test_threshold_boundary_just_under_not_replayed() -> None:
    # age < threshold → fresh → NOT replayed (no provision). ADR-056 §2: waits for concurrent ready
    # rather than touching; here the concurrent provisioner is simulated to finish on first read.
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(stale_seconds=600, ready_seconds=1))
    uid = _seed_provisioning(reg, mgr, age_seconds=30, container_id=None)

    original_get = reg.get

    async def _get_then_running(u: uuid.UUID) -> _Row | None:
        row = await original_get(u)
        if row is not None and row.status == "provisioning":
            row.status = "running"
            row.endpoint = f"http://hermes-user-{u}:{HERMES_API_PORT}"
        return row

    reg.get = _get_then_running  # type: ignore[method-assign]

    await mgr.ensure_running(uid)
    assert len(backend.provision_calls) == 0  # not replayed
    assert reg.touch_calls == 0  # fresh provisioning is no longer touch-only (ADR-056 §2)


@pytest.mark.asyncio
async def test_threshold_non_positive_disables_replay() -> None:
    # A non-positive stale threshold disables the stale replay (_is_stale_provisioning → False, any
    # provisioning row treated as fresh). ADR-056 §3 forbids stale<=ready at Settings construction,
    # so a non-positive value is no longer settable via config; we set it on the manager's settings
    # AFTER construction to exercise the (still-present) disable branch in isolation. The row
    # is treated as a FRESH concurrent provisioning (waits, no replay), not touched.
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    settings = _settings(stale_seconds=120)
    settings.hermes_provisioning_stale_seconds = (
        0  # post-construction override (validator bypassed)
    )
    mgr = _manager(reg, backend, settings)
    uid = _seed_provisioning(reg, mgr, age_seconds=9999, container_id=None)

    original_get = reg.get

    async def _get_then_running(u: uuid.UUID) -> _Row | None:
        row = await original_get(u)
        if row is not None and row.status == "provisioning":
            row.status = "running"
            row.endpoint = f"http://hermes-user-{u}:{HERMES_API_PORT}"
        return row

    reg.get = _get_then_running  # type: ignore[method-assign]

    await mgr.ensure_running(uid)
    assert len(backend.provision_calls) == 0  # disabled replay → no fresh provision
    assert len(backend.remove_calls) == 0  # not treated as stale → no teardown
    assert reg.touch_calls == 0
