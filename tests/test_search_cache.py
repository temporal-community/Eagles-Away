"""Short shared search cache contracts, independent of provider traffic."""

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.testing import ActivityEnvironment

from cpfc_trip.config import Settings
from cpfc_trip.domain import FlightFailure, Party, SearchBatch, SearchSpec, SessionInput
from cpfc_trip.persistence.database import engine
from cpfc_trip.persistence.models import Base
from cpfc_trip.persistence.search_cache import STALE_GAP, SearchCache
from cpfc_trip.planner.recorded import sample
from cpfc_trip.temporal.activities import SearchActivities

CHECKED = datetime(2026, 10, 5, 12, tzinfo=UTC)


@pytest.fixture
async def cache_engine(settings: Settings, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Create a separate database so two cache instances share durable rows."""
    database = engine(
        settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"})
    )
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()


def search_spec(data: SessionInput) -> SearchSpec:
    """Build a dated request with a known origin, destination and party."""
    return SearchSpec(
        fixture=data.fixtures[0],
        route=data.routes[0],
        party=Party(adults=2),
        outbound_date=date(2026, 10, 14),
        return_date=date(2026, 10, 16),
    )


async def test_transport_cache_is_shared_for_24_hours_and_party_safe(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    """Only the same route, dates, mode and travellers may reuse a journey."""
    request = search_spec(session_input)
    batch = sample(request, "flight").model_copy(update={"calls": 3})
    writer = SearchCache(cache_engine)
    reader = SearchCache(cache_engine)
    await writer.put(request, "flight", batch, now=CHECKED)

    hit = await reader.get(request, "flight", now=CHECKED + timedelta(hours=23))
    assert hit is not None and hit.calls == 0
    assert hit.journeys == batch.journeys
    assert await reader.get(request, "rail", now=CHECKED) is None
    assert (
        await reader.get(
            request.model_copy(update={"party": Party(adults=1)}), "flight", now=CHECKED
        )
        is None
    )
    assert (
        await reader.get(
            request.model_copy(update={"return_date": date(2026, 10, 17)}), "flight", now=CHECKED
        )
        is None
    )
    assert await reader.get(request, "flight", now=CHECKED + timedelta(hours=24)) is None


async def test_configured_ttl_applies_to_rows_already_stored(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    """Changing the TTL changes reuse of existing rows; zero always searches live."""
    request = search_spec(session_input)
    batch = sample(request, "flight")
    await SearchCache(cache_engine).put(request, "flight", batch, now=CHECKED)
    later = CHECKED + timedelta(hours=30)

    assert await SearchCache(cache_engine).get(request, "flight", now=later) is None
    week = SearchCache(cache_engine, timedelta(days=7))
    assert await week.get(request, "flight", now=later) is not None
    short = SearchCache(cache_engine, timedelta(minutes=10))
    assert await short.get(request, "flight", now=CHECKED + timedelta(minutes=11)) is None

    disabled = SearchCache(cache_engine, timedelta(0))
    assert await disabled.get(request, "flight", now=CHECKED) is None
    other = request.model_copy(update={"party": Party(adults=1)})
    await disabled.put(other, "flight", sample(other, "flight"), now=CHECKED)
    assert await week.get(other, "flight", now=CHECKED) is None


async def test_early_expiration_scales_with_search_cost_and_draw(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    """XFetch: a read may refresh before the TTL by fetch_seconds * beta * -ln(1 - U)."""
    request = search_spec(session_input)
    await SearchCache(cache_engine).put(
        request, "flight", sample(request, "flight"), fetch_seconds=10, now=CHECKED
    )
    nearly_expired = CHECKED + timedelta(hours=23, minutes=30)

    def cache(draw: float) -> SearchCache:
        return SearchCache(cache_engine, early_beta=360, uniform=lambda: draw)

    # A draw of 0 never refreshes early; 1 - 1/e refreshes one mean lead (10 s x 360 = 1 h) early.
    assert await cache(0.0).get(request, "flight", now=nearly_expired) is not None
    lead = 1 - math.exp(-1)
    assert await cache(lead).get(request, "flight", now=nearly_expired) is None
    assert await cache(lead).get(request, "flight", now=CHECKED + timedelta(hours=22)) is not None
    cheap = request.model_copy(update={"party": Party(adults=1)})
    await SearchCache(cache_engine).put(
        cheap, "flight", sample(cheap, "flight"), fetch_seconds=1, now=CHECKED
    )
    assert await cache(lead).get(cheap, "flight", now=nearly_expired) is not None


@pytest.mark.parametrize(("age_hours", "served"), [(30, True), (49, False)])
async def test_failed_live_search_falls_back_to_recent_cached_result(
    cache_engine: AsyncEngine,
    session_input: SessionInput,
    monkeypatch: pytest.MonkeyPatch,
    age_hours: int,
    served: bool,
) -> None:
    """Stale-if-error: within TTL + window a cached copy stands in, labelled as older."""
    request = search_spec(session_input)
    batch = sample(request, "flight")
    await SearchCache(cache_engine).put(
        request, "flight", batch, now=datetime.now(UTC) - timedelta(hours=age_hours)
    )
    failure = SearchBatch(gaps=("Provider failed",), calls=3, failure="provider_error")

    async def failing_acquire(spec: SearchSpec, mode: str) -> SearchBatch:
        return failure

    monkeypatch.setattr("cpfc_trip.temporal.activities.acquire", failing_acquire)
    cache = SearchCache(cache_engine, stale_if_error=timedelta(hours=24))
    result = await SearchActivities(cache).search_flights(request)
    if served:
        assert result.stale
        outbound = result.journeys[0].outbound[0]
        assert outbound.offer == batch.journeys[0].outbound[0].offer
        assert STALE_GAP in outbound.caveats
        assert STALE_GAP in result.gaps
        assert result.calls == 3
    else:
        assert result == failure


@pytest.mark.parametrize("failure_reason", ["provider_empty", "search_limits"])
async def test_non_error_live_result_does_not_use_stale_copy(
    cache_engine: AsyncEngine,
    session_input: SessionInput,
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: FlightFailure,
) -> None:
    request = search_spec(session_input)
    await SearchCache(cache_engine).put(
        request, "flight", sample(request, "flight"), now=datetime.now(UTC) - timedelta(hours=30)
    )
    empty = SearchBatch(gaps=("No flights",), calls=2, failure=failure_reason)

    async def empty_acquire(spec: SearchSpec, mode: str) -> SearchBatch:
        return empty

    monkeypatch.setattr("cpfc_trip.temporal.activities.acquire", empty_acquire)
    cache = SearchCache(cache_engine, stale_if_error=timedelta(hours=24))
    assert await SearchActivities(cache).search_flights(request) == empty


async def test_hotel_cache_ignores_route_but_keys_preferences(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    """Accommodation can be reused across routes, never across party or room needs."""
    request = search_spec(session_input)
    batch = sample(request, "stay").model_copy(update={"calls": 3})
    cache = SearchCache(cache_engine)
    await cache.put(request, "stay", batch, now=CHECKED)

    other_route = request.model_copy(update={"route": session_input.routes[1]})
    hit = await cache.get(other_route, "stay", now=CHECKED)
    assert hit is not None and hit.calls == 0 and hit.stays == batch.stays
    assert (
        await cache.get(request.model_copy(update={"budget_tier": "comfort"}), "stay", now=CHECKED)
        is None
    )
    assert (
        await cache.get(request.model_copy(update={"private_bathroom": True}), "stay", now=CHECKED)
        is None
    )
    assert (
        await cache.get(request.model_copy(update={"party": Party(adults=1)}), "stay", now=CHECKED)
        is None
    )
    assert request.fixture.venue_coordinates is not None
    moved_venue = request.fixture.model_copy(
        update={
            "venue_coordinates": request.fixture.venue_coordinates.model_copy(
                update={"latitude": request.fixture.venue_coordinates.latitude + 0.01}
            )
        }
    )
    assert (
        await cache.get(request.model_copy(update={"fixture": moved_venue}), "stay", now=CHECKED)
        is None
    )
    assert await cache.get(request, "stay", now=CHECKED + timedelta(hours=24)) is None


async def test_provider_failures_are_not_shared(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    cache = SearchCache(cache_engine)
    request = search_spec(session_input)
    await cache.put(request, "flight", SearchBatch(gaps=("Provider failed",), calls=1), now=CHECKED)
    for failure in ("provider_error", "search_limits", "onward_rail"):
        batch = SearchBatch(gaps=("No result",), calls=1, failure=failure)
        await cache.put(request, "flight", batch, now=CHECKED)
    assert await cache.get(request, "flight", now=CHECKED) is None
    await cache.put(request, "stay", SearchBatch(gaps=("No room",), calls=1), now=CHECKED)
    assert await cache.get(request, "stay", now=CHECKED) is None


async def test_flight_dead_end_is_reused_for_an_hour_and_never_replaces_a_result(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    cache = SearchCache(
        cache_engine, stale_if_error=timedelta(days=1), early_beta=1000, uniform=lambda: 0.5
    )
    request = search_spec(session_input)
    empty = SearchBatch(gaps=("No flights",), calls=1, failure="provider_empty")
    await cache.put(request, "flight", empty, fetch_seconds=25, now=CHECKED)
    hit = await cache.get(request, "flight", now=CHECKED + timedelta(minutes=59))
    assert hit is not None and hit.calls == 0 and hit.failure == "provider_empty"
    assert await cache.stale(request, "flight", now=CHECKED + timedelta(minutes=59)) is None
    assert await cache.get(request, "flight", now=CHECKED + timedelta(hours=1)) is None
    now = CHECKED + timedelta(hours=1, seconds=1)
    await cache.put(request, "flight", empty, now=now)
    refreshed = await cache.get(request, "flight", now=now)
    assert refreshed is not None and refreshed.failure == "provider_empty"

    other = request.model_copy(update={"return_date": date(2026, 10, 17)})
    found = sample(other, "flight")
    await cache.put(other, "flight", found, now=CHECKED)
    await cache.put(other, "flight", empty, now=CHECKED)
    kept = await cache.get(other, "flight", now=CHECKED)
    assert kept is not None and kept.journeys == found.journeys


async def test_nearby_departure_route_reuses_the_same_airport_search(
    cache_engine: AsyncEngine, session_input: SessionInput
) -> None:
    from cpfc_trip.origins import alternate_departure_route, routes_for_origin

    cache = SearchCache(cache_engine)
    manchester = routes_for_origin(session_input.routes, "Manchester")[0]
    liverpool = routes_for_origin(session_input.routes, "Liverpool")[0]
    via_manchester = alternate_departure_route(liverpool, "Liverpool", "Manchester")
    assert via_manchester is not None and via_manchester.id != manchester.id
    request = search_spec(session_input).model_copy(update={"route": manchester})
    await cache.put(request, "flight", sample(request, "flight"), now=CHECKED)

    hit = await cache.get(
        request.model_copy(update={"route": via_manchester}), "flight", now=CHECKED
    )
    assert hit is not None and hit.journeys
    assert {journey.route_id for journey in hit.journeys} == {via_manchester.id}
    assert (
        await cache.get(request.model_copy(update={"route": liverpool}), "flight", now=CHECKED)
        is None
    )


async def test_activity_uses_shared_cache_without_charging_calls(
    cache_engine: AsyncEngine, session_input: SessionInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second worker uses the first worker's result and original quote times."""
    request = search_spec(session_input)
    batch = sample(request, "flight").model_copy(update={"calls": 2})
    calls = 0

    async def fake_acquire(spec: SearchSpec, mode: str) -> SearchBatch:
        nonlocal calls
        assert spec == request and mode == "flight"
        calls += 1
        return batch

    monkeypatch.setattr("cpfc_trip.temporal.activities.acquire", fake_acquire)
    first = SearchActivities(SearchCache(cache_engine))
    second = SearchActivities(SearchCache(cache_engine))
    assert await first.search_flights(request) == batch
    reused = await second.search_flights(request)
    assert reused.calls == 0
    assert reused.journeys == batch.journeys
    assert calls == 1


async def test_concurrent_identical_searches_share_one_provider_search(
    cache_engine: AsyncEngine, session_input: SessionInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = search_spec(session_input)
    batch = sample(request, "flight").model_copy(update={"calls": 3})
    release = asyncio.Event()
    calls = 0

    async def slow_acquire(spec: SearchSpec, mode: str) -> SearchBatch:
        nonlocal calls
        calls += 1
        await release.wait()
        return batch

    monkeypatch.setattr("cpfc_trip.temporal.activities.acquire", slow_acquire)
    search = SearchActivities(SearchCache(cache_engine))
    first = asyncio.create_task(search.search_flights(request))
    others = [asyncio.create_task(search.search_flights(request)) for _ in range(3)]
    await asyncio.sleep(0.05)
    release.set()
    leader, *followers = await asyncio.gather(first, *others)
    assert calls == 1
    assert leader.calls == 3
    assert all(f.calls == 0 and f.journeys == batch.journeys for f in followers)
    assert not search.inflight


async def test_cache_failure_falls_through_to_provider(
    cache_engine: AsyncEngine, session_input: SessionInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache outage must not prevent a paid live search from proceeding."""
    request = search_spec(session_input)
    batch = sample(request, "flight")
    provider_calls = 0

    async def broken_get(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("cache unavailable")

    async def fake_acquire(spec: SearchSpec, mode: str) -> SearchBatch:
        nonlocal provider_calls
        provider_calls += 1
        return batch

    monkeypatch.setattr(SearchCache, "get", broken_get)
    monkeypatch.setattr("cpfc_trip.temporal.activities.acquire", fake_acquire)
    assert await SearchActivities(SearchCache(cache_engine)).search_flights(request) == batch
    assert provider_calls == 1


async def test_recovered_search_reserves_both_attempts(
    cache_engine: AsyncEngine, session_input: SessionInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous first attempt cannot refund its possibly paid calls."""
    request = search_spec(session_input)
    observed_heartbeats: list[tuple[object, ...]] = []

    async def fake_acquire(spec: SearchSpec, mode: str) -> SearchBatch:
        return sample(spec, mode).model_copy(update={"calls": 3})

    monkeypatch.setattr("cpfc_trip.temporal.activities.acquire", fake_acquire)
    environment = ActivityEnvironment()
    environment.info = replace(environment.info, attempt=2)
    environment.on_heartbeat = lambda *details: observed_heartbeats.append(details)
    recovered = await environment.run(
        SearchActivities(SearchCache(cache_engine)).search_flights, request
    )
    assert recovered.journeys
    assert recovered.calls == 16
    assert observed_heartbeats
