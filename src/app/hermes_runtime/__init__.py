"""Per-user Hermes runtime: lifecycle manager, Docker backend, registry (ADR-046).

Manages one Hermes container + ``HERMES_HOME`` volume per user (provision / wake / hibernate /
deprovision / health), with the per-instance ``API_SERVER_KEY`` envelope-encrypted via ``byok.kms``
(ADR-003). The ``RuntimeBackend`` interface leaves room for Modal/Daytona backends post-MVP.
"""

from app.hermes_runtime.docker_backend import (
    ContainerRef,
    DockerBackend,
    ProvisionSpec,
    RuntimeBackend,
)
from app.hermes_runtime.manager import HermesInstanceManager, InstanceEndpoint
from app.hermes_runtime.registry import HermesInstanceRegistry

__all__ = [
    "ContainerRef",
    "DockerBackend",
    "HermesInstanceManager",
    "HermesInstanceRegistry",
    "InstanceEndpoint",
    "ProvisionSpec",
    "RuntimeBackend",
]
