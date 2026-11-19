from datetime import date
from urllib.parse import parse_qs, urlsplit

from cpfc_trip.catalog import load_catalog
from cpfc_trip.domain import Party, SearchSpec
from cpfc_trip.origins import (
    DEPARTURE_ONLY,
    LIVE_ORIGINS,
    airports_for,
    alternate_departure_cities,
    alternate_departure_route,
    routes_for_origin,
)
from cpfc_trip.planner.links import dated_recovery_links, safe_url


def test_nearby_airports_rank_surface_alternatives_before_london():
    assert alternate_departure_cities(" liverpool ") == ("Manchester", "London")
    assert alternate_departure_cities("Glasgow") == ("Edinburgh", "London")
    assert alternate_departure_cities("London") == ("Birmingham",)
    assert alternate_departure_cities("Belfast") == ("Dublin",)
    assert alternate_departure_cities("Paris") == ()
    for city in LIVE_ORIGINS:
        alternates = alternate_departure_cities(city)
        assert city not in alternates
        if city not in ("Belfast", "London"):
            assert alternates[-1] == "London"
        for alternate in alternates:
            assert alternate in LIVE_ORIGINS or alternate in DEPARTURE_ONLY
            assert set(airports_for(city)).isdisjoint(airports_for(alternate))
    assert "Dublin" not in LIVE_ORIGINS


def test_belfast_can_depart_from_dublin_with_coach_guidance():
    _, routes = load_catalog()
    route = next(r for r in routes_for_origin(routes, "Belfast") if r.destination == "WAW")
    dublin = alternate_departure_route(route, "Belfast", "dublin")
    assert dublin is not None
    assert dublin.origin == "DUB"
    assert "between Belfast and Dublin Airport separately" in dublin.transfer_note
    assert "photo ID" in dublin.transfer_note
    assert dublin.guidance[0].source_url.startswith("https://www.dublinairport.com/")
    assert alternate_departure_route(route, "Liverpool", "Dublin") is None


def test_alternate_flight_keeps_reviewed_checks_and_explains_airport_transfer():
    _, routes = load_catalog()
    primary_routes = routes_for_origin(routes, "Glasgow")
    route = next(route for route in primary_routes if route.destination == "WAW")
    alternate = alternate_departure_route(route, "Glasgow", "Edinburgh")
    assert alternate is not None
    assert alternate.id != route.id
    assert alternate == alternate_departure_route(route, "Glasgow", "Edinburgh")
    assert alternate.origin == "EDI"
    assert alternate.destination == route.destination
    assert alternate.fixture_id == route.fixture_id
    assert alternate.onward_stations == route.onward_stations
    assert alternate.minimum_transfer_minutes == route.minimum_transfer_minutes
    assert alternate.gateway_arrival_hours == route.gateway_arrival_hours
    assert alternate.gateway_departure_hours == route.gateway_departure_hours
    assert "between Glasgow and Edinburgh Airport separately" in alternate.transfer_note
    assert route.transfer_note in alternate.transfer_note
    assert alternate.guidance[1:] == route.guidance
    assert alternate.guidance[0].source_url in alternate.source_urls
    assert alternate_departure_route(alternate, "Glasgow", "Edinburgh") is None
    london = alternate_departure_route(route, "Glasgow", "London")
    assert london is not None and london.origin == "LHR,LGW,STN,LTN"
    assert "between Glasgow and London’s airports separately" in london.transfer_note
    assert alternate_departure_route(route, "Glasgow", "Manchester") is None
    rail = next(route for route in routes if route.mode == "rail")
    assert alternate_departure_route(rail, "London", "Birmingham") is None


def test_contextual_provider_links_keep_dates_party_and_selected_airports(session_input):
    route = routes_for_origin(session_input.routes, "Liverpool")[0]
    spec = SearchSpec(
        fixture=session_input.fixtures[0],
        route=route,
        party=Party(adults=2, child_ages=(5, 12), rooms=2),
        outbound_date=date(2026, 10, 14),
        return_date=date(2026, 10, 16),
        private_bathroom=True,
    )
    flight, stay = dated_recovery_links(spec)
    assert flight.label == "Search flights from LPL on Google Flights"
    assert stay.label == "Search stays in Lyon on Booking.com (2026-10-14 to 2026-10-16)"
    assert safe_url(flight.url) == flight.url
    assert safe_url(stay.url) == stay.url
    assert urlsplit(flight.url).hostname == "www.google.com"
    assert urlsplit(stay.url).hostname == "www.booking.com"
    flight_query = (
        "Flights from LPL to LYS on 2026-10-14 through 2026-10-16 for 2 adults and 2 children"
    )
    assert parse_qs(urlsplit(flight.url).query) == {
        "q": [flight_query],
        "gl": ["GB"],
        "hl": ["en-GB"],
        "curr": ["GBP"],
    }
    assert parse_qs(urlsplit(stay.url).query) == {
        "ss": ["Lyon"],
        "checkin": ["2026-10-14"],
        "checkout": ["2026-10-16"],
        "group_adults": ["2"],
        "group_children": ["2"],
        "no_rooms": ["2"],
        "selected_currency": ["GBP"],
        "age": ["5", "12"],
    }
    assert dated_recovery_links(spec, include_stays=False) == (flight,)
    assert dated_recovery_links(spec, include_flights=False) == (stay,)
    assert dated_recovery_links(spec, include_flights=False, include_stays=False) == ()
    alternate = alternate_departure_route(route, "Liverpool", "Manchester")
    assert alternate is not None
    (alternate_flight,) = dated_recovery_links(
        spec.model_copy(update={"route": alternate}), include_stays=False
    )
    assert alternate_flight.label == "Search flights from MAN on Google Flights"
    rail = next(route for route in session_input.routes if route.mode == "rail")
    assert dated_recovery_links(spec.model_copy(update={"route": rail})) == (stay,)
