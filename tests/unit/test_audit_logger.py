import pytest

from app.audit.logger import AuditLogger, _mask_pii, compute_row_hash, verify_hash_chain


class MockConn:
    def __init__(self, fetch_result=None):
        self.fetch_result = fetch_result

    async def fetchrow(self, *args, **kwargs):
        if self.fetch_result and len(self.fetch_result) > 0:
            return self.fetch_result[-1]
        return None

    async def fetch(self, *args, **kwargs):
        return self.fetch_result or []

    async def execute(self, *args, **kwargs):
        pass


def test_audit_logger_masks_pii():
    payload = {"email": "test@example.com", "phone": "+1234567890", "amount": 500}
    masked = _mask_pii(payload)

    assert masked["email"] == "tes****om"
    assert masked["phone"] == "+12****90"
    assert masked["amount"] == 500


def test_audit_logger_hash_chain():
    prev_hash = "abc123hash"
    payload = {"test": 123}

    new_hash = compute_row_hash(prev_hash, payload)
    assert new_hash is not None
    assert isinstance(new_hash, str)


@pytest.mark.asyncio
async def test_audit_logger_log_event():
    conn = MockConn([{"row_hash": "prev"}])
    logger = AuditLogger(conn)
    result = await logger.log_event("tx_1", "test_event", "test_actor", {"a": 1})
    assert result is not None


@pytest.mark.asyncio
async def test_verify_hash_chain():
    payload = {"a": 1}
    h1 = compute_row_hash(None, payload)
    h2 = compute_row_hash(h1, payload)

    rows = [
        {"id": 1, "payload": '{"a": 1}', "prev_hash": None, "row_hash": h1},
        {"id": 2, "payload": '{"a": 1}', "prev_hash": h1, "row_hash": h2},
    ]

    conn = MockConn(rows)
    is_valid = await verify_hash_chain(conn, "tx_1")
    assert is_valid


@pytest.mark.asyncio
async def test_verify_hash_chain_tampered():
    payload = {"a": 1}
    h1 = compute_row_hash(None, payload)

    rows = [
        {"id": 1, "payload": '{"a": 2}', "prev_hash": None, "row_hash": h1},
    ]

    conn = MockConn(rows)
    is_valid = await verify_hash_chain(conn, "tx_1")
    assert not is_valid
