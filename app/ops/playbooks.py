"""Pre-approved multi-step recovery playbooks for the Ops agent."""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

PLAYBOOKS: dict[str, dict[str, Any]] = {
    "send_reminder": {
        "name": "Send Reminder (existing promise)",
        "steps": [{"tool": "resend_promise_reminder"}],
    },
    "broken_promise": {
        "name": "Broken Promise Recovery",
        "description": "Generate a new payment link, email the customer, and officially close the broken promise.",
        "steps": [
            {
                "tool": "generate_payment_link",
                "description": "Generate a new payment link for the promised amount",
            },
            {
                "tool": "trigger_customer_email",
                "description": "Email the customer with the new payment link",
            },
            {
                "tool": "cancel_promise",
                "description": "Mark the promise as broken and escalate the transaction",
            },
        ],
    },
    "recovery_nudge": {
        "name": "Recovery Nudge",
        "description": "Generate a payment link for the full amount and email the customer.",
        "steps": [
            {
                "tool": "generate_payment_link",
                "description": "Generate a payment link for the full transaction amount",
            },
            {
                "tool": "trigger_customer_email",
                "description": "Email the customer the recovery link",
            },
        ],
    },
}


def get_playbook(name: str) -> dict[str, Any] | None:
    return PLAYBOOKS.get(name)


def list_playbooks() -> list[str]:
    return list(PLAYBOOKS.keys())
