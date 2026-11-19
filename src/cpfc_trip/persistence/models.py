from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Float, Integer, String, false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(36), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    access_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    workflow_input: Mapped[dict[str, Any]] = mapped_column(JSON)


class SearchCacheRow(Base):
    __tablename__ = "search_cache"
    namespace: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetch_seconds: Mapped[float] = mapped_column(Float)
    is_negative: Mapped[bool] = mapped_column(server_default=false())


class RateLimitHit(Base):
    __tablename__ = "rate_limit_hits"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    hits: Mapped[int] = mapped_column(Integer)
