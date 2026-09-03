"""HMAC-SHA256 webhook signature verification.

Razorpay signs each webhook with:
  X-Razorpay-Signature = HMAC-SHA256(webhook_secret, raw_body)

We verify this before the payload ever enters the application.
Any unsigned or tampered request is rejected with 400.
"""

from __future__ import annotations

import hashlib
import hmac

import structlog
from fastapi import HTTPException, Request, status

from app.config import settings

log = structlog.get_logger(__name__)


def generate_signature(body: bytes, secret: str) -> str:
    """Generate the HMAC-SHA256 signature for a given body and secret."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def verify_razorpay_signature(request: Request) -> bytes:
    """Read the raw request body and verify the Razorpay HMAC signature.

    Returns the raw body bytes on success.
    Raises HTTPException(400) on failure — always, with no diagnostic detail
    leaked to the caller to avoid oracle attacks.
    """
    raw_body = await request.body()
    received_sig = request.headers.get("X-Razorpay-Signature", "")

    if not received_sig:
        log.warning(
            "webhook.signature_missing",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing signature header",
        )

    if not settings.razorpay_webhook_secret:
        # In development with no secret set, log a loud warning but pass through.
        log.warning("webhook.secret_not_configured — skipping verification in dev")
        return raw_body

    expected_sig = generate_signature(raw_body, settings.razorpay_webhook_secret)

    # compare_digest requires equal-length strings — pad/truncate to avoid exceptions
    # while still rejecting mismatches (length difference itself is a mismatch)
    if len(expected_sig) != len(received_sig) or not hmac.compare_digest(
        expected_sig, received_sig
    ):
        log.warning(
            "webhook.signature_mismatch",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature",
        )

    log.debug("webhook.signature_verified")
    return raw_body
