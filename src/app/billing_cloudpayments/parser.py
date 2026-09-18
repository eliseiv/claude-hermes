"""Pure defensive parsing of a CloudPayments-format payment callback (ADR-068).

The broadapps aggregator (fronting YooKassa) posts a server-to-server callback in the CloudPayments
wire format: flat top-level PascalCase fields (``AccountId`` / ``TransactionId`` / ``Status`` /
``OperationType`` / ``Amount`` / ``Currency`` / ``TestMode``) plus a ``Data`` field that is itself a
JSON *string* (snake_case business fields). These functions read best-effort values without ever
raising on missing / wrongly-typed fields, so a malformed callback yields an ``ignored`` outcome in
the service rather than a 422 (which the aggregator would retry).

PII by-design exclusion: card fields (``CardFirstSix`` / ``CardLastFour`` / ``Issuer`` /
``CardType``) and ``DateTime`` / ``Description`` are NEVER read into ``ParsedPayment`` — they are
not part of business logic and must not be logged or persisted.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

_TOKENS_NAME_RE = re.compile(r"^\d+_tokens", re.I)
_SUB_KEYWORDS = ("week", "month", "year", "day")
_SUB_SUFFIXES = ("_nottrial", "_trial")
_INTERVAL_UNITS = frozenset({"year", "month", "week", "day"})

KIND_SUBSCRIPTION = "subscription"
KIND_TOKENS = "tokens"
KIND_UNKNOWN = "unknown"

_EXPIRY_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_DEFAULT_EXPIRY_DAYS = 30


@dataclass(frozen=True)
class ParsedPayment:
    """A defensively parsed CloudPayments payment. ``user_id`` is already a validated UUID."""

    transaction_id: str
    user_id: uuid.UUID
    product_id: str
    status: str
    operation_type: str
    billing_interval_unit: str | None
    billing_interval_count: int
    billing_phase: str | None
    subscription_id: str | None
    is_trial_initial: bool | None
    is_trial_conversion: bool | None
    is_initial_payment: bool | None
    amount: int | None
    currency: str | None
    test_mode: bool | None
    kind: str = KIND_UNKNOWN


def _first_str(*candidates: Any) -> str | None:
    """First candidate that is a non-empty string, or a non-bool int coerced to str, else None."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return str(candidate)
    return None


def _first_bool(*candidates: Any) -> bool | None:
    """First candidate that is strictly a ``bool``, else None (``1``/``"true"`` do NOT count)."""
    for candidate in candidates:
        if isinstance(candidate, bool):
            return candidate
    return None


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` (e.g. the string ``"1"``) to int; ``< 1`` / unparseable -> ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 1 else default
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return default
        return parsed if parsed >= 1 else default
    return default


def _parse_data(body: dict[str, Any]) -> dict[str, Any] | None:
    """Parse the ``Data`` field: a JSON *string* -> dict, an already-dict -> as-is, else None."""
    raw = body.get("Data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def parse_transaction_id(body: dict[str, Any]) -> str | None:
    """``TransactionId`` (top-level). Absent -> None."""
    return _first_str(body.get("TransactionId"))


def parse_status(body: dict[str, Any]) -> str:
    """``Status`` normalised to lower-case; absent -> ``""``."""
    return str(body.get("Status") or "").strip().lower()


def parse_operation_type(body: dict[str, Any]) -> str:
    """``OperationType`` normalised to lower-case; absent -> ``""``."""
    return str(body.get("OperationType") or "").strip().lower()


def parse_gate(status: str, operation_type: str) -> bool:
    """Processing gate: only a completed payment is applied."""
    return status == "completed" and operation_type == "payment"


def parse_user_id(body: dict[str, Any], data: dict[str, Any]) -> uuid.UUID | None:
    """``AccountId`` (top-level) -> fallback ``Data.user_id``. UPPER -> lower UUID."""
    raw = _first_str(body.get("AccountId"), data.get("user_id"))
    if raw is None:
        return None
    try:
        return uuid.UUID(raw.lower())
    except ValueError:
        return None


def parse_product_id(data: dict[str, Any]) -> str | None:
    """``Data.product_id``. Absent/empty -> None."""
    return _first_str(data.get("product_id"))


def parse_billing_interval_unit(data: dict[str, Any]) -> str | None:
    """``Data.billing_interval_unit`` lower-cased (may be absent for a token package)."""
    raw = _first_str(data.get("billing_interval_unit"))
    return raw.lower() if raw is not None else None


def parse_billing_interval_count(data: dict[str, Any]) -> int:
    """``Data.billing_interval_count`` (arrives as a string ``"1"``); ``< 1``/invalid -> 1."""
    return _parse_int(data.get("billing_interval_count"), default=1)


def parse_billing_phase(data: dict[str, Any]) -> str | None:
    """``Data.billing_phase`` (audit-only)."""
    return _first_str(data.get("billing_phase"))


def parse_subscription_id(data: dict[str, Any], body: dict[str, Any]) -> str | None:
    """``Data.subscription_id`` -> fallback top-level ``SubscriptionId`` (audit-only)."""
    return _first_str(data.get("subscription_id"), body.get("SubscriptionId"))


def parse_trial_flags(data: dict[str, Any]) -> tuple[bool | None, bool | None, bool | None]:
    """``(is_trial_initial, is_trial_conversion, is_initial_payment)`` — strictly bool | None."""
    return (
        _first_bool(data.get("is_trial_initial")),
        _first_bool(data.get("is_trial_conversion")),
        _first_bool(data.get("is_initial_payment")),
    )


def parse_amount(body: dict[str, Any]) -> int | None:
    """Top-level ``Amount`` (audit only — NEVER used to size a grant)."""
    value = body.get("Amount")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def parse_currency(body: dict[str, Any]) -> str | None:
    """Top-level ``Currency`` (audit only)."""
    return _first_str(body.get("Currency"))


def parse_test_mode(body: dict[str, Any]) -> bool | None:
    """Top-level ``TestMode`` — strictly bool | None."""
    return _first_bool(body.get("TestMode"))


def _looks_like_subscription(product_id: str) -> bool:
    """Name heuristic: contains an interval keyword OR ends with a trial/non-trial suffix."""
    lowered = product_id.lower()
    if any(keyword in lowered for keyword in _SUB_KEYWORDS):
        return True
    return lowered.endswith(_SUB_SUFFIXES)


def classify_product(
    product_id: str, billing_interval_unit: str | None, token_product_ids: frozenset[str]
) -> str:
    """Classify a product into ``subscription`` | ``tokens`` | ``unknown``.

    Deterministic order (first match wins). ``token_product_ids`` is ``settings.token_products()``'s
    key set (passed in so this stays a pure function).
    """
    if product_id in token_product_ids:
        return KIND_TOKENS
    if billing_interval_unit is not None and billing_interval_unit in _INTERVAL_UNITS:
        return KIND_SUBSCRIPTION
    if _TOKENS_NAME_RE.match(product_id):
        return KIND_TOKENS
    if _looks_like_subscription(product_id):
        return KIND_SUBSCRIPTION
    return KIND_UNKNOWN


def infer_interval_unit_from_code(product_code: str) -> str | None:
    """Infer the subscription interval unit from a product code name."""
    lowered = product_code.lower()
    for keyword in _SUB_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def _compute_expiry(now: datetime.datetime, unit: str | None, count: int) -> datetime.datetime:
    """Subscription expiry (timedelta approximation). Unknown/None unit -> 30 days."""
    days = _EXPIRY_DAYS.get(unit or "", _DEFAULT_EXPIRY_DAYS)
    return now + datetime.timedelta(days=days * max(count, 1))


def sanitize_payload(parsed: ParsedPayment) -> dict[str, Any]:
    """Allowlist projection for persist / audit. Card data is excluded by-design."""
    return {
        "transactionId": parsed.transaction_id,
        "productId": parsed.product_id,
        "kind": parsed.kind,
        "status": parsed.status,
        "operationType": parsed.operation_type,
        "amount": parsed.amount,
        "currency": parsed.currency,
        "testMode": parsed.test_mode,
        "billingIntervalUnit": parsed.billing_interval_unit,
        "billingIntervalCount": parsed.billing_interval_count,
        "billingPhase": parsed.billing_phase,
        "subscriptionId": parsed.subscription_id,
    }
