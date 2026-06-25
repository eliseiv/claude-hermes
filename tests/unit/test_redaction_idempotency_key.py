"""Unit: redaction allowlist for idempotencyKey (ADR-050) and usage token-counts (ADR-049).

Enumerated-contour coverage of the ADR-050/ADR-049 redaction invariants (task C11/C12):
- idempotencyKey / idempotency_key (both casings) SURVIVE redaction (dedup-key ≠ secret).
- usage token-counts (input_tokens/output_tokens/total_tokens, both casings) SURVIVE.
- the secret denylist is NOT weakened: api_key, API_SERVER_KEY, CLIENT_API_KEY, encrypted_key,
  encrypted_dek (defense-in-depth, ADR-050 §needs_code_sync), dek, x-admin-token, authorization,
  access_token are ALL ***REDACTED***.

Each name listed in the task is asserted DIRECTLY (not "covered by the same substring rule") so a
regression that opens a single secret cannot pass silently.
"""

from __future__ import annotations

import pytest

from app.observability.redaction import REDACTED, redact


# --------------------------- C11: idempotencyKey survives (both casings) ------------------------
@pytest.mark.parametrize("key", ["idempotencyKey", "idempotency_key"])
def test_idempotency_key_survives_redaction_both_casings(key: str) -> None:
    # ADR-050: the dedup-key is a client/operator identifier, NOT a secret. The audit contract
    # (ADR-048 §2 / admin/06-rbac.md) REQUIRES it carried verbatim for traceability, despite
    # containing the "key" denylist substring.
    out = redact({key: "sub-grant-42"})
    assert out[key] == "sub-grant-42"


def test_idempotency_key_survives_nested_in_audit_payload() -> None:
    # The admin_subscription_grant audit payload nests idempotencyKey alongside redacted siblings:
    # the carve-out preserves the dedup-key while a real secret in the same dict is still redacted.
    payload = {
        "actor": "admin",
        "idempotencyKey": "sub-grant-99",
        "x-admin-token": "ADMIN-SECRET",
    }
    out = redact(payload)
    assert out["idempotencyKey"] == "sub-grant-99"
    assert out["x-admin-token"] == REDACTED


# --------------------------- C11: usage token-counts survive (both casings) ---------------------
def test_usage_token_counts_survive_snake_case() -> None:
    out = redact({"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
    assert out == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}


def test_usage_token_counts_survive_camel_case() -> None:
    # camelCase is lowercased before the allowlist lookup (ADR-049).
    out = redact({"inputTokens": 1, "outputTokens": 2, "totalTokens": 3})
    assert out == {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3}


# --------------------------- C12: every real secret name is STILL redacted ----------------------
# Each entry is asserted by name (enumerated-contour) so opening ANY one is caught directly.
# Includes encrypted_dek (ADR-050 defense-in-depth: previously NOT in _DENY_EXACT and lacking a
# "key" substring match — must now be redacted by construction).
_REAL_SECRETS = [
    "api_key",
    "API_SERVER_KEY",
    "CLIENT_API_KEY",
    "encrypted_key",
    "encrypted_dek",
    "dek",
    "x-admin-token",
    "authorization",
    "access_token",
]


@pytest.mark.parametrize("name", _REAL_SECRETS)
def test_real_secret_is_redacted(name: str) -> None:
    out = redact({name: "super-secret-value"})
    assert out[name] == REDACTED, f"{name} must be redacted, got {out[name]!r}"


def test_all_real_secrets_redacted_in_one_payload() -> None:
    # Belt-and-suspenders: a single payload carrying every real secret name → all REDACTED.
    payload = {name: f"v-{i}" for i, name in enumerate(_REAL_SECRETS)}
    out = redact(payload)
    assert all(out[name] == REDACTED for name in _REAL_SECRETS), out


def test_encrypted_dek_defense_in_depth_redacted() -> None:
    # ADR-050 §needs_code_sync closes a pre-existing gap: encrypted_dek had neither a _DENY_EXACT
    # entry nor a "key"/"token"/"secret" substring → it used to pass through. MUST be redacted now.
    out = redact({"encrypted_dek": b"\x00\x01wrapped-dek-bytes"})
    assert out["encrypted_dek"] == REDACTED


def test_allowlist_does_not_open_lookalike_secret() -> None:
    # Boundary: the allowlist is a closed exact-match set. A lookalike key that merely CONTAINS
    # "idempotency" but is not the exact dedup-key name still falls under the "key" denylist.
    out = redact({"idempotency_api_key": "leak"})
    assert out["idempotency_api_key"] == REDACTED
