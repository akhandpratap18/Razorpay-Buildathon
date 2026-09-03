"""Lightweight sentiment scorer for customer chat messages."""

from __future__ import annotations

import structlog
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq
from pydantic import SecretStr

from app.config import settings

log = structlog.get_logger(__name__)

# Score in order: cooperative, resistant, hostile
VALID_SCORES = {"cooperative", "resistant", "hostile"}


async def score_sentiment(user_message: str) -> str:
    """Score the sentiment of a customer message.

    Returns one of: 'cooperative', 'resistant', 'hostile'.
    Defaults to 'cooperative' on any failure so it never breaks the chat flow.
    """
    try:
        llm = ChatGroq(
            api_key=SecretStr(settings.groq_api_key),
            model=settings.groq_fallback_model,
            temperature=0,
        )
        prompt = (
            "Classify the sentiment of this customer message in a payment recovery context. "
            "Reply with ONLY one word — no explanation.\n"
            "- cooperative: willing to pay, asking questions, polite, neutral\n"
            "- resistant: making excuses, asking for delays, evasive, non-committal\n"
            "- hostile: threatening, abusive language, explicit refusal, demanding to be left alone\n\n"
            f"Message: {user_message[:500]}"
        )
        result = await llm.ainvoke([HumanMessage(content=prompt)])
        score = str(result.content).strip().lower().split()[0]
        return score if score in VALID_SCORES else "cooperative"
    except Exception as e:
        log.warning("sentiment.score_failed", error=str(e))
        return "cooperative"
