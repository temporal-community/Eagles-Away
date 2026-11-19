import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cpfc_trip.persistence.models import RateLimitHit


@dataclass(frozen=True)
class Hit:
    count: int
    retry_after: int


class SharedRateLimiter:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sessions = sessions
        self.clock = clock

    async def hit(self, key: str, window_seconds: int) -> Hit:
        now = self.clock()
        start = int(now) // window_seconds * window_seconds
        async with self.sessions() as db:
            dialect = db.get_bind().dialect.name
            insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
            statement = insert(RateLimitHit).values(key=key, window_start=start, hits=1)
            count = (
                await db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[RateLimitHit.key, RateLimitHit.window_start],
                        set_={"hits": RateLimitHit.hits + 1},
                    ).returning(RateLimitHit.hits)
                )
            ).scalar_one()
            await db.commit()
        return Hit(count=count, retry_after=max(1, math.ceil(start + window_seconds - now)))
