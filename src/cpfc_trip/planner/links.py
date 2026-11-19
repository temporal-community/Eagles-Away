"""Direct outbound links only. Never fetch arbitrary user/model-supplied URLs."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cpfc_trip.domain import Party, RecoveryLink, SearchSpec

ALLOWED = {
    "www.eticketing.co.uk",
    "www.olvallee.fr",
    "www.rhonexpress.fr",
    "www.google.com",
    "www.booking.com",
    "www.eurostar.com",
    "www.sncf-connect.com",
    "www.bahn.de",
    "int.bahn.de",
    "www.oebb.at",
    "shop.oebbtickets.at",
    "www.intercity.pl",
    "ebilet.intercity.pl",
    "www.omio.com",
    "www.thetrainline.com",
}


def safe_url(value: str) -> str | None:
    if len(value) > 16000:
        return None
    try:
        u = urlsplit(value)
        if u.scheme != "https" or u.hostname not in ALLOWED or u.username or u.password:
            return None
        if u.port not in (None, 443) or any(ord(c) < 32 for c in value):
            return None
        if u.hostname == "www.google.com" and not (
            u.path.startswith("/travel/flights") or u.path == "/maps/dir/"
        ):
            return None
        return value
    except ValueError:
        return None


def flight_url(value: str) -> str | None:
    if not safe_url(value) or not urlsplit(value).path.startswith("/travel/flights"):
        return None
    u = urlsplit(value)
    params = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k not in {"gl", "hl", "curr"}
    ]
    params += [("gl", "GB"), ("hl", "en-GB"), ("curr", "GBP")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(params), u.fragment))


def hotel_url(value: str, start: str, end: str, party: Party) -> str | None:
    u = urlsplit(value)
    if not safe_url(value) or u.hostname != "www.booking.com" or not u.path.startswith("/hotel/"):
        return None
    # Explicit occupancy replaces any stale parameters on the provider URL.
    params = {
        k: v
        for k, v in parse_qsl(u.query)
        if k not in {"checkin", "checkout", "group_adults", "group_children", "no_rooms", "age"}
    }
    return urlunsplit((u.scheme, u.netloc, u.path, _booking_query(start, end, party, params), ""))


def _booking_query(start: str, end: str, party: Party, params: dict[str, str]) -> str:
    params = {
        **params,
        "checkin": start,
        "checkout": end,
        "group_adults": str(party.adults),
        "group_children": str(len(party.child_ages)),
        "no_rooms": str(party.rooms),
        "selected_currency": "GBP",
    }
    return urlencode([*params.items(), *(("age", str(age)) for age in party.child_ages)])


def dated_recovery_links(
    spec: SearchSpec, *, include_flights: bool = True, include_stays: bool = True
) -> tuple[RecoveryLink, ...]:
    start, end = spec.outbound_date.isoformat(), spec.return_date.isoformat()
    party = spec.party
    links = []
    if include_flights and spec.route.mode == "flight":
        airports = spec.route.origin.split(",")
        origin = ", ".join(airports)
        travellers = f"{party.adults} {'adult' if party.adults == 1 else 'adults'}"
        children = len(party.child_ages)
        if children:
            travellers += f" and {children} {'child' if children == 1 else 'children'}"
        search = f"Flights from {origin} to {spec.route.destination} on {start} through {end} for {travellers}"
        query = urlencode({"q": search})
        flight = flight_url(f"https://www.google.com/travel/flights?{query}")
        if flight:
            label = f"Search flights from {'/'.join(airports)} on Google Flights"
            links.append(RecoveryLink(label=label, url=flight))
    if include_stays:
        query = _booking_query(start, end, party, {"ss": spec.fixture.city})
        stay = safe_url(f"https://www.booking.com/searchresults.en-gb.html?{query}")
        if stay:
            label = f"Search stays in {spec.fixture.city} on Booking.com ({start} to {end})"
            links.append(RecoveryLink(label=label, url=stay))
    return tuple(links)
