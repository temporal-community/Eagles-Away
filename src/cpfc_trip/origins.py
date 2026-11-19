"""Reviewed UK departure cities, airports and direct London rail gateways."""

from datetime import UTC, datetime

from cpfc_trip.domain import Route, TransferGuidance

# Explicit airport coverage keeps a typed city from silently becoming a different airport.
UK_ORIGINS: dict[str, tuple[str, ...]] = {
    "Aberdeen": ("ABZ",),
    "Belfast": ("BFS", "BHD"),
    "Birmingham": ("BHX",),
    "Bournemouth": ("BOH",),
    "Bristol": ("BRS",),
    "Cardiff": ("CWL",),
    "Edinburgh": ("EDI",),
    "Exeter": ("EXT",),
    "Glasgow": ("GLA",),
    "Inverness": ("INV",),
    "Leeds": ("LBA",),
    "Liverpool": ("LPL",),
    "London": ("LHR", "LGW", "STN", "LTN"),
    "Manchester": ("MAN",),
    "Newcastle": ("NCL",),
    "Norwich": ("NWI",),
    "Nottingham": ("EMA",),
    "Southampton": ("SOU",),
}

# Provider city, UK departure station, London arrival station. These direct station
# pairs were checked against dated SearchApi train results on 5 October 2026.
UK_RAIL: dict[str, tuple[str, str, str]] = {
    "Birmingham": ("Birmingham", "Birmingham New Street", "Euston"),
    "Bristol": ("Bristol", "Bristol Temple Meads", "Paddington"),
    "Cardiff": ("Cardiff", "Cardiff Central", "Paddington"),
    "Edinburgh": ("Edinburgh", "Edinburgh Waverley", "King’s Cross"),
    "Leeds": ("Leeds", "Leeds", "King’s Cross"),
    "Liverpool": ("Liverpool", "Liverpool Lime Street", "Euston"),
    "Manchester": ("Manchester", "Manchester Piccadilly", "Euston"),
    "Newcastle": ("Newcastle upon Tyne", "Newcastle", "King’s Cross"),
}

# Fixed preview list. Each city's supported airport handled at least 5 million
# passengers in the CAA April 2024–March 2025 table. This is a connectivity
# filter, not a promise of a dated flight for every match.
LIVE_ORIGINS: tuple[str, ...] = (
    "Belfast",
    "Birmingham",
    "Bristol",
    "Edinburgh",
    "Glasgow",
    "Liverpool",
    "London",
    "Manchester",
    "Newcastle",
)

NEARBY_DEPARTURES: dict[str, tuple[str, ...]] = {
    "belfast": ("Dublin",),
    "birmingham": ("Manchester", "London"),
    "bristol": ("Birmingham", "London"),
    "edinburgh": ("Glasgow", "London"),
    "glasgow": ("Edinburgh", "London"),
    "liverpool": ("Manchester", "London"),
    "london": ("Birmingham",),
    "manchester": ("Liverpool", "London"),
    "newcastle": ("Edinburgh", "London"),
}
DEPARTURE_ONLY: dict[str, tuple[str, ...]] = {"Dublin": ("DUB",)}
AIRPORT_TRANSPORT_GUIDE = "https://www.nationalrail.co.uk/airports/trains-to-british-airports/"
AIRPORT_GUIDES = {"Dublin": "https://www.dublinairport.com/to-from-the-airport/by-bus"}

LONDON_TRANSFER = TransferGuidance(
    title="Changing stations in London",
    description=(
        "The UK train arrives at a London terminal. Transfer to St Pancras International "
        "separately; the planner allows at least three hours before Eurostar. Confirm local "
        "transport, the Eurostar check-in time and any disruption. Separate tickets may not "
        "protect a missed connection; London transfer fares are not included."
    ),
    summary="London terminal → St Pancras",
    source_url="https://www.eurostar.com/uk-en/travel-info/your-trip/check-in",
    reviewed_at=datetime(2026, 10, 5, tzinfo=UTC),
)


def canonical_origin(city: str) -> str:
    """Return a supported UK city name, ignoring case and surrounding whitespace."""
    name = next((name for name in UK_ORIGINS if name.casefold() == city.strip().casefold()), None)
    if name is None:
        raise ValueError("Choose a supported UK departure city from the suggestions")
    return name


def airports_for(city: str) -> tuple[str, ...]:
    name = next(
        (name for name in DEPARTURE_ONLY if name.casefold() == city.strip().casefold()), None
    )
    return DEPARTURE_ONLY[name] if name else UK_ORIGINS[canonical_origin(city)]


def alternate_departure_cities(origin_city: str) -> tuple[str, ...]:
    return NEARBY_DEPARTURES.get(origin_city.strip().casefold(), ())


def alternate_departure_route(route: Route, origin_city: str, departure_city: str) -> Route | None:
    alternate = next(
        (
            city
            for city in alternate_departure_cities(origin_city)
            if city.casefold() == departure_city.strip().casefold()
        ),
        None,
    )
    if route.mode != "flight" or alternate is None:
        return None
    airports = airports_for(alternate)
    if set(airports).intersection(route.origin.split(",")):
        return None

    place = "London’s airports" if alternate == "London" else f"{alternate} Airport"
    note = (
        f"Arrange travel between {canonical_origin(origin_city)} and {place} separately. "
        "Allow time for check-in and the journey home. These fares are separate from the trip total."
    )
    if alternate == "Dublin":
        note += " Direct coaches run between Belfast and Dublin Airport; airlines ask for photo ID."
    guide = AIRPORT_GUIDES.get(alternate, AIRPORT_TRANSPORT_GUIDE)
    guidance = route.guidance
    if len(guidance) < 5:
        guidance = (
            TransferGuidance(
                title=f"Getting to and from {place}",
                description=note,
                source_url=guide,
                reviewed_at=datetime(2026, 11, 18, tzinfo=UTC),
            ),
            *guidance,
        )
    return route.model_copy(
        update={
            "id": f"{route.id}-depart-{alternate.casefold()}",
            "origin": ",".join(airports),
            "transfer_note": f"{note} {route.transfer_note}",
            "guidance": guidance,
            "source_urls": tuple(dict.fromkeys((*route.source_urls, guide))),
        }
    )


def routes_for_origin(routes: tuple[Route, ...], city: str) -> tuple[Route, ...]:
    """Freeze selected city's reviewed air and rail gateways into route templates."""
    name = canonical_origin(city)
    if name == "London":
        return routes
    airports = ",".join(airports_for(name))
    slug = name.lower().replace(" ", "-")
    selected = []
    for route in routes:
        if route.mode == "flight":
            selected.append(
                route.model_copy(update={"id": f"{slug}-{route.id}", "origin": airports})
            )
        elif route.mode == "rail" and name in UK_RAIL:
            selected.append(
                route.model_copy(
                    update={
                        "id": f"{slug}-{route.id}",
                        "origin": name,
                        "onward_stations": ("London", *route.onward_stations),
                        # The reviewed Edinburgh connection reaches Lyon after 23:00.
                        "gateway_arrival_hours": (6, 24)
                        if name == "Edinburgh"
                        else route.gateway_arrival_hours,
                        "guidance": (LONDON_TRANSFER, *route.guidance),
                        "transfer_note": (
                            route.transfer_note
                            + " Edinburgh rail can arrive late; confirm hotel check-in before booking."
                            if name == "Edinburgh"
                            else route.transfer_note
                        ),
                        "transfer_headline": (
                            "Late Edinburgh arrival: confirm hotel check-in"
                            if name == "Edinburgh"
                            else route.transfer_headline
                        ),
                        "source_urls": (*route.source_urls, LONDON_TRANSFER.source_url),
                    }
                )
            )
    return tuple(selected)
