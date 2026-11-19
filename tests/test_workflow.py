import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from cpfc_trip.catalog import load_catalog, validate_brief
from cpfc_trip.domain import DatePair, Limits, SearchBatch, SearchSpec
from cpfc_trip.origins import airports_for, alternate_departure_route, routes_for_origin
from cpfc_trip.planner.providers.searchapi import (
    PRIVATE_BATHROOM_UNVERIFIED_GAP,
)
from cpfc_trip.planner.recorded import sample
from cpfc_trip.temporal.workflow import TravelPlanningSessionWorkflow as Workflow

TEMPLATE_SUMMARY = (
    "Balanced price and journey time. Transfer fares and outstanding checks are excluded."
)


@pytest.fixture
async def environment():
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal"),
        data_converter=pydantic_data_converter,
    ) as env:
        yield env


def live(session_input, **fields):
    return session_input.model_copy(
        update={
            "planner_mode": "live",
            "routes": (session_input.routes[0].model_copy(update={"enabled": True}),),
            **fields,
        }
    )


@activity.defn(name="search_flights")
async def sample_flights(spec: SearchSpec) -> SearchBatch:
    return sample(spec, "flight")


@activity.defn(name="search_stays")
async def sample_stays(spec: SearchSpec) -> SearchBatch:
    return sample(spec, "stay")


async def replay(handle) -> None:
    await Replayer(workflows=[Workflow], data_converter=pydantic_data_converter).replay_workflow(
        await handle.fetch_history()
    )


async def test_recorded_plan_query_restart_and_replay(environment, session_input):
    queue = str(uuid4())
    async with Worker(environment.client, task_queue=queue, workflows=[Workflow]):
        handle = await environment.client.start_workflow(
            Workflow.run, session_input, id=str(uuid4()), task_queue=queue
        )
        result = await handle.result()
        assert result.phase == "closed"
        assert result.development_mode
        assert result.itinerary and result.itinerary.trips[0].journey
        assert result.itinerary.trips[0].stay
        assert result.itinerary.trips[0].summary == TEMPLATE_SUMMARY
        checks = result.itinerary.trips[0].checks
        # Each gap has a scannable headline; the full sentence is kept as its detail.
        assert [c.detail for c in checks] == list(result.itinerary.trips[0].gaps)
        assert all(0 < len(c.headline) <= 60 for c in checks)
        assert result.preview_trip is None
        for _ in range(3):
            assert await handle.query(Workflow.get_snapshot, result.state_revision) is None
    # No worker-side in-memory state survives this fresh Worker instance.
    async with Worker(environment.client, task_queue=queue, workflows=[Workflow]):
        recovered = await handle.query(Workflow.get_snapshot)
    assert recovered == result
    await replay(handle)
    assert len(result.model_dump_json().encode()) < 100_000


async def test_no_route_fails_and_replays(environment, session_input):
    value = session_input.model_copy(update={"routes": ()})
    queue = str(uuid4())
    async with Worker(environment.client, task_queue=queue, workflows=[Workflow]):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        result = await handle.result()
    assert result.phase == "failed"
    assert result.itinerary is None
    assert "No supported travel route" in result.progress_message
    await replay(handle)


@pytest.mark.parametrize(
    ("origin", "fixture_id", "failure", "reason", "message", "offered"),
    [
        (
            "Edinburgh",
            "uel-2026-lyon-away",
            "provider_empty",
            "no_flights",
            "No flights from Edinburgh to Lyon were listed on the dates we checked.",
            ["Glasgow", "London"],
        ),
        (
            "Cardiff",
            "uel-2026-lyon-away",
            "provider_empty",
            "no_flights",
            "No flights from Cardiff to Lyon were listed on the dates we checked.",
            [],
        ),
        (
            "Nottingham",
            "uel-2026-jagiellonia-away",
            "no_timely_flight",
            "flight_timing",
            "lands in Warsaw early enough for a train that reaches Białystok",
            [],
        ),
        (
            "Nottingham",
            "uel-2026-jagiellonia-away",
            "onward_rail",
            "onward_rail",
            "train times for the Warsaw–Białystok connection",
            [],
        ),
        (
            "Liverpool",
            "uel-2026-salzburg-away",
            "unverified",
            "flights_unverified",
            "Flights from Liverpool looked suitable",
            ["London"],
        ),
    ],
)
async def test_failed_flight_search_explains_cause_and_offers_only_verified_airports(
    environment, session_input, origin, fixture_id, failure, reason, message, offered
):
    flight_origins = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        flight_origins.append(spec.route.origin)
        airports = set(spec.route.origin.split(","))
        if airports & ({*airports_for(origin), "MAN"}):
            return SearchBatch(failure=failure, gaps=("No journey was verified.",))
        return sample(spec, "flight")

    fixtures, catalog_routes = load_catalog()
    fixture = next(item for item in fixtures if item.id == fixture_id)
    brief = session_input.brief.model_copy(
        update={
            "origin_city": origin,
            "transport_mode": "flight",
            "fixture_ids": (fixture_id,),
            "windows": (),
        }
    )
    brief = validate_brief(brief, (fixture,), datetime(2026, 9, 6, tzinfo=UTC))
    routes = routes_for_origin(
        tuple(route for route in catalog_routes if route.fixture_id == fixture_id), origin
    )
    value = session_input.model_copy(
        update={"brief": brief, "fixtures": (fixture,), "routes": routes, "planner_mode": "live"}
    )
    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        result = await handle.result()
    assert result.phase == "failed"
    assert result.failure_reason == reason
    assert message in result.progress_message
    assert [action.departure_city for action in result.recovery_actions] == offered
    assert all(action.kind == "depart_from" for action in result.recovery_actions)
    if failure == "onward_rail":
        assert not result.searched_dates
    else:
        assert result.searched_dates
        if not offered and len(flight_origins) > len(result.searched_dates):
            assert "We also checked" in result.progress_message
    assert result.preview_trip and result.preview_trip.stay
    assert result.recovery_links
    await replay(handle)


@pytest.mark.parametrize(
    ("reason", "failure_reason", "expected"),
    [
        ("no_stay", "stay", "verify a stay"),
        ("late_return", "flight_timing", "arrives five hours before kickoff"),
        ("no_route", "search_limits", "within the search limits"),
        ("hotel_error", "stay", "verify a stay"),
        ("flight_error", "search_limits", "within the search limits"),
        ("private_bathroom", "private_bathroom", "confirm a private bathroom"),
    ],
)
async def test_no_complete_trip_explains_evidence_gap(
    environment, session_input, reason, failure_reason, expected
):
    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if reason == "flight_error":
            raise ApplicationError("Travel provider unavailable", non_retryable=True)
        if reason == "no_route":
            return SearchBatch(gaps=("Complete outbound and return flights were not available.",))
        batch = sample(spec, "flight")
        if reason == "late_return" and spec.route.origin != "DUB":
            journey = batch.journeys[0]
            inbound = journey.inbound[0].model_copy(
                update={
                    "departs_at": spec.fixture.kickoff_at,
                    "arrives_at": spec.fixture.kickoff_at + timedelta(hours=2),
                }
            )
            return SearchBatch(journeys=(journey.model_copy(update={"inbound": (inbound,)}),))
        return batch

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        if reason == "hotel_error":
            raise ApplicationError("Hotel provider unavailable", non_retryable=True)
        if reason == "private_bathroom" and spec.private_bathroom:
            return SearchBatch(gaps=(PRIVATE_BATHROOM_UNVERIFIED_GAP,))
        return SearchBatch() if reason == "no_stay" else sample(spec, "stay")

    queue = str(uuid4())
    brief = session_input.brief.model_copy(
        update={"origin_city": "Belfast", "private_bathroom": reason == "private_bathroom"}
    )
    routes = routes_for_origin(session_input.routes, "Belfast")
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run,
            live(session_input, brief=brief, routes=routes),
            id=str(uuid4()),
            task_queue=queue,
        )
        result = await handle.result()
    assert result.phase == "failed"
    assert result.itinerary is None
    assert result.failure_reason == failure_reason
    assert expected in result.progress_message
    assert "unavailable" not in result.progress_message
    if reason in {"no_stay", "hotel_error"}:
        assert result.preview_trip and result.preview_trip.journey
        assert result.preview_trip.stay is None
    elif reason in {"no_route", "flight_error"}:
        assert result.preview_trip and result.preview_trip.stay
        assert result.preview_trip.journey is None
    if reason == "private_bathroom":
        (action,) = result.recovery_actions
        assert action.kind == "without_private_bathroom"
    elif reason == "late_return":
        assert [action.departure_city for action in result.recovery_actions] == ["Dublin"]
    else:
        assert not result.recovery_actions
    await replay(handle)


def salzburg_flight_input(
    session_input, departure_city=None, flexibility="day_either_side", origin="Liverpool"
):
    fixtures, catalog_routes = load_catalog()
    fixture = next(item for item in fixtures if item.city == "Salzburg")
    brief = validate_brief(
        session_input.brief.model_copy(
            update={
                "origin_city": origin,
                "fixture_ids": (fixture.id,),
                "windows": (),
                "transport_mode": "flight",
                "private_room": True,
                "private_bathroom": True,
                "departure_city": departure_city,
                "flexibility": flexibility,
            }
        ),
        (fixture,),
        datetime(2026, 11, 17, tzinfo=UTC),
    )
    routes = routes_for_origin(
        tuple(
            route for route in catalog_routes if route.fixture_id == fixture.id and route.enabled
        ),
        origin,
    )
    if departure_city is not None:
        alternate = alternate_departure_route(routes[0], origin, departure_city)
        assert alternate is not None
        routes = (alternate,)
    return live(session_input, brief=brief, fixtures=(fixture,), routes=routes)


@pytest.mark.parametrize("journey_rejected_on_timing", [False, True])
async def test_recovery_check_keeps_stay_preview_without_waiting_for_unneeded_rooms(
    environment, session_input, journey_rejected_on_timing
):
    value = salzburg_flight_input(session_input)
    fixture = value.fixtures[0]
    preferred_outbound = fixture.kickoff_at.date() - timedelta(days=1)
    release_flights = asyncio.Event()
    slow_stay_started = asyncio.Event()
    release_stay = asyncio.Event()
    flight_requests = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        flight_requests.append((spec.route.origin, spec.outbound_date))
        if spec.route.origin != "LPL":
            return sample(spec, "flight")
        await release_flights.wait()
        if journey_rejected_on_timing:
            journey = sample(spec, "flight").journeys[0]
            inbound = journey.inbound[0].model_copy(
                update={
                    "departs_at": fixture.kickoff_at,
                    "arrives_at": fixture.kickoff_at + timedelta(hours=2),
                }
            )
            return SearchBatch(journeys=(journey.model_copy(update={"inbound": (inbound,)}),))
        return SearchBatch(failure="provider_empty", gaps=("No flights were listed.",))

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        if spec.outbound_date != preferred_outbound:
            slow_stay_started.set()
            await release_stay.wait()
        return sample(spec, "stay")

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        try:
            await asyncio.wait_for(slow_stay_started.wait(), timeout=5)
            async with asyncio.timeout(5):
                while True:
                    snapshot = await handle.query(Workflow.get_snapshot)
                    if snapshot and snapshot.preview_trip and snapshot.preview_trip.stay:
                        break
                    await asyncio.sleep(0.03)
            release_flights.set()
            result = await asyncio.wait_for(handle.result(), timeout=5)
            assert not release_stay.is_set()
        finally:
            release_flights.set()
            release_stay.set()

    assert {request for request in flight_requests if request[0] == "LPL"} == {
        ("LPL", preferred_outbound),
        ("LPL", fixture.kickoff_at.date()),
        ("LPL", preferred_outbound - timedelta(days=1)),
    }
    assert result.phase == "failed" and result.itinerary is None
    assert result.failure_reason == (
        "flight_timing" if journey_rejected_on_timing else "no_flights"
    )
    assert result.progress_message.startswith(
        "No flight from Liverpool arrives five hours before kickoff"
        if journey_rejected_on_timing
        else "No flights from Liverpool to Salzburg were listed"
    )
    assert result.preview_trip == snapshot.preview_trip
    actions = result.recovery_actions
    assert [action.departure_city for action in actions] == ["Manchester", "London"]
    assert "MAN" in actions[0].detail and "LHR" in actions[1].detail
    assert result.alternate_origin_city is None
    assert result.recovery_links
    assert all("MAN" not in link.url for link in result.recovery_links)
    await replay(handle)


async def test_slow_stay_does_not_hold_back_the_next_date_pair(environment, session_input):
    value = salzburg_flight_input(session_input, flexibility="two_days")
    release_stay = asyncio.Event()
    flight_dates = []
    third_pair_started = asyncio.Event()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if spec.route.origin == "LPL":
            flight_dates.append((spec.outbound_date, spec.return_date))
            if len(flight_dates) == 3:
                third_pair_started.set()
        return SearchBatch(failure="provider_empty", gaps=("No flights were listed.",))

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        await release_stay.wait()
        return sample(spec, "stay")

    queue = str(uuid4())
    async with Worker(
        environment.client, task_queue=queue, workflows=[Workflow], activities=[flights, stays]
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        try:
            await asyncio.wait_for(third_pair_started.wait(), timeout=5)
        finally:
            release_stay.set()
        result = await asyncio.wait_for(handle.result(), timeout=10)
    assert len(set(flight_dates)) == 3
    assert result.phase == "failed" and result.failure_reason == "no_flights"
    await replay(handle)


async def test_recovery_checks_leave_budget_for_the_next_date_pair(environment, session_input):
    value = salzburg_flight_input(session_input, flexibility="two_days")
    kickoff = value.fixtures[0].kickoff_at.date()
    final_departure = kickoff - timedelta(days=1)
    final_return = kickoff + timedelta(days=2)
    release_nearby = asyncio.Event()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if spec.route.origin != "LPL":
            await release_nearby.wait()
        elif (spec.outbound_date, spec.return_date) == (final_departure, final_return):
            release_nearby.set()
            return sample(spec, "flight").model_copy(update={"calls": 8})
        return SearchBatch(failure="provider_empty", calls=8)

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        if spec.return_date != final_return:
            await asyncio.sleep(1)
        return sample(spec, "stay").model_copy(update={"calls": 8})

    queue = str(uuid4())
    async with Worker(
        environment.client, task_queue=queue, workflows=[Workflow], activities=[flights, stays]
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        try:
            result = await asyncio.wait_for(handle.result(), timeout=10)
        finally:
            release_nearby.set()
    assert result.phase == "closed" and result.itinerary
    stay = result.itinerary.trips[0].stay
    assert stay and (stay.check_in, stay.check_out) == (final_departure, final_return)
    await replay(handle)


async def test_failed_comparison_dates_start_nearby_checks_while_preferred_is_slow(
    environment, session_input
):
    value = salzburg_flight_input(session_input)
    preferred_outbound = value.fixtures[0].kickoff_at.date() - timedelta(days=1)
    release_preferred = asyncio.Event()
    nearby_checked = asyncio.Event()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if spec.route.origin != "LPL":
            nearby_checked.set()
            return sample(spec, "flight")
        if spec.outbound_date == preferred_outbound:
            await release_preferred.wait()
        return SearchBatch(failure="no_timely_flight", gaps=("No listed flight fits.",))

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        try:
            await asyncio.wait_for(nearby_checked.wait(), timeout=15)
        finally:
            release_preferred.set()
        result = await asyncio.wait_for(handle.result(), timeout=10)
    assert result.phase == "failed"
    assert result.recovery_actions[0].departure_city == "Manchester"
    await replay(handle)


async def test_slow_preferred_dates_with_no_nearby_airport_left_do_not_spin(
    environment, session_input
):
    from temporalio.api.enums.v1 import EventType

    value = salzburg_flight_input(session_input, departure_city="Birmingham", origin="London")
    preferred_outbound = value.fixtures[0].kickoff_at.date() - timedelta(days=1)

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if spec.outbound_date == preferred_outbound:
            await asyncio.sleep(14)
        return SearchBatch(failure="no_timely_flight", gaps=("No listed flight fits.",))

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    timers = [
        event
        async for event in handle.fetch_history_events()
        if event.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    ]
    assert result.phase == "failed" and result.failure_reason == "flight_timing"
    assert len(timers) < 8
    await replay(handle)


async def test_refused_provider_is_reported_as_busy_without_recovery_checks(
    environment, session_input
):
    value = salzburg_flight_input(session_input)
    origins = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        origins.append(spec.route.origin)
        return SearchBatch(failure="provider_error", gaps=("Travel evidence is incomplete.",))

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        result = await asyncio.wait_for(handle.result(), timeout=10)
    assert result.phase == "failed" and result.failure_reason == "unavailable"
    assert result.progress_message.startswith("Travel search is busy right now")
    assert not result.recovery_actions
    assert set(origins) == {"LPL"}
    await replay(handle)


@pytest.mark.parametrize("incomplete", ["provider_error", "search_limits", "activity_failed"])
async def test_only_completed_main_flight_dates_are_reported(
    environment, session_input, incomplete
):
    value = salzburg_flight_input(session_input, flexibility="two_days")
    day = value.fixtures[0].kickoff_at.date()
    completed = DatePair(outbound_date=day - timedelta(days=1), return_date=day + timedelta(days=1))
    refused = set()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if spec.route.origin != "LPL":
            return SearchBatch(failure="provider_empty", calls=1)
        pair = DatePair(outbound_date=spec.outbound_date, return_date=spec.return_date)
        if pair == completed:
            return SearchBatch(failure="no_timely_flight", calls=1)
        refused.add(pair)
        if incomplete == "activity_failed":
            raise ApplicationError("Search unavailable", non_retryable=True)
        return SearchBatch(failure=incomplete, calls=1)

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        result = await environment.client.execute_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
    assert refused
    assert result.failure_reason == "flight_timing"
    assert result.searched_dates == (completed,)


@pytest.mark.parametrize(
    "london_result", ["provider_error", "search_limits", "activity_failed", "stale"]
)
async def test_recovery_message_only_names_completed_negatives(
    environment, session_input, london_result
):
    value = salzburg_flight_input(session_input, flexibility="two_days")
    checked = set()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        origin = spec.route.origin
        checked.add(origin)
        if origin in {"LPL", "MAN"}:
            return SearchBatch(failure="provider_empty", calls=1)
        if london_result == "activity_failed":
            raise ApplicationError("Search unavailable", non_retryable=True)
        if london_result == "stale":
            return sample(spec, "flight").model_copy(update={"stale": True, "calls": 1})
        return SearchBatch(failure=london_result, calls=1)

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        result = await environment.client.execute_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
    assert ",".join(airports_for("London")) in checked
    assert result.failure_reason == "no_flights"
    assert not result.recovery_actions
    assert result.progress_message.endswith(
        "We also checked flights from Manchester and found nothing that fits."
    )


@pytest.mark.parametrize("comparison_finds_flights", [True, False])
async def test_nearby_airports_are_checked_while_preferred_search_continues(
    environment, session_input, comparison_finds_flights, caplog
):
    value = salzburg_flight_input(session_input)
    fixture = value.fixtures[0]
    preferred_outbound = fixture.kickoff_at.date() - timedelta(days=1)
    release_comparison = asyncio.Event()
    nearby_checked = asyncio.Event()
    release_nearby = asyncio.Event()
    nearby_started = set()
    requests = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        requests.append((spec.route.origin, spec.outbound_date))
        if spec.route.origin != "LPL":
            nearby_started.add(spec.route.origin)
            if len(nearby_started) == 2:
                nearby_checked.set()
            if comparison_finds_flights:
                await release_nearby.wait()
            return sample(spec, "flight")
        if spec.outbound_date == preferred_outbound:
            return SearchBatch(failure="provider_empty", gaps=("No flights were listed.",))
        await release_comparison.wait()
        if comparison_finds_flights:
            return sample(spec, "flight")
        return SearchBatch(failure="provider_empty", gaps=("No flights were listed.",))

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        try:
            await asyncio.wait_for(nearby_checked.wait(), timeout=5)
        finally:
            release_comparison.set()
        try:
            result = await asyncio.wait_for(handle.result(), timeout=10)
        finally:
            release_nearby.set()

    nearby = [origin for origin, outbound in requests if origin != "LPL"]
    assert set(nearby) == {"MAN", "LHR,LGW,STN,LTN"}
    if comparison_finds_flights:
        assert result.phase == "closed" and result.itinerary
        assert result.itinerary.trips[0].journey.outbound[0].origin == "LPL"
        assert not result.recovery_actions
        assert not any("Search activity failed" in message for message in caplog.messages)
    else:
        assert result.phase == "failed"
        assert [action.departure_city for action in result.recovery_actions] == [
            "Manchester",
            "London",
        ]
        assert len(nearby) == len(set(nearby))
    await replay(handle)


@pytest.mark.parametrize("has_flights", [False, True])
async def test_chosen_airport_search_never_suggests_it_again(
    environment, session_input, has_flights
):
    value = salzburg_flight_input(session_input, departure_city="Manchester")
    flight_origins = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        flight_origins.append(spec.route.origin)
        if has_flights or spec.route.origin != "MAN":
            return sample(spec, "flight")
        return SearchBatch(failure="no_timely_flight", gaps=("No listed flight fits.",))

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        result = await handle.result()

    assert flight_origins[:2] == ["MAN", "MAN"]
    assert result.origin_city == "Liverpool"
    assert result.departure_city == "Manchester"
    if has_flights:
        assert result.phase == "closed" and result.itinerary
        assert result.alternate_origin_city == "Manchester"
        trip = result.itinerary.trips[0]
        assert trip.journey and trip.journey.outbound[0].origin == "MAN"
        assert trip.stay and trip.stay.bathroom == "private"
        assert len(flight_origins) == 2
    else:
        assert result.phase == "failed"
        assert result.alternate_origin_city is None
        assert "from Manchester" in result.progress_message
        assert [action.departure_city for action in result.recovery_actions] == ["London"]
    await replay(handle)


async def test_live_plan_closes_with_template_summary_and_replays(environment, session_input):
    searches = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        searches.append("flight")
        return sample(spec, "flight")

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        searches.append("stay")
        return sample(spec, "stay")

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, live(session_input), id=str(uuid4()), task_queue=queue
        )
        result = await handle.result()
    assert result.phase == "closed"
    assert not result.development_mode
    assert result.preview_trip is None
    assert result.itinerary and len(result.itinerary.trips) == 1
    trip = result.itinerary.trips[0]
    assert trip.journey and trip.stay
    assert trip.summary == TEMPLATE_SUMMARY
    assert result.progress_message.startswith("Your trip is ready.")
    count = len(searches)
    assert count == 4  # Two waves give one verified option and one comparison.
    # Both waves run together, so compare searches without relying on start order.
    assert sorted(searches) == ["flight", "flight", "stay", "stay"]
    await replay(handle)
    assert len(searches) == count  # Replay uses the completed search Activity results.


async def test_live_search_retries_once_after_lost_attempt(environment, session_input):
    """A retryable lost attempt is recovered within the same planning run."""
    attempts: list[int] = []

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        attempt = activity.info().attempt
        attempts.append(attempt)
        if attempt == 1 and len(attempts) == 1:
            raise RuntimeError("simulated worker loss")
        return sample(spec, "flight").model_copy(update={"calls": 16 if attempt > 1 else 0})

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        result = await environment.client.execute_workflow(
            Workflow.run, live(session_input), id=str(uuid4()), task_queue=queue
        )
    assert result.phase == "closed"
    assert result.itinerary is not None
    # The lost first attempt is retried once; the comparison wave may start in between.
    assert attempts[0] == 1 and attempts.count(2) == 1


async def test_second_worker_recovers_a_search_when_first_worker_stops(environment, session_input):
    """A lost in-flight Activity can resume on another poller without losing the plan."""
    first_started = asyncio.Event()
    recovered = asyncio.Event()

    @activity.defn(name="search_flights")
    async def interrupted_flight(spec: SearchSpec) -> SearchBatch:
        activity.heartbeat()
        first_started.set()
        while True:
            await asyncio.sleep(1)
            activity.heartbeat()

    @activity.defn(name="search_flights")
    async def recovered_flight(spec: SearchSpec) -> SearchBatch:
        assert activity.info().attempt == 2
        recovered.set()
        return sample(spec, "flight").model_copy(update={"calls": 16})

    value = live(session_input, limits=Limits(planning_seconds=60))
    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[interrupted_flight, sample_stays],
        graceful_shutdown_timeout=timedelta(0),
    ) as first_worker:
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        await asyncio.wait_for(first_started.wait(), timeout=5)
        await first_worker.shutdown()
        async with Worker(
            environment.client,
            task_queue=queue,
            workflows=[Workflow],
            activities=[recovered_flight, sample_stays],
        ):
            await asyncio.wait_for(recovered.wait(), timeout=25)
            # Comparison searches start with the first wave, so the stopped worker can also
            # lose one that only retries after the 15-second heartbeat timeout.
            result = await asyncio.wait_for(handle.result(), timeout=90)
    assert result.phase == "closed"
    assert result.itinerary and result.itinerary.trips[0].journey


async def test_slow_comparison_keeps_first_complete_wave(environment, session_input):
    from cpfc_trip.planner.planning import enumerate_specs

    value = live(session_input, limits=Limits(planning_seconds=10))
    first_spec = enumerate_specs(value.brief, value.fixtures, value.routes)[0]
    first_dates = (first_spec.outbound_date, first_spec.return_date)
    release = asyncio.Event()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if (spec.outbound_date, spec.return_date) != first_dates:
            await release.wait()
        return sample(spec, "flight")

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        if (spec.outbound_date, spec.return_date) != first_dates:
            await release.wait()
        return sample(spec, "stay")

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, stays],
    ):
        try:
            result = await environment.client.execute_workflow(
                Workflow.run, value, id=str(uuid4()), task_queue=queue
            )
        finally:
            release.set()
    assert result.phase == "closed"
    assert result.itinerary and result.itinerary.trips[0].journey
    assert result.itinerary.trips[0].stay
    assert result.itinerary.trips[0].stay.check_in == first_dates[0]


async def test_comparison_wave_starts_before_first_wave_finishes(environment, session_input):
    from cpfc_trip.planner.planning import enumerate_specs

    first_spec = enumerate_specs(session_input.brief, session_input.fixtures, session_input.routes)[
        0
    ]
    first_dates = (first_spec.outbound_date, first_spec.return_date)
    comparison_started = asyncio.Event()
    searched_dates = set()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        dates = (spec.outbound_date, spec.return_date)
        searched_dates.add(dates)
        if dates == first_dates:
            # Sequential waves would never start the comparison while this search waits.
            await asyncio.wait_for(comparison_started.wait(), timeout=5)
        else:
            comparison_started.set()
        return sample(spec, "flight")

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, sample_stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, live(session_input), id=str(uuid4()), task_queue=queue
        )
        result = await handle.result()
    assert result.itinerary and result.itinerary.trips[0].journey
    assert result.itinerary.trips[0].stay
    assert len(searched_dates) == 2
    await replay(handle)


@pytest.mark.parametrize("first_kind", ["flight", "stay"])
async def test_partial_result_is_visible_while_other_search_continues(
    environment, session_input, first_kind
):
    search_started = asyncio.Event()
    release_search = asyncio.Event()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        if first_kind == "stay":
            search_started.set()
            await release_search.wait()
        return sample(spec, "flight")

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        if first_kind == "flight":
            search_started.set()
            await release_search.wait()
        return sample(spec, "stay")

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, live(session_input), id=str(uuid4()), task_queue=queue
        )
        await asyncio.wait_for(search_started.wait(), timeout=5)
        for _ in range(50):
            state = await handle.query(Workflow.get_snapshot)
            if state.preview_trip:
                break
            await asyncio.sleep(0.05)
        assert state.phase == "researching"
        assert state.itinerary is None
        assert state.preview_trip is not None
        assert (state.preview_trip.journey is not None) == (first_kind == "flight")
        assert (state.preview_trip.stay is not None) == (first_kind == "stay")
        release_search.set()
        completed = await handle.result()
    assert completed.phase == "closed"
    assert completed.itinerary and completed.itinerary.trips[0].stay
    assert completed.preview_trip is None


@pytest.mark.parametrize("slow_flight_is_better", [False, True])
async def test_matching_dates_show_before_slow_search_finishes(
    environment, session_input, slow_flight_is_better
):
    fixtures, routes = load_catalog()
    fixture = next(f for f in fixtures if f.city == "Salzburg")
    route = next(r for r in routes if r.fixture_id == fixture.id and r.enabled)
    match_day = fixture.kickoff_at.date()
    first_outbound = match_day - timedelta(days=1)
    brief = validate_brief(
        session_input.brief.model_copy(update={"fixture_ids": (fixture.id,), "windows": ()}),
        (fixture,),
        datetime(2026, 11, 17, tzinfo=UTC),
    )
    value = live(session_input, brief=brief, fixtures=(fixture,), routes=(route,))
    release_matching_pair = asyncio.Event()
    release_slow_flight = asyncio.Event()
    searched_dates = set()

    @activity.defn(name="search_flights")
    async def flights(spec: SearchSpec) -> SearchBatch:
        searched_dates.add((spec.outbound_date, spec.return_date))
        if spec.outbound_date == first_outbound:
            await release_slow_flight.wait()
            if not slow_flight_is_better:
                return SearchBatch()
            batch = sample(spec, "flight")
            journey = batch.journeys[0]
            quote = journey.outbound[0].quote
            cheaper = quote.model_copy(
                update={"amount": quote.amount.model_copy(update={"minor_units": 12000})}
            )
            return SearchBatch(
                journeys=(
                    journey.model_copy(
                        update={
                            "outbound": (
                                journey.outbound[0].model_copy(update={"quote": cheaper}),
                            ),
                            "inbound": (journey.inbound[0].model_copy(update={"quote": cheaper}),),
                        }
                    ),
                )
            )
        await release_matching_pair.wait()
        return sample(spec, "flight")

    @activity.defn(name="search_stays")
    async def stays(spec: SearchSpec) -> SearchBatch:
        searched_dates.add((spec.outbound_date, spec.return_date))
        if spec.outbound_date != first_outbound:
            await release_matching_pair.wait()
        return sample(spec, "stay")

    queue = str(uuid4())
    async with Worker(
        environment.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[flights, stays],
    ):
        handle = await environment.client.start_workflow(
            Workflow.run, value, id=str(uuid4()), task_queue=queue
        )
        try:
            async with asyncio.timeout(5):
                while True:
                    initial = await handle.query(Workflow.get_snapshot)
                    if initial and initial.preview_trip:
                        break
                    await asyncio.sleep(0.03)
            assert (
                initial.preview_trip.stay and initial.preview_trip.stay.check_in == first_outbound
            )
            assert initial.preview_trip.journey is None
            release_matching_pair.set()

            async with asyncio.timeout(5):
                while True:
                    updated = await handle.query(Workflow.get_snapshot)
                    if updated and updated.preview_trip and updated.preview_trip.journey:
                        break
                    await asyncio.sleep(0.03)
            assert updated.phase == "researching"
            assert updated.preview_trip.stay and updated.preview_trip.stay.check_in == match_day
            assert updated.preview_options == (initial.preview_trip,)
        finally:
            release_matching_pair.set()
            release_slow_flight.set()
        completed = await handle.result()

    assert searched_dates == {
        (first_outbound, match_day + timedelta(days=1)),
        (match_day, match_day + timedelta(days=1)),
    }
    assert completed.itinerary and completed.itinerary.trips[0].stay
    if slow_flight_is_better:
        assert completed.itinerary.trips[0].stay.check_in == first_outbound
        assert completed.preview_options == (updated.preview_trip,)
    else:
        assert completed.itinerary.trips[0].stay.check_in == match_day
        assert completed.preview_options == (initial.preview_trip,)
    await replay(handle)
