"""Unit: client API-KEY auth + OpenAPI AND-merge (ADR-044).

Pure tests (no DB / no I/O):
- verify_client_api_key: missing / wrong / blank header → 401; valid → pass; constant-time
  (hmac.compare_digest) is used so an empty configured key never matches a blank header.
- rotation: CLIENT_API_KEY_PREV is accepted during the grace window; junk is not.
- _merge_client_contour_security: collapses the FastAPI OR pair into a single AND object, and
  leaves admin / adapty / public requirements untouched.

Settings are exercised by constructing Settings(...) directly (env-independent) and patching the
``get_settings`` symbol that auth.py reads, so these unit tests do not depend on the live env.
"""

from __future__ import annotations

import pytest

from app.api_gateway import auth as auth_mod
from app.config import Settings
from app.errors import UnauthorizedError
from app.main import _merge_client_contour_security

_PRIMARY = "primary-client-key-0123456789abcdef0123456789abcdef"
_PREV = "previous-client-key-fedcba9876543210fedcba9876543210"


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, key: str, prev: str = "") -> None:
    settings = Settings(CLIENT_API_KEY=key, CLIENT_API_KEY_PREV=prev)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)


# ----------------------------- verify_client_api_key -----------------------------
def test_valid_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, key=_PRIMARY)
    # No exception == authenticated.
    auth_mod.verify_client_api_key(_PRIMARY)


def test_missing_key_raises_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, key=_PRIMARY)
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key(None)


def test_wrong_key_raises_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, key=_PRIMARY)
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key("not-the-key")


def test_blank_presented_header_raises_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty presented value must never authenticate, even against a configured key.
    _patch_settings(monkeypatch, key=_PRIMARY)
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key("")


def test_blank_configured_key_never_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset/blank CLIENT_API_KEY → no presented value (incl. "") authenticates.
    _patch_settings(monkeypatch, key="")
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key("")
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key("anything")


def test_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard against a regression to plain ==: the matcher must go through hmac.compare_digest.
    _patch_settings(monkeypatch, key=_PRIMARY)
    calls: list[tuple[str, str]] = []
    real = auth_mod.hmac.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", _spy)
    auth_mod.verify_client_api_key(_PRIMARY)
    assert calls, "verify_client_api_key must use hmac.compare_digest"


# ----------------------------- rotation -----------------------------
def test_rotation_prev_key_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, key=_PRIMARY, prev=_PREV)
    auth_mod.verify_client_api_key(_PRIMARY)  # primary still works
    auth_mod.verify_client_api_key(_PREV)  # prev accepted during grace window


def test_rotation_junk_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, key=_PRIMARY, prev=_PREV)
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key("garbage-not-primary-nor-prev")


def test_rotation_blank_prev_never_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty CLIENT_API_KEY_PREV (the default) must not authenticate a blank header.
    _patch_settings(monkeypatch, key=_PRIMARY, prev="")
    with pytest.raises(UnauthorizedError):
        auth_mod.verify_client_api_key("")


# ----------------------------- _merge_client_contour_security -----------------------------
def test_merge_or_pair_into_single_and_object() -> None:
    # FastAPI default OR form → one AND requirement object with BOTH keys.
    or_form = [{"clientApiKey": []}, {"userId": []}]
    merged = _merge_client_contour_security(or_form)
    assert merged == [{"clientApiKey": [], "userId": []}]


def test_merge_idempotent_on_already_merged() -> None:
    already = [{"clientApiKey": [], "userId": []}]
    assert _merge_client_contour_security(already) == [{"clientApiKey": [], "userId": []}]


def test_merge_leaves_admin_untouched() -> None:
    admin = [{"adminToken": []}]
    assert _merge_client_contour_security(admin) == admin


def test_merge_leaves_adapty_untouched() -> None:
    adapty = [{"adaptyWebhook": []}]
    assert _merge_client_contour_security(adapty) == adapty


def test_merge_leaves_partial_contour_untouched() -> None:
    # Only one of the pair present (should not happen for client ops, but must not be mangled).
    only_key = [{"clientApiKey": []}]
    assert _merge_client_contour_security(only_key) == only_key
