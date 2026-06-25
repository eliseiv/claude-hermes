"""RuntimeBackend abstraction + DockerBackend (docker-py) — ADR-046 §2, hermes-runtime/02.

``RuntimeBackend`` is the extensible interface (pattern: ``KmsClient`` ADR-003 / ``LLMClient``
ADR-033) so future Modal/Daytona backends plug in without touching ``manager.py``/``registry.py``.
MVP fixes ``DockerBackend`` on docker-py.

docker-py is synchronous; every blocking call runs in a worker thread (``asyncio.to_thread``) so
the event loop is never blocked. The container's port (8642) is NOT published to the host — the
container joins the dedicated control-plane network and is addressed by its DNS name. The Docker
client is created lazily and reused per backend instance.

No secret (``API_SERVER_KEY``, provider key) is ever logged here; only metadata (container id,
name, status, endpoint) is logged by callers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.errors import UpstreamError

logger = logging.getLogger("app.hermes_runtime.docker_backend")

# Fixed Hermes API-server contract (ADR-046 §1; do not change): in-container port and command.
HERMES_API_PORT = 8642
HERMES_COMMAND = ["gateway", "run"]
# In-container mount point of HERMES_HOME (the user's volume).
HERMES_HOME_MOUNT = "/opt/data"
# In-container path of the toolset-restricting config.yaml (ADR-046 §5/§6).
HERMES_CONFIG_PATH = "/opt/data/config.yaml"


@dataclass(frozen=True)
class ContainerRef:
    """Opaque handle to a provisioned runtime container.

    ``container_id`` and ``name`` identify the unit; ``endpoint`` is the DNS address used by the
    Agent Proxy (``http://hermes-user-<id>:8642``). Backend-agnostic so non-Docker backends can
    populate the same shape.
    """

    container_id: str
    name: str
    endpoint: str


@dataclass(frozen=True)
class ProvisionSpec:
    """Inputs for provisioning a single Hermes container (ADR-046 §1, hermes-runtime/02)."""

    name: str
    image: str
    env: dict[str, str]
    volume_host_path: str
    network: str
    config_yaml: str


class RuntimeBackend(Protocol):
    """Lifecycle backend for per-user Hermes runtimes (ADR-046 §2).

    Implementations: ``DockerBackend`` (MVP). All methods are async; concrete backends wrap any
    blocking SDK calls off the event loop.
    """

    async def provision(self, spec: ProvisionSpec) -> ContainerRef: ...

    async def start(self, container_ref: ContainerRef) -> None: ...

    async def stop(self, container_ref: ContainerRef) -> None: ...

    async def remove(self, container_ref: ContainerRef) -> None: ...

    async def health(self, endpoint: str, api_key: str) -> bool: ...


class DockerBackend:
    """``RuntimeBackend`` on docker-py (ADR-046 §2). Sync SDK calls run in worker threads.

    The Docker client requires control-plane access to the Docker socket (07-deployment.md,
    05-security.md). It is created lazily so importing the module (and unit tests that mock the
    backend) never touches a real socket.
    """

    def __init__(self, health_timeout_seconds: float = 5.0) -> None:
        self._client: Any = None
        self._health_timeout = health_timeout_seconds

    def _docker_client(self) -> Any:
        """Lazily create and cache the docker-py client from the ambient environment.

        ``docker.from_env()`` reads ``DOCKER_HOST`` / the default socket. Failure to connect is a
        control-plane misconfiguration surfaced as an upstream error by the caller.
        """
        if self._client is None:
            import docker  # local import: keep module import socket-free (unit tests)

            # docker-py ships no py.typed; mypy resolves the package as present-but-untyped and
            # does not see `from_env` (re-exported via docker.api.*). Reach it through an Any-typed
            # module ref so the untyped public API is accessed cleanly (no per-line type: ignore).
            docker_module: Any = docker
            self._client = docker_module.from_env()
        return self._client

    async def provision(self, spec: ProvisionSpec) -> ContainerRef:
        """``docker run`` the Hermes image: volume + restricted config.yaml + network, no host port.

        Writing ``config.yaml`` (toolset restriction, ADR-046 §6) into the bind-mounted volume
        BEFORE the container starts guarantees the instance boots with the safe toolset. The port
        is exposed inside the network only (no ``ports=`` mapping ⇒ not published to the host).
        Raises :class:`UpstreamError` (→ 502 at the proxy) on any Docker failure.
        """
        return await asyncio.to_thread(self._provision_sync, spec)

    def _provision_sync(self, spec: ProvisionSpec) -> ContainerRef:
        import os

        import docker.errors as derr

        # Ensure the per-user volume root exists and write the restricted config.yaml into it.
        os.makedirs(spec.volume_host_path, exist_ok=True)
        config_file = os.path.join(spec.volume_host_path, "config.yaml")
        # ADR-056 §4(3): idempotent config.yaml. On reuse (the file already exists and is valid) do
        # NOT rewrite it — this removes the reuse path's dependency on write permission to a file
        # the instance may have re-owned. The file is (re)written ONLY when absent (first provision)
        # or invalid/corrupt (recovery; possible now that ownership is aligned via §4(1)). A full
        # config refresh happens via deprovision+provision (TD-031 replay), not here.
        if not self._existing_config_valid(config_file):
            with open(config_file, "w", encoding="utf-8") as handle:
                handle.write(spec.config_yaml)

        client = self._docker_client()
        try:
            container = client.containers.run(
                image=spec.image,
                command=HERMES_COMMAND,
                name=spec.name,
                detach=True,
                environment=spec.env,
                volumes={spec.volume_host_path: {"bind": HERMES_HOME_MOUNT, "mode": "rw"}},
                network=spec.network,
                # The in-container port is reachable inside the network; NOT published to the host.
                ports={},
                restart_policy={"Name": "unless-stopped"},
            )
        except (derr.ImageNotFound, derr.APIError, derr.DockerException) as exc:
            # Do not include env (provider key) in the message.
            logger.error("hermes container provision failed name=%s", spec.name)
            raise UpstreamError("failed to provision hermes runtime") from exc

        endpoint = f"http://{spec.name}:{HERMES_API_PORT}"
        logger.info(
            "hermes container provisioned name=%s container_id=%s endpoint=%s",
            spec.name,
            container.id,
            endpoint,
        )
        return ContainerRef(container_id=container.id, name=spec.name, endpoint=endpoint)

    @staticmethod
    def _existing_config_valid(config_file: str) -> bool:
        """True when an existing ``config.yaml`` is present and structurally valid (ADR-056 §4(3)).

        Valid ⟺ the file exists, is non-empty, and carries the mandatory sections the control plane
        always renders: ``platform_toolsets.api_server`` + ``model.default`` + ``model.provider``.
        The file is control-plane-generated (a small, fixed, hand-rendered shape), so a marker check
        is a sufficient structural validation without adding a YAML-parser dependency: a partial /
        corrupt write misses ≥1 marker → treated invalid → rewritten (recovery). Any read error
        (missing / unreadable) → invalid → (re)write.
        """
        import os

        if not os.path.isfile(config_file):
            return False
        try:
            with open(config_file, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            return False
        if not content.strip():
            return False
        required = ("platform_toolsets:", "api_server:", "model:", "default:", "provider:")
        return all(marker in content for marker in required)

    async def start(self, container_ref: ContainerRef) -> None:
        """Wake a hibernated container (``docker start``)."""
        await asyncio.to_thread(self._start_sync, container_ref)

    def _start_sync(self, container_ref: ContainerRef) -> None:
        import docker.errors as derr

        client = self._docker_client()
        try:
            container = client.containers.get(container_ref.container_id)
            container.start()
        except (derr.NotFound, derr.APIError, derr.DockerException) as exc:
            logger.error(
                "hermes container start failed container_id=%s", container_ref.container_id
            )
            raise UpstreamError("failed to start hermes runtime") from exc
        logger.info("hermes container started container_id=%s", container_ref.container_id)

    async def stop(self, container_ref: ContainerRef) -> None:
        """Hibernate a container (``docker stop``). The volume is preserved."""
        await asyncio.to_thread(self._stop_sync, container_ref)

    def _stop_sync(self, container_ref: ContainerRef) -> None:
        import docker.errors as derr

        client = self._docker_client()
        try:
            container = client.containers.get(container_ref.container_id)
            container.stop()
        except derr.NotFound:
            # Already gone (external removal). Idempotent: treat as stopped.
            logger.warning(
                "hermes container missing on stop (treated as stopped) container_id=%s",
                container_ref.container_id,
            )
            return
        except (derr.APIError, derr.DockerException) as exc:
            logger.error("hermes container stop failed container_id=%s", container_ref.container_id)
            raise UpstreamError("failed to stop hermes runtime") from exc
        logger.info("hermes container stopped container_id=%s", container_ref.container_id)

    async def remove(self, container_ref: ContainerRef) -> None:
        """Remove a container (``docker rm -f``). Host volume is preserved (ADR-046, Q-046-2)."""
        await asyncio.to_thread(self._remove_sync, container_ref)

    def _remove_sync(self, container_ref: ContainerRef) -> None:
        import docker.errors as derr

        client = self._docker_client()
        try:
            container = client.containers.get(container_ref.container_id)
            container.remove(force=True)
        except derr.NotFound:
            # Already removed: idempotent deprovision.
            logger.warning(
                "hermes container already absent on remove container_id=%s",
                container_ref.container_id,
            )
            return
        except (derr.APIError, derr.DockerException) as exc:
            logger.error(
                "hermes container remove failed container_id=%s", container_ref.container_id
            )
            raise UpstreamError("failed to remove hermes runtime") from exc
        logger.info("hermes container removed container_id=%s", container_ref.container_id)

    async def health(self, endpoint: str, api_key: str) -> bool:
        """Probe ``GET {endpoint}/health`` with the instance bearer key.

        Returns True only on a 2xx response; any connection error / non-2xx ⇒ False (the caller
        decides whether to reprovision). The bearer key is sent but never logged (redaction).
        Uses ``verify=True`` (default) — the endpoint is plain HTTP inside the docker network, but
        we keep TLS verification on for any future https endpoint.
        """
        url = f"{endpoint}/health"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._health_timeout) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            logger.warning("hermes health probe failed endpoint=%s", endpoint)
            return False
        return 200 <= response.status_code < 300
