"""Agent proxy service: launch, SSE relay + billing, approval/stop passthrough (ADR-045/047).

Owns the proxy logic so the router stays thin and the flow is unit-testable with a mocked Hermes
instance (respx/httpx) and a mocked ``HermesInstanceManager``. Instance lifecycle and the
``API_SERVER_KEY`` are owned by ``hermes_runtime`` (ADR-046); the decrypted key lives only in the
``InstanceEndpoint`` returned by ``ensure_running`` and is never logged or relayed to the client.

SSE wire format (Hermes external contract): blocks of ``data: <json>\\n\\n`` — the patched
production image (ADR-065) emits NO ``event: <name>`` header line, the event type travels in the
JSON field ``"event"`` (per a raw SSE capture of a prod run; the canonical statement lives in
docs/modules/agent-proxy/05-events.md). Dispatch therefore goes through :func:`_event_name`, which
reads the JSON ``event``/``type`` field and treats the SSE header as an optional fallback only.
The relay forwards every event byte-for-byte to the client and, on the terminal ``run.completed``
carrying ``usage``, debits the wallet exactly once (idempotency by ``runId``, ADR-047 §4).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_proxy.billing import usage_to_credits
from app.agent_proxy.broker import AgentRunBroker, Cursor
from app.agent_proxy.runs_repo import AgentRunsRepository
from app.agent_proxy.snapshots_repo import AgentRunSnapshotsRepository, SnapshotUpsertResult
from app.audit.service import (
    EVENT_BILLING_DEBIT_INSUFFICIENT,
    AuditEvent,
    AuditService,
)
from app.config import Settings
from app.errors import (
    InsufficientCreditsError,
    NotFoundError,
    ResumeInProgressError,
    RunNotResumableError,
    SessionExpiredError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.hermes_runtime.manager import HermesInstanceManager, InstanceEndpoint
from app.observability.logging import log_event
from app.observability.metrics import (
    agent_run_launch_upstream_timeout_total,
    agent_run_orphan_finalized_total,
)
from app.policy.engine import Decision, Mode, evaluate
from app.policy.loader import load_policy_state
from app.wallet.service import WalletService

if TYPE_CHECKING:  # circular at runtime: consumer.py imports this module for its domain rules
    from app.agent_proxy.consumer import ConsumerLauncher

logger = logging.getLogger("app.agent_proxy.service")

# audit eventType for an agent run (audit catalog; agent-proxy/05-events.md). billing_debit is
# recorded by WalletService.consume itself; this marks the run lifecycle.
EVENT_AGENT_RUN = "agent_run"
# ADR-067 §5: the reaper finalized a run whose consumer disappeared.
EVENT_ORPHAN_FINALIZED = "agent_run_orphan_finalized"
# ADR-067 §5.2 billingBasis — the ground the sweep charged (or did not charge) a run on. Reported
# both in the audit payload and as the metric label; see :func:`_orphan_billing_basis`.
BILLING_BASIS_SNAPSHOT = "snapshot"
BILLING_BASIS_ZERO_USAGE = "zero_usage"
BILLING_BASIS_NO_SNAPSHOT = "no_snapshot"
# Q-067-17: the log event carrying the identifiers the metric may not (userId, runId, duration).
EVENT_LAUNCH_UPSTREAM_TIMEOUT = "agent_run_launch_upstream_timeout"
# Phases of the launch path, the BOUNDED label set of agent_run_launch_upstream_timeout_total.
_PHASE_CONNECT = "connect"
_PHASE_READINESS = "readiness"
_PHASE_LAUNCH = "launch"
_PHASE_HYDRATE = "hydrate"
_PHASE_BUDGET = "budget"

# Terminal SSE event name that triggers billing (Hermes external contract, agent-proxy/05-events).
_EVENT_RUN_COMPLETED = "run.completed"
# ADR-066 §3: terminal failure. No debit (no usage) — but it DOES flush the snapshot and record
# agent_runs.status='failed', which before ADR-066 was never written (a crashed run stayed
# 'running' forever).
_EVENT_RUN_FAILED = "run.failed"
# ADR-066 §6: the run is waiting for the user to answer an approval request. Persisted so the
# derived client status waiting_approval survives an SSE drop / app kill.
_EVENT_APPROVAL_REQUEST = "approval.request"
# ADR-064 §7: per-LLM-call usage event emitted inside the Hermes tool-loop (image patch). Carries
# cumulative_input_tokens/cumulative_output_tokens (billing source) + step_index (per-step key).
_EVENT_USAGE_DELTA = "usage.delta"
# ADR-064 §3: assistant text delta and tool events buffered locally to build the run.paused body.
_EVENT_MESSAGE_DELTA = "message.delta"
# Token-count field sets of a usage carrier, probed IN ORDER (_extract_usage_counts). CUMULATIVE
# FIRST — per-step deltas read as run totals would UNDER-bill; the cumulative anchors are the total.
_USAGE_FIELD_SETS = (
    (
        "cumulative",
        "cumulative_input_tokens",
        "cumulative_output_tokens",
        "cumulative_total_tokens",
    ),
    ("per_step", "input_tokens", "output_tokens", "total_tokens"),
)
# How deep the run.completed billing gate looks for ANY mention of usage/tokens before declaring a
# block provably usage-free (_is_provably_usage_free). Not a parsing depth — carriers are still read
# only where they are known to live; this is purely "could this block be carrying usage at all?".
# Generous next to the two layouts ever observed (top level, one nesting level), bounded so a
# pathological payload cannot turn the gate into a deep walk.
_USAGE_GATE_MAX_DEPTH = 4
# Upper bound on the key paths a diagnostic log line may carry (_shape_summary). Enough to describe
# any plausible usage payload, small enough that a hostile block cannot turn a warning into a dump.
_SHAPE_SUMMARY_MAX_KEYS = 40
# Consecutive usage.delta events whose cumulative anchors did not move before the relay says so
# (_note_anchor_progress). Above the benign reasons a single step may not advance (a duplicate
# event, a step that genuinely added nothing), below the length of any real run — a frozen anchor
# must not survive to the end of the stream unreported.
_USAGE_ANCHOR_STALL_WARN_AFTER = 5
# Known text carriers of a message.delta, probed IN ORDER (_extract_delta_text). ``delta`` is a bare
# string on the ADR-065 production image and a {text: …} wrapper on other builds — both accepted.
_DELTA_TEXT_KEYS = ("text", "delta", "content")
# How many text-less message.delta events end the benefit of the doubt: past this count with ZERO
# characters extracted for the whole relay, the aggregate latch fires (ADR-065 regression guard).
# Small enough to catch the defect within the first second of a run, large enough that a run whose
# opening deltas are genuinely empty does not warn.
_DELTA_SILENT_WARN_AFTER = 10
# Envelope/metadata fields OBSERVED on a real message.delta block (event/run_id/timestamp) plus the
# obvious aliases. Used ONLY by the shape-drift heuristic (_delta_shape_looks_unknown): `event` and
# `run_id` are non-empty strings on every single delta, so without this exclusion the heuristic
# would fire on every legitimately empty one. Kept as SHORT as possible — every entry is a key the
# heuristic will ignore, i.e. a place a renamed text carrier could hide. `status`/`reason` are
# deliberately NOT here: they never occur on a message.delta (they are run.paused fields) and are
# among the most plausible prose carriers, so excluding them would only widen the blind spot.
_DELTA_ENVELOPE_KEYS = frozenset(
    {
        *_DELTA_TEXT_KEYS,
        "event",
        "type",
        "run_id",
        "runId",
        "id",
        "session_id",
        "sessionId",
        "model",
        "role",
        "index",
        "timestamp",
    }
)
_TOOL_EVENT_PREFIX = "tool."
# ADR-064 §3: synthetic terminal event generated by the control plane (NOT Hermes) on pause.
_EVENT_RUN_PAUSED = "run.paused"
# ADR-064 §3: pause reason when the wallet balance is exhausted mid-run.
_REASON_CREDITS_EXHAUSTED = "credits_exhausted"

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

# Smallest slice of the end-to-end budget worth spending on one more HTTP attempt. Below it the
# attempt cannot plausibly complete a connect + response, so starting it only delays a 502 that is
# already inevitable — the whole point of the budget is that the client gets its answer AT the
# deadline, not a second past it. Also the retry gate: a retry is only started when the backoff
# plus this window still fit.
_MIN_ATTEMPT_SECONDS = 1.0


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


@dataclass(frozen=True)
class RunResumeResult:
    """Outcome of ``POST /v1/agent/runs/{runId}/resume`` (ADR-064 §5).

    ``blocked`` (HTTP 200) carries ``block_reason`` and no run ids (policy still blocks after the
    top-up gate). An allowed resolve (HTTP 202) carries the NEW child ``run_id`` and the
    ``continued_from`` parent id. Technical failures (404/409/502) are raised as exceptions.
    """

    blocked: bool
    block_reason: str | None = None
    run_id: str | None = None
    continued_from: str | None = None


# Client-facing run status of GET .../state (ADR-066 §4). Derived on read from agent_runs.status
# (+ pending approval); the DB enum agent_run_status is NOT extended. `queued` is not emitted in v1
# (forward-compat member only).
ClientRunStatus = Literal[
    "queued", "running", "waiting_approval", "paused", "completed", "failed", "stopped"
]

# Terminal DB statuses → client status. running/resumed are handled separately (they depend on the
# pending approval), `cancelled` is renamed to the client vocabulary (`POST /stop` → `stopped`).
_CLIENT_STATUS_BY_DB: dict[str, ClientRunStatus] = {
    "paused": "paused",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "stopped",
}


@dataclass(frozen=True)
class RunStateView:
    """Read-only state snapshot of a run for ``GET /v1/agent/runs/{runId}/state`` (ADR-066 §5).

    Assembled from ``agent_runs`` (lifecycle: status, session, resume chain, pause reason) plus the
    optional ``agent_run_snapshots`` row (UX state: text, tool, approval, tokens). ``status`` is
    already the CLIENT-facing value (see :func:`map_client_status`).
    """

    run_id: str
    session_id: str
    status: ClientRunStatus
    result_text: str
    last_tool: str | None
    pending_approval: dict[str, Any] | None
    block_reason: str | None
    input_tokens: int
    output_tokens: int
    updated_at: datetime.datetime
    continued_from: str | None


@dataclass
class _RelayState:
    """Relay-local accumulation for the run snapshot and the synthetic ``run.paused`` body.

    Lives for one ``stream_events`` call. Accumulated ALWAYS — outside the
    ``agent_incremental_billing_enabled`` branch (ADR-066 §6): the snapshot is written regardless of
    the billing flag, and with the flag OFF the token counters are filled from ``run.completed``
    instead of ``usage.delta``.
    """

    # message.delta pieces. COLLAPSED into a single head-truncated element on every flush, so the
    # buffer is bounded by AGENT_STATE_RESULT_TEXT_MAX_CHARS regardless of run length and the join
    # never walks an ever-growing list. Consequence: the run.paused `output` is bounded by the same
    # cap — acceptable, that body is a convenience snapshot, not an authoritative transcript.
    partial_text: list[str] = field(default_factory=list)
    # Raw tool.* payloads — the `steps` array of the synthetic run.paused body (ADR-064 §3).
    # Bounded by the number of tool calls of a single run (units-to-hundreds), not by stream volume.
    steps: list[dict[str, Any]] = field(default_factory=list)
    last_tool: str | None = None
    # The relay's belief about a pending approval. Asserted to the DB only on immediate flushes —
    # the client can answer POST …/approval out of band, so a throttled text flush must not
    # resurrect a stale value (see _flush_snapshot).
    pending_approval: dict[str, Any] | None = None
    # Cumulative token counters (monotonic; also the run.paused usage anchors).
    input_tokens: int = 0
    output_tokens: int = 0
    # time.monotonic() of the last DB flush; 0.0 => the first delta flushes immediately.
    last_flush_at: float = 0.0
    # One-shot latches: each anomaly is logged ONCE per relay (on transition), not per flush —
    # a frozen text or a tenancy collision persists for the rest of the stream and would otherwise
    # emit a line every few seconds for the whole run.
    tenancy_skip_logged: bool = False
    text_frozen_logged: bool = False
    # Per-event shape probe: a single message.delta looked wrong (see _delta_shape_looks_unknown).
    # Logged ONCE per relay, KEY NAMES only — the payload is user content and must not be logged.
    delta_shape_unknown_logged: bool = False
    # AGGREGATE regression guard (ADR-065). The per-event probe is a heuristic and can miss — the
    # original defect was a TYPE change under a KNOWN key, which no per-event rule flagged. These
    # two counters make the failure impossible to hide: whatever the cause, "many deltas arrived and
    # the run text is still empty" is the symptom that mattered, and it is checked directly.
    delta_events: int = 0
    delta_text_seen: bool = False
    delta_silent_logged: bool = False
    # Usage anomalies are latched per RELAY, not per event: usage.delta arrives once per step, so an
    # unlatched line would repeat for the whole run and stop being read (see _warn_usage_anomalies).
    # Keyed by (event kind, anomaly) — a per-step anomaly must NOT swallow the same anomaly on the
    # terminal event: with incremental billing off that one is the run's only debit.
    usage_anomalies_logged: set[tuple[str, str]] = field(default_factory=set)
    # Consecutive usage.delta events that did NOT move the cumulative anchors. Frozen anchors are
    # invisible to any per-event check (every single event looks fine) yet stop billing dead.
    usage_events_since_anchor_advance: int = 0

    def latch_usage_anomaly(self, kind: str, anomaly: str) -> bool:
        """Claim the single log line this ``(kind, anomaly)`` gets per relay. True on the first."""
        key = (kind, anomaly)
        if key in self.usage_anomalies_logged:
            return False
        self.usage_anomalies_logged.add(key)
        return True


@dataclass
class _RunProcessing:
    """Everything the domain rules of ONE run carry between events (ADR-064/065/066).

    Lives for the length of one upstream subscription — the background consumer's working task
    (ADR-067 §6.1) or, until the broker switch lands, the client relay. Bundling the three
    cross-event values that used to be loop locals (``charged``, ``billed`` and the accumulation
    buffer) is what lets the per-event rules live in one method that either contour can drive.
    """

    user_id: uuid.UUID
    run_id: str
    # ADR-064: incremental (per-step) billing vs. the post-hoc single debit on run.completed. Read
    # ONCE per run, not per event: a flag flip mid-run would otherwise split one run across two
    # billing regimes and break the telescoping invariant (ADR-064 §1).
    incremental: bool
    # Credits already debited for this run, seeded from the ledger (see new_run_processing).
    charged: int
    # The post-hoc debit happened. Guards against a replayed run.completed billing twice — the
    # ledger idempotency key would catch it anyway, this avoids the round-trip.
    billed: bool = False
    state: _RelayState = field(default_factory=_RelayState)


@dataclass(frozen=True)
class _UsageCounts:
    """Token counts pulled off a usage-carrying event + whether a known carrier actually matched.

    ``recognised=False`` means NO probed shape was found — the counts are zeros that mean "unknown",
    not zeros that mean "free". The distinction is the whole point: billing treats both the same
    (``owed=0``), so only this flag lets the relay say so out loud (see _extract_usage_counts).
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    recognised: bool
    # Exactly one of the two counts was readable: the other half is a zero we INVENTED, not one the
    # upstream reported. Billing still uses what it has (losing half beats losing all), but says so.
    partial: bool = False
    # Labels of every FULL match folded into the counts above (``carrier.field_set``).
    sources: tuple[str, ...] = ()
    # Two carriers reported DIFFERENT values for the SAME field set (e.g. `usage.cumulative` vs
    # `top.cumulative`). The fold takes a maximum, so the larger one is what gets billed — fine
    # while the counters are what we think they are, a silent overcharge if they are not. Reported,
    # never resolved here: only a human can say which carrier is authoritative. A difference ACROSS
    # field sets is normal (a per-step delta is not a running total) and never sets this.
    divergent: bool = False
    # A per-step value exceeded the cumulative total it should be contained in. Not a disagreement
    # between carriers but a broken invariant: one of the two is not the quantity it claims to be.
    non_monotonic: bool = False


def map_client_status(db_status: str, *, has_pending_approval: bool) -> ClientRunStatus:
    """Map ``agent_runs.status`` (+ approval presence) to the client status (ADR-066 §4). Pure.

    ``running``/``resumed`` → ``running``, or ``waiting_approval`` when an approval is pending —
    a DERIVED status computed on read, deliberately NOT a value of the ``agent_run_status`` enum
    (a DB enum migration is more expensive than a mapping, and duplicating the status into the
    snapshot would create a second source of truth). ``resumed`` belongs to the PARENT row after a
    resume — the work continues in the child run whose id ``/resume`` returned — so it maps to
    ``running`` too. ``cancelled`` → ``stopped`` (the client vocabulary follows ``POST …/stop``;
    the server enum value is not renamed). ``queued`` is never emitted in v1 (forward-compat only).
    """
    if db_status in ("running", "resumed"):
        return "waiting_approval" if has_pending_approval else "running"
    # paused/completed/failed pass through, cancelled becomes stopped. An unknown value can only
    # come from a future enum member; degrade to `running` rather than emit an off-contract status.
    return _CLIENT_STATUS_BY_DB.get(db_status, "running")


def _timeout_phase(phase: str, exc: UpstreamTimeoutError) -> str:
    """Refine a launch-path timeout into the Q-067-17 phase enum, from what actually timed out.

    The distinction the tripwire needs is "the instance never accepted the connection" versus "it
    accepted and then said nothing" versus "our own end-to-end budget ran out first" — the first two
    are properties of the instance, the third is a property of our deadline and would otherwise be
    filed as instance muteness it does not evidence.

    Read from ``__cause__`` because that is where the code already records it: a connect-phase httpx
    timeout, any other httpx timeout (read/write — post-send silence), or nothing httpx-shaped at
    all, which on these paths means the budget itself expired (``asyncio.timeout``'s builtin
    ``TimeoutError``, or ``_remaining`` refusing to start an attempt that cannot fit).
    """
    cause = exc.__cause__
    if isinstance(cause, httpx.ConnectTimeout | httpx.PoolTimeout):
        return _PHASE_CONNECT
    if isinstance(cause, httpx.TimeoutException):
        return phase
    return _PHASE_BUDGET


def _orphan_billing_basis(*, snapshot_present: bool, input_tokens: int, output_tokens: int) -> str:
    """Which of the three §5.2 grounds the sweep is finalizing a run on. Pure.

    Zero credits owed has three completely different meanings, and before ADR-067 §5.2 the sweep
    reported the same silent zero for all of them:

    * ``no_snapshot`` — there is no snapshot row, i.e. the consumer never made its FIRST DB call.
      Nothing about this run was ever observed, so the zero is the absence of a measurement rather
      than one. §5.2 calls this a revenue incident: after the §4.1 fix its main cause (Redis down
      at start) is gone, so whatever remains is a defect — read on its own, never averaged in.
    * ``zero_usage`` — a row exists and reports zero: the run really was observed and really did end
      before its first ``usage.delta``.
    * ``snapshot`` — a non-zero cumulative was observed; ordinary best-effort finalization.
    """
    if not snapshot_present:
        return BILLING_BASIS_NO_SNAPSHOT
    if input_tokens == 0 and output_tokens == 0:
        return BILLING_BASIS_ZERO_USAGE
    return BILLING_BASIS_SNAPSHOT


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
        runs: AgentRunsRepository,
        snapshots: AgentRunSnapshotsRepository,
        consumers: ConsumerLauncher | None = None,
        broker: AgentRunBroker | None = None,
    ) -> None:
        self._session = session
        self._manager = manager
        self._wallet = wallet
        self._audit = audit
        self._settings = settings
        self._runs_repo = runs
        self._snapshots_repo = snapshots
        # ADR-067: None => the background consumer contour is off (kill-switch, or a unit test
        # exercising the domain rules alone). The launch path then behaves exactly as before.
        self._consumers = consumers
        # ADR-067: None => kill-switch off / contour unwired => the legacy direct relay below runs
        # UNCHANGED, billing, snapshot and status included. That is the rollback path.
        self._broker = broker

    # --- end-to-end request budget ------------------------------------------------------------

    def _budget_deadline(self) -> float:
        """``time.monotonic()`` instant by which a proxied request must have an answer.

        Taken ONCE at an entry point and threaded through every phase — instance resolution
        (``ensure_running``: provision/wake + the ADR-056/ADR-062 readiness poll), the HTTP call
        itself and every ADR-062 connect-retry — so the phases share ONE budget instead of stacking
        their own. Before this, ``HERMES_PROXY_TIMEOUT_SECONDS`` bounded a single attempt only:
        a ~90s readiness gate followed by 3 × 30s of launch attempts plus backoffs left the client
        with no response at all for minutes (TD-040, measured in prod against a wedged instance,
        which answers neither the request nor a health probe — silence, not a transport error, so
        nothing else in the path could have ended the wait).

        SCOPE, precisely — this bounds the time spent WAITING ON THE INSTANCE (lock wait, readiness
        polls, HTTP attempts), with three documented gaps:

        1. Post-deadline cleanup still runs (a final health probe, a best-effort container
           stop/remove — ADR-056 §1, ADR-062 §1b). Bounded, but outside the budget.
        2. The Docker calls of TD-041 are untimed and outside it entirely.
        3. httpx timeouts are PER-OPERATION, not cumulative: ``read`` restarts on every chunk
           received, so an upstream dripping one byte every 29s never trips a 30s read timeout and
           would outlive any purely phase-based bound. That is why each non-streaming attempt is
           ALSO wrapped in ``asyncio.timeout(remaining budget)`` (see :meth:`_launch_run`) — the
           phase caps decide the normal cases quickly, the wrapper makes the budget a real ceiling
           rather than a hopeful one.

        (1) and (2) mean the client's observed latency can still exceed the budget by a bounded
        amount; (3) is closed.
        """
        return time.monotonic() + self._settings.hermes_launch_budget_seconds

    def _remaining(self, deadline: float, *, phase: str) -> float:
        """Seconds left of the budget, or :class:`UpstreamTimeoutError` if an attempt cannot fit.

        Refusing below ``_MIN_ATTEMPT_SECONDS`` is what turns an exhausted budget into an immediate
        deterministic 502 instead of a token attempt nobody is waiting for any more.
        """
        left = deadline - time.monotonic()
        if left < _MIN_ATTEMPT_SECONDS:
            logger.warning("hermes %s budget exhausted before the attempt", phase)
            raise UpstreamTimeoutError("hermes instance did not answer within the request budget")
        return left

    def _attempt_timeout(self, left: float) -> httpx.Timeout:
        """Per-attempt httpx timeout, with connect bounded SEPARATELY from read/write.

        The split is the fix, not decoration. A single float sets all four phases in httpx, so the
        launch path did have a connect timeout — it had a 30s one. The ADR-062 retry is by
        construction spent ONLY on the connect phase, so that 30s was multiplied by the attempt
        count: 3 × 30 + 2 × 2 = 94s, which is precisely the ≥90s of silence TD-040 measured. ADR-062
        §Последствия priced its own change at ``(attempts-1) × backoff ≤ 4s`` and missed this.
        Connect gets ``HERMES_CONNECT_TIMEOUT_SECONDS`` (an instance is one DNS hop away on the
        docker network: sub-second when healthy), read/write keep the full proxy timeout — a run
        launch may legitimately take seconds to be accepted. ``pool`` follows connect: waiting for a
        free connection is a local-resource wait, not the instance thinking.

        The result is bounded under BOTH unobserved failure modes of a wedged instance (see
        ``unverified_external_assumptions``), and under their mixture, which is the true worst case:
        a connect-refusing mode costs ``attempts × connect + backoffs`` (~34s at defaults); a mode
        that accepts TCP and then goes silent costs ONE proxy timeout (30s — the ReadTimeout is
        post-send and is never retried, ADR-062: double-run risk); an instance that refuses the
        first attempts and accepts the last costs both, ~54s. None of the three depends on which
        mode is real, and all sit under the budget.

        ``left`` is the remaining budget, already validated by :meth:`_remaining`; the caps are
        clamped to it so the last attempt of a nearly-spent budget cannot overrun it.
        """
        connect = min(self._settings.hermes_connect_timeout_seconds, left)
        read_write = min(self._settings.hermes_proxy_timeout_seconds, left)
        return httpx.Timeout(connect=connect, read=read_write, write=read_write, pool=connect)

    @staticmethod
    def _transport_error(phase: str, exc: httpx.HTTPError) -> UpstreamError:
        """Classify an httpx failure into the 502 the client sees. Single rule, every path.

        ``upstream_timeout`` ⟺ the instance said NOTHING within the time it was given
        (:class:`httpx.TimeoutException` — connect, read, write or pool). Everything else — refused,
        reset, DNS, protocol — is the instance (or the network) answering in the negative and stays
        the generic ``upstream_error``. The two are operationally different: silence means "still
        booting / wedged, retry later", a refusal means "something is broken now".
        """
        if isinstance(exc, httpx.TimeoutException):
            logger.warning("hermes %s timed out (no answer)", phase)
            return UpstreamTimeoutError("hermes instance did not answer within the request budget")
        logger.warning("hermes %s transport error", phase)
        return UpstreamError("hermes instance unreachable")

    @contextlib.asynccontextmanager
    async def _launch_timeout_probe(
        self,
        phase: str,
        *,
        user_id: uuid.UUID,
        run_id: str | None = None,
        refine: bool = True,
    ) -> AsyncIterator[None]:
        """Observe an ``upstream_timeout`` on the launch path, then re-raise it (Q-067-17).

        The tripwire ADR-067 §5.1 was withdrawn under. Its question — "does a user's instance ever
        actually go mute?" — is answered by ``502 upstream_timeout`` CLUSTERING ON ONE USER, and
        until now nothing in the process recorded either half: no counter for this contour and no
        log line to group by. Both halves are produced here, and they are deliberately separate
        artefacts: the metric carries only the bounded ``phase`` (a userId label would be unbounded
        cardinality plus user content in a metric), the log line carries ``userId``/``runId``/
        duration, which is where identifiers belong. Order of use: the counter says whether to look,
        the log says at whom.

        Purely observational — the exception continues unchanged, so no behaviour depends on this.

        ``refine`` distinguishes the timeouts whose phase can be read off the cause from those whose
        cannot. Everything reached through ``_remaining``/``asyncio.timeout`` (the launch and the
        hydrate calls) tells connect-phase silence from a read-phase one and both from the shared
        budget expiring; ``ensure_running`` raises its own readiness verdict with no such cause, and
        guessing at one would only mislabel it.
        """
        started = time.monotonic()
        try:
            yield
        except UpstreamTimeoutError as exc:
            observed = _timeout_phase(phase, exc) if refine else phase
            agent_run_launch_upstream_timeout_total.labels(phase=observed).inc()
            log_event(
                logger,
                logging.WARNING,
                EVENT_LAUNCH_UPSTREAM_TIMEOUT,
                phase=observed,
                userId=str(user_id),
                runId=run_id,
                durationSeconds=round(time.monotonic() - started, 3),
            )
            raise

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

        Steps (2) and (3) share ONE end-to-end deadline (``HERMES_LAUNCH_BUDGET_SECONDS``, see
        :meth:`_budget_deadline`), so the launch path always terminates: 502 ``upstream_timeout``
        at the budget instead of the unbounded silence a stuck instance used to produce.
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

        # (2) Resolve (provision/wake) the user's Hermes instance. The budget starts HERE, after the
        # DB-only gates above (they cannot hang on the instance) and covers everything that talks to
        # it: the readiness gate and the launch share one deadline instead of stacking.
        deadline = self._budget_deadline()
        async with self._launch_timeout_probe(_PHASE_READINESS, user_id=user_id, refine=False):
            endpoint = await self._manager.ensure_running(user_id, deadline=deadline)

        # (3) Proxy the launch. Map iOS body → Hermes body (ADR-045 §4). ADR-064 §5: compute a
        # STABLE session_id (client-supplied or a fresh uuid4) and pass it to Hermes so the whole
        # continuation chain shares one session; resume resolves it from the paused run_id (the
        # client never has to store it).
        effective_session_id = session_id or str(uuid.uuid4())
        hermes_body: dict[str, Any] = {"input": message, "session_id": effective_session_id}
        if model is not None:
            hermes_body["model"] = model

        async with self._launch_timeout_probe(_PHASE_LAUNCH, user_id=user_id):
            run_id, status = await self._launch_run(endpoint, hermes_body, deadline=deadline)
        # ADR-066 §3: the ROOT agent_runs row (continued_from NULL) is created ALWAYS — the row is
        # an unconditional LIFECYCLE record, no longer gated by agent_incremental_billing_enabled
        # (with the flag OFF the default configuration used to store no agent runs at all, so
        # neither /state nor any run history existed). The flag keeps gating only the BILLING
        # fields/operations. Committed by the request session_scope teardown (run() is a plain
        # handler, like the audit rows below).
        await self._runs_repo.create_running(
            run_id, user_id, effective_session_id, model, status="running"
        )
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_AGENT_RUN,
                payload={"phase": "launched", "runId": run_id, "status": status},
            )
        )
        # (4) COMMIT BEFORE WAITING (ADR-067 §6.1.1). The wait below is on a background task, and
        # holding this transaction across it would keep a pooled connection and an open transaction
        # for its whole duration — the shape of TD-040, where a lock held across a network call
        # turned into a hang we spent iterations misdiagnosing. Committing first also makes the
        # lifecycle row DURABLE before anything can fail: the orphan reaper (§5) sweeps from
        # agent_runs, so a run that exists only in an uncommitted transaction would be finalized by
        # nobody if the consumer never came up.
        await self._session.commit()
        # (5) Start the consumer and wait BRIEFLY for its subscription (§3): the Hermes stream is
        # one-shot, so the server must be the one holding it. Failure is not fatal — 202 is returned
        # regardless (§6.4.1) and the reaper finalizes a run whose consumer never started.
        await self._start_consumer(user_id=user_id, run_id=run_id, endpoint=endpoint)
        return RunLaunchResult(blocked=False, run_id=run_id, status=status)

    async def _start_consumer(
        self, *, user_id: uuid.UUID, run_id: str, endpoint: InstanceEndpoint
    ) -> None:
        """Start the background consumer for a freshly launched run, if the contour is wired.

        Never raises: a launch that already succeeded must not be reported as a failure because its
        consumer did not start (ADR-067 §6.4.1 — the run is answered 202 and the reaper finalizes
        it). The launcher already logs and audits; here we only guarantee that nothing escapes.
        """
        if self._consumers is None:
            return
        try:
            await self._consumers.start_and_wait(user_id=user_id, run_id=run_id, endpoint=endpoint)
        except Exception:  # noqa: BLE001 - see the docstring: the launch itself has succeeded
            logger.warning("agent run consumer failed to start run_id=%s", run_id)

    async def _launch_run(
        self, endpoint: InstanceEndpoint, body: dict[str, Any], *, deadline: float
    ) -> tuple[str, str]:
        """POST {base}/v1/runs with the instance bearer; return (run_id, status). 502 on failure.

        ADR-062 §2 connect-only retry (defense-in-depth for the wake-gap / a transient connect
        blip): on a CONNECT-phase transport error — the request is guaranteed NOT to have reached
        the server — retry up to ``hermes_launch_retry_attempts`` TOTAL attempts with a fixed
        backoff. POST /v1/runs is NOT idempotent (no client key), so ONLY the connect phase is safe
        to retry; any post-send error (write/read/protocol) may have created a run and is re-raised
        immediately as 502 (double-run risk). A fresh ``httpx.AsyncClient`` is used per attempt.

        TWO things bound the cycle, and the FIRST is what fixed TD-040: each attempt's connect phase
        is capped at ``HERMES_CONNECT_TIMEOUT_SECONDS`` rather than at the proxy timeout (see
        :meth:`_attempt_timeout`), so the worst case is ``attempts × connect + backoffs`` ≈ 34s
        instead of ``attempts × 30s`` ≈ 94s. The second is ``deadline`` — the budget SHARED with
        ``ensure_running`` — which stops the cycle from stacking on top of a readiness gate; it is
        the outer safety net, not the mechanism (at defaults it is never the binding constraint on
        this path). A retry is skipped when the backoff plus a usable attempt window no longer fit.

        ADR-062 is intact to the letter: the connect set is unchanged, the connect phase is still
        the ONLY retryable one, a post-send error is still re-raised immediately (double-run risk),
        and the attempt count still caps the retries. What changed is the price of an attempt, not
        which attempts happen. ADR-062 §Последствия priced its own worst case at
        ``(attempts-1) × backoff ≤ 4s`` — true only if connect were cheap, which nothing enforced
        until now; that missing premise is the defect.
        """
        url = f"{endpoint.base_url}/v1/runs"
        # ADR-062 §2: EXPLICIT tuple, NOT a base class. In httpx 0.28.1 ConnectTimeout is NOT a
        # subclass of ConnectError (both connect-phase, but split under
        # TimeoutException/NetworkError respectively); catching
        # TimeoutException/NetworkError/TransportError would also swallow ReadTimeout/WriteError
        # (post-send) → double-run risk. Match strictly the connect set.
        connect_errors = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        attempts = max(self._settings.hermes_launch_retry_attempts, 1)
        backoff = self._settings.hermes_launch_retry_backoff_seconds
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            left = self._remaining(deadline, phase="run launch")
            try:
                # asyncio.timeout is the CEILING; the httpx phase caps are what normally fires.
                # Both are needed: httpx timeouts are per-operation and restart on every chunk, so
                # a drip-feeding upstream satisfies them forever (see _budget_deadline SCOPE §3).
                # Cancelling here is safe in a way it is NOT inside ensure_running: no DB row, lock
                # or container state is in flight, so no cleanup path is skipped. The worst outcome
                # is a run created upstream whose id we never learn — the exact orphan class a
                # ReadTimeout already produces, which ADR-062 handles by never retrying (below).
                async with (
                    asyncio.timeout(left),
                    httpx.AsyncClient(timeout=self._attempt_timeout(left)) as client,
                ):
                    response = await client.post(
                        url, json=body, headers=self._bearer_headers(endpoint.api_key)
                    )
                break
            except TimeoutError as exc:
                # The budget ceiling, not a phase cap. Never retried: the request was in flight, so
                # this is post-send by construction (double-run risk, ADR-062 §2).
                logger.warning("hermes run launch exceeded the request budget attempt=%d", attempt)
                raise UpstreamTimeoutError(
                    "hermes instance did not answer within the request budget"
                ) from exc
            except httpx.HTTPError as exc:
                # Retry ONLY on a connect-phase error with attempts AND budget remaining; else 502.
                budget_left = deadline - time.monotonic()
                retryable = isinstance(exc, connect_errors) and attempt < attempts
                if retryable and budget_left - backoff >= _MIN_ATTEMPT_SECONDS:
                    logger.warning(
                        "hermes run launch connect error, retrying attempt=%d/%d",
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(backoff)
                    continue
                if retryable:
                    logger.warning(
                        "hermes run launch connect error, budget exhausted attempt=%d/%d",
                        attempt,
                        attempts,
                    )
                # The code follows what the LAST error actually was, never why we stopped retrying.
                # A ConnectError is the instance REFUSING (connection refused / reset) — reporting
                # that as `upstream_timeout` just because the budget also happened to run out would
                # invert the distinction errors.py:UpstreamTimeoutError promises. Only a genuine
                # timeout (connect or read) means "it said nothing".
                raise self._transport_error("run launch", exc) from exc
        if response is None:  # pragma: no cover - loop always breaks on success or raises
            # Defensive: the loop breaks on success or raises on the final failed attempt.
            raise UpstreamError("hermes instance unreachable")

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

    # --- domain processing (shared by the background consumer and the legacy relay) ------------

    async def new_run_processing(self, *, user_id: uuid.UUID, run_id: str) -> _RunProcessing:
        """Seed the per-run processing state (ADR-064 §6, ADR-066 §6).

        ``charged`` is re-seeded from the LEDGER so a re-subscription after a dropped stream does
        not re-bill already-charged steps (per-step idempotency guarantees that directly; the seed
        additionally avoids redundant no-op consume calls). Post-hoc mode keeps ``charged == 0``.
        """
        incremental = self._settings.agent_incremental_billing_enabled
        charged = await self._wallet.charged_for_run(user_id, run_id) if incremental else 0
        return _RunProcessing(
            user_id=user_id, run_id=run_id, incremental=incremental, charged=charged
        )

    async def prepare_consumer_snapshot(self, *, user_id: uuid.UUID, run_id: str) -> None:
        """Create the snapshot row when the consumer starts (ADR-067 §6.1). Idempotent.

        Eager, not lazy: the heartbeat is a bare ``UPDATE`` and would match nothing until the first
        event arrives, so a run whose first event is minutes away would look — to the orphan sweep —
        exactly like a run whose consumer never started (see ``snapshots_repo.ensure_row``).
        """
        await self._snapshots_repo.ensure_row(run_id, user_id)
        await self._session.commit()

    async def consumer_heartbeat(self, *, user_id: uuid.UUID, run_id: str) -> bool:
        """One liveness stamp: snapshot heartbeat + instance activity. Returns False if no row.

        Two writes, one meaning "this run is being consumed": ``consumer_heartbeat_at`` is what the
        orphan sweep reads (ADR-067 §5), and ``hermes_instances.last_active_at`` keeps the idle
        reaper from hibernating an instance whose run is still being consumed with no client
        attached — the exact situation this whole contour exists to support.

        ⚠️ The snapshot write is a SINGLE-COLUMN UPDATE and must never become an upsert: the upsert
        moves ``updated_at``, which the client reads as "the state changed" (ADR-066 §5), and a
        heartbeat is written precisely when it did not. Committed here — a background task has no
        request-scoped teardown to do it. Failures propagate: the supervisor decides what a failed
        heartbeat means, this method does not swallow it.
        """
        stamped = await self._snapshots_repo.touch_consumer_heartbeat(run_id)
        await self._manager.touch_active(user_id)
        await self._session.commit()
        return stamped > 0

    async def record_consumer_event(
        self, *, user_id: uuid.UUID, run_id: str, event_type: str
    ) -> None:
        """Audit why a background consumer stopped (ADR-067 §6.4 step 3). Commits.

        Goes through the service — and therefore through one short session like every other
        consumer DB operation — rather than holding an ``AuditService`` bound to a session that
        outlives the run. Carries no user content: the run id and the reason, nothing else.
        """
        await self._audit.record(
            AuditEvent(user_id=user_id, event_type=event_type, payload={"runId": run_id})
        )
        await self._session.commit()

    async def list_orphan_candidates(self, *, timeout_seconds: int, limit: int) -> list[Any]:
        """Candidates for the orphan sweep (ADR-067 §5).

        Read-only, and only condition 2 of the three: the live-lease and Redis-uptime checks belong
        to the caller, which is the only place that can apply them fail-closed.
        """
        return await self._runs_repo.list_orphan_candidates(
            timeout_seconds=timeout_seconds, limit=limit
        )

    async def finalize_orphan_run(
        self,
        *,
        user_id: uuid.UUID,
        run_id: str,
        input_tokens: int,
        output_tokens: int,
        snapshot_present: bool,
        heartbeat_age_seconds: float,
    ) -> None:
        """Best-effort finalization of an abandoned run (ADR-067 §5). Idempotent by ``runId``.

        Order mirrors the normal terminal path (ADR-066 §3): charge what the snapshot's last
        observed cumulative says is owed, then record the status — but here BOTH are best-effort and
        conditional, because this run's outcome was never observed. The debit uses
        ``idempotency_key=runId``, the same key the consumer's own finalization would use, so
        whichever happens first wins and the other is a no-op rather than a double charge. The
        status transition is conditional (``WHERE status IN ('running','resumed')``), so a run that
        finished normally in the meantime is left alone.

        ⚠️ **The REMAINDER is charged, not the whole run** (ADR-067 §5 step 1, TD-042). Subtracting
        ``charged_for_run`` is not belt-and-braces on top of the idempotency key — the key protects
        nothing here: incremental billing (ADR-064 §1) debits under ``runId:<step>`` keys, so the
        bare ``runId`` key is still FREE when the sweep arrives and a full-amount debit goes through
        on top of every step already paid. The run is then billed twice in its entirety. It has been
        latent only because ``AGENT_INCREMENTAL_BILLING_ENABLED`` defaults to false — a condition
        for the defect to appear, not a mitigation.

        ``snapshot_present`` decides ``billingBasis`` (§5.2): a zero that was MEASURED
        (``zero_usage``) and a zero that stands for the absence of any measurement
        (``no_snapshot`` — the consumer never reached its first DB call) are recorded as different
        events, in the audit payload and in the metric. The status is ``failed`` in every case,
        unconditionally: an upper bound on a run's life outranks the precision of its label
        (leaving it ``running`` is TD-037 returning).

        ``heartbeat_age_seconds`` comes from the candidate query, computed against the same
        ``now()`` as the staleness predicate itself (``list_orphan_candidates``), so the audited age
        is the age the decision was actually made on.
        """
        basis = _orphan_billing_basis(
            snapshot_present=snapshot_present,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        observed = usage_to_credits(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            credits_per_1k_input=self._settings.credits_per_1k_input,
            credits_per_1k_output=self._settings.credits_per_1k_output,
        )
        already_charged = await self._wallet.charged_for_run(user_id, run_id)
        if already_charged > observed:
            # INVARIANT BREACH, mirroring the terminal path's own check (:meth:`_bill_completed`)
            # and diagnostic for the same reason: the snapshot is flushed IMMEDIATELY BEFORE each
            # per-step debit (ADR-064 §1), so its cumulative can only ever run AHEAD of the ledger.
            # The ledger showing more than the snapshot means snapshot writes were lost, and the
            # clamp below would otherwise hide it — worse, `basis` then reports the ordinary
            # `zero_usage` for a run whose ledger proves usage did happen.
            logger.warning(
                "agent run orphan ledger exceeds the observed snapshot run_id=%s observed=%d "
                "charged=%d basis=%s",
                run_id,
                observed,
                already_charged,
                basis,
            )
        owed = max(observed - already_charged, 0)
        # ⚠️ What was actually TAKEN, not what was asked for. Reporting the request would overstate
        # the money on three reachable paths: an idempotent replay takes nothing, the ADR-051 debt
        # path takes only the partial balance, and a suppressed shortfall takes nothing at all.
        billed = 0
        if owed > 0:
            try:
                result = await self._wallet.consume(
                    user_id=user_id,
                    amount=owed,
                    idempotency_key=run_id,
                    meta={"source": "agent_run", "orphan": True, "runId": run_id},
                )
                billed = result.charged_amount
            except InsufficientCreditsError:
                # A shortfall must not stop the finalization: the status matters more than the last
                # credits. But it must not be SILENT either — this is the branch reached when
                # AGENT_DEBT_RECONCILE_ENABLED is off, where nothing else records the uncharged
                # usage, unlike the terminal path which has always audited it (ADR-047 §6, the same
                # call ``_bill_completed`` makes). Without this the money simply disappeared from
                # the record: `billed` claimed the full amount and no other row contradicted it.
                await self._record_insufficient(
                    user_id=user_id,
                    run_id=run_id,
                    amount=owed,
                    # Unknown here by construction: the sweep finalizes a run whose events nobody
                    # observed, so there is no model to name and no total the snapshot carries —
                    # the two counts it does carry are passed as they are, and their sum is a
                    # derivation, not a measurement.
                    model=None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                )
        await self._mark_terminal(run_id, "failed")
        await self._audit.record(
            AuditEvent(
                # userId is the audit ROW's own column — not repeated in the payload.
                user_id=user_id,
                event_type=EVENT_ORPHAN_FINALIZED,
                # ADR-067 §5 step 3 in full: the run, the last usage anyone observed, what was
                # charged and how old the heartbeat was.
                #
                # ⚠️ `billed` is what THIS write actually TOOK. The remainder rule (TD-042) already
                # made it narrower than "what the run was worth", and a replay, a partial debt-path
                # debit or a suppressed shortfall narrow it to zero. So a fully pre-billed run and a
                # run whose snapshot writes were lost both read as `billed=0` under
                # `basis="snapshot"`: `observed` and `alreadyCharged` are what separate them
                # afterwards, and the distinction §5.2 introduced `billingBasis` for is not
                # reconstructible from the charged sum alone.
                #
                # The token counts survive redaction by the ADR-049 usage carve-out
                # (observability/redaction.py) — they are integer billing analytics, not secrets.
                payload={
                    "runId": run_id,
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                    "observed": observed,
                    "alreadyCharged": already_charged,
                    "billed": billed,
                    "billingBasis": basis,
                    "heartbeatAgeSeconds": round(heartbeat_age_seconds, 1),
                },
            )
        )
        await self._session.commit()
        # After the commit: the counter describes finalizations that actually happened. Bounded
        # label, no identifiers — those are in the audit row written just above.
        agent_run_orphan_finalized_total.labels(basis=basis).inc()

    async def flush_run_snapshot(self, run: _RunProcessing) -> None:
        """Final immediate snapshot flush for the §6.4 self-termination procedure.

        Bypasses the throttle so text accumulated since the last write is not lost when a run ends
        abnormally. Best-effort by construction (``_flush_snapshot`` swallows and rolls back its own
        DB errors), and the caller additionally bounds it in time: a consumer stopping BECAUSE it
        wedged on a DB write must not wedge again here — dropping the lease (step 2) is what makes
        the run visible to the reaper and outranks saving the last few characters of text.
        """
        await self._flush_snapshot(
            user_id=run.user_id, run_id=run.run_id, state=run.state, immediate=True
        )

    async def process_event(self, run: _RunProcessing, block: _SseEvent) -> bytes | None:
        """Apply the DOMAIN rules of one upstream event: snapshot, status, billing, pause-at-zero.

        Extracted VERBATIM from the client relay loop when ADR-067 moved these duties to the
        background consumer (§2 "меняется исполнитель, не правила"). Every rule the two contours
        share therefore has exactly one implementation: the terminal status written BEFORE billing
        and independently of its outcome (ADR-066 §3), the conditional status transitions, the
        per-column replay-guarded snapshot upsert with its throttle, the usage anomaly latches
        (ADR-065), the telescoping incremental debits (ADR-064 §1) and the post-hoc fallback.

        Returns the synthetic terminal ``run.paused`` block when pause-at-zero fired, else None.
        Returning it rather than emitting it keeps this method free of any transport concern: the
        relay yields it to its client, the consumer publishes it to the ring — and neither can
        forget that the run ENDED here, because the block is also the signal to stop.
        """
        name = _event_name(block)
        if name == _EVENT_MESSAGE_DELTA:
            text_piece = _extract_delta_text(block)
            run.state.delta_events += 1
            if text_piece:
                run.state.delta_text_seen = True
                run.state.partial_text.append(text_piece)
                # Throttled: text is the only high-frequency writer (ADR-066 §6.1).
                await self._flush_snapshot(
                    user_id=run.user_id, run_id=run.run_id, state=run.state, immediate=False
                )
            elif not run.state.delta_shape_unknown_logged and _delta_shape_looks_unknown(block):
                # Per-event probe: this delta's shape looks wrong (unusable known
                # carrier, or text hiding under an unknown key). Latched to one line per
                # relay; KEY NAMES only (payload is user content — ADR-066 §6).
                run.state.delta_shape_unknown_logged = True
                logger.warning(
                    "hermes message.delta text carrier unknown run_id=%s keys=%s",
                    run.run_id,
                    sorted(block.data),
                )
            if (
                not run.state.delta_text_seen
                and not run.state.delta_silent_logged
                and run.state.delta_events >= _DELTA_SILENT_WARN_AFTER
            ):
                # AGGREGATE guard, independent of ANY shape heuristic: this many
                # deltas relayed and still not one character extracted is the exact
                # signature of the ADR-065 defect (resultText and run.paused.output
                # empty for the whole run) — the shape probes above missed it because
                # the carrier key never changed, only its TYPE. A superset of every
                # silent-miss cause, present and future.
                run.state.delta_silent_logged = True
                logger.warning(
                    "hermes message.delta yielded no text for %d events run_id=%s " "last_keys=%s",
                    run.state.delta_events,
                    run.run_id,
                    sorted(block.data),
                )
        elif name is not None and name.startswith(_TOOL_EVENT_PREFIX):
            run.state.steps.append(block.data)
            tool = _extract_tool_name(block)
            if tool:
                run.state.last_tool = tool
            # The agent moved on => any pending approval is resolved (ADR-066 §6).
            run.state.pending_approval = None
            await self._flush_snapshot(
                user_id=run.user_id, run_id=run.run_id, state=run.state, immediate=True
            )
        elif name == _EVENT_APPROVAL_REQUEST:
            run.state.pending_approval = self._build_pending_approval(block)
            # IMMEDIATE, bypassing the throttle: a delayed write would leave the client
            # unaware that the run is waiting for its answer (ADR-066 §6.1).
            await self._flush_snapshot(
                user_id=run.user_id, run_id=run.run_id, state=run.state, immediate=True
            )
        elif name == _EVENT_USAGE_DELTA:
            # Cumulative token anchors — recorded for the snapshot even with billing
            # OFF (a patched image may emit usage.delta regardless). Read through the
            # same union as billing: anchors that go stale on a drift the biller
            # survives would make /state disagree with the ledger for a second reason.
            anchors = _extract_usage_counts(block)
            # Reported HERE, outside `if incremental`, not inside _bill_step: these
            # anchors feed /state whether or not the billing flag is on, so a payload
            # read badly is a defect in both modes. Leaving the report on the billing
            # path made it invisible in exactly the configuration that needs it most —
            # with the flag OFF (the default) run.completed is the run's ONLY debit, so
            # a step-level shape problem would surface only as a wrong final charge.
            self._warn_usage_anomalies(
                run_id=run.run_id,
                kind=_EVENT_USAGE_DELTA,
                usage=anchors,
                state=run.state,
                event=block,
            )
            before = (run.state.input_tokens, run.state.output_tokens)
            run.state.input_tokens = max(run.state.input_tokens, anchors.input_tokens)
            run.state.output_tokens = max(run.state.output_tokens, anchors.output_tokens)
            advanced = (run.state.input_tokens, run.state.output_tokens) != before
            self._note_anchor_progress(run_id=run.run_id, state=run.state, advanced=advanced)
            if advanced:
                # Persist the anchors NOW, bypassing the throttle but WITHOUT asserting
                # approval (ADR-066 §6.2): the step debit below commits immediately, so
                # a stream that drops right here would otherwise leave the ledger
                # charged while /state still reports usage {0,0} — permanently, since no
                # terminal event ever arrives to flush it (Q-047-2). `usage` is
                # contractually monotonic (02-api-contracts.md), and a usage.delta may
                # legitimately arrive after the client answered POST …/approval, so this
                # flush must not speak for the approval run.state.
                #
                # Gated on the anchors actually MOVING: without new anchors there is
                # nothing this write would add (text carries its own throttle), and the
                # once-per-step cadence that makes an unthrottled write affordable is
                # only as good as the capture behind it — ONE usage.delta per 15
                # message.delta. Should the image start emitting per chunk, this gate
                # keeps the write rate tied to real progress instead of event volume.
                await self._flush_snapshot(
                    user_id=run.user_id,
                    run_id=run.run_id,
                    state=run.state,
                    immediate=True,
                    assert_approval=False,
                )
            if run.incremental:
                run.charged, depleted = await self._bill_step(
                    user_id=run.user_id, run_id=run.run_id, event=block, charged=run.charged
                )
                if depleted:
                    # Pause-at-zero: interrupt the run, persist paused, hand back the
                    # synthetic terminal run.paused. The caller emits it and stops —
                    # no run.completed follows (ADR-064 §3).
                    return await self._pause_run(
                        user_id=run.user_id,
                        run_id=run.run_id,
                        charged=run.charged,
                        state=run.state,
                    )
        elif name == _EVENT_RUN_FAILED:
            # No debit (no usage). ADR-066 §3: final flush + a CONDITIONAL failed
            # status — before ADR-066 a crashed run stayed 'running' forever.
            self._warn_if_no_delta_text(run_id=run.run_id, state=run.state)
            run.state.pending_approval = None
            await self._flush_snapshot(
                user_id=run.user_id, run_id=run.run_id, state=run.state, immediate=True
            )
            await self._mark_terminal(run.run_id, "failed")
        elif name == _EVENT_RUN_COMPLETED:
            # Flag OFF => there are no usage.delta events, so the snapshot token
            # counters come from the terminal usage payload (ADR-066 §6).
            # The usage sanity gate lives in _bill_completed, where `owed` is known:
            # an invariant on the BILLED result catches every shape drift, whereas a
            # check on the parsed shape only catches the drifts it was taught.
            usage = _extract_usage_counts(block)
            self._warn_if_no_delta_text(run_id=run.run_id, state=run.state)
            run.state.input_tokens = max(run.state.input_tokens, usage.input_tokens)
            run.state.output_tokens = max(run.state.output_tokens, usage.output_tokens)
            run.state.pending_approval = None
            await self._flush_snapshot(
                user_id=run.user_id, run_id=run.run_id, state=run.state, immediate=True
            )
            # ADR-066 §3: the lifecycle status is recorded HERE, before billing and
            # independently of its outcome. Inside _bill_completed it would be skipped
            # whenever an unexpected billing error hit the generic rollback branch,
            # leaving the run 'running' forever and making /state lie indefinitely.
            # The transition is conditional, so a replayed run.completed is a no-op.
            await self._mark_terminal(run.run_id, "completed")
            if not run.billed:
                run.billed = await self._bill_completed(
                    user_id=run.user_id,
                    run_id=run.run_id,
                    event=block,
                    charged=run.charged,
                    state=run.state,
                )
        # Only pause-at-zero ends a run from inside this method; every other event continues it.
        return None

    # --- SSE relay + billing ----------------------------------------------------------------

    async def stream_events(
        self, *, user_id: uuid.UUID, run_id: str, cursor: Cursor | None = None
    ) -> AsyncIterator[bytes]:
        """Relay the instance SSE stream to the client; bill per-step or once (ADR-047/ADR-064).

        The run is addressed within the user's own instance (RBAC: runId is namespaced to the
        subject's instance, agent-proxy/06-rbac.md), so ``ensure_running(user_id)`` resolves it —
        a foreign run is unreachable by construction. Events are forwarded as-is.

        With ``AGENT_INCREMENTAL_BILLING_ENABLED`` (ADR-064): ``charged`` is seeded from the ledger
        (reconnect-safe), each ``usage.delta`` bills the cumulative-owed-minus-charged delta
        (self-clamping, no debt), and on balance exhaustion the run is stopped and a synthetic
        terminal ``run.paused`` is emitted (no ``run.completed`` follows). On ``run.completed`` the
        remainder (``owed_final - charged``) is finalized. Flag OFF => ``charged`` stays 0 and
        ``run.completed`` bills the full usage once (idempotency_key=run_id) — the ADR-047 behaviour
        unchanged. ``run.failed`` is forwarded without any debit (ADR-047 §4).

        ADR-066 §6 — SNAPSHOT SIDE EFFECT (independent of the billing flag): the same pass upserts
        the run state into ``agent_run_snapshots`` (text/last tool/pending approval/tokens, each
        write committed explicitly — the streaming context runs after the request-session teardown)
        and records terminal statuses in ``agent_runs`` (``completed``/``failed``, conditional).
        This is the ONLY source of ``GET /v1/agent/runs/{runId}/state``: while nobody is subscribed
        here, the snapshot of an active run does not move (a known v1 limitation — a background
        consumer is deferred, ADR-066 §8).
        """
        if self._broker is not None:
            # ADR-067 broker model: the client stream is READ-ONLY and never touches Hermes — the
            # background consumer is the sole upstream subscriber, and it owns billing, the snapshot
            # and the terminal status. This branch therefore performs none of them.
            #
            # ⚠️ RBAC must be asserted EXPLICITLY on this path. On the legacy path below it was
            # implicit: ensure_running(user_id) resolved the caller's OWN instance, so a foreign
            # runId was unreachable by construction. Reading from Redis has no such property —
            # run_id comes straight from the path.
            #
            # DEFENSE IN DEPTH ONLY. The authoritative check runs in the ROUTE HANDLER, because a
            # rejection here cannot produce a 404: Starlette commits the 200 status line before
            # pulling the first item from this generator. Keeping it means a direct caller of
            # stream_events (tests, any future non-HTTP consumer) still cannot read a foreign run —
            # the guarantee does not depend on every caller remembering to pre-check.
            await self.assert_run_owner(user_id=user_id, run_id=run_id)
            # ⛔ RELEASE the request session before streaming, and this is not tidiness. The check
            # above is a read, so the session now holds a pooled connection inside an open
            # transaction — and this generator lives inside a ``StreamingResponse``, whose teardown
            # runs when the STREAM closes — up to ``AGENT_RUN_MAX_DURATION_SECONDS`` (2 h) later.
            # Fifteen concurrent streams would therefore exhaust a worker's pool
            # (``DB_POOL_SIZE + DB_MAX_OVERFLOW``) and every other endpoint of that worker would
            # start failing after ``DB_POOL_TIMEOUT``. A rollback of a read-only transaction returns
            # the connection to the pool and costs nothing; the eventual teardown then finds nothing
            # to commit. Nothing below uses this session — the broker has its own short-lived ones.
            await self._session.rollback()
            async for chunk in self._broker.stream(run_id=run_id, cursor=cursor or Cursor()):
                yield chunk
            return
        # The SETUP phase gets the same end-to-end budget as every other proxied path: waking a
        # stuck instance must not leave the subscriber hanging before a single byte is due. The
        # STREAM itself is deliberately unbounded in read (below) — that is the long-lived part.
        endpoint = await self._manager.ensure_running(user_id, deadline=self._budget_deadline())
        url = f"{endpoint.base_url}/v1/runs/{run_id}/events"
        # Long-lived stream: bound connect/write/pool, disable ONLY the read timeout — a run may
        # legitimately think for minutes between events, but a dead instance must still fail fast
        # at connect, and a blocked pool must not wait forever for a free connection.
        timeout = httpx.Timeout(
            connect=self._settings.hermes_sse_connect_timeout_seconds,
            read=None,
            write=self._settings.hermes_sse_connect_timeout_seconds,
            pool=self._settings.hermes_sse_connect_timeout_seconds,
        )
        run = await self.new_run_processing(user_id=user_id, run_id=run_id)
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
                    paused = await self.process_event(run, block)
                    if paused is not None:
                        # Pause-at-zero produced the synthetic terminal block: emit it and close
                        # the stream (no run.completed follows) — ADR-064 §3.
                        yield paused
                        return
        except httpx.HTTPError as exc:
            # A mid-stream transport drop: the client connection ends. Billing idempotency by
            # run_id (and per-step run_id:step) lets a re-subscription complete billing later
            # (ADR-045 §6, ADR-064 §6, Q-047-2). Classified by the same rule as every other path so
            # the code means one thing everywhere — reachable here only for connect/write/pool
            # (read is disabled by design: this stream times out silence, not death).
            raise self._transport_error("events stream", exc) from exc

    async def _bill_step(
        self, *, user_id: uuid.UUID, run_id: str, event: _SseEvent, charged: int
    ) -> tuple[int, bool]:
        """Bill one ``usage.delta`` (ADR-064 §1): cumulative-owed-minus-charged, self-clamping.

        Returns ``(charged, depleted)``. ``owed = usage_to_credits(cumulative_in, cumulative_out)``
        (the SAME pure function as post-hoc, so the telescoping sum of per-step charges converges
        EXACTLY to ``usage_to_credits(final)`` — no price inflation). ``want = owed - charged``;
        ``charge = min(want, balance)``. A ``charge > 0`` debits via the incremental (no-debt)
        consume path (idempotency ``run_id:step``) and commits (streaming context — the request
        session teardown already ran). ``charged`` and the agent_runs mirror advance by the ACTUAL
        credits decremented (``ConsumeResult.charged_amount`` — may be < ``charge`` if a concurrent
        chat-debit clamped the balance), so both stay consistent with the ledger (money invariant).
        ``depleted = actual < want`` signals a balance shortfall → the caller pauses. A ``charge``
        of 0 (balance already 0) skips the debit and reports depletion. An idempotent replay of the
        step is not double-counted and does not trigger a spurious pause.
        """
        data = event.data
        # Same reader as the terminal path, for the same reason: reading only the flat top level
        # would bill every step at zero if the carrier ever moved or nested — pause-at-zero would
        # then never fire, and with no run.completed nothing would be charged at all. The flat
        # layout IS the one confirmed by the capture (tests/fixtures/hermes_prod_run_adr065.sse:
        # cumulative_* at the top level of usage.delta); the union is drift insurance, not a second
        # assumed contract. The element-wise maximum keeps the cumulative anchors winning, which is
        # what the telescoping invariant needs — per-step deltas must never be read as run totals.
        # Anomalies of this payload are reported by the CALLER, before the billing flag is even
        # consulted (see the usage.delta branch of :meth:`stream_events`): observability must not
        # depend on a billing switch. Reading the event again here keeps billing self-contained —
        # same pure function on the same block, so the two reads cannot disagree.
        usage = _extract_usage_counts(event)
        cumulative_in = usage.input_tokens
        cumulative_out = usage.output_tokens
        step_index = _as_int(data.get("step_index"))
        model = _extract_model(event)
        owed = usage_to_credits(
            input_tokens=cumulative_in,
            output_tokens=cumulative_out,
            credits_per_1k_input=self._settings.credits_per_1k_input,
            credits_per_1k_output=self._settings.credits_per_1k_output,
        )
        want = owed - charged
        if want <= 0:
            # Nothing new owed yet (cumulative unchanged / rounding): no debit, not depleted.
            return charged, False
        balance = await self._wallet.current_balance(user_id)
        charge = min(want, balance)
        actual = 0
        if charge > 0:
            meta: dict[str, Any] = {
                "source": "agent_run",
                "incremental": True,
                "runId": run_id,
                "stepIndex": step_index,
                "usage": {
                    # RAW, as the event carried them — an audit record must state what arrived,
                    # not what the reader made of it. Since the union fold landed, the billed
                    # anchors may come from another carrier or from a partial match whose other
                    # half is an invented zero, so writing them under `cumulative_*` would have the
                    # ledger assert a field the event never had. Per-step names and the cumulative_*
                    # anchors survive redaction via _USAGE_COUNT_ALLOWLIST (TD-035 closed).
                    "input_tokens": _as_int(data.get("input_tokens")),
                    "output_tokens": _as_int(data.get("output_tokens")),
                    "cumulative_input_tokens": _as_int(data.get("cumulative_input_tokens")),
                    "cumulative_output_tokens": _as_int(data.get("cumulative_output_tokens")),
                    # What this debit was actually computed from, kept separate from the raw record.
                    # Deliberately NOT named *_tokens: those names hit the ADR-049 substring
                    # denylist and would need an allowlist entry of their own to survive redaction.
                    "billed_input": cumulative_in,
                    "billed_output": cumulative_out,
                    "billed_from": list(usage.sources),
                },
                "model": model,
            }
            result = await self._wallet.consume(
                user_id=user_id,
                amount=charge,
                idempotency_key=f"{run_id}:{step_index}",
                meta=meta,
            )
            if result.idempotent_replay:
                # Step already billed (reconnect / concurrent stream): the prior debit stands — do
                # not double-count and do not treat it as a fresh depletion.
                return charged, False
            # Use the ACTUAL credits decremented (may be < charge if a concurrent chat-debit clamped
            # the balance) so charged + the agent_runs mirror stay consistent with the ledger
            # (money invariant). Advance the mirror in the SAME commit as the debit — the streaming
            # context needs an explicit commit (teardown already ran).
            actual = result.charged_amount
            if actual > 0:
                await self._runs_repo.record_step(run_id, step_index, actual)
            await self._session.commit()
            charged += actual
        # Depleted when the wallet could not fully cover `want` (balance was 0, or a race clamped
        # the actual charge below want).
        depleted = actual < want
        return charged, depleted

    def _note_anchor_progress(self, *, run_id: str, state: _RelayState, advanced: bool) -> None:
        """Warn once when the cumulative anchors stop moving across consecutive ``usage.delta``.

        The class no single event can reveal: the counters are readable and self-consistent, yet
        FROZEN — per-step deltas keep arriving while ``cumulative_*`` stands still. Every per-event
        check passes (nothing is malformed), billing computes ``owed`` from an anchor that never
        grows, so ``want <= 0`` on every step: the run bills once and then rides free, and
        pause-at-zero can never trigger. Only the sequence shows it, so only relay-level state can.

        The threshold buys tolerance for the benign reasons a single step may not advance (a
        duplicate event, a step whose counts genuinely did not change) without letting a truly
        stuck anchor run to the end of the run unreported.
        """
        if advanced:
            state.usage_events_since_anchor_advance = 0
            return
        state.usage_events_since_anchor_advance += 1
        if state.usage_events_since_anchor_advance < _USAGE_ANCHOR_STALL_WARN_AFTER:
            return
        if state.latch_usage_anomaly(_EVENT_USAGE_DELTA, "anchors_frozen"):
            logger.warning(
                "hermes usage.delta cumulative anchors have not advanced run_id=%s events=%d "
                "input=%d output=%d",
                run_id,
                state.usage_events_since_anchor_advance,
                state.input_tokens,
                state.output_tokens,
            )

    def _warn_usage_anomalies(
        self,
        *,
        run_id: str,
        kind: str,
        usage: _UsageCounts,
        state: _RelayState,
        event: _SseEvent | None = None,
    ) -> None:
        """Report how a usage payload was read badly — on EVERY usage path, not just the last one.

        Both anomalies used to live on the terminal path only — the one path a systematically
        malformed stream never reaches in a useful state: a half-read ``usage.delta`` under-bills
        EVERY step (and, with no ``run.completed``, is never reconciled — TD-037), and a divergence
        between carriers picks the larger one on every step. Whatever bills also reports.

        Passing ``event`` adds the INVARIANT check — nothing recognised out of a block that is not
        provably usage-free. The per-anomaly checks below only fire on a carrier that was found and
        then read badly; a carrier that is not found AT ALL (counts as strings, a renamed carrier, a
        list-shaped ``usage``) sets none of them, and on the step path that silence is expensive:
        every step bills 0, pause-at-zero never triggers, /state reports no usage, and nothing says
        why. ``run.completed`` gets the same guarantee from its stronger ``owed == 0`` gate
        (:meth:`_assert_billable_usage`) and so passes no ``event``, which also keeps that path from
        logging the same fact twice. Relying on the terminal gate alone was never enough anyway —
        under TD-037 ``run.completed`` is often not processed at all.

        Latched per relay: usage events arrive once per step, so an unlatched line would repeat all
        run long. Counts and carrier LABELS only — the labels are key names, not user content.
        """
        if (
            event is not None
            and not usage.recognised
            and not _is_provably_usage_free(event)
            and state.latch_usage_anomaly(kind, "unrecognised")
        ):
            logger.warning(
                "hermes %s usage shape unknown, no carrier recognised run_id=%s keys=%s",
                kind,
                run_id,
                _shape_summary(event),
            )
        if usage.partial and state.latch_usage_anomaly(kind, "half_read"):
            logger.warning(
                "hermes %s usage half-read run_id=%s input=%d output=%d source=%s",
                kind,
                run_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.sources,
            )
        if usage.divergent and state.latch_usage_anomaly(kind, "divergent"):
            logger.warning(
                # The fold bills the MAXIMUM, so a disagreement is a potential OVERCHARGE — the one
                # direction the max-based fold made worse in exchange for zeroed carriers no longer
                # shadowing populated ones. Whichever way it is resolved, it is not ours to decide
                # silently: two carriers claim different values for the SAME quantity.
                "hermes %s usage carriers disagree, billing the maximum run_id=%s "
                "input=%d output=%d sources=%s",
                kind,
                run_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.sources,
            )
        if usage.non_monotonic and state.latch_usage_anomaly(kind, "non_monotonic"):
            logger.warning(
                # A per-step count above the cumulative total it belongs to. The billed maximum is
                # then the per-step value read as a run total — an OVERCHARGE that also breaks the
                # telescoping invariant of ADR-064 §1, so it cannot pass as a rounding artefact.
                "hermes %s per-step usage exceeds the cumulative total run_id=%s "
                "input=%d output=%d sources=%s",
                kind,
                run_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.sources,
            )

    def _assert_billable_usage(
        self,
        *,
        run_id: str,
        event: _SseEvent,
        usage: _UsageCounts,
        owed: int,
        state: _RelayState,
    ) -> None:
        """Warn when a ``run.completed`` bills ZERO from a block that plainly carries usage.

        The gate is stated as an INVARIANT on the outcome — "owed == 0 while the block is not
        provably usage-free" — rather than as a list of shapes to distrust. Shape-specific checks
        only catch the drifts they were taught: the ADR-065 defect was a TYPE change under a known
        key, so ``usage`` arriving as a list or a string, or counts arriving as strings, defeats
        every "is it the layout I expect?" probe while still being obviously usage. The invariant
        does not care how it drifted — zero credits out of a non-empty usage block is reported.

        Silent only for a block with no usage carrier at all — nothing to bill, nothing to miss.
        A run whose counts genuinely are ``0`` while a ``usage`` block is present DOES warn: that
        is a deliberate, accepted false positive (a completed run that consumed zero input tokens
        is not a thing in practice) bought in exchange for the guarantee that no unreadable carrier
        can ever bill zero quietly. Half-read and divergent carriers are reported by
        :meth:`_warn_usage_anomalies`, which every usage path shares. Keys and flags only; token
        VALUES are billing analytics, not log material here.

        The last check needs no payload knowledge at all: if the run PRODUCED ASSISTANT TEXT, tokens
        were spent, whatever the payload calls them. That closes the one class names cannot —
        a semantic rename (``in``/``out``, ``prompt``/``completion``, ``cost_usd``) that no
        ``*token*``/``usage`` marker matches — using the only signal the relay owns end to end.
        """
        self._warn_usage_anomalies(run_id=run_id, kind="run.completed", usage=usage, state=state)
        if owed > 0:
            return
        if state.delta_text_seen:
            logger.warning(
                "hermes run.completed billed zero for a run that produced assistant text "
                "run_id=%s deltas=%d recognised=%s keys=%s",
                run_id,
                state.delta_events,
                usage.recognised,
                _shape_summary(event),
            )
            # No early return: this signal is INDEPENDENT of the name-based gate below, and when
            # both fire they say different things (tokens were certainly spent / the block still
            # mentions usage we could not read). Each is latched to at most one line per run.
        if _is_provably_usage_free(event):
            return
        logger.warning(
            # Marker text kept STABLE ("usage shape unknown") across the gate's redesign: it is what
            # the ADR-065 tests and any log-based alerting grep for. What changed is the trigger —
            # an invariant on the billed result, not a list of distrusted shapes — and the fields.
            "hermes run.completed usage shape unknown, billed zero from a non-empty usage block "
            "run_id=%s recognised=%s numeric_token_fields=%s keys=%s",
            run_id,
            usage.recognised,
            _has_token_like_field(event),
            # PATHS, not top-level names: a drift inside the carrier ({'usage':{'prompt_tokens'}})
            # rendered as ['event','run_id','usage'] and told the operator nothing, while this line
            # is the only postmortem route to the real field names until Q-067-10 lands a capture.
            _shape_summary(event),
        )

    def _warn_if_no_delta_text(self, *, run_id: str, state: _RelayState) -> None:
        """Terminal-event half of the aggregate ADR-065 guard: deltas arrived, text never did.

        The counting latch in :meth:`stream_events` needs ``_DELTA_SILENT_WARN_AFTER`` deltas before
        it fires, so a run that ends after fewer than that would slip through with an empty
        ``resultText`` and no log line. Any terminal event closes that window: at that point the run
        is over, so "deltas were relayed and not one character was extracted" is conclusive rather
        than premature. Shares the ``delta_silent_logged`` latch — one line per relay, never two.
        """
        if state.delta_events > 0 and not state.delta_text_seen and not state.delta_silent_logged:
            state.delta_silent_logged = True
            logger.warning(
                "hermes message.delta yielded no text for the whole run run_id=%s deltas=%d",
                run_id,
                state.delta_events,
            )

    # --- snapshot writer (ADR-066 §6) ---------------------------------------------------------

    async def _flush_snapshot(
        self,
        *,
        user_id: uuid.UUID,
        run_id: str,
        state: _RelayState,
        immediate: bool,
        assert_approval: bool | None = None,
    ) -> None:
        """Persist the accumulated relay state into ``agent_run_snapshots`` (ADR-066 §6).

        ``immediate=False`` (the ``message.delta`` path) is THROTTLED to at most one write per
        ``AGENT_STATE_FLUSH_INTERVAL_SECONDS``; terminal events and ``approval.request`` pass
        ``immediate=True`` and bypass the throttle.

        The two properties are SEPARATE: ``immediate`` is about the throttle, ``assert_approval``
        about whether this event may speak for the approval state. They coincide for every event
        that carries authoritative approval information, so it defaults to ``immediate`` — but
        ``usage.delta`` needs the first without the second. Its token anchors must be persisted at
        once (the matching debit is committed in the same breath, and an SSE drop right after would
        otherwise leave the ledger charged and ``/state`` reporting zero usage — Q-047-2), while a
        ``usage.delta`` can legitimately arrive AFTER the client answered ``POST …/approval``, so
        re-asserting the relay's cached approval would resurrect a false ``waiting_approval``
        (forbidden, ADR-066 §6.2).

        ``result_text`` is truncated HEAD-preserving to ``AGENT_STATE_RESULT_TEXT_MAX_CHARS``:
        keeping the beginning keeps the prefix stable, which is what the per-column replay-guard
        (prefix-continuation check, ADR-066 §6.2) relies on. Once the cap is reached the length
        freezes, and every subsequent write submits exactly the same first N characters — incoming
        and stored values coincide in full, so both the length and the prefix condition hold and
        updates keep going through (the write is simply a no-op for that column, while tools,
        approval and tokens still advance). The full text always remains available through
        ``/events``. The joined+truncated text REPLACES the delta buffer,
        so a long run neither grows the buffer without bound nor re-joins an ever-longer list every
        few seconds; the operation is idempotent w.r.t. the value (concatenating the collapsed head
        with the following deltas yields the same head).

        ``pending_approval`` is asserted only when ``assert_approval`` holds. The events that set
        it (``approval.request``) or clear it (``tool.*``, terminal) assert it; a throttled
        ``message.delta`` flush and the ``usage.delta`` anchor flush do not: neither knows whether
        the client has answered ``POST …/approval`` out of band, and re-asserting the relay's
        cached value would resurrect a false ``waiting_approval``.

        An explicit ``commit()`` is required (streaming context: the request-session teardown has
        already run, ADR-064 pattern ``_bill_step``). A persistence failure must NEVER break the
        relay — most notably a foreign-key miss for a run started before ``agent_runs`` became an
        unconditional lifecycle row: it is rolled back and logged WITHOUT any user content.
        """
        if assert_approval is None:
            assert_approval = immediate
        if not immediate:
            elapsed = time.monotonic() - state.last_flush_at
            if elapsed < self._settings.agent_state_flush_interval_seconds:
                return
        result_text = "".join(state.partial_text)
        max_chars = self._settings.agent_state_result_text_max_chars
        if len(result_text) > max_chars:
            result_text = result_text[:max_chars]  # head-preserving (never the tail)
        # Collapse the buffer: bounded memory for a long run and an O(1)-sized join next time.
        state.partial_text = [result_text]
        try:
            written = await self._snapshots_repo.upsert(
                run_id=run_id,
                user_id=user_id,
                result_text=result_text,
                last_tool=state.last_tool,
                pending_approval=state.pending_approval,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                assert_pending_approval=assert_approval,
            )
            await self._session.commit()
            self._log_write_anomaly(
                run_id=run_id, state=state, written=written, submitted_length=len(result_text)
            )
        except SQLAlchemyError:
            # Generic log (no run text, no approval preview — this is user content, ADR-066 §5).
            logger.warning("agent run snapshot write failed run_id=%s", run_id)
            await self._session.rollback()
        # Reset the throttle window even on failure so a permanently failing write (e.g. a missing
        # agent_runs parent row) cannot turn every single message.delta into a DB round-trip.
        state.last_flush_at = time.monotonic()

    @staticmethod
    def _log_write_anomaly(
        *,
        run_id: str,
        state: _RelayState,
        written: SnapshotUpsertResult,
        submitted_length: int,
    ) -> None:
        """Surface the two SILENT refusal modes of a snapshot upsert. One line per relay each.

        Both outcomes are indistinguishable from a successful flush at the call site, yet each is
        the observable signal behind an open question:

        * not applied → the tenancy guard rejected the write, i.e. this ``run_id`` already belongs
          to another user's snapshot — the evidence needed to settle whether Hermes run ids are
          globally unique (Q-066-2). WARNING: it means the snapshot of this run is not being
          persisted at all.
        * applied but the stored text is shorter than what was submitted → ``result_text`` did not
          advance because the incoming text does not continue the stored one (Q-066-1: replay-from-
          start vs. new-events-only relay semantics). DEBUG: the run keeps working, the snapshot
          simply freezes at its fullest known text; tools/approval/tokens still update.

        Latched per relay so a persistent condition costs one line, not one every few seconds. Only
        ids and LENGTHS are logged — never ``result_text`` or the approval preview (user content,
        ADR-066 §5).
        """
        if not written.applied:
            if not state.tenancy_skip_logged:
                state.tenancy_skip_logged = True
                logger.warning("agent run snapshot upsert skipped (tenancy) run_id=%s", run_id)
            return
        if written.stored_text_length < submitted_length and not state.text_frozen_logged:
            state.text_frozen_logged = True
            logger.debug(
                "agent run snapshot result_text frozen run_id=%s stored=%d submitted=%d",
                run_id,
                written.stored_text_length,
                submitted_length,
            )

    def _build_pending_approval(self, event: _SseEvent) -> dict[str, Any]:
        """Build the ``{tool, preview}`` payload of an ``approval.request`` (ADR-066 §6).

        The event shape is Hermes' external contract, so both carriers are probed defensively; a
        miss yields ``None`` for that field rather than dropping the pending state (the fact that an
        approval is awaited matters more than its label). The preview is bounded by the same
        ``AGENT_STATE_RESULT_TEXT_MAX_CHARS`` cap as the run text — it is user content stored in
        JSONB and must not be unbounded; no separate knob is introduced.
        """
        preview = _extract_approval_preview(event)
        if preview is not None:
            max_chars = self._settings.agent_state_result_text_max_chars
            if len(preview) > max_chars:
                preview = preview[:max_chars]
        return {"tool": _extract_tool_name(event), "preview": preview}

    async def _mark_terminal(self, run_id: str, status: str) -> None:
        """Record a terminal ``agent_runs.status`` (ADR-066 §3) and commit (streaming context).

        Unconditional w.r.t. the billing flag (the lifecycle row now always exists) but CONDITIONAL
        on the run still being active (``WHERE status IN ('running','resumed')``, enforced by the
        repository) so the FIRST terminal status wins and a late event cannot overwrite a
        ``cancelled``/``paused`` that was recorded meanwhile.

        Like :meth:`_flush_snapshot`, a DB failure must not escape the relay generator: it would
        bypass the ``httpx.HTTPError`` handler, surface as a raw error mid-stream and leave the
        session without a rollback. It is logged and rolled back instead — the status simply stays
        as it was, and a re-subscription re-applies the same conditional transition.
        """
        try:
            await self._runs_repo.mark_status(run_id, status)
            await self._session.commit()
        except SQLAlchemyError:
            logger.warning("agent run status write failed run_id=%s status=%s", run_id, status)
            await self._session.rollback()

    async def _pause_run(
        self,
        *,
        user_id: uuid.UUID,
        run_id: str,
        charged: int,
        state: _RelayState,
    ) -> bytes:
        """Stop the run at zero balance, build the synthetic terminal ``run.paused`` (ADR-064 §3).

        NOT a generator: (1) INTERRUPT the Hermes run so no further loop API-calls happen — via
        ``_interrupt_run``, which deliberately does NOT mark the run ``cancelled`` (ADR-066 §3: the
        status belongs to the client ``/stop`` path only, otherwise a credits-exhausted run would
        transiently read as ``stopped`` and ``/resume`` would answer 409); (2) flush the final
        snapshot and persist ``status=paused``/``paused_reason``, committing (streaming context);
        (3) return a self-contained ``run.paused`` SSE block built from the LOCAL relay buffer
        (accumulated message.delta text + collected tool events) — no round-trip to Hermes.
        No debt is created (the last debit was ``charge <= balance``); balance is 0.
        """
        await self._interrupt_run(user_id=user_id, run_id=run_id)
        # Terminal event: `output` below is about to ship whatever the relay buffer holds, so an
        # empty buffer after N deltas is now conclusive — this is the exact block the prod defect
        # delivered with output="" and no warning anywhere (ADR-065 capture).
        self._warn_if_no_delta_text(run_id=run_id, state=state)
        # Terminal for the client: nothing is awaited from them any more (ADR-066 §6).
        state.pending_approval = None
        await self._flush_snapshot(user_id=user_id, run_id=run_id, state=state, immediate=True)
        await self._runs_repo.mark_paused(run_id, _REASON_CREDITS_EXHAUSTED)
        await self._session.commit()
        payload: dict[str, Any] = {
            "event": _EVENT_RUN_PAUSED,
            "run_id": run_id,
            "reason": _REASON_CREDITS_EXHAUSTED,
            "status": "paused",
            "output": "".join(state.partial_text),
            "steps": state.steps,
            "billed": charged,
            "balance": 0,
            "usage": {
                "cumulative_input_tokens": state.input_tokens,
                "cumulative_output_tokens": state.output_tokens,
            },
        }
        logger.info("agent run paused run_id=%s billed=%d", run_id, charged)
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def _bill_completed(
        self,
        *,
        user_id: uuid.UUID,
        run_id: str,
        event: _SseEvent,
        charged: int = 0,
        state: _RelayState,
    ) -> bool:
        """Finalize a completed run's remainder. Returns True if billing was attempted.

        ADR-064 §2: ``remainder = usage_to_credits(final) - charged`` is debited once with the BARE
        ``run_id`` idempotency key (a separate keyspace from the per-step ``run_id:step`` keys).
        With the flag OFF ``charged == 0`` so ``remainder == usage_to_credits(final)`` — the full
        ADR-047 post-hoc debit, idempotent by ``run_id``. The remainder goes through the ADR-047/
        ADR-051 debt-capable path (meta WITHOUT ``incremental``): usually covered by balance; a debt
        is possible only in a rare race (fallback). Insufficient balance: ``consume`` rolls back its
        savepoint (no orphan row, balance untouched — ADR-047 §6) and the uncharged delta is an
        audit-only ``billing_debit_insufficient`` event; the relay is never broken.

        Billing ONLY — the terminal ``agent_runs.status`` is NOT written here (ADR-066 §3). The
        lifecycle status is recorded by the ``run.completed`` handler in :meth:`stream_events`
        BEFORE this call, so that it never depends on the outcome of a debit: an unexpected billing
        failure (the generic ``except`` below rolls back and swallows) used to leave the run
        ``running`` forever and make ``/state`` lie indefinitely.
        """
        usage = _extract_usage_counts(event)
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        total_tokens = usage.total_tokens
        model = _extract_model(event)
        owed = usage_to_credits(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            credits_per_1k_input=self._settings.credits_per_1k_input,
            credits_per_1k_output=self._settings.credits_per_1k_output,
        )
        self._assert_billable_usage(run_id=run_id, event=event, usage=usage, owed=owed, state=state)
        # ADR-064 §2: only the remainder over what per-step billing already charged.
        amount = owed - charged
        if amount <= 0:
            if charged > 0 and owed < charged:
                # INVARIANT BREACH, not a rounding artefact: usage_to_credits is monotonic in both
                # counts and `charged` only ever grew by credits actually taken for THIS run, so a
                # correctly read final total can never be worth less than the sum of its own steps.
                # It means the final usage payload was read as something smaller than reality —
                # i.e. a shape miss — and without this line it would leave as "no remainder".
                logger.warning(
                    "agent run final usage below what was already billed run_id=%s owed=%d "
                    "charged=%d recognised=%s partial=%s",
                    run_id,
                    owed,
                    charged,
                    usage.recognised,
                    usage.partial,
                )
            logger.info(
                # recognised/partial separate a genuinely free or fully pre-billed run from one
                # whose usage payload was not understood (the gate above logs the keys).
                "agent run completed, no remainder run_id=%s owed=%d charged=%d recognised=%s",
                run_id,
                owed,
                charged,
                usage.recognised,
            )
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
        """Passthrough ``POST {base}/v1/runs/{runId}/approval`` (ADR-045 §3).

        ADR-066 §6: AFTER a 2xx passthrough the stored ``pending_approval`` is dropped — the third
        clearing point besides ``tool.*`` and the terminal events. Without it the derived
        ``waiting_approval`` status would stick in ``/state`` after the user already answered.
        Owner-scoped and never INSERTs; a snapshot-less run simply has nothing to clear. Committed
        by the request session_scope teardown (this is a plain handler, not the streaming context).
        """
        await self.assert_run_owner(user_id=user_id, run_id=run_id)
        result = await self._passthrough_post(user_id, f"/v1/runs/{run_id}/approval", body)
        await self._snapshots_repo.clear_pending_approval(run_id, user_id)
        return result

    async def stop(self, *, user_id: uuid.UUID, run_id: str) -> dict[str, Any]:
        """CLIENT stop path: passthrough ``POST {base}/v1/runs/{runId}/stop`` + ``cancelled``.

        ADR-066 §3: after a 2xx passthrough the run is marked ``cancelled`` — CONDITIONALLY
        (``WHERE status IN ('running','resumed')``), so stopping an already finished/paused run does
        not overwrite its terminal status. This method is the CLIENT path ONLY: the internal
        Hermes interrupt of pause-at-zero goes through :meth:`_interrupt_run`, which performs the
        very same passthrough WITHOUT touching the status (ADR-064 §3) — marking it there would make
        a credits-exhausted run transiently ``cancelled``, so ``/state`` would report ``stopped``
        instead of ``paused`` (no top-up offer in the UI) and ``POST …/resume`` would answer
        ``409 run_not_resumable`` inside that window. Committed by the request session_scope
        teardown (plain handler). ``stop → stopped`` is eventually consistent: Hermes may still be
        flushing buffered events into an open relay at this point.

        The status write is OWNER-SCOPED (``mark_stopped(run_id, user_id)``): ``run_id`` arrives
        straight from the request path and Hermes may answer 2xx for an unknown/foreign run
        (idempotent-stop semantics), so an unscoped UPDATE would let one user cancel another user's
        run. A foreign id updates 0 rows; the passthrough result is still returned (no 403).
        """
        await self.assert_run_owner(user_id=user_id, run_id=run_id)
        result = await self._interrupt_run(user_id=user_id, run_id=run_id)
        await self._runs_repo.mark_stopped(run_id, user_id)
        return result

    async def _interrupt_run(self, *, user_id: uuid.UUID, run_id: str) -> dict[str, Any]:
        """Interrupt the Hermes run WITHOUT recording any status (ADR-066 §3).

        The transport-level half of :meth:`stop`, shared with the internal pause-at-zero path. Kept
        separate precisely so the ``cancelled`` status can never leak onto the internal path.
        """
        return await self._passthrough_post(user_id, f"/v1/runs/{run_id}/stop", None)

    async def assert_run_owner(self, *, user_id: uuid.UUID, run_id: str) -> None:
        """404 unless ``run_id`` belongs to the subject (06-rbac.md). FIRST step of a runId route.

        ⚠️ On a STREAMING route this must be awaited by the ROUTE HANDLER, not from inside the
        response generator. Starlette sends ``http.response.start`` with status 200 before pulling
        the generator's first item, so an exception raised inside it cannot become a 404 — it
        surfaces as ``RuntimeError: Caught handled exception, but response already started`` and,
        under ``BaseHTTPMiddleware``, wedges the connection. Same reason ``?afterSeq=`` is parsed in
        the handler. The generator keeps its own call as defense in depth for direct callers.

        RBAC here is a property of the RESOURCE, not of what a particular route does. Both
        ``/approval`` and ``/stop`` used to rely on an INDIRECT guarantee — ``ensure_running``
        resolves the caller's OWN instance, so a foreign ``runId`` was unreachable by construction —
        which is the same implicit mechanism that stopped holding on ``/events`` the moment its
        executor changed. An implicit guarantee disappears silently when the mechanism behind it
        moves, so it is now asserted directly on all five runId routes.

        Called BEFORE ``ensure_running`` and before any passthrough deliberately: checking later
        would let a foreign ``runId`` wake our instance and reach upstream — a state change on
        someone else's run — before being rejected.

        404, never 403, matching ``/state`` and ``/resume``: existence of another user's run is not
        disclosed. A run predating ADR-066 has no ``agent_runs`` row and answers 404 here as it
        already does on ``/state``.
        """
        run = await self._runs_repo.get(run_id)
        if run is None or run.user_id != user_id:
            raise NotFoundError("run not found")

    async def _passthrough_post(
        self, user_id: uuid.UUID, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        """POST to the user's instance; relay the JSON body. 502 on transport/non-2xx failure.

        Shares the launch path's end-to-end budget (``ensure_running`` + the call), so ``/stop``,
        ``/approval`` and the internal pause interrupt cannot hang the way the launch did: waking a
        stuck instance is exactly as slow here, and ``/stop`` in particular is what a user reaches
        for WHEN a run is already misbehaving — the one request that must never hang.
        """
        deadline = self._budget_deadline()
        endpoint = await self._manager.ensure_running(user_id, deadline=deadline)
        url = f"{endpoint.base_url}{path}"
        left = self._remaining(deadline, phase=f"passthrough {path}")
        try:
            async with (
                asyncio.timeout(left),  # budget ceiling (see _budget_deadline SCOPE §3)
                httpx.AsyncClient(timeout=self._attempt_timeout(left)) as client,
            ):
                response = await client.post(
                    url, json=body, headers=self._bearer_headers(endpoint.api_key)
                )
        except TimeoutError as exc:
            logger.warning("hermes passthrough exceeded the request budget path=%s", path)
            raise UpstreamTimeoutError(
                "hermes instance did not answer within the request budget"
            ) from exc
        except httpx.HTTPError as exc:
            raise self._transport_error(f"passthrough {path}", exc) from exc
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

    # --- state snapshot (read-only) ---------------------------------------------------------

    async def get_state(self, *, user_id: uuid.UUID, run_id: str) -> RunStateView:
        """``GET /v1/agent/runs/{runId}/state`` — STRICTLY read-only snapshot (ADR-066 §5).

        Invariants, all deliberate:

        * **No ``ensure_running``** — reading the state never wakes a hibernated container, which
          would otherwise cost a cold start (~30-40 s, ADR-056) on every background polling tick.
        * **No call to Hermes at all** — only ``SELECT`` from ``agent_runs`` +
          ``agent_run_snapshots`` (after hibernation Hermes lost its in-memory run registry anyway).
        * **No debit** — reading is free, ``WalletService`` is not involved.
        * **No policy-gate** — a credits block applies to STARTING a generation, not to reading what
          already happened, so ``200 {status:blocked}`` cannot occur on this route.

        Ownership follows the ``/resume`` pattern: a missing row OR a foreign ``user_id`` raises
        :class:`NotFoundError` → 404, never 403 (agent-proxy/06-rbac.md). Runs launched before this
        feature was deployed have no ``agent_runs`` row and therefore also answer 404 (no backfill —
        the source data never existed).

        The snapshot row may be ABSENT while the lifecycle row exists (the relay writer has not
        flushed a single event yet) → 200 with defaults: empty text, no tool, no approval, zero
        usage and ``updated_at`` taken from ``agent_runs``.
        """
        run = await self._runs_repo.get(run_id)
        if run is None or run.user_id != user_id:
            raise NotFoundError("run not found")
        # Owner-scoped read as well (defense-in-depth): the RBAC decision above is already made on
        # agent_runs, but a Hermes run_id colliding across tenants (Q-064-4) must not surface
        # another user's text — a mismatch degrades to the empty-snapshot defaults, never a leak.
        snapshot = await self._snapshots_repo.get(run_id, user_id)
        pending_approval = snapshot.pending_approval if snapshot is not None else None
        status = map_client_status(run.status, has_pending_approval=pending_approval is not None)
        return RunStateView(
            run_id=run.run_id,
            session_id=run.session_id,
            status=status,
            result_text=snapshot.result_text if snapshot is not None else "",
            last_tool=snapshot.last_tool if snapshot is not None else None,
            pending_approval=pending_approval,
            # ADR-066 §5: this carries agent_runs.paused_reason (v1: only 'credits_exhausted') and
            # is NOT the policy blockReason enum of ADR-004 — the value sets do not overlap. Filled
            # only while the run is actually paused.
            block_reason=run.paused_reason if status == "paused" else None,
            input_tokens=snapshot.input_tokens if snapshot is not None else 0,
            output_tokens=snapshot.output_tokens if snapshot is not None else 0,
            # Staleness detector for the client: with no subscriber on /events an active run's
            # snapshot does not move. The retention sweep never shifts it.
            updated_at=snapshot.updated_at if snapshot is not None else run.updated_at,
            continued_from=run.continued_from_run_id,
        )

    # --- resume (continuation) --------------------------------------------------------------

    async def resume(
        self, *, user_id: uuid.UUID, run_id: str, message: str | None
    ) -> RunResumeResult:
        """Resume a paused run as a continuation in the same Hermes session (ADR-064 §5).

        Order (ADR-064 §5): RBAC-404 → status pre-guard (409 run_not_resumable) → policy-gate
        (200 blocked if still no credits/debt) → atomic CAS ``paused→resumed`` (the single race
        arbiter) → ensure_running → hydrate transcript → launch a NEW run in the SAME session →
        chain-insert the child (continued_from = runId) → 202 {runId: new, continuedFrom: runId}.
        The CAS loser resolves idempotently to the existing child (202) or 409 resume_in_progress.
        A launch failure AFTER a won CAS reverts ``resumed→paused`` (run stays resumable) → 502.
        """
        # (2) RBAC: a foreign / unknown run is invisible → 404 (ADR-064 §5 step 2).
        run = await self._runs_repo.get(run_id)
        if run is None or run.user_id != user_id:
            raise NotFoundError("run not found")
        # (3) Informational status pre-guard; the authoritative arbiter is the CAS (step 5).
        if run.status not in ("paused", "resumed"):
            raise RunNotResumableError("run is not resumable")

        # (4) Policy-gate (read-only, BEFORE any status change): if the balance is still 0 / in debt
        # after a would-be top-up, resume does NOT flip status or launch — 200 blocked (ADR-004).
        blocked = await self._resume_policy_block(user_id)
        if blocked is not None:
            return blocked

        # (5) Atomic CAS paused→resumed — the single serializing step (own short transaction).
        cas = await self._runs_repo.cas_resume(run_id)
        if cas is None:
            # Lost the CAS (already resumed / retry): resolve to the existing child idempotently.
            child = await self._runs_repo.active_child(run_id)
            if child is not None:
                return RunResumeResult(blocked=False, run_id=child.run_id, continued_from=run_id)
            # Narrow window between the winner's CAS and its chain-insert → client retries.
            raise ResumeInProgressError("resume in progress")
        session_id = str(cas.session_id)
        model = cas.model
        # Commit the CAS immediately to release the row lock (short transaction, ADR-064 §5).
        await self._session.commit()

        # (6-9) Only the CAS winner launches. On any failure BEFORE a child is chained, revert
        # resumed→paused (guarded by NOT EXISTS child) so the run stays resumable, then surface the
        # error (502 for upstream, 409 for an expired session). A launched-but-unchained run is an
        # orphan handled by the idle-reaper (Q-064-1).
        try:
            # One budget for wake + hydrate + launch (see :meth:`_budget_deadline`). On exhaustion
            # the UpstreamTimeoutError travels the SAME path as any other failure here, so the CAS
            # revert below still runs and the run stays resumable.
            deadline = self._budget_deadline()
            # Same tripwire as run() (Q-067-17): resume walks the identical launch path — wake the
            # instance, hydrate, launch — and a mute instance is exactly as observable here.
            async with self._launch_timeout_probe(
                _PHASE_READINESS, user_id=user_id, run_id=run_id, refine=False
            ):
                endpoint = await self._manager.ensure_running(user_id, deadline=deadline)
            async with self._launch_timeout_probe(_PHASE_HYDRATE, user_id=user_id, run_id=run_id):
                history = await self._fetch_session_transcript(
                    endpoint, session_id, deadline=deadline
                )
            hermes_body: dict[str, Any] = {"session_id": session_id}
            if message is not None:
                hermes_body["input"] = message
            if model is not None:
                hermes_body["model"] = model
            if history:
                hermes_body["conversation_history"] = history
            async with self._launch_timeout_probe(_PHASE_LAUNCH, user_id=user_id, run_id=run_id):
                new_run_id, status = await self._launch_run(
                    endpoint, hermes_body, deadline=deadline
                )
        except Exception:
            await self._runs_repo.revert_cas(run_id)
            await self._session.commit()
            raise

        # (9) Chain-insert the continuation child (runId is already 'resumed' from the CAS).
        await self._runs_repo.create_running(
            new_run_id,
            user_id,
            session_id,
            model,
            continued_from_run_id=run_id,
            status="running",
        )
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_AGENT_RUN,
                payload={
                    "phase": "resumed",
                    "runId": new_run_id,
                    "continuedFrom": run_id,
                    "status": status,
                },
            )
        )
        # Same order as run(): chain-insert + audit → COMMIT → wait for the consumer. The commit
        # must come first for the same two reasons — the wait must hold no transaction, and the
        # child row the orphan reaper sweeps from must be durable before anything can fail.
        await self._session.commit()
        await self._start_consumer(user_id=user_id, run_id=new_run_id, endpoint=endpoint)
        return RunResumeResult(blocked=False, run_id=new_run_id, continued_from=run_id)

    async def _resume_policy_block(self, user_id: uuid.UUID) -> RunResumeResult | None:
        """Re-evaluate the credits policy-gate for resume (ADR-064 §5 step 4). None = allowed.

        Same achievable block set as ``run()`` (credits branch + the ADR-051 debt gate). Read-only:
        no status flip, no launch, no debit. Returns a blocked RunResumeResult (200) or None.
        """
        state = await load_policy_state(self._session, user_id)
        decision: Decision = evaluate(state, Mode.credits)
        if not decision.allow:
            reason = decision.block_reason.value if decision.block_reason is not None else None
            await self._audit.record(
                AuditEvent(
                    user_id=user_id,
                    event_type=EVENT_AGENT_RUN,
                    payload={"phase": "resumed", "blockReason": reason},
                )
            )
            if reason not in _AGENT_BLOCK_REASONS:
                logger.warning("agent resume blocked with unexpected reason=%s", reason)
            return RunResumeResult(blocked=True, block_reason=reason)
        if self._settings.agent_debt_reconcile_enabled:
            debt = await self._wallet.current_debt(user_id)
            if debt > 0:
                await self._audit.record(
                    AuditEvent(
                        user_id=user_id,
                        event_type=EVENT_AGENT_RUN,
                        payload={
                            "phase": "resumed",
                            "blockReason": _DEBT_OUTSTANDING,
                            "debt": debt,
                        },
                    )
                )
                return RunResumeResult(blocked=True, block_reason=_DEBT_OUTSTANDING)
        return None

    async def _fetch_session_transcript(
        self, endpoint: InstanceEndpoint, session_id: str, *, deadline: float
    ) -> list[dict[str, Any]]:
        """Hydrate the Hermes session transcript into ``conversation_history`` (ADR-064 §7).

        ``GET {base}/api/sessions/{session_id}/messages`` (Bearer) → ``[{role, content}]``. The
        exact response shape is pending the Hermes image patch (Q-064-3); the mapping is defensive:
        ``messages``/``data`` list envelope or a bare list, keeping only items with ``role`` +
        ``content``. A 404 or an EMPTY history → the session cannot be continued → 409
        ``session_expired`` (caller reverts the CAS). Transport failure → 502.
        """
        url = f"{endpoint.base_url}/api/sessions/{session_id}/messages"
        left = self._remaining(deadline, phase="session transcript")
        try:
            async with (
                asyncio.timeout(left),  # budget ceiling (see _budget_deadline SCOPE §3)
                httpx.AsyncClient(timeout=self._attempt_timeout(left)) as client,
            ):
                response = await client.get(url, headers=self._bearer_headers(endpoint.api_key))
        except TimeoutError as exc:
            logger.warning("hermes session transcript exceeded the request budget")
            raise UpstreamTimeoutError(
                "hermes instance did not answer within the request budget"
            ) from exc
        except httpx.HTTPError as exc:
            raise self._transport_error("session transcript", exc) from exc
        if response.status_code == 404:
            raise SessionExpiredError("session transcript not found")
        if not 200 <= response.status_code < 300:
            logger.warning("hermes session transcript non-2xx status=%s", response.status_code)
            raise UpstreamError("hermes session transcript failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("hermes session transcript invalid body") from exc
        raw_messages: Any = payload
        if isinstance(payload, dict):
            raw_messages = payload.get("messages") or payload.get("data") or []
        if not isinstance(raw_messages, list):
            raw_messages = []
        history: list[dict[str, Any]] = [
            {"role": item["role"], "content": item["content"]}
            for item in raw_messages
            if isinstance(item, dict) and "role" in item and "content" in item
        ]
        if not history:
            raise SessionExpiredError("session transcript empty")
        return history

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
    *,
    on_bytes: Callable[[int], None] | None = None,
) -> AsyncIterator[tuple[_SseEvent, bytes]]:
    """Yield (parsed_event, raw_block_bytes) for each SSE block of the stream.

    ``on_bytes`` is called with the size of every chunk as it ARRIVES, before any block boundary is
    found. The background consumer's inert-subscription guard (ADR-067 §6.4.2) fires on
    ``bytes_read == 0``, and counting only completed blocks would report zero for a subscription
    that is receiving a large first event perfectly normally — killing a working run, the one
    failure mode that guard must never have.

    Splits on the blank-line block separator. The raw bytes (including the trailing ``\\n\\n``) are
    forwarded to the client verbatim so relaying never mutates the wire format; the parsed event is
    used only to detect terminal billing events. Malformed/partial blocks are forwarded raw with an
    empty parsed payload (the relay must not drop bytes on a parse miss).
    """
    buffer = b""
    async for chunk in response.aiter_bytes():
        if on_bytes is not None:
            on_bytes(len(chunk))
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


def _as_event_text(value: Any) -> str | None:
    """Resolve an event field that may be either a bare string OR a ``{text: …}`` wrapper.

    Hermes carries the same logical field in BOTH shapes depending on the image build: the patched
    production image (ADR-065) emits ``"delta": "<text>"`` (bare string — the shape recorded in the
    raw SSE capture of a prod run; the canonical contract statement is owned by
    docs/modules/agent-proxy/05-events.md), while other builds emit ``"delta": {"text": "<text>"}``.
    Probing only one shape makes the other one silently resolve to nothing — the ADR-066 prod defect
    (``resultText`` always empty). Anything that is neither a string nor a string-carrying wrapper
    yields ``None``; structured payloads are NOT serialised into the snapshot (these fields are UI
    labels/text, not transcripts — /events stays the source).
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            nested = value.get(key)
            if isinstance(nested, str) and nested:
                return nested
    return None


def _extract_tool_name(event: _SseEvent) -> str | None:
    """Tool name of a ``tool.*`` / ``approval.request`` block (ADR-066 §6). Best-effort.

    The payload shape is Hermes' external contract (``{tool, ...}`` per agent-proxy/05-events.md);
    the common aliases are probed defensively so an upstream rename degrades to ``None`` instead of
    breaking the relay. Each alias is shape-tolerant for the same reason ``message.delta`` is: a
    bare string OR an IDENTIFIER wrapper — ``{name|tool|tool_name: "<tool>"}``. Deliberately NOT
    :func:`_as_event_text`: a tool name is an identifier, not prose, so the prose carriers that
    helper accepts (``text``/``content``/``value``) are not probed here — otherwise an arbitrary
    text blob nested under a tool payload would end up in ``last_tool``, which /state surfaces as a
    UI label. Nothing matches => ``None`` (the pending/tool state is still recorded, unlabelled).
    """
    for key in ("tool", "tool_name", "name"):
        value = event.data.get(key)
        if isinstance(value, str):
            if value:
                return value
            continue
        if isinstance(value, dict):
            for nested_key in ("name", "tool", "tool_name"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested:
                    return nested
    return None


def _extract_approval_preview(event: _SseEvent) -> str | None:
    """Human-readable preview of what an ``approval.request`` is asking to approve (§6).

    Only text carriers are accepted — a bare string or a ``{text: …}`` wrapper
    (:func:`_as_event_text`); an otherwise structured payload is not serialised into the snapshot
    (the preview is a UI label, not a transcript; the full event is always available via /events).
    """
    for key in ("preview", "description", "summary"):
        text = _as_event_text(event.data.get(key))
        if text:
            return text
    return None


def _extract_usage_counts(event: _SseEvent) -> _UsageCounts:
    """Token counts of a usage-carrying event, as a UNION of the observed carrier shapes.

    THE MONEY PATH. With ``agent_incremental_billing_enabled`` off (the default) the debit derived
    from these counts is the ONLY one of the whole run, and a miss is indistinguishable from a
    legitimate zero: ``owed = 0`` → ``amount <= 0`` → the early ``no remainder`` return of
    :meth:`AgentProxyService._bill_completed`. Hence the union rather than a single assumed shape.

    Matches are not ranked, they are FOLDED: every carrier × field set that yields both counts
    contributes to an element-wise maximum. Any ranking scheme (field set first, or carrier first)
    lets whichever match is probed first win even when it is all zeros — ``{'usage': {6313, 658},
    'cumulative_input_tokens': 0, 'cumulative_output_tokens': 0}`` billed the run at zero. The
    maximum needs no priority rule at all: these counters are monotonic within a run, so the
    cumulative anchors dominate the per-step ones exactly when they carry real data, and a zeroed
    carrier can no longer shadow a populated one. Reading per-step deltas as run totals under-bills
    the run; the cumulative anchors ARE the run total.

    A carrier matches FULLY only when both counts are present AND int-coercible. A carrier with just
    one of them is kept as a fallback (better than losing the half we can read) but reported via
    ``partial`` so the money path can say so out loud — the missing half would otherwise be a silent
    zero. The fallback applies ONLY when no full match exists anywhere. A count that is not
    int-coercible (``'6313'``) does NOT make a carrier recognised: billing would read it as 0, so
    claiming recognition there would be a lie in the logs.

    Both probed layouts are observed, not assumed: ``usage.delta`` is FLAT (``input_tokens`` /
    ``cumulative_*`` at the top level) and the nested ``usage`` object of our own synthetic
    ``run.paused`` uses the ``cumulative_*`` names. The layout of ``run.completed`` itself is NOT
    confirmed by any capture — every prod capture available so far ends at ``run.paused`` — so it
    stays an open question (a raw capture of a run that reaches ``run.completed`` is needed;
    architect owns the Q-number, see docs/modules/agent-proxy/05-events.md). Recognition is never
    the ONLY guard, though: the caller's gate keys off ``owed == 0`` on a block that is not provably
    usage-free (:func:`_is_provably_usage_free`), so an unreadable carrier warns whatever its shape.
    """
    carriers = [
        (label, source)
        for label, source in (("usage", event.data.get("usage")), ("top", event.data))
        if isinstance(source, dict)
    ]
    full: list[tuple[str, str, int, int, int]] = []
    partial: _UsageCounts | None = None
    for field_set, input_key, output_key, total_key in _USAGE_FIELD_SETS:
        for carrier, source in carriers:
            input_tokens = _as_int_or_none(source.get(input_key))
            output_tokens = _as_int_or_none(source.get(output_key))
            if input_tokens is None and output_tokens is None:
                continue
            if input_tokens is None or output_tokens is None:
                partial = partial or _UsageCounts(
                    input_tokens=input_tokens or 0,
                    output_tokens=output_tokens or 0,
                    total_tokens=_as_int_or_none(source.get(total_key)) or 0,
                    recognised=True,
                    partial=True,
                    sources=(f"{carrier}.{field_set}",),
                )
                continue
            full.append(
                (
                    field_set,
                    f"{carrier}.{field_set}",
                    input_tokens,
                    output_tokens,
                    _as_int_or_none(source.get(total_key)) or 0,
                )
            )
    if full:
        # FULL matches are FOLDED element-wise, never ranked. Ranking (by field set or by carrier)
        # means a zero-valued winner shadows a populated loser — a `cumulative_*: 0` pair next to a
        # real per-step carrier billed the run at 0. The max is safe precisely because these
        # counters are monotonic per run: the cumulative anchors are >= any per-step value, so they
        # still win whenever they carry real data. What the max CANNOT do is tell a legitimately
        # larger cumulative anchor from a carrier that is large because it counts something else —
        # so a disagreement between COMPARABLE full matches is surfaced (`divergent`).
        #
        # Comparable means SAME field set. Across sets a difference is the contract, not a conflict:
        # on step >1 of a run `input_tokens` is that step's delta while `cumulative_input_tokens` is
        # the running total (05-events.md), so comparing them would fire on every multi-step run and
        # burn exactly the attention these latches exist to protect. What IS meaningful across sets
        # is the ORDER breaking — a per-step value above the cumulative total contradicts monotonic
        # counters, i.e. at least one of the two is not the quantity we think it is.
        by_set: dict[str, set[tuple[int, int]]] = {}
        for field_set, _label, input_tokens, output_tokens, _total in full:
            by_set.setdefault(field_set, set()).add((input_tokens, output_tokens))
        per_step = by_set.get("per_step")
        cumulative = by_set.get("cumulative")
        non_monotonic = bool(
            per_step
            and cumulative
            and (
                max(pair[0] for pair in per_step) > max(pair[0] for pair in cumulative)
                or max(pair[1] for pair in per_step) > max(pair[1] for pair in cumulative)
            )
        )
        return _UsageCounts(
            input_tokens=max(counts[2] for counts in full),
            output_tokens=max(counts[3] for counts in full),
            total_tokens=max(counts[4] for counts in full),
            recognised=True,
            partial=False,
            sources=tuple(counts[1] for counts in full),
            divergent=any(len(pairs) > 1 for pairs in by_set.values()),
            non_monotonic=non_monotonic,
        )
    if partial is not None:
        return partial
    return _UsageCounts(
        input_tokens=0, output_tokens=0, total_tokens=0, recognised=False, partial=False
    )


def _is_provably_usage_free(event: _SseEvent) -> bool:
    """True only when the block CANNOT be carrying usage — anywhere in it, at any nesting level.

    The invariant behind the ``run.completed`` billing gate. Keyed on NAMES ONLY, on ANY value type
    and now at ANY depth (up to ``_USAGE_GATE_MAX_DEPTH``, through dicts and lists of dicts). Both
    relaxations exist for the same reason: the layout of ``run.completed`` is not confirmed by any
    capture, so its carrier's TYPE and its DEPTH are exactly equally unguaranteed. A top-level-only
    check was still an assumption in disguise — ``{'result': {'usage': {...}}}`` or the literal
    Anthropic ``{'message': {'usage': {...}}}`` (Hermes proxies Claude) passed as "provably free"
    and billed the run at zero with no warning, while ``event``/``run_id`` at the top level kept
    dispatch, the terminal status and the whole happy path intact.

    An ``usage`` key whose value is ``None`` or an EMPTY container is treated as absent rather than
    as an unreadable carrier: a run interrupted before its first step legitimately reports an empty
    usage object, and warning on it would be noise on real traffic (the scan continues for
    ``*token*`` keys regardless). Anything else that mentions usage or tokens, billed at zero, gets
    a line: a false positive costs one warning, a false negative costs the run's entire revenue in
    silence (ADR-047/ADR-064 with the flag off).
    """
    return not _mentions_usage(event.data, depth=_USAGE_GATE_MAX_DEPTH)


def _mentions_usage(node: Any, *, depth: int) -> bool:
    """Recursive half of :func:`_is_provably_usage_free`: does anything here mention usage/tokens?

    Depth-bounded so a pathological payload cannot turn the gate into a deep walk; the bound is
    generous next to the two layouts ever observed (top level and one nesting level).
    """
    if depth <= 0:
        return False
    if isinstance(node, dict):
        for key, value in node.items():
            # BOTH markers are CASE-FOLDED SUBSTRING matches, deliberately symmetric. An exact-match
            # rule for `usage` let `usages`/`usage_summary` (and `token_usage`, via the other half)
            # pass as "provably free"; a case-sensitive one let `inputTokens`/`totalTokens` do the
            # same, and camelCase is not hypothetical here — it is this project's API convention and
            # already carried in the redaction allowlist (ADR-049). Over-matching costs one warning.
            lowered = key.lower()
            if "token" in lowered:
                return True
            if "usage" in lowered and not _is_empty_value(value):
                return True
            if _mentions_usage(value, depth=depth - 1):
                return True
        return False
    if isinstance(node, list):
        return any(_mentions_usage(item, depth=depth - 1) for item in node)
    return False


def _is_empty_value(value: Any) -> bool:
    """``None`` or an empty container — i.e. a carrier that asserts "nothing", not "unreadable"."""
    if value is None:
        return True
    return isinstance(value, dict | list | str | tuple) and len(value) == 0


def _key_paths(node: Any, *, depth: int, prefix: str = "") -> list[str]:
    """Dotted key PATHS of a block (``usage.prompt_tokens``), depth-bounded. Names only, no values.

    What an operator needs from a "usage shape unknown" line is the names that did not match, and
    those are exactly what a top-level key list hides: a drift inside the carrier renders as
    ``['event', 'run_id', 'usage']``, which says nothing. With no capture of a completed run
    available (Q-067-10), this line is the only way the real field names can be learned at all —
    from production, after the fact. Paths only; VALUES are never collected, let alone logged.
    """
    if depth <= 0 or not isinstance(node, dict | list):
        return []
    paths: list[str] = []
    items = (
        node.items()
        if isinstance(node, dict)
        else ((str(index), v) for index, v in enumerate(node))
    )
    for key, value in items:
        path = f"{prefix}{key}"
        if isinstance(value, dict | list) and depth > 1 and value:
            paths.extend(_key_paths(value, depth=depth - 1, prefix=f"{path}."))
        else:
            paths.append(path)
    return paths


def _shape_summary(event: _SseEvent) -> list[str]:
    """Sorted, truncated key paths for a diagnostic line — bounded so a log line stays a line."""
    paths = sorted(set(_key_paths(event.data, depth=_USAGE_GATE_MAX_DEPTH)))
    return paths[:_SHAPE_SUMMARY_MAX_KEYS]


def _has_token_like_field(event: _SseEvent) -> bool:
    """True when the block carries an int field whose NAME mentions tokens (top level or nested).

    NOT the billing gate (that is :func:`_is_provably_usage_free`, which ignores value types on
    purpose). This one is the narrow "there really are numeric counts here we failed to read"
    signal, attached to the warning to separate a name drift (``prompt_tokens: 10``) from a type
    drift (``usage`` as a string/list). Names and types only — values are never logged.
    """
    sources: list[dict[str, Any]] = [event.data]
    nested = event.data.get("usage")
    if isinstance(nested, dict):
        sources.append(nested)
    for source in sources:
        for key, value in source.items():
            # Singular substring + case-folded: the drift class this reports includes `token_count`
            # and `inputTokens`, not only the plural snake_case spelling.
            if "token" in key.lower() and isinstance(value, int) and not isinstance(value, bool):
                return True
    return False


def _as_int_or_none(value: Any) -> int | None:
    """Strict variant of :func:`_as_int`: ``None`` when the value is not a real number.

    Separates "the count is 0" from "there is no usable count here", which :func:`_as_int` folds
    together — that folding is exactly how a string-valued count would bill as zero while reporting
    itself as recognised. ``bool`` is not a count (``True`` is not 1 token).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _extract_delta_text(event: _SseEvent) -> str:
    """Assistant text of a ``message.delta`` block for the snapshot / run.paused buffer (§3, §6).

    The exact Hermes shape is relayed as-is (external contract); this local buffer probes the known
    carriers defensively:

    * ``text`` — top-level string;
    * ``delta`` — **bare string**: the shape the patched production image (ADR-065) emits, as
      recorded in the raw SSE capture of a prod run and fixed as contract in
      docs/modules/agent-proxy/05-events.md. A ``{text: …}`` wrapper is also accepted for builds
      that nest it — defense-in-depth, NOT the source of truth;
    * ``content`` — top-level string / ``{text: …}`` wrapper.

    A miss still yields ``""`` (the buffer is a convenience snapshot, not an authoritative
    transcript — /events stays the source), but it is no longer silent: the caller counts text-less
    deltas and warns once the whole relay has produced nothing (``_DELTA_SILENT_WARN_AFTER``), which
    is the symptom the ADR-066 defect showed for an entire run without a single log line.
    """
    data = event.data
    for key in _DELTA_TEXT_KEYS:
        text = _as_event_text(data.get(key))
        if text:
            return text
    return ""


def _delta_shape_looks_unknown(event: _SseEvent) -> bool:
    """Per-event heuristic: this ``message.delta`` yielded no text and looks MALFORMED, not empty.

    Called only after :func:`_extract_delta_text` returned ``""``. True in two cases:

    1. a KNOWN carrier is present and non-empty, but nothing usable could be read out of it —
       ``{'delta': {'chunk': 'hi'}}``, ``{'delta': [{'text': 'hi'}]}``, ``{'delta': 42}``: the key
       stayed, its TYPE changed;
    2. no known carrier produced text, yet some key OUTSIDE the carriers and the envelope metadata
       carries text — ``{'text': '', 'message': 'hello'}``: the carrier was renamed.

    **What it does NOT cover, and why the aggregate latch exists.** Replaying the 15
    ``message.delta`` blocks of the committed capture
    (``tests/fixtures/hermes_prod_run_adr065.sse``) through the PRE-fix extractor plus this rule
    yields zero warnings: back then the value ``"delta": "<text>"`` was a plain non-empty string,
    so case 1 could not fire (a bare string IS usable — the old
    extractor simply refused to look) and case 2 could not either (the text sat under a known key).
    A per-event rule cannot flag a payload that looks perfectly normal; only "deltas came and the
    run text is still empty" can — the counting latch (``_DELTA_SILENT_WARN_AFTER``) and its
    terminal-event half (:meth:`AgentProxyService._warn_if_no_delta_text`). Treat this function as
    the fast, specific signal and the aggregate pair as the guarantee.

    Envelope keys are skipped so a legitimately empty delta stays silent: ``event``/``run_id`` are
    non-empty strings on EVERY delta, and warning on each of them would train the reader to ignore
    the line. Only KEY SHAPE is inspected and only key names are ever logged — the values are user
    content (ADR-066 §6).
    """
    for key in _DELTA_TEXT_KEYS:
        value = event.data.get(key)
        if value is None or value == "":
            continue  # absent, or present-and-empty => a legitimately empty delta, not a drift
        if _as_event_text(value) is None:
            return True  # known carrier, unusable shape (dict without text/content/value, list, …)
    for key, value in event.data.items():
        if key in _DELTA_ENVELOPE_KEYS:
            continue
        if _as_event_text(value):
            return True
    return False


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


# --- shared with the background consumer (ADR-067) ---------------------------------------------
# The SSE reader and the terminal-event test are used by BOTH contours since the consumer took over
# the domain duties (ADR-067 §2). Exported under public names rather than importing the underscore
# ones across modules; the private names stay because the ADR-065 regression tests reference them.
iter_sse_blocks = _iter_sse_blocks


def is_terminal_event(block: _SseEvent) -> bool:
    """Whether a parsed event ends the run: ``run.completed`` / ``run.failed`` / ``run.paused``.

    ``run.paused`` is included although it is SYNTHETIC — generated by us on pause-at-zero rather
    than sent by Hermes (ADR-064 §3) — because for every consumer of this predicate the question is
    "does anything follow?", and after a pause nothing does.
    """
    return _event_name(block) in (_EVENT_RUN_COMPLETED, _EVENT_RUN_FAILED, _EVENT_RUN_PAUSED)
