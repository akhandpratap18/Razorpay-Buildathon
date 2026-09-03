from __future__ import annotations

from decimal import Decimal

import pytest

from app.payment.split_math import (
    calculate_custom_split,
    calculate_equal_split,
    calculate_partial_and_promise,
)


class TestCalculateEqualSplit:
    def test_two_legs_even_amount(self) -> None:
        legs = calculate_equal_split(Decimal("100.00"), 2)
        assert len(legs) == 2
        assert sum(leg.amount_inr for leg in legs) == Decimal("100.00")
        assert legs[0].amount_inr == Decimal("50.00")
        assert legs[1].amount_inr == Decimal("50.00")

    def test_three_legs_even_amount(self) -> None:
        legs = calculate_equal_split(Decimal("300.00"), 3)
        assert len(legs) == 3
        assert sum(leg.amount_inr for leg in legs) == Decimal("300.00")

    def test_two_legs_odd_remainder_goes_to_first(self) -> None:
        legs = calculate_equal_split(Decimal("10.01"), 2)
        assert legs[0].amount_inr == Decimal("5.01")
        assert legs[1].amount_inr == Decimal("5.00")
        assert sum(leg.amount_inr for leg in legs) == Decimal("10.01")

    def test_three_legs_remainder_to_first(self) -> None:
        legs = calculate_equal_split(Decimal("100.01"), 3)
        total = sum(leg.amount_inr for leg in legs)
        assert total == Decimal("100.01")

    def test_due_days_schedule(self) -> None:
        legs = calculate_equal_split(Decimal("300.00"), 3)
        assert legs[0].due_days_from_now == 0
        assert legs[1].due_days_from_now == 7
        assert legs[2].due_days_from_now == 14

    def test_leg_numbers_are_one_indexed(self) -> None:
        legs = calculate_equal_split(Decimal("100.00"), 2)
        assert legs[0].leg_number == 1
        assert legs[1].leg_number == 2

    def test_invalid_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            calculate_equal_split(Decimal("0"), 2)

    def test_invalid_num_legs_raises(self) -> None:
        with pytest.raises(ValueError, match="2 or 3"):
            calculate_equal_split(Decimal("100.00"), 1)


class TestCalculateCustomSplit:
    def test_valid_custom_split(self) -> None:
        legs = calculate_custom_split(
            Decimal("100.00"), [Decimal("60.00"), Decimal("40.00")]
        )
        assert len(legs) == 2
        assert sum(leg.amount_inr for leg in legs) == Decimal("100.00")

    def test_invariant_violation_raises(self) -> None:
        with pytest.raises(ValueError, match="invariant violated"):
            calculate_custom_split(
                Decimal("100.00"), [Decimal("60.00"), Decimal("30.00")]
            )

    def test_negative_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            calculate_custom_split(
                Decimal("100.00"), [Decimal("110.00"), Decimal("-10.00")]
            )

    def test_empty_amounts_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            calculate_custom_split(Decimal("100.00"), [])


class TestCalculatePartialAndPromise:
    def test_valid_partial(self) -> None:
        split = calculate_partial_and_promise(Decimal("100.00"), Decimal("60.00"))
        assert split.immediate_leg_inr == Decimal("60.00")
        assert split.promised_leg_inr == Decimal("40.00")
        assert split.immediate_leg_inr + split.promised_leg_inr == Decimal("100.00")

    def test_too_small_partial_raises(self) -> None:
        with pytest.raises(ValueError, match="too small"):
            calculate_partial_and_promise(Decimal("100.00"), Decimal("49.99"))

    def test_equal_or_greater_partial_raises(self) -> None:
        with pytest.raises(ValueError, match="strictly less"):
            calculate_partial_and_promise(Decimal("100.00"), Decimal("100.00"))
        with pytest.raises(ValueError, match="strictly less"):
            calculate_partial_and_promise(Decimal("100.00"), Decimal("110.00"))

    def test_invalid_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            calculate_partial_and_promise(Decimal("0"), Decimal("10.00"))
