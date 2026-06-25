"""Unit tests for agent usage→credits conversion (ADR-047 §2, agent-proxy/05-events.md).

Pure, I/O-free: exercises ``usage_to_credits`` directly (the billing rule the SSE relay applies
on ``run.completed``). Covers the contract from 05-events.md / follow_up_for_qa #11:
- ceil rounding of the fractional cost,
- floor of 1 credit on any non-zero usage,
- zero usage → 0 (the caller performs no debit then),
- negative / malformed token counts clamped to 0,
- fractional per-1k coefficients.
"""

from __future__ import annotations

import math

import pytest

from app.agent_proxy.billing import usage_to_credits

# Defaults from config.py (CREDITS_PER_1K_INPUT=1.0, CREDITS_PER_1K_OUTPUT=5.0).
_IN = 1.0
_OUT = 5.0


def _credits(inp: int, out: int, *, ci: float = _IN, co: float = _OUT) -> int:
    return usage_to_credits(
        input_tokens=inp,
        output_tokens=out,
        credits_per_1k_input=ci,
        credits_per_1k_output=co,
    )


def test_zero_usage_is_zero() -> None:
    # Both token counts 0 → 0 (caller does NOT debit).
    assert _credits(0, 0) == 0


@pytest.mark.parametrize(
    ("inp", "out"),
    [
        pytest.param(1, 0, id="one-input-token"),
        pytest.param(0, 1, id="one-output-token"),
        pytest.param(10, 0, id="tiny-input"),
        pytest.param(0, 10, id="tiny-output"),
        pytest.param(1, 1, id="one-each"),
    ],
)
def test_nonzero_usage_floors_to_one(inp: int, out: int) -> None:
    # Any non-zero usage costs at least 1 credit (a real run can never be free).
    assert _credits(inp, out) == 1


def test_ceil_rounding_up() -> None:
    # 1500 in * 1/1000 = 1.5 ; 0 out → ceil(1.5) = 2.
    assert _credits(1500, 0) == 2
    # 1000 in (=1.0) + 200 out (*5/1000 = 1.0) = 2.0 → 2 (exact, no rounding).
    assert _credits(1000, 200) == 2
    # 1001 in (1.001) + 0 → ceil = 2.
    assert _credits(1001, 0) == 2


def test_exact_integer_cost_not_inflated() -> None:
    # 2000 in * 1/1000 = 2.0 exactly → 2, not 3 (ceil of an integer is the integer).
    assert _credits(2000, 0) == 2
    # 1000 out * 5/1000 = 5.0 exactly → 5.
    assert _credits(0, 1000) == 5


def test_combined_input_output_cost() -> None:
    # 3000 in (3.0) + 2000 out (10.0) = 13.0 → 13.
    assert _credits(3000, 2000) == 13


@pytest.mark.parametrize(
    ("inp", "out"),
    [
        pytest.param(-5, -5, id="both-negative"),
        pytest.param(-100, 0, id="negative-input-only"),
        pytest.param(0, -100, id="negative-output-only"),
    ],
)
def test_negative_tokens_clamped_to_zero(inp: int, out: int) -> None:
    # Malformed upstream payload: negatives are clamped to 0 → no spurious cost.
    assert _credits(inp, out) == 0


def test_negative_input_with_positive_output() -> None:
    # input clamped to 0, output drives the cost: 1000 out * 5/1000 = 5.0 → 5.
    assert _credits(-999, 1000) == 5


@pytest.mark.parametrize(
    ("ci", "co", "inp", "out"),
    [
        pytest.param(0.5, 2.5, 1000, 1000, id="half-and-two-half"),
        pytest.param(0.1, 0.3, 5000, 5000, id="small-fractions"),
        pytest.param(1.25, 6.75, 800, 400, id="quarter-coeffs"),
    ],
)
def test_fractional_coefficients_match_ceil_formula(
    ci: float, co: float, inp: int, out: int
) -> None:
    expected = math.ceil(inp / 1000.0 * ci + out / 1000.0 * co)
    assert _credits(inp, out, ci=ci, co=co) == max(expected, 1)


def test_fractional_coefficients_floor_to_one() -> None:
    # Tiny usage with tiny coefficients still rounds up to >= 1.
    assert _credits(1, 1, ci=0.001, co=0.001) == 1
