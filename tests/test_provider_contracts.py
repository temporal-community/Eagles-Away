import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cpfc_trip.catalog import load_catalog
from cpfc_trip.config import Settings
from cpfc_trip.domain import Party, SearchSpec
from cpfc_trip.origins import routes_for_origin
from cpfc_trip.planner.links import flight_url, hotel_url, safe_url
from cpfc_trip.planner.prices import total
from cpfc_trip.planner.providers.searchapi import (
    ProviderError,
    SearchApi,
    connect_onward_rail,
    normalize_flights,
    normalize_stay,
    normalize_trains,
)
from cpfc_trip.planner.recorded import sample

CHECKED = datetime(2026, 9, 6, tzinfo=UTC)


def data(name):
    return json.loads((Path(__file__).parent / "data" / f"{name}.json").read_text())


def spec(session_input):
    return SearchSpec(
        fixture=session_input.fixtures[0],
        route=session_input.routes[0],
        party=Party(adults=2),
        outbound_date=date(2026, 10, 4),
        return_date=date(2026, 10, 6),
    )


def test_flight_round_trip_counted_once(session_input):
    s = spec(session_input)
    result = normalize_flights(data("flight-booking"), s, CHECKED)
    j = result.journeys[0]
    assert j.outbound[0].origin == "LGW" and j.inbound[0].destination == "LGW"
    assert j.outbound[0].service_number == "U2 8429"
    assert j.inbound[0].service_number == "U2 8430"
    price, _ = total([j.outbound[0].quote, j.inbound[0].quote], s.party)
    assert price.minor_units == 14800
    url = parse_qs(urlsplit(j.outbound[0].offer.booking_url).query)
    assert url["curr"] == ["GBP"] and url["gl"] == ["GB"] and url["tfs"]


def test_selected_city_airports_gate_returned_flights(session_input):
    route = routes_for_origin(session_input.routes, "Manchester")[0]
    selected = spec(session_input).model_copy(update={"route": route})
    raw = data("flight-booking")
    assert not normalize_flights(raw, selected, CHECKED).journeys
    raw["selected_flights"][0]["flights"][0]["departure_airport"]["id"] = "MAN"
    raw["selected_flights"][1]["flights"][0]["arrival_airport"]["id"] = "MAN"
    result = normalize_flights(raw, selected, CHECKED)
    assert result.journeys[0].outbound[0].origin == "MAN"
    assert result.journeys[0].inbound[0].destination == "MAN"


def connecting_booking():
    raw = data("flight-booking")
    outward, home = raw["selected_flights"]
    outward["flights"] = [
        {
            **outward["flights"][0],
            "departure_airport": {"id": "LGW", "date": "2026-10-04", "time": "08:00"},
            "arrival_airport": {"id": "AMS", "date": "2026-10-04", "time": "10:15"},
            "duration": 75,
            "airline": "KLM",
            "flight_number": "KL 1001",
        },
        {
            **outward["flights"][0],
            "departure_airport": {"id": "AMS", "date": "2026-10-04", "time": "11:45"},
            "arrival_airport": {"id": "LYS", "date": "2026-10-04", "time": "13:15"},
            "duration": 90,
            "airline": "KLM",
            "flight_number": "KL 1002",
        },
    ]
    home["flights"] = [
        {
            **home["flights"][0],
            "departure_airport": {"id": "LYS", "date": "2026-10-06", "time": "10:00"},
            "arrival_airport": {"id": "AMS", "date": "2026-10-06", "time": "11:30"},
            "duration": 90,
            "airline": "KLM",
            "flight_number": "KL 1003",
        },
        {
            **home["flights"][0],
            "departure_airport": {"id": "AMS", "date": "2026-10-06", "time": "13:00"},
            "arrival_airport": {"id": "LGW", "date": "2026-10-06", "time": "13:15"},
            "duration": 75,
            "airline": "KLM",
            "flight_number": "KL 1004",
        },
    ]
    outward["layovers"] = home["layovers"] = [{"id": "AMS", "duration": 90}]
    return raw


def test_one_stop_round_trip_keeps_connection_and_one_price(session_input):
    chosen = spec(session_input)
    result = normalize_flights(connecting_booking(), chosen, CHECKED)
    journey = result.journeys[0]
    assert len(journey.outbound) == len(journey.inbound) == 1
    assert journey.outbound[0].origin == "LGW"
    assert journey.outbound[0].destination == "LYS"
    assert "AMS" in journey.outbound[0].caveats[0]
    assert "KL 1001" in journey.outbound[0].service_number
    assert "KL 1002" in journey.outbound[0].service_number
    assert (
        total([journey.outbound[0].quote, journey.inbound[0].quote], chosen.party)[0].minor_units
        == 14800
    )


@pytest.mark.parametrize("change", ["wrong_airport", "short_layover", "wrong_duration"])
def test_one_stop_requires_supported_connection_evidence(session_input, change):
    raw = connecting_booking()
    if change == "wrong_airport":
        raw["selected_flights"][0]["flights"][1]["departure_airport"]["id"] = "FRA"
    elif change == "short_layover":
        raw["selected_flights"][0]["flights"][1]["duration"] = 130
    else:
        raw["selected_flights"][0]["layovers"][0]["duration"] = 200
    assert not normalize_flights(raw, spec(session_input), CHECKED).journeys


def test_hotel_price_tax_and_exact_room_identity(session_input):
    search = data("hotels")
    s = spec(session_input)
    stay = normalize_stay(search["properties"][0], data("hotel-detail"), search, s, CHECKED)
    assert stay.room_description == "Twin Room"  # Never accidentally pick the first/triple room.
    assert stay.quote.amount.minor_units == 5900
    price, _ = total([stay.quote], s.party)
    assert price.minor_units == 6800  # £59 + £9; supplier checkout may differ by rounding.
    params = parse_qs(urlsplit(stay.offer.booking_url).query)
    assert params["group_adults"] == ["2"] and params["checkin"] == ["2026-10-04"]
    assert normalize_stay(search["properties"][1], data("hotel-detail"), search, s, CHECKED) is None


def test_hotel_rejects_changed_dates_and_keeps_dorm_party_scope(session_input):
    search, details, s = data("hotels"), data("hotel-detail"), spec(session_input)
    details["search_parameters"]["check_out_date"] = "2026-10-07"
    assert normalize_stay(search["properties"][0], details, search, s, CHECKED) is None
    details = data("hotel-detail")
    details["search_parameters"]["adults"] = 1
    assert normalize_stay(search["properties"][0], details, search, s, CHECKED) is None
    details = data("hotel-detail")
    name = "Bed in Mixed Dormitory Room"
    search["properties"][0]["room_type"] = name
    details["rooms"][1].update(name=name, max_guests=1)
    stay = normalize_stay(search["properties"][0], details, search, s, CHECKED)
    assert stay.room_type == "dorm" and stay.max_guests is None
    assert stay.quote.priced_party == s.party


def test_rail_date_party_currency_are_not_guessed():
    raw = data("trains")
    legs = normalize_trains(raw, "London", "Paris", "2026-10-04", CHECKED)
    assert len(legs) == 1 and legs[0].quote.amount.minor_units == 5800
    assert legs[0].departs_at.utcoffset().total_seconds() == 3600
    assert legs[0].arrives_at.utcoffset().total_seconds() == 7200
    assert total([legs[0].quote], Party(adults=2))[0] is None
    assert not normalize_trains(raw, "London", "Paris", "2026-10-05", CHECKED)
    assert not normalize_trains(raw, "Paris", "London", "2026-10-04", CHECKED)


def test_warsaw_onward_rail_needs_reviewed_stations_and_airport_transfer(session_input):
    fixtures, routes = load_catalog()
    route = next(route for route in routes if route.id == "london-waw")
    fixture = next(fixture for fixture in fixtures if fixture.id == route.fixture_id)
    chosen = spec(session_input).model_copy(
        update={
            "fixture": fixture,
            "route": route,
            "outbound_date": date(2026, 12, 9),
            "return_date": date(2026, 12, 11),
        }
    )
    flight = sample(chosen, "flight").journeys[0]

    def train(origin, destination, day, departure, arrival, start_station, end_station):
        return {
            "train_results": {
                "origin": origin,
                "destination": destination,
                "date": day,
                "trains": [
                    {
                        "transfers": 0,
                        "time_window": {"depart_at_iso": departure, "arrive_at_iso": arrival},
                        "instructions": [
                            {
                                "depart_from": {"place": start_station, "at_iso": departure},
                                "arrive_at": {"place": end_station, "at_iso": arrival},
                                "service_provider": {
                                    "name": "PKP Intercity",
                                    "website": "https://www.intercity.pl/pl/",
                                },
                            }
                        ],
                    }
                ],
            }
        }

    outward = normalize_trains(
        train(
            "Warsaw",
            "Białystok",
            "2026-12-09",
            "2026-12-09T15:00:00+01:00",
            "2026-12-09T17:30:00+01:00",
            "Warsaw Central",
            "Zielone Wzgórza",
        ),
        "Warsaw",
        "Białystok",
        "2026-12-09",
        CHECKED,
    )
    inbound = normalize_trains(
        train(
            "Białystok",
            "Warsaw",
            "2026-12-11",
            "2026-12-11T07:00:00+01:00",
            "2026-12-11T10:00:00+01:00",
            "Zielone Wzgórza",
            "Warsaw Central",
        ),
        "Białystok",
        "Warsaw",
        "2026-12-11",
        CHECKED,
    )
    assert outward and inbound
    assert outward[0].quote is None and inbound[0].quote is None
    assert "fare unavailable" in outward[0].caveats[0]
    joined = connect_onward_rail(flight, outward, inbound, chosen.route.minimum_transfer_minutes)
    assert joined and joined[0].outbound[-1].destination == "Zielone Wzgórza"
    assert len(joined[0].outbound) == len(joined[0].inbound) == 2
    too_close = outward[0].model_copy(
        update={"departs_at": flight.outbound[-1].arrives_at + timedelta(hours=1)}
    )
    assert not connect_onward_rail(
        flight, (too_close,), inbound, chosen.route.minimum_transfer_minutes
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://www.google.com.evil.test/travel/flights",
        "https://www.booking.com@evil.test/",
        "http://www.booking.com/",
        "https://127.0.0.1",
        "https://www.google.com/travel/clk/f",
        "https://www.booking.com:444/",
    ],
)
def test_unsafe_links_rejected(url):
    assert safe_url(url) is None


def test_locale_preserves_opaque_tokens_and_child_occupancy():
    result = flight_url(
        "https://www.google.com/travel/flights/search?tfs=a%2Bb%3D&tfu=x&gl=US&curr=USD"
    )
    params = parse_qs(urlsplit(result).query)
    assert params["tfs"] == ["a+b="] and params["tfu"] == ["x"] and params["curr"] == ["GBP"]
    link = hotel_url(
        "https://www.booking.com/hotel/fr/test.html?age=99&checkin=old",
        "2026-10-04",
        "2026-10-06",
        Party(adults=2, child_ages=(4, 8), rooms=2),
    )
    params = parse_qs(urlsplit(link).query)
    assert params["age"] == ["4", "8"] and params["no_rooms"] == ["2"]


async def test_provider_error_never_exposes_key(settings):
    settings.searchapi_api_key = (
        "test-secret"  # pydantic assignment not validated; construct correctly below
    )
    settings = settings.model_copy(
        update={"searchapi_api_key": __import__("pydantic").SecretStr("test-secret")}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "test-secret"})
        )
    ) as client:
        api = SearchApi(client, settings)
        with pytest.raises(ProviderError) as exc:
            await api.get({"engine": "google"})
        assert "test-secret" not in str(exc.value)
        assert api.calls == 1


def lyon_spec(session_input):
    return spec(session_input).model_copy(
        update={"outbound_date": date(2026, 10, 15), "return_date": date(2026, 10, 16)}
    )


def lyon_booking(outbound=("08:15", "10:55"), inbound=("12:00", "12:40")) -> dict:
    booking = data("flight-booking")
    booking["search_parameters"].update(outbound_date="2026-10-15", return_date="2026-10-16")
    (out,), (back,) = (direction["flights"] for direction in booking["selected_flights"])
    for leg, day, (departs, arrives) in (
        (out, "2026-10-15", outbound),
        (back, "2026-10-16", inbound),
    ):
        leg["departure_airport"].update(date=day, time=departs)
        leg["arrival_airport"].update(date=day, time=arrives)
    return booking


def listed(
    origin: str,
    destination: str,
    departs: str,
    arrives: str,
    token: str,
    *,
    layover: int | None = None,
    key: str = "departure_token",
) -> dict:
    (start_day, start), (end_day, end) = departs.split(), arrives.split()
    first = {"departure_airport": {"id": origin, "date": start_day, "time": start}}
    last = {"arrival_airport": {"id": destination, "date": end_day, "time": end}}
    if layover is None:
        return {"flights": [{**first, **last}], key: token}
    via = {"id": "FRA", "date": start_day, "time": start}
    return {
        "flights": [{**first, "arrival_airport": via}, {"departure_airport": via, **last}],
        "layovers": [{"id": "FRA", "duration": layover}],
        key: token,
    }


def flight_server(discovery: list[dict], returns: dict, bookings: dict, calls: list):
    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        calls.append(params)
        if "booking_token" in params:
            return httpx.Response(200, json=bookings[params["booking_token"]])
        if "departure_token" in params:
            return httpx.Response(200, json={"best_flights": returns[params["departure_token"]]})
        return httpx.Response(200, json={"best_flights": discovery})

    return httpx.MockTransport(respond)


async def run_flight_search(settings, transport, search_spec):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import search_flights

    async with httpx.AsyncClient(transport=transport) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        return await search_flights(api, search_spec)


async def test_empty_flight_discovery_costs_one_request(session_input, settings):
    calls: list[httpx.QueryParams] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params)
        return httpx.Response(200, json={"error": "Google Flights didn't return any results."})

    result = await run_flight_search(
        settings, httpx.MockTransport(respond), lyon_spec(session_input)
    )
    assert result.calls == 1
    assert [call["stops"] for call in calls] == ["one_stop_or_fewer"]
    assert not result.journeys
    assert result.failure == "provider_empty"
    assert result.gaps == ("No flights were listed for this route and date pair.",)


async def test_screening_follows_only_flights_that_make_the_match(session_input, settings):
    calls: list[httpx.QueryParams] = []
    discovery = [
        listed("LGW", "LYS", "2026-10-15 15:00", "2026-10-15 17:40", "late"),
        listed("LGW", "LYS", "2026-10-15 06:00", "2026-10-15 09:30", "short", layover=45),
        listed("LGW", "LYS", "2026-10-15 06:30", "2026-10-15 11:00", "connect", layover=90),
        listed("LGW", "LYS", "2026-10-15 08:15", "2026-10-15 10:55", "good"),
    ]
    returns = {
        "good": [
            listed(
                "LYS", "LGW", "2026-10-16 06:55", "2026-10-16 07:35", "dawn", key="booking_token"
            ),
            listed("LYS", "LGW", "2026-10-16 12:00", "2026-10-16 12:40", "ok", key="booking_token"),
        ],
        "connect": [],
    }
    transport = flight_server(discovery, returns, {"ok": lyon_booking()}, calls)
    result = await run_flight_search(settings, transport, lyon_spec(session_input))
    assert len(result.journeys) == 1
    followed = [call["departure_token"] for call in calls if "departure_token" in call]
    assert sorted(followed) == ["connect", "good"]
    assert [call["booking_token"] for call in calls if "booking_token" in call] == ["ok"]
    assert result.calls == 4
    assert (result.listed, result.screened, result.followed) == (4, 2, 2)
    assert result.failure is None and not result.gaps


@pytest.mark.parametrize("limit", ["time", "requests"])
async def test_truncated_flight_follow_ups_are_not_cacheable(
    session_input, settings, monkeypatch, limit
):
    from cpfc_trip.persistence.search_cache import CACHEABLE_FAILURES
    from cpfc_trip.planner.providers import searchapi

    if limit == "time":
        monkeypatch.setattr(searchapi, "FOLLOW_UP_SECONDS", 0.05)
    calls: list[httpx.QueryParams] = []
    discovery = [
        listed("LGW", "LYS", f"2026-10-15 0{hour}:00", f"2026-10-15 0{hour + 2}:40", str(hour))
        for hour in (5, 6, 7)
    ]
    back = listed("LYS", "LGW", "2026-10-16 12:00", "2026-10-16 12:40", "", key="booking_token")
    ranks = 1 if limit == "time" else 2
    returns = {
        str(hour): [{**back, "booking_token": f"b{hour}-{rank}"} for rank in range(ranks)]
        for hour in (5, 6, 7)
    }
    stale = lyon_booking()
    stale["search_parameters"]["return_date"] = "2026-10-17"

    async def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        calls.append(params)
        if "booking_token" in params:
            if params["booking_token"] == "b7-0":
                return httpx.Response(200, json=lyon_booking())
            if limit == "time":
                await asyncio.sleep(0.1)
            return httpx.Response(200, json=stale)
        if "departure_token" in params:
            return httpx.Response(200, json={"best_flights": returns[params["departure_token"]]})
        return httpx.Response(200, json={"best_flights": discovery})

    result = await run_flight_search(
        settings, httpx.MockTransport(respond), lyon_spec(session_input)
    )
    assert result.followed == 2
    assert "7" not in [call.get("departure_token") for call in calls]
    assert result.failure == "search_limits"
    assert result.failure not in CACHEABLE_FAILURES
    assert "Returned flight dates did not match the requested dates." in result.gaps


async def test_next_option_is_followed_when_booking_details_do_not_verify(session_input, settings):
    calls: list[httpx.QueryParams] = []
    discovery = [
        listed("LGW", "LYS", "2026-10-15 07:00", "2026-10-15 09:40", "first"),
        listed("LGW", "LYS", "2026-10-15 08:15", "2026-10-15 10:55", "second"),
    ]
    back = [listed("LYS", "LGW", "2026-10-16 12:00", "2026-10-16 12:40", "", key="booking_token")]
    mismatched = lyon_booking()
    mismatched["search_parameters"]["return_date"] = "2026-10-17"
    returns = {
        "first": [{**back[0], "booking_token": "stale"}],
        "second": [{**back[0], "booking_token": "ok"}],
    }
    bookings = {"stale": mismatched, "ok": lyon_booking()}
    result = await run_flight_search(
        settings, flight_server(discovery, returns, bookings, calls), lyon_spec(session_input)
    )
    assert len(result.journeys) == 1
    assert [call.get("departure_token") for call in calls if "departure_token" in call] == [
        "first",
        "second",
    ]
    assert result.calls == 5
    assert result.followed == 2


async def test_no_flight_fitting_the_match_spends_only_discovery(session_input, settings):
    calls: list[httpx.QueryParams] = []
    discovery = [
        listed("LGW", "LYS", "2026-10-15 15:00", "2026-10-15 17:40", "late"),
        listed("STN", "LYS", "2026-10-15 19:00", "2026-10-15 21:40", "evening"),
    ]
    result = await run_flight_search(
        settings, flight_server(discovery, {}, {}, calls), lyon_spec(session_input)
    )
    assert result.calls == 1
    assert result.failure == "no_timely_flight"
    assert result.gaps == ("No listed flight arrives in time for the match and returns after it.",)
    assert (result.listed, result.screened, result.followed) == (2, 0, 0)


async def test_returns_before_the_match_ends_are_never_booked(session_input, settings):
    calls: list[httpx.QueryParams] = []
    discovery = [
        listed("LGW", "LYS", f"2026-10-15 0{hour}:00", f"2026-10-15 0{hour + 2}:40", str(hour))
        for hour in (6, 7)
    ]
    early = [listed("LYS", "LGW", "2026-10-16 06:30", "2026-10-16 07:10", "x", key="booking_token")]
    returns = {"6": early, "7": early}
    result = await run_flight_search(
        settings, flight_server(discovery, returns, {}, calls), lyon_spec(session_input)
    )
    assert not any("booking_token" in call for call in calls)
    assert result.calls == 3
    assert result.failure == "no_timely_flight"


async def test_connections_that_change_airports_are_never_booked(session_input, settings):
    calls: list[httpx.QueryParams] = []
    self_transfer = listed(
        "BHD", "LYS", "2026-10-15 06:00", "2026-10-15 11:00", "lhr-lgw", layover=90
    )
    self_transfer["flights"][0]["arrival_airport"] = {
        "id": "LHR",
        "date": "2026-10-15",
        "time": "07:30",
    }
    self_transfer["flights"][1]["departure_airport"] = {
        "id": "LGW",
        "date": "2026-10-15",
        "time": "09:00",
    }
    belfast = lyon_spec(session_input).model_copy(
        update={"route": lyon_spec(session_input).route.model_copy(update={"origin": "BFS,BHD"})}
    )
    result = await run_flight_search(
        settings, flight_server([self_transfer], {}, {}, calls), belfast
    )
    assert result.calls == 1
    assert result.failure == "unsupported_connection"
    assert "change airports" in result.gaps[0]


async def test_returns_landing_after_the_travel_window_are_never_booked(session_input, settings):
    calls: list[httpx.QueryParams] = []
    discovery = [listed("LGW", "LYS", "2026-10-15 08:15", "2026-10-15 10:55", "good")]
    returns = {
        "good": [
            listed(
                "LYS", "LGW", "2026-10-16 22:50", "2026-10-17 00:10", "late", key="booking_token"
            ),
            listed("LYS", "LGW", "2026-10-16 12:00", "2026-10-16 12:40", "ok", key="booking_token"),
        ]
    }
    deadline = datetime(2026, 10, 16, 23, 59, tzinfo=UTC) - timedelta(hours=1)
    search_spec = lyon_spec(session_input).model_copy(update={"latest_return": deadline})
    result = await run_flight_search(
        settings, flight_server(discovery, returns, {"ok": lyon_booking()}, calls), search_spec
    )
    assert [call["booking_token"] for call in calls if "booking_token" in call] == ["ok"]
    assert len(result.journeys) == 1


async def test_transient_retry_repeats_only_failed_request(settings: Settings) -> None:
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import SearchApi

    requests: list[httpx.QueryParams] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params)
        return httpx.Response(503 if len(requests) == 2 else 200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client,
            settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")}),
            limit=3,
        )
        assert await api.get({"engine": "google", "q": "discovery"}) == {"results": []}
        assert await api.get({"engine": "google", "q": "detail"}) == {"results": []}
        assert api.calls == 3
        assert requests[0] != requests[1] == requests[2]


async def test_permanent_failure_and_exhausted_budget_do_not_retry(settings: Settings) -> None:
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import ProviderError, SearchApi

    calls: list[httpx.Request] = []

    def reject(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        api = SearchApi(
            client,
            settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")}),
            limit=2,
        )
        with pytest.raises(ProviderError):
            await api.get({"engine": "google"})
        assert api.calls == 1
        assert len(calls) == 1

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        api = SearchApi(
            client,
            settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")}),
            limit=1,
        )
        with pytest.raises(ProviderError):
            await api.get({"engine": "google"})
        assert api.calls == 1


async def test_throttled_provider_pauses_the_worker_without_retrying(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import ProviderError, SearchApi

    monkeypatch.setattr(SearchApi, "paused_until", 0.0)
    requests: list[httpx.Request] = []

    def throttle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    keyed = settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
    async with httpx.AsyncClient(transport=httpx.MockTransport(throttle)) as client:
        first, second = SearchApi(client, keyed), SearchApi(client, keyed)
        with pytest.raises(ProviderError):
            await first.get({"engine": "google"})
        with pytest.raises(ProviderError):
            await second.get({"engine": "google"})
    assert len(requests) == 1
    assert (first.calls, second.calls) == (1, 0)


@pytest.mark.parametrize(
    ("refused", "status", "deadline_spent"),
    [
        ("departure_token", 402, False),
        ("booking_token", 503, False),
        ("departure_token", 503, True),
        ("booking_token", 503, True),
    ],
)
async def test_incomplete_follow_up_is_not_a_cacheable_dead_end(
    session_input, settings, monkeypatch, refused, status, deadline_spent
):
    from pydantic import SecretStr

    from cpfc_trip.persistence.search_cache import CACHEABLE_FAILURES
    from cpfc_trip.planner.providers.searchapi import search_flights

    monkeypatch.setattr(SearchApi, "paused_until", 0.0)
    discovery = [listed("LGW", "LYS", "2026-10-15 08:15", "2026-10-15 10:55", "good")]
    back = [listed("LYS", "LGW", "2026-10-16 12:00", "2026-10-16 12:40", "ok", key="booking_token")]

    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if refused in params:
            if deadline_spent:
                api.deadline = asyncio.get_running_loop().time()
            return httpx.Response(status)
        if "booking_token" in params:
            return httpx.Response(200, json=lyon_booking())
        if "departure_token" in params:
            return httpx.Response(200, json={"best_flights": back})
        return httpx.Response(200, json={"best_flights": discovery})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        result = await search_flights(api, lyon_spec(session_input))
    assert not result.journeys
    assert result.failure == ("search_limits" if deadline_spent else "provider_error")
    assert result.failure not in CACHEABLE_FAILURES


async def test_later_property_failure_preserves_checked_rooms(session_input, settings):
    from cpfc_trip.planner.providers.searchapi import search_stays

    search = data("hotels")
    prop = search["properties"][0]
    search["properties"] = [prop, {**prop, "property_id": "another-property"}]
    responses = [
        httpx.Response(200, json=search),
        httpx.Response(200, json=data("hotel-detail")),
        httpx.Response(503),
        httpx.Response(503),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: responses.pop(0))
    ) as client:
        from pydantic import SecretStr

        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        batch = await search_stays(api, spec(session_input))
        assert len(batch.stays) == 1
        assert batch.stays[0].room_description == "Twin Room"
        assert batch.calls == 4


async def test_hotel_shortlist_considers_stadium_location_within_first_40(session_input, settings):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import search_stays

    fixture = session_input.fixtures[0]
    assert fixture.venue_coordinates is not None
    base = data("hotels")["properties"][0]
    properties = [
        {
            **base,
            "link": f"https://www.booking.com/hotel/fr/candidate-{index}.html",
            "extracted_price": 100,
            "rating": 8,
            "gps_coordinates": {"latitude": 45.7527, "longitude": 4.8387},
        }
        for index in range(39)
    ]
    properties[0]["gps_coordinates"] = None
    properties.append(
        {
            **base,
            "link": "https://www.booking.com/hotel/fr/near-stadium.html",
            "extracted_price": 115,
            "rating": 8,
            "gps_coordinates": fixture.venue_coordinates.model_dump(),
        }
    )
    properties.append(
        {
            **base,
            "link": "https://www.booking.com/hotel/fr/after-first-40.html",
            "extracted_price": 10,
            "rating": 10,
            "gps_coordinates": fixture.venue_coordinates.model_dump(),
        }
    )
    requested_details = []

    def respond(request):
        params = parse_qs(request.url.query.decode())
        if params["engine"] == ["booking"]:
            return httpx.Response(200, json={"properties": properties})
        requested_details.append(params["url"][0])
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        await search_stays(api, spec(session_input))

    assert len(requested_details) == 2
    assert any("near-stadium" in url for url in requested_details)
    assert all("after-first-40" not in url for url in requested_details)


async def test_hotel_shortlist_prioritizes_named_private_bathroom_and_stops_on_success(
    session_input, settings
):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import search_stays

    search, details = data("hotels"), data("hotel-detail")
    base = search["properties"][0]
    search["properties"] = [
        {
            **base,
            "title": f"Hotel {index}",
            "link": f"https://www.booking.com/hotel/fr/candidate-{index}.html",
            "property_id": str(index),
            "extracted_price": 50 + index,
        }
        for index in range(1, 5)
    ]
    search["properties"][3]["room_type"] = details["rooms"][0]["name"]
    checks = []

    def respond(request):
        params = request.url.params
        if params["engine"] == "booking":
            return httpx.Response(200, json=search)
        checks.append(urlsplit(params["url"]).path)
        return httpx.Response(200, json=details)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        result = await search_stays(
            api,
            spec(session_input).model_copy(update={"private_room": True, "private_bathroom": True}),
        )

    assert checks == ["/hotel/fr/candidate-4.html", "/hotel/fr/candidate-1.html"]
    assert [(stay.property_name, stay.bathroom, stay.room_type) for stay in result.stays] == [
        ("Hotel 4", "private", "private")
    ]
    assert not result.gaps
    assert result.calls == 3


@pytest.mark.parametrize(
    ("limit", "private_at", "expected_checks"),
    [
        pytest.param(8, 3, 6, id="verified-in-fallback"),
        pytest.param(8, 6, 6, id="last-fallback-candidate"),
        pytest.param(8, 7, 6, id="maximum-six-checks"),
        pytest.param(5, 4, 3, id="one-call-left"),
        pytest.param(4, 4, 2, id="no-calls-left"),
    ],
)
async def test_private_bathroom_fallback_checks_only_remaining_ranked_candidates(
    session_input, settings, limit, private_at, expected_checks
):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import PRIVATE_BATHROOM_UNVERIFIED_GAP, search_stays

    search = data("hotels")
    base = search["properties"][0]
    search["filters"] = [{"options": [{"name": "Private bathroom", "value": "private-bath"}]}]
    search["properties"] = [
        {
            **base,
            "link": f"https://www.booking.com/hotel/fr/candidate-{index}.html",
            "property_id": str(index),
            "extracted_price": 50 + index,
            "amenities": ["Private bathroom"],
        }
        for index in range(1, 8)
    ]
    property_checks = []
    searches = []

    def respond(request):
        params = request.url.params
        if params["engine"] == "booking":
            searches.append(params.get("filters"))
            return httpx.Response(200, json=search)
        path = urlsplit(params["url"]).path
        property_checks.append(path)
        details = data("hotel-detail")
        if path == f"/hotel/fr/candidate-{private_at}.html":
            details["rooms"][1]["amenities"] = ["Private bathroom"]
        return httpx.Response(200, json=details)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client,
            settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")}),
            limit=limit,
        )
        result = await search_stays(
            api, spec(session_input).model_copy(update={"private_bathroom": True})
        )

    assert searches == [None, "private-bath"]
    assert property_checks == [
        f"/hotel/fr/candidate-{index}.html" for index in range(1, expected_checks + 1)
    ]
    assert result.calls == expected_checks + len(searches)
    if private_at <= expected_checks:
        assert [stay.bathroom for stay in result.stays] == ["private"]
        assert not result.gaps
    else:
        assert not result.stays
        assert result.gaps == (PRIVATE_BATHROOM_UNVERIFIED_GAP,)


def warsaw_spec(session_input):
    fixtures, routes = load_catalog()
    route = next(route for route in routes if route.id == "london-waw")
    fixture = next(fixture for fixture in fixtures if fixture.id == route.fixture_id)
    return spec(session_input).model_copy(
        update={
            "fixture": fixture,
            "route": route,
            "outbound_date": date(2026, 12, 9),
            "return_date": date(2026, 12, 11),
        }
    )


def warsaw_booking() -> dict:
    booking = data("flight-booking")
    booking["search_parameters"].update(outbound_date="2026-12-09", return_date="2026-12-11")
    legs = []

    def collect(node: object) -> None:
        if isinstance(node, dict):
            if "departure_airport" in node:
                legs.append(node)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(booking)
    outbound, inbound = legs
    outbound["departure_airport"]["date"] = outbound["arrival_airport"]["date"] = "2026-12-09"
    outbound["arrival_airport"]["id"] = inbound["departure_airport"]["id"] = "WAW"
    # An evening return leaves the required transfer time after the train back to Warsaw.
    inbound["departure_airport"].update(date="2026-12-11", time="20:55")
    inbound["arrival_airport"].update(date="2026-12-11", time="22:35")
    return booking


def warsaw_train(query: str) -> dict:
    outward = "from Warsaw" in query
    day = "2026-12-09" if outward else "2026-12-11"
    start, end = (
        ("Warsaw Central", "Zielone Wzgórza") if outward else ("Zielone Wzgórza", "Warsaw Central")
    )
    departure, arrival = (
        (f"{day}T15:00:00+01:00", f"{day}T17:30:00+01:00")
        if outward
        else (f"{day}T14:00:00+01:00", f"{day}T16:30:00+01:00")
    )
    return {
        "train_results": {
            "origin": "Warsaw" if outward else "Białystok",
            "destination": "Białystok" if outward else "Warsaw",
            "date": day,
            "trains": [
                {
                    "transfers": 0,
                    "time_window": {"depart_at_iso": departure, "arrive_at_iso": arrival},
                    "instructions": [
                        {
                            "depart_from": {"place": start, "at_iso": departure},
                            "arrive_at": {"place": end, "at_iso": arrival},
                            "service_provider": {
                                "name": "PKP Intercity",
                                "website": "https://www.intercity.pl/pl/",
                            },
                        }
                    ],
                }
            ],
        }
    }


async def test_bialystok_screens_flights_against_onward_trains(session_input, settings):
    requests: list[str] = []
    trains_requested = asyncio.Event()
    discovery = [
        listed("LGW", "WAW", "2026-12-09 05:00", "2026-12-09 09:30", "trap", layover=45),
        listed("LTN", "WAW", "2026-12-09 09:00", "2026-12-09 12:30", "tight"),
        listed("LGW", "WAW", "2026-12-09 08:15", "2026-12-09 10:55", "good"),
    ]
    returns = [
        listed("WAW", "LGW", "2026-12-11 17:00", "2026-12-11 18:40", "rushed", key="booking_token"),
        listed("WAW", "LGW", "2026-12-11 20:55", "2026-12-11 22:35", "ok", key="booking_token"),
    ]

    async def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params["engine"] == "google":
            requests.append("train")
            if requests.count("train") == 2:
                trains_requested.set()
            return httpx.Response(200, json=warsaw_train(params["q"]))
        if "booking_token" in params:
            requests.append(f"booking:{params['booking_token']}")
            return httpx.Response(200, json=warsaw_booking())
        if "departure_token" in params:
            requests.append(f"returns:{params['departure_token']}")
            return httpx.Response(200, json={"best_flights": returns})
        requests.append("discovery")
        await asyncio.wait_for(trains_requested.wait(), timeout=2)
        return httpx.Response(200, json={"best_flights": discovery})

    result = await run_flight_search(
        settings, httpx.MockTransport(respond), warsaw_spec(session_input)
    )
    assert sorted(requests[:3]) == ["discovery", "train", "train"]
    assert requests[3:] == ["returns:good", "booking:ok"]
    assert result.calls == 5
    assert result.journeys and result.journeys[0].outbound[-1].destination == "Zielone Wzgórza"
    assert result.journeys[0].inbound[0].origin == "Zielone Wzgórza"
    assert (result.listed, result.screened, result.followed) == (3, 1, 1)


async def test_failed_train_prefetch_after_empty_discovery_reports_no_flights(
    session_input, settings
):
    flight_searches = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["engine"] == "google":
            return httpx.Response(400)
        flight_searches.append(request.url.params["stops"])
        return httpx.Response(200, json={"error": "Google Flights didn't return any results."})

    result = await run_flight_search(
        settings, httpx.MockTransport(respond), warsaw_spec(session_input)
    )
    assert flight_searches == ["one_stop_or_fewer"]
    assert not result.journeys
    assert result.failure == "provider_empty"


@pytest.mark.parametrize("rail_timeout", [True, False])
async def test_unavailable_bialystok_rail_skips_flight_follow_ups_and_cancels_sibling(
    session_input, settings, rail_timeout
):
    from cpfc_trip.planner.providers import searchapi

    sibling_started = asyncio.Event()
    sibling_stopped = asyncio.Event()
    flight_requests = 0
    rail_requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal flight_requests, rail_requests
        params = request.url.params
        if params["engine"] == "google":
            rail_requests += 1
            if "from Warsaw" in params["q"]:
                await sibling_started.wait()
                if rail_timeout:
                    raise httpx.ReadTimeout("train search timed out")
                return httpx.Response(200, json={})
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                sibling_stopped.set()
        if "departure_token" in params or "booking_token" in params:
            pytest.fail("Flight details are unusable after the mandatory train search fails")
        flight_requests += 1
        good = listed("LGW", "WAW", "2026-12-09 08:15", "2026-12-09 10:55", "good")
        return httpx.Response(200, json={"best_flights": [good]})

    result = await run_flight_search(
        settings, httpx.MockTransport(respond), warsaw_spec(session_input)
    )
    assert not result.journeys
    assert result.failure == "onward_rail"
    assert result.gaps == (searchapi.BIALYSTOK_ONWARD_RAIL_GAP,)
    assert sibling_stopped.is_set()
    assert flight_requests == 1
    assert rail_requests == (3 if rail_timeout else 2)
    assert result.calls == flight_requests + rail_requests


async def test_cancelled_bialystok_search_drains_train_requests(session_input, settings):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers import searchapi

    started = 0
    all_started = asyncio.Event()
    cancelled = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal started, cancelled
        started += 1
        if started == 3:
            all_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        task = asyncio.create_task(searchapi.search_flights(api, warsaw_spec(session_input)))
        await asyncio.wait_for(all_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled == 3


async def test_bialystok_call_count_includes_rail_retry_started_during_cleanup(
    session_input, settings, monkeypatch
):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers import searchapi

    retry_pending = asyncio.Event()
    release_retry = asyncio.Event()
    requests = 0

    async def pause_retry(_delay):
        retry_pending.set()
        await release_retry.wait()

    monkeypatch.setattr(searchapi.asyncio, "sleep", pause_retry)

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        params = request.url.params
        if params["engine"] == "google":
            if "from Warsaw" in params["q"]:
                return httpx.Response(503)
            await asyncio.Future()
        await asyncio.wait_for(retry_pending.wait(), timeout=2)
        release_retry.set()
        return httpx.Response(200, json={"error": "Google Flights didn't return any results."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        result = await searchapi.search_flights(api, warsaw_spec(session_input))
    assert result.calls == requests


async def test_shortlisted_property_details_are_checked_together(session_input, settings):
    from pydantic import SecretStr

    from cpfc_trip.planner.providers.searchapi import search_stays

    search = data("hotels")
    prop = search["properties"][0]
    search["properties"] = [prop, {**prop, "property_id": "another-property"}]
    detail_requests = 0
    both_requested = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal detail_requests
        if request.url.params["engine"] == "booking":
            return httpx.Response(200, json=search)
        detail_requests += 1
        if detail_requests == 2:
            both_requested.set()
        # A sequential check would never send the second request while the first waits.
        await asyncio.wait_for(both_requested.wait(), timeout=2)
        return httpx.Response(200, json=data("hotel-detail"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        api = SearchApi(
            client, settings.model_copy(update={"searchapi_api_key": SecretStr("test-secret")})
        )
        batch = await search_stays(api, spec(session_input))
    assert batch.calls == 3
    assert len(batch.stays) == 2
