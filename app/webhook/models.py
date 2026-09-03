"""Razorpay webhook Pydantic models — failure taxonomy."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FailureCategory(str, Enum):
    """The canonical failure categories Recoup operates on."""

    BANK_DOWNTIME = "bank_downtime"
    CARD_LIMIT_EXCEEDED = "card_limit_exceeded"
    ABANDONED_CART = "abandoned_cart"
    B2B_PROMISE_TO_PAY = "b2b_promise_to_pay"
    FRAUD_HARD_STOP = "fraud_hard_stop"
    UNKNOWN = "unknown"
    SUBSCRIPTION_CHARGE_FAILED = "subscription_charge_failed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecoveryStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RECOVERED = "recovered"
    FAILED = "failed"
    ESCALATED = "escalated"
    KILLED = "killed"  # fraud / hard stop


# ── Razorpay webhook payload models ─────────────────────────────────────────


class RazorpayPaymentEntity(BaseModel):
    """Core payment fields from Razorpay webhook payload."""

    id: str
    entity: str = "payment"
    amount: int  # in paise
    currency: str = "INR"
    status: str
    order_id: str | None = None
    international: bool = False
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    contact: str | None = None
    email: str | None = None
    notes: dict[str, Any] = Field(default_factory=dict)


class RazorpayWebhookPayload(BaseModel):
    """Top-level Razorpay webhook envelope."""

    entity: str = "event"
    account_id: str
    event: str
    contains: list[str]
    payload: dict[str, Any]
    created_at: int = 0


class ParsedFailureEvent(BaseModel):
    """Normalised failure event passed to the LangGraph pipeline."""

    event_id: str  # used as idempotency key
    payment_id: str
    order_id: str | None
    amount_inr: Decimal
    currency: str
    method: str | None
    error_code: str | None
    error_description: str | None
    error_source: str | None
    error_reason: str | None
    contact: str | None
    email: str | None
    notes: dict[str, Any]
    raw_event: str  # JSON-serialised for audit
