"""Diagnostic agent node — classification entry point.

Implements the two-stage pipeline:
1. Deterministic rule table (classify_by_rules) — handles ≥90% of cases
2. Groq LLM fallback (classify_with_llm) — only for unmapped/ambiguous cases

Fraud cases NEVER reach the LLM — they are caught by rules first.
"""

from __future__ import annotations

import structlog

from app.classification.llm import classify_with_llm
from app.classification.rules import classify_by_rules
from app.graph.state import RecoveryState
from app.webhook.models import FailureCategory, RecoveryStatus

log = structlog.get_logger(__name__)


def diagnostic_agent(state: RecoveryState) -> RecoveryState:
    """Classify the failure event. Updates state with category, risk, and confidence."""

    error_code = state.get("error_code")
    error_description = state.get("error_description")
    error_reason = state.get("error_reason")
    method = state.get("method")

    log.info(
        "diagnostic.start",
        event_id=state.get("event_id"),
        error_code=error_code,
    )

    # Stage 1: Deterministic rules (fast path)
    result = classify_by_rules(error_code, error_description, error_reason)

    if result is None:
        # Stage 2: LLM fallback (only for truly unmapped cases)
        log.info("diagnostic.rules_missed_falling_to_llm", error_code=error_code)
        try:
            result = classify_with_llm(
                error_code, error_description, error_reason, method
            )
        except Exception as exc:
            log.error("diagnostic.llm_failed", error=str(exc))
            # Fail safe: unknown category routes to human escalation
            from app.classification.rules import ClassificationResult
            from app.webhook.models import RiskLevel

            result = ClassificationResult(
                category=FailureCategory.UNKNOWN,
                risk_level=RiskLevel.MEDIUM,
                classified_by="fallback",
                confidence=0.0,
            )

    log.info(
        "diagnostic.classified",
        category=result.category,
        risk=result.risk_level,
        by=result.classified_by,
        confidence=result.confidence,
    )

    return {
        **state,
        "category": result.category,
        "risk_level": result.risk_level,
        "classified_by": result.classified_by,
        "classification_confidence": result.confidence,
        "status": RecoveryStatus.PROCESSING,
    }
