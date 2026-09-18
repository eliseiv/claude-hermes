"""Unit: CloudPayments-format parsing + product classification (no I/O)."""

from __future__ import annotations

import uuid

import pytest

from app.billing_cloudpayments import parser
from app.billing_cloudpayments.parser import ParsedPayment

_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"
_UID = uuid.UUID(_UID_UPPER.lower())
_TOKENS = frozenset({"100_tokens_9.99", "2000_Tokens_99.99"})


def test_classify_token_map_wins_over_interval() -> None:
    assert parser.classify_product("100_tokens_9.99", "year", _TOKENS) == parser.KIND_TOKENS


@pytest.mark.parametrize("unit", ["year", "month", "week", "day"])
def test_classify_interval_unit_is_subscription(unit: str) -> None:
    assert parser.classify_product("yearly_49.99_nottrial", unit, _TOKENS) == (
        parser.KIND_SUBSCRIPTION
    )


def test_classify_token_name_pattern_no_interval() -> None:
    assert parser.classify_product("999_tokens_pack", None, _TOKENS) == parser.KIND_TOKENS


@pytest.mark.parametrize(
    "product_id",
    ["yearly_49.99_nottrial", "week_6.99_nottrial", "monthly_trial", "some_year_plan"],
)
def test_classify_subscription_name_pattern(product_id: str) -> None:
    assert parser.classify_product(product_id, None, _TOKENS) == parser.KIND_SUBSCRIPTION


@pytest.mark.parametrize("product_id", ["randomsku", "gift_card", "consumable_extra"])
def test_classify_unknown(product_id: str) -> None:
    assert parser.classify_product(product_id, None, _TOKENS) == parser.KIND_UNKNOWN


def test_parse_data_from_json_string() -> None:
    data = parser._parse_data({"Data": '{"product_id":"p1","billing_interval_unit":"year"}'})
    assert data == {"product_id": "p1", "billing_interval_unit": "year"}


def test_parse_data_accepts_already_dict() -> None:
    assert parser._parse_data({"Data": {"product_id": "p1"}}) == {"product_id": "p1"}


@pytest.mark.parametrize("bad", ["{not json", "[1,2,3]", '"a string"', "null", ""])
def test_parse_data_malformed_or_non_object_is_none(bad: str) -> None:
    assert parser._parse_data({"Data": bad}) is None


def test_parse_user_id_upper_account_id_normalised_lower() -> None:
    assert parser.parse_user_id({"AccountId": _UID_UPPER}, {}) == _UID


def test_parse_user_id_fallbacks_to_data_user_id() -> None:
    assert parser.parse_user_id({}, {"user_id": _UID_UPPER}) == _UID


def test_parse_user_id_both_absent_is_none() -> None:
    assert parser.parse_user_id({}, {}) is None


@pytest.mark.parametrize(
    "status, op, expected",
    [
        ("Completed", "Payment", True),
        ("completed", "payment", True),
        ("Pending", "Payment", False),
        ("Completed", "Refund", False),
    ],
)
def test_gate_case_insensitive(status: str, op: str, expected: bool) -> None:
    s = parser.parse_status({"Status": status})
    o = parser.parse_operation_type({"OperationType": op})
    assert parser.parse_gate(s, o) is expected


def test_infer_interval_unit_from_code() -> None:
    assert parser.infer_interval_unit_from_code("week_6.99_nottrial") == "week"
    assert parser.infer_interval_unit_from_code("yearly_49.99") == "year"
    assert parser.infer_interval_unit_from_code("pack_100") is None


def _parsed(**overrides: object) -> ParsedPayment:
    base: dict[str, object] = {
        "transaction_id": "t-1",
        "user_id": _UID,
        "product_id": "yearly_49.99_nottrial",
        "status": "completed",
        "operation_type": "payment",
        "billing_interval_unit": "year",
        "billing_interval_count": 1,
        "billing_phase": "regular",
        "subscription_id": "sub-1",
        "is_trial_initial": None,
        "is_trial_conversion": None,
        "is_initial_payment": None,
        "amount": 3990,
        "currency": "RUB",
        "test_mode": False,
        "kind": parser.KIND_SUBSCRIPTION,
    }
    base.update(overrides)
    return ParsedPayment(**base)  # type: ignore[arg-type]


def test_sanitize_payload_allowlist_only_no_card_fields() -> None:
    sanitized = parser.sanitize_payload(_parsed())
    blob = str(sanitized)
    for forbidden in ("CardFirstSix", "CardLastFour", "Issuer", "CardType"):
        assert forbidden not in blob
