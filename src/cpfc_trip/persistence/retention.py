"""Delete expired personal data from the application database."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cpfc_trip.config import Settings
from cpfc_trip.persistence.database import engine
from cpfc_trip.persistence.models import RateLimitHit, SessionRow

RETENTION_DAYS = 30
RATE_LIMIT_RETENTION = timedelta(hours=1)


async def prune_expired(
    sessions: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    checked = now or datetime.now(UTC)
    cutoff = checked - timedelta(days=RETENTION_DAYS)
    rate_limit_cutoff = int((checked - RATE_LIMIT_RETENTION).timestamp())
    async with sessions.begin() as db:
        session_rows = await db.execute(delete(SessionRow).where(SessionRow.created_at < cutoff))
        await db.execute(delete(RateLimitHit).where(RateLimitHit.window_start <= rate_limit_cutoff))
    return cast(CursorResult[Any], session_rows).rowcount


async def _run() -> None:
    database = engine(Settings())
    try:
        count = await prune_expired(async_sessionmaker(database))
        print(f"Expired sessions deleted: {count}")
    finally:
        await database.dispose()


def main() -> None:
    asyncio.run(_run())
