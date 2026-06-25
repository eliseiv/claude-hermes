"""Unit: DockerBackend over a MOCKED docker-py client (ADR-046 §2, hermes-runtime/09).

No real Docker socket is touched: the lazy ``_client`` attribute is pre-populated with a fake
client, and ``docker.errors`` is imported for the real exception types the backend catches. Covers
the NotFound→no-op (idempotent stop/remove) and APIError/DockerException→UpstreamError(502)
contours, plus the no-host-port provision invariant and HTTP health probing.
"""

from __future__ import annotations

from typing import Any

import docker.errors as derr  # real exception types the backend catches
import httpx
import pytest

from app.errors import UpstreamError
from app.hermes_runtime.docker_backend import (
    HERMES_API_PORT,
    HERMES_COMMAND,
    HERMES_HOME_MOUNT,
    ContainerRef,
    DockerBackend,
    ProvisionSpec,
)


class _FakeContainer:
    def __init__(self, cid: str = "cid-123") -> None:
        self.id = cid
        self.started = False
        self.stopped = False
        self.removed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None
        self.run_exc: Exception | None = None
        self.get_exc: Exception | None = None
        self.container = _FakeContainer()

    def run(self, **kwargs: Any) -> _FakeContainer:
        self.run_kwargs = kwargs
        if self.run_exc is not None:
            raise self.run_exc
        return self.container

    def get(self, container_id: str) -> _FakeContainer:
        if self.get_exc is not None:
            raise self.get_exc
        return self.container


class _FakeDockerClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


def _backend_with_fake() -> tuple[DockerBackend, _FakeDockerClient]:
    backend = DockerBackend()
    fake = _FakeDockerClient()
    backend._client = fake  # inject so _docker_client() never calls docker.from_env()
    return backend, fake


def _ref() -> ContainerRef:
    return ContainerRef(container_id="cid-123", name="hermes-user-x", endpoint="http://h:8642")


def _spec(tmp_path: Any) -> ProvisionSpec:
    return ProvisionSpec(
        name="hermes-user-x",
        image="hermes:test",
        env={"API_SERVER_KEY": "secret-key-0123456789"},
        volume_host_path=str(tmp_path / "vol"),
        network="hermes-net",
        config_yaml="platform_toolsets:\n  api_server:\n    - web\n",
    )


# ============================ provision (follow_up #3/#8) ============================
async def test_provision_no_host_port_and_writes_config(tmp_path: Any) -> None:
    backend, fake = _backend_with_fake()
    spec = _spec(tmp_path)

    ref = await backend.provision(spec)

    kwargs = fake.containers.run_kwargs
    assert kwargs is not None
    # No host port published: ports must be empty (DNS-only addressing).
    assert kwargs["ports"] == {}
    assert kwargs["command"] == HERMES_COMMAND
    assert kwargs["detach"] is True
    assert kwargs["network"] == "hermes-net"
    # Volume bind-mounted to HERMES_HOME mount point.
    binds = kwargs["volumes"]
    assert list(binds.values())[0]["bind"] == HERMES_HOME_MOUNT
    # config.yaml written into the volume before boot.
    config_file = tmp_path / "vol" / "config.yaml"
    assert config_file.exists()
    assert "api_server" in config_file.read_text(encoding="utf-8")
    assert ref.endpoint == f"http://{spec.name}:{HERMES_API_PORT}"


# ============================ ADR-056 §4(3): idempotent config.yaml ============================
_VALID_CONFIG = (
    "platform_toolsets:\n  api_server:\n    - web\n"
    "approvals:\n  mode: deny\n"
    'model:\n  default: "anthropic/m"\n  provider: "anthropic"\n'
)


def _valid_spec(tmp_path: Any) -> ProvisionSpec:
    return ProvisionSpec(
        name="hermes-user-x",
        image="hermes:test",
        env={"API_SERVER_KEY": "secret-key-0123456789"},
        volume_host_path=str(tmp_path / "vol"),
        network="hermes-net",
        config_yaml=_VALID_CONFIG,
    )


async def test_provision_writes_config_when_absent(tmp_path: Any) -> None:
    """First provision: no config.yaml on the volume → it IS written (ADR-056 §4(3))."""
    backend, _ = _backend_with_fake()
    await backend.provision(_valid_spec(tmp_path))
    config_file = tmp_path / "vol" / "config.yaml"
    assert config_file.exists()
    assert config_file.read_text(encoding="utf-8") == _VALID_CONFIG


async def test_provision_does_not_overwrite_existing_valid_config(tmp_path: Any) -> None:
    """Reuse: an existing VALID config.yaml is NOT rewritten (ADR-056 §4(3) idempotent write)."""
    backend, _ = _backend_with_fake()
    vol = tmp_path / "vol"
    vol.mkdir()
    config_file = vol / "config.yaml"
    # A pre-existing, structurally valid config with a DISTINCT marker so a rewrite is detectable.
    preexisting = _VALID_CONFIG + "# preserved-marker\n"
    config_file.write_text(preexisting, encoding="utf-8")

    await backend.provision(_valid_spec(tmp_path))

    # Untouched: the reuse path must not depend on write permission to a file the instance re-owned.
    assert config_file.read_text(encoding="utf-8") == preexisting


async def test_provision_overwrites_corrupt_existing_config(tmp_path: Any) -> None:
    """Recovery: a corrupt/incomplete existing config (missing markers) IS rewritten (§4(3))."""
    backend, _ = _backend_with_fake()
    vol = tmp_path / "vol"
    vol.mkdir()
    config_file = vol / "config.yaml"
    config_file.write_text(
        "platform_toolsets:\n  api_server:\n    - web\n", encoding="utf-8"
    )  # no model

    await backend.provision(_valid_spec(tmp_path))

    assert config_file.read_text(encoding="utf-8") == _VALID_CONFIG  # recovered


async def test_provision_overwrites_empty_existing_config(tmp_path: Any) -> None:
    """An empty existing config.yaml is treated as invalid → rewritten (§4(3))."""
    backend, _ = _backend_with_fake()
    vol = tmp_path / "vol"
    vol.mkdir()
    config_file = vol / "config.yaml"
    config_file.write_text("   \n", encoding="utf-8")

    await backend.provision(_valid_spec(tmp_path))

    assert config_file.read_text(encoding="utf-8") == _VALID_CONFIG


def test_existing_config_valid_recognises_complete_config(tmp_path: Any) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_VALID_CONFIG, encoding="utf-8")
    assert DockerBackend._existing_config_valid(str(config_file)) is True


def test_existing_config_valid_false_for_missing_file(tmp_path: Any) -> None:
    assert DockerBackend._existing_config_valid(str(tmp_path / "nope.yaml")) is False


def test_existing_config_valid_false_when_missing_model_markers(tmp_path: Any) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("platform_toolsets:\n  api_server:\n    - web\n", encoding="utf-8")
    assert DockerBackend._existing_config_valid(str(config_file)) is False


def test_existing_config_valid_false_when_empty(tmp_path: Any) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("", encoding="utf-8")
    assert DockerBackend._existing_config_valid(str(config_file)) is False


async def test_provision_image_not_found_raises_upstream(tmp_path: Any) -> None:
    backend, fake = _backend_with_fake()
    fake.containers.run_exc = derr.ImageNotFound("no image")
    with pytest.raises(UpstreamError):
        await backend.provision(_spec(tmp_path))


async def test_provision_api_error_raises_upstream(tmp_path: Any) -> None:
    backend, fake = _backend_with_fake()
    fake.containers.run_exc = derr.APIError("boom")
    with pytest.raises(UpstreamError):
        await backend.provision(_spec(tmp_path))


async def test_provision_error_message_omits_secret(tmp_path: Any) -> None:
    backend, fake = _backend_with_fake()
    fake.containers.run_exc = derr.APIError("boom")
    with pytest.raises(UpstreamError) as ei:
        await backend.provision(_spec(tmp_path))
    assert "secret-key-0123456789" not in str(ei.value)


# ============================ start ============================
async def test_start_calls_container_start() -> None:
    backend, fake = _backend_with_fake()
    await backend.start(_ref())
    assert fake.containers.container.started is True


async def test_start_not_found_raises_upstream() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.NotFound("gone")
    with pytest.raises(UpstreamError):
        await backend.start(_ref())


async def test_start_api_error_raises_upstream() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.APIError("boom")
    with pytest.raises(UpstreamError):
        await backend.start(_ref())


# ============================ stop — NotFound no-op, APIError→502 (follow_up #8) ===========
async def test_stop_calls_container_stop() -> None:
    backend, fake = _backend_with_fake()
    await backend.stop(_ref())
    assert fake.containers.container.stopped is True


async def test_stop_not_found_is_noop() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.NotFound("gone")
    # Idempotent: already-removed container treated as stopped, no raise.
    await backend.stop(_ref())


async def test_stop_api_error_raises_upstream() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.APIError("boom")
    with pytest.raises(UpstreamError):
        await backend.stop(_ref())


async def test_stop_docker_exception_raises_upstream() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.DockerException("daemon down")
    with pytest.raises(UpstreamError):
        await backend.stop(_ref())


# ============================ remove — NotFound no-op, APIError→502 (follow_up #8) =========
async def test_remove_calls_force_remove() -> None:
    backend, fake = _backend_with_fake()
    await backend.remove(_ref())
    assert fake.containers.container.removed is True


async def test_remove_not_found_is_noop() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.NotFound("gone")
    # Idempotent deprovision: already absent → no raise.
    await backend.remove(_ref())


async def test_remove_api_error_raises_upstream() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.APIError("boom")
    with pytest.raises(UpstreamError):
        await backend.remove(_ref())


async def test_remove_docker_exception_raises_upstream() -> None:
    backend, fake = _backend_with_fake()
    fake.containers.get_exc = derr.DockerException("daemon down")
    with pytest.raises(UpstreamError):
        await backend.remove(_ref())


# ============================ health (follow_up #7) ============================
async def test_health_true_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DockerBackend()

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> _Resp:
            assert headers["Authorization"] == "Bearer key-abc"
            assert url.endswith("/health")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await backend.health("http://h:8642", "key-abc") is True


async def test_health_false_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DockerBackend()

    class _Resp:
        status_code = 503

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await backend.health("http://h:8642", "key-abc") is False


async def test_health_false_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DockerBackend()

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> Any:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await backend.health("http://h:8642", "key-abc") is False
