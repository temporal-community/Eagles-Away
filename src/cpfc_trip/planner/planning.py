"""Pure enumeration, feasibility and ranking. No I/O and no invented commercial facts."""

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from cpfc_trip.domain import (
    Alternative,
    Brief,
    Check,
    Fixture,
    Journey,
    Party,
    Route,
    SearchBatch,
    SearchSpec,
    Stay,
    Trip,
)
from cpfc_trip.planner.maps_links import transfer_links
from cpfc_trip.planner.prices import total

MATCH_TIME_BUFFER = timedelta(hours=5)


def _return_possible(
    fixture: Fixture, route: Route, return_date: date, latest_return: datetime
) -> bool:
    earliest_departure = (fixture.kickoff_at + MATCH_TIME_BUFFER).astimezone(UTC)
    latest_arrival = latest_return.astimezone(UTC)
    if earliest_departure >= latest_arrival:
        return False
    if (
        route.destination_timezone != fixture.timezone
        or (route.mode == "flight" and route.onward_stations not in ((), ("Warsaw", "Białystok")))
        or (route.mode == "rail" and route.destination != "Lyon")
    ):
        return True
    start_hour, end_hour = route.gateway_departure_hours
    if not 0 <= start_hour < end_hour <= 24:
        return True
    local = ZoneInfo(fixture.timezone)
    midnight = datetime.combine(return_date, time.min)
    opens = midnight + timedelta(hours=start_hour)
    closes = midnight + timedelta(hours=end_hour)
    opens_at = min(opens.replace(tzinfo=local, fold=fold).astimezone(UTC) for fold in (0, 1))
    closes_at = max(closes.replace(tzinfo=local, fold=fold).astimezone(UTC) for fold in (0, 1))
    return max(earliest_departure, opens_at) < min(latest_arrival, closes_at)


def enumerate_specs(
    brief: Brief,
    fixtures: tuple[Fixture, ...],
    routes: tuple[Route, ...],
    recorded: bool = False,
    *,
    screen_impossible_dates: bool = False,
) -> list[SearchSpec]:
    grouped: list[list[SearchSpec]] = []
    for f in fixtures:
        window = next(w for w in brief.windows if w.fixture_id == f.id)
        day = f.kickoff_at.astimezone(ZoneInfo(f.timezone)).date()
        dates = [
            window.earliest_departure.date() + timedelta(days=i)
            for i in range(
                (window.latest_return.date() - window.earliest_departure.date()).days + 1
            )
        ]
        pairs = [
            (start, end) for start in dates for end in dates if start <= day <= end and start < end
        ]
        preferred = (day - timedelta(days=1), day + timedelta(days=1))
        pairs.sort(
            key=lambda pair: (
                (pair[1] - pair[0]).days if brief.flexibility == "tight" else pair != preferred,
                abs((pair[0] - preferred[0]).days) + abs((pair[1] - preferred[1]).days),
                pair,
            )
        )
        allowed = [
            r
            for r in routes
            if r.fixture_id == f.id
            and (r.enabled or recorded)
            and (not brief.transport_mode or r.mode == brief.transport_mode)
        ]
        eligible_pairs = []
        for start, end in pairs:
            eligible_routes = [
                r
                for r in allowed
                if not screen_impossible_dates or _return_possible(f, r, end, window.latest_return)
            ]
            if eligible_routes:
                eligible_pairs.append((start, end, eligible_routes))
            if len(eligible_pairs) == 3:
                break
        options = [
            SearchSpec(
                fixture=f,
                route=r,
                party=brief.travellers,
                outbound_date=start,
                return_date=end,
                budget_tier=brief.budget_tier,
                private_room=brief.private_room,
                private_bathroom=brief.private_bathroom,
                latest_return=window.latest_return,
            )
            for start, end, eligible_routes in eligible_pairs
            for r in eligible_routes
        ]
        grouped.append(options[: len(allowed)] if recorded else options)
    # Give every selected fixture an initial route/date candidate before spending on alternatives.
    return [
        options[index]
        for index in range(max(map(len, grouped), default=0))
        for options in grouped
        if index < len(options)
    ]


def feasible_stay(stay: Stay, fixture: Fixture, brief: Brief) -> bool:
    """Check a standalone stay covers match night and the requested room constraints."""
    match_day = fixture.kickoff_at.astimezone(ZoneInfo(fixture.timezone)).date()
    window = next(window for window in brief.windows if window.fixture_id == fixture.id)
    return (
        window.earliest_departure.date()
        <= stay.check_in
        <= match_day
        < stay.check_out
        <= window.latest_return.date()
        and (not brief.private_room or stay.room_type == "private")
        and (not brief.private_bathroom or stay.bathroom == "private")
        and (
            stay.max_guests is None
            or stay.max_guests * brief.travellers.rooms >= brief.travellers.size
        )
    )


def journey_stay_dates(journey: Journey, fixture: Fixture) -> tuple[date, date]:
    local = ZoneInfo(fixture.timezone)
    return (
        journey.outbound[-1].arrives_at.astimezone(local).date(),
        journey.inbound[0].departs_at.astimezone(local).date(),
    )


def feasible(
    journey: Journey,
    stay: Stay,
    fixture: Fixture,
    brief: Brief,
    route: Route | None = None,
) -> bool:
    if not feasible_journey(journey, fixture, brief, route):
        return False
    if (stay.check_in, stay.check_out) != journey_stay_dates(journey, fixture):
        return False
    if brief.private_room and stay.room_type != "private":
        return False
    if brief.private_bathroom and stay.bathroom != "private":
        return False
    if (
        stay.max_guests is not None
        and stay.max_guests * brief.travellers.rooms < brief.travellers.size
    ):
        return False
    return True


def feasible_journey(
    journey: Journey,
    fixture: Fixture,
    brief: Brief,
    route: Route | None = None,
) -> bool:
    """Check that a dated return journey fits the brief before showing it."""
    window = next(w for w in brief.windows if w.fixture_id == fixture.id)
    outbound, inbound = journey.outbound, journey.inbound
    if brief.transport_mode and outbound[0].mode != brief.transport_mode:
        return False
    if (
        outbound[0].departs_at < window.earliest_departure
        or inbound[-1].arrives_at > window.latest_return
    ):
        return False
    return fits_match(journey, fixture, route)


def arrives_in_time(arrival: datetime, fixture: Fixture, route: Route | None = None) -> bool:
    if route:
        hour = arrival.astimezone(ZoneInfo(fixture.timezone)).hour
        if not route.gateway_arrival_hours[0] <= hour < route.gateway_arrival_hours[1]:
            return False
    return arrival <= fixture.kickoff_at - MATCH_TIME_BUFFER


def leaves_after_match(departure: datetime, fixture: Fixture, route: Route | None = None) -> bool:
    if route:
        hour = departure.astimezone(ZoneInfo(fixture.timezone)).hour
        if not route.gateway_departure_hours[0] <= hour < route.gateway_departure_hours[1]:
            return False
    return departure >= fixture.kickoff_at + MATCH_TIME_BUFFER


def fits_match(journey: Journey, fixture: Fixture, route: Route | None = None) -> bool:
    outbound, inbound = journey.outbound, journey.inbound
    if not arrives_in_time(outbound[-1].arrives_at, fixture, route):
        return False
    if not leaves_after_match(inbound[0].departs_at, fixture, route):
        return False
    if (
        route
        and (route.mode == "rail" or route.onward_stations)
        and any(
            b.departs_at - a.arrives_at < timedelta(minutes=route.minimum_transfer_minutes)
            for direction in (outbound, inbound)
            for a, b in pairwise(direction)
        )
    ):
        return False
    return not any(
        b.departs_at < a.arrives_at
        for direction in (outbound, inbound)
        for a, b in pairwise(direction)
    )


def grow_preview(
    current: Trip | None,
    fixtures: tuple[Fixture, ...],
    batches: dict[str, list[SearchBatch]],
    routes: tuple[Route, ...],
    brief: Brief,
) -> tuple[Trip | None, bool]:
    journeys = {
        f.id: [
            j
            for b in batches[f.id]
            for j in b.journeys
            if (route := next((r for r in routes if r.id == j.route_id), None))
            and feasible_journey(j, f, brief, route)
        ]
        for f in fixtures
    }
    journey_found = any(journeys.values())
    if current and current.journey and current.stay:
        return current, journey_found

    candidates = sorted(
        fixtures,
        key=lambda f: (current is not None and f.id != current.fixture.id, not journeys[f.id]),
    )
    for fixture in candidates:
        if journeys[fixture.id] and any(batch.stays for batch in batches[fixture.id]):
            trip = choose(fixture, batches[fixture.id], brief, routes)
            if trip.journey and trip.stay:
                return trip, journey_found

    if current and (current.journey or not journey_found):
        return current, journey_found
    for fixture in candidates:
        journey = next(iter(journeys[fixture.id]), None)
        if journey:
            return Trip(
                fixture=fixture,
                journey=journey,
                summary="A dated return journey was found. We’re still checking accommodation and other options.",
                gaps=("Accommodation and local transfers are still being checked.",),
            ), journey_found
        if current:
            continue
        stay = next(
            (
                stay
                for batch in batches[fixture.id]
                for stay in batch.stays
                if feasible_stay(stay, fixture, brief)
            ),
            None,
        )
        if stay:
            return Trip(
                fixture=fixture,
                stay=stay,
                summary="A dated stay was found. We’re still checking return travel.",
                gaps=("Return travel is still being checked.",),
            ), journey_found
    return current, journey_found


def rank(journey: Journey, stay: Stay, party: Party, tier: str) -> float:
    legs = journey.outbound + journey.inbound
    cost, coverage = total([*(x.quote for x in legs), stay.quote], party)
    # Unknown scope/currency never wins by pretending to be free.
    pounds = cost.minor_units / 100 if cost and cost.currency == "GBP" else 10000
    hours = sum(
        (x[-1].arrives_at - x[0].departs_at).total_seconds() / 3600
        for x in (journey.outbound, journey.inbound)
    )
    comfort_penalty = (30 if stay.room_type == "dorm" else 0) + (10 - (stay.review_score or 5)) * 5
    return (
        pounds
        + (0 if tier == "budget" else 25 if tier == "value" else 60) * hours
        + (1 - coverage) * 1500
        + (comfort_penalty * (0 if tier == "budget" else 2 if tier == "value" else 5))
    )


def choose(
    fixture: Fixture,
    batches: list[SearchBatch],
    brief: Brief,
    routes: tuple[Route, ...],
) -> Trip:
    journeys = [j for b in batches for j in b.journeys]
    stays = [s for b in batches for s in b.stays]
    by_route = {r.id: r for r in routes}
    candidates = [
        (j, s)
        for j in journeys
        for s in stays
        if j.route_id in by_route and feasible(j, s, fixture, brief, by_route[j.route_id])
    ]
    if not candidates:
        return Trip(
            fixture=fixture,
            summary="We could not verify a complete trip for this fixture.",
            gaps=tuple(
                dict.fromkeys(
                    [
                        "Travel and accommodation evidence is incomplete.",
                        *(g for b in batches for g in b.gaps),
                    ]
                )
            )[:8],
        )
    candidates.sort(key=lambda x: rank(x[0], x[1], brief.travellers, brief.budget_tier))
    journey, stay = candidates[0]
    route = next(r for r in routes if r.id == journey.route_id)
    amount, coverage = total(
        [*(x.quote for x in journey.outbound + journey.inbound), stay.quote, None, None, None],
        brief.travellers,
    )
    checks = [
        Check(
            headline=route.transfer_headline or "Check local transfers", detail=route.transfer_note
        ),
        Check(
            headline="Late check-in & post-match access",
            detail="Confirm late check-in and post-match access to the accommodation.",
        ),
    ]
    if (
        route.onward_stations
        and route.mode != "rail"
        and all(leg.mode != "rail" for leg in journey.outbound)
    ):
        # Never label an airport-only journey as a complete trip to an onward destination.
        checks.append(
            Check(
                headline="Onward rail not verified",
                detail="Onward rail has not been verified; this is an incomplete travel option.",
            )
        )
    if fixture.venue_status != "confirmed":
        checks.append(
            Check(
                headline="Venue & away-end access provisional",
                detail="The match venue and away-supporter access instructions remain provisional.",
            )
        )
    gaps = [c.detail for c in checks]
    alternatives = []
    seen = {(journey.outbound[0].mode, stay.property_name)}
    for other, room in candidates[1:]:
        key = (other.outbound[0].mode, room.property_name)
        if key in seen:
            continue
        seen.add(key)
        value, fraction = total(
            [*(x.quote for x in other.outbound + other.inbound), room.quote, None, None, None],
            brief.travellers,
        )
        alternatives.append(
            Alternative(
                journey=other,
                stay=room,
                known_total=value,
                price_coverage=fraction,
                summary=f"{other.outbound[0].mode.title()} with {room.property_name}; compare missing costs separately.",
            )
        )
        if len(alternatives) == 2:
            break
    return Trip(
        fixture=fixture,
        journey=journey,
        stay=stay,
        known_total=amount,
        price_coverage=coverage,
        gaps=tuple(gaps),
        checks=tuple(checks),
        feasibility="needs_checks",
        transfers=route.guidance,
        maps_transfers=transfer_links(brief.origin_city, journey, stay, fixture),
        alternatives=tuple(alternatives),
        summary=f"{'Prioritises lower known costs among the checked options' if brief.budget_tier == 'budget' else 'Balanced price and journey time' if brief.budget_tier == 'value' else 'Prioritises shorter journeys and more comfortable stays'}. Transfer fares and outstanding checks are excluded.",
    )
