import secrets

import structlog

from app.communication.fallback_chain import send_recovery_email
from app.graph.state import RecoveryState
from app.webhook.models import RecoveryStatus

log = structlog.get_logger(__name__)


def send_email_node(state: RecoveryState) -> RecoveryState:
    email = state.get("email")
    transaction_id = state.get("transaction_id", "")
    errors = list(state.get("errors", []))
    attempt = state.get("recovery_attempts", 0) + 1

    if not email:
        errors.append("no_email_provided")
        return {**state, "status": RecoveryStatus.FAILED, "errors": errors}

    recovery_token = secrets.token_urlsafe(32)

    try:
        send_recovery_email(transaction_id, email, recovery_token)
    except Exception as exc:
        log.error("send_email.delivery_failed", error=str(exc))
        errors.append(f"delivery: {exc}")
        return {**state, "status": RecoveryStatus.FAILED, "errors": errors}

    return {
        **state,
        "recovery_token": recovery_token,
        "recovery_attempts": attempt,
        "status": RecoveryStatus.PROCESSING,
        "errors": errors,
    }
