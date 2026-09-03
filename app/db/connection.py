"""Supabase / Postgres async connection pool.

Uses asyncpg for direct, high-performance Postgres access.
The Supabase connection string is read from settings.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create and return a shared asyncpg connection pool."""
    global _pool
    if _pool is None:
        # Convert SQLAlchemy-style URL to raw DSN for asyncpg
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=10,
            command_timeout=60,
        )
        log.info("db.pool_created", dsn=dsn.split("@")[-1])  # log host only
    return _pool


async def close_pool() -> None:
    """Gracefully close the connection pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool_closed")


@asynccontextmanager
async def get_db_pool() -> AsyncGenerator[asyncpg.Connection, None]:
    """Context manager that yields a connection from the pool."""
    pool = await create_pool()
    async with pool.acquire() as conn:
        yield conn
