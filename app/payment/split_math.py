"""Split-pay math — pure, deterministic, unit-tested.

All amount arithmetic happens here, in pure Python.
The LLM never sees or touches payment amounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SplitLeg:
    """A single leg of a split payment."""

    leg_number: int  # 1-indexed
    amount_inr: Decimal
    due_days_from_now: int  # 0 = immediate, N = scheduled


def calculate_equal_split(
    original_amount_inr: Decimal,
    num_legs: int,
) -> list[SplitLeg]:
    """Split original_amount_inr into num_legs equal legs."""
    if original_amount_inr <= Decimal("0"):
        raise ValueError(f"Amount must be positive, got {original_amount_inr}")
    if not 2 <= num_legs <= 3:
        raise ValueError(f"num_legs must be 2 or 3, got {num_legs}")

    # We use paise (cents) internally for perfect integer division
    original_paise = int(original_amount_inr * 100)
    base_paise = original_paise // num_legs
    remainder = original_paise % num_legs

    legs = [
        SplitLeg(
            leg_number=i + 1,
            amount_inr=Decimal(base_paise + (remainder if i == 0 else 0)) / 100,
            due_days_from_now=i * 7,
        )
        for i in range(num_legs)
    ]

    total = sum(leg.amount_inr for leg in legs)
    assert total == original_amount_inr, "Split invariant violated"
    return legs


def calculate_custom_split(
    original_amount_inr: Decimal,
    amounts_inr: list[Decimal],
) -> list[SplitLeg]:
    """Validate and create legs from a custom split specification."""
    if not amounts_inr:
        raise ValueError("amounts_inr cannot be empty")
    if any(a <= Decimal("0") for a in amounts_inr):
        raise ValueError("All split amounts must be positive values")

    total = sum(amounts_inr)
    if total != original_amount_inr:
        raise ValueError("Custom split invariant violated")

    return [
        SplitLeg(
            leg_number=i + 1,
            amount_inr=amt,
            due_days_from_now=i * 7,
        )
        for i, amt in enumerate(amounts_inr)
    ]


@dataclass(frozen=True)
class PartialPromiseSplit:
    immediate_leg_inr: Decimal
    promised_leg_inr: Decimal


def calculate_partial_and_promise(
    original_amount_inr: Decimal,
    stated_payable_now_inr: Decimal,
) -> PartialPromiseSplit:
    """Calculate the split for a partial payment and a promise for the rest."""
    min_partial_inr = Decimal("50.00")

    if original_amount_inr <= Decimal("0"):
        raise ValueError("Original amount must be positive")

    if stated_payable_now_inr < min_partial_inr:
        raise ValueError(
            f"Stated partial amount is too small. Minimum is ₹{min_partial_inr}."
        )

    if stated_payable_now_inr >= original_amount_inr:
        raise ValueError(
            "Stated partial amount must be strictly less than the original amount."
        )

    promised_leg_inr = original_amount_inr - stated_payable_now_inr

    assert (
        stated_payable_now_inr + promised_leg_inr == original_amount_inr
    ), "Split invariant violated"

    return PartialPromiseSplit(
        immediate_leg_inr=stated_payable_now_inr,
        promised_leg_inr=promised_leg_inr,
    )
