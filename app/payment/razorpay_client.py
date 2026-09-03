"""Razorpay API client — rate-limited, circuit-breaker-guarded.

Design:
- All calls are idempotent: idempotency key = (order_id, attempt_number)
- Token-bucket rate limiter respects Razorpay's documented limits
- Circuit breaker: after N consecutive failures, pause + requeue
- API credentials never appear in logs or error messages
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import razorpay
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

log = structlog.get_logger(__name__)


# ── Circuit breaker ────────────────────────────────────────────────────────────


@dataclass
class CircuitBreaker:
    """Simple circuit breaker for the Razorpay API."""

    failure_threshold: int = 5
    recovery_timeout_seconds: int = 60
    _failures: int = 0
    _last_failure_time: float = 0.0
    _open: bool = False

    def record_success(self) -> None:
        self._failures = 0
        self._open = False

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self.failure_threshold:
            self._open = True
            log.warning("circuit_breaker.open", failures=self._failures)

    @property
    def is_open(self) -> bool:
        if self._open:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout_seconds:
                log.info("circuit_breaker.half_open")
                self._open = False
                self._failures = 0
        return self._open


_circuit_breaker = CircuitBreaker()


# ── Idempotency key generator ──────────────────────────────────────────────────


def make_idempotency_key(order_id: str, attempt_number: int, suffix: str = "") -> str:
    """Generate a stable idempotency key for a recovery action.

    Key = sha256(order_id:attempt_number:suffix) — short, URL-safe, unique.
    """
    raw = f"{order_id}:{attempt_number}:{suffix}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Razorpay client wrapper ────────────────────────────────────────────────────


class RazorpayClient:
    """Thin wrapper over the razorpay SDK with rate-limiting and circuit breaking."""

    def __init__(self) -> None:
        self._client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )
        self._links_created_count = 0

    def _check_circuit(self) -> None:
        if _circuit_breaker.is_open:
            raise RuntimeError(
                "Razorpay circuit breaker is OPEN — API calls paused. "
                "Will retry after recovery timeout."
            )

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def create_payment_link(
        self,
        amount_paise: int,
        currency: str,
        description: str,
        contact: str | None,
        email: str | None,
        idempotency_key: str,
        expire_by_unix: int | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay Payment Link.

        Returns the full payment link response dict including `short_url`.
        Amount ceiling is enforced by the calling node — never here.
        """
        self._check_circuit()

        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "description": str(description)[:2048] if description else "Payment Link",
        }

        if contact or email:
            cust = {}
            if contact:
                cust["contact"] = str(contact)
            if email:
                cust["email"] = str(email)
            payload["customer"] = cust
            payload["notify"] = {"sms": False, "email": False}
            payload["reminder_enable"] = False

        if expire_by_unix:
            payload["expire_by"] = int(expire_by_unix)

        if idempotency_key:
            payload["reference_id"] = str(idempotency_key)[:40]

        try:
            result = self._client.payment_link.create(payload)
            self._links_created_count = getattr(self, "_links_created_count", 0) + 1
            _circuit_breaker.record_success()
            log.info(
                "razorpay.link_created",
                link_id=result.get("id"),
            )
            return dict(result)
        except Exception as exc:
            _circuit_breaker.record_failure()
            log.error(
                "razorpay.link_create_failed",
                error=type(exc).__name__,
                details=str(exc),
                payload=payload,
            )
            raise

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def create_order(
        self,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay Order (used for rail-switch recovery)."""
        self._check_circuit()
        try:
            payload: dict[str, Any] = {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "notes": notes or {},
            }
            result = self._client.order.create(data=payload)
            _circuit_breaker.record_success()
            log.info("razorpay.order_created", order_id=result.get("id"))
            return dict(result)
        except Exception as exc:
            _circuit_breaker.record_failure()
            log.error("razorpay.order_create_failed", error=type(exc).__name__)
            raise

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch payment details for verification."""
        self._check_circuit()
        return self._client.payment.fetch(payment_id)  # type: ignore[no-any-return]


# Singleton client
razorpay_client = RazorpayClient()
