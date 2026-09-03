from typing import Any

import structlog
from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent
from pydantic import SecretStr

from app.agents.shared_persona import CORE_IDENTITY
from app.config import settings
from app.ops.tools import (
    ask_human,
    cancel_promise,
    generate_payment_link,
    get_audit_and_chat_history,
    get_promise_details,
    get_recovery_metrics,
    run_playbook,
    search_transactions,
    trigger_customer_email,
)

log = structlog.get_logger(__name__)

OPS_ADDENDUM = """
AUDIENCE: An internal Razorpay ops manager. Skip pleasantries beyond the
one-clause acknowledgment above — this is a working tool, not a support chat.

ACCESS: You can search and act across ALL transactions.

PROACTIVE SUGGESTIONS:
When a user asks to view pending transactions or promises, you MUST proactively ask them if they want to send reminders to those customers using the `send_reminder` playbook or by using the `run_playbook` tool. Instruct them on their capabilities! For example: "Here are the pending transactions. Would you like me to run the 'send_reminder' playbook for any of these to nudge the customers?"

CRITICAL INSTRUCTION FOR AUDIT TRAILS:
When a user asks for the audit trail or chat history of a transaction, you MUST use the `get_audit_and_chat_history` tool.
When returning the result to the user:
1. Always display the audit trail as a table.
2. ALWAYS display the full `chat_history` verbatim as a conversational transcript below the table (e.g. "Customer: ...", "AI: ..."). Do not summarize or omit the chat history!

INTERACTIVE PROMPTS FOR MISSING INFO:
If the user asks you to perform an action (like generating a link, running a playbook, or searching) but FORGETS to provide required information (like a transaction ID, email, or date), you MUST NOT ask them in plain text. Instead, you MUST immediately call the `ask_human` tool.
- If it's an open-ended question (like asking for an ID or Date), call `ask_human(prompt="Please provide the transaction ID or Date:")` (leave options empty to show a text box).
- If it's a multiple choice question, provide the `options` array so they get clickable numbered buttons.
This ensures the Ops Console UI renders the beautiful interactive input boxes!
"""

SYSTEM_PROMPT = CORE_IDENTITY + OPS_ADDENDUM


def get_ops_agent() -> Any:
    llm = ChatGroq(
        api_key=SecretStr(settings.groq_api_key),
        model=settings.groq_primary_model,
        temperature=0,
    ).with_fallbacks(
        [
            ChatGroq(
                api_key=SecretStr(settings.groq_api_key),
                model=settings.groq_fallback_model,
                temperature=0,
            )
        ]
    )

    tools = [
        get_audit_and_chat_history,
        search_transactions,
        get_recovery_metrics,
        get_promise_details,
        cancel_promise,
        generate_payment_link,
        trigger_customer_email,
        run_playbook,
        ask_human,
    ]

    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
    return agent
