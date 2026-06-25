"""Agent proxy: /v1/agent/* control plane to per-user Hermes instances (ADR-045, ADR-047).

Thin proxy between iOS and the user's Hermes runtime: policy-gate + launch (`POST /v1/agent/run`),
SSE relay with usage-based billing on `run.completed` (`GET .../events`), and approval/stop
passthrough. Instance lifecycle is owned by ``hermes_runtime`` (ADR-046); billing/policy/audit
reuse the existing wallet/policy/audit services.
"""

from app.agent_proxy.billing import usage_to_credits
from app.agent_proxy.service import AgentProxyService, RunLaunchResult

__all__ = ["AgentProxyService", "RunLaunchResult", "usage_to_credits"]
