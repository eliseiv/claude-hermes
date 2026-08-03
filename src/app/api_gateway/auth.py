"""JWT authentication (RS256) with JWKS or static public key (05-security.md, Q-005-1).

Verifies signature, exp, iss, aud. Extracts sub (userId) and device_id. Never logs the
token. JWKS keys are cached for a short TTL.

Also hosts ``require_admin`` (ADR-009): the isolated admin authorization, fully separate from
``get_current_user`` — different secret, header and dependency, no provisioning/trial.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import Annotated

import httpx
import jwt
from fastapi import Depends
from jwt import PyJWKClient

from app.api_gateway.openapi_security import admin_key_scheme, admin_scheme
from app.config import get_settings
from app.errors import ForbiddenError, UnauthorizedError


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    device_id: str | None


class JwtVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self._issuer = settings.jwt_issuer or None
        self._audience = settings.jwt_audience or None
        self._jwks_url = settings.jwt_jwks_url or None
        # Resolve the public key with file-path priority over the \n-escaped string (ADR-018 §7).
        # verify() logic itself is unchanged; this only broadens how the key is sourced.
        self._public_key = settings.resolve_public_key() or None
        # PyJWKClient keeps a per-kid cache internally, so token rotation / multiple kids
        # each resolve their own signing key. lifespan bounds how long a JWKS fetch is reused.
        self._jwks_client: PyJWKClient | None = (
            PyJWKClient(
                self._jwks_url,
                cache_keys=True,
                lifespan=settings.jwks_cache_ttl_seconds,
            )
            if self._jwks_url
            else None
        )

    def _signing_key(self, token: str) -> object:
        if self._jwks_client is not None:
            try:
                return self._jwks_client.get_signing_key_from_jwt(token).key
            except (jwt.PyJWKClientError, httpx.HTTPError) as exc:
                raise UnauthorizedError("unable to resolve signing key") from exc
        if self._public_key:
            return self._public_key
        raise UnauthorizedError("no JWT verification key configured")

    def verify(self, token: str) -> AuthenticatedUser:
        key = self._signing_key(token)
        options = {"require": ["exp", "sub"], "verify_aud": self._audience is not None}
        try:
            claims = jwt.decode(
                token,
                key=key,  # type: ignore[arg-type]
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options=options,
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid token") from exc

        sub = claims.get("sub")
        if not sub:
            raise UnauthorizedError("missing sub")
        try:
            user_id = uuid.UUID(str(sub))
        except ValueError as exc:
            raise UnauthorizedError("sub is not a valid user id") from exc
        return AuthenticatedUser(user_id=user_id, device_id=claims.get("device_id"))


_verifier_singleton: JwtVerifier | None = None


def get_jwt_verifier() -> JwtVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = JwtVerifier()
    return _verifier_singleton


def _client_api_key_matches(presented: str) -> bool:
    """Constant-time compare the presented X-API-Key against the active client key(s) (ADR-044).

    Accepts a match with CLIENT_API_KEY or (during rotation) CLIENT_API_KEY_PREV. Both comparisons
    are constant-time (``hmac.compare_digest``). An empty/unset configured key never matches (so a
    blank header can never authenticate). Both candidates are always evaluated to avoid early-exit
    timing leaks, mirroring ``_admin_token_matches`` (ADR-044 §1, ADR-009 §3/§5).
    """
    settings = get_settings()
    matched = False
    for candidate in (settings.client_api_key, settings.client_api_key_prev):
        if candidate and hmac.compare_digest(presented, candidate):
            matched = True
    return matched


def verify_client_api_key(presented: str | None) -> None:
    """Authenticate the client contour by the trusted X-API-Key (ADR-044 §1).

    Pure, side-effect-free (no DB, no logging of the key) so it stays unit-testable in isolation
    and mirrors the admin path (``_admin_token_matches`` / ``require_admin``). A missing or
    mismatching key raises 401 without revealing the reason (the key is the only auth factor; the
    subject identity comes separately from ``X-User-Id`` in ``get_current_user``). The key is never
    logged (redaction allowlist covers ``X-API-Key``, ADR-044 §3).
    """
    if presented is None or not _client_api_key_matches(presented):
        raise UnauthorizedError("invalid client api key")


def _admin_secret_candidates(settings: object) -> tuple[str, ...]:
    """All configured admin secrets (primary, rotation, CRM alias)."""
    from app.config import Settings

    s: Settings = settings  # type: ignore[assignment]
    return tuple(
        candidate
        for candidate in (
            s.admin_api_secret,
            s.admin_api_secret_prev,
            s.admin_api_key,
        )
        if candidate
    )


def _admin_token_matches(presented: str) -> bool:
    """Constant-time compare the presented admin secret against configured key(s)."""
    settings = get_settings()
    matched = False
    for candidate in _admin_secret_candidates(settings):
        if hmac.compare_digest(presented, candidate):
            matched = True
    return matched


async def require_admin(
    x_admin_token: Annotated[str | None, Depends(admin_scheme)] = None,
    x_admin_key: Annotated[str | None, Depends(admin_key_scheme)] = None,
) -> None:
    """Authorize an admin request via X-Admin-Token or X-Admin-Key (CRM alias).

    Missing header → 403 (broad-crm contract). Wrong secret → 401. Empty configured secret
    never matches (fail-closed).
    """
    presented = x_admin_token if x_admin_token is not None else x_admin_key
    if presented is None:
        raise ForbiddenError("admin credentials required")
    if not _admin_token_matches(presented):
        raise UnauthorizedError("invalid admin token")
