from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cpfc_trip.config import Settings


def engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    sslmode = url.query.get("sslmode")
    if isinstance(sslmode, str) and url.drivername == "postgresql+asyncpg":
        url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": sslmode})
    pool: dict[str, Any] = {}
    if url.get_backend_name() == "postgresql":
        pool = {
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_recycle": 1800,
        }
    return create_async_engine(url, pool_pre_ping=True, **pool)
