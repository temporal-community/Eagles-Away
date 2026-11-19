"""Planning sessions are kept for at most 30 days in the application database."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cpfc_trip.persistence.models import RateLimitHit, SessionRow
from cpfc_trip.persistence.retention import prune_expired


async def test_prune_expired_removes_old_sessions(repository) -> None:
    """Retain recent plans and remove sessions outside the retention window."""
    now = datetime(2026, 10, 9, tzinfo=UTC)
    async with repository.sessions() as db:
        for sid, age in (("old", 31), ("recent", 29)):
            db.add(
                SessionRow(
                    id=sid,
                    submission_id=sid,
                    request_hash="r",
                    access_hash="a",
                    created_at=now - timedelta(days=age),
                    workflow_input={},
                )
            )
        await db.commit()

    assert await prune_expired(repository.sessions, now=now) == 1
    async with repository.sessions() as db:
        assert list(await db.scalars(select(SessionRow.id))) == ["recent"]


async def test_scheduled_prune_removes_old_ip_buckets_without_new_submissions(repository) -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    expired = int((now - timedelta(hours=1)).timestamp())
    current = int((now - timedelta(minutes=10)).timestamp())
    async with repository.sessions.begin() as db:
        db.add_all(
            (
                RateLimitHit(key="create:203.0.113.9", window_start=expired, hits=1),
                RateLimitHit(key="create:2001:db8::/64", window_start=current, hits=2),
                RateLimitHit(key="create:all", window_start=expired, hits=3),
            )
        )

    assert await prune_expired(repository.sessions, now=now) == 0
    async with repository.sessions() as db:
        assert list(await db.scalars(select(RateLimitHit.key))) == ["create:2001:db8::/64"]
