"""Catalog I/O occurs at session creation, never during Workflow replay."""

import json
from datetime import datetime, time, timedelta
from importlib.resources import files
from zoneinfo import ZoneInfo

from cpfc_trip.domain import Brief, Fixture, Route, Window
from cpfc_trip.origins import alternate_departure_cities, canonical_origin


def load_catalog() -> tuple[tuple[Fixture, ...], tuple[Route, ...]]:
    data = json.loads(files("cpfc_trip.data").joinpath("catalog.json").read_text())
    return tuple(Fixture.model_validate(x) for x in data["fixtures"]), tuple(
        Route.model_validate(x) for x in data["routes"]
    )


def validate_brief(brief: Brief, fixtures: tuple[Fixture, ...], now: datetime) -> Brief:
    origin_city = canonical_origin(brief.origin_city)
    departure_city = brief.departure_city
    if departure_city is not None:
        if brief.transport_mode == "rail":
            raise ValueError("A nearby departure city is only available for flights")
        departure_city = next(
            (
                city
                for city in alternate_departure_cities(origin_city)
                if city.casefold() == departure_city.strip().casefold()
            ),
            None,
        )
        if departure_city is None:
            raise ValueError("Choose a supported nearby departure city for your starting city")
    selected = {x.id: x for x in fixtures}
    if not set(brief.fixture_ids) <= selected.keys():
        raise ValueError("Choose fixtures from the Palace catalog")
    windows = {x.fixture_id: x for x in brief.windows}
    london = ZoneInfo("Europe/London")
    for fixture_id in brief.fixture_ids:
        f = selected[fixture_id]
        if f.kickoff_at <= now:
            raise ValueError("This fixture has already started")
        day = f.kickoff_at.astimezone(london).date()
        days = 2 if brief.flexibility == "two_days" else 1
        w = windows.get(fixture_id) or Window(
            fixture_id=fixture_id,
            earliest_departure=datetime.combine(day - timedelta(days=days), time.min, london),
            latest_return=datetime.combine(day + timedelta(days=days), time(23, 59), london),
        )
        if not w.earliest_departure < f.kickoff_at < w.latest_return:
            raise ValueError("Travel dates must contain the fixture")
        if w.latest_return <= now:
            raise ValueError("Travel window is in the past")
        windows[fixture_id] = w
    return brief.model_copy(
        update={
            "origin_city": origin_city,
            "departure_city": departure_city,
            "windows": tuple(windows.values()),
        }
    )
