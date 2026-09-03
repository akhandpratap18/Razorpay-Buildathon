"""Unit tests for HMAC-SHA256 webhook signature verification.

Tests the verify_razorpay_signature function directly using mock Requests,
avoiding any DB/network dependency.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.webhook.verification import verify_razorpay_signature

WEBHOOK_SECRET = "test_secret_abc123"
SAMPLE_BODY = b'{"entity":"event","event":"payment.failed"}'


def _make_sig(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _mock_request(
    body: bytes, sig_header: str | None, monkeypatch: pytest.MonkeyPatch
) -> MagicMock:
    """Build a mock FastAPI Request with the given body and signature header."""
    monkeypatch.setattr(
        "app.webhook.verification.settings.razorpay_webhook_secret", WEBHOOK_SECRET
    )
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    headers: dict[str, str] = {}
    if sig_header is not None:
        headers["X-Razorpay-Signature"] = sig_header
    req.headers = headers
    return req


class TestWebhookSignatureVerification:
    @pytest.mark.asyncio
    async def test_valid_signature_returns_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sig = _make_sig(SAMPLE_BODY)
        req = _mock_request(SAMPLE_BODY, sig, monkeypatch)
        result = await verify_razorpay_signature(req)
        assert result == SAMPLE_BODY

    @pytest.mark.asyncio
    async def test_missing_signature_raises_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _mock_request(SAMPLE_BODY, None, monkeypatch)
        with pytest.raises(HTTPException) as exc_info:
            await verify_razorpay_signature(req)
        assert exc_info.value.status_code == 400
        assert "signature" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_wrong_signature_raises_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _mock_request(SAMPLE_BODY, "deadbeef" * 8, monkeypatch)
        with pytest.raises(HTTPException) as exc_info:
            await verify_razorpay_signature(req)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_tampered_body_raises_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Original sig + tampered body must be rejected — 100% of the time."""
        original_sig = _make_sig(SAMPLE_BODY)
        tampered = SAMPLE_BODY + b"INJECTED_PAYLOAD"
        req = _mock_request(tampered, original_sig, monkeypatch)
        with pytest.raises(HTTPException) as exc_info:
            await verify_razorpay_signature(req)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_body_with_correct_sig_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = b""
        sig = _make_sig(empty)
        req = _mock_request(empty, sig, monkeypatch)
        result = await verify_razorpay_signature(req)
        assert result == empty

    @pytest.mark.parametrize(
        "bad_sig",
        [
            "a" * 64,
            "0" * 64,
            "abc",
            "not-a-hex-string!!",
        ],
    )
    @pytest.mark.asyncio
    async def test_various_bad_sigs_all_rejected(
        self, bad_sig: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = _mock_request(SAMPLE_BODY, bad_sig, monkeypatch)
        with pytest.raises(HTTPException) as exc_info:
            await verify_razorpay_signature(req)
        assert exc_info.value.status_code == 400
