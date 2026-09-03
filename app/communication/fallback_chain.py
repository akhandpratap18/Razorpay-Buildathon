"""Email-Only Outreach orchestrator — single unified template."""

from __future__ import annotations

import resend
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


def _send_email(to: str, subject: str, html_body: str, transaction_id: str) -> None:
    """Internal helper — sends via Resend with domain fallback."""
    if (
        not settings.resend_api_key
        or settings.resend_api_key.startswith("test_")
        or settings.resend_api_key == "invalid_key"
    ):
        log.info(
            "communication.mock_delivered",
            channel="email",
            transaction_id=transaction_id,
            note="MOCK EMAIL SENT",
        )
        return

    resend.api_key = settings.resend_api_key

    def _attempt(from_addr: str) -> None:
        resend.Emails.send(
            {"from": from_addr, "to": to, "subject": subject, "html": html_body}
        )

    try:
        _attempt(settings.resend_from_email)
        log.info(
            "communication.delivered", channel="email", transaction_id=transaction_id
        )
    except Exception as exc:
        err = str(exc).lower()
        if "verify a domain" in err or "own email address" in err:
            try:
                _attempt("onboarding@resend.dev")
                log.info(
                    "communication.delivered",
                    channel="email",
                    transaction_id=transaction_id,
                    note="fallback onboarding@resend.dev",
                )
            except Exception as e2:
                log.warning("communication.email_failed_swallowed", error=str(e2))
        else:
            log.warning("communication.email_failed_swallowed", error=str(exc))


def send_reminder_email(
    transaction_id: str,
    email: str,
    recovery_token: str,
    amount: str,
    payment_link: str,
    is_urgent: bool = False,
) -> None:
    """Unified outreach email — always includes both a Pay Now button and an Open Support Chat button."""
    chat_url = (
        f"{settings.frontend_url}/recoup_customer_chat.html?token={recovery_token}"
    )

    if is_urgent:
        subject = f"URGENT: Action Required for your pending payment of ₹{amount}"
        header_html = '<h2 style="color: #dc3545;">URGENT: Payment Escalated</h2>'
        body_html = (
            f"<p>Hi there,</p>"
            f"<p>Your previously scheduled payment of ₹{amount} has not been fulfilled "
            f"and the promise to pay was broken. This transaction has been escalated.</p>"
            f"<p>Please resolve this immediately to avoid further actions on your account.</p>"
        )
        btn_color = "#dc3545"
    elif payment_link:
        subject = f"Action Required: Complete your payment of ₹{amount}"
        header_html = "<h2>Payment Update</h2>"
        body_html = (
            f"<p>Hi there,</p>"
            f"<p>Your payment of ₹{amount} is due. Use the button below to pay securely, "
            f"or open the support chat if you need help.</p>"
        )
        btn_color = "#10b981"
    else:
        subject = "Action Required: Complete your pending payment"
        header_html = "<h2>Payment Update</h2>"
        body_html = (
            "<p>Hi there,</p>"
            "<p>It looks like your payment didn't go through. "
            "Our support assistant can help you resolve this quickly.</p>"
        )
        btn_color = "#007bff"

    # Pay Now button — only when a Razorpay link is available
    pay_btn = (
        f"""
        <div style="text-align: center; margin: 25px 0;">
            <a href="{payment_link}"
               style="display: inline-block; padding: 14px 28px; background-color: {btn_color};
                      color: white; text-decoration: none; border-radius: 6px;
                      font-weight: bold; font-size: 16px;">
                Pay Now (₹{amount})
            </a>
        </div>"""
        if payment_link
        else ""
    )

    html_body = f"""
    <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto; color: #333;
                border: 1px solid #eaeaea; border-radius: 8px; padding: 24px;">
        {header_html}
        {body_html}
        {pay_btn}
        <p style="margin-top: 20px; color: #666;">
            Need to speak with our support assistant? Click below:
        </p>
        <div style="margin: 12px 0;">
            <a href="{chat_url}"
               style="display: inline-block; padding: 10px 20px; background-color: #6c757d;
                      color: white; text-decoration: none; border-radius: 4px; font-size: 14px;
                      font-weight: 600;">
                Open Support Chat
            </a>
        </div>
        <hr style="border: none; border-top: 1px solid #eaeaea; margin-top: 30px;" />
        <p style="color: #999; font-size: 12px; text-align: center;">
            <a href="{chat_url}/unsubscribe" style="color: #999; text-decoration: underline;">
                Unsubscribe
            </a>
        </p>
    </div>
    """

    _send_email(email, subject, html_body, transaction_id)


def send_recovery_email(transaction_id: str, email: str, recovery_token: str) -> None:
    """Backwards-compat wrapper — sends the unified template without a payment link."""
    send_reminder_email(
        transaction_id=transaction_id,
        email=email,
        recovery_token=recovery_token,
        amount="",
        payment_link="",
    )
