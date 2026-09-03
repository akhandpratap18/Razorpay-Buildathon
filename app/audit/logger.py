"""Hash-chained audit log writer.

Every state transition and message is logged here.
row_hash = sha256(prev_hash || row_json) — tamper-evident chain.

If any row is modified after the fact, all subsequent hashes become invalid,
making tampering detectable on inspection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def compute_row_hash(prev_hash: str | None, payload: dict[str, Any]) -> str:
    """Compute sha256(prev_hash + row_json) for the hash chain.

    Args:
        prev_hash: Hash of the previous audit row (None for the first row).
        payload: The event payload dict (will be serialised to JSON).

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    prev = prev_hash or "GENESIS"
    row_json = json.dumps(payload, sort_keys=True, default=str)
    raw = f"{prev}{row_json}".encode()
    return hashlib.sha256(raw).hexdigest()


class AuditLogger:
    """Writes hash-chained audit entries to the Supabase audit_log table."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def log_event(
        self,
        transaction_id: str | None,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> str:
        """Write one audit entry and return its row hash."""
        # Mask PII before logging
        safe_payload = _mask_pii(payload)

        # Get the last hash in the chain for this transaction (only when there's a transaction)
        prev_hash: str | None = None
        if transaction_id:
            prev_row = await self._conn.fetchrow(
                """
                SELECT row_hash FROM audit_log
                WHERE transaction_id = $1::uuid
                ORDER BY id DESC
                LIMIT 1
                """,
                transaction_id,
            )
            prev_hash = prev_row["row_hash"] if prev_row else None

        row_hash = compute_row_hash(prev_hash, safe_payload)

        await self._conn.execute(
            """
            INSERT INTO audit_log
                (transaction_id, event_type, actor, payload, prev_hash, row_hash)
            VALUES ($1::uuid, $2, $3, $4::jsonb, $5, $6)
            """,
            transaction_id,
            event_type,
            actor,
            json.dumps(safe_payload, default=str),
            prev_hash,
            row_hash,
        )

        log.info(
            "audit.logged",
            transaction_id=transaction_id,
            event_type=event_type,
            actor=actor,
        )
        return row_hash


def _mask_pii(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask known PII fields in audit payloads.

    Full PII is accessible only via a separate audited lookup path.
    """
    masked = dict(payload)
    pii_fields = {"contact", "email", "phone", "card_number", "cvv", "pan"}
    for field in pii_fields:
        if field in masked and masked[field]:
            raw = str(masked[field])
            masked[field] = raw[:3] + "****" + raw[-2:] if len(raw) > 5 else "****"
    return masked


async def verify_hash_chain(conn: Any, transaction_id: str) -> bool:
    """Verify the hash chain is unbroken for a given transaction.

    Returns True if all hashes are consistent, False if tampering is detected.
    """
    rows = await conn.fetch(
        """
        SELECT id, payload, prev_hash, row_hash
        FROM audit_log
        WHERE transaction_id = $1::uuid
        ORDER BY id ASC
        """,
        transaction_id,
    )

    if not rows:
        return True  # No entries — nothing to verify

    prev_hash: str | None = None
    for row in rows:
        payload = json.loads(row["payload"])
        expected_hash = compute_row_hash(prev_hash, payload)
        if expected_hash != row["row_hash"]:
            log.error(
                "audit.chain_broken",
                row_id=row["id"],
                expected=expected_hash,
                actual=row["row_hash"],
            )
            return False
        prev_hash = row["row_hash"]

    return True
