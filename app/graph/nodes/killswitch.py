"""Terminal killswitch node — fraud / hard stop.

This node is the ONLY destination for FRAUD_HARD_STOP category.
It does exactly three things:
1. Logs an audit event
2. Updates state to 'killed'
3. Returns — no customer contact, no payment action, nothing else

Allowed tools: log_audit_event ONLY.
Any attempt to use any other tool will raise AllowlistViolation.
"""

from __future__ import annotations

import structlog

from app.graph.state import RecoveryState
from app.webhook.models import RecoveryStatus

log = structlog.get_logger(__name__)


def killswitch_node(state: RecoveryState) -> RecoveryState:
    """Hard stop — log and terminate. No customer outreach. No payment actions."""

    log.warning(
        "killswitch.triggered",
        event_id=state.get("event_id"),
        category=state.get("category"),
        risk_level=state.get("risk_level"),
        payment_id=state.get("payment_id"),
        # Deliberately NOT logging contact/email — PII, and irrelevant for hard stop
    )

    # The audit log entry is written by the worker executor after this node returns.
    # This node itself has no DB access — single responsibility.

    return {
        **state,
        "status": RecoveryStatus.KILLED,
        "selected_route": "killswitch",
        "errors": list(state.get("errors", [])),
    }
