import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent
from pydantic import SecretStr

from app.agents.shared_persona import CORE_IDENTITY
from app.config import settings
from app.payment.razorpay_client import RazorpayClient
from app.payment.split_math import calculate_partial_and_promise

log = structlog.get_logger(__name__)


CUSTOMER_ADDENDUM = """
AUDIENCE: A customer whose payment failed. Be warmer than the internal
tool register — this person may be frustrated or confused. Still concise;
warmth means acknowledging the situation, not padding with extra sentences.

ACCESS: You may only see and act on the ONE transaction tied to this
conversation's token — you have no knowledge of and no tools for any other
transaction, any other customer, or any internal metrics. If asked about
anything outside this transaction, say plainly that you don't have access
to that.
"""


def get_system_prompt(
    amount_inr: Decimal,
    category: str,
    active_promise_text: str = "",
    error_reason: str = "",
) -> str:
    category_type = "SUBSCRIPTION" if "subscription" in category.lower() else "ONE_TIME"

    negotiation_instructions = f"""
TRANSACTION CONTEXT:
Failed Amount: ₹{amount_inr}
Type: {category_type}
Failure Reason: {error_reason}
{active_promise_text}
Today's Date: {datetime.utcnow().strftime("%Y-%m-%d")}

TASK RULES:
- NEVER make up or confirm a successful payment status without using a tool.
- TWO-STEP INTRO: You have already asked the user if they want help. If their first response is just "yes", you should THEN suggest their options.
- If ONE_TIME: Do not mention subscriptions or saved cards. If they want to just pay the full amount, use `generate_full_payment_link`. Offer to split the payment into two parts if they can't pay the full amount today. Use `agree_to_split` when they provide an immediate amount and a future date.
- If SUBSCRIPTION: Offer to either retry their saved card right now (use `retry_same_method`), send a link to update their card (use `update_payment_method_link`), or pause it (use `pause_subscription`). Explain these options clearly.
- If they want to opt out or stop hearing from us, use `record_opt_out`.
- If the customer says they cannot pay at all, explicitly asks for a human, or gets extremely frustrated, you MUST use `request_human_escalation`.
- Format any dates in a human-readable format (e.g., "August 25, 2026") rather than YYYY-MM-DD.
- CRITICAL LANGUAGE RULE: You MUST respond ENTIRELY in the same language as the user's messages. If the user writes in Hindi or Hinglish, your response MUST be in Hindi. If the user writes in English, your response MUST be in English.
- FORMATTING RULE: NEVER output markdown tables, bold text, or special formatting. You are speaking in a voice UI. Keep responses natural, conversational, and in plain text sentences.
"""
    return CORE_IDENTITY + CUSTOMER_ADDENDUM + negotiation_instructions


def get_customer_agent(
    transaction_id: str,
    amount_inr: Decimal,
    order_id: str,
    email: str | None = None,
    contact: str | None = None,
    category: str = "one_time",
    active_promise_text: str = "",
    has_active_promise: bool = False,
    negotiation_attempts: int = 0,
    recovery_token: str = "",
    error_reason: str = "",
) -> Any:

    @tool
    async def retry_same_method() -> str:
        """Use this tool if the customer wants to retry their failed subscription payment with the exact same payment method they used before."""
        from app.audit.logger import AuditLogger
        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await AuditLogger(conn).log_event(
                transaction_id=transaction_id,
                event_type="subscription_retry_requested",
                actor="customer_agent",
                payload={},
            )
        finally:
            await pool.release(conn)
        return "The subscription payment has been queued for immediate retry. You will receive an email receipt shortly."

    @tool
    async def update_payment_method_link() -> str:
        """Use this tool if the customer wants to use a different card or payment method for their subscription."""
        from app.payment.razorpay_client import RazorpayClient

        client = RazorpayClient()
        try:
            # We create a 1 INR authorization link to bind the new card
            link_data = client.create_payment_link(
                amount_paise=100,
                currency="INR",
                description="Update Subscription Payment Method",
                contact=contact or "",
                email=email or "",
                idempotency_key=f"update_sub_{transaction_id[:8]}",
            )

            from app.audit.logger import AuditLogger
            from app.db.connection import create_pool

            pool = await create_pool()
            conn = await pool.acquire()
            try:
                await AuditLogger(conn).log_event(
                    transaction_id=transaction_id,
                    event_type="payment_method_update_requested",
                    actor="customer_agent",
                    payload={"payment_link": link_data["short_url"]},
                )
            finally:
                await pool.release(conn)

            return f"Here is your secure link to update your payment method: {link_data['short_url']}. Please click it to add your new card."
        except Exception:
            return "Here is your secure link to update your payment method: https://rzp.io/i/update-sub. Please click it to add your new card."

    @tool
    async def pause_subscription() -> str:
        """Use this tool ONLY if the customer explicitly asks to pause, suspend, or downgrade their subscription because they cannot pay right now."""
        from app.audit.logger import AuditLogger
        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await conn.execute(
                "UPDATE transactions SET status = 'killed' WHERE id = $1::uuid",
                transaction_id,
            )
            await AuditLogger(conn).log_event(
                transaction_id=transaction_id,
                event_type="subscription_paused",
                actor="customer_agent",
                payload={},
            )
        finally:
            await pool.release(conn)
        return "I have successfully paused your subscription for the current billing cycle. You won't be charged, and your services will remain paused until you reactivate."

    @tool
    async def record_opt_out() -> str:
        """Call this tool if the customer explicitly says stop contacting me, don't email me, or opt out."""
        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await conn.execute(
                "INSERT INTO opt_outs (phone, transaction_id, opted_out_at) VALUES ($1, $2::uuid, NOW()) ON CONFLICT DO NOTHING",
                contact or "unknown",
                transaction_id,
            )

            await conn.execute(
                "UPDATE transactions SET status = 'killed' WHERE id = $1::uuid",
                transaction_id,
            )
            from app.audit.logger import AuditLogger

            await AuditLogger(conn).log_event(
                transaction_id=transaction_id,
                event_type="customer_opted_out",
                actor="customer_agent",
                payload={},
            )
        finally:
            await pool.release(conn)

        return "Opt-out recorded. Customer will not be contacted again."

    @tool
    async def generate_full_payment_link() -> str:
        """Call this tool if the customer just wants to pay the full failed amount now."""
        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await conn.execute(
                "UPDATE transactions SET negotiation_attempts = negotiation_attempts + 1 WHERE id = $1::uuid",
                transaction_id,
            )
        finally:
            await pool.release(conn)

        client = RazorpayClient()
        expire_immediate = int((datetime.utcnow() + timedelta(days=3)).timestamp())
        try:
            link_data = client.create_payment_link(
                amount_paise=int(amount_inr * 100),
                currency="INR",
                description=f"Full Payment for {transaction_id}",
                contact=contact,
                email=email,
                idempotency_key=f"ops_{transaction_id}",
                expire_by_unix=expire_immediate,
            )
        except Exception as e:
            return f"Failed to generate Razorpay link due to API error: {str(e)}. Tell the customer to please try again in a minute."
        short_url = link_data.get("short_url", "")
        if link_data.get("is_mock"):
            pass  # Do not append space to url

        from app.audit.logger import AuditLogger
        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await conn.execute(
                "UPDATE transactions SET recovery_link_url = $1 WHERE id = $2::uuid",
                short_url,
                transaction_id,
            )
            await AuditLogger(conn).log_event(
                transaction_id=transaction_id,
                event_type="payment_link_generated",
                actor="customer_agent",
                payload={"payment_link": short_url, "amount": amount_inr},
            )
        finally:
            await pool.release(conn)

        # Send email with payment link + chat support link
        if email and short_url:
            try:
                from app.communication.fallback_chain import send_reminder_email

                send_reminder_email(
                    transaction_id=transaction_id,
                    email=email,
                    recovery_token=recovery_token,
                    amount=str(amount_inr),
                    payment_link=short_url,
                )
            except Exception:
                pass  # Email failure should never block the chat reply

        return f"Payment link generated successfully. Provide this exact Markdown to the customer so they can click it: [Click here to pay ₹{amount_inr}]({short_url})"

    @tool
    async def agree_to_split(
        immediate_amount_inr: float = 0.0, due_date_str: str = ""
    ) -> str:
        """Call this tool when the customer confirms the amount AND the date for the remainder. You MUST pass `immediate_amount_inr` (float) and `due_date_str` (YYYY-MM-DD)."""

        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await conn.execute(
                "UPDATE transactions SET negotiation_attempts = negotiation_attempts + 1 WHERE id = $1::uuid",
                transaction_id,
            )
        finally:
            await pool.release(conn)

        if not immediate_amount_inr or immediate_amount_inr <= 0:
            return "ERROR: You forgot to provide `immediate_amount_inr`. You must ask the user for the amount and confirm it first."

        if not due_date_str:
            return "ERROR: You forgot to provide `due_date_str`. You must ask the user what date they want to schedule the remaining balance for (in YYYY-MM-DD format)."

        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
        except ValueError:
            return "ERROR: `due_date_str` must be in YYYY-MM-DD format."

        # Ensure date is in the future
        if due_date.date() < datetime.utcnow().date():
            due_date = datetime.utcnow() + timedelta(days=7)

        try:
            split = calculate_partial_and_promise(
                amount_inr, Decimal(str(immediate_amount_inr))
            )
        except ValueError as e:
            return f"ERROR: {str(e)} Tell the user they need to pay a higher amount."

        client = RazorpayClient()
        expire_immediate = int((due_date + timedelta(days=3)).timestamp())
        try:
            link_data = client.create_payment_link(
                amount_paise=int(split.immediate_leg_inr * 100),
                currency="INR",
                description=f"Partial Payment for {transaction_id}",
                contact=contact,
                email=email,
                idempotency_key=f"split_{transaction_id}_{int(split.immediate_leg_inr * 100)}",
                expire_by_unix=expire_immediate,
            )
        except Exception as e:
            return f"Failed to generate Razorpay link due to API error: {str(e)}. Tell the customer to please try again in a minute."
        short_url = link_data.get("short_url", "")
        if link_data.get("is_mock"):
            pass  # Do not append space to url

        # Generate the promised payment link immediately
        expire_promised = int((due_date + timedelta(days=2)).timestamp())
        try:
            promised_link_data = client.create_payment_link(
                amount_paise=int(split.promised_leg_inr * 100),
                currency="INR",
                description=f"Scheduled Payment for {transaction_id}",
                contact=contact,
                email=email,
                idempotency_key=f"remind_{transaction_id}",
                expire_by_unix=expire_promised,
            )
            promised_short_url = promised_link_data.get("short_url", "")
            if promised_link_data.get("is_mock"):
                pass  # Do not append space to url
        except Exception:
            promised_short_url = None

        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            existing = await conn.fetchrow(
                "SELECT id FROM promise_to_pay WHERE transaction_id = $1 AND status NOT IN ('paid')",
                transaction_id,
            )
            recovery_token = secrets.token_urlsafe(32)
            if existing:
                await conn.execute(
                    """
                    UPDATE promise_to_pay
                    SET immediate_leg_inr = $1, promised_leg_inr = $2, due_date = $3, promised_payment_link = $4, status = 'pending'
                    WHERE id = $5
                    """,
                    split.immediate_leg_inr,
                    split.promised_leg_inr,
                    due_date,
                    promised_short_url,
                    existing["id"],
                )
                row = await conn.fetchrow(
                    "SELECT recovery_token FROM promise_to_pay WHERE id = $1",
                    existing["id"],
                )
                recovery_token = row["recovery_token"]
            else:
                await conn.execute(
                    """
                    INSERT INTO promise_to_pay (transaction_id, original_order_id, immediate_leg_inr, promised_leg_inr, due_date, status, recovery_token, promised_payment_link)
                    VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7)
                    """,
                    transaction_id,
                    order_id or "N/A",
                    split.immediate_leg_inr,
                    split.promised_leg_inr,
                    due_date,
                    recovery_token,
                    promised_short_url,
                )

            from app.audit.logger import AuditLogger

            await AuditLogger(conn).log_event(
                transaction_id=transaction_id,
                event_type="customer_agreed_split",
                actor="customer_agent",
                payload={
                    "immediate_leg_inr": str(split.immediate_leg_inr),
                    "promised_leg_inr": str(split.promised_leg_inr),
                    "due_date": due_date.isoformat(),
                    "payment_link": short_url,
                },
            )
            await conn.execute(
                "UPDATE transactions SET recovery_link_url = $1 WHERE id = $2::uuid",
                short_url,
                transaction_id,
            )

            # Send email immediately with the promised link
            if email and promised_short_url:
                try:
                    from app.communication.fallback_chain import send_reminder_email

                    send_reminder_email(
                        transaction_id=str(transaction_id),
                        email=email,
                        recovery_token=recovery_token,
                        amount=str(split.promised_leg_inr),
                        payment_link=promised_short_url,
                    )
                except Exception:
                    pass  # Non-fatal if email fails

        finally:
            from app.db.connection import create_pool

            pool = await create_pool()
            await pool.release(conn)

        return f"Success! Generated IMMEDIATE payment link for {split.immediate_leg_inr} INR: {short_url}. The remainder of {split.promised_leg_inr} INR has been successfully scheduled for {due_date.strftime('%Y-%m-%d')}."

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

    @tool
    async def request_human_escalation(reason: str) -> str:
        """Call this when the customer explicitly asks to speak to a human,
        says they can't or won't resolve this through chat, or when you've
        tried reasonable options and the conversation isn't progressing.
        `reason` should be a short, specific summary for the ops team —
        not a generic string."""
        from app.audit.logger import AuditLogger
        from app.db.connection import create_pool

        pool = await create_pool()
        conn = await pool.acquire()
        try:
            await conn.execute(
                """UPDATE transactions
                   SET status = 'escalated', error_reason = $2
                   WHERE id = $1::uuid""",
                transaction_id,
                reason,
            )
            await AuditLogger(conn).log_event(
                transaction_id=transaction_id,
                event_type="customer_requested_escalation",
                actor="customer_agent",
                payload={"reason": reason},
            )
        finally:
            await pool.release(conn)

        return (
            "I've flagged this for a member of our team to follow up with you "
            "directly. They'll reach out as soon as possible."
        )

    # If the customer has an active promise, remove agree_to_split entirely.
    # This makes renegotiation/extension technically impossible — not just prompted-against.
    # request_human_support is ALWAYS available regardless of promise state.
    available_tools = [record_opt_out, request_human_escalation]
    max_negotiation_attempts = 3
    if not has_active_promise:
        available_tools.append(generate_full_payment_link)
        if negotiation_attempts < max_negotiation_attempts:
            available_tools.insert(0, agree_to_split)

    # Subscription-specific tools — only registered when relevant
    is_subscription = "subscription" in category.lower()
    if is_subscription:
        available_tools.extend(
            [retry_same_method, update_payment_method_link, pause_subscription]
        )

    return create_react_agent(
        llm,
        available_tools,
        prompt=get_system_prompt(
            amount_inr, category, active_promise_text, error_reason
        ),
    )
