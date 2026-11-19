"""Versioned, bounded public records. Provider payloads never enter these models."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Party(Record):
    adults: int = Field(1, ge=1, le=8)
    child_ages: tuple[Annotated[int, Field(ge=0, le=17)], ...] = Field((), max_length=6)
    rooms: int = Field(1, ge=1, le=4)

    @property
    def size(self) -> int:
        return self.adults + len(self.child_ages)


class Window(Record):
    fixture_id: str
    earliest_departure: AwareDatetime
    latest_return: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.latest_return <= self.earliest_departure:
            raise ValueError("Return must be after departure")
        if (self.latest_return - self.earliest_departure).days > 7:
            raise ValueError("Travel windows are limited to seven days")
        return self


class Brief(Record):
    fixture_ids: tuple[str, ...] = Field(min_length=1, max_length=4)
    origin_city: str = Field("London", min_length=2, max_length=50)
    departure_city: str | None = Field(
        default=None, min_length=2, max_length=50, exclude_if=lambda city: city is None
    )
    travellers: Party = Party()
    budget_tier: Literal["budget", "value", "comfort"] = "value"
    flexibility: Literal["tight", "day_either_side", "two_days"] = "day_either_side"
    windows: tuple[Window, ...] = Field((), max_length=4)
    private_room: bool = False
    private_bathroom: bool = False
    transport_mode: Literal["flight", "rail"] | None = None

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.fixture_ids)) != len(self.fixture_ids):
            raise ValueError("Choose each fixture once")
        ids = [w.fixture_id for w in self.windows]
        if len(set(ids)) != len(ids) or not set(ids) <= set(self.fixture_ids):
            raise ValueError("Travel windows must refer to distinct selected fixtures")
        return self


class CreateSession(Record):
    submission_id: UUID
    brief: Brief


class Coordinates(Record):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class Fixture(Record):
    id: str
    opponent: str
    city: str
    timezone: str
    kickoff_at: AwareDatetime
    venue: str
    venue_location: str = ""
    venue_coordinates: Coordinates | None = None
    venue_status: Literal["provisional", "confirmed"] = "provisional"
    source_url: str
    source_checked_at: AwareDatetime
    catalog_version: str


class TransferGuidance(Record):
    title: str
    description: str
    # A few words for compact link chips; the description stays the full guidance.
    summary: str = ""
    source_url: str
    reviewed_at: AwareDatetime


class MapsTransfer(Record):
    title: str
    description: str
    summary: str = ""
    url: str


class Route(Record):
    id: str
    fixture_id: str
    mode: Literal["flight", "rail"]
    origin: str
    destination: str
    destination_timezone: str
    enabled: bool = False
    onward_stations: tuple[str, ...] = ()
    minimum_transfer_minutes: int = Field(180, ge=0)
    transfer_note: str
    # One-line version of transfer_note for the "Before you book" checklist.
    transfer_headline: str = ""
    source_urls: tuple[str, ...] = ()
    reviewed_at: AwareDatetime | None = None
    guidance: tuple[TransferGuidance, ...] = Field((), max_length=5)
    # Conservative supported local-clock windows, not claimed train timetables.
    gateway_arrival_hours: tuple[int, int] = (0, 24)
    gateway_departure_hours: tuple[int, int] = (0, 24)


class Limits(Record):
    planning_seconds: int = Field(180, ge=1, le=180)
    search_calls: int = Field(64, ge=1, le=64)


class SessionInput(Record):
    public_session_id: UUID
    brief: Brief
    fixtures: tuple[Fixture, ...]
    routes: tuple[Route, ...]
    limits: Limits = Limits()
    planner_mode: Literal["live", "recorded"] = "live"
    catalog_version: str = "2026-10-05.away.1"


class Money(Record):
    minor_units: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Evidence(Record):
    provider: str = "SearchApi"
    underlying_source: str
    source_url: str | None = None
    retrieved_at: AwareDatetime


class Offer(Record):
    id: str
    booking_url: str | None = None
    link_kind: Literal["direct", "contextual_search", "generic_search"] = "contextual_search"
    seller: str | None = None
    evidence: Evidence
    search_fingerprint: str


class Quote(Record):
    id: str
    amount: Money
    scope: Literal["round_trip", "leg", "stay", "night", "unknown"]
    unit: Literal["party", "person", "room", "bed", "unknown"]
    priced_party: Party | None = None
    taxes: Literal["included", "excluded", "unknown"] = "unknown"
    additional_taxes: Money | None = None
    payment_currency: str | None = None
    observed_at: AwareDatetime
    caveats: tuple[str, ...] = ()


class Leg(Record):
    kind: Literal["transport"] = "transport"
    id: str
    mode: Literal["flight", "rail"]
    origin: str
    destination: str
    departs_at: AwareDatetime
    arrives_at: AwareDatetime
    operator: str
    service_number: str = ""
    offer: Offer
    quote: Quote | None = None
    caveats: tuple[str, ...] = ()

    @model_validator(mode="after")
    def chronological(self) -> Self:
        if self.arrives_at <= self.departs_at:
            raise ValueError("Leg arrival must follow departure")
        return self


class Stay(Record):
    kind: Literal["stay"] = "stay"
    id: str
    property_name: str
    check_in: date
    check_out: date
    room_description: str
    bathroom: Literal["private", "shared", "unknown"] = "unknown"
    room_type: Literal["private", "dorm", "unknown"] = "unknown"
    max_guests: int | None = None
    review_score: float | None = None
    offer: Offer
    quote: Quote | None = None
    caveats: tuple[str, ...] = ()

    @model_validator(mode="after")
    def chronological(self) -> Self:
        if self.check_out <= self.check_in:
            raise ValueError("Stay must include at least one night")
        return self


class Journey(Record):
    id: str
    route_id: str
    outbound: tuple[Leg, ...] = Field(min_length=1, max_length=4)
    inbound: tuple[Leg, ...] = Field(min_length=1, max_length=4)


class Alternative(Record):
    journey: Journey
    stay: Stay
    known_total: Money | None = None
    price_coverage: float = Field(0, ge=0, le=1)
    summary: str


class Check(Record):
    """Something the traveller must confirm before booking: a short headline plus full detail."""

    headline: str
    detail: str = ""


class Trip(Record):
    fixture: Fixture
    journey: Journey | None = None
    stay: Stay | None = None
    known_total: Money | None = None
    price_coverage: float = Field(0, ge=0, le=1)
    feasibility: Literal["verified", "needs_checks"] = "needs_checks"
    gaps: tuple[str, ...] = ()
    # Headline form of gaps; empty on trips stored before checks existed.
    checks: tuple[Check, ...] = ()
    summary: str
    transfers: tuple[TransferGuidance, ...] = Field((), max_length=5)
    maps_transfers: tuple[MapsTransfer, ...] = Field((), max_length=3)
    alternatives: tuple[Alternative, ...] = Field((), max_length=2)


class Itinerary(Record):
    generated_at: AwareDatetime
    trips: tuple[Trip, ...] = Field(min_length=1, max_length=4)
    caveats: tuple[str, ...] = (
        "Prices may have changed since retrieval. Confirm the full price before booking.",
        "Match tickets and travel to/from your London departure hub are not included.",
    )
    ranking_policy_version: str = "1"


Phase = Literal["created", "researching", "closed", "failed"]


class RecoveryLink(Record):
    label: str
    url: str


FailureReason = Literal[
    "no_flights",
    "flight_timing",
    "flights_unverified",
    "connections",
    "onward_rail",
    "stay",
    "private_bathroom",
    "dates",
    "no_route",
    "search_limits",
    "unavailable",
]


class DatePair(Record):
    outbound_date: date
    return_date: date


class RecoveryAction(Record):
    """A next step the run already verified with a successful search."""

    kind: Literal["depart_from", "wider_dates", "without_private_bathroom"]
    label: str
    detail: str
    departure_city: str | None = None


class Snapshot(Record):
    development_mode: bool = False
    public_session_id: UUID
    origin_city: str = "London"
    departure_city: str | None = None
    state_revision: int = 0
    phase: Phase = "created"
    progress_message: str = "Checking routes from London…"
    itinerary: Itinerary | None = None
    # A checked journey or stay can be shown before a complete trip is verified.
    preview_trip: Trip | None = None
    preview_options: tuple[Trip, ...] = Field((), max_length=2)
    alternate_origin_city: str | None = None
    failure_reason: FailureReason | None = None
    searched_dates: tuple[DatePair, ...] = Field((), max_length=6)
    recovery_actions: tuple[RecoveryAction, ...] = Field((), max_length=3)
    recovery_links: tuple[RecoveryLink, ...] = Field((), max_length=4)


class SearchSpec(Record):
    fixture: Fixture
    route: Route
    party: Party
    outbound_date: date
    return_date: date
    budget_tier: Literal["budget", "value", "comfort"] = "value"
    private_room: bool = False
    private_bathroom: bool = False
    latest_return: AwareDatetime | None = None


FlightFailure = Literal[
    "provider_empty",
    "no_timely_flight",
    "unverified",
    "unsupported_connection",
    "onward_rail",
    "search_limits",
    "provider_error",
]


class SearchBatch(Record):
    journeys: tuple[Journey, ...] = ()
    stays: tuple[Stay, ...] = ()
    gaps: tuple[str, ...] = ()
    calls: int = 0
    failure: FlightFailure | None = None
    stale: bool = False
    listed: int = 0
    screened: int = 0
    followed: int = 0
