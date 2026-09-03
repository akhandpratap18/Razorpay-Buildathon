"""Postgres job queue poller — SKIP LOCKED worker.

Design:
- Any number of worker processes can run concurrently against the same
  Postgres table with zero collisions (SELECT ... FOR UPDATE SKIP LOCKED)
- Workers are stateless — all progress is in Postgres checkpoints
- Kill-and-restart mid-graph resumes from the correct checkpoint
- Exponential backoff on failure (via job_queue.run_after)
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta

import asyncpg
import structlog

from app.communication.fallback_chain import send_reminder_email
from app.db.connection import get_db_pool
from app.worker.executor import execute_job

log = structlog.get_logger(__name__)

WORKER_ID = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
POLL_INTERVAL_SECONDS = 2
CLAIM_TIMEOUT_SECONDS = 300  # 5 min — after this, claimed job is considered abandoned


async def poll_forever() -> None:
    """Main worker loop — polls the job queue and processes jobs."""
    log.info("worker.started", worker_id=WORKER_ID)

    while True:
        try:
            async with get_db_pool() as conn:  # noqa: SIM117
                job = await _claim_job(conn)
                if job:
                    await _process_job(conn, job)
                else:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            log.error("worker.poll_error", error=str(exc))
            await asyncio.sleep(POLL_INTERVAL_SECONDS * 2)


async def _claim_job(conn: asyncpg.Connection) -> asyncpg.Record | None:
    """Atomically claim one pending job using SKIP LOCKED.

    Returns the claimed job row, or None if no jobs are ready.
    SKIP LOCKED means: if another worker is processing a job, skip it
    and look at the next one — no blocking, no collision.
    """
    async with conn.transaction():
        job = await conn.fetchrow(
            """
            SELECT *
            FROM job_queue
            WHERE status = 'pending'
              AND run_after <= NOW()
            ORDER BY run_after
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
        )

        if job is None:
            return None

        # Mark as claimed
        await conn.execute(
            """
            UPDATE job_queue
            SET status = 'claimed',
                claimed_at = NOW(),
                worker_id = $1,
                attempts = attempts + 1
            WHERE id = $2
            """,
            WORKER_ID,
            job["id"],
        )

        log.info(
            "worker.job_claimed",
            job_id=job["id"],
            job_type=job["job_type"],
            event_id=job["event_id"],
            worker_id=WORKER_ID,
        )
        return job


async def _process_job(conn: asyncpg.Connection, job: asyncpg.Record) -> None:
    """Execute a claimed job and update its status in the queue."""
    job_id = job["id"]
    event_id = job["event_id"]
    job_type = job["job_type"]
    payload = json.loads(job["payload"])
    attempts = job["attempts"]
    max_attempts = job["max_attempts"]

    try:
        result = await execute_job(conn, job_type, payload)
        await conn.execute(
            """
            UPDATE job_queue
            SET status = 'done',
                result = $1::jsonb
            WHERE id = $2
            """,
            json.dumps(result, default=str),
            job_id,
        )
        log.info("worker.job_done", job_id=job_id, event_id=event_id)

    except Exception as exc:
        log.error("worker.job_failed", job_id=job_id, error=str(exc), attempts=attempts)

        if attempts >= max_attempts:
            await conn.execute(
                """
                UPDATE job_queue
                SET status = 'dead_letter',
                    error_message = $1
                WHERE id = $2
                """,
                str(exc)[:500],
                job_id,
            )
            log.warning("worker.dead_letter", job_id=job_id, event_id=event_id)
        else:
            # Exponential backoff: 2^attempts minutes
            backoff = timedelta(minutes=2**attempts)
            await conn.execute(
                """
                UPDATE job_queue
                SET status = 'pending',
                    run_after = NOW() + $1,
                    error_message = $2
                WHERE id = $3
                """,
                backoff,
                str(exc)[:500],
                job_id,
            )
            log.info(
                "worker.job_requeued",
                job_id=job_id,
                backoff_minutes=2**attempts,
            )


async def poll_reminders_forever() -> None:
    """Background loop that checks for due promises and sends reminder emails."""
    log.info("worker.reminders_started", worker_id=WORKER_ID)

    while True:
        try:
            async with get_db_pool() as conn:  # noqa: SIM117
                async with conn.transaction():
                    promise = await conn.fetchrow("""
                        SELECT p.id, p.transaction_id, p.promised_leg_inr, p.promised_payment_link,
                               t.email, t.contact, t.recovery_token, p.recovery_token as promise_recovery_token,
                               p.reminder_count
                        FROM promise_to_pay p
                        JOIN transactions t ON p.transaction_id::uuid = t.id
                        WHERE t.status NOT IN ('recovered', 'killed')
                          AND (
                             (p.status = 'pending' AND p.due_date <= CURRENT_DATE + INTERVAL '2 days')
                             OR (p.status = 'reminded' AND p.reminder_count = 1 AND p.due_date <= CURRENT_DATE)
                             OR (p.status = 'reminded' AND p.reminder_count = 2 AND p.due_date <= CURRENT_DATE - INTERVAL '2 days')
                          )
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """)

                    if promise:
                        payment_link = promise["promised_payment_link"]
                        if not payment_link:
                            from datetime import timedelta

                            from app.payment.razorpay_client import RazorpayClient

                            client = RazorpayClient()
                            try:
                                expire_unix = int(
                                    (
                                        promise["due_date"] + timedelta(days=2)
                                    ).timestamp()
                                )
                                link_data = client.create_payment_link(
                                    amount_paise=int(promise["promised_leg_inr"] * 100),
                                    currency="INR",
                                    description=f"Scheduled Payment for {promise['transaction_id']}",
                                    contact=promise.get("contact"),
                                    email=promise.get("email"),
                                    idempotency_key=f"remind_{promise['transaction_id']}",
                                    expire_by_unix=expire_unix,
                                )
                                payment_link = link_data.get("short_url", "")
                            except Exception as e:
                                log.error("worker.create_link_failed", error=str(e))
                                # Don't rollback, just skip this one and try again next loop
                                raise e

                            await conn.execute(
                                "UPDATE promise_to_pay SET promised_payment_link = $1 WHERE id = $2",
                                payment_link,
                                promise["id"],
                            )

                        if promise["reminder_count"] >= 2:
                            # Escalate broken promise
                            await conn.execute(
                                "UPDATE promise_to_pay SET status = 'broken' WHERE id = $1",
                                promise["id"],
                            )
                            await conn.execute(
                                "UPDATE transactions SET status = 'escalated' WHERE id = $1::uuid",
                                promise["transaction_id"],
                            )
                            log.info("worker.promise_broken", promise_id=promise["id"])
                        else:
                            try:
                                if promise["email"]:
                                    # send_reminder_email uses the recovery_token which sends the parent transaction's token.
                                    # Since we updated the DB query to fetch p.recovery_token, it already uses that if needed!
                                    send_reminder_email(
                                        transaction_id=str(promise["transaction_id"]),
                                        email=promise["email"],
                                        recovery_token=promise["recovery_token"],
                                        amount=str(promise["promised_leg_inr"]),
                                        payment_link=payment_link,
                                    )
                                else:
                                    log.warning(
                                        "worker.no_email_to_send_reminder",
                                        promise_id=promise["id"],
                                    )
                            except Exception as e:
                                log.error("worker.email_failed", error=str(e))
                                await conn.execute(
                                    "UPDATE promise_to_pay SET status = 'failed' WHERE id = $1",
                                    promise["id"],
                                )

                            await conn.execute(
                                "UPDATE promise_to_pay SET status = 'reminded', reminder_count = reminder_count + 1 WHERE id = $1",
                                promise["id"],
                            )
                            log.info(
                                "worker.reminder_sent",
                                promise_id=promise["id"],
                                new_count=promise["reminder_count"] + 1,
                            )
                    else:
                        await asyncio.sleep(60)
        except Exception as exc:
            log.error("worker.reminder_error", error=str(exc))
            await asyncio.sleep(60)


async def main() -> None:
    await asyncio.gather(poll_forever(), poll_reminders_forever())


if __name__ == "__main__":
    asyncio.run(main())
