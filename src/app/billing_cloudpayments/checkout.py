"""Outgoing broadapps ``/payments/link`` call that creates a RU payment link (ADR-068)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.billing_cloudpayments.parser import KIND_TOKENS, KIND_UNKNOWN, classify_product
from app.config import Settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import log_event

logger = logging.getLogger(__name__)

_CHECKOUT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CheckoutResult:
    """Passthrough of the broadapps payment-link response."""

    payment_id: str
    payment_url: str
    status: str
    expires_at: str | None


class CloudPaymentsCheckoutClient:
    """Creates a RU payment link via broadapps. Passthrough — no DB, no persisted state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate_product(self, product_id: str) -> None:
        """Allowlist gate symmetric with the webhook: only issue a link we could later credit."""
        one_time_ids = frozenset(self._settings.token_products())
        kind = classify_product(product_id, None, one_time_ids)
        if kind == KIND_UNKNOWN:
            raise ValidationFailedError("unknown_product")
        if kind == KIND_TOKENS and (self._settings.token_products().get(product_id) or 0) <= 0:
            raise ValidationFailedError("unknown_product")

    async def list_products(self) -> list[dict[str, Any]] | None:
        """Fetch the broadapps product catalog for this app (GET /apps/{app_id}/products).

        Returns the raw ``data`` list of product dicts, or ``None`` on any failure / unconfigured
        app (the caller then falls back to the static catalog). Never raises; token never logged.
        """
        settings = self._settings
        if not settings.cloudpayments_app_id or not settings.cloudpayments_api_token:
            return None
        url = f"{settings.cloudpayments_api_base}/apps/{settings.cloudpayments_app_id}/products"
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
            if not (200 <= resp.status_code < 300):
                return None
            body = resp.json()
        except (httpx.HTTPError, ValueError, UnicodeDecodeError):
            return None
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else None

    async def create_payment_link(
        self, *, user_id: uuid.UUID, product_id: str, customer_email: str
    ) -> CheckoutResult:
        """POST broadapps ``/payments/link`` and return the created link.

        ``user_id`` is the authenticated subject, never a client-supplied value.
        """
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/payments/link"
        files: dict[str, tuple[None, str]] = {
            "app_id": (None, settings.cloudpayments_app_id),
            "product_id": (None, product_id),
            "user_id": (None, str(user_id)),
            "customer_email": (None, customer_email),
        }
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                response = await client.post(url, files=files, headers=headers)
        except httpx.TimeoutException as exc:
            raise self._upstream_error("timeout", user_id=user_id, product_id=product_id) from exc
        except httpx.RequestError as exc:
            raise self._upstream_error(
                "connect_error", user_id=user_id, product_id=product_id
            ) from exc

        if not (200 <= response.status_code < 300):
            raise self._upstream_error("upstream_status", user_id=user_id, product_id=product_id)

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._upstream_error(
                "malformed_response", user_id=user_id, product_id=product_id
            ) from exc

        result = self._to_result(body)
        if result is None:
            raise self._upstream_error("malformed_response", user_id=user_id, product_id=product_id)

        log_event(
            logger,
            logging.INFO,
            "cloudpayments_checkout_outcome",
            result="created",
            userId=str(user_id),
            productId=product_id,
            status=result.status,
            paymentId=result.payment_id,
        )
        return result

    @staticmethod
    def _to_result(body: Any) -> CheckoutResult | None:
        """Project the broadapps JSON body into a ``CheckoutResult`` or None if malformed."""
        if not isinstance(body, dict):
            return None
        payment_url = body.get("payment_url")
        if not isinstance(payment_url, str) or not payment_url:
            return None
        payment_id = body.get("payment_id")
        status = body.get("status")
        expires_at = body.get("expires_at")
        return CheckoutResult(
            payment_id=str(payment_id) if payment_id is not None else "",
            payment_url=payment_url,
            status=str(status) if status is not None else "",
            expires_at=expires_at if isinstance(expires_at, str) else None,
        )

    def _upstream_error(self, reason: str, *, user_id: uuid.UUID, product_id: str) -> UpstreamError:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_checkout_outcome",
            result="error",
            reason=reason,
            userId=str(user_id),
            productId=product_id,
        )
        return UpstreamError("payment provider unavailable")
