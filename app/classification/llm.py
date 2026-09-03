"""Groq LLM fallback classifier.

Only invoked when the deterministic rule table returns None.
Fraud cases NEVER reach this function — they are caught in rules.py first.

Design:
- Parses JSON from the LLM response without strict response_format mode
  (for maximum model compatibility across Groq's changing catalogue).
- Primary model: openai/gpt-oss-20b
- Temperature = 0 for deterministic output
- Schema validator rejects any out-of-enum response before it propagates
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from groq import Groq, RateLimitError

from app.classification.rules import ClassificationResult
from app.config import settings
from app.webhook.models import FailureCategory, RiskLevel

log = structlog.get_logger(__name__)

_client = Groq(api_key=settings.groq_api_key)

# Valid enum values for validation
_VALID_CATEGORIES = {c.value for c in FailureCategory}
_VALID_RISKS = {r.value for r in RiskLevel}

_SYSTEM_PROMPT = """You are a payment failure classifier for an Indian e-commerce platform.
Classify the payment failure into EXACTLY ONE category.

Categories:
- bank_downtime: Bank/gateway/infrastructure is unavailable or erroring
- card_limit_exceeded: Insufficient funds, daily/weekly/monthly limit reached
- abandoned_cart: Customer cancelled, timed out, or did not complete payment
- fraud_hard_stop: Suspected fraud, stolen card, risk threshold exceeded
- b2b_promise_to_pay: Invoice pending internal approval and manual b2b transfer
- subscription_charge_failed: Recurring payment failed due to expired card or mandate failure
- unknown: Cannot determine cause

Respond ONLY with this exact JSON (no other text, no markdown):
{"category": "<one of the seven categories above>", "risk_level": "<low|medium|high>", "confidence": <0.0-1.0>}"""


def _extract_json(text: str) -> Any:
    """Extract the first JSON object from a string, even if surrounded by text."""
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Try to find JSON object in the text via regex
    match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def _classify_with_model(
    model: str,
    error_code: str | None,
    error_description: str | None,
    error_reason: str | None,
    method: str | None,
) -> ClassificationResult:
    """Call Groq with the specified model and parse the response."""
    user_content = (
        f"error_code: {error_code or 'none'}\n"
        f"error_description: {error_description or 'none'}\n"
        f"error_reason: {error_reason or 'none'}\n"
        f"payment_method: {method or 'none'}"
    )

    response = _client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        max_tokens=100,  # The JSON response is tiny — 100 tokens is more than enough
    )

    raw = response.choices[0].message.content or "{}"
    parsed = _extract_json(raw)

    # Validate and sanitise enum values before they propagate
    category_val = parsed.get("category", "unknown")
    risk_val = parsed.get("risk_level", "medium")

    if category_val not in _VALID_CATEGORIES:
        log.warning("llm.invalid_category", raw=category_val)
        category_val = "unknown"

    if risk_val not in _VALID_RISKS:
        log.warning("llm.invalid_risk", raw=risk_val)
        risk_val = "medium"

    return ClassificationResult(
        category=FailureCategory(category_val),
        risk_level=RiskLevel(risk_val),
        classified_by="llm",
        confidence=float(parsed.get("confidence", 0.5)),
    )


def classify_with_llm(
    error_code: str | None,
    error_description: str | None,
    error_reason: str | None,
    method: str | None,
) -> ClassificationResult:
    """Classify failure using Groq LLM with automatic model fallback."""
    try:
        result = _classify_with_model(
            model=settings.groq_primary_model,
            error_code=error_code,
            error_description=error_description,
            error_reason=error_reason,
            method=method,
        )
        log.info(
            "llm.classified",
            category=result.category,
            model=settings.groq_primary_model,
        )
        return result
    except RateLimitError:
        log.warning(
            "llm.rate_limit_hit_switching_to_fallback",
            primary=settings.groq_primary_model,
            fallback=settings.groq_fallback_model,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("diagnostic.llm_failed", error=str(exc))

    # Fallback — also catches primary model errors
    try:
        result = _classify_with_model(
            model=settings.groq_fallback_model,
            error_code=error_code,
            error_description=error_description,
            error_reason=error_reason,
            method=method,
        )
        log.info(
            "llm.classified",
            category=result.category,
            model=settings.groq_fallback_model,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("diagnostic.llm_fallback_also_failed", error=str(exc))

    # Hard fallback — never crash, always return something safe
    log.warning("llm.using_safe_default")
    return ClassificationResult(
        category=FailureCategory.UNKNOWN,
        risk_level=RiskLevel.MEDIUM,
        classified_by="fallback",
        confidence=0.0,
    )
