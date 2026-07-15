"""Unit: redaction carve-out for cumulative usage token-counts (TD-035 / ADR-064 §7, ADR-049).

The ``usage.delta`` / ``run.paused`` meta carries ``cumulative_input_tokens`` /
``cumulative_output_tokens`` (integer billing analytics, NOT secrets). These MUST survive
``redact()`` (they are in the closed-set allowlist) so usage is preserved wherever meta.usage is
routed through the audit/log redactor. At the same time the generic ``token`` denylist substring
MUST still redact every REAL secret (``api_key``/``authorization``/``API_SERVER_KEY``/…). This
guards both halves so the allowlist can never be widened into a secret leak.
"""

from __future__ import annotations

from app.observability.redaction import REDACTED, assert_no_secrets, redact


def test_cumulative_usage_counts_survive_redaction() -> None:
    # The exact meta.usage shape the agent per-step biller writes (ADR-064 §7).
    payload = {
        "runId": "run_1",
        "stepIndex": 3,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 45,
            "cumulative_input_tokens": 2000,
            "cumulative_output_tokens": 1000,
            "cumulative_total_tokens": 3000,
        },
        "model": "m",
    }
    out = redact(payload)
    assert out["usage"]["cumulative_input_tokens"] == 2000
    assert out["usage"]["cumulative_output_tokens"] == 1000
    assert out["usage"]["cumulative_total_tokens"] == 3000
    # Per-step delta counts survive too (input_tokens/output_tokens allowlist).
    assert out["usage"]["input_tokens"] == 120
    assert out["usage"]["output_tokens"] == 45


def test_camelcase_cumulative_counts_survive_redaction() -> None:
    # The lookup lowercases the key, so a camelCase carrier is covered by the closed allowlist.
    payload = {
        "usage": {
            "cumulativeInputTokens": 10,
            "cumulativeOutputTokens": 20,
            "cumulativeTotalTokens": 30,
        }
    }
    out = redact(payload)
    assert out["usage"] == {
        "cumulativeInputTokens": 10,
        "cumulativeOutputTokens": 20,
        "cumulativeTotalTokens": 30,
    }


def test_real_secrets_are_still_redacted_alongside_usage_counts() -> None:
    # The allowlist opens the usage counts but the `token`/`key`/`authorization` denylist still
    # redacts every real secret in the SAME payload (no widening into a leak).
    payload = {
        "cumulative_input_tokens": 2000,  # survives
        "api_key": "sk-ant-super-secret",  # redacted (exact apikey after lowercase? no — substring)
        "apiKey": "sk-ant-camel",  # redacted (_DENY_EXACT apikey)
        "authorization": "Bearer leak-me",  # redacted (authorization substring)
        "access_token": "tok-leak",  # redacted (token substring, NOT a usage count)
        "API_SERVER_KEY": "instance-bearer",  # redacted (key substring)
        "client_secret": "shh",  # redacted (secret substring)
    }
    out = redact(payload)
    assert out["cumulative_input_tokens"] == 2000
    assert out["api_key"] == REDACTED
    assert out["apiKey"] == REDACTED
    assert out["authorization"] == REDACTED
    assert out["access_token"] == REDACTED
    assert out["API_SERVER_KEY"] == REDACTED
    assert out["client_secret"] == REDACTED


def test_assert_no_secrets_guard_preserves_usage_and_redacts_secrets() -> None:
    # The audit guard (assert_no_secrets) applies the same policy — a billing_debit_insufficient /
    # agent_run audit payload keeps its usage counts and still loses any secret.
    payload = {
        "runId": "run_1",
        "usage": {"cumulative_input_tokens": 5, "cumulative_output_tokens": 7},
        "authorization": "Bearer leak",
    }
    out = assert_no_secrets(payload)
    assert out["usage"] == {"cumulative_input_tokens": 5, "cumulative_output_tokens": 7}
    assert out["authorization"] == REDACTED


def test_plain_token_key_without_usage_prefix_is_redacted() -> None:
    # A bare "token"/"secret_token" is NOT a usage count → must be redacted (the carve-out is an
    # EXACT closed set, never a prefix/substring match).
    payload = {"token": "leak", "refresh_token": "leak2", "session_token": "leak3"}
    out = redact(payload)
    assert out == {"token": REDACTED, "refresh_token": REDACTED, "session_token": REDACTED}
