"""Unit tests for guardrails — allowlist, limits, opt-out."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.guardrails.allowlist import (
    AllowlistViolation,
    enforce_amount_ceiling,
    make_guarded_executor,
)
from app.guardrails.limits import (
    MAX_RECOVERY_ATTEMPTS,
    can_attempt_recovery,
    is_opt_out_message,
)


class TestAllowlist:
    """Tool-call allowlist enforcement."""

    def test_allowed_tool_executes(self) -> None:
        allowed = frozenset({"my_tool"})
        execute = make_guarded_executor("test_node", allowed)
        called_with: list[str] = []

        def my_tool(**kwargs: object) -> str:
            called_with.append("called")
            return "ok"

        result = execute("my_tool", my_tool)
        assert result == "ok"
        assert called_with == ["called"]

    def test_disallowed_tool_raises(self) -> None:
        allowed = frozenset({"safe_tool"})
        execute = make_guarded_executor("test_node", allowed)

        def dangerous_tool(**kwargs: object) -> str:
            return "SHOULD_NOT_RUN"

        with pytest.raises(AllowlistViolation, match="dangerous_tool"):
            execute("dangerous_tool", dangerous_tool)

    def test_killswitch_only_allows_log(self) -> None:
        """Killswitch node can ONLY call log_audit_event."""
        from app.graph.nodes.router import ROUTE_ALLOWED_TOOLS

        ks_tools = ROUTE_ALLOWED_TOOLS["killswitch"]
        assert "log_audit_event" in ks_tools
        assert "create_payment_link" not in ks_tools
        assert "send_whatsapp" not in ks_tools

    @pytest.mark.parametrize(
        "injection_attempt",
        [
            "create_payment_link",
            "send_whatsapp",
            "issue_refund",
            "apply_discount",
            "set_amount_to_zero",
        ],
    )
    def test_injection_attempts_blocked_on_killswitch(
        self, injection_attempt: str
    ) -> None:
        """Adversarial: 5+ injection attempts all blocked."""
        from app.graph.nodes.router import ROUTE_ALLOWED_TOOLS

        execute = make_guarded_executor("killswitch", ROUTE_ALLOWED_TOOLS["killswitch"])
        with pytest.raises(AllowlistViolation):
            execute(injection_attempt, lambda: None)


class TestAmountCeiling:
    def test_same_amount_passes(self) -> None:
        enforce_amount_ceiling(
            Decimal("1000.00"), Decimal("1000.00")
        )  # Should not raise

    def test_lower_amount_passes(self) -> None:
        enforce_amount_ceiling(Decimal("1000.00"), Decimal("500.00"))

    def test_higher_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            enforce_amount_ceiling(Decimal("1000.00"), Decimal("1001.00"))

    def test_zero_original_raises(self) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            enforce_amount_ceiling(Decimal("0.00"), Decimal("1.00"))


class TestOptOut:
    def test_english_stop_keyword(self) -> None:
        assert is_opt_out_message("STOP") is True
        assert is_opt_out_message("stop") is True
        assert is_opt_out_message("Stop!") is True

    def test_hindi_keyword(self) -> None:
        assert is_opt_out_message("band karo") is True
        assert is_opt_out_message("nahi chahiye") is True

    def test_normal_message_not_opt_out(self) -> None:
        assert is_opt_out_message("Yes I want to pay") is False
        assert is_opt_out_message("Please help me") is False

    def test_can_attempt_recovery_within_limit(self) -> None:
        assert can_attempt_recovery(0) is True
        assert can_attempt_recovery(MAX_RECOVERY_ATTEMPTS - 1) is True

    def test_cannot_attempt_at_limit(self) -> None:
        assert can_attempt_recovery(MAX_RECOVERY_ATTEMPTS) is False
        assert can_attempt_recovery(MAX_RECOVERY_ATTEMPTS + 5) is False
