"""Prometheus metrics (01-architecture.md#наблюдаемость)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

chat_run_latency_seconds = Histogram(
    "chat_run_latency_seconds",
    "Latency of chat orchestration (policy + orchestrator + db), excluding Anthropic.",
)
blocked_requests_total = Counter(
    "blocked_requests_total",
    "Count of business-blocked requests by reason.",
    ["reason"],
)
wallet_debit_total = Counter(
    "wallet_debit_total",
    "Count of wallet debit attempts by result.",
    ["result"],
)
tool_call_roundtrip_latency_seconds = Histogram(
    "tool_call_roundtrip_latency_seconds",
    "Latency from tool_call initiation to tool_result handling.",
)
byok_usage_share = Gauge(
    "byok_usage_share",
    "Share of chat requests using BYOK mode.",
)
token_usage_total = Counter(
    "token_usage_total",
    "Total tokens by direction and model.",
    ["direction", "model"],
)
# Admin (ADM-7): grant outcomes by result (success | conflict | not_found).
admin_grant_total = Counter(
    "admin_grant_total",
    "Count of admin credit-grant attempts by result.",
    ["result"],
)
# Admin subscription grant (ADR-048 §2, ADM-11): manual subscription activation outcomes
# by result (success | conflict | not_found).
admin_subscription_grant_total = Counter(
    "admin_subscription_grant_total",
    "Count of admin subscription-grant attempts by result.",
    ["result"],
)
# Admin wallet debit (ADR-061, ADM-14): operator credit-debit outcomes by result
# (success | conflict | insufficient | not_found).
admin_debit_total = Counter(
    "admin_debit_total",
    "Count of admin wallet-debit attempts by result.",
    ["result"],
)
# Token purchase (ADR-015): consumable purchase outcomes by result
# (granted | replay | unknown_product | invalid_transaction | forbidden).
token_purchase_total = Counter(
    "token_purchase_total",
    "Count of consumable token-purchase attempts by result.",
    ["result"],
)
# Website builder (WB-8).
site_file_write_total = Counter(
    "site_file_write_total",
    "Count of site.write_file tool executions by result.",
    ["result"],
)
preview_request_total = Counter(
    "preview_request_total",
    "Count of preview endpoint requests by result (ok | forbidden | not_found).",
    ["result"],
)
# Anthropic upstream errors (TD-014): bounded enum labels only (no user-content).
# status_code is the numeric HTTP status or "none" for timeout/connection errors;
# error_type is the Anthropic error.type (or "unknown" when the body has none).
# KEPT for existing dashboards/tests; the generalized provider-labeled metric below is the
# ADR-033 §10 unified series (both are incremented on the Anthropic path).
anthropic_upstream_errors_total = Counter(
    "anthropic_upstream_errors_total",
    "Count of Anthropic upstream errors by status_code and error_type.",
    ["status_code", "error_type"],
)
# Generalized LLM upstream errors (ADR-033 §10): provider-labeled unified series for both
# Anthropic and OpenAI. provider ∈ {anthropic, openai}; status_code is the numeric HTTP status or
# "none" for timeout/connection errors; error_type is the provider error.type / exception class
# (or "unknown"). Bounded enum labels only (no user-content).
llm_upstream_errors_total = Counter(
    "llm_upstream_errors_total",
    "Count of LLM upstream errors by provider, status_code and error_type.",
    ["provider", "status_code", "error_type"],
)
# Agent-run launch-path upstream timeouts (ADR-067 §5.1 tripwire, Q-067-17). ADR-067 §5.1 (forcibly
# unwedging a user's Hermes instance) was withdrawn from v1 because its premise was disproved — and
# withdrawn AGAINST THIS SAFETY NET: if instance muteness is ever real, it surfaces here as
# 502 upstream_timeout, and this counter is the only thing that can say so. Nothing observed it
# before (anthropic_upstream_errors_total is a different contour).
#
# ⚠️ phase ∈ {connect, readiness, launch, hydrate, budget} — a BOUNDED enum, and deliberately NOT
# labelled by userId even though clustering by user is exactly what the tripwire is looking for:
# a user id in a label is unbounded cardinality plus user content in a metric, against this file's
# standing convention. Signal and diagnosis are therefore split — the counter says WHETHER anything
# is wrong, the `agent_run_launch_upstream_timeout` log event (userId, runId, duration) says WHO and
# WHICH RUN. Read the counter first, then the logs.
agent_run_launch_upstream_timeout_total = Counter(
    "agent_run_launch_upstream_timeout_total",
    "Count of agent-run launch-path upstream timeouts by phase.",
    ["phase"],
)
# Orphan runs finalized by the ADR-067 §5 sweep, by the BASIS of their billing (§5.2):
# snapshot (a non-zero cumulative was observed) | zero_usage (a row exists, usage was zero) |
# no_snapshot (no row at all — usage was never observed). Bounded enum labels only; the runId and
# userId of each finalization live in the agent_run_orphan_finalized audit record, not here.
#
# The three are NOT interchangeable: a non-zero rate of `no_snapshot` means consumers are failing to
# start at all, which is a revenue incident to be read on its own and never averaged into the
# healthy `snapshot` case.
agent_run_orphan_finalized_total = Counter(
    "agent_run_orphan_finalized_total",
    "Count of orphan agent runs finalized by the sweep, by billing basis.",
    ["basis"],
)


# Concurrency of the agent-run contour (ADR-067, Q-067-2 PRECONDITION). Three ceilings are shared by
# every simultaneous run and stream — DB pool connections, Redis subscriptions and ring memory — and
# until these two gauges existed NONE of them was observable before it was hit: `/events` streams
# were not counted anywhere, and the live-consumer count existed as a property nothing ever read.
#
# ⚠️ They are the precondition of Q-067-2, not its answer: choosing a cap is meaningless while the
# quantity it would cap is unmeasured. No labels at all — a run or user id here would be unbounded
# cardinality and user content in a metric, against this file's standing convention.
agent_run_consumers_active = Gauge(
    "agent_run_consumers_active",
    "Background agent-run consumers currently running in this worker.",
)
agent_run_event_streams_active = Gauge(
    "agent_run_event_streams_active",
    "Client /events streams currently open in this worker.",
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
