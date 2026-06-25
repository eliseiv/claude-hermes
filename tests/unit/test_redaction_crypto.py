"""Unit tests for secret redaction (AC-7) and KMS envelope crypto (AC-4/ADR-003)."""

from __future__ import annotations

import base64
import os

import pytest

from app.byok.kms import LocalKmsClient
from app.observability.redaction import REDACTED, assert_no_secrets, redact


# --- Redaction (logs/audit must never carry secrets) ---
def test_redact_sensitive_keys() -> None:
    payload = {
        "apiKey": "sk-ant-secret",
        "authorization": "Bearer abc",
        "token": "jwt.value",
        "nested": {"secret": "x", "password": "p"},
        "transaction": "jws...",
    }
    out = redact(payload)
    assert out["apiKey"] == REDACTED
    assert out["authorization"] == REDACTED
    assert out["token"] == REDACTED
    assert out["nested"]["secret"] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert out["transaction"] == REDACTED


def test_redact_keeps_status_metadata() -> None:
    # keyStatus must survive (AC-7 byok_change audit needs valid|invalid|missing).
    out = redact({"keyStatus": "valid", "byokEnabled": True})
    assert out["keyStatus"] == "valid"
    assert out["byokEnabled"] is True


def test_redact_recurses_lists() -> None:
    out = redact({"items": [{"apiKey": "x"}, {"ok": 1}]})
    assert out["items"][0]["apiKey"] == REDACTED
    assert out["items"][1]["ok"] == 1


def test_assert_no_secrets_returns_copy() -> None:
    src = {"apiKey": "x"}
    out = assert_no_secrets(src)
    assert out["apiKey"] == REDACTED
    assert src["apiKey"] == "x"  # original untouched


# --- ADR-049: usage token-COUNT allowlist survives redaction; real secrets still redacted ---
def test_redact_keeps_usage_token_counts_snake_case() -> None:
    # input_tokens/output_tokens/total_tokens are billing analytics, not secrets (ADR-049).
    out = redact({"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
    assert out == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    assert all(isinstance(out[k], int) for k in out)


def test_redact_keeps_usage_token_counts_camel_case() -> None:
    # chat path emits camelCase; key is lowercased before allowlist lookup (ADR-049).
    out = redact({"inputTokens": 1, "outputTokens": 2, "totalTokens": 3})
    assert out == {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3}


def test_redact_still_redacts_real_token_secrets() -> None:
    # SECURITY: the allowlist is a closed exact-match set; ALL real token/secret fields
    # remain redacted (the "token"/"secret"/... denylist substrings still win).
    secrets = {
        "identityToken": "x",
        "push_token": "y",
        "x-admin-token": "z",
        "access_token": "a",
        "refresh_token": "b",
        "bearer_token": "c",
        "api_token": "d",
        "api_key": "e",
        "apiKey": "f",
        "authorization": "g",
        "secret": "h",
        "password": "i",
        "credential": "j",
    }
    out = redact(secrets)
    assert all(out[k] == REDACTED for k in secrets), out


def test_redact_redacts_non_allowlisted_token_count() -> None:
    # Boundary: cache_read_input_tokens is NOT in the closed allowlist but contains
    # "token" → redacted (expected per ADR-049).
    out = redact({"cache_read_input_tokens": 5})
    assert out["cache_read_input_tokens"] == REDACTED


def test_redact_usage_nested_in_audit_payload() -> None:
    # usage block nested in an audit payload is preserved via recursion (ADR-047 §6).
    out = redact({"usage": {"input_tokens": 10}})
    assert out["usage"]["input_tokens"] == 10


# --- KMS envelope crypto round-trip ---
def test_kms_dek_round_trip() -> None:
    master = os.urandom(32)
    kms = LocalKmsClient(master)
    dek = os.urandom(32)
    wrapped = kms.encrypt_dek(dek)
    assert wrapped != dek
    assert kms.decrypt_dek(wrapped) == dek


def test_kms_rejects_bad_master_key_length() -> None:
    with pytest.raises(ValueError):
        LocalKmsClient(b"short")


def test_kms_ciphertext_nondeterministic() -> None:
    kms = LocalKmsClient(base64.b64decode("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="))
    dek = os.urandom(32)
    assert kms.encrypt_dek(dek) != kms.encrypt_dek(dek)  # random nonce
