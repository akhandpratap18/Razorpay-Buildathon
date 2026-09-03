"""Job executor — runs the LangGraph recovery graph for a claimed job.

Responsibilities:
1. Deserialise the job payload into RecoveryState
2. Create/upsert the transaction row in Supabase
3. Run the LangGraph graph
4. Persist the final state and write audit log entry
5. Return result to poller
"""

from __future__ import annotations

from typing import Any

import asyncpg
import structlog

from app.audit.logger import AuditLogger
from app.graph.graph import recovery_graph
from app.graph.state import RecoveryState
from app.webhook.models import ParsedFailureEvent, RecoveryStatus

log = structlog.get_logger(__name__)


async def execute_job(
    conn: asyncpg.Connection,
    job_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute one recovery job through the LangGraph pipeline.

    Returns a result dict with the final status and transaction_id.
    Raises on unrecoverable errors (causes the poller to requeue with backoff).
    """
    if job_type == "payment.failed":
        return await _handle_payment_failed(conn, payload)
    elif job_type == "order.paid":
        return await _handle_order_paid(conn, payload)
    elif job_type == "payment.success":
        return await _handle_payment_success(conn, payload)
    else:
        log.warning("executor.unknown_job_type", job_type=job_type)
        return {"status": "ignored", "job_type": job_type}


async def _handle_payment_failed(
    conn: asyncpg.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run the full recovery graph for a payment.failed event."""
    event = ParsedFailureEvent.model_validate(payload)
    audit = AuditLogger(conn)

    # Upsert transaction row (idempotent)
    tx_row = await conn.fetchrow(
        """
        INSERT INTO transactions
            (event_id, payment_id, order_id, amount_inr, currency,
             method, error_code, error_description, error_source, error_reason,
             contact, email, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'pending')
        ON CONFLICT (event_id) DO UPDATE
            SET status = EXCLUDED.status
        RETURNING id, status
        """,
        event.event_id,
        event.payment_id,
        event.order_id,
        event.amount_inr,
        event.currency,
        event.method,
        event.error_code,
        event.error_description,
        event.error_source,
        event.error_reason,
        event.contact,
        event.email,
    )

    transaction_id = str(tx_row["id"])

    # Build initial state
    initial_state: RecoveryState = {
        "event_id": event.event_id,
        "transaction_id": transaction_id,
        "payment_id": event.payment_id,
        "order_id": event.order_id,
        "amount_inr": event.amount_inr,
        "currency": event.currency,
        "method": event.method,
        "error_code": event.error_code,
        "error_description": event.error_description,
        "error_source": event.error_source,
        "error_reason": event.error_reason,
        "contact": event.contact,
        "email": event.email,
        "notes": event.notes,
        "status": RecoveryStatus.PENDING,
        "recovery_attempts": 0,
        "messages_sent": [],
        "errors": [],
        "audit_entries": [],
        "customer_opted_out": False,
        "commitment_confirmed": False,
        "split_legs": [],
    }

    await audit.log_event(
        transaction_id=transaction_id,
        event_type="recovery_started",
        actor="executor",
        payload={"event_id": event.event_id, "amount_inr": event.amount_inr},
    )

    # Run the graph (synchronous — LangGraph is sync by default)
    try:
        final_state: Any = recovery_graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": event.event_id}},
        )
    except Exception as exc:
        log.error("executor.graph_failed", event_id=event.event_id, error=str(exc))
        await audit.log_event(
            transaction_id=transaction_id,
            event_type="graph_error",
            actor="executor",
            payload={"error": str(exc)},
        )
        raise

    final_status = final_state.get("status", RecoveryStatus.FAILED)

    # Update transaction row with final state
    await conn.execute(
        """
        UPDATE transactions
        SET status = $1,
            category = $2,
            risk_level = $3,
            classified_by = $4,
            recovery_attempts = $5,
            recovery_link_url = $6,
            recovery_link_id = $7,
            recovery_token = $8
        WHERE id = $9
        """,
        final_status.value if hasattr(final_status, "value") else final_status,
        final_state.get("category"),
        final_state.get("risk_level"),
        final_state.get("classified_by"),
        final_state.get("recovery_attempts", 0),
        final_state.get("recovery_link_url"),
        final_state.get("recovery_link_id"),
        final_state.get("recovery_token"),
        transaction_id,
    )

    await audit.log_event(
        transaction_id=transaction_id,
        event_type="recovery_completed",
        actor="executor",
        payload={
            "final_status": str(final_status),
            "route": final_state.get("selected_route"),
            "attempts": final_state.get("recovery_attempts"),
            "errors": final_state.get("errors", []),
        },
    )

    log.info(
        "executor.complete",
        transaction_id=transaction_id,
        status=final_status,
        route=final_state.get("selected_route"),
    )

    return {"transaction_id": transaction_id, "status": str(final_status)}


async def _handle_order_paid(
    conn: asyncpg.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Mark a transaction as recovered when order.paid arrives."""
    order_id = payload.get("order_id", "")
    if not order_id:
        return {"status": "ignored", "reason": "no_order_id"}

    result = await conn.fetchrow(
        """
        UPDATE transactions
        SET status = 'recovered'
        WHERE order_id = $1
          AND status NOT IN ('recovered', 'killed')
        RETURNING id
        """,
        order_id,
    )

    if result:
        log.info(
            "executor.order_paid_recovered", order_id=order_id, tx_id=str(result["id"])
        )
        return {"status": "recovered", "transaction_id": str(result["id"])}

    return {"status": "no_match", "order_id": order_id}


async def _handle_payment_success(
    conn: asyncpg.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Handle payment_link.paid and payment.captured with partial payment awareness."""
    import re
    from decimal import Decimal

    from app.audit.logger import AuditLogger

    plink_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})

    ref_id = plink_entity.get("reference_id") or ""
    desc = payment_entity.get("description") or ""
    target_str = ref_id if ref_id else desc

    paid_paise = plink_entity.get("amount_paid") or payment_entity.get("amount") or 0
    paid_inr = Decimal(str(paid_paise)) / 100

    match = re.search(
        r"(ops|remind|split|pb_[a-z_]+)_(?:[0-9]+_)?([a-f0-9\-]{36})", target_str
    )
    if not match:
        return {"status": "ignored", "reason": "no_matching_reference_id"}

    tx_id = match.group(2)
    link_type = match.group(1)

    audit = AuditLogger(conn)

    promise = await conn.fetchrow(
        "SELECT id, promised_leg_inr FROM promise_to_pay WHERE transaction_id = $1 AND status NOT IN ('paid', 'broken') ORDER BY created_at DESC LIMIT 1",
        tx_id,
    )

    if link_type == "split":
        await audit.log_event(
            transaction_id=tx_id,
            event_type="immediate_leg_paid",
            actor="system",
            payload={"paid_inr": str(paid_inr)},
        )
        return {"status": "immediate_leg_paid", "transaction_id": tx_id}

    if not promise:
        await conn.execute(
            "UPDATE transactions SET status = 'recovered' WHERE id = $1::uuid AND status != 'recovered'",
            tx_id,
        )
        return {"status": "recovered_no_promise", "transaction_id": tx_id}

    promised_inr = promise["promised_leg_inr"]
    tolerance = Decimal("0.01")

    if abs(paid_inr - promised_inr) <= tolerance:
        await conn.execute(
            "UPDATE promise_to_pay SET status = 'paid', updated_at = NOW() WHERE id = $1",
            promise["id"],
        )
        await conn.execute(
            "UPDATE transactions SET status = 'recovered' WHERE id = $1::uuid", tx_id
        )
        await audit.log_event(
            transaction_id=tx_id,
            event_type="promise_fulfilled",
            actor="system",
            payload={"paid_inr": str(paid_inr), "promised_inr": str(promised_inr)},
        )
        log.info("executor.promise_fulfilled", tx_id=tx_id, paid_inr=str(paid_inr))
        return {"status": "recovered", "transaction_id": tx_id}

    elif paid_inr < promised_inr:
        remainder = promised_inr - paid_inr
        import secrets
        from datetime import datetime, timedelta

        new_due = datetime.utcnow() + timedelta(days=7)
        await conn.execute(
            "UPDATE promise_to_pay SET status = 'partially_paid', updated_at = NOW() WHERE id = $1",
            promise["id"],
        )
        await conn.execute(
            """
            INSERT INTO promise_to_pay (transaction_id, original_order_id, immediate_leg_inr, promised_leg_inr, due_date, status, recovery_token)
            SELECT transaction_id, original_order_id, 0, $2, $3, 'pending', $4 FROM promise_to_pay WHERE id = $1
            """,
            promise["id"],
            remainder,
            new_due,
            secrets.token_urlsafe(32),
        )
        await audit.log_event(
            transaction_id=tx_id,
            event_type="partial_payment_received",
            actor="system",
            payload={"paid_inr": str(paid_inr), "remainder_inr": str(remainder)},
        )
        log.info(
            "executor.partial_payment",
            tx_id=tx_id,
            paid=str(paid_inr),
            remainder=str(remainder),
        )
        return {
            "status": "partial",
            "transaction_id": tx_id,
            "remainder_inr": str(remainder),
        }

    else:
        overpaid = paid_inr - promised_inr
        await conn.execute(
            "UPDATE promise_to_pay SET status = 'paid', updated_at = NOW() WHERE id = $1",
            promise["id"],
        )
        await conn.execute(
            "UPDATE transactions SET status = 'recovered' WHERE id = $1::uuid", tx_id
        )
        await audit.log_event(
            transaction_id=tx_id,
            event_type="overpayment_received",
            actor="system",
            payload={
                "paid_inr": str(paid_inr),
                "promised_inr": str(promised_inr),
                "overpaid_inr": str(overpaid),
            },
        )
        log.info("executor.overpayment", tx_id=tx_id, overpaid=str(overpaid))
        return {"status": "recovered_overpaid", "transaction_id": tx_id}
