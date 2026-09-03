"""B2B Promise-to-Pay conversion node.

For B2B customers who need to schedule payment at a future date.
Extracts commitment_date and amount via confidence-thresholded parsing.
Requires explicit double-confirmation before any mandate/link action.
Ambiguous dates route to human review — never silently misinterpreted.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import structlog

from app.graph.state import RecoveryState
from app.webhook.models import RecoveryStatus

log = structlog.get_logger(__name__)

# Confidence threshold — below this, route to human review
DATE_CONFIDENCE_THRESHOLD = 0.80


def parse_commitment_date(raw_date_text: str) -> tuple[date | None, float]:
    """Parse a commitment date from natural-language text.

    Returns (parsed_date, confidence). confidence < THRESHOLD → human review.
    This is deterministic rule-based parsing — no LLM involved.
    """
    text = raw_date_text.lower().strip()
    today = date.today()

    # Exact ISO date: "2024-12-31"
    iso_match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if iso_match:
        try:
            parsed = date.fromisoformat(iso_match.group(1))
            return parsed, 1.0
        except ValueError:
            pass

    # Common patterns (medium confidence)
    if "today" in text:
        return today, 0.95
    if "tomorrow" in text:
        return today + timedelta(days=1), 0.95

    # "next friday", "next monday" etc.
    weekdays = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    for i, day_name in enumerate(weekdays):
        if f"next {day_name}" in text:
            days_ahead = (i - today.weekday() + 7) % 7 or 7
            return today + timedelta(days=days_ahead), 0.85

    # "end of month"
    if "end of month" in text or "month end" in text:
        next_month = today.replace(day=28) + timedelta(days=4)
        end_of_month = next_month - timedelta(days=next_month.day)
        return end_of_month, 0.75  # Below threshold → human review

    # "next week"
    if "next week" in text:
        return today + timedelta(days=7), 0.70  # Below threshold → human review

    # "in N days"
    days_match = re.search(r"in (\d+) days?", text)
    if days_match:
        n = int(days_match.group(1))
        return today + timedelta(days=n), 0.90

    return None, 0.0


def b2b_mandate_node(state: RecoveryState) -> RecoveryState:
    """Handle B2B promise-to-pay flow.

    IMPORTANT: No commitment action is taken without explicit double-confirmation.
    This node only processes confirmed commitments — the actual payment link
    creation happens in a subsequent node after user confirmation.
    """
    errors: list[str] = list(state.get("errors", []))
    notes = state.get("notes", {})

    # Extract raw date text from notes (set by the conversational inbound handler)
    raw_date = notes.get("commitment_date_raw", "")
    raw_amount = notes.get("commitment_amount_inr")
    confirmed = state.get("commitment_confirmed", False)

    # Requirement: no action without explicit confirmation
    if not confirmed:
        log.info("b2b_mandate.awaiting_confirmation")
        return {
            **state,
            "status": RecoveryStatus.PROCESSING,
            "errors": errors,
        }

    if not raw_date:
        log.warning("b2b_mandate.no_date_provided")
        errors.append("no_commitment_date")
        return {**state, "status": RecoveryStatus.ESCALATED, "errors": errors}

    parsed_date, confidence = parse_commitment_date(raw_date)

    log.info(
        "b2b_mandate.date_parsed",
        raw=raw_date,
        parsed=str(parsed_date),
        confidence=confidence,
    )

    if confidence < DATE_CONFIDENCE_THRESHOLD or parsed_date is None:
        # Route to human review — never guess
        log.warning(
            "b2b_mandate.low_confidence_routing_to_human",
            confidence=confidence,
            raw=raw_date,
        )
        errors.append(f"low_date_confidence:{confidence:.2f}:{raw_date}")
        return {**state, "status": RecoveryStatus.ESCALATED, "errors": errors}

    # Past date check
    if parsed_date < date.today():
        log.warning("b2b_mandate.past_date", date=str(parsed_date))
        errors.append(f"commitment_date_in_past:{parsed_date}")
        return {**state, "status": RecoveryStatus.ESCALATED, "errors": errors}

    raw_val = raw_amount or state.get("amount_inr") or 0
    from decimal import Decimal

    commitment_amount = Decimal(raw_val)

    log.info(
        "b2b_mandate.commitment_recorded",
        date=str(parsed_date),
        amount_inr=commitment_amount,
    )

    return {
        **state,
        "commitment_date": parsed_date.isoformat(),
        "commitment_amount_inr": commitment_amount,
        "commitment_confirmed": True,
        "status": RecoveryStatus.PROCESSING,
        "errors": errors,
    }
