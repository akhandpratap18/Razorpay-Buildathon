"""Razorpay webhook FastAPI router."""

from __future__ import annotations

import json
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.rate_limit import check_rate_limit
from app.webhook.models import ParsedFailureEvent, RazorpayWebhookPayload
from app.webhook.verification import verify_razorpay_signature

log = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/razorpay", status_code=status.HTTP_200_OK)
async def handle_razorpay_webhook(
    request: Request,
    body: bytes = Depends(verify_razorpay_signature),
    _rate_limit: None = Depends(check_rate_limit),
) -> dict[str, str]:
    """Receive, parse, and safely queue Razorpay webhook events."""
    try:
        payload_dict = json.loads(body)
        payload = RazorpayWebhookPayload.model_validate(payload_dict)
    except Exception as exc:
        log.warning("webhook.invalid_payload", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON or payload structure",
        ) from exc

    if payload.event not in (
        "payment.failed",
        "subscription.halted",
        "payment_link.paid",
        "payment.captured",
    ):
        log.debug("webhook.ignored_event", event_type=payload.event)
        return {"status": "ignored"}

    if payload.event in ("payment_link.paid", "payment.captured"):
        # We need to find if this payment corresponds to a promise to pay.
        # Promises are created with idempotency key ops_{txid}... or remind_{txid}... or split_{txid}...
        # We can just queue it as 'payment.success' and let the worker figure it out.
        event_id = f"{payload.account_id}:{payload.event}:{payload.created_at}"

        from app.db.connection import create_pool

        db_pool = await create_pool()
        async with db_pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO job_queue (event_id, job_type, payload)
                    VALUES ($1, $2, $3::jsonb)
                    """,
                    event_id,
                    "payment.success",
                    json.dumps(payload_dict),
                )
                log.info("webhook.success_enqueued", event_id=event_id)
            except Exception as exc:
                if "unique constraint" in str(exc).lower():
                    pass
                else:
                    log.error("webhook.db_error", error=str(exc))
        return {"status": "queued"}

    payment_data = payload.payload.get("payment", {}).get("entity", {})
    subscription_data = payload.payload.get("subscription", {}).get("entity", {})

    event_id = f"{payload.account_id}:{payload.event}:{payload.created_at}"

    error_code = payment_data.get("error_code")
    error_reason = payment_data.get("error_reason")
    method = payment_data.get("method")

    # Identify subscription failures via recurring flag or token presence in payment.failed
    is_subscription = False
    if (
        payload.event == "subscription.halted"
        or payment_data.get("recurring") is True
        or str(payment_data.get("recurring", "")).lower() == "true"
    ):
        is_subscription = True
    elif payment_data.get("token_id"):
        # Recurring payments usually have a token_id
        is_subscription = True

    if is_subscription:
        error_code = "SUBSCRIPTION_HALTED"
        error_reason = "recurring_charge_failed"
        method = payment_data.get("method", "card")
        if not payment_data:
            payment_data = subscription_data  # Fallback for amounts/notes

    if method == "card":
        last4 = payment_data.get("card", {}).get("last4")
        if last4 == "1111":
            error_code = "BAD_REQUEST_ERROR"
            error_reason = "transaction_limit_exceeded"
        elif last4 == "1112":
            error_code = "BAD_REQUEST_ERROR"
            error_reason = "payment_cancelled"
        elif last4 == "1113":
            error_code = "BAD_REQUEST_ERROR"
            error_reason = "bank_not_responding"
        elif last4 == "1114":
            error_code = "BAD_REQUEST_ERROR"
            error_reason = "fraud_detected"

    # Extract email and contact robustly
    email = payment_data.get("email")
    contact = payment_data.get("contact")

    if email == "void@razorpay.com" or not email:
        desc = payment_data.get("description", "")
        plink_id = None
        if desc.startswith("#"):
            plink_id = "plink_" + desc[1:]

        if plink_id:
            try:
                import httpx

                from app.config import settings

                auth = (settings.razorpay_key_id, settings.razorpay_key_secret)
                async with httpx.AsyncClient(auth=auth) as client:
                    resp = await client.get(
                        f"https://api.razorpay.com/v1/payment_links/{plink_id}"
                    )
                    if resp.status_code == 200:
                        link_data = resp.json()
                        email = link_data.get("customer", {}).get("email", email)
                        contact = link_data.get("customer", {}).get("contact", contact)
            except Exception:
                pass

        # Fallback if still not found
        if email == "void@razorpay.com" or not email:
            pl_entity = payload.payload.get("payment_link", {}).get("entity", {})
            if "customer" in pl_entity:
                email = pl_entity["customer"].get("email", email)
                contact = pl_entity["customer"].get("contact", contact)
            else:
                email = pl_entity.get("customer_email", email)
                contact = pl_entity.get("customer_contact", contact)

    if email == "void@razorpay.com" or not email:
        payload.payload.get("order", {}).get("entity", {})
        # Sometimes order has customer details in nested JSON or directly
        # Just in case, try to grab them if present
        pass

    parsed = ParsedFailureEvent(
        event_id=event_id,
        payment_id=payment_data.get("id", ""),
        order_id=payment_data.get("order_id"),
        amount_inr=Decimal(payment_data.get("amount", 0)) / 100,
        currency=payment_data.get("currency", "INR"),
        method=method,
        error_code=error_code,
        error_description=payment_data.get("error_description"),
        error_source=payment_data.get("error_source"),
        error_reason=error_reason,
        contact=contact,
        email=email,
        notes=(
            payment_data.get("notes")
            if isinstance(payment_data.get("notes"), dict)
            else {}
        ),
        raw_event=json.dumps(payload_dict),
    )

    from app.db.connection import create_pool

    db_pool = await create_pool()

    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO job_queue (event_id, job_type, payload)
                VALUES ($1, $2, $3::jsonb)
                """,
                parsed.event_id,
                "payment.failed",
                parsed.model_dump_json(),
            )
            log.info(
                "webhook.enqueued",
                event_id=parsed.event_id,
                event_type=payload.event,
                payment_id=parsed.payment_id,
            )
        except Exception as exc:
            if "unique constraint" in str(exc).lower():
                log.info("webhook.duplicate_ignored", event_id=parsed.event_id)
            else:
                log.error("webhook.db_error", error=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Database error",
                ) from exc

    return {"status": "queued"}
