"""Shared cache of normalized provider results, split by search domain.

Freshness is decided when reading: rows record when they were stored and how long the live
search took, and the configured policy applies to existing rows immediately.

- ``ttl``: how long a row is fresh. Zero disables the cache.
- ``stale_if_error``: extra time a row may still be served when the live search fails.
- ``early_beta``: probabilistic early expiration ("XFetch", Vattani et al., VLDB 2015).
  Each read treats the row as expired ``fetch_seconds * early_beta * -ln(U)`` early, so
  routes that are read often, or are slow to search, refresh before they expire. The mean
  lead time is ``fetch_seconds * early_beta``. Zero disables early expiration.
"""

import hashlib
import json
import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import and_, delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cpfc_trip.domain import Leg, SearchBatch, SearchSpec, Stay
from cpfc_trip.persistence.models import SearchCacheRow

SearchMode = Literal["flight", "rail", "stay"]
DEFAULT_TTL = timedelta(hours=24)
STALE_GAP = "The live search failed, so these results are from an earlier check."
NEGATIVE_CACHE_TTL = timedelta(hours=1)
CACHEABLE_FAILURES = frozenset(
    {"provider_empty", "no_timely_flight", "unverified", "unsupported_connection"}
)
ROUTE_SEARCH_FIELDS = frozenset(
    {
        "mode",
        "origin",
        "destination",
        "destination_timezone",
        "onward_stations",
        "minimum_transfer_minutes",
        "gateway_arrival_hours",
        "gateway_departure_hours",
    }
)


def cache_identity(spec: SearchSpec, mode: SearchMode) -> tuple[str, str]:
    """Return separate hotel or transport namespace and a request-safe key.

    The route key includes the exact party because flight and rail prices depend on it.
    Hotel results can be shared across route candidates for the same city and stay.
    """
    common = (str(spec.outbound_date), str(spec.return_date), spec.party.model_dump(mode="json"))
    fields: tuple[object, ...]
    if mode == "stay":
        namespace = "hotel"
        fields = (
            "hotel-v2",
            spec.fixture.city.casefold(),
            spec.fixture.venue_coordinates.model_dump(mode="json")
            if spec.fixture.venue_coordinates
            else None,
            *common,
            spec.budget_tier,
            spec.private_room,
            spec.private_bathroom,
        )
    else:
        namespace = "transport"
        fields = (
            "transport-v2",
            spec.fixture.id,
            spec.fixture.kickoff_at.isoformat(),
            spec.route.model_dump(mode="json", include=set(ROUTE_SEARCH_FIELDS)),
            spec.latest_return.isoformat() if spec.latest_return else None,
            mode,
            *common,
        )
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return namespace, hashlib.sha256(payload.encode()).hexdigest()


def for_route(batch: SearchBatch, spec: SearchSpec) -> SearchBatch:
    journeys = tuple(
        journey.model_copy(update={"route_id": spec.route.id}) for journey in batch.journeys
    )
    return batch.model_copy(update={"journeys": journeys})


def _mark_stale[T: (Leg, Stay)](item: T) -> T:
    return item.model_copy(update={"caveats": (*item.caveats, STALE_GAP)})


class SearchCache:
    def __init__(
        self,
        database: AsyncEngine,
        ttl: timedelta = DEFAULT_TTL,
        *,
        stale_if_error: timedelta = timedelta(0),
        early_beta: float = 0.0,
        uniform: Callable[[], float] = random.random,
    ) -> None:
        self.sessions = async_sessionmaker(database, expire_on_commit=False)
        self.ttl = ttl
        self.stale_if_error = stale_if_error
        self.early_beta = early_beta
        self.uniform = uniform

    @property
    def _negative_ttl(self) -> timedelta:
        return min(self.ttl, NEGATIVE_CACHE_TTL)

    async def _row(
        self, spec: SearchSpec, mode: SearchMode, checked: datetime
    ) -> tuple[SearchBatch, timedelta, float, bool] | None:
        if self.ttl <= timedelta(0):
            return None
        namespace, key = cache_identity(spec, mode)
        async with self.sessions() as session:
            row = await session.get(SearchCacheRow, (namespace, key))
        if row is None:
            return None
        stored_at = row.stored_at
        if stored_at.tzinfo is None:
            stored_at = stored_at.replace(tzinfo=UTC)
        try:
            batch = SearchBatch.model_validate(row.payload, extra="ignore")
        except ValidationError:
            return None
        return (
            for_route(batch, spec).model_copy(update={"calls": 0}),
            checked - stored_at,
            row.fetch_seconds,
            row.is_negative,
        )

    async def get(
        self, spec: SearchSpec, mode: SearchMode, *, now: datetime | None = None
    ) -> SearchBatch | None:
        """Return a fresh batch with zero new provider calls, or ``None`` to search live."""
        found = await self._row(spec, mode, now or datetime.now(UTC))
        if found is None:
            return None
        batch, age, fetch_seconds, negative = found
        if negative:
            return batch if age < self._negative_ttl else None
        # 1 - U lies in (0, 1], so the logarithm is finite.
        early = fetch_seconds * self.early_beta * -math.log(1 - self.uniform())
        return batch if age + timedelta(seconds=early) < self.ttl else None

    async def stale(
        self, spec: SearchSpec, mode: SearchMode, *, now: datetime | None = None
    ) -> SearchBatch | None:
        """Return a batch within ``ttl + stale_if_error`` after a failed live search."""
        found = await self._row(spec, mode, now or datetime.now(UTC))
        if found is None:
            return None
        batch, age, _, negative = found
        if negative or age >= self.ttl + self.stale_if_error:
            return None
        return batch.model_copy(
            update={
                "stale": True,
                "gaps": (*batch.gaps, STALE_GAP),
                "journeys": tuple(
                    journey.model_copy(
                        update={
                            "outbound": tuple(map(_mark_stale, journey.outbound)),
                            "inbound": tuple(map(_mark_stale, journey.inbound)),
                        }
                    )
                    for journey in batch.journeys
                ),
                "stays": tuple(map(_mark_stale, batch.stays)),
            }
        )

    async def put(
        self,
        spec: SearchSpec,
        mode: SearchMode,
        batch: SearchBatch,
        *,
        fetch_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> None:
        positive = bool(batch.stays if mode == "stay" else batch.journeys)
        negative = mode == "flight" and not positive and batch.failure in CACHEABLE_FAILURES
        if self.ttl <= timedelta(0) or not (positive or negative):
            return
        namespace, key = cache_identity(spec, mode)
        checked = now or datetime.now(UTC)
        payload = cast(
            dict[str, object],
            json.loads(batch.model_copy(update={"calls": 0}).model_dump_json()),
        )
        values = {
            "payload": payload,
            "stored_at": checked,
            "fetch_seconds": fetch_seconds,
            "is_negative": negative,
        }
        async with self.sessions.begin() as session:
            await session.execute(
                delete(SearchCacheRow).where(
                    or_(
                        and_(
                            SearchCacheRow.is_negative,
                            SearchCacheRow.stored_at <= checked - self._negative_ttl,
                        ),
                        and_(
                            ~SearchCacheRow.is_negative,
                            SearchCacheRow.stored_at <= checked - self.ttl - self.stale_if_error,
                        ),
                    )
                )
            )
            try:
                async with session.begin_nested():
                    session.add(SearchCacheRow(namespace=namespace, key=key, **values))
                    await session.flush()
            except IntegrityError:
                if negative:
                    return
                await session.execute(
                    update(SearchCacheRow)
                    .where(SearchCacheRow.namespace == namespace, SearchCacheRow.key == key)
                    .values(**values)
                )
