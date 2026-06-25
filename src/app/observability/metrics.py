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


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
