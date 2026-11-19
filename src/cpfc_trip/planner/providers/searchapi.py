"""Bounded SearchApi acquisition; raw JSON is discarded inside each Activity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from math import asin, cos, isfinite, radians, sin, sqrt
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from cpfc_trip.config import Settings
from cpfc_trip.domain import (
    Coordinates,
    Evidence,
    Journey,
    Leg,
    Offer,
    Quote,
    SearchBatch,
    SearchSpec,
    Stay,
)
from cpfc_trip.origins import UK_RAIL
from cpfc_trip.planner.links import flight_url, hotel_url, safe_url
from cpfc_trip.planner.planning import arrives_in_time, fits_match, leaves_after_match
from cpfc_trip.planner.prices import money

ENDPOINT = "https://www.searchapi.io/api/v1/search"
REQUEST_CAPS = {"flight": 8, "rail": 8, "stay": 8}
SEARCH_ACTIVITY_ATTEMPTS = 2
REQUEST_TIMEOUT_SECONDS = 20
# Live Booking.com property pages returned within 8.2 seconds in November 2026 checks.
# A hung page should not hold the whole stay search for the generic timeout. Google train
# results often take 10–20 seconds, so they keep the generic timeout.
ENGINE_TIMEOUT_SECONDS = {"booking_property": 12}
# Flight discovery and return lists answered within 6.1 seconds in recorded live runs
# (booking details take up to 17), so a hung list request is retried after 10 seconds.
FLIGHT_LIST_TIMEOUT_SECONDS = 10
BIALYSTOK_ONWARD_RAIL_GAP = (
    "Onward train times between Warsaw and Białystok could not be verified for these dates."
)
STAY_UNVERIFIED_GAP = "No dated room with verified identity was found."
PRIVATE_BATHROOM_UNVERIFIED_GAP = "No dated room with a verified private bathroom was found."


class ProviderError(Exception):
    """A sanitized, user-safe provider failure (never includes credentials or raw URLs)."""


class NoResultsError(ProviderError):
    """The provider explicitly found no results for this search."""


class RequestBudgetSpent(ProviderError):
    """This search used its request cap or deadline."""


THROTTLE_PAUSE_SECONDS = {429: 30, 402: 300}
MAX_THROTTLE_PAUSE_SECONDS = 900


def _pause_seconds(response: httpx.Response) -> float:
    header = response.headers.get("retry-after", "")
    seconds = float(header) if header.isdigit() else THROTTLE_PAUSE_SECONDS[response.status_code]
    return min(seconds, MAX_THROTTLE_PAUSE_SECONDS)


def fingerprint(params: object) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:24]


class SearchApi:
    paused_until = 0.0

    def __init__(self, client: httpx.AsyncClient, settings: Settings, limit: int = 8):
        self.client, self.settings, self.limit = client, settings, limit
        self.calls = 0
        self.deadline = asyncio.get_running_loop().time() + 80

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        if (
            not self.settings.searchapi_enabled
            or not self.settings.searchapi_api_key.get_secret_value()
        ):
            raise ProviderError("Travel search is currently unavailable")
        for attempt in range(2):
            if time.monotonic() < SearchApi.paused_until:
                raise ProviderError("Travel search is busy")
            remaining = self.deadline - asyncio.get_running_loop().time()
            if self.calls >= self.limit or remaining <= 0:
                raise RequestBudgetSpent("Search request limit or deadline reached")
            self.calls += 1
            started = time.monotonic()
            try:
                response = await self.client.get(
                    ENDPOINT,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.settings.searchapi_api_key.get_secret_value()}"
                    },
                    timeout=min(
                        FLIGHT_LIST_TIMEOUT_SECONDS
                        if params.get("engine") == "google_flights"
                        and "booking_token" not in params
                        else ENGINE_TIMEOUT_SECONDS.get(
                            str(params.get("engine")), REQUEST_TIMEOUT_SECONDS
                        ),
                        remaining,
                    ),
                )
                response.raise_for_status()
                logging.getLogger("cpfc_trip.provider").info(
                    "SearchApi request: engine=%s status=%s elapsed_ms=%d",
                    params.get("engine"),
                    response.status_code,
                    (time.monotonic() - started) * 1000,
                )
                if len(response.content) > 4_000_000:
                    raise ProviderError("Travel search response exceeded its size limit")
                data = response.json()
                if (
                    isinstance(data, dict)
                    and data.get("error") == "Google Flights didn't return any results."
                ):
                    raise NoResultsError("No flights found")
                if not isinstance(data, dict) or data.get("error") or data.get("errors"):
                    raise ProviderError("Travel search returned an error")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                # Engine/status only: never log request URLs, headers, bodies or secrets.
                logging.getLogger("cpfc_trip.provider").warning(
                    "SearchApi request failed: engine=%s error=%s status=%s elapsed_ms=%d",
                    params.get("engine"),
                    type(exc).__name__,
                    status,
                    (time.monotonic() - started) * 1000,
                )
                if isinstance(exc, httpx.HTTPStatusError) and status in THROTTLE_PAUSE_SECONDS:
                    SearchApi.paused_until = max(
                        SearchApi.paused_until, time.monotonic() + _pause_seconds(exc.response)
                    )
                    raise ProviderError("Travel search is busy") from None
                retryable = isinstance(exc, httpx.TransportError) or (
                    status is not None and status >= 500
                )
                if not retryable or attempt or self.calls >= self.limit:
                    raise ProviderError("Travel search did not return usable results") from None
                await asyncio.sleep(
                    min(0.25, max(0, self.deadline - asyncio.get_running_loop().time()))
                )
        raise ProviderError("Travel search did not return usable results")


def evidence(data: dict[str, Any], source: str, checked: datetime) -> Evidence:
    metadata = data.get("search_metadata", {})
    value = metadata.get("request_url", "")
    return Evidence(
        underlying_source=source,
        retrieved_at=checked,
        source_url=safe_url(value) if value else None,
    )


def offer(
    data: dict[str, Any],
    source: str,
    params: object,
    checked: datetime,
    url: str | None,
    seller: str | None = None,
    key: str = "",
) -> Offer:
    fid = fingerprint(params)
    return Offer(
        id=f"{fid}-{key}",
        booking_url=url,
        seller=seller,
        evidence=evidence(data, source, checked),
        search_fingerprint=fid,
    )


def local_time(point: dict[str, Any], timezone: str) -> datetime:
    return datetime.fromisoformat(f"{point['date']}T{point['time']}").replace(
        tzinfo=ZoneInfo(timezone)
    )


def normalize_flights(data: dict[str, Any], spec: SearchSpec, checked: datetime) -> SearchBatch:
    selected = data.get("selected_flights", [])
    if len(selected) != 2:
        return SearchBatch(gaps=("Complete outbound and return flights were not available.",))
    params = data.get("search_parameters", {})
    if params.get("outbound_date") != str(spec.outbound_date) or params.get("return_date") != str(
        spec.return_date
    ):
        return SearchBatch(gaps=("Returned flight dates did not match the requested dates.",))
    url = flight_url(data.get("search_metadata", {}).get("request_url", ""))
    ref = offer(data, "Google Flights", params, checked, url, "Google Flights")
    options = data.get("booking_options", [])
    value = options[0] if options else {}
    amount = money(value.get("price"), params.get("currency", ""))
    matched = (
        str(params.get("adults")) == str(spec.party.adults)
        and int(params.get("children", 0)) == len(spec.party.child_ages)
        and not any(a < 2 for a in spec.party.child_ages)
    )
    quote = (
        Quote(
            id=ref.id,
            amount=amount,
            scope="round_trip",
            unit="party",
            priced_party=spec.party if matched else None,
            observed_at=checked,
            caveats=("Rounded planning price; baggage and optional charges may be extra.",),
        )
        if amount
        else None
    )
    legs: list[tuple[Leg, ...]] = []
    for index, direction in enumerate(selected):
        flights = direction.get("flights", [])
        if not 1 <= len(flights) <= 2:
            return SearchBatch(gaps=("Only nonstop and one-stop flights are supported.",))
        dep, arr = flights[0]["departure_airport"], flights[-1]["arrival_airport"]
        origin_airports = spec.route.origin.split(",")
        if index == 0:
            valid = dep["id"] in origin_airports and arr["id"] == spec.route.destination
            dep_tz, arr_tz = "Europe/London", spec.route.destination_timezone
        else:
            valid = dep["id"] == spec.route.destination and arr["id"] in origin_airports
            dep_tz, arr_tz = spec.route.destination_timezone, "Europe/London"
        if not valid or dep.get("date") != str(
            spec.outbound_date if index == 0 else spec.return_date
        ):
            return SearchBatch(gaps=("Returned airports did not match the catalog route.",))
        caveats = ["Select and confirm this itinerary on Google Flights."]
        if len(flights) == 2:
            connection_arrival = flights[0]["arrival_airport"]
            connection_departure = flights[1]["departure_airport"]
            connection = connection_arrival.get("id")
            durations = [flight.get("duration") for flight in flights]
            if (
                not connection
                or connection != connection_departure.get("id")
                or any(not isinstance(duration, int) or duration <= 0 for duration in durations)
            ):
                return SearchBatch(gaps=("The flight connection could not be verified.",))
            arrival_local = datetime.fromisoformat(
                f"{connection_arrival['date']}T{connection_arrival['time']}"
            )
            departure_local = datetime.fromisoformat(
                f"{connection_departure['date']}T{connection_departure['time']}"
            )
            first_arrival = local_time(dep, dep_tz).astimezone(UTC) + timedelta(
                minutes=durations[0]
            )
            second_departure = local_time(arr, arr_tz).astimezone(UTC) - timedelta(
                minutes=durations[1]
            )
            layover_minutes = round((second_departure - first_arrival).total_seconds() / 60)
            reported = direction.get("layovers") or []
            if (
                departure_local <= arrival_local
                or not 60 <= layover_minutes <= 480
                or len(reported) != 1
                or reported[0].get("id") != connection
                or not isinstance(reported[0].get("duration"), int)
                or abs(reported[0]["duration"] - layover_minutes) > 10
            ):
                return SearchBatch(gaps=("The flight connection could not be verified.",))
            caveats.insert(
                0,
                f"One stop via {connection} ({layover_minutes} minutes). Confirm the connection, baggage and terminal details before booking.",
            )
        legs.append(
            (
                Leg(
                    id=f"{ref.id}-{index}",
                    mode="flight",
                    origin=dep["id"],
                    destination=arr["id"],
                    departs_at=local_time(dep, dep_tz),
                    arrives_at=local_time(arr, arr_tz),
                    operator=" / ".join(dict.fromkeys(flight["airline"] for flight in flights)),
                    service_number=" / ".join(
                        flight.get("flight_number", "") for flight in flights
                    ),
                    offer=ref,
                    quote=quote,
                    caveats=tuple(caveats),
                ),
            )
        )
    return SearchBatch(
        journeys=(Journey(id=ref.id, route_id=spec.route.id, outbound=legs[0], inbound=legs[1]),)
    )


WarsawTrains = tuple[tuple[Leg, ...], tuple[Leg, ...]]


class _OnwardRailUnavailable(Exception):
    """A mandatory direction has no usable rail evidence."""


def _onward_to_bialystok(spec: SearchSpec) -> bool:
    return spec.route.onward_stations == ("Warsaw", "Białystok")


async def _warsaw_trains(api: SearchApi, spec: SearchSpec) -> WarsawTrains:
    async def fetch(origin: str, destination: str, day: date) -> tuple[Leg, ...]:
        try:
            data = await api.get(
                {
                    "engine": "google",
                    "q": f"trains from {origin} to {destination} on {day:%d %B %Y}",
                    "gl": "uk",
                    "hl": "en",
                }
            )
            legs = normalize_trains(data, origin, destination, str(day), datetime.now(UTC))
        except (ProviderError, KeyError, ValueError):
            raise _OnwardRailUnavailable from None
        if not legs:
            raise _OnwardRailUnavailable
        return legs

    try:
        async with asyncio.TaskGroup() as group:
            outward = group.create_task(fetch("Warsaw", "Białystok", spec.outbound_date))
            inbound = group.create_task(fetch("Białystok", "Warsaw", spec.return_date))
    except* _OnwardRailUnavailable:
        raise _OnwardRailUnavailable from None
    return outward.result(), inbound.result()


MAX_FLIGHT_FOLLOW_UPS = 3
FOLLOW_UP_WIDTH = 2
RETURNS_PER_OUTBOUND = 2
FOLLOW_UP_SECONDS = 25
MAX_ONWARD_WAIT = timedelta(hours=8)


def _listed_flights(item: dict[str, Any]) -> list[dict[str, Any]]:
    flights = item.get("flights")
    return flights if isinstance(flights, list) else []


def _connection_supported(item: dict[str, Any]) -> bool:
    flights = _listed_flights(item)
    if len(flights) == 1:
        return True
    if len(flights) != 2:
        return False
    try:
        if flights[0]["arrival_airport"]["id"] != flights[1]["departure_airport"]["id"]:
            return False
    except (KeyError, TypeError):
        return False
    layovers = item.get("layovers")
    if not layovers:
        return True
    duration = layovers[0].get("duration") if len(layovers) == 1 else None
    return isinstance(duration, int) and 60 <= duration <= 480


def _outbound_fits(item: dict[str, Any], spec: SearchSpec, trains: tuple[Leg, ...]) -> bool:
    flights = _listed_flights(item)
    if not flights:
        return False
    try:
        departure, arrival = flights[0]["departure_airport"], flights[-1]["arrival_airport"]
        if (
            departure.get("id") not in spec.route.origin.split(",")
            or arrival.get("id") != spec.route.destination
            or departure.get("date") != str(spec.outbound_date)
        ):
            return False
        arrives = local_time(arrival, spec.route.destination_timezone)
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
    if not _onward_to_bialystok(spec):
        return arrives_in_time(arrives, spec.fixture, spec.route)
    minimum = timedelta(minutes=spec.route.minimum_transfer_minutes)
    return any(
        train.origin == "Warsaw Central"
        and minimum <= train.departs_at - arrives <= MAX_ONWARD_WAIT
        and train.departs_at.astimezone(arrives.tzinfo).date() == arrives.date()
        and arrives_in_time(train.arrives_at, spec.fixture, spec.route)
        for train in trains
    )


def _return_fits(item: dict[str, Any], spec: SearchSpec, trains: tuple[Leg, ...]) -> bool:
    flights = _listed_flights(item)
    if not flights or not item.get("booking_token"):
        return False
    try:
        departure, arrival = flights[0]["departure_airport"], flights[-1]["arrival_airport"]
        if (
            departure.get("id") != spec.route.destination
            or arrival.get("id") not in spec.route.origin.split(",")
            or departure.get("date") != str(spec.return_date)
        ):
            return False
        leaves = local_time(departure, spec.route.destination_timezone)
        lands = local_time(arrival, "Europe/London")
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
    if spec.latest_return and lands > spec.latest_return:
        return False
    if not _onward_to_bialystok(spec):
        return leaves_after_match(leaves, spec.fixture, spec.route)
    minimum = timedelta(minutes=spec.route.minimum_transfer_minutes)
    return any(
        train.origin == "Zielone Wzgórza"
        and minimum <= leaves - train.arrives_at <= MAX_ONWARD_WAIT
        and train.arrives_at.astimezone(leaves.tzinfo).date() == leaves.date()
        and leaves_after_match(train.departs_at, spec.fixture, spec.route)
        for train in trains
    )


def _flight_gap(spec: SearchSpec, failure: str) -> str:
    onward = _onward_to_bialystok(spec)
    if failure == "provider_empty":
        return "No flights were listed for this route and date pair."
    if failure == "unsupported_connection":
        return (
            "Listed flights that fit the match change airports, have more than one stop or "
            "have a very short or long layover, so they cannot be checked."
        )
    if failure == "no_timely_flight":
        return (
            "No listed flight connects with a Warsaw–Białystok train in time for the match "
            "and back afterwards."
            if onward
            else "No listed flight arrives in time for the match and returns after it."
        )
    if failure == "search_limits":
        return "No complete flight was verified within the search limits."
    return (
        "No complete flight and Warsaw–Białystok train connection was verified."
        if onward
        else "No complete flight result was verified."
    )


async def search_flights(api: SearchApi, spec: SearchSpec) -> SearchBatch:
    if any(a < 2 for a in spec.party.child_ages):
        return SearchBatch(gaps=("Infant flight pricing is not yet supported.",))
    onward = _onward_to_bialystok(spec)
    onward_rail = asyncio.create_task(_warsaw_trains(api, spec)) if onward else None
    p = dict(
        engine="google_flights",
        departure_id=spec.route.origin,
        arrival_id=spec.route.destination,
        outbound_date=str(spec.outbound_date),
        return_date=str(spec.return_date),
        flight_type="round_trip",
        stops="one_stop_or_fewer",
        adults=spec.party.adults,
        children=len(spec.party.child_ages),
        currency="GBP",
        gl="GB",
        hl="en",
    )
    listed: list[dict[str, Any]] = []
    screened: list[dict[str, Any]] = []
    followed = 0
    journeys: list[Journey] = []

    def batch(failure: str | None) -> SearchBatch:
        return SearchBatch.model_validate(
            {
                "journeys": journeys,
                "calls": api.calls,
                "failure": failure,
                "gaps": (_flight_gap(spec, failure),) if failure else (),
                "listed": len(listed),
                "screened": len(screened),
                "followed": followed,
            }
        )

    async def search() -> SearchBatch:
        nonlocal listed, screened, followed
        try:
            initial = await api.get(p)
        except NoResultsError:
            return batch("provider_empty")
        except RequestBudgetSpent:
            return batch("search_limits")
        listed = [
            item
            for item in initial.get("best_flights", []) + initial.get("other_flights", [])
            if item.get("departure_token")
        ]
        if not listed:
            return batch("provider_empty")
        out_trains, in_trains = await onward_rail if onward_rail is not None else ((), ())
        timely = [item for item in listed if _outbound_fits(item, spec, out_trains)]
        screened = sorted(
            (item for item in timely if _connection_supported(item)),
            key=lambda item: len(_listed_flights(item)),
        )
        if not screened:
            return batch("unsupported_connection" if timely else "no_timely_flight")
        loop = asyncio.get_running_loop()
        stop_at = min(api.deadline, loop.time() + FOLLOW_UP_SECONDS)
        rejected: list[str] = []

        unsupported_returns = False
        provider_failed = False
        truncated = False

        async def returns_for(item: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal unsupported_returns, provider_failed, truncated
            try:
                returns = await api.get({**p, "departure_token": item["departure_token"]})
            except RequestBudgetSpent:
                truncated = True
                return []
            except (NoResultsError, KeyError, ValueError):
                return []
            except ProviderError:
                provider_failed = True
                return []
            back = returns.get("best_flights", []) + returns.get("other_flights", [])
            if not back:
                back = returns.get("flights", [])
            fitting = [option for option in back if _return_fits(option, spec, in_trains)]
            usable = [option for option in fitting if _connection_supported(option)]
            unsupported_returns = unsupported_returns or len(usable) < len(fitting)
            return sorted(usable, key=lambda option: len(_listed_flights(option)))

        async def verify(option: dict[str, Any]) -> list[Journey]:
            nonlocal provider_failed, truncated
            try:
                selected = await api.get({**p, "booking_token": option["booking_token"]})
                result = normalize_flights(selected, spec, datetime.now(UTC))
            except RequestBudgetSpent:
                truncated = True
                return []
            except (NoResultsError, KeyError, TypeError, ValueError):
                return []
            except ProviderError:
                provider_failed = True
                return []
            rejected.extend(result.gaps)
            verified = (
                [
                    journey
                    for flight in result.journeys
                    for journey in connect_onward_rail(
                        flight, out_trains, in_trains, spec.route.minimum_transfer_minutes
                    )
                ]
                if onward
                else list(result.journeys)
            )
            return [
                journey
                for journey in verified
                if fits_match(journey, spec.fixture, spec.route)
                and not (spec.latest_return and journey.inbound[-1].arrives_at > spec.latest_return)
            ]

        returns_fit = False
        candidates = screened[:MAX_FLIGHT_FOLLOW_UPS]
        for start in range(0, len(candidates), FOLLOW_UP_WIDTH):
            group = candidates[start : start + FOLLOW_UP_WIDTH][: (api.limit - api.calls) // 2]
            if not group or loop.time() >= stop_at:
                break
            followed += len(group)
            option_lists = await asyncio.gather(*(returns_for(item) for item in group))
            returns_fit = returns_fit or any(option_lists)
            if any(len(options) > RETURNS_PER_OUTBOUND for options in option_lists):
                truncated = True
            for rank in range(RETURNS_PER_OUTBOUND):
                tries = [options[rank] for options in option_lists if len(options) > rank]
                if not tries:
                    break
                available = api.limit - api.calls
                if loop.time() >= stop_at:
                    truncated = True
                    break
                if len(tries) > available:
                    truncated = True
                    tries = tries[:available]
                if not tries:
                    break
                for found in await asyncio.gather(*(verify(option) for option in tries)):
                    if found:
                        journeys.extend(found)
                        return batch(None)
        result = batch(
            "provider_error"
            if provider_failed
            else "search_limits"
            if truncated or followed < len(screened)
            else "unverified"
            if returns_fit
            else "unsupported_connection"
            if unsupported_returns
            else "no_timely_flight"
        )
        return result.model_copy(
            update={"gaps": tuple(dict.fromkeys((*result.gaps, *rejected)))[:4]}
        )

    try:
        result = await search()
    except _OnwardRailUnavailable:
        result = batch("onward_rail").model_copy(update={"gaps": (BIALYSTOK_ONWARD_RAIL_GAP,)})
    finally:
        if onward_rail is not None:
            onward_rail.cancel()
            # Retrieve the outcome so a failed or cancelled prefetch is never left dangling.
            await asyncio.gather(onward_rail, return_exceptions=True)
    return result.model_copy(update={"calls": api.calls})


PRIVATE_BATHROOM_WORDS = ("private bathroom", "en-suite", "ensuite", "en suite")


def _mentions_private_bathroom(text: str) -> bool:
    lowered = text.casefold()
    return any(words in lowered for words in PRIVATE_BATHROOM_WORDS)


def normalize_stay(
    prop: dict[str, Any],
    details: dict[str, Any],
    search: dict[str, Any],
    spec: SearchSpec,
    checked: datetime,
) -> Stay | None:
    for response in (search, details):
        parameters = response.get("search_parameters", {})
        if parameters.get("check_in_date") != str(spec.outbound_date) or parameters.get(
            "check_out_date"
        ) != str(spec.return_date):
            return None
        if (
            parameters.get("adults") != spec.party.adults
            or parameters.get("rooms") != spec.party.rooms
        ):
            return None
    if prop.get("city", "").casefold() != spec.fixture.city.casefold():
        return None
    name = prop.get("room_type", "")
    room = next((x for x in details.get("rooms", []) if x.get("name") == name), None)
    if room is None:
        return None
    url = hotel_url(
        prop.get("link", ""), str(spec.outbound_date), str(spec.return_date), spec.party
    )
    if not url:
        return None
    ref = offer(
        search,
        "Booking.com",
        spec.model_dump(mode="json"),
        checked,
        url,
        "Booking.com",
        str(prop.get("property_id", "")),
    )
    summary = prop.get("stay_summary", "")
    nights = (spec.return_date - spec.outbound_date).days
    # Only the tested adult/one-room shape establishes group scope. Others remain observations.
    verified = (
        not spec.party.child_ages
        and spec.party.rooms == 1
        and summary
        == f"{nights} {'night' if nights == 1 else 'nights'}, {spec.party.adults} {'adult' if spec.party.adults == 1 else 'adults'}"
    )
    amount = money(prop.get("extracted_price"), prop.get("currency", ""))
    taxes = money(prop.get("extracted_taxes_and_charges"), prop.get("currency", ""))
    quote = (
        Quote(
            id=ref.id,
            amount=amount,
            scope="stay",
            unit="party" if verified else "unknown",
            priced_party=spec.party if verified else None,
            observed_at=checked,
            taxes="excluded" if taxes else "unknown",
            additional_taxes=taxes,
            caveats=("Search price is rounded. Confirm exact total and cancellation terms.",),
        )
        if amount
        else None
    )
    is_dorm = "dorm" in name.lower() or "bed in" in name.lower()
    room_facts = json.dumps(room.get("amenities", []), ensure_ascii=False).lower()
    bathroom = (
        "private"
        if _mentions_private_bathroom(name) or _mentions_private_bathroom(room_facts)
        else "shared"
        if "shared bathroom" in name.lower() or "shared bathroom" in room_facts
        else "unknown"
    )
    if (spec.private_room and is_dorm) or (spec.private_bathroom and bathroom != "private"):
        return None
    return Stay(
        id=ref.id,
        property_name=prop["title"],
        check_in=spec.outbound_date,
        check_out=spec.return_date,
        room_description=name,
        offer=ref,
        quote=quote,
        bathroom=bathroom,
        room_type="dorm" if is_dorm else "private",
        # Dorm metadata describes one bed's capacity, not the requested party's allocation.
        # The dated search summary establishes party price; bed allocation needs reconfirmation.
        max_guests=None if is_dorm else room.get("max_guests"),
        review_score=prop.get("rating"),
        caveats=(
            "Property and room must be reselected on Booking.com.",
            "Venue access, late check-in and post-match transfers need confirmation.",
        )
        + (("Confirm the number and allocation of dorm beds for your party.",) if is_dorm else ()),
    )


def _stadium_distance_km(raw: object, stadium: Coordinates) -> float | None:
    if not isinstance(raw, dict):
        return None
    latitude, longitude = raw.get("latitude"), raw.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    if not (isfinite(latitude) and isfinite(longitude)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    latitude_delta = radians(latitude - stadium.latitude)
    longitude_delta = radians(longitude - stadium.longitude)
    arc = (
        sin(latitude_delta / 2) ** 2
        + cos(radians(stadium.latitude)) * cos(radians(latitude)) * sin(longitude_delta / 2) ** 2
    )
    return 6371 * 2 * asin(sqrt(min(arc, 1)))


async def search_stays(api: SearchApi, spec: SearchSpec) -> SearchBatch:
    p: dict[str, Any] = dict(
        engine="booking",
        q=spec.fixture.city,
        check_in_date=str(spec.outbound_date),
        check_out_date=str(spec.return_date),
        adults=spec.party.adults,
        rooms=spec.party.rooms,
        currency="GBP",
        language="en-gb",
        sort_by="price_low_to_high",
    )
    if spec.party.child_ages:
        # Engine-specific child parameter contract must be proven before quoting children.
        return SearchBatch(
            gaps=("Accommodation prices for children need a direct occupancy check.",)
        )
    search = await api.get(p)
    if spec.private_bathroom:
        token = next(
            (
                o.get("value")
                for group in search.get("filters", [])
                for o in group.get("options", [])
                if o.get("name", "").casefold() == "private bathroom"
            ),
            None,
        )
        if token:
            p["filters"] = token
            search = await api.get(p)
    properties = [
        prop
        for prop in search.get("properties", [])[:40]
        if prop.get("city", "").casefold() == spec.fixture.city.casefold()
        and not (
            spec.private_room
            and any(x in prop.get("room_type", "").lower() for x in ("dorm", "bed in"))
        )
    ]
    quality_weight = {"budget": 0, "value": 1, "comfort": 4}[spec.budget_tier]
    distance_weight = {"budget": 2, "value": 6, "comfort": 10}[spec.budget_tier]
    stadium = spec.fixture.venue_coordinates

    def property_rank(prop: dict[str, Any]) -> float:
        price = prop.get("extracted_price")
        cost = float(price) if isinstance(price, (float, int)) else 10000
        dorm = any(x in prop.get("room_type", "").lower() for x in ("dorm", "bed in"))
        rating = prop.get("rating") or 5
        penalty = (150 if dorm else 0) + (10 - float(rating)) * 25
        distance = _stadium_distance_km(prop.get("gps_coordinates"), stadium) if stadium else None
        distance_penalty = (
            min(distance if distance is not None else 25, 25) * distance_weight if stadium else 0
        )
        return cost + penalty * quality_weight + distance_penalty

    properties.sort(
        key=lambda prop: (
            spec.private_bathroom and not _mentions_private_bathroom(prop.get("room_type", "")),
            property_rank(prop),
        )
    )

    provider_failed = False

    async def check(prop: dict[str, Any]) -> Stay | None:
        nonlocal provider_failed
        url = hotel_url(
            prop.get("link", ""), str(spec.outbound_date), str(spec.return_date), spec.party
        )
        if not url:
            return None
        try:
            details = await api.get(
                {
                    k: v
                    for k, v in {**p, "engine": "booking_property", "url": url}.items()
                    if k not in {"q", "sort_by"}
                }
            )
            return normalize_stay(prop, details, search, spec, datetime.now(UTC))
        except (RequestBudgetSpent, NoResultsError, KeyError, ValueError):
            # One unavailable property must not erase already checked rooms.
            return None
        except ProviderError:
            provider_failed = True
            return None

    async def check_shortlist(shortlist: list[dict[str, Any]]) -> tuple[Stay, ...]:
        async with asyncio.TaskGroup() as group:
            checks = [group.create_task(check(prop)) for prop in shortlist]
        return tuple(stay for task in checks if (stay := task.result()))

    stays = await check_shortlist(properties[:2])
    if (
        spec.private_bathroom
        and not stays
        and api.calls < api.limit
        and asyncio.get_running_loop().time() < api.deadline
    ):
        stays = await check_shortlist(properties[2 : 2 + min(4, api.limit - api.calls)])
    gap = PRIVATE_BATHROOM_UNVERIFIED_GAP if spec.private_bathroom else STAY_UNVERIFIED_GAP
    return SearchBatch(
        stays=stays,
        calls=api.calls,
        gaps=() if stays else (gap,),
        failure="provider_error" if provider_failed and not stays else None,
    )


def normalize_trains(
    data: dict[str, Any], origin: str, destination: str, day: str, checked: datetime
) -> tuple[Leg, ...]:
    block = data.get("train_results") or {}
    if (
        block.get("date") != day
        or block.get("origin") != origin
        or block.get("destination") != destination
    ):
        return ()
    legs = []
    for train in block.get("trains", [])[:48]:
        window = train.get("time_window", {})
        instructions = train.get("instructions", [])
        if train.get("transfers") != 0 or len(instructions) != 1:
            continue
        instruction = instructions[0]
        stations = {
            ("London", "Paris"): ("St Pancras International", "Gare du Nord"),
            ("Paris", "London"): ("Gare du Nord", "St Pancras International"),
            ("Paris", "Lyon"): ("Gare de Lyon", "Lyon Part Dieu"),
            ("Lyon", "Paris"): ("Lyon Part Dieu", "Gare de Lyon"),
            ("Warsaw", "Białystok"): ("Warsaw Central", "Zielone Wzgórza"),
            ("Białystok", "Warsaw"): ("Zielone Wzgórza", "Warsaw Central"),
        }.get((origin, destination))
        for provider_city, departure_station, london_station in UK_RAIL.values():
            if (origin, destination) == (provider_city, "London"):
                stations = (departure_station, london_station)
            elif (origin, destination) == ("London", provider_city):
                stations = (london_station, departure_station)
        if (
            not stations
            or (
                instruction.get("depart_from", {}).get("place"),
                instruction.get("arrive_at", {}).get("place"),
            )
            != stations
        ):
            continue
        offers = train.get("buy_ticket", {}).get("offers", [])
        selected = next((x for x in offers if safe_url(x.get("link", ""))), None)
        operator_url = instruction.get("service_provider", {}).get("website", "")
        unpriced_warsaw = stations in {
            ("Warsaw Central", "Zielone Wzgórza"),
            ("Zielone Wzgórza", "Warsaw Central"),
        } and safe_url(operator_url)
        if not selected and not unpriced_warsaw:
            continue
        try:
            dep = datetime.fromisoformat(window["depart_at_iso"])
            arr = datetime.fromisoformat(window["arrive_at_iso"])
            if dep.date().isoformat() != day or dep.tzinfo is None or arr.tzinfo is None:
                continue
            if (
                datetime.fromisoformat(instruction["depart_from"]["at_iso"]) != dep
                or datetime.fromisoformat(instruction["arrive_at"]["at_iso"]) != arr
            ):
                continue
            ref = offer(
                data,
                "Google train results",
                (origin, destination, day, str(dep)),
                checked,
                safe_url(selected["link"]) if selected else safe_url(operator_url),
                selected.get("source")
                if selected
                else instruction.get("service_provider", {}).get("name"),
            )
            amount = (
                money(selected.get("extracted_price"), selected.get("currency", ""))
                if selected
                else None
            )
            quote = (
                Quote(
                    id=ref.id,
                    amount=amount,
                    scope="leg",
                    unit="person",
                    priced_party=None,
                    observed_at=checked,
                    caveats=(
                        "Single-person indicative fare; party, railcards and payable currency unverified.",
                    ),
                )
                if amount
                else None
            )
            legs.append(
                Leg(
                    id=ref.id,
                    mode="rail",
                    origin=stations[0],
                    destination=stations[1],
                    departs_at=dep,
                    arrives_at=arr,
                    operator=instruction.get("service_provider", {}).get("name", "Rail"),
                    service_number=instruction.get("line_number", ""),
                    offer=ref,
                    quote=quote,
                    caveats=(
                        "Direct service with station and time evidence. Confirm the selected fare and passengers."
                        if selected
                        else "Dated train time found; fare unavailable. Choose and confirm this train with the operator.",
                    ),
                )
            )
        except (KeyError, ValueError, ValidationError):
            continue
    return tuple(legs)


def connect_onward_rail(
    flight: Journey, outward: tuple[Leg, ...], inbound: tuple[Leg, ...], minutes: int
) -> tuple[Journey, ...]:
    """Join a WAW flight to reviewed direct trains, leaving time to change stations."""
    if flight.outbound[-1].destination != "WAW" or flight.inbound[0].origin != "WAW":
        return ()
    journeys = []
    minimum = timedelta(minutes=minutes)
    maximum = timedelta(hours=8)
    for out in outward:
        if (out.origin, out.destination) != ("Warsaw Central", "Zielone Wzgórza"):
            continue
        out_wait = out.departs_at - flight.outbound[-1].arrives_at
        if not (
            minimum <= out_wait <= maximum
            and out.departs_at.date() == flight.outbound[-1].arrives_at.date()
        ):
            continue
        for back in inbound:
            if (back.origin, back.destination) != ("Zielone Wzgórza", "Warsaw Central"):
                continue
            back_wait = flight.inbound[0].departs_at - back.arrives_at
            if not (
                minimum <= back_wait <= maximum
                and back.arrives_at.date() == flight.inbound[0].departs_at.date()
            ):
                continue
            journeys.append(
                Journey(
                    id=fingerprint((flight.id, out.id, back.id)),
                    route_id=flight.route_id,
                    outbound=(*flight.outbound, out),
                    inbound=(back, *flight.inbound),
                )
            )
    journeys.sort(
        key=lambda j: (
            j.outbound[-1].arrives_at - j.outbound[0].departs_at,
            j.inbound[-1].arrives_at - j.inbound[0].departs_at,
        )
    )
    return tuple(journeys[:8])


def connect_rail(
    first: tuple[Leg, ...], second: tuple[Leg, ...], minutes: int
) -> list[tuple[Leg, Leg]]:
    pairs = [
        (a, b)
        for a in first
        for b in second
        if (a.destination, b.origin)
        in {("Gare du Nord", "Gare de Lyon"), ("Gare de Lyon", "Gare du Nord")}
        and a.arrives_at.date() == b.departs_at.date()
        and timedelta(minutes=minutes) <= b.departs_at - a.arrives_at <= timedelta(hours=5)
        and 7 <= a.arrives_at.astimezone(ZoneInfo("Europe/Paris")).hour < 21
        and b.departs_at.astimezone(ZoneInfo("Europe/Paris")).hour <= 21
    ]
    pairs.sort(key=lambda pair: (pair[-1].arrives_at - pair[0].departs_at, pair[0].departs_at))
    return pairs[:8]


def london_connection(first: Leg, second: Leg, minutes: int) -> bool:
    """Require a same-day, buffered transfer between a UK terminal and Eurostar."""
    terminals = {station for _, _, station in UK_RAIL.values()}
    return (
        (
            (first.destination in terminals and second.origin == "St Pancras International")
            or (first.destination == "St Pancras International" and second.origin in terminals)
        )
        and first.arrives_at.date() == second.departs_at.date()
        and timedelta(minutes=minutes) <= second.departs_at - first.arrives_at <= timedelta(hours=8)
    )


async def search_trains(api: SearchApi, spec: SearchSpec) -> SearchBatch:
    if spec.route.id != "london-paris-lyon" and spec.route.origin not in UK_RAIL:
        return SearchBatch(gaps=("This rail pattern has not passed station-transfer review.",))
    segments = [
        ("London", "Paris", spec.outbound_date),
        ("Paris", "Lyon", spec.outbound_date),
        ("Lyon", "Paris", spec.return_date),
        ("Paris", "London", spec.return_date),
    ]
    if spec.route.origin in UK_RAIL:
        provider_city = UK_RAIL[spec.route.origin][0]
        segments = [
            (provider_city, "London", spec.outbound_date),
            *segments,
            ("London", provider_city, spec.return_date),
        ]
    semaphore = asyncio.Semaphore(3)

    async def fetch(origin: str, destination: str, day: datetime | date) -> tuple[Leg, ...]:
        async with semaphore:
            data = await api.get(
                dict(
                    engine="google",
                    q=f"trains from {origin} to {destination} on {day:%d %B %Y}",
                    gl="uk",
                    hl="en",
                )
            )
        return normalize_trains(data, origin, destination, str(day), datetime.now(UTC))

    legs = await asyncio.gather(*(fetch(*segment) for segment in segments))
    if len(legs) == 4:
        outbound: list[tuple[Leg, ...]] = [
            tuple(pair)
            for pair in connect_rail(legs[0], legs[1], spec.route.minimum_transfer_minutes)
        ]
        inbound: list[tuple[Leg, ...]] = [
            tuple(pair)
            for pair in connect_rail(legs[2], legs[3], spec.route.minimum_transfer_minutes)
        ]
    else:
        paris_out = connect_rail(legs[1], legs[2], spec.route.minimum_transfer_minutes)
        paris_in = connect_rail(legs[3], legs[4], spec.route.minimum_transfer_minutes)
        outbound = list[tuple[Leg, ...]](
            (domestic, *pair)
            for domestic in legs[0]
            for pair in paris_out
            if london_connection(domestic, pair[0], spec.route.minimum_transfer_minutes)
        )[:8]
        inbound = list[tuple[Leg, ...]](
            (*pair, domestic)
            for pair in paris_in
            for domestic in legs[5]
            if london_connection(pair[-1], domestic, spec.route.minimum_transfer_minutes)
        )[:8]
    combinations = [(a, b) for a in outbound for b in inbound]
    combinations.sort(
        key=lambda pair: sum((x[-1].arrives_at - x[0].departs_at).total_seconds() for x in pair)
    )
    journeys = tuple(
        Journey(
            id=fingerprint(tuple(x.id for x in (*a, *b))),
            route_id=spec.route.id,
            outbound=a,
            inbound=b,
        )
        for a, b in combinations[:12]
    )
    return SearchBatch(
        journeys=journeys,
        calls=api.calls,
        gaps=()
        if journeys
        else ("No dated rail connection meets the reviewed London and Paris transfer buffers.",),
    )


async def acquire(spec: SearchSpec, mode: str) -> SearchBatch:
    if not spec.route.enabled:
        return SearchBatch(gaps=("This catalog pattern has not passed its evidence review.",))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        api = SearchApi(client, Settings(), limit=REQUEST_CAPS[mode])
        try:
            async with asyncio.timeout(80):
                if mode == "stay":
                    return await search_stays(api, spec)
                if mode == "flight":
                    return await search_flights(api, spec)
                return await search_trains(api, spec)
        except (ProviderError, KeyError, ValueError, TimeoutError):
            return SearchBatch(
                gaps=("Travel evidence is currently incomplete; please check the provider.",),
                calls=api.calls,
                failure="provider_error",
            )
