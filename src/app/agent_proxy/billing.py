"""Usage→credits conversion for agent runs (ADR-047 §2). Pure, side-effect-free.

    amount = ceil(input_tokens/1000 * CREDITS_PER_1K_INPUT
                  + output_tokens/1000 * CREDITS_PER_1K_OUTPUT)

Credits are integers (03-data-model.md): the fractional result is rounded UP (ceil), with a floor
of 1 credit on any non-zero usage (a run with real consumption can never cost 0). Zero usage costs
0 (no debit is performed by the caller in that case).
"""

from __future__ import annotations

import math


def usage_to_credits(
    *,
    input_tokens: int,
    output_tokens: int,
    credits_per_1k_input: float,
    credits_per_1k_output: float,
) -> int:
    """Convert token usage to an integer credit amount (ADR-047 §2).

    Negative token counts (malformed upstream payload) are clamped to 0. The raw cost is rounded
    up; any non-zero usage yields at least 1 credit. Returns 0 only when both token counts are 0.
    """
    safe_input = max(input_tokens, 0)
    safe_output = max(output_tokens, 0)
    if safe_input == 0 and safe_output == 0:
        return 0
    raw = safe_input / 1000.0 * credits_per_1k_input + safe_output / 1000.0 * credits_per_1k_output
    amount = math.ceil(raw)
    return max(amount, 1)
