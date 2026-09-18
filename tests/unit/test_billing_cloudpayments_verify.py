"""Unit: pure CloudPayments payment reconciliation (no network)."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.billing_cloudpayments.verify import (
    CreditablePayment,
    payment_statuses,
    select_creditable_payments,
)

_PAID = frozenset({"succeeded"})


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 7, 3, 12, 0, tzinfo=datetime.UTC)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _item(
    *,
    payment_id: str = "pay-1",
    status: str = "succeeded",
    paid_at: str | None = None,
    code: str = "100_tokens_9.99",
    payment_type: str = "one_time",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "payment_id": payment_id,
        "status": status,
        "paid_at": paid_at if paid_at is not None else _iso(_now() - datetime.timedelta(minutes=5)),
        "product": {"code": code, "payment_type": payment_type},
    }
    item.update(extra)
    return item


def test_select_keeps_a_fresh_succeeded_payment() -> None:
    out = select_creditable_payments([_item()], paid_statuses=_PAID, now=_now(), freshness_hours=72)
    assert len(out) == 1
    p = out[0]
    assert isinstance(p, CreditablePayment)
    assert (p.payment_id, p.product_code, p.payment_type) == (
        "pay-1",
        "100_tokens_9.99",
        "one_time",
    )


@pytest.mark.parametrize("status", ["pending", "failed", "refunded"])
def test_select_drops_non_paid_status(status: str) -> None:
    assert (
        select_creditable_payments(
            [_item(status=status)], paid_statuses=_PAID, now=_now(), freshness_hours=72
        )
        == []
    )


def test_select_drops_stale_payment_outside_window() -> None:
    stale = _item(paid_at=_iso(_now() - datetime.timedelta(hours=100)))
    assert (
        select_creditable_payments([stale], paid_statuses=_PAID, now=_now(), freshness_hours=72)
        == []
    )


def test_payment_statuses_collects_raw() -> None:
    assert payment_statuses([_item(), _item(status="pending")]) == ["succeeded", "pending"]
