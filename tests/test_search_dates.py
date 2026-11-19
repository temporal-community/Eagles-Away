from datetime import UTC, date, datetime

import pytest

from cpfc_trip.catalog import load_catalog, validate_brief
from cpfc_trip.domain import Brief, Window
from cpfc_trip.planner.planning import enumerate_specs, feasible
from cpfc_trip.planner.recorded import sample

REFERENCE_DATE = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    ("city", "match_day"),
    [("Salzburg", date(2027, 1, 28)), ("Białystok", date(2026, 12, 10))],
)
@pytest.mark.parametrize("flexibility", ["tight", "day_either_side", "two_days"])
def test_search_skips_impossible_return_before_the_date_pair_cap(city, match_day, flexibility):
    fixtures, routes = load_catalog()
    fixture = next(f for f in fixtures if f.city == city)
    brief = validate_brief(
        Brief(fixture_ids=(fixture.id,), flexibility=flexibility, transport_mode="flight"),
        fixtures,
        REFERENCE_DATE,
    )
    legacy = enumerate_specs(brief, (fixture,), routes)
    screened = enumerate_specs(brief, (fixture,), routes, screen_impossible_dates=True)

    assert len(legacy) == 3
    assert any(spec.return_date == match_day for spec in legacy)
    assert len(screened) == (3 if flexibility == "two_days" else 2)
    assert all(spec.return_date > match_day for spec in screened)
    if flexibility == "tight":
        assert legacy[0].return_date == match_day
    if flexibility == "two_days":
        legacy_dates = {(spec.outbound_date, spec.return_date) for spec in legacy}
        assert any((spec.outbound_date, spec.return_date) not in legacy_dates for spec in screened)


def test_search_keeps_a_feasible_one_night_trip_after_an_early_kickoff():
    fixtures, routes = load_catalog()
    salzburg = next(f for f in fixtures if f.city == "Salzburg")
    fixture = salzburg.model_copy(update={"kickoff_at": datetime(2027, 1, 28, 8, tzinfo=UTC)})
    brief = validate_brief(
        Brief(fixture_ids=(fixture.id,), transport_mode="flight", flexibility="tight"),
        (fixture,),
        REFERENCE_DATE,
    )

    spec = enumerate_specs(brief, (fixture,), routes, screen_impossible_dates=True)[0]
    assert spec.outbound_date == date(2027, 1, 27)
    assert spec.return_date == date(2027, 1, 28)
    assert feasible(
        sample(spec, "flight").journeys[0],
        sample(spec, "stay").stays[0],
        fixture,
        brief,
        spec.route,
    )


def test_search_screens_custom_window_that_ends_before_a_return_can_depart():
    fixtures, routes = load_catalog()
    fixture = next(f for f in fixtures if f.city == "Salzburg")
    window = Window(
        fixture_id=fixture.id,
        earliest_departure=datetime(2027, 1, 27, tzinfo=UTC),
        latest_return=datetime(2027, 1, 28, 23, 59, tzinfo=UTC),
    )
    brief = validate_brief(
        Brief(fixture_ids=(fixture.id,), transport_mode="flight", windows=(window,)),
        fixtures,
        REFERENCE_DATE,
    )

    assert enumerate_specs(brief, (fixture,), routes, recorded=True)
    assert enumerate_specs(brief, (fixture,), routes)
    assert enumerate_specs(brief, (fixture,), routes, screen_impossible_dates=True) == []


def test_search_uses_each_routes_exclusive_cutoff_in_destination_time():
    fixtures, routes = load_catalog()
    salzburg = next(f for f in fixtures if f.city == "Salzburg")
    fixture = salzburg.model_copy(update={"kickoff_at": datetime(2027, 1, 28, 17, tzinfo=UTC)})
    route = next(r for r in routes if r.fixture_id == salzburg.id and r.enabled)
    late_route = route.model_copy(update={"id": "late", "gateway_departure_hours": (6, 24)})
    brief = validate_brief(
        Brief(fixture_ids=(fixture.id,), transport_mode="flight", flexibility="tight"),
        (fixture,),
        REFERENCE_DATE,
    )

    specs = enumerate_specs(brief, (fixture,), (route, late_route), screen_impossible_dates=True)
    same_day_routes = {spec.route.id for spec in specs if spec.return_date == date(2027, 1, 28)}
    assert same_day_routes == {late_route.id}


def test_search_keeps_an_available_return_hour_after_the_clocks_go_back():
    fixtures, routes = load_catalog()
    salzburg = next(f for f in fixtures if f.city == "Salzburg")
    fixture = salzburg.model_copy(update={"kickoff_at": datetime(2026, 10, 24, 20, 30, tzinfo=UTC)})
    route = next(r for r in routes if r.fixture_id == salzburg.id and r.enabled)
    before_change = route.model_copy(update={"gateway_departure_hours": (0, 2)})
    after_change = route.model_copy(
        update={"id": "after-change", "gateway_departure_hours": (0, 3)}
    )
    brief = validate_brief(
        Brief(fixture_ids=(fixture.id,), transport_mode="flight", flexibility="tight"),
        (fixture,),
        REFERENCE_DATE,
    )

    specs = enumerate_specs(
        brief, (fixture,), (before_change, after_change), screen_impossible_dates=True
    )
    assert {spec.route.id for spec in specs if spec.return_date == date(2026, 10, 25)} == {
        after_change.id
    }
