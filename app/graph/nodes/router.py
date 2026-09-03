"""Decision router node — deterministic routing (no LLM)."""

from __future__ import annotations

import structlog

from app.graph.state import RecoveryState
from app.webhook.models import FailureCategory, RiskLevel

log = structlog.get_logger(__name__)

_ROUTING_TABLE: dict[tuple[FailureCategory, RiskLevel], str] = {
    # Bank downtime
    (FailureCategory.BANK_DOWNTIME, RiskLevel.LOW): "send_email",
    (FailureCategory.BANK_DOWNTIME, RiskLevel.MEDIUM): "send_email",
    (FailureCategory.BANK_DOWNTIME, RiskLevel.HIGH): "send_email",
    # Card limit
    (FailureCategory.CARD_LIMIT_EXCEEDED, RiskLevel.LOW): "send_email",
    (FailureCategory.CARD_LIMIT_EXCEEDED, RiskLevel.MEDIUM): "send_email",
    (FailureCategory.CARD_LIMIT_EXCEEDED, RiskLevel.HIGH): "escalate",
    # Abandoned cart
    (FailureCategory.ABANDONED_CART, RiskLevel.LOW): "send_email",
    (FailureCategory.ABANDONED_CART, RiskLevel.MEDIUM): "send_email",
    (FailureCategory.ABANDONED_CART, RiskLevel.HIGH): "escalate",
    # B2B Promise to pay
    (FailureCategory.B2B_PROMISE_TO_PAY, RiskLevel.LOW): "send_email",
    (FailureCategory.B2B_PROMISE_TO_PAY, RiskLevel.MEDIUM): "send_email",
    (FailureCategory.B2B_PROMISE_TO_PAY, RiskLevel.HIGH): "escalate",
    # Fraud / hard stop
    (FailureCategory.FRAUD_HARD_STOP, RiskLevel.LOW): "killswitch",
    (FailureCategory.FRAUD_HARD_STOP, RiskLevel.MEDIUM): "killswitch",
    (FailureCategory.FRAUD_HARD_STOP, RiskLevel.HIGH): "killswitch",
    # Subscription
    (FailureCategory.SUBSCRIPTION_CHARGE_FAILED, RiskLevel.LOW): "send_email",
    (FailureCategory.SUBSCRIPTION_CHARGE_FAILED, RiskLevel.MEDIUM): "send_email",
    (FailureCategory.SUBSCRIPTION_CHARGE_FAILED, RiskLevel.HIGH): "escalate",
    # Unknown
    (FailureCategory.UNKNOWN, RiskLevel.LOW): "escalate",
    (FailureCategory.UNKNOWN, RiskLevel.MEDIUM): "escalate",
    (FailureCategory.UNKNOWN, RiskLevel.HIGH): "escalate",
}

ROUTE_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "send_email": frozenset({"send_email"}),
    "killswitch": frozenset({"log_audit_event"}),
    "escalate": frozenset({"log_audit_event", "notify_human_reviewer"}),
}

MAX_AMOUNT_DELTA: int = 0


def router_node(state: RecoveryState) -> RecoveryState:
    category = state.get("category") or FailureCategory.UNKNOWN
    risk_level = state.get("risk_level") or RiskLevel.MEDIUM

    route = _ROUTING_TABLE.get((category, risk_level), "escalate") or "escalate"

    log.info(
        "router.selected",
        route=route,
        category=category,
        risk=risk_level,
        event_id=state.get("event_id"),
    )

    return {**state, "selected_route": route}


def get_next_node(state: RecoveryState) -> str:
    return state.get("selected_route") or "escalate"
