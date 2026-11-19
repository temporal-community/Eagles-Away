from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

SEARCHAPI_HOST = "www.searchapi.io"
SEARCHAPI_PATH = "/api/v1/search"
EXPLICIT_EMPTY = {"error": "Google Flights didn't return any results."}
LATENCY = {"google_flights": 1.2, "google": 1.0, "booking": 2.0, "booking_property": 1.5}

AIRPORTS: dict[str, tuple[str, str]] = {
    "LHR": ("Europe/London", "Heathrow Airport"),
    "LGW": ("Europe/London", "London Gatwick Airport"),
    "STN": ("Europe/London", "London Stansted Airport"),
    "LTN": ("Europe/London", "London Luton Airport"),
    "LPL": ("Europe/London", "Liverpool John Lennon Airport"),
    "MAN": ("Europe/London", "Manchester Airport"),
    "BFS": ("Europe/London", "Belfast International Airport"),
    "BHD": ("Europe/London", "George Best Belfast City Airport"),
    "BHX": ("Europe/London", "Birmingham Airport"),
    "BRS": ("Europe/London", "Bristol Airport"),
    "EDI": ("Europe/London", "Edinburgh Airport"),
    "GLA": ("Europe/London", "Glasgow Airport"),
    "NCL": ("Europe/London", "Newcastle International Airport"),
    "WAW": ("Europe/Warsaw", "Warsaw Chopin Airport"),
    "SZG": ("Europe/Vienna", "Salzburg Airport W. A. Mozart"),
    "AMS": ("Europe/Amsterdam", "Amsterdam Airport Schiphol"),
    "FRA": ("Europe/Berlin", "Frankfurt Airport"),
    "DUB": ("Europe/Dublin", "Dublin Airport"),
}
CITY_AIRPORTS: dict[str, tuple[str, ...]] = {
    "London": ("LHR", "LGW", "STN", "LTN"),
    "Liverpool": ("LPL",),
    "Manchester": ("MAN",),
    "Belfast": ("BFS", "BHD"),
    "Birmingham": ("BHX",),
    "Bristol": ("BRS",),
    "Edinburgh": ("EDI",),
    "Glasgow": ("GLA",),
    "Newcastle": ("NCL",),
}
AIRPORT_CITY = {code: city for city, codes in CITY_AIRPORTS.items() for code in codes}
AIRLINES = {
    "KL": "KLM",
    "LH": "Lufthansa",
    "LO": "LOT",
    "W6": "Wizz Air",
    "FR": "Ryanair",
    "U2": "easyJet",
    "EI": "Aer Lingus",
    "LS": "Jet2",
}
MATCH_DAY = {"WAW": date(2026, 12, 10), "SZG": date(2027, 1, 28)}


@dataclass(frozen=True)
class Profile:
    archetype: str
    outbound: dict[int | str, tuple[str, ...]]
    returns: tuple[str, ...]
    unverifiable: frozenset[int] = frozenset()
    note: str = ""


def _same(*options: str) -> dict[int | str, tuple[str, ...]]:
    return {"before": options, "match": options}


WAW_LATE_NOTE = "every WAW arrival is after 17:30, so no Warsaw→Białystok train leaves ≥3h later"

PROFILES: dict[tuple[str, str], Profile] = {
    ("London", "WAW"): Profile(
        "works",
        _same("LTN 06:10 W61305/140 WAW", "LHR 11:30 LO282/145 WAW"),
        ("WAW 15:45 LO281/150 LHR", "WAW 19:50 W61306/150 LTN"),
        note="day-before nonstop arrives 09:30; match-day arrivals are ≥09:30 (too late)",
    ),
    ("Manchester", "WAW"): Profile(
        "first-candidate trap (outbound layover)",
        _same(
            "MAN 06:00 KL1072/75 AMS +45 KL1365/110 WAW",
            "MAN 08:30 LH941/100 FRA +90 LH1614/105 WAW",
        ),
        (
            "WAW 13:30 KL1366/120 AMS +85 KL1071/80 MAN",
            "WAW 17:20 LH1615/110 FRA +75 LH946/105 MAN",
        ),
        note="no nonstops; earliest and first-listed one-stop has a 45-minute layover "
        "(normalizer needs 60–480); the 14:25 FRA one-stop fits the 17:30 train",
    ),
    ("Birmingham", "WAW"): Profile(
        "first-candidate trap (return selection)",
        _same("BHX 07:00 W62711/150 WAW"),
        ("WAW 15:00 W62712/150 BHX", "WAW 22:40 FR2714/150 BHX"),
        note="latest-departing return lands at BHX 00:10 the next day, after the "
        "23:59 return window; the 15:00 return fits",
    ),
    ("Liverpool", "WAW"): Profile(
        "genuinely late",
        _same("LPL 14:30 FR1011/145 WAW", "LPL 12:00 EI3327/60 DUB +120 EI654/170 WAW"),
        ("WAW 16:05 FR1012/150 LPL",),
        note=WAW_LATE_NOTE,
    ),
    ("Edinburgh", "WAW"): Profile(
        "genuinely late",
        _same("EDI 15:00 FR3122/160 WAW", "EDI 13:20 KL1284/85 AMS +100 KL1365/110 WAW"),
        ("WAW 16:00 FR3123/165 EDI",),
        note=WAW_LATE_NOTE,
    ),
    ("Newcastle", "WAW"): Profile(
        "genuinely late",
        _same("NCL 13:00 KL948/70 AMS +100 KL1365/110 WAW"),
        ("WAW 14:10 KL1362/120 AMS +90 KL951/75 NCL",),
        note=WAW_LATE_NOTE,
    ),
    ("Bristol", "WAW"): Profile(
        "genuinely late",
        _same("BRS 15:20 FR2405/155 WAW"),
        ("WAW 18:40 FR2406/165 BRS",),
        note=WAW_LATE_NOTE,
    ),
    ("Glasgow", "WAW"): Profile(
        "works",
        _same("GLA 07:00 KL1462/85 AMS +95 KL1361/110 WAW"),
        ("WAW 14:10 KL1362/120 AMS +90 KL1477/85 GLA",),
        note="one-stop only; arrives 12:50, train 16:30",
    ),
    ("Belfast", "WAW"): Profile(
        "wider dates help",
        {-2: ("BFS 07:00 FR8231/180 WAW",), "before": (), "match": ()},
        ("WAW 15:30 FR8232/180 BFS",),
        note="explicit empty on 9 and 10 Dec; a valid nonstop on 8 Dec",
    ),
    ("London", "SZG"): Profile(
        "works",
        {
            "before": ("LGW 09:00 U28341/120 SZG", "STN 17:30 FR8544/115 SZG"),
            "match": ("LGW 14:30 U28343/120 SZG", "STN 17:30 FR8544/115 SZG"),
        },
        ("SZG 12:45 U28342/130 LGW", "SZG 18:40 FR8545/125 STN"),
    ),
    ("Manchester", "SZG"): Profile(
        "works",
        {"before": ("MAN 10:00 LS1681/135 SZG",), "match": ("MAN 15:30 LS1683/135 SZG",)},
        ("SZG 14:00 LS1682/145 MAN",),
    ),
    ("Birmingham", "SZG"): Profile(
        "works",
        {"before": ("BHX 08:45 LS1621/130 SZG",), "match": ("BHX 14:45 LS1623/130 SZG",)},
        ("SZG 12:40 LS1622/130 BHX",),
    ),
    ("Edinburgh", "SZG"): Profile(
        "works",
        {
            "before": ("EDI 06:30 KL1276/85 AMS +100 KL1791/85 SZG",),
            "match": ("EDI 12:30 KL1280/85 AMS +100 KL1795/85 SZG",),
        },
        ("SZG 13:00 KL1792/80 AMS +90 KL1287/90 EDI",),
        note="one-stop only",
    ),
    ("Glasgow", "SZG"): Profile(
        "genuinely late",
        {
            "before": ("GLA 18:15 KL1474/85 AMS +75 KL1793/85 SZG",),
            "match": ("GLA 11:00 KL1470/85 AMS +120 KL1795/85 SZG",),
        },
        ("SZG 13:00 KL1792/80 AMS +90 KL1477/85 GLA",),
        note="day-before lands 23:20 (gateway hours 06–23); match day lands 16:50 (>16:00)",
    ),
    ("Newcastle", "SZG"): Profile(
        "genuinely late",
        {
            "before": ("NCL 18:30 LH965/105 FRA +70 LH2112/60 SZG",),
            "match": ("NCL 12:00 LH963/105 FRA +90 LH2108/60 SZG",),
        },
        ("SZG 12:30 LH2113/60 FRA +80 LH966/105 NCL",),
        note="day-before lands 23:25 (gateway hours 06–23); match day lands 17:15 (>16:00)",
    ),
    ("Liverpool", "SZG"): Profile("provider empty", {"before": (), "match": ()}, ()),
    ("Bristol", "SZG"): Profile("provider empty", {"before": (), "match": ()}, ()),
    ("Belfast", "SZG"): Profile(
        "wider dates help",
        _same("BFS 08:00 KL1086/105 AMS +95 KL1793/85 SZG"),
        ("SZG 13:30 KL1794/85 AMS +95 KL1089/100 BFS",),
        unverifiable=frozenset({-1, 0}),
        note="27/28 Jan candidates list fine but booking returns an incomplete selection; "
        "26 Jan verifies",
    ),
}


def _outbound_specs(profile: Profile, offset: int) -> tuple[str, ...]:
    if offset in profile.outbound:
        return profile.outbound[offset]
    if offset < 0:
        return profile.outbound.get("before", ())
    if offset == 0:
        return profile.outbound.get("match", ())
    return ()


def _point(code: str, at: datetime) -> dict[str, Any]:
    local = at.astimezone(ZoneInfo(AIRPORTS[code][0]))
    return {
        "name": AIRPORTS[code][1],
        "id": code,
        "date": local.date().isoformat(),
        "time": local.strftime("%H:%M"),
    }


def itinerary(spec: str, day: date) -> dict[str, Any]:
    tokens = spec.split()
    here = tokens[0]
    at = datetime.combine(day, time.fromisoformat(tokens[1]), ZoneInfo(AIRPORTS[here][0]))
    at = at.astimezone(UTC)
    flights: list[dict[str, Any]] = []
    layovers: list[dict[str, Any]] = []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("+"):
            wait = int(token[1:])
            layovers.append({"id": here, "name": AIRPORTS[here][1], "duration": wait})
            at += timedelta(minutes=wait)
            index += 1
            continue
        number, minutes = token.split("/")
        destination = tokens[index + 1]
        arrive = at + timedelta(minutes=int(minutes))
        flights.append(
            {
                "departure_airport": _point(here, at),
                "arrival_airport": _point(destination, arrive),
                "duration": int(minutes),
                "airplane": "Airbus A320",
                "airline": AIRLINES[number[:2]],
                "travel_class": "Economy",
                "flight_number": f"{number[:2]} {number[2:]}",
            }
        )
        at, here = arrive, destination
        index += 2
    total = sum(f["duration"] for f in flights) + sum(x["duration"] for x in layovers)
    item: dict[str, Any] = {"flights": flights, "total_duration": total, "type": "Round trip"}
    if layovers:
        item["layovers"] = layovers
    return item


def _price(spec: str) -> int:
    return 45 + sum(int(t.split("/")[1]) for t in spec.split() if "/" in t) // 4


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(token: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


WARSAW = ZoneInfo("Europe/Warsaw")
RAIL_STATIONS = {
    ("Warsaw", "Białystok"): ("Warsaw Central", "Zielone Wzgórza", range(6, 21, 2), 30),
    ("Białystok", "Warsaw"): ("Zielone Wzgórza", "Warsaw Central", range(5, 20, 2), 30),
}
RAIL_MINUTES = 145


def _trains(origin: str, destination: str, day: date) -> dict[str, Any]:
    departure_station, arrival_station, hours, minute = RAIL_STATIONS[(origin, destination)]
    trains = []
    for position, hour in enumerate(hours, start=1):
        depart = datetime.combine(day, time(hour, minute), WARSAW)
        arrive = (depart.astimezone(UTC) + timedelta(minutes=RAIL_MINUTES)).astimezone(WARSAW)
        number = f"IC {1500 + hour * 10 + (1 if origin == 'Warsaw' else 2)}"
        trains.append(
            {
                "position": position,
                "time_window": {
                    "depart_at": depart.strftime("%H:%M"),
                    "depart_at_iso": depart.isoformat(),
                    "arrive_at": arrive.strftime("%H:%M"),
                    "arrive_at_iso": arrive.isoformat(),
                },
                "formatted_duration": "2h 25m",
                "duration": RAIL_MINUTES * 60,
                "transfers": 0,
                "service_provider": {
                    "name": "PKP Intercity",
                    "website": "https://www.intercity.pl/",
                },
                "instructions": [
                    {
                        "depart_from": {"place": departure_station, "at_iso": depart.isoformat()},
                        "arrive_at": {"place": arrival_station, "at_iso": arrive.isoformat()},
                        "service_provider": {
                            "name": "PKP Intercity",
                            "website": "https://www.intercity.pl/",
                        },
                        "line_number": number,
                    }
                ],
                "buy_ticket": {"offers": []},
            }
        )
    return {"origin": origin, "destination": destination, "date": day.isoformat(), "trains": trains}


@dataclass(frozen=True)
class Property:
    id: str
    title: str
    slug: str
    country: str
    room: str
    amenities: tuple[str, ...]
    nightly: int
    rating: float
    latitude: float
    longitude: float


PROPERTIES: dict[str, tuple[Property, ...]] = {
    "Białystok": (
        Property(
            "9100001",
            "Hotel Branicki Arena",
            "branicki-arena",
            "pl",
            "Double Room with Private Bathroom",
            ("Free WiFi", "Desk"),
            55,
            8.6,
            53.1031,
            23.1502,
        ),
        Property(
            "9100002",
            "Podlasie Old Town Rooms",
            "podlasie-old-town-rooms",
            "pl",
            "Double Room with Private Bathroom",
            ("Free WiFi",),
            48,
            8.1,
            53.1325,
            23.1591,
        ),
        Property(
            "9100003",
            "Jagiellonia Guest House",
            "jagiellonia-guest-house",
            "pl",
            "Single Room with Shared Bathroom",
            ("Free WiFi",),
            30,
            7.9,
            53.1210,
            23.1660,
        ),
        Property(
            "9100004",
            "Białystok Central Apartments",
            "bialystok-central-apartments",
            "pl",
            "Studio with Private Bathroom",
            ("Kitchenette", "Free WiFi"),
            62,
            9.0,
            53.1300,
            23.1500,
        ),
    ),
    "Salzburg": (
        Property(
            "9200001",
            "Hotel Klessheim Gate",
            "klessheim-gate",
            "at",
            "Double Room",
            ("En-suite bathroom", "Free WiFi", "Flat-screen TV"),
            95,
            8.4,
            47.8160,
            13.0050,
        ),
        Property(
            "9200002",
            "Altstadt Pension Mozart",
            "altstadt-pension-mozart",
            "at",
            "Double Room",
            ("En-suite bathroom", "Free WiFi"),
            88,
            8.8,
            47.8000,
            13.0430,
        ),
        Property(
            "9200003",
            "Salzburg Airport Inn",
            "salzburg-airport-inn",
            "at",
            "Standard Double Room",
            ("En-suite bathroom", "Shower"),
            79,
            7.8,
            47.7940,
            13.0020,
        ),
        Property(
            "9200004",
            "Riverside Rooms Salzburg",
            "riverside-rooms",
            "at",
            "Double Room",
            ("En-suite bathroom", "Balcony"),
            102,
            9.1,
            47.8060,
            13.0380,
        ),
    ),
}
PROPERTY_BY_SLUG = {p.slug: (city, p) for city, props in PROPERTIES.items() for p in props}


def _property_link(prop: Property) -> str:
    return f"https://www.booking.com/hotel/{prop.country}/{prop.slug}.en-gb.html"


def _stay_parameters(params: dict[str, str], engine: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "engine": engine,
        "check_in_date": params["check_in_date"],
        "check_out_date": params["check_out_date"],
        "adults": int(params.get("adults", "1")),
        "rooms": int(params.get("rooms", "1")),
        "currency": params.get("currency", "GBP"),
        "language": params.get("language", "en-gb"),
    }
    for key in ("q", "url", "sort_by", "filters"):
        if key in params:
            out[key] = params[key]
    return out


def _nights(params: dict[str, str]) -> int:
    nights = (
        date.fromisoformat(params["check_out_date"]) - date.fromisoformat(params["check_in_date"])
    ).days
    if nights < 1:
        raise Unsupported("stay with no nights")
    return nights


def _summary(nights: int, adults: int) -> str:
    return f"{nights} {'night' if nights == 1 else 'nights'}, {adults} {'adult' if adults == 1 else 'adults'}"


class Unsupported(Exception):
    """The request shape is outside the simulated inventory."""


@dataclass
class Ledger:
    rows: list[dict[str, Any]] = field(default_factory=list)
    by_workflow: Counter[str] = field(default_factory=Counter)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    clock: float = field(default_factory=monotonic)

    def for_workflow(self, workflow_id: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["workflow"] == workflow_id]


LEDGER = Ledger()


def _stage(params: dict[str, str]) -> str:
    engine = params.get("engine")
    if engine == "google_flights":
        if "booking_token" in params:
            return "flight booking token"
        if "departure_token" in params:
            return "flight return selection"
        return f"flight discovery ({params.get('stops', 'any')})"
    return {"google": "rail", "booking": "stay listing", "booking_property": "stay property"}.get(
        str(engine), str(engine)
    )


def _profile(params: dict[str, str]) -> tuple[str, str, Profile]:
    airports = params.get("departure_id", "").split(",")
    cities = {AIRPORT_CITY.get(code) for code in airports}
    if len(cities) != 1 or None in cities:
        raise Unsupported(f"departure_id {params.get('departure_id')!r}")
    city = cities.pop()
    destination = params.get("arrival_id", "")
    if (city, destination) not in PROFILES:
        raise Unsupported(f"route {city}->{destination}")
    if params.get("flight_type", "round_trip") != "round_trip":
        raise Unsupported(f"flight_type {params.get('flight_type')!r}")
    return city, destination, PROFILES[(city, destination)]


def _allowed(spec: str, stops: str | None) -> bool:
    return stops != "nonstop" or spec.count("/") == 1


def _flight_listing(items: list[dict[str, Any]], params: dict[str, str]) -> dict[str, Any]:
    return {
        "search_metadata": {"status": "Success", "request_url": _flights_url(params)},
        "search_parameters": _flight_parameters(params),
        "best_flights": items[:1],
        "other_flights": items[1:],
    }


def _flight_parameters(params: dict[str, str]) -> dict[str, Any]:
    keys = (
        "engine",
        "departure_id",
        "arrival_id",
        "outbound_date",
        "return_date",
        "flight_type",
        "stops",
        "adults",
        "children",
        "currency",
        "gl",
        "hl",
    )
    return {k: params[k] for k in keys if k in params}


def _flights_url(params: dict[str, str]) -> str:
    tfs = _encode(
        {k: params.get(k) for k in ("departure_id", "arrival_id", "outbound_date", "return_date")}
    )
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&hl=en&gl=uk&curr=GBP"


def google_flights(params: dict[str, str]) -> dict[str, Any]:
    city, destination, profile = _profile(params)
    outbound_date = date.fromisoformat(params["outbound_date"])
    return_date = date.fromisoformat(params["return_date"])
    offset = (outbound_date - MATCH_DAY[destination]).days
    stops = params.get("stops")
    if "booking_token" in params:
        token = _decode(params["booking_token"])
        if (token["od"], token["rd"], token["dest"]) != (
            str(outbound_date),
            str(return_date),
            destination,
        ):
            raise Unsupported("booking_token reused for another search")
        out, back = itinerary(token["out"], outbound_date), itinerary(token["ret"], return_date)
        selected = [out] if offset in profile.unverifiable else [out, back]
        numbers = [f["flight_number"] for leg in (out, back) for f in leg["flights"]]
        return {
            "search_metadata": {"status": "Success", "request_url": _flights_url(params)},
            "search_parameters": _flight_parameters(params),
            "selected_flights": selected,
            "booking_options": [
                {
                    "book_with": out["flights"][0]["airline"],
                    "flight_numbers": numbers,
                    "price": _price(token["out"]) + _price(token["ret"]),
                }
            ],
        }
    if "departure_token" in params:
        token = _decode(params["departure_token"])
        if (token["od"], token["rd"], token["dest"]) != (
            str(outbound_date),
            str(return_date),
            destination,
        ):
            raise Unsupported("departure_token reused for another search")
        items = []
        for spec in profile.returns:
            if not _allowed(spec, stops):
                continue
            item = itinerary(spec, return_date)
            item["price"] = _price(token["out"]) + _price(spec)
            item["booking_token"] = _encode({**token, "ret": spec})
            items.append(item)
        if not items:
            return dict(EXPLICIT_EMPTY)
        return _flight_listing(items, params)
    items = []
    for spec in _outbound_specs(profile, offset):
        if not _allowed(spec, stops):
            continue
        item = itinerary(spec, outbound_date)
        item["price"] = _price(spec) * 2
        item["departure_token"] = _encode(
            {
                "city": city,
                "dest": destination,
                "od": str(outbound_date),
                "rd": str(return_date),
                "out": spec,
            }
        )
        items.append(item)
    if not items:
        return dict(EXPLICIT_EMPTY)
    return _flight_listing(items, params)


TRAIN_QUERY = re.compile(r"^trains from (.+) to (.+) on (\d{1,2} [A-Za-z]+ \d{4})$")


def google(params: dict[str, str]) -> dict[str, Any]:
    match = TRAIN_QUERY.match(params.get("q", ""))
    if not match:
        raise Unsupported(f"google query {params.get('q')!r}")
    origin, destination, day = (
        match.group(1),
        match.group(2),
        datetime.strptime(match.group(3), "%d %B %Y").date(),
    )
    metadata = {"status": "Success", "request_url": "https://www.google.com/search"}
    if (origin, destination) not in RAIL_STATIONS:
        return {"search_metadata": metadata, "search_parameters": params, "organic_results": []}
    return {
        "search_metadata": metadata,
        "search_parameters": params,
        "train_results": _trains(origin, destination, day),
    }


def booking(params: dict[str, str]) -> dict[str, Any]:
    city = next(
        (
            c
            for c in PROPERTIES
            if c.casefold() == params.get("q", "").split(",")[0].strip().casefold()
        ),
        None,
    )
    if city is None:
        raise Unsupported(f"booking q {params.get('q')!r}")
    nights, adults = _nights(params), int(params.get("adults", "1"))
    properties = []
    for position, prop in enumerate(PROPERTIES[city], start=1):
        price = prop.nightly * nights
        properties.append(
            {
                "position": position,
                "property_id": prop.id,
                "title": prop.title,
                "link": _property_link(prop),
                "gps_coordinates": {"latitude": prop.latitude, "longitude": prop.longitude},
                "city": city,
                "country_code": prop.country,
                "rating": prop.rating,
                "max_rating": 10,
                "price": f"£{price}",
                "extracted_price": price,
                "currency": "GBP",
                "taxes_and_charges": "£4",
                "extracted_taxes_and_charges": 4,
                "stay_summary": _summary(nights, adults),
                "room_type": prop.room,
            }
        )
    return {
        "search_metadata": {
            "status": "Success",
            "request_url": "https://www.booking.com/searchresults.en-gb.html",
        },
        "search_parameters": _stay_parameters(params, "booking"),
        "filters": [
            {
                "title": "Facilities",
                "options": [
                    {"name": "Free WiFi", "value": "hotelfacility=107"},
                    {"name": "Private bathroom", "value": "roomfacility=38"},
                ],
            }
        ],
        "properties": properties,
    }


def booking_property(params: dict[str, str]) -> dict[str, Any]:
    path = urlsplit(params.get("url", "")).path
    slug = path.rsplit("/", 1)[-1].removesuffix(".en-gb.html").removesuffix(".html")
    if slug not in PROPERTY_BY_SLUG:
        raise Unsupported(f"booking_property url path {path!r}")
    _, prop = PROPERTY_BY_SLUG[slug]
    nights = _nights(params)
    price = prop.nightly * nights
    return {
        "search_metadata": {"status": "Success", "request_url": _property_link(prop)},
        "search_parameters": _stay_parameters(params, "booking_property"),
        "rooms": [
            {
                "position": 1,
                "name": prop.room,
                "price": f"£{price}",
                "extracted_price": price,
                "currency": "GBP",
                "max_guests": 1 if prop.room.startswith("Single") else 2,
                "amenities": list(prop.amenities),
            }
        ],
    }


HANDLERS = {
    "google_flights": google_flights,
    "google": google,
    "booking": booking,
    "booking_property": booking_property,
}


def _activity_context() -> tuple[str, str]:
    try:
        from temporalio import activity

        info = activity.info()
        return info.workflow_id or "?", info.activity_type
    except RuntimeError:
        return "(outside activity)", "-"


_original_send = httpx.AsyncClient.send
_original_async_transport = httpx.AsyncHTTPTransport.handle_async_request
_original_sync_transport = httpx.HTTPTransport.handle_request


async def _send(
    self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any
) -> httpx.Response:
    if request.url.host != SEARCHAPI_HOST:
        return await _original_send(self, request, *args, **kwargs)
    params = dict(request.url.params)
    workflow_id, activity_type = _activity_context()
    engine = params.get("engine", "")
    row = {
        "workflow": workflow_id,
        "activity": activity_type,
        "engine": engine,
        "stage": _stage(params),
        "at": round(monotonic() - LEDGER.clock, 3),
    }
    LEDGER.rows.append(row)
    LEDGER.by_workflow[workflow_id] += 1
    if request.url.path != SEARCHAPI_PATH or engine not in HANDLERS:
        LEDGER.violations.append(f"unexpected SearchApi request {request.url.path} engine={engine}")
        raise RuntimeError("Benchmark: unexpected SearchApi request shape")
    await asyncio.sleep(LATENCY[engine])
    try:
        payload = HANDLERS[engine](params)
    except (Unsupported, KeyError, ValueError) as exc:
        detail = {"engine": engine, "stage": row["stage"], "reason": f"{type(exc).__name__}: {exc}"}
        LEDGER.unsupported.append(detail)
        row["unsupported"] = detail["reason"]
        payload = {"error": "Benchmark inventory does not support this request."}
    if payload.get("error") == EXPLICIT_EMPTY["error"]:
        row["result"] = "explicit empty"
    return httpx.Response(200, json=payload, request=request)


async def _blocked_async(self: Any, request: httpx.Request) -> httpx.Response:
    LEDGER.violations.append(f"real network request blocked: {request.url.host}")
    raise RuntimeError(f"Benchmark blocks real network traffic ({request.url.host})")


def _blocked_sync(self: Any, request: httpx.Request) -> httpx.Response:
    LEDGER.violations.append(f"real network request blocked: {request.url.host}")
    raise RuntimeError(f"Benchmark blocks real network traffic ({request.url.host})")


def install() -> Ledger:
    httpx.AsyncClient.send = _send
    httpx.AsyncHTTPTransport.handle_async_request = _blocked_async
    httpx.HTTPTransport.handle_request = _blocked_sync
    return LEDGER


def uninstall() -> None:
    httpx.AsyncClient.send = _original_send
    httpx.AsyncHTTPTransport.handle_async_request = _original_async_transport
    httpx.HTTPTransport.handle_request = _original_sync_transport


def archetypes() -> dict[str, dict[str, str]]:
    return {
        f"{city}->{dest}": {"archetype": p.archetype, "note": p.note}
        for (city, dest), p in PROFILES.items()
    }
