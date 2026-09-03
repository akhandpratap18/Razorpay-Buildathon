from typing import Any

import asyncpg


async def compute_recovery_metrics(conn: asyncpg.Connection) -> dict[str, Any]:
    total_tx = await conn.fetchval("SELECT COUNT(*) FROM transactions")

    # Group by status
    status_rows = await conn.fetch(
        "SELECT status, COUNT(*) as count FROM transactions GROUP BY status"
    )
    statuses = {r["status"]: r["count"] for r in status_rows}

    # Group by category
    cat_rows = await conn.fetch(
        "SELECT category, COUNT(*) as count FROM transactions GROUP BY category"
    )
    categories = {r["category"] or "unclassified": r["count"] for r in cat_rows}

    # Amount recovered
    amount_recovered_full = await conn.fetchval(
        "SELECT COALESCE(SUM(amount_inr), 0) FROM transactions WHERE status = 'recovered'"
    )
    # FIX #1: only count actually paid promises!
    amount_recovered_split = await conn.fetchval(
        "SELECT COALESCE(SUM(immediate_leg_inr), 0) FROM promise_to_pay WHERE status = 'paid'"
    )
    total_recovered_inr = float(amount_recovered_full) + float(amount_recovered_split)

    # Amount pending
    amount_pending_split = await conn.fetchval(
        "SELECT COALESCE(SUM(promised_leg_inr), 0) FROM promise_to_pay WHERE status IN ('pending', 'partially_paid', 'reminded')"
    )
    amount_pending_onetime = await conn.fetchval(
        "SELECT COALESCE(SUM(amount_inr), 0) FROM transactions WHERE recovery_link_url IS NOT NULL AND status IN ('pending', 'processing')"
    )
    amount_pending = float(amount_pending_split) + float(amount_pending_onetime)

    # Amount at risk (escalated/failed/killed)
    amount_at_risk = await conn.fetchval(
        "SELECT COALESCE(SUM(amount_inr), 0) FROM transactions WHERE status IN ('escalated', 'failed', 'killed')"
    )

    return {
        "total_transactions": total_tx,
        "statuses": statuses,
        "categories": categories,
        "total_recovered_inr": total_recovered_inr,
        "amount_pending": amount_pending,
        "amount_at_risk": float(amount_at_risk),
    }
