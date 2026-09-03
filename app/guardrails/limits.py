"""Recovery limits and guardrails.

Structural limits that are enforced as constants — no graph edge
exists past these limits, so they cannot be bypassed.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import settings

# ── Hard structural limits ─────────────────────────────────────────────────────

# Maximum number of recovery attempts before declaring failure/escalation
MAX_RECOVERY_ATTEMPTS: int = settings.max_recovery_attempts  # default: 2

# Maximum number of split payment legs
MAX_SPLIT_LEGS: int = settings.max_split_legs  # default: 3

# Payment link TTL — after this, a new link must be generated
RECOVERY_LINK_TTL_HOURS: int = settings.recovery_link_ttl_hours  # default: 24

# Minimum split leg amount (Razorpay minimum is ₹1 = 100 INR)
MIN_LEG_AMOUNT_PAISE: Decimal = Decimal("1.00")

# Maximum outbound messages per transaction (prevents harassment)
MAX_OUTBOUND_MESSAGES: int = 3

# Opt-out keywords (English + Hindi minimum — master plan requirement)
OPT_OUT_KEYWORDS: frozenset[str] = frozenset(
    {
        # English
        "stop",
        "unsubscribe",
        "cancel",
        "opt out",
        "opt-out",
        "optout",
        "no more",
        "remove me",
        "dont contact",
        "do not contact",
        # Hindi (transliterated)
        "band karo",
        "mat bhejo",
        "nahi chahiye",
        "rokein",
        "mat karo",
        "rokna",
        "band",
        "nahi",
    }
)


def is_opt_out_message(message: str) -> bool:
    """Return True if the message contains an opt-out keyword.

    Case-insensitive. Strips punctuation.
    """
    normalized = message.lower().strip().rstrip("!.,;?")
    return any(kw in normalized for kw in OPT_OUT_KEYWORDS)


def can_attempt_recovery(current_attempts: int) -> bool:
    """Return True if another recovery attempt is allowed."""
    return current_attempts < MAX_RECOVERY_ATTEMPTS
