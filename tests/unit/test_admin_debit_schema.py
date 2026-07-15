"""Unit: AdminDebitRequest schema validation (ADR-061, admin/09-testing.md).

Mirror of AdminGrantRequest (StrictModel, extra='forbid'): amount > 0, non-empty reason,
bounded idempotencyKey (1..128), userId must be a valid uuid.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.admin import AdminDebitRequest


def _base() -> dict[str, object]:
    return {
        "userId": str(uuid.uuid4()),
        "amount": 10,
        "idempotencyKey": "d-1",
        "reason": "manual downward correction",
    }


def test_valid_debit() -> None:
    req = AdminDebitRequest.model_validate(_base())
    assert req.amount == 10
    assert req.reason == "manual downward correction"


@pytest.mark.parametrize("amount", [0, -1, -100])
def test_nonpositive_amount_rejected(amount: int) -> None:
    payload = _base() | {"amount": amount}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_empty_reason_rejected() -> None:
    payload = _base() | {"reason": ""}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_missing_reason_rejected() -> None:
    payload = _base()
    del payload["reason"]
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_reason_over_cap_rejected() -> None:
    payload = _base() | {"reason": "x" * 513}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_extra_field_rejected() -> None:
    # extra='forbid' — a leaked secret (or any unexpected field) is a validation error.
    payload = _base() | {"adminToken": "leak"}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_empty_idempotency_key_rejected() -> None:
    payload = _base() | {"idempotencyKey": ""}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_idempotency_key_over_128_rejected() -> None:
    payload = _base() | {"idempotencyKey": "k" * 129}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)


def test_idempotency_key_at_max_128_accepted() -> None:
    # Boundary: exactly 128 chars is valid (min_length=1, max_length=128).
    payload = _base() | {"idempotencyKey": "k" * 128}
    req = AdminDebitRequest.model_validate(payload)
    assert len(req.idempotencyKey) == 128


def test_bad_uuid_rejected() -> None:
    payload = _base() | {"userId": "not-a-uuid"}
    with pytest.raises(ValidationError):
        AdminDebitRequest.model_validate(payload)
