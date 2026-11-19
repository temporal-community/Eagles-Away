from datetime import timedelta

import pytest
from pydantic import ValidationError

from cpfc_trip.catalog import load_catalog
from cpfc_trip.domain import Brief, Party
from cpfc_trip.origins import airports_for, routes_for_origin
from cpfc_trip.planner.planning import enumerate_specs, feasible, grow_preview
from cpfc_trip.planner.recorded import sample


def test_catalog_fails_closed(session_input):
    assert not enumerate_specs(
        session_input.brief,
        session_input.fixtures,
        tuple(r.model_copy(update={"enabled": False}) for r in session_input.routes),
    )
    assert (
        len(
            enumerate_specs(session_input.brief, session_input.fixtures, session_input.routes, True)
        )
        == 2
    )


def test_preview_only_grows_with_matching_dates(session_input):
    s = enumerate_specs(session_input.brief, session_input.fixtures, session_input.routes, True)[0]
    flights, stays = sample(s, "flight"), sample(s, "stay")

    def grow(current, found):
        return grow_preview(
            current,
            session_input.fixtures,
            {s.fixture.id: found},
            session_input.routes,
            session_input.brief,
        )

    stay_first, journey_found = grow(None, [stays])
    assert stay_first and stay_first.stay and stay_first.journey is None
    assert not journey_found

    stay = stays.stays[0]
    later_stay = stay_first.model_copy(
        update={"stay": stay.model_copy(update={"check_in": stay.check_in + timedelta(days=1)})}
    )
    travel, journey_found = grow(later_stay, [flights])
    assert journey_found
    assert travel and travel.journey and travel.stay is None

    both, _ = grow(stay_first, [stays, flights])
    assert both and both.stay == stay and both.journey == flights.journeys[0]
    assert grow(both, [])[0] is both


def test_constraints_and_kickoff_gate(session_input):
    s = enumerate_specs(session_input.brief, session_input.fixtures, session_input.routes, True)[0]
    journey = sample(s, "flight").journeys[0]
    stay = sample(s, "stay").stays[0]
    brief = session_input.brief
    assert feasible(journey, stay, s.fixture, brief)
    late = journey.model_copy(
        update={
            "outbound": (
                journey.outbound[0].model_copy(update={"arrives_at": s.fixture.kickoff_at}),
            )
        }
    )
    assert not feasible(late, stay, s.fixture, brief)
    dorm = stay.model_copy(update={"room_type": "dorm", "bathroom": "shared"})
    assert feasible(journey, dorm, s.fixture, brief)
    assert not feasible(journey, dorm, s.fixture, brief.model_copy(update={"private_room": True}))
    assert not feasible(
        journey, dorm, s.fixture, brief.model_copy(update={"private_bathroom": True})
    )


@pytest.mark.parametrize("adults", [0, 9, -1])
def test_party_bound(adults):
    with pytest.raises(ValidationError):
        Party(adults=adults)


def test_duplicate_fixture_rejected():
    with pytest.raises(ValidationError):
        Brief(fixture_ids=("same", "same"))


def test_trip_brief_keeps_legacy_multi_match_sessions_readable():
    assert Brief(fixture_ids=("uel-2026-lyon-away",)).fixture_ids == ("uel-2026-lyon-away",)
    assert len(Brief(fixture_ids=("uel-2026-lyon-away", "uel-2026-besiktas-away")).fixture_ids) == 2


def test_all_four_away_matches_have_enabled_live_routes():
    fixtures, routes = load_catalog()
    assert {fixture.id for fixture in fixtures} == {
        route.fixture_id for route in routes if route.enabled
    }
    assert {route.id for route in routes if route.enabled} == {
        "london-lys",
        "london-paris-lyon",
        "london-ist",
        "london-waw",
        "london-szg",
    }
    assert all(fixture.venue_location for fixture in fixtures)


def test_enabled_routes_have_short_headlines_for_the_checklist():
    _, routes = load_catalog()
    for route in (r for r in routes if r.enabled):
        assert 0 < len(route.transfer_headline) <= 60, route.id
        assert all(0 < len(g.summary) <= 40 for g in route.guidance), route.id


def test_uk_departure_city_resolves_without_guessing(session_input):
    assert airports_for(" manchester ") == ("MAN",)
    assert airports_for("London") == ("LHR", "LGW", "STN", "LTN")
    with pytest.raises(ValueError, match="supported UK departure city"):
        airports_for("Paris")
    routes = routes_for_origin(session_input.routes, "Manchester")
    assert len(routes) == 2
    assert routes[0].origin == "MAN" and routes[0].mode == "flight"
    assert routes[0].id != session_input.routes[0].id
    assert routes[1].origin == "Manchester" and routes[1].mode == "rail"
    assert all(
        route.mode == "flight" for route in routes_for_origin(session_input.routes, "Belfast")
    )
    assert any(route.mode == "rail" for route in routes_for_origin(session_input.routes, "London"))
    edinburgh_rail = next(
        route
        for route in routes_for_origin(session_input.routes, "Edinburgh")
        if route.mode == "rail"
    )
    assert edinburgh_rail.gateway_arrival_hours == (6, 24)
    assert "hotel check-in" in edinburgh_rail.transfer_note


def test_recorded_city_offers_a_rail_example(session_input):
    routes = routes_for_origin(session_input.routes, "Manchester")
    any_mode = session_input.brief.model_copy(update={"origin_city": "Manchester"})
    specs = enumerate_specs(any_mode, session_input.fixtures, routes, True)
    assert {spec.route.mode for spec in specs} == {"flight", "rail"}
    brief = any_mode.model_copy(update={"transport_mode": "rail"})
    specs = enumerate_specs(brief, session_input.fixtures, routes, True)
    assert {spec.route.mode for spec in specs} == {"rail"}
    rail = specs[0]
    journey = sample(rail, "rail").journeys[0]
    stay = sample(rail, "stay").stays[0]
    assert journey.outbound[0].mode == "rail"
    assert journey.outbound[0].origin == "Manchester Piccadilly"
    assert feasible(journey, stay, rail.fixture, brief, rail.route)


def test_search_respects_custom_window_and_prioritizes_each_fixture(session_input):
    from datetime import timedelta

    from cpfc_trip.domain import Window

    fixture = session_input.fixtures[0]
    start = fixture.kickoff_at - timedelta(days=2)
    end = fixture.kickoff_at + timedelta(days=2)
    brief = session_input.brief.model_copy(
        update={
            "windows": (Window(fixture_id=fixture.id, earliest_departure=start, latest_return=end),)
        }
    )
    routes = tuple(r.model_copy(update={"enabled": True}) for r in session_input.routes)
    specs = enumerate_specs(brief, session_input.fixtures, routes)
    assert 1 < len({(s.outbound_date, s.return_date) for s in specs}) <= 3
    assert all(start.date() <= s.outbound_date < s.return_date <= end.date() for s in specs)
    assert any(s.outbound_date == start.date() for s in specs)
