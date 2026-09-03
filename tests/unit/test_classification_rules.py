"""Unit tests for the deterministic classification rule table."""

from __future__ import annotations

import pytest

from app.classification.rules import classify_by_rules
from app.webhook.models import FailureCategory, RiskLevel


class TestClassifyByRules:
    """Rule table must classify known codes deterministically."""

    def test_bank_downtime_code(self) -> None:
        result = classify_by_rules(
            "BAD_REQUEST_ERROR",
            "PAYMENT_FAILED",
            "bank_not_responding",
        )
        assert result is not None
        assert result.category == FailureCategory.BANK_DOWNTIME
        assert result.classified_by == "rules"

    def test_card_limit_code(self) -> None:
        result = classify_by_rules(
            "BAD_REQUEST_ERROR",
            "PAYMENT_FAILED",
            "insufficient_funds",
        )
        assert result is not None
        assert result.category == FailureCategory.CARD_LIMIT_EXCEEDED

    def test_abandoned_cart_code(self) -> None:
        result = classify_by_rules(
            "BAD_REQUEST_ERROR",
            "PAYMENT_FAILED",
            "payment_cancelled",
        )
        assert result is not None
        assert result.category == FailureCategory.ABANDONED_CART

    def test_fraud_code_never_unknown(self) -> None:
        """Fraud must ALWAYS be caught by rules — never falls to LLM."""
        result = classify_by_rules(
            "BAD_REQUEST_ERROR",
            "PAYMENT_FAILED",
            "fraud_detected",
        )
        assert result is not None
        assert result.category == FailureCategory.FRAUD_HARD_STOP
        assert result.risk_level == RiskLevel.HIGH

    def test_fraud_via_description_keyword(self) -> None:
        result = classify_by_rules(None, "suspected fraud on card", None)
        assert result is not None
        assert result.category == FailureCategory.FRAUD_HARD_STOP

    def test_unknown_code_returns_none(self) -> None:
        """Unmapped codes must return None (fall through to LLM)."""
        result = classify_by_rules(
            "TOTALLY_UNKNOWN_CODE_XYZ",
            "something random",
            None,
        )
        assert result is None

    def test_none_inputs_returns_none(self) -> None:
        result = classify_by_rules(None, None, None)
        assert result is None

    def test_determinism(self) -> None:
        """Same input must always produce the same output."""
        inputs = ("BAD_REQUEST_ERROR", "PAYMENT_FAILED", "insufficient_funds")
        r1 = classify_by_rules(*inputs)
        r2 = classify_by_rules(*inputs)
        r3 = classify_by_rules(*inputs)
        assert r1 == r2 == r3

    @pytest.mark.parametrize(
        "error_reason,expected_cat",
        [
            ("bank_not_responding", FailureCategory.BANK_DOWNTIME),
            ("insufficient_funds", FailureCategory.CARD_LIMIT_EXCEEDED),
            ("payment_cancelled", FailureCategory.ABANDONED_CART),
            ("fraud_detected", FailureCategory.FRAUD_HARD_STOP),
        ],
    )
    def test_all_four_categories_covered(
        self, error_reason: str, expected_cat: FailureCategory
    ) -> None:
        result = classify_by_rules("BAD_REQUEST_ERROR", "PAYMENT_FAILED", error_reason)
        assert result is not None
        assert result.category == expected_cat
