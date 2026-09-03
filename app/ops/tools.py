from decimal import Decimal
from typing import Any

import asyncpg
import structlog
from langchain_core.tools import tool

from app.payment.razorpay_client import RazorpayClient

log = structlog.get_logger(__name__)


async def _get_conn() -> asyncpg.Connection:
    from app.db.connection import create_pool

    pool = await create_pool()
    return await pool.acquire()


async def _release_conn(conn: asyncpg.Connection) -> None:
    from app.db.connection import create_pool

    pool = await create_pool()
    await pool.release(conn)


@tool
async def search_transactions(
    status: str | None = None,
    error_reason: str | None = None,
    has_promise: bool | None = None,
    promise_status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    transaction_id_match: str | None = None,
    email_match: str | None = None,
    limit: int = 5,
) -> str:
    """
    Search for transactions using specific filters.
    Use this to find failed/pending transactions, or to find broken promises.
    Arguments:
    - status: (e.g. 'failed', 'pending', 'recovered', 'escalated')
    - error_reason: (e.g. 'insufficient_funds', 'bank_downtime')
    - has_promise: set to True if you only want transactions that have a Promise-to-Pay.
    - promise_status: (e.g. 'broken', 'pending', 'fulfilled') if searching for specific promise states.
    - start_date: (YYYY-MM-DD) Filter transactions created after this date.
    - end_date: (YYYY-MM-DD) Filter transactions created before this date.
    - transaction_id_match: A partial or full transaction ID to search for (e.g. 'af863275' or full UUID).
    - email_match: A partial or full customer email to search for.
    """
    conn = await _get_conn()
    try:
        query_parts = [
            "SELECT t.id, t.amount_inr, t.category, t.error_reason, t.status, t.created_at, t.email, t.contact FROM transactions t"
        ]

        if has_promise or promise_status:
            query_parts.append("JOIN promise_to_pay p ON t.id = p.transaction_id::uuid")

        where_clauses = []
        args: list[Any] = []
        arg_idx = 1

        if status:
            where_clauses.append(f"t.status = ${arg_idx}")
            args.append(status.lower())
            arg_idx += 1

        if error_reason:
            where_clauses.append(f"t.error_reason ILIKE ${arg_idx}")
            args.append(f"%{error_reason}%")
            arg_idx += 1

        if promise_status:
            where_clauses.append(f"p.status = ${arg_idx}")
            args.append(promise_status.lower())
            arg_idx += 1

        if transaction_id_match:
            where_clauses.append(f"t.id::text ILIKE ${arg_idx}")
            args.append(f"%{transaction_id_match}%")
            arg_idx += 1

        if email_match:
            where_clauses.append(f"t.email ILIKE ${arg_idx}")
            args.append(f"%{email_match}%")
            arg_idx += 1

        from datetime import datetime

        if start_date:
            try:
                parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
                where_clauses.append(f"t.created_at >= ${arg_idx}::date")
                args.append(parsed_start)
                arg_idx += 1
            except ValueError:
                pass  # Ignore invalid date formats

        if end_date:
            try:
                parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
                where_clauses.append(f"t.created_at <= ${arg_idx}::date")
                args.append(parsed_end)
                arg_idx += 1
            except ValueError:
                pass

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        query_parts.append(f"ORDER BY t.created_at DESC LIMIT ${arg_idx}")
        args.append(limit)

        sql = " ".join(query_parts)
        rows = await conn.fetch(sql, *args)

        import json

        return json.dumps([dict(r) for r in rows], default=str)
    finally:
        await _release_conn(conn)


@tool
async def get_audit_and_chat_history(transaction_id: str) -> str:
    """Get the audit trail and the full customer chat history for a transaction."""
    conn = await _get_conn()
    try:
        # Get audit logs
        audit_rows = await conn.fetch(
            """
            SELECT event_type, actor, payload, created_at
            FROM audit_log
            WHERE transaction_id = $1::uuid
            ORDER BY created_at ASC
            """,
            transaction_id,
        )

        # Get chat history
        chat_row = await conn.fetchrow(
            """
            SELECT c.messages
            FROM chat_history c
            JOIN transactions t ON c.recovery_token = t.recovery_token
            WHERE t.id = $1::uuid
            """,
            transaction_id,
        )

        import json

        result = {"audit_trail": [dict(r) for r in audit_rows], "chat_history": []}

        if chat_row and chat_row["messages"]:
            messages = (
                json.loads(chat_row["messages"])
                if isinstance(chat_row["messages"], str)
                else chat_row["messages"]
            )
            # Extract just the content of the messages for readability
            readable_chat = []
            for msg in messages:
                role = msg.get("type", "unknown")
                if role in ("human", "ai"):
                    readable_chat.append({role: msg.get("data", {}).get("content", "")})
            result["chat_history"] = readable_chat

        return json.dumps(result, default=str)
    except Exception as e:
        return f"Error retrieving audit trail: {str(e)}"
    finally:
        await _release_conn(conn)


@tool
async def get_promise_details(transaction_id: str) -> str:
    """Get the active Promise-to-Pay details for a given transaction ID."""
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            SELECT id, immediate_leg_inr, promised_leg_inr, due_date, status, reminder_count, promised_payment_link
            FROM promise_to_pay
            WHERE transaction_id = $1
            ORDER BY created_at DESC LIMIT 1
            """,
            transaction_id,
        )
        import json

        return json.dumps(dict(row), default=str) if row else "No promise found."
    finally:
        await _release_conn(conn)


@tool
async def cancel_promise(transaction_id: str) -> str:
    """Cancel a promise-to-pay and escalate the transaction."""
    conn = await _get_conn()
    try:
        await conn.execute(
            "UPDATE promise_to_pay SET status = 'cancelled' WHERE transaction_id = $1",
            transaction_id,
        )
        await conn.execute(
            "UPDATE transactions SET status = 'escalated' WHERE id = $1::uuid",
            transaction_id,
        )
        return f"Promise cancelled and transaction {transaction_id} escalated."
    finally:
        await _release_conn(conn)


@tool
async def generate_payment_link(
    amount_inr: str,
    description: str,
    transaction_id: str | None = None,
    expiry_date: str | None = None,
    customer_email: str | None = None,
    customer_contact: str | None = None,
) -> str:
    """Generate a direct Razorpay payment link.

    `transaction_id` is optional — only provide it if the link is tied to a failed transaction.
    `expiry_date` is optional (YYYY-MM-DD). For standalone links, you SHOULD call ask_human to ask
    whether the manager wants an expiry date or no expiry before calling this tool.
    If expiry_date is 'none' or empty, the link is created with no expiry.
    If `customer_email` or `customer_contact` are provided, they will be attached to the link in Razorpay.
    """
    import time
    import uuid

    from app.audit.logger import AuditLogger
    from app.guardrails.allowlist import (
        enforce_amount_ceiling,
        enforce_standalone_ceiling,
    )

    proposed_amount = Decimal(amount_inr)
    amount_paise = int(proposed_amount * 100)
    client = RazorpayClient()

    conn = await _get_conn()
    try:
        contact = None
        email = None

        if (
            transaction_id
            and transaction_id.strip()
            and transaction_id.lower() != "none"
        ):
            row = await conn.fetchrow(
                "SELECT amount_inr, email, contact FROM transactions WHERE id = $1::uuid",
                transaction_id,
            )
            if not row:
                return f"Error: Transaction {transaction_id} not found."

            original_amount = row["amount_inr"]
            # Guardrail: Enforce ceiling
            try:
                enforce_amount_ceiling(original_amount, proposed_amount)
            except ValueError as e:
                return f"Guardrail Violation: {str(e)}"

            contact = customer_contact or row.get("contact")
            email = customer_email or row.get("email")
            idem_key = f"ops_{int(time.time())}_{transaction_id}"[:40]
        else:
            # Standalone generic payment link
            try:
                enforce_standalone_ceiling(proposed_amount)
            except ValueError as e:
                return f"Guardrail Violation: {str(e)}"
            idem_key = f"ops_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            contact = customer_contact
            email = customer_email

        # Resolve expiry
        expire_by_unix = None
        if (
            expiry_date
            and expiry_date.strip()
            and expiry_date.lower() not in ("none", "no", "never", "")
        ):
            try:
                from datetime import datetime as _dt

                expire_by_unix = int(
                    _dt.strptime(expiry_date.strip(), "%Y-%m-%d").timestamp()
                )
            except ValueError:
                pass  # bad format — ignore, no expiry

        # Generate link
        try:
            link = client.create_payment_link(
                amount_paise=amount_paise,
                currency="INR",
                description=description,
                contact=contact,
                email=email,
                idempotency_key=idem_key,
                expire_by_unix=expire_by_unix,
            )
        except Exception as e:
            return f"Failed to generate link due to Razorpay API error: {str(e)}"
        short_url = link.get("short_url", "")
        is_mock = link.get("is_mock", False)
        if is_mock:
            short_url = short_url + " (MOCK LINK - TEST QUOTA EXHAUSTED)"

        # Guardrail: Audit Log
        logger = AuditLogger(conn)
        await logger.log_event(
            transaction_id=(
                transaction_id
                if transaction_id
                and transaction_id.strip()
                and transaction_id.lower() != "none"
                else None
            ),
            event_type="ops_payment_link_generated",
            actor="ops_agent",
            payload={
                "amount_inr": str(proposed_amount),
                "link": short_url,
                "description": description,
            },
        )
        if (
            transaction_id
            and transaction_id.strip()
            and transaction_id.lower() != "none"
        ):
            await conn.execute(
                "UPDATE transactions SET recovery_link_url = $1 WHERE id = $2::uuid",
                short_url,
                transaction_id,
            )
        else:
            import secrets

            dummy_pid = f"manual_{uuid.uuid4().hex[:16]}"
            dummy_token = secrets.token_urlsafe(32)
            await conn.execute(
                """INSERT INTO transactions
                (payment_id, amount_inr, category, status, error_reason, recovery_token, email, contact, recovery_link_url)
                VALUES ($1, $2, 'untracked_manual_link', 'pending', 'Manual standalone link', $3, $4, $5, $6)
                """,
                dummy_pid,
                float(proposed_amount),
                dummy_token,
                email,
                contact,
                short_url,
            )

        return f"Link generated successfully: {short_url}"
    finally:
        await _release_conn(conn)


@tool
async def trigger_customer_email(
    transaction_id: str | None = None,
    payment_link: str | None = None,
    amount_inr: str | None = None,
    direct_email: str | None = None,
) -> str:
    """Trigger an email to a customer.
    - If `transaction_id` is provided: looks up the customer email from the database.
    - If `direct_email` is provided (e.g. 'user@gmail.com'): sends the email directly without needing a transaction.
    - If both `payment_link` and `amount_inr` are provided, sends the payment link template; otherwise sends the standard recovery template.
    """
    from app.audit.logger import AuditLogger
    from app.communication.fallback_chain import (
        send_recovery_email,
        send_reminder_email,
    )

    conn = await _get_conn()
    try:
        email = direct_email
        recovery_token = None

        # Look up email from transaction if not provided directly
        if not email and transaction_id:
            row = await conn.fetchrow(
                "SELECT email, recovery_token FROM transactions WHERE id = $1::uuid",
                transaction_id,
            )
            if not row or not row["email"]:
                return f"Error: Transaction {transaction_id} not found or has no email address."
            email = row["email"]

            promise_row = await conn.fetchrow(
                """SELECT recovery_token FROM promise_to_pay
                   WHERE transaction_id = $1
                   ORDER BY created_at DESC LIMIT 1""",
                transaction_id,
            )
            recovery_token = (
                promise_row["recovery_token"] if promise_row else row["recovery_token"]
            )

            if not recovery_token:
                return "Error: No valid recovery token found for this transaction or its promise — refusing to send a broken link."

        if not email:
            return "Error: No email address provided. Please provide either a transaction_id or a direct_email address."

        try:
            if payment_link and amount_inr:
                send_reminder_email(
                    transaction_id or "standalone",
                    email,
                    recovery_token or "",
                    amount_inr,
                    payment_link,
                )
                template_used = "payment_link_template"
            else:
                send_recovery_email(
                    transaction_id or "standalone", email, recovery_token or ""
                )
                template_used = "chat_recovery_template"

            # Guardrail: Audit Log
            logger = AuditLogger(conn)
            await logger.log_event(
                transaction_id=transaction_id if transaction_id else None,
                event_type="ops_email_triggered",
                actor="ops_agent",
                payload={
                    "template": template_used,
                    "payment_link": payment_link,
                    "email": email,
                },
            )

            # If it's a reminder and tied to a transaction, increment the promise reminder count
            if template_used == "payment_link_template" and transaction_id:
                await conn.execute(
                    """
                    UPDATE promise_to_pay
                    SET reminder_count = reminder_count + 1, updated_at = NOW()
                    WHERE id = (
                        SELECT id FROM promise_to_pay
                        WHERE transaction_id = $1
                        ORDER BY created_at DESC LIMIT 1
                    )
                    """,
                    transaction_id,
                )

            return f"Successfully triggered {template_used} to {email}."
        except Exception as exc:
            return f"Failed to send email: {exc}"
    finally:
        await _release_conn(conn)


@tool
async def get_recovery_metrics() -> str:
    """Get the current recovery rate, failed transaction counts, and overall metrics."""
    import json

    from app.ops.metrics import compute_recovery_metrics

    conn = await _get_conn()
    try:
        metrics = await compute_recovery_metrics(conn)
        return json.dumps(metrics, indent=2)
    except Exception as e:
        return f"Failed to get metrics: {e}"
    finally:
        await _release_conn(conn)


class AskHumanException(Exception):
    def __init__(self, prompt: str, options: list[str] | None = None):
        self.prompt = prompt
        self.options = options
        super().__init__(prompt)


@tool
def ask_human(prompt: str, options: list[str] | None = None) -> str:
    """Ask the human operator for a piece of missing information needed to complete their request.
    Use `options` when there's a specific, short set of valid choices (e.g. which category, which action).
    Omit it for genuinely open-ended input (e.g. a transaction ID, an amount)."""
    raise AskHumanException(prompt, options)


@tool
async def run_playbook(
    playbook_name: str,
    transaction_id: str,
    customer_email: str | None = None,
) -> str:
    """Execute a pre-approved multi-step recovery playbook autonomously and report each step.

    Available playbooks:
    - 'broken_promise': Generate new payment link + email customer + cancel/close the broken promise
    - 'recovery_nudge': Generate payment link + email customer

    `transaction_id` is required. `customer_email` is optional if the transaction already has an email.
    """
    import time as _time

    from app.audit.logger import AuditLogger
    from app.communication.fallback_chain import send_reminder_email
    from app.ops.playbooks import get_playbook, list_playbooks
    from app.payment.razorpay_client import RazorpayClient

    playbook = get_playbook(playbook_name)
    if not playbook:
        return f"Unknown playbook '{playbook_name}'. Available: {', '.join(list_playbooks())}"

    conn = await _get_conn()
    results = []
    try:
        row = await conn.fetchrow(
            "SELECT amount_inr, email, contact, status, recovery_token FROM transactions WHERE id = $1::uuid",
            transaction_id,
        )
        if not row:
            return f"Transaction {transaction_id} not found."

        email = customer_email or row["email"]
        amount_inr = row["amount_inr"]
        contact = row["contact"]
        client = RazorpayClient()
        payment_link: str | None = None

        results.append(f"▶ **{playbook['name']}** — `...{transaction_id[-8:]}`")

        for step in playbook["steps"]:
            tool_name = step["tool"]

            if tool_name == "generate_payment_link":
                try:
                    idem = (
                        f"pb_{playbook_name}_{transaction_id[-8:]}_{int(_time.time())}"
                    )
                    link_data = client.create_payment_link(
                        amount_paise=int(amount_inr * 100),
                        currency="INR",
                        description=f"{playbook['name']} recovery",
                        contact=contact,
                        email=email,
                        idempotency_key=idem,
                    )
                    payment_link = link_data.get("short_url", "")
                    if link_data.get("is_mock"):
                        payment_link += " (MOCK)"
                    results.append(f"✅ Payment link generated: {payment_link}")
                except Exception as e:
                    results.append(f"❌ Link generation failed: {e}")
                    break

            elif tool_name == "resend_promise_reminder":
                try:
                    res = await resend_promise_reminder.ainvoke(
                        {"transaction_id": transaction_id}
                    )
                    results.append(f"✅ Reminder: {res}")
                except Exception as e:
                    results.append(f"❌ Reminder failed: {e}")
                    break

            elif tool_name == "trigger_customer_email":
                if not email:
                    results.append("⚠️ No email on file — email step skipped.")
                    continue
                try:
                    tx_recovery_token = row["recovery_token"] or ""
                    send_reminder_email(
                        transaction_id=transaction_id,
                        email=email,
                        recovery_token=tx_recovery_token,
                        amount=str(amount_inr),
                        payment_link=payment_link or "",
                    )
                    results.append(f"✅ Email sent to {email}")
                except Exception as e:
                    results.append(f"❌ Email failed: {e}")

            elif tool_name == "cancel_promise":
                try:
                    await conn.execute(
                        "UPDATE promise_to_pay SET status = 'broken' WHERE transaction_id = $1 AND status IN ('pending', 'reminded')",
                        transaction_id,
                    )
                    await conn.execute(
                        "UPDATE transactions SET status = 'escalated' WHERE id = $1::uuid",
                        transaction_id,
                    )
                    results.append(
                        "✅ Promise closed as broken, transaction escalated."
                    )
                except Exception as e:
                    results.append(f"❌ Promise cancel failed: {e}")

        # Extract link if generated
        pb_link = None
        for r in results:
            if "Payment link generated:" in r:
                pb_link = r.split("Payment link generated:")[1].strip()

        audit = AuditLogger(conn)
        await audit.log_event(
            transaction_id=transaction_id,
            event_type="playbook_executed",
            actor="ops_agent",
            payload=(
                {
                    "playbook": playbook_name,
                    "steps": len(playbook["steps"]),
                    "payment_link": pb_link,
                }
                if pb_link
                else {"playbook": playbook_name, "steps": len(playbook["steps"])}
            ),
        )
        if (
            pb_link
            and transaction_id
            and transaction_id.strip()
            and transaction_id.lower() != "none"
        ):
            await conn.execute(
                "UPDATE transactions SET recovery_link_url = $1 WHERE id = $2::uuid",
                pb_link,
                transaction_id,
            )

        results.append(f"\n✅ Playbook complete — {len(playbook['steps'])} steps run.")
        return "\n".join(results)
    finally:
        await _release_conn(conn)


@tool
async def resend_promise_reminder(transaction_id: str) -> str:
    """Resend the reminder for an EXISTING promise — does not create a new link
    unless the promise's link has expired. Increments reminder_count. Use this
    instead of run_playbook when a transaction already has an active promise."""
    from app.audit.logger import AuditLogger
    from app.communication.fallback_chain import send_reminder_email
    from app.payment.razorpay_client import RazorpayClient

    conn = await _get_conn()
    try:
        promise = await conn.fetchrow(
            "SELECT * FROM promise_to_pay WHERE transaction_id = $1 AND status IN ('pending','reminded') ORDER BY created_at DESC LIMIT 1",
            transaction_id,
        )
        if not promise:
            return "No active promise found for this transaction — use run_playbook to create a fresh link instead."

        link = promise["promised_payment_link"]
        if not link:
            # only generate one if none exists yet — don't replace a live link
            client = RazorpayClient()
            link_data = client.create_payment_link(
                amount_paise=int(promise["promised_leg_inr"] * 100),
                currency="INR",
                description="Promised payment reminder",
                contact=None,
                email=None,
                idempotency_key=f"promise_reminder_{promise['id']}",
            )
            link = link_data.get("short_url", "")
            await conn.execute(
                "UPDATE promise_to_pay SET promised_payment_link = $1 WHERE id = $2",
                link,
                promise["id"],
            )
            await conn.execute(
                "UPDATE transactions SET recovery_link_url = $1 WHERE id = $2::uuid",
                link,
                transaction_id,
            )

        tx = await conn.fetchrow(
            "SELECT email FROM transactions WHERE id = $1::uuid", transaction_id
        )
        if tx and tx["email"]:
            send_reminder_email(
                transaction_id=transaction_id,
                email=tx["email"],
                recovery_token=promise["recovery_token"],
                amount=str(promise["promised_leg_inr"]),
                payment_link=link,
            )

        await conn.execute(
            "UPDATE promise_to_pay SET reminder_count = reminder_count + 1, status = 'reminded' WHERE id = $1",
            promise["id"],
        )
        await AuditLogger(conn).log_event(
            transaction_id=transaction_id,
            event_type="manual_reminder_sent",
            actor="ops_agent",
            payload={
                "reminder_count": promise["reminder_count"] + 1,
                "payment_link": link,
            },
        )
        return f"Reminder sent. This is reminder #{promise['reminder_count'] + 1} for this promise."
    except Exception as e:
        return f"Failed to send reminder: {str(e)}"
    finally:
        await _release_conn(conn)
