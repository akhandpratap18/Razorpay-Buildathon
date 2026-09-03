"""Failure taxonomy classification — deterministic rule table.

Design principles (from the master plan):
- Money math and routing is NEVER AI math.
- ≥90% of failures must be classified by rules alone, without LLM.
- Fraud/blacklist cases NEVER reach the LLM node.
- Classification is a pure function: same input → same output, always.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.webhook.models import FailureCategory, RiskLevel


@dataclass(frozen=True)
class ClassificationResult:
    category: FailureCategory
    risk_level: RiskLevel
    classified_by: str  # 'rules' | 'llm'
    confidence: float  # 0.0 – 1.0


# ── Deterministic rule table ───────────────────────────────────────────────────
# Razorpay error codes → (category, risk_level)
# Source: https://razorpay.com/docs/payments/payments/error-codes/

_RULE_TABLE: dict[str, tuple[FailureCategory, RiskLevel]] = {
    "SUBSCRIPTION_HALTED": (
        FailureCategory.SUBSCRIPTION_CHARGE_FAILED,
        RiskLevel.LOW,
    ),
    # ── Bank Downtime / Unavailable Rails (Rail Switch) ───────────────────────
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.bank_not_responding": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.gateway_not_responding": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "GATEWAY_ERROR.PAYMENT_FAILED.gateway_technical_error": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.MEDIUM,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.gateway_technical_error": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.MEDIUM,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.bank_technical_error": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "GATEWAY_ERROR.PAYMENT_FAILED.payment_failed": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.MEDIUM,
    ),
    "GATEWAY_ERROR": (FailureCategory.BANK_DOWNTIME, RiskLevel.MEDIUM),
    "SERVER_ERROR": (FailureCategory.BANK_DOWNTIME, RiskLevel.MEDIUM),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.international_card_blocked": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.card_disabled_for_online_payments": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.card_not_enrolled": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.debit_instrument_inactive": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.debit_instrument_blocked": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.MEDIUM,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.card_expired": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    "GATEWAY_ERROR.PAYMENT_FAILED.authentication_failed": (
        FailureCategory.BANK_DOWNTIME,
        RiskLevel.LOW,
    ),
    # ── Card Limit Exceeded (Split Pay) ───────────────────────────────────────
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.insufficient_funds": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.insufficient_fund": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.daily_limit_exceeded": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.weekly_limit_exceeded": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.monthly_limit_exceeded": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.credit_limit_reached": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.card_limit_exhausted": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.upi_daily_exceeded": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.transaction_limit_exceeded": (
        FailureCategory.CARD_LIMIT_EXCEEDED,
        RiskLevel.LOW,
    ),
    # ── Abandoned Cart (Conversational) ───────────────────────────────────────
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.payment_cancelled": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.user_cancelled": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.payment_timeout": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.payment_timed_out": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.otp_timeout": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.customer_cancelled": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.payment_failed": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.incorrect_cvv": (
        FailureCategory.ABANDONED_CART,
        RiskLevel.LOW,
    ),
    # ── Fraud / Hard Stop ─────────────────────────────────────────────────────
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.card_declined": (
        FailureCategory.FRAUD_HARD_STOP,
        RiskLevel.HIGH,
    ),
    "BAD_REQUEST_ERROR.PAYMENT_FAILED.card_number_invalid": (
        FailureCategory.FRAUD_HARD_STOP,
        RiskLevel.HIGH,
    ),
}

# Additional keyword-based rules for error descriptions
_DESCRIPTION_FRAUD_KEYWORDS = frozenset(
    {"fraud", "stolen", "blocked", "blacklist", "risk", "suspicious", "unauthorized"}
)

_DESCRIPTION_BANK_KEYWORDS = frozenset(
    {"bank", "gateway", "timeout", "not responding", "technical error", "server error"}
)

_DESCRIPTION_LIMIT_KEYWORDS = frozenset({"insufficient", "limit", "exceeded", "funds"})

_DESCRIPTION_CART_KEYWORDS = frozenset({"cancel", "timeout", "abandoned", "otp"})


_DESCRIPTION_B2B_KEYWORDS = frozenset({"invoice", "b2b", "receivable"})


def classify_by_rules(
    error_code: str | None,
    error_description: str | None,
    error_reason: str | None,
) -> ClassificationResult | None:
    """Attempt deterministic classification. Returns None if no rule matches.

    This is a pure function. Side-effect free. Fully unit-testable.
    """
    # Build composite lookup key from the available error fields
    lookup_candidates: list[str] = []

    if error_code and error_reason:
        # Razorpay docs typically map: ERROR_CODE.ERROR_SOURCE.ERROR_REASON
        # Our table uses PAYMENT_FAILED as the standard source for these webhooks.
        lookup_candidates.append(f"{error_code}.PAYMENT_FAILED.{error_reason}")

    if error_code and error_description and error_reason:
        lookup_candidates.append(f"{error_code}.{error_description}.{error_reason}")

    if error_code and error_description:
        lookup_candidates.append(f"{error_code}.{error_description}")

    if error_code:
        lookup_candidates.append(error_code)

    for key in lookup_candidates:
        if key in _RULE_TABLE:
            category, risk = _RULE_TABLE[key]
            return ClassificationResult(
                category=category,
                risk_level=risk,
                classified_by="rules",
                confidence=1.0,
            )

    # Keyword fallback on error_description and error_reason
    search_text = f"{error_description or ''} {error_reason or ''}".lower()
    if search_text.strip():
        # Check fraud keywords first — highest priority
        if any(kw in search_text for kw in _DESCRIPTION_FRAUD_KEYWORDS):
            return ClassificationResult(
                category=FailureCategory.FRAUD_HARD_STOP,
                risk_level=RiskLevel.HIGH,
                classified_by="rules",
                confidence=0.85,
            )

        if any(kw in search_text for kw in _DESCRIPTION_LIMIT_KEYWORDS):
            return ClassificationResult(
                category=FailureCategory.CARD_LIMIT_EXCEEDED,
                risk_level=RiskLevel.LOW,
                classified_by="rules",
                confidence=0.85,
            )

        if any(kw in search_text for kw in _DESCRIPTION_BANK_KEYWORDS):
            return ClassificationResult(
                category=FailureCategory.BANK_DOWNTIME,
                risk_level=RiskLevel.MEDIUM,
                classified_by="rules",
                confidence=0.75,
            )

        if any(kw in search_text for kw in _DESCRIPTION_CART_KEYWORDS):
            return ClassificationResult(
                category=FailureCategory.ABANDONED_CART,
                risk_level=RiskLevel.LOW,
                classified_by="rules",
                confidence=0.75,
            )

        if any(kw in search_text for kw in _DESCRIPTION_B2B_KEYWORDS):
            return ClassificationResult(
                category=FailureCategory.B2B_PROMISE_TO_PAY,
                risk_level=RiskLevel.LOW,
                classified_by="rules",
                confidence=0.85,
            )

    # No rule matched → fall through to LLM
    return None
