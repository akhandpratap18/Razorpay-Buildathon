"""B2B date parsing unit tests — must handle all edge cases correctly."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.graph.nodes.b2b_mandate import DATE_CONFIDENCE_THRESHOLD, parse_commitment_date


class TestCommitmentDateParsing:
    def test_iso_date_high_confidence(self) -> None:
        parsed, confidence = parse_commitment_date("2025-12-31")
        assert parsed == date(2025, 12, 31)
        assert confidence == 1.0

    def test_today_keyword(self) -> None:
        parsed, confidence = parse_commitment_date("today")
        assert parsed == date.today()
        assert confidence >= 0.9

    def test_tomorrow_keyword(self) -> None:
        parsed, confidence = parse_commitment_date("tomorrow")
        assert parsed == date.today() + timedelta(days=1)
        assert confidence >= 0.9

    def test_in_n_days(self) -> None:
        parsed, confidence = parse_commitment_date("in 5 days")
        assert parsed == date.today() + timedelta(days=5)
        assert confidence >= 0.85

    def test_next_friday_above_threshold(self) -> None:
        parsed, confidence = parse_commitment_date("next friday")
        assert parsed is not None
        assert confidence >= DATE_CONFIDENCE_THRESHOLD

    def test_end_of_month_below_threshold(self) -> None:
        """'end of month' is ambiguous — confidence < threshold → human review."""
        _, confidence = parse_commitment_date("end of month")
        assert confidence < DATE_CONFIDENCE_THRESHOLD

    def test_next_week_below_threshold(self) -> None:
        """'next week' is ambiguous — must route to human review."""
        _, confidence = parse_commitment_date("next week")
        assert confidence < DATE_CONFIDENCE_THRESHOLD

    def test_gibberish_returns_zero_confidence(self) -> None:
        parsed, confidence = parse_commitment_date("whenever I feel like it")
        assert confidence == 0.0

    def test_empty_string_returns_zero(self) -> None:
        parsed, confidence = parse_commitment_date("")
        assert confidence == 0.0

    @pytest.mark.parametrize(
        "ambiguous_input",
        [
            "end of month",
            "next week",
            "soon",
            "when I have money",
            "after payday",
        ],
    )
    def test_ambiguous_dates_all_below_threshold(self, ambiguous_input: str) -> None:
        """All ambiguous phrases must be below the confidence threshold."""
        _, confidence = parse_commitment_date(ambiguous_input)
        assert confidence < DATE_CONFIDENCE_THRESHOLD, (
            f"'{ambiguous_input}' got confidence {confidence:.2f} "
            f"which is >= threshold {DATE_CONFIDENCE_THRESHOLD}"
        )
