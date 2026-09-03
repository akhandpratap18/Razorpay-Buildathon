"""Post-recovery verification node.

After a recovery action, this node determines the final outcome:
- RECOVERED: The payment was completed (order.paid confirmed)
- RETRY: Attempt another recovery strategy if within limits
- ESCALATED: Max attempts reached or unresolvable failure
"""

from __future__ import annotations

import structlog

from app.graph.state import RecoveryState
from app.guardrails.limits import can_attempt_recovery
from app.payment.razorpay_client import razorpay_client
from app.webhook.models import RecoveryStatus

log = structlog.get_logger(__name__)


def verification_node(state: RecoveryState) -> RecoveryState:
    """Check whether the recovery action resulted in a completed payment."""

    payment_id = state.get("payment_id", "")
    attempts = state.get("recovery_attempts", 0)
    status = state.get("status", RecoveryStatus.PENDING)
    errors: list[str] = list(state.get("errors", []))

    # Hard stop or escalation — don't verify, just pass through
    if status in (RecoveryStatus.KILLED, RecoveryStatus.ESCALATED):
        return state

    log.info("verification.start", payment_id=payment_id, attempts=attempts)

    # Try to fetch the current payment status from Razorpay
    try:
        payment = razorpay_client.fetch_payment(payment_id)
        payment_status = payment.get("status", "")

        if payment_status == "captured":
            log.info("verification.payment_recovered", payment_id=payment_id)
            return {**state, "status": RecoveryStatus.RECOVERED}

        log.info("verification.not_yet_paid", razorpay_status=payment_status)
    except Exception as exc:
        log.warning("verification.fetch_failed", error=str(exc))
        errors.append(f"verification_fetch: {exc}")

    # Not yet paid — decide retry or escalate
    if can_attempt_recovery(attempts):
        log.info("verification.scheduling_retry", attempts=attempts)
        return {
            **state,
            "status": RecoveryStatus.PENDING,  # re-enqueued by worker
            "errors": errors,
        }

    log.warning("verification.max_attempts_reached", attempts=attempts)
    return {
        **state,
        "status": RecoveryStatus.ESCALATED,
        "errors": [*errors, f"max_attempts:{attempts}"],
    }


def get_final_route(state: RecoveryState) -> str:
    """LangGraph conditional edge: determine terminal state routing."""
    status = state.get("status")
    if status == RecoveryStatus.RECOVERED:
        return "success"
    if status in (
        RecoveryStatus.KILLED,
        RecoveryStatus.ESCALATED,
        RecoveryStatus.FAILED,
    ):
        return "terminal"
    return "retry"
