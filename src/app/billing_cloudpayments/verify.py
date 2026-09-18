"""Outgoing broadapps payment verification + pure reconciliation (ADR-068).

The RU CloudPayments callback carries no signature and no auth, so it is only a TRIGGER. The
single trusted "payment happened" signal is the broadapps API, queried with our API token.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.errors import CloudPaymentsVerificationUnavailableError
from app.observability.logging import log_event

logger = logging.getLogger(__name__)

_VERIFY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CreditablePayment:
    """A broadapps payment that passed reconciliation and is ready to credit."""

    payment_id: str
    product_code: str
    payment_type: str
    status: str
    paid_at: datetime.datetime


class CloudPaymentsVerifyClient:
    """Queries broadapps for a device's payments. Stateless — needs only settings (no DB)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        """``GET {api_base}/users/{device_id}/payments`` and return the raw ``data`` list."""
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/users/{device_id}/payments"
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise self._unavailable("timeout") from exc
        except httpx.RequestError as exc:
            raise self._unavailable("connect_error") from exc

        if response.status_code == 404:
            return []
        if not (200 <= response.status_code < 300):
            raise self._unavailable("upstream_status")

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._unavailable("malformed_response") from exc
        if not isinstance(body, dict):
            raise self._unavailable("malformed_response")
        data = body.get("data")
        if not isinstance(data, list):
            raise self._unavailable("malformed_response")
        return [item for item in data if isinstance(item, dict)]

    def _unavailable(self, reason: str) -> CloudPaymentsVerificationUnavailableError:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_verify_outcome",
            verify="api_error",
            reason=reason,
        )
        return CloudPaymentsVerificationUnavailableError("cloudpayments verification unavailable")


def _parse_paid_at(value: Any) -> datetime.datetime | None:
    """Parse a broadapps ``paid_at`` (ISO-8601) -> aware UTC. Unparseable -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def payment_statuses(data: list[dict[str, Any]]) -> list[str]:
    """The raw broadapps ``status`` strings from ``data[]`` for the outcome log."""
    return [str(item.get("status")) for item in data if isinstance(item, dict)]


def select_creditable_payments(
    data: list[dict[str, Any]],
    *,
    paid_statuses: frozenset[str],
    now: datetime.datetime,
    freshness_hours: int,
) -> list[CreditablePayment]:
    """Pure reconciliation: pick creditable payments from a broadapps ``data`` list."""
    cutoff = now - datetime.timedelta(hours=freshness_hours)
    creditable: list[CreditablePayment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in paid_statuses:
            continue
        paid_at = _parse_paid_at(item.get("paid_at"))
        if paid_at is None or paid_at < cutoff:
            continue
        payment_id = item.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id.strip():
            continue
        product = item.get("product")
        if not isinstance(product, dict):
            continue
        product_code = product.get("code")
        payment_type = product.get("payment_type")
        if not isinstance(product_code, str) or not product_code.strip():
            continue
        if not isinstance(payment_type, str) or not payment_type.strip():
            continue
        creditable.append(
            CreditablePayment(
                payment_id=payment_id.strip(),
                product_code=product_code.strip(),
                payment_type=payment_type.strip().lower(),
                status=status,
                paid_at=paid_at,
            )
        )
    return creditable
