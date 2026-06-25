"""Agent proxy service: launch, SSE relay + billing, approval/stop passthrough (ADR-045/047).

Owns the proxy logic so the router stays thin and the flow is unit-testable with a mocked Hermes
instance (respx/httpx) and a mocked ``HermesInstanceManager``. Instance lifecycle and the
``API_SERVER_KEY`` are owned by ``hermes_runtime`` (ADR-046); the decrypted key lives only in the
``InstanceEndpoint`` returned by ``ensure_running`` and is never logged or relayed to the client.

SSE wire format (Hermes external contract): ``event: <name>\\ndata: <json>\\n\\n``. The relay
forwards every event byte-for-event to the client and, on the terminal ``run.completed`` carrying
``usage``, debits the wallet exactly once (idempotency by ``runId``, ADR-047 §4).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_proxy.billing import usage_to_credits
from app.audit.service import (
    EVENT_BILLING_DEBIT_INSUFFICIENT,
    AuditEvent,
    AuditService,
)
from app.config import Settings
from app.errors import InsufficientCreditsError, UpstreamError
from app.hermes_runtime.manager import HermesInstanceManager, InstanceEndpoint
from app.policy.engine import Decision, Mode, evaluate
from app.policy.loader import load_policy_state
from app.wallet.service import WalletService

logger = logging.getLogger("app.agent_proxy.service")

# audit eventType for an agent run (audit catalog; agent-proxy/05-events.md). billing_debit is
# recorded by WalletService.consume itself; this marks the run lifecycle.
EVENT_AGENT_RUN = "agent_run"

# Terminal SSE event name that triggers billing (Hermes external contract, agent-proxy/05-events).
# run.failed needs no special handling — it is relayed like any other event, without a debit.
_EVENT_RUN_COMPLETED = "run.completed"

# Block reasons surfaced as 200 {status:blocked} for the agent path (credits-branch only, ADR-047
# §3): the agent contour never runs in byok mode on MVP, so only these can occur. debt_outstanding
# (ADR-051 §4) is included UNCONDITIONALLY (NOT gated by the flag): the default
# AGENT_DEBT_RECONCILE_ENABLED=true makes it reachable, so it must be a valid member of the
# achievable set to avoid a false "unexpected reason" log (agent-proxy/02-api-contracts.md
# needs_code_sync). The flag gates EMISSION (whether the debt check runs), not enum membership.
_AGENT_BLOCK_REASONS = frozenset(
    {"credits_empty", "subscription_expired", "trial_used", "debt_outstanding"}
)
# Block reason for an unsettled agent-run debt (ADR-051 §4).
_DEBT_OUTSTANDING = "debt_outstanding"


@dataclass(frozen=True)
class RunLaunchResult:
    """Outcome of ``POST /v1/agent/run``.

    ``blocked`` carries ``block_reason`` and no ``run_id`` / ``status``; an allowed launch carries
    the Hermes ``run_id`` and ``status`` (queued|running) and no ``block_reason``.
    """

    blocked: bool
    block_reason: str | None = None
    run_id: str | None = None
    status: str | None = None


class AgentProxyService:
    """Proxies the client agent contour to the user's Hermes instance (ADR-045)."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        manager: HermesInstanceManager,
        wallet: WalletService,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        self._session = session
        self._manager = manager
        self._wallet = wallet
        self._audit = audit
        self._settings = settings

    # --- run launch -------------------------------------------------------------------------

    async def run(
        self,
        *,
        user_id: uuid.UUID,
        message: str,
        session_id: str | None,
        model: str | None,
    ) -> RunLaunchResult:
        """Policy-gate → ensure_running → proxy ``POST /v1/runs`` (ADR-045 §2, ADR-047 §3).

        Order is strict: (1) policy evaluate in the credits branch — blocked stops here WITHOUT
        waking the container or debiting (200 blocked, ADR-004); (2) ``ensure_running`` resolves
        the user's instance endpoint + bearer key; (3) proxy the launch with the mapped body.
        Any upstream/instance failure surfaces as 502 (UpstreamError), never as 200 blocked.
        """
        # (1) Policy gate (credits branch; agent path has no byok mode on MVP, ADR-047 §3).
        state = await load_policy_state(self._session, user_id)
        decision: Decision = evaluate(state, Mode.credits)
        if not decision.allow:
            reason = decision.block_reason.value if decision.block_reason is not None else None
            await self._audit.record(
                AuditEvent(
                    user_id=user_id,
                    event_type=EVENT_AGENT_RUN,
                    payload={"phase": "blocked", "blockReason": reason},
                )
            )
            # Defensive: only the credits-branch reasons are expected here.
            if reason not in _AGENT_BLOCK_REASONS:
                logger.warning("agent run blocked with unexpected reason=%s", reason)
            return RunLaunchResult(blocked=True, block_reason=reason)

        # (1b) Debt-gate (ADR-051 §4): an unsettled agent-run debt blocks a NEW run BEFORE waking
        # the container (200 blocked, ADR-004). Reachable only on the agent path, only when
        # AGENT_DEBT_RECONCILE_ENABLED (the EMISSION gate). Cleared by clawback on the next grant.
        if self._settings.agent_debt_reconcile_enabled:
            debt = await self._wallet.current_debt(user_id)
            if debt > 0:
                await self._audit.record(
                    AuditEvent(
                        user_id=user_id,
                        event_type=EVENT_AGENT_RUN,
                        payload={
                            "phase": "blocked",
                            "blockReason": _DEBT_OUTSTANDING,
                            "debt": debt,
                        },
                    )
                )
                return RunLaunchResult(blocked=True, block_reason=_DEBT_OUTSTANDING)

        # (2) Resolve (provision/wake) the user's Hermes instance.
        endpoint = await self._manager.ensure_running(user_id)

        # (3) Proxy the launch. Map iOS body → Hermes body (ADR-045 §4).
        hermes_body: dict[str, Any] = {"input": message}
        if session_id is not None:
            hermes_body["session_id"] = session_id
        if model is not None:
            hermes_body["model"] = model

        run_id, status = await self._launch_run(endpoint, hermes_body)
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_AGENT_RUN,
                payload={"phase": "launched", "runId": run_id, "status": status},
            )
        )
        return RunLaunchResult(blocked=False, run_id=run_id, status=status)

    async def _launch_run(
        self, endpoint: InstanceEndpoint, body: dict[str, Any]
    ) -> tuple[str, str]:
        """POST {base}/v1/runs with the instance bearer; return (run_id, status). 502 on failure."""
        url = f"{endpoint.base_url}/v1/runs"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.hermes_proxy_timeout_seconds
            ) as client:
                response = await client.post(
                    url, json=body, headers=self._bearer_headers(endpoint.api_key)
                )
        except httpx.HTTPError as exc:
            logger.warning("hermes run launch transport error")
            raise UpstreamError("hermes instance unreachable") from exc

        if not 200 <= response.status_code < 300:
            logger.warning("hermes run launch non-2xx status=%s", response.status_code)
            raise UpstreamError("hermes run launch failed")

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("hermes run launch returned invalid body") from exc
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise UpstreamError("hermes run launch returned no run_id")
        status = payload.get("status")
        if status not in ("queued", "running"):
            # Normalize any other/missing status to queued (the client only distinguishes
            # queued/running; both mean "accepted").
            status = "queued"
        return run_id, status

    # --- SSE relay + billing ----------------------------------------------------------------

    async def stream_events(self, *, user_id: uuid.UUID, run_id: str) -> AsyncIterator[bytes]:
        """Relay the instance SSE stream to the client; bill once on ``run.completed`` (ADR-047).

        The run is addressed within the user's own instance (RBAC: runId is namespaced to the
        subject's instance, agent-proxy/06-rbac.md), so ``ensure_running(user_id)`` resolves it —
        a foreign run is unreachable by construction. Events are forwarded as-is. On the terminal
        ``run.completed`` carrying ``usage`` we debit the wallet with idempotency_key=run_id; a
        re-subscription / duplicate event debits at most once (ADR-005 unique index). ``run.failed``
        is forwarded without any debit (ADR-047 §4).
        """
        endpoint = await self._manager.ensure_running(user_id)
        url = f"{endpoint.base_url}/v1/runs/{run_id}/events"
        # Long-lived stream: bound connect/write, disable read timeout so a slow run is not killed.
        timeout = httpx.Timeout(
            connect=self._settings.hermes_sse_connect_timeout_seconds,
            read=None,
            write=self._settings.hermes_sse_connect_timeout_seconds,
            pool=self._settings.hermes_sse_connect_timeout_seconds,
        )
        billed = False
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream(
                    "GET", url, headers=self._bearer_headers(endpoint.api_key)
                ) as response,
            ):
                if not 200 <= response.status_code < 300:
                    logger.warning("hermes events non-2xx status=%s", response.status_code)
                    raise UpstreamError("hermes events stream failed")
                async for block, raw in _iter_sse_blocks(response):
                    # Relay the raw bytes verbatim to the client (no re-encoding drift).
                    yield raw
                    if not billed and _is_run_completed(block):
                        billed = await self._bill_completed(
                            user_id=user_id, run_id=run_id, event=block
                        )
        except httpx.HTTPError as exc:
            # A mid-stream transport drop: the client connection ends. Billing idempotency by
            # run_id lets a re-subscription complete the debit later (ADR-045 §6, Q-047-2).
            logger.warning("hermes events transport error (stream ended)")
            raise UpstreamError("hermes events stream error") from exc

    async def _bill_completed(self, *, user_id: uuid.UUID, run_id: str, event: _SseEvent) -> bool:
        """Debit the wallet for a completed run's usage. Returns True if billing was attempted.

        Idempotent by ``run_id`` (ADR-047 §4): a duplicate/replayed ``run.completed`` debits once.
        Zero usage ⇒ no debit (amount 0). Insufficient balance: ``consume`` rolls back its savepoint
        (no debit, no orphan row, balance untouched — ADR-047 §6) and the uncharged delta is
        recorded as a ``billing_debit_insufficient`` audit event (not a ledger row); the relay is
        never broken (the run already completed upstream; Q-047-2 / TD-029 reconciliation).
        """
        usage = _extract_usage(event)
        input_tokens = _as_int(usage.get("input_tokens"))
        output_tokens = _as_int(usage.get("output_tokens"))
        total_tokens = _as_int(usage.get("total_tokens"))
        model = _extract_model(event)
        amount = usage_to_credits(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            credits_per_1k_input=self._settings.credits_per_1k_input,
            credits_per_1k_output=self._settings.credits_per_1k_output,
        )
        if amount <= 0:
            logger.info("agent run completed with zero usage, no debit run_id=%s", run_id)
            return True

        meta: dict[str, Any] = {
            "source": "agent_run",
            "runId": run_id,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "model": model,
        }
        try:
            result = await self._wallet.consume(
                user_id=user_id,
                amount=amount,
                idempotency_key=run_id,
                meta=meta,
            )
        except InsufficientCreditsError:
            # Balance too low for the run's usage. consume already rolled back its savepoint
            # (ADR-047 §6): no debit ledger row, balance untouched, no orphan row. Do NOT break the
            # SSE relay (run already completed upstream). Record the uncharged delta as an audit
            # event — NOT a ledger row — so real usage is not silently lost (reconciliation
            # deferred, Q-047-2 / TD-029). No secrets in payload (runId/usage/model/amount/balance).
            await self._record_insufficient(
                user_id=user_id,
                run_id=run_id,
                amount=amount,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            # Streaming-context persistence (ADR-047 §6): _bill_completed runs INSIDE the
            # StreamingResponse body generator, AFTER FastAPI has already torn down the request
            # session dependency (session_scope yield → commit). So the outer transaction is never
            # committed by the teardown for this path. consume()'s begin_nested() only released a
            # savepoint (savepoint release ≠ transaction commit); the billing_debit_insufficient
            # audit row would be lost. Commit the SAME session that recorded it. Idempotent by
            # runId: a replay re-commits the same (or no) state harmlessly. The chat path is
            # unaffected — it bills in the plain POST handler whose session_scope teardown commits.
            await self._session.commit()
            return True
        except Exception:
            # Never break the SSE relay on a non-insufficient billing failure: the run is already
            # done upstream. Roll back any partial/dirty state so it is not carried into the rest of
            # the stream (no commit — there is nothing to persist on this path). Generic log, no
            # secrets.
            logger.warning("agent run billing failed run_id=%s", run_id)
            await self._session.rollback()
            return True
        # Streaming-context persistence (ADR-047 §6): see the InsufficientCreditsError branch
        # above — _bill_completed runs inside the StreamingResponse body generator, after the
        # session dependency teardown has already committed/closed this request, so the debit
        # savepoint
        # released by consume() (begin_nested) is never committed by the teardown. Commit the SAME
        # session through which consume() INSERTed the debit so the ledger row + billing_debit audit
        # persist. Idempotent by runId (ADR-047 §4): a replayed run.completed hits ON CONFLICT (no
        # new row) and this commit is a harmless no-op.
        await self._session.commit()
        logger.info(
            "agent run billed run_id=%s amount=%d replay=%s",
            run_id,
            amount,
            result.idempotent_replay,
        )
        return True

    async def _record_insufficient(
        self,
        *,
        user_id: uuid.UUID,
        run_id: str,
        amount: int,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        """Record the uncharged agent-run delta as a ``billing_debit_insufficient`` audit event.

        Audit-only (append-only audit_logs), NOT a ledger row (ADR-047 §6): the financial ledger
        stays clean and reconcilable while the real usage is captured for later reconciliation
        (Q-047-2 / TD-029). Payload carries runId/usage/model/required amount/current balance and no
        secrets (redaction guard in AuditService also enforces this).
        """
        balance = await self._wallet.current_balance(user_id)
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BILLING_DEBIT_INSUFFICIENT,
                payload={
                    "runId": run_id,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    },
                    "model": model,
                    "amount": amount,
                    "balance": balance,
                },
            )
        )
        logger.info(
            "agent run billing insufficient run_id=%s amount=%d balance=%d",
            run_id,
            amount,
            balance,
        )

    # --- approval / stop passthrough --------------------------------------------------------

    async def approval(
        self, *, user_id: uuid.UUID, run_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Passthrough ``POST {base}/v1/runs/{runId}/approval`` (ADR-045 §3)."""
        return await self._passthrough_post(user_id, f"/v1/runs/{run_id}/approval", body)

    async def stop(self, *, user_id: uuid.UUID, run_id: str) -> dict[str, Any]:
        """Passthrough ``POST {base}/v1/runs/{runId}/stop`` (ADR-045 §3)."""
        return await self._passthrough_post(user_id, f"/v1/runs/{run_id}/stop", None)

    async def _passthrough_post(
        self, user_id: uuid.UUID, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        """POST to the user's instance; relay the JSON body. 502 on transport/non-2xx failure."""
        endpoint = await self._manager.ensure_running(user_id)
        url = f"{endpoint.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.hermes_proxy_timeout_seconds
            ) as client:
                response = await client.post(
                    url, json=body, headers=self._bearer_headers(endpoint.api_key)
                )
        except httpx.HTTPError as exc:
            logger.warning("hermes passthrough transport error path=%s", path)
            raise UpstreamError("hermes instance unreachable") from exc
        if not 200 <= response.status_code < 300:
            logger.warning(
                "hermes passthrough non-2xx path=%s status=%s", path, response.status_code
            )
            raise UpstreamError("hermes request failed")
        try:
            data = response.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _bearer_headers(api_key: str) -> dict[str, str]:
        """Authorization header for the instance. Never logged (redaction `*authorization*`)."""
        return {"Authorization": f"Bearer {api_key}"}


@dataclass(frozen=True)
class _SseEvent:
    """A parsed SSE block: the optional ``event:`` name and the decoded ``data:`` JSON (if any)."""

    name: str | None
    data: dict[str, Any]


async def _iter_sse_blocks(
    response: httpx.Response,
) -> AsyncIterator[tuple[_SseEvent, bytes]]:
    """Yield (parsed_event, raw_block_bytes) for each SSE block of the stream.

    Splits on the blank-line block separator. The raw bytes (including the trailing ``\\n\\n``) are
    forwarded to the client verbatim so relaying never mutates the wire format; the parsed event is
    used only to detect terminal billing events. Malformed/partial blocks are forwarded raw with an
    empty parsed payload (the relay must not drop bytes on a parse miss).
    """
    buffer = b""
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            raw = block + b"\n\n"
            yield _parse_sse_block(block), raw
    if buffer.strip():
        # Trailing block without a terminating blank line (stream closed): forward + parse.
        yield _parse_sse_block(buffer), buffer


def _parse_sse_block(block: bytes) -> _SseEvent:
    """Parse a single SSE block into an :class:`_SseEvent` (name + data JSON). Never raises."""
    name: str | None = None
    data_lines: list[str] = []
    for line_bytes in block.split(b"\n"):
        try:
            line = line_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if line.startswith(":"):
            continue  # SSE comment / keepalive
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    data: dict[str, Any] = {}
    if data_lines:
        try:
            parsed = json.loads("\n".join(data_lines))
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = {}
    return _SseEvent(name=name, data=data)


def _event_name(event: _SseEvent) -> str | None:
    """Resolve the event name from the SSE ``event:`` field or a ``type``/``event`` data field."""
    if event.name:
        return event.name
    for key in ("type", "event"):
        value = event.data.get(key)
        if isinstance(value, str):
            return value
    return None


def _is_run_completed(event: _SseEvent) -> bool:
    return _event_name(event) == _EVENT_RUN_COMPLETED


def _extract_usage(event: _SseEvent) -> dict[str, Any]:
    usage = event.data.get("usage")
    return usage if isinstance(usage, dict) else {}


def _extract_model(event: _SseEvent) -> str | None:
    model = event.data.get("model")
    return model if isinstance(model, str) else None


def _as_int(value: Any) -> int:
    """Coerce a usage token count to int; non-int/missing → 0 (robust vs upstream drift)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
