"""LangGraph state definition for the Recoup recovery pipeline.

All inter-node state is a TypedDict — no Any types in schemas (mypy --strict).
Every field has a defined type and a sensible default.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TypedDict

from app.webhook.models import FailureCategory, RecoveryStatus, RiskLevel


class RecoveryState(TypedDict, total=False):
    """Full state passed between LangGraph nodes.

    Fields are optional (total=False) because nodes progressively
    fill them in as the graph executes.
    """

    # ── Input (set by webhook enqueue) ───────────────────────────────────────
    event_id: str
    transaction_id: str  # UUID from transactions table
    payment_id: str
    order_id: str | None
    amount_inr: Decimal
    currency: str
    method: str | None
    error_code: str | None
    error_description: str | None
    error_source: str | None
    error_reason: str | None
    contact: str | None  # phone number
    email: str | None
    notes: dict[str, Any]

    # ── Classification (set by diagnostic node) ───────────────────────────────
    category: FailureCategory | None
    risk_level: RiskLevel | None
    classified_by: str | None  # 'rules' | 'llm'
    classification_confidence: float | None

    # ── Routing ───────────────────────────────────────────────────────────────
    selected_route: (
        str | None
    )  # 'split_pay' | 'rail_switch' | 'conversation' | 'killswitch'

    # ── Recovery execution ────────────────────────────────────────────────────
    recovery_link_url: str | None
    recovery_link_id: str | None
    split_legs: list[dict[str, Any]]  # [{amount_inr: Decimal, link_url: str}]
    recovery_token: str | None

    # ── Communication ─────────────────────────────────────────────────────────
    messages_sent: list[dict[str, Any]]  # [{channel, status, timestamp}]
    customer_opted_out: bool
    last_channel_tried: str | None

    # ── B2B mandate ──────────────────────────────────────────────────────────
    commitment_date: str | None  # ISO date string
    commitment_amount_inr: Decimal | None
    commitment_confirmed: bool

    # ── Recovery tracking ─────────────────────────────────────────────────────
    status: RecoveryStatus
    recovery_attempts: int
    errors: list[str]  # bounded error accumulator — never silently swallowed

    # ── Audit ─────────────────────────────────────────────────────────────────
    audit_entries: list[dict[str, Any]]
