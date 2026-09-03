import asyncio

import asyncpg
import structlog

from app.db.connection import get_db_pool

log = structlog.get_logger(__name__)
CLAIM_TIMEOUT_SECONDS = 300


async def reap_stale_jobs(conn: asyncpg.Connection) -> None:
    """Release jobs stuck in 'processing' for too long."""
    result = await conn.execute(
        """
        UPDATE job_queue
        SET status = 'pending', worker_id = NULL
        WHERE status = 'claimed'
          AND updated_at < NOW() - INTERVAL '1 second' * $1
        """,
        CLAIM_TIMEOUT_SECONDS,
    )
    log.info("reaper.stale_jobs", result=result)


async def start_reaper_loop() -> None:
    while True:
        try:
            async with get_db_pool() as conn:
                await reap_stale_jobs(conn)
        except Exception as exc:
            log.error("reaper.loop_error", error=str(exc))
        await asyncio.sleep(60)
