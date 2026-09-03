from decimal import Decimal

from app.agents.shared_persona import CORE_IDENTITY
from app.ops.chat_agent import SYSTEM_PROMPT
from app.recovery.customer_agent import get_system_prompt


def test_shared_persona_in_both_agents():
    # 1. Ops Agent
    assert CORE_IDENTITY in SYSTEM_PROMPT

    # 2. Customer Agent
    customer_prompt = get_system_prompt(Decimal("100"), "one_time", "")
    assert CORE_IDENTITY in customer_prompt

    # Ensure they diverge where required
    assert "AUDIENCE: An internal Razorpay ops manager." in SYSTEM_PROMPT
    assert "AUDIENCE: A customer whose payment failed." in customer_prompt
