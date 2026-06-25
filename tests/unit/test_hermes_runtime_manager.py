"""Unit: HermesInstanceManager lifecycle + envelope encryption (ADR-046 §1/§4/§5).

Docker is mocked at the ``RuntimeBackend`` boundary (``FakeRuntimeBackend``) — no real socket is
touched (hermes-runtime/09). The DB is a hand-rolled in-memory ``FakeRegistry`` so the manager's
branch logic (missing→provision, stopped→start, running→touch) is exercised deterministically and
fast; the PK/ON CONFLICT/FOR UPDATE race semantics are covered against a REAL Postgres in
tests/integration/test_hermes_instances_registry.py.

KMS is the real ``LocalKmsClient`` (AES-256-GCM master-key wrap, ADR-003) so the encrypt→decrypt
round-trip and "plaintext never persisted" invariants are tested for real, not faked.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.byok.kms import LocalKmsClient
from app.config import Settings
from app.errors import UpstreamError
from app.hermes_runtime.docker_backend import (
    HERMES_API_PORT,
    ContainerRef,
    ProvisionSpec,
)
from app.hermes_runtime.manager import HermesInstanceManager, InstanceEndpoint

_MASTER_KEY = bytes(range(32))  # deterministic 32-byte AES master key for the local KMS


def _kms() -> LocalKmsClient:
    return LocalKmsClient(_MASTER_KEY)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "HERMES_IMAGE": "hermes:test-1.0",
        "HERMES_LLM_API_KEY": "service-llm-key-xyz",
        "HERMES_LLM_PROVIDER": "anthropic",
        "HERMES_MODEL": "claude-sonnet-4-5",
        "HERMES_DOCKER_NETWORK": "hermes-net",
        "HERMES_VOLUME_ROOT": "/opt/data/hermes",
        "HERMES_API_KEY_BYTES": 32,
        # ADR-056 §1/§3: keep the readiness budget tiny so the few tests that exercise a
        # timeout/cleanup path do not actually sleep the 90s default. stale (default 120) stays >
        # ready (config invariant, ADR-056 §3). interval >= 1 (manager floors it).
        "HERMES_PROVISION_READY_TIMEOUT_SECONDS": 4,
        "HERMES_PROVISION_READY_INTERVAL_SECONDS": 1,
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------------------------
# Fake registry: an in-memory stand-in honoring the HermesInstanceRegistry contract used by the
# manager. Records call counts so race / idempotency assertions can be made without a DB.
# --------------------------------------------------------------------------------------------
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
    # TD-031: ensure_running reads created_at to detect a stale `provisioning` row. Fresh by default
    # (now()) so existing branch tests treat a provisioning row as a live concurrent provisioning.
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
        self.mark_running_calls = 0
        self.mark_stopped_calls = 0

    async def get(self, user_id: uuid.UUID) -> _Row | None:
        return self.rows.get(user_id)

    async def get_for_update(self, user_id: uuid.UUID) -> _Row | None:
        return self.rows.get(user_id)

    async def create_provisioning(
        self, user_id: uuid.UUID, *, api_key_enc: bytes, encrypted_dek: bytes, nonce: bytes
    ) -> _Row | None:
        self.create_calls += 1
        if user_id in self.rows:  # ON CONFLICT DO NOTHING
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
        row.last_active_at = datetime.datetime.now(datetime.UTC)

    async def mark_stopped(self, user_id: uuid.UUID) -> None:
        self.mark_stopped_calls += 1
        self.rows[user_id].status = "stopped"

    async def touch_active(self, user_id: uuid.UUID) -> None:
        self.touch_calls += 1
        self.rows[user_id].last_active_at = datetime.datetime.now(datetime.UTC)

    async def list_idle_running(self, threshold_seconds: int, limit: int) -> list[_Row]:
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=threshold_seconds)
        idle = [
            r for r in self.rows.values() if r.status == "running" and r.last_active_at < cutoff
        ]
        idle.sort(key=lambda r: r.last_active_at)
        return idle[:limit]

    async def delete(self, user_id: uuid.UUID) -> None:
        self.rows.pop(user_id, None)


class FakeRuntimeBackend:
    """RuntimeBackend double recording calls. ``provision`` returns a deterministic ContainerRef.

    ADR-056 §1 readiness gate: ``health`` is polled until 200 between ``provision`` and
    ``mark_running``. ``health_return`` is the steady value; ``health_sequence`` (if set) yields a
    per-call result (``True``/``False``/an exception to raise) so a "ready on the N-th poll" or
    "never ready → timeout" path can be exercised deterministically without sleeping the budget.
    """

    def __init__(self) -> None:
        self.provision_calls: list[ProvisionSpec] = []
        self.start_calls: list[ContainerRef] = []
        self.stop_calls: list[ContainerRef] = []
        self.remove_calls: list[ContainerRef] = []
        self.health_calls: list[tuple[str, str]] = []
        self.health_return = True
        self.health_sequence: list[Any] | None = None  # per-call: bool | Exception
        self.stop_fail_on: set[str] = set()  # container_ids that raise UpstreamError on stop
        self._counter = 0

    async def provision(self, spec: ProvisionSpec) -> ContainerRef:
        self.provision_calls.append(spec)
        self._counter += 1
        return ContainerRef(
            container_id=f"cid-{self._counter}",
            name=spec.name,
            endpoint=f"http://{spec.name}:{HERMES_API_PORT}",
        )

    async def start(self, container_ref: ContainerRef) -> None:
        self.start_calls.append(container_ref)

    async def stop(self, container_ref: ContainerRef) -> None:
        self.stop_calls.append(container_ref)
        if container_ref.container_id in self.stop_fail_on:
            raise UpstreamError("stop failed")

    async def remove(self, container_ref: ContainerRef) -> None:
        self.remove_calls.append(container_ref)

    async def health(self, endpoint: str, api_key: str) -> bool:
        self.health_calls.append((endpoint, api_key))
        if self.health_sequence is not None:
            # Consume the next scripted result; once exhausted, fall back to health_return so a
            # short sequence does not run off the end during a timeout poll.
            outcome = self.health_sequence.pop(0) if self.health_sequence else self.health_return
            if isinstance(outcome, BaseException):
                raise outcome
            return bool(outcome)
        return self.health_return


class _FakeSession:
    """Minimal AsyncSession double.

    The manager calls ``flush`` (legacy lifecycle paths), ``commit`` (ADR-056: the provisioning
    row is committed before docker run; mark_running is a later commit; concurrent-wait releases the
    connection per poll) and ``rollback`` (ADR-056 §2: release the row-lock before waiting on a
    concurrent provisioner). All are no-ops on the in-memory FakeRegistry; call counts are recorded
    so transaction-boundary assertions can be made.
    """

    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self.expire_all_calls = 0

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    def expire_all(self) -> None:
        # SQLAlchemy Session.expire_all is SYNC. On the in-memory FakeRegistry there is no
        # identity-map cache to drop (each get() returns fresh state), so this is a no-op; the
        # real-session staleness it guards against (ADR-056 concurrent-loser, expire_on_commit
        # =False) is covered by the integration test against a live session.
        self.expire_all_calls += 1


def _manager(
    registry: FakeRegistry, backend: FakeRuntimeBackend, settings: Settings | None = None
) -> HermesInstanceManager:
    return HermesInstanceManager(
        session=_FakeSession(),  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        backend=backend,
        kms=_kms(),
        settings=settings or _settings(),
    )


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the readiness/concurrent-wait polls on a deterministic virtual clock (ADR-056 §1/§2).

    The manager bounds its poll loops with ``time.monotonic`` and waits ``asyncio.sleep(interval)``
    between iterations. Real wall-clock would make the timeout-path tests slow (90s budget) or, if
    sleep were merely no-op'd, spin the CPU until the real budget elapsed. Instead we replace BOTH:
    ``asyncio.sleep`` ADVANCES a virtual monotonic clock by the requested interval (no real wait),
    and ``time.monotonic`` reads it. The budget/interval arithmetic in the manager is exercised
    exactly, but instantly and without flakiness.
    """
    import app.hermes_runtime.manager as manager_mod

    clock = {"now": 1000.0}

    def _monotonic() -> float:
        return clock["now"]

    async def _sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(manager_mod.time, "monotonic", _monotonic)
    monkeypatch.setattr(manager_mod.asyncio, "sleep", _sleep)


# ============================ ensure_running branches (follow_up #1) ============================
async def test_ensure_running_missing_row_provisions_one_container() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()

    ep = await mgr.ensure_running(uid)

    assert isinstance(ep, InstanceEndpoint)
    assert len(backend.provision_calls) == 1  # exactly one container provisioned
    assert reg.rows[uid].status == "running"
    assert ep.base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"


async def test_ensure_running_stopped_starts_and_marks_running() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    # Seed a stopped row directly.
    enc, dek, nonce = mgr._encrypt_key("seed-key-abcdef0123")
    reg.rows[uid] = _Row(
        user_id=uid,
        api_key_enc=enc,
        encrypted_dek=dek,
        nonce=nonce,
        status="stopped",
        container_id="cid-existing",
        endpoint=f"http://hermes-user-{uid}:{HERMES_API_PORT}",
    )

    await mgr.ensure_running(uid)

    assert len(backend.start_calls) == 1  # docker start invoked
    assert backend.start_calls[0].container_id == "cid-existing"
    assert reg.rows[uid].status == "running"
    assert len(backend.provision_calls) == 0  # no re-provision on wake


async def test_ensure_running_running_only_touches_active() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("seed-key-abcdef0123")
    reg.rows[uid] = _Row(
        user_id=uid,
        api_key_enc=enc,
        encrypted_dek=dek,
        nonce=nonce,
        status="running",
        container_id="cid-existing",
        endpoint=f"http://hermes-user-{uid}:{HERMES_API_PORT}",
    )

    await mgr.ensure_running(uid)

    assert reg.touch_calls == 1
    assert len(backend.start_calls) == 0
    assert len(backend.provision_calls) == 0
    assert reg.rows[uid].status == "running"


# ADR-056 §2 (masking-regression): a FRESH `provisioning` row is NO LONGER treated like running
# (touch-only). The previous test (`..._provisioning_only_touches_active`, asserting touch_calls==1)
# encoded the OLD contract and would now pass for the wrong reason — it is replaced by the
# concurrent-wait tests below (`test_ensure_running_fresh_provisioning_*`), which assert the new
# behaviour: release the row-lock and wait for the concurrent provisioner to reach `running`.
async def test_ensure_running_fresh_provisioning_waits_then_returns_running_endpoint() -> None:
    """ADR-056 §2: a fresh provisioning row → release the lock, wait until it becomes running.

    Does NOT provision a second container, does NOT start, does NOT touch. The concurrent
    provisioner is simulated by flipping the row to ``running`` on the FIRST concurrent-wait read.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("seed-key-abcdef0123")
    reg.rows[uid] = _Row(
        user_id=uid,
        api_key_enc=enc,
        encrypted_dek=dek,
        nonce=nonce,
        status="provisioning",
        endpoint=f"http://hermes-user-{uid}:{HERMES_API_PORT}",
    )

    # The "other" provisioner reaches running between our rollback and the first wait-read.
    original_get = reg.get
    state = {"reads": 0}

    async def _get_flipping_to_running(u: uuid.UUID) -> _Row | None:
        state["reads"] += 1
        row = await original_get(u)
        if row is not None and state["reads"] >= 1:
            row.status = "running"
            row.container_id = "cid-from-concurrent"
        return row

    reg.get = _get_flipping_to_running  # type: ignore[method-assign]

    ep = await mgr.ensure_running(uid)

    assert isinstance(ep, InstanceEndpoint)
    assert ep.base_url == f"http://hermes-user-{uid}:{HERMES_API_PORT}"
    assert len(backend.provision_calls) == 0  # no second container
    assert len(backend.start_calls) == 0
    assert reg.touch_calls == 0  # NOT treated as running/touch
    assert reg.rows[uid].status == "running"
    # The lock was released before waiting (ADR-056 §2: avoid deadlock vs the owner's mark_running).
    assert mgr._session.rollback_calls >= 1  # type: ignore[attr-defined]


async def test_ensure_running_fresh_provisioning_timeout_raises_no_second_container() -> None:
    """ADR-056 §2: if the concurrent provisioner never reaches running within budget → 502.

    The waiter NEVER creates a second container and NEVER drives cleanup (the owner does that).
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("seed-key-abcdef0123")
    reg.rows[uid] = _Row(
        user_id=uid,
        api_key_enc=enc,
        encrypted_dek=dek,
        nonce=nonce,
        status="provisioning",  # stays provisioning forever in this test
    )

    with pytest.raises(UpstreamError):
        await mgr.ensure_running(uid)

    assert len(backend.provision_calls) == 0  # never a second container
    assert len(backend.remove_calls) == 0  # waiter does not clean up the owner's container
    assert reg.touch_calls == 0


async def test_ensure_running_returns_decrypted_key_matching_provision() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()

    ep_first = await mgr.ensure_running(uid)  # provisions, returns plaintext key
    ep_second = await mgr.ensure_running(uid)  # running path, decrypts stored key

    # The endpoint returned on the running path must decrypt to the SAME key that was generated.
    assert ep_second.api_key == ep_first.api_key
    assert len(ep_second.api_key) >= 16


# ============================ provision (follow_up #3/#4) ============================
async def test_provision_generates_unique_key_min_16_chars() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    keys = {(await mgr.provision(uuid.uuid4())).api_key for _ in range(5)}
    assert len(keys) == 5  # unique per instance
    assert all(len(k) >= 16 for k in keys)


async def test_provision_persists_encrypted_material_not_null() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    await mgr.provision(uid)
    row = reg.rows[uid]
    assert row.api_key_enc and row.encrypted_dek and row.nonce  # all NOT NULL / non-empty


async def test_provision_plaintext_key_absent_from_stored_row() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    ep = await mgr.provision(uid)
    row = reg.rows[uid]
    plaintext = ep.api_key.encode("utf-8")
    # The plaintext bytes must not appear in any stored field.
    assert plaintext not in row.api_key_enc
    assert plaintext not in row.encrypted_dek
    assert plaintext not in row.nonce


async def test_provision_config_yaml_safe_toolset_only() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    await mgr.provision(uuid.uuid4())
    spec = backend.provision_calls[0]
    cfg = spec.config_yaml
    for dangerous in ("terminal", "browser", "code_execution", "computer_use"):
        assert dangerous not in cfg
    assert "platform_toolsets" in cfg and "api_server" in cfg


async def test_provision_env_has_api_server_and_no_llm_model_provider() -> None:
    # ADR-055 §4 (masking-regression): LLM_MODEL/LLM_PROVIDER are NO LONGER passed in env — the
    # image resolves the model/provider from config.yaml only. The previous assertions (which
    # required env["LLM_PROVIDER"]/env["LLM_MODEL"]) now contradict the contract and were removed.
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    await mgr.provision(uuid.uuid4())
    env = backend.provision_calls[0].env
    assert env["API_SERVER_ENABLED"] == "true"
    assert env["API_SERVER_HOST"] == "0.0.0.0"
    assert env["API_SERVER_PORT"] == str(HERMES_API_PORT)
    assert len(env["API_SERVER_KEY"]) >= 16
    assert "LLM_PROVIDER" not in env
    assert "LLM_MODEL" not in env
    # Provider key mapped to the provider's key-env (ADR-055 §4): anthropic → ANTHROPIC_API_KEY.
    assert env["ANTHROPIC_API_KEY"] == "service-llm-key-xyz"


# ================= ADR-055 §4: _container_env key-env mapping =================
def _env_for(**overrides: Any) -> dict[str, str]:
    """Build a manager from _settings(**overrides) and call _container_env directly."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(**overrides))
    return mgr._container_env("test-key")


def _assert_api_server_present(env: dict[str, str]) -> None:
    assert env["API_SERVER_ENABLED"] == "true"
    assert env["API_SERVER_HOST"] == "0.0.0.0"
    assert env["API_SERVER_PORT"] == str(HERMES_API_PORT)
    assert env["API_SERVER_KEY"] == "test-key"


def test_container_env_anthropic_maps_to_anthropic_api_key() -> None:
    env = _env_for(HERMES_LLM_PROVIDER="anthropic")
    assert env["ANTHROPIC_API_KEY"] == "service-llm-key-xyz"
    assert "LLM_MODEL" not in env and "LLM_PROVIDER" not in env
    _assert_api_server_present(env)


def test_container_env_gemini_maps_to_google_api_key() -> None:
    env = _env_for(HERMES_LLM_PROVIDER="gemini")
    assert env["GOOGLE_API_KEY"] == "service-llm-key-xyz"
    assert "LLM_MODEL" not in env and "LLM_PROVIDER" not in env
    _assert_api_server_present(env)


def test_container_env_huggingface_maps_to_hf_token() -> None:
    env = _env_for(HERMES_LLM_PROVIDER="huggingface")
    assert env["HF_TOKEN"] == "service-llm-key-xyz"
    assert "LLM_MODEL" not in env and "LLM_PROVIDER" not in env
    _assert_api_server_present(env)


def test_container_env_custom_uses_instance_llm_key_not_custom_api_key() -> None:
    # ADR-055 §6: custom has no env-key (env_vars=()) → key goes under the neutral
    # HERMES_INSTANCE_LLM_KEY (referenced from config.yaml model.api_key env-ref); the useless
    # CUSTOM_API_KEY is NOT set.
    env = _env_for(HERMES_LLM_PROVIDER="custom", HERMES_LLM_BASE_URL="https://api.example.com/v1")
    assert env["HERMES_INSTANCE_LLM_KEY"] == "service-llm-key-xyz"
    assert "CUSTOM_API_KEY" not in env
    assert "LLM_MODEL" not in env and "LLM_PROVIDER" not in env
    _assert_api_server_present(env)


def test_container_env_unknown_valid_provider_falls_back_to_upper_api_key() -> None:
    # `xiaomi` IS in HERMES_PROVIDER_ALLOWLIST but absent from HERMES_PROVIDER_KEY_ENV → the
    # conservative fallback "<PROVIDER_UPPER>_API_KEY" applies (ADR-055 §4).
    env = _env_for(HERMES_LLM_PROVIDER="xiaomi")
    assert env["XIAOMI_API_KEY"] == "service-llm-key-xyz"
    assert "LLM_MODEL" not in env and "LLM_PROVIDER" not in env
    _assert_api_server_present(env)


# ============= ADR-055 §2/§5: _require_provision_config fail-fast =============
async def _assert_provision_blocked(**overrides: Any) -> None:
    """provision must raise UpstreamError BEFORE the backend is touched (no container created)."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(**overrides))
    with pytest.raises(UpstreamError):
        await mgr.provision(uuid.uuid4())
    assert len(backend.provision_calls) == 0


async def test_provision_fails_fast_provider_openai() -> None:
    # `openai` is not in the allowlist → rejected before docker run.
    await _assert_provision_blocked(HERMES_LLM_PROVIDER="openai")


async def test_provision_fails_fast_provider_auto_forbidden() -> None:
    # `auto` is in the image set but FORBIDDEN for control-plane provisioning.
    await _assert_provision_blocked(HERMES_LLM_PROVIDER="auto")


async def test_provision_fails_fast_empty_model() -> None:
    await _assert_provision_blocked(HERMES_MODEL="")


async def test_provision_fails_fast_custom_without_base_url() -> None:
    await _assert_provision_blocked(HERMES_LLM_PROVIDER="custom", HERMES_LLM_BASE_URL="")


async def test_provision_fails_fast_azure_foundry_without_base_url() -> None:
    await _assert_provision_blocked(HERMES_LLM_PROVIDER="azure-foundry", HERMES_LLM_BASE_URL="")


# ================= ADR-055: valid config emits model section =================
async def test_provision_config_yaml_has_model_section() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings())  # anthropic / claude-sonnet-4-5
    await mgr.provision(uuid.uuid4())
    cfg = backend.provision_calls[0].config_yaml
    assert "model:" in cfg
    assert 'default: "anthropic/' in cfg
    assert 'provider: "anthropic"' in cfg


async def test_provision_custom_provider_with_base_url_succeeds() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(
        reg,
        backend,
        _settings(
            HERMES_LLM_PROVIDER="custom",
            HERMES_MODEL="my-model",
            HERMES_LLM_BASE_URL="https://api.example.com/v1",
        ),
    )
    await mgr.provision(uuid.uuid4())
    spec = backend.provision_calls[0]
    assert 'base_url: "https://api.example.com/v1"' in spec.config_yaml
    # ADR-055 §6: custom → key via config.yaml model.api_key env-ref (+ HERMES_INSTANCE_LLM_KEY
    # env), NOT CUSTOM_API_KEY.
    assert 'api_key: "${HERMES_INSTANCE_LLM_KEY}"' in spec.config_yaml
    assert spec.env["HERMES_INSTANCE_LLM_KEY"] == "service-llm-key-xyz"
    assert "CUSTOM_API_KEY" not in spec.env


async def test_provision_no_host_port_published() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    await mgr.provision(uid)
    # The manager records port=None (host port not published; DNS addressing only).
    assert reg.rows[uid].port is None


async def test_provision_idempotent_when_row_exists() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    first = await mgr.provision(uid)
    second = await mgr.provision(uid)  # row already exists → return existing, no new container
    assert len(backend.provision_calls) == 1
    assert second.api_key == first.api_key


async def test_provision_fails_fast_without_image() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(HERMES_IMAGE=""))
    with pytest.raises(UpstreamError):
        await mgr.provision(uuid.uuid4())
    assert len(backend.provision_calls) == 0


async def test_provision_fails_fast_without_llm_key() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend, _settings(HERMES_LLM_API_KEY=""))
    with pytest.raises(UpstreamError):
        await mgr.provision(uuid.uuid4())


# ===================== race (follow_up #2) — manager-level guard =====================
async def test_provision_locked_yields_to_winner_on_conflict() -> None:
    """When create_provisioning returns None (ON CONFLICT), no second container is created."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    # Simulate: another caller already inserted the row between get_for_update and create.
    enc, dek, nonce = mgr._encrypt_key("winner-key-abcdef01")
    existing = _Row(user_id=uid, api_key_enc=enc, encrypted_dek=dek, nonce=nonce, status="running")
    reg.rows[uid] = existing

    # _provision_locked bypasses the get_for_update precheck; create_provisioning returns None.
    ep = await mgr._provision_locked(uid)

    assert len(backend.provision_calls) == 0  # loser does NOT provision a second container
    assert ep.api_key == mgr._decrypt_key(existing)  # returns the winner's endpoint key
    # ADR-056 §2: on ON CONFLICT the loser rolls back its row-lock before awaiting the winner.
    assert mgr._session.rollback_calls >= 1  # type: ignore[attr-defined]


# ============================ stop_idle (follow_up #5) ============================
async def _seed_running(
    reg: FakeRegistry, mgr: HermesInstanceManager, *, idle_seconds: float
) -> uuid.UUID:
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("k-abcdef0123456789")
    reg.rows[uid] = _Row(
        user_id=uid,
        api_key_enc=enc,
        encrypted_dek=dek,
        nonce=nonce,
        status="running",
        container_id=f"cid-{uid}",
        endpoint=f"http://hermes-user-{uid}:{HERMES_API_PORT}",
        last_active_at=datetime.datetime.now(datetime.UTC)
        - datetime.timedelta(seconds=idle_seconds),
    )
    return uid


async def test_stop_idle_stops_only_rows_older_than_threshold() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    old = await _seed_running(reg, mgr, idle_seconds=1000)
    fresh = await _seed_running(reg, mgr, idle_seconds=10)

    stopped = await mgr.stop_idle(threshold_seconds=600)

    assert stopped == 1
    assert reg.rows[old].status == "stopped"
    assert reg.rows[fresh].status == "running"


async def test_stop_idle_one_backend_failure_does_not_abort_batch() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    bad = await _seed_running(reg, mgr, idle_seconds=1000)
    good = await _seed_running(reg, mgr, idle_seconds=1000)
    backend.stop_fail_on.add(f"cid-{bad}")

    stopped = await mgr.stop_idle(threshold_seconds=600)

    assert stopped == 1  # only the good one counted
    assert reg.rows[bad].status == "running"  # failed one left running for next tick
    assert reg.rows[good].status == "stopped"


async def test_stop_idle_preserves_row_volume_status_stopped() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = await _seed_running(reg, mgr, idle_seconds=1000)
    await mgr.stop_idle(threshold_seconds=600)
    # Row (and thus the host volume reference) is preserved, only status flips to stopped.
    assert uid in reg.rows
    assert reg.rows[uid].status == "stopped"


# ============================ deprovision (follow_up #6) ============================
async def test_deprovision_removes_container_and_row() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    await mgr.provision(uid)
    await mgr.deprovision(uid)
    assert len(backend.remove_calls) == 1
    assert uid not in reg.rows


async def test_deprovision_idempotent_when_missing() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    await mgr.deprovision(uuid.uuid4())  # no row → no-op
    assert len(backend.remove_calls) == 0


async def test_deprovision_no_remove_when_container_id_absent() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("k-abcdef0123456789")
    # provisioning row without a container_id yet.
    reg.rows[uid] = _Row(user_id=uid, api_key_enc=enc, encrypted_dek=dek, nonce=nonce)
    await mgr.deprovision(uid)
    assert len(backend.remove_calls) == 0  # nothing to remove
    assert uid not in reg.rows  # row still deleted


# ============================ health (follow_up #7) ============================
async def test_health_false_when_no_row() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    assert await mgr.health(uuid.uuid4()) is False
    assert len(backend.health_calls) == 0


async def test_health_false_when_no_endpoint() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    enc, dek, nonce = mgr._encrypt_key("k-abcdef0123456789")
    reg.rows[uid] = _Row(
        user_id=uid, api_key_enc=enc, encrypted_dek=dek, nonce=nonce, endpoint=None
    )
    assert await mgr.health(uid) is False
    assert len(backend.health_calls) == 0


async def test_health_proxies_backend_and_passes_decrypted_key() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    ep = await mgr.provision(uid)
    backend.health_return = True
    backend.health_calls.clear()  # drop the readiness-gate probe (ADR-056 §1); assert later only

    assert await mgr.health(uid) is True
    assert len(backend.health_calls) == 1
    probed_endpoint, probed_key = backend.health_calls[0]
    assert probed_endpoint == reg.rows[uid].endpoint
    assert probed_key == ep.api_key  # decrypted key handed to the probe


async def test_health_returns_backend_false() -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    await mgr.provision(uid)
    backend.health_return = False
    assert await mgr.health(uid) is False


async def test_health_does_not_log_key(caplog: pytest.LogCaptureFixture) -> None:
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    ep = await mgr.provision(uid)
    with caplog.at_level("DEBUG"):
        await mgr.health(uid)
    assert ep.api_key not in caplog.text


# ============================ encryption round-trip (follow_up #4) ============================
def test_encrypt_decrypt_round_trip_restores_key() -> None:
    mgr = _manager(FakeRegistry(), FakeRuntimeBackend())
    secret = "API-SERVER-KEY-round-trip-0123456789"
    enc, dek, nonce = mgr._encrypt_key(secret)
    # Build a row-like object exposing the bytes fields _decrypt_key reads.
    row = _Row(user_id=uuid.uuid4(), api_key_enc=enc, encrypted_dek=dek, nonce=nonce)
    assert mgr._decrypt_key(row) == secret


def test_encrypt_uses_distinct_dek_and_nonce_each_call() -> None:
    mgr = _manager(FakeRegistry(), FakeRuntimeBackend())
    enc1, dek1, n1 = mgr._encrypt_key("same-plaintext-key-0123")
    enc2, dek2, n2 = mgr._encrypt_key("same-plaintext-key-0123")
    # Random DEK + nonce per call ⇒ ciphertext differs even for identical plaintext.
    assert n1 != n2
    assert enc1 != enc2
    assert dek1 != dek2


# ============================ ADR-056 §1: readiness gate (provision) ============================
async def test_provision_readiness_gate_marks_running_only_after_health_200() -> None:
    """ADR-056 §1: health 200 on the N-th poll → mark_running + running + endpoint returned.

    The row stays `provisioning` while health is not yet 200 (mark_running fires only after 200),
    and the endpoint is returned only after the row is running.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    # Not ready on the first two polls, ready on the third.
    backend.health_sequence = [False, False, True]

    ep = await mgr.ensure_running(uid)

    assert isinstance(ep, InstanceEndpoint)
    assert len(backend.provision_calls) == 1
    assert reg.mark_running_calls == 1  # marked running exactly once, after readiness
    assert reg.rows[uid].status == "running"
    assert reg.rows[uid].endpoint == f"http://hermes-user-{uid}:{HERMES_API_PORT}"
    assert len(backend.health_calls) == 3  # polled until the first 200
    # The provisioning row was committed BEFORE docker run; mark_running is a later commit.
    assert mgr._session.commit_calls >= 2  # type: ignore[attr-defined]


async def test_provision_readiness_gate_first_poll_200_marks_running() -> None:
    """Steady-state happy path: health 200 on the first poll → single probe, mark_running."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    backend.health_return = True

    await mgr.ensure_running(uid)

    assert reg.rows[uid].status == "running"
    assert len(backend.health_calls) == 1
    assert reg.mark_running_calls == 1


async def test_provision_readiness_timeout_removes_container_and_row_and_raises() -> None:
    """ADR-056 §1 cleanup: health never 200 → remove container + drop row + UpstreamError(→502).

    No inconsistent `running` row and no lingering `provisioning` row remain; mark_running is never
    called. The host volume is NOT removed (only backend.remove on the container).
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    backend.health_return = False  # never becomes ready within the budget

    with pytest.raises(UpstreamError):
        await mgr.ensure_running(uid)

    assert len(backend.provision_calls) == 1
    assert len(backend.remove_calls) == 1  # unready container removed (cleanup)
    assert reg.mark_running_calls == 0  # never marked running
    assert uid not in reg.rows  # provisioning row dropped → next ensure_running starts clean
    # health was polled more than once (budget/interval) before giving up.
    assert len(backend.health_calls) >= 2


async def test_provision_readiness_timeout_cleanup_survives_remove_failure() -> None:
    """ADR-056 §1: a container-remove failure during cleanup must not mask the readiness timeout.

    The row is still dropped and UpstreamError is raised even when backend.remove raises.
    """
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    uid = uuid.uuid4()
    backend.health_return = False

    async def _remove_boom(_ref: ContainerRef) -> None:
        backend.remove_calls.append(_ref)
        raise UpstreamError("remove failed")

    backend.remove = _remove_boom  # type: ignore[method-assign]

    with pytest.raises(UpstreamError):
        await mgr.ensure_running(uid)

    assert len(backend.remove_calls) == 1  # remove was attempted
    assert uid not in reg.rows  # row dropped despite the remove failure


# ============================ ADR-056 §4(1): volume ownership env ============================
def test_container_env_carries_hermes_uid_gid_default_10001() -> None:
    """ADR-056 §4(1): HERMES_UID/HERMES_GID in the container env, default 10001 (api uid/gid)."""
    env = _env_for()  # default settings
    assert env["HERMES_UID"] == "10001"
    assert env["HERMES_GID"] == "10001"


def test_container_env_hermes_uid_gid_configurable() -> None:
    env = _env_for(HERMES_UID=20002, HERMES_GID=30003)
    assert env["HERMES_UID"] == "20002"
    assert env["HERMES_GID"] == "30003"


async def test_provision_passes_uid_gid_into_container_spec() -> None:
    """End-to-end through provision: the ProvisionSpec env carries the ownership UID/GID."""
    reg, backend = FakeRegistry(), FakeRuntimeBackend()
    mgr = _manager(reg, backend)
    await mgr.provision(uuid.uuid4())
    env = backend.provision_calls[0].env
    assert env["HERMES_UID"] == "10001"
    assert env["HERMES_GID"] == "10001"


# ================= ADR-056 §3: config invariant (stale > ready) =================
def test_settings_rejects_stale_le_ready() -> None:
    """ADR-056 §3 fail-fast: stale <= ready is a ValidationError at Settings construction."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _settings(
            HERMES_PROVISIONING_STALE_SECONDS=90,
            HERMES_PROVISION_READY_TIMEOUT_SECONDS=90,  # equal → rejected
        )
    with pytest.raises(pydantic.ValidationError):
        _settings(
            HERMES_PROVISIONING_STALE_SECONDS=10,
            HERMES_PROVISION_READY_TIMEOUT_SECONDS=90,  # stale < ready → rejected
        )


def test_settings_defaults_satisfy_stale_gt_ready() -> None:
    """The production defaults (stale 120 > ready 90) construct cleanly (no override)."""
    s = Settings(
        HERMES_IMAGE="hermes:test-1.0",
        HERMES_LLM_API_KEY="k",
        HERMES_MODEL="m",
    )
    assert s.hermes_provisioning_stale_seconds > s.hermes_provision_ready_timeout_seconds
