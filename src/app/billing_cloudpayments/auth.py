"""Observational (non-blocking) dependency for the PUBLIC RU CloudPayments webhook (ADR-068).

broadapps sends the payment callback WITHOUT any authorization (``authScheme=none``). Requiring
a token would mean a permanent ``401`` and lost payments. The trust anchor is the outgoing
broadapps API verification done with our ``CLOUDPAYMENTS_API_TOKEN``.

``require_cloudpayments_webhook`` never raises. It records one observational log and keeps the
``cloudPaymentsWebhook`` OpenAPI scheme as decorative.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import cloudpayments_webhook_scheme
from app.config import get_settings
from app.observability.logging import log_event

logger = logging.getLogger(__name__)

_AUTH_HEADER_ALLOWLIST = (
    "authorization",
    "x-api-key",
    "x-signature",
    "x-sign",
    "x-webhook-signature",
    "x-content-hmac",
    "content-hmac",
    "signature",
)


def _extract_webhook_credential(authorization: str | None) -> str | None:
    """Lenient extraction of a presented credential from a raw ``Authorization`` header."""
    if authorization is None:
        return None
    value = authorization.strip()
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        rest = parts[1].strip()
        return rest or None
    return value


def _auth_scheme_label(authorization: str | None) -> str:
    """Return the scheme WORD only (never the token value) for the observational log."""
    if authorization is None:
        return "none"
    value = authorization.strip()
    if not value:
        return "empty"
    parts = value.split(None, 1)
    return parts[0].lower() if len(parts) == 2 else "raw"


def _log_auth_observed(request: Request) -> None:
    """Emit one non-blocking ``cloudpayments_webhook_auth_observed`` record."""
    header = request.headers.get("authorization")
    secret = get_settings().cloudpayments_webhook_token
    if secret:
        candidate = _extract_webhook_credential(header) or ""
        matched = hmac.compare_digest(candidate, secret)
    else:
        matched = False
    present = [name for name in _AUTH_HEADER_ALLOWLIST if name in request.headers]
    log_event(
        logger,
        logging.INFO,
        "cloudpayments_webhook_auth_observed",
        matched=matched,
        authScheme=_auth_scheme_label(header),
        presentAuthHeaders=present,
    )


def require_cloudpayments_webhook(
    request: Request,
    _scheme: Annotated[
        HTTPAuthorizationCredentials | None, Depends(cloudpayments_webhook_scheme)
    ] = None,
) -> None:
    """Observational, non-blocking auth dependency for the PUBLIC webhook. NEVER raises."""
    _log_auth_observed(request)
