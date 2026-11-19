"""One bounded planning run; Queries read progress while the run commits each step."""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import date, datetime, timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    # Pydantic imports this lazily during validation; load it before sandbox activation.
    import annotated_types as annotated_types
    from temporalio.common import RetryPolicy
    from temporalio.exceptions import CancelledError as TemporalCancelledError

    from cpfc_trip.catalog import validate_brief
    from cpfc_trip.domain import (
        Brief,
        DatePair,
        FailureReason,
        Fixture,
        Itinerary,
        RecoveryAction,
        RecoveryLink,
        SearchBatch,
        SearchSpec,
        SessionInput,
        Snapshot,
        Trip,
    )
    from cpfc_trip.origins import (
        airports_for,
        alternate_departure_cities,
        alternate_departure_route,
    )
    from cpfc_trip.planner.links import dated_recovery_links
    from cpfc_trip.planner.planning import (
        choose,
        enumerate_specs,
        feasible_journey,
        feasible_stay,
        grow_preview,
        journey_stay_dates,
    )
    from cpfc_trip.planner.providers.searchapi import (
        PRIVATE_BATHROOM_UNVERIFIED_GAP,
        REQUEST_CAPS,
        SEARCH_ACTIVITY_ATTEMPTS,
        STAY_UNVERIFIED_GAP,
        fingerprint,
    )
    from cpfc_trip.planner.recorded import sample

FLIGHT_REASONS = {"no_flights", "flight_timing", "flights_unverified", "connections", "onward_rail"}
RECOVERY_MIN_SECONDS = 20
RECOVERY_GRACE_SECONDS = 5
SLOW_PREFERRED_SECONDS = 10
Job = tuple[str, SearchSpec, SearchBatch, int]
Probe = tuple[str, SearchSpec, RecoveryAction, str, Brief]
AIRPORT_FIXABLE = {"provider_empty", "no_timely_flight", "unverified", "unsupported_connection"}


class RecoveryChecks:
    def __init__(
        self,
        fixture: Fixture,
        search: Callable[[str, SearchSpec, int], Coroutine[Any, Any, SearchBatch]],
    ) -> None:
        self.fixture = fixture
        self.search = search
        self.probes: list[Probe] = []
        self.queue: list[int] = []
        self.running: dict[asyncio.Task[SearchBatch], int] = {}
        self.verified: set[int] = set()
        self.negative: list[int] = []
        self.held = 0

    def add(self, probes: list[Probe]) -> None:
        known = {probe[3] for probe in self.probes}
        for probe in probes:
            if probe[3] not in known:
                self.queue.append(len(self.probes))
                self.probes.append(probe)

    def start(self, available: int) -> None:
        while self.queue and REQUEST_CAPS[self.probes[self.queue[0]][0]] <= available - self.held:
            index = self.queue.pop(0)
            kind, spec = self.probes[index][:2]
            self.held += REQUEST_CAPS[kind]
            self.running[asyncio.create_task(self.search(kind, spec, 1))] = index

    def collect(self) -> None:
        for task in [task for task in self.running if task.done()]:
            index = self.running.pop(task)
            kind, spec, _, _, brief = self.probes[index]
            result = task.result()
            self.held += min(REQUEST_CAPS[kind], result.calls) - REQUEST_CAPS[kind]
            if result.stale:
                continue
            if any(
                feasible_journey(journey, self.fixture, brief, spec.route)
                for journey in result.journeys
            ) or (
                kind == "stay"
                and any(feasible_stay(stay, self.fixture, brief) for stay in result.stays)
            ):
                self.verified.add(index)
            elif (kind == "flight" and result.failure in AIRPORT_FIXABLE) or (
                kind == "stay" and result.failure is None and result.stays
            ):
                self.negative.append(index)

    async def cancel(self) -> None:
        for task in self.running:
            task.cancel()
        await asyncio.gather(*self.running, return_exceptions=True)
        self.running.clear()

    def fits(self, index: int, reason: FailureReason) -> bool:
        kind = self.probes[index][2].kind
        if reason == "private_bathroom":
            return kind == "without_private_bathroom"
        return reason in FLIGHT_REASONS and kind != "without_private_bathroom"

    def relevant(self, reason: FailureReason) -> list[str]:
        return [probe[3] for index, probe in enumerate(self.probes) if self.fits(index, reason)]

    def checked_for(self, reason: FailureReason) -> list[str]:
        return [
            self.probes[index][3] for index in sorted(self.negative) if self.fits(index, reason)
        ]

    def actions(self, reason: FailureReason) -> tuple[RecoveryAction, ...]:
        return tuple(
            self.probes[index][2] for index in sorted(self.verified) if self.fits(index, reason)
        )[:3]


class NoCompleteTrip(Exception):
    """A specific, safe explanation for a search with no verifiable travel."""

    def __init__(
        self,
        message: str,
        *,
        reason: FailureReason,
        actions: tuple[RecoveryAction, ...] = (),
        searched: tuple[DatePair, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.actions = actions
        self.searched = searched


def short_date(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def date_range(spec: SearchSpec) -> str:
    return f"{short_date(spec.outbound_date)} to {short_date(spec.return_date)}"


def airport_city(city: str) -> str:
    return f"{city} ({', '.join(airports_for(city))})"


@workflow.defn(name="TravelPlanningSessionWorkflowV5")
class TravelPlanningSessionWorkflow:
    @workflow.init
    def __init__(self, data: SessionInput):
        self.data = data
        self.explanation: str | None = None
        self.checks: RecoveryChecks | None = None
        departure_city = data.brief.departure_city or data.brief.origin_city
        self.state = Snapshot(
            development_mode=data.planner_mode == "recorded",
            public_session_id=data.public_session_id,
            origin_city=data.brief.origin_city,
            departure_city=departure_city,
            progress_message=f"Checking routes from {departure_city}…",
        )

    def change(self, **fields: Any) -> None:
        self.state = self.state.model_copy(
            update={**fields, "state_revision": self.state.state_revision + 1}
        )

    @workflow.query
    def get_snapshot(self, after_revision: int | None = None) -> Snapshot | None:
        if after_revision == self.state.state_revision:
            return None
        return self.state

    def recovery_links(self) -> tuple[RecoveryLink, ...]:
        window_ids = {window.fixture_id for window in self.data.brief.windows}
        if self.data.planner_mode == "recorded" or not window_ids.issuperset(
            fixture.id for fixture in self.data.fixtures
        ):
            return ()
        specs = enumerate_specs(
            self.data.brief,
            self.data.fixtures,
            self.data.routes,
            screen_impossible_dates=True,
        )
        if not specs:
            return ()
        preview = self.state.preview_trip
        preferred = specs[0]
        dates = None
        if preview and preview.stay:
            dates = (preview.stay.check_in, preview.stay.check_out)
        elif preview and preview.journey:
            dates = journey_stay_dates(preview.journey, preview.fixture)
        if preview and dates:
            preferred = next(
                (
                    spec
                    for spec in specs
                    if spec.fixture.id == preview.fixture.id
                    and (spec.outbound_date, spec.return_date) == dates
                ),
                preferred,
            )
        need_flights = preview is None or preview.journey is None
        need_stays = preview is None or preview.stay is None
        return dated_recovery_links(
            preferred, include_flights=need_flights, include_stays=need_stays
        )

    async def search(
        self, kind: str, spec: SearchSpec, attempts: int = SEARCH_ACTIVITY_ATTEMPTS
    ) -> SearchBatch:
        name = (
            "search_stays"
            if kind == "stay"
            else "search_flights"
            if kind == "flight"
            else "search_trains"
        )
        try:
            batch: SearchBatch = await workflow.execute_activity(
                name,
                spec,
                result_type=SearchBatch,
                start_to_close_timeout=timedelta(seconds=85),
                schedule_to_close_timeout=timedelta(seconds=175),
                heartbeat_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=attempts),
            )
            return batch
        except Exception as error:
            cause = getattr(error, "cause", None) or error
            if not isinstance(cause, TemporalCancelledError):
                workflow.logger.warning(
                    "Search activity failed: activity=%s cause=%s", name, type(cause).__name__
                )
            return SearchBatch(
                gaps=("This travel search could not be completed.",),
                calls=REQUEST_CAPS[kind] * attempts,
            )

    async def plan(self, deadline: datetime) -> Itinerary:
        brief = self.data.brief
        self.change(phase="researching", progress_message="Checking your travel preferences…")
        if brief.transport_mode == "rail" and not any(
            route.mode == "rail" for route in self.data.routes
        ):
            raise NoCompleteTrip("Train search is not available from this city.", reason="no_route")
        recorded = self.data.planner_mode == "recorded"
        specs = enumerate_specs(
            brief,
            self.data.fixtures,
            self.data.routes,
            recorded=recorded,
            screen_impossible_dates=not recorded,
        )
        if (
            not recorded
            and not specs
            and enumerate_specs(brief, self.data.fixtures, self.data.routes)
        ):
            raise NoCompleteTrip(
                "The selected dates don’t leave enough time to return after the match. "
                "Try a later return date.",
                reason="dates",
            )
        departure_city = brief.departure_city or brief.origin_city
        routes = self.data.routes
        used = 0
        batches: dict[str, list[SearchBatch]] = {f.id: [] for f in self.data.fixtures}
        flight_batches: list[SearchBatch] = []
        attempted: dict[DatePair, bool] = {}
        by_fixture = {f.id: [s for s in specs if s.fixture.id == f.id] for f in self.data.fixtures}
        waves = [
            [options[index] for options in by_fixture.values() if index < len(options)]
            for index in range(max(map(len, by_fixture.values()), default=0))
        ]
        seen: set[str] = set()
        self.change(progress_message="Comparing transport and accommodation…")
        semaphore = asyncio.Semaphore(4)
        first_journey_at: datetime | None = None

        def show_partial_trip(progress: bool = True) -> None:
            nonlocal first_journey_at
            previous = self.state.preview_trip
            preview, journey_found = grow_preview(
                previous, self.data.fixtures, batches, routes, brief
            )
            if journey_found and first_journey_at is None:
                first_journey_at = workflow.now()
            if preview is not None and preview is not previous:
                earlier = self.state.preview_options
                if previous and (
                    (previous.journey and previous.journey != preview.journey)
                    or (previous.stay and previous.stay != preview.stay)
                ):
                    earlier = (*earlier, previous)[-2:]
                self.change(
                    preview_trip=preview,
                    preview_options=earlier,
                    alternate_origin_city=brief.departure_city if preview.journey else None,
                    progress_message=(
                        self.state.progress_message
                        if not progress
                        else "A journey and stay were found. Comparing options…"
                        if preview.journey and preview.stay
                        else "A journey was found. Checking stays and alternatives…"
                        if preview.journey
                        else "A stay was found. Checking return travel…"
                    ),
                )

        async def acquire_job(kind: str, spec: SearchSpec, cap: int) -> Job:
            async with semaphore:
                response = await self.search(kind, spec)
            return kind, spec, response, cap

        checks = self.checks = (
            RecoveryChecks(self.data.fixtures[0], self.search)
            if not recorded and specs and len(self.data.fixtures) == 1
            else None
        )

        def held() -> int:
            return checks.held if checks else 0

        started_at = workflow.now()
        preferred_pending = True
        other_dates_failed = False
        nearby_requested = False

        def record(task: asyncio.Task[Job]) -> None:
            nonlocal used, preferred_pending, other_dates_failed, nearby_requested
            kind, spec, response, cap = task.result()
            used -= cap - min(cap, response.calls)
            batches[spec.fixture.id].append(response)
            if (
                kind != "stay"
                and not response.stale
                and (response.journeys or response.failure in AIRPORT_FIXABLE)
            ):
                pair = DatePair(outbound_date=spec.outbound_date, return_date=spec.return_date)
                attempted[pair] = True
            if kind != "flight":
                return
            flight_batches.append(response)
            failed = response.failure in AIRPORT_FIXABLE and not any(
                feasible_journey(journey, spec.fixture, brief, spec.route)
                for journey in response.journeys
            )
            if spec == specs[0]:
                preferred_pending = False
                if checks and failed:
                    nearby_requested = True
                    checks.add(self.airport_probes(spec))
            elif failed:
                other_dates_failed = True

        def nearby_due() -> datetime | None:
            if checks and preferred_pending and other_dates_failed and not nearby_requested:
                return started_at + timedelta(seconds=SLOW_PREFERRED_SECONDS)
            return None

        def speculate(pending_wave_calls: int = 0) -> None:
            if checks is None:
                return
            checks.collect()
            still_needed = sum(
                REQUEST_CAPS[kind] * SEARCH_ACTIVITY_ATTEMPTS
                for wave in rounds
                for spec in wave
                for kind in (spec.route.mode, "stay")
            )
            checks.start(self.data.limits.search_calls - used - still_needed - pending_wave_calls)

        def settle_stay(task: asyncio.Task[Job]) -> None:
            record(task)
            show_partial_trip(progress=False)

        unfinished_stays: list[asyncio.Task[Job]] = []
        kinds: dict[asyncio.Task[Job], str] = {}

        trips: tuple[Trip, ...] = ()
        rounds = list(waves)
        started_waves = 0
        while rounds:
            wave = rounds.pop(0)
            comparison = rounds.pop(0) if started_waves == 0 and rounds else []
            if not recorded and (deadline - workflow.now()).total_seconds() <= 2:
                break
            needed = sum(
                REQUEST_CAPS[kind] * SEARCH_ACTIVITY_ATTEMPTS
                for spec in wave
                for kind in (spec.route.mode, "stay")
            )
            while unfinished_stays and used + held() + needed > self.data.limits.search_calls:
                done, _ = await workflow.wait(
                    unfinished_stays,
                    timeout=max(0, (deadline - workflow.now()).total_seconds() - 2),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                for task in [task for task in unfinished_stays if task in done]:
                    record(task)
                    unfinished_stays.remove(task)
                show_partial_trip()
                speculate(needed)
            jobs: list[tuple[str, SearchSpec, int]] = []
            deferred: list[SearchSpec] = []
            for spec, is_comparison in [
                *((spec, False) for spec in wave),
                *((spec, True) for spec in comparison),
            ]:
                for kind in (spec.route.mode, "stay"):
                    if recorded:
                        response = sample(spec, kind)
                        batches[spec.fixture.id].append(response)
                        if kind == "flight":
                            flight_batches.append(response)
                        continue
                    key = fingerprint(
                        (
                            spec.fixture.id,
                            kind,
                            spec.route.id if kind != "stay" else "",
                            str(spec.outbound_date),
                            str(spec.return_date),
                            spec.party.model_dump(mode="json"),
                            (spec.budget_tier, spec.private_room, spec.private_bathroom)
                            if kind == "stay"
                            else None,
                        )
                    )
                    if key in seen:
                        continue
                    cap = REQUEST_CAPS[kind] * SEARCH_ACTIVITY_ATTEMPTS
                    if used + held() + cap > self.data.limits.search_calls:
                        if not is_comparison:
                            seen.add(key)
                        elif spec not in deferred:
                            deferred.append(spec)
                        continue
                    seen.add(key)
                    used += cap
                    jobs.append((kind, spec, cap))
                    if kind != "stay":
                        attempted.setdefault(
                            DatePair(
                                outbound_date=spec.outbound_date, return_date=spec.return_date
                            ),
                            False,
                        )
            started_waves += 1 + bool(comparison)
            if deferred:
                rounds.insert(0, deferred)
            timed_out = False
            show_partial_trip()
            if jobs:
                started = [asyncio.create_task(acquire_job(*job)) for job in jobs]
                kinds.update(zip(started, (job[0] for job in jobs), strict=True))
                order = [*unfinished_stays, *started]
                unfinished_stays = []
                pending = order
                while pending:
                    search_end = deadline - timedelta(seconds=2)
                    if first_journey_at is not None:
                        search_end = min(search_end, first_journey_at + timedelta(seconds=30))
                    due = nearby_due()
                    wake = min(search_end, due) if due else search_end
                    done, waiting = await workflow.wait(
                        pending,
                        timeout=max(0, (wake - workflow.now()).total_seconds()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    pending = [task for task in order if task in waiting]
                    if not done and due and checks and workflow.now() < search_end:
                        nearby_requested = True
                        checks.add(self.airport_probes(specs[0]))
                        speculate()
                        continue
                    if not done:
                        timed_out = True
                        break
                    for task in order:
                        if task in done:
                            record(task)
                    show_partial_trip()
                    speculate()
                    if (
                        first_journey_at is None
                        and flight_batches
                        and all(kinds[task] == "stay" for task in pending)
                    ):
                        unfinished_stays, pending = pending, []
                        break
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            trips = tuple(choose(f, batches[f.id], brief, routes) for f in self.data.fixtures)
            if (
                timed_out
                or (
                    started_waves >= 2 and not deferred and all(t.journey and t.stay for t in trips)
                )
                or (
                    first_journey_at is not None
                    and workflow.now() >= first_journey_at + timedelta(seconds=30)
                )
            ):
                break
        if not any(t.journey and t.stay for t in trips):
            searched = tuple(pair for pair, completed in attempted.items() if completed)[:6]
            reason = self.diagnose(specs, batches, flight_batches)
            message = self.explain(reason, specs, departure_city)
            actions: tuple[RecoveryAction, ...] = ()
            checked: list[str] = []
            if checks:
                actions, checked = await self.verify_recovery(
                    reason,
                    message,
                    specs,
                    searched,
                    deadline,
                    checks,
                    lambda: self.data.limits.search_calls - used,
                    unfinished_stays,
                    settle_stay,
                )
            if unfinished_stays:
                preview = self.state.preview_trip
                if not actions and not (preview and preview.stay):
                    seconds = min(5, (deadline - workflow.now()).total_seconds() - 2)
                    await workflow.wait(unfinished_stays, timeout=max(0, seconds))
                for task in unfinished_stays:
                    if task.done():
                        record(task)
                    else:
                        task.cancel()
                await asyncio.gather(*unfinished_stays, return_exceptions=True)
                show_partial_trip()
            if checks:
                await checks.cancel()
            if checked and not actions:
                message += f" We also checked {self.join(checked)} and found nothing that fits."
            raise NoCompleteTrip(message, reason=reason, actions=actions, searched=searched)
        if checks:
            await checks.cancel()
        for task in unfinished_stays:
            task.cancel()
        await asyncio.gather(*unfinished_stays, return_exceptions=True)
        return Itinerary(
            generated_at=workflow.now(),
            trips=trips,
            caveats=(
                "Prices may have changed since retrieval. Confirm the full price before booking.",
                f"Match tickets and travel to/from your {departure_city} departure hub are not included.",
            ),
        )

    @staticmethod
    def join(items: list[str]) -> str:
        return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"

    def diagnose(
        self,
        specs: list[SearchSpec],
        batches: dict[str, list[SearchBatch]],
        flight_batches: list[SearchBatch],
    ) -> FailureReason:
        brief = self.data.brief
        by_id = {route.id: route for route in self.data.routes}
        if any(
            feasible_journey(journey, fixture, brief, by_id.get(journey.route_id))
            for fixture in self.data.fixtures
            for batch in batches[fixture.id]
            for journey in batch.journeys
            if journey.route_id in by_id
        ):
            bathroom_unverified = (
                brief.private_bathroom
                and not any(batch.stays for results in batches.values() for batch in results)
                and any(
                    gap in (PRIVATE_BATHROOM_UNVERIFIED_GAP, STAY_UNVERIFIED_GAP)
                    for results in batches.values()
                    for batch in results
                    for gap in batch.gaps
                )
            )
            stay_batches = [
                batch
                for results in batches.values()
                for batch in results
                if not any(batch is flight for flight in flight_batches)
            ]
            if not any(batch.stays for batch in stay_batches) and any(
                batch.failure == "provider_error" for batch in stay_batches
            ):
                return "unavailable"
            return "private_bathroom" if bathroom_unverified else "stay"
        if not specs:
            return "no_route"
        if not all(spec.route.mode == "flight" for spec in specs) or not flight_batches:
            return "search_limits"
        failures = {batch.failure for batch in flight_batches}
        if "unverified" in failures:
            return "flights_unverified"
        if "unsupported_connection" in failures:
            return "connections"
        if any(batch.journeys for batch in flight_batches) or "no_timely_flight" in failures:
            return "flight_timing"
        if "onward_rail" in failures:
            return "onward_rail"
        if failures == {"provider_empty"}:
            return "no_flights"
        if "provider_error" in failures:
            return "unavailable"
        return "search_limits"

    def explain(self, reason: FailureReason, specs: list[SearchSpec], departure_city: str) -> str:
        fixture = self.data.fixtures[0]
        onward = bool(specs) and specs[0].route.onward_stations == ("Warsaw", "Białystok")
        return {
            "no_flights": (
                f"No flights from {departure_city} to "
                f"{'Warsaw' if onward else fixture.city} were listed on the dates we checked."
            ),
            "flight_timing": (
                f"No flight from {departure_city} lands in Warsaw early enough for a train that "
                "reaches Białystok five hours before kickoff, with a way back after the match, "
                "on the dates we checked."
                if onward
                else f"No flight from {departure_city} arrives five hours before kickoff and "
                "returns after the match on the dates we checked."
            ),
            "flights_unverified": (
                f"Flights from {departure_city} looked suitable, but we couldn’t confirm "
                "booking details for a complete return journey on the dates we checked."
            ),
            "connections": (
                f"Flights from {departure_city} that fit the match change airports on the way "
                "(for example Heathrow to Gatwick) or have a connection we can’t check, so we "
                "can’t plan them as one journey."
            ),
            "onward_rail": (
                "We couldn’t verify train times for the Warsaw–Białystok connection on the "
                "dates we checked."
            ),
            "stay": (
                "We found a return journey, but couldn’t verify a stay that fits its dates and "
                "room requirements."
            ),
            "private_bathroom": (
                "We found a return journey, but couldn’t confirm a private bathroom in the "
                "rooms we checked."
            ),
            "search_limits": (
                "We couldn't verify a complete trip within the search limits. Try again or "
                "adjust your preferences."
            ),
            "no_route": (
                "No supported travel route was available for this match, city and travel mode. "
                "Try another starting city or travel mode."
            ),
            "dates": "The selected dates don’t leave enough time to return after the match.",
            "unavailable": (
                "Travel search is busy right now, so we couldn’t check every option. Please "
                "try again in a few minutes."
            ),
        }[reason]

    def airport_probes(self, preferred: SearchSpec) -> list[Probe]:
        brief = self.data.brief
        probes: list[Probe] = []
        if preferred.route.mode != "flight":
            return probes
        for city in alternate_departure_cities(brief.origin_city):
            if city == brief.departure_city:
                continue
            route = alternate_departure_route(preferred.route, brief.origin_city, city)
            if route is None:
                continue
            probe = preferred.model_copy(update={"route": route})
            action = RecoveryAction(
                kind="depart_from",
                departure_city=city,
                label=f"Plan from {city}",
                detail=f"Flights from {airport_city(city)} fit the match, {date_range(probe)}.",
            )
            probes.append(("flight", probe, action, f"flights from {city}", brief))
        return probes

    async def verify_recovery(
        self,
        reason: FailureReason,
        message: str,
        specs: list[SearchSpec],
        searched: tuple[DatePair, ...],
        deadline: datetime,
        checks: "RecoveryChecks",
        remaining: Callable[[], int],
        stays: list[asyncio.Task[Job]],
        settle: Callable[[asyncio.Task[Job]], None],
    ) -> tuple[tuple[RecoveryAction, ...], list[str]]:
        brief = self.data.brief
        fixture = checks.fixture
        preferred = specs[0]
        if reason in FLIGHT_REASONS:
            checks.add(self.airport_probes(preferred))
            wider = self.wider_brief()
            dated = self.wider_spec(wider, specs) if wider else None
            if wider and dated:
                action = RecoveryAction(
                    kind="wider_dates",
                    label="Search two days either side",
                    detail=f"Flights from {brief.departure_city or brief.origin_city} fit the "
                    f"match, {date_range(dated)}.",
                )
                checks.add([("flight", dated, action, "two days either side", wider)])
        elif reason == "private_bathroom":
            trip = self.state.preview_trip
            check_in, check_out = (
                journey_stay_dates(trip.journey, fixture)
                if trip and trip.journey
                else (preferred.outbound_date, preferred.return_date)
            )
            relaxed = brief.model_copy(update={"private_bathroom": False})
            probe = preferred.model_copy(
                update={
                    "outbound_date": check_in,
                    "return_date": check_out,
                    "private_bathroom": False,
                }
            )
            action = RecoveryAction(
                kind="without_private_bathroom",
                label="Show rooms without a confirmed private bathroom",
                detail=f"Rooms in {fixture.city} are available, {date_range(probe)}; "
                "their bathroom isn’t confirmed.",
            )
            checks.add([("stay", probe, action, "rooms without a private bathroom", relaxed)])
        relevant = checks.relevant(reason)
        if not relevant:
            return (), []
        late = (deadline - workflow.now()).total_seconds() < RECOVERY_MIN_SECONDS
        if not late:
            self.explanation = message
            self.change(
                failure_reason=reason,
                searched_dates=searched,
                progress_message=f"{message} Checking {self.join(relevant)}…",
            )
        answer_by: datetime | None = None
        while True:
            checks.collect()
            if answer_by is None and any(
                action.kind == "depart_from" for action in checks.actions(reason)
            ):
                answer_by = workflow.now() + timedelta(seconds=RECOVERY_GRACE_SECONDS)
            if not late:
                checks.start(remaining())
            if not checks.running and not (checks.queue and stays and not late):
                break
            end = deadline - timedelta(seconds=2)
            if answer_by:
                end = min(end, answer_by)
            timeout = (end - workflow.now()).total_seconds()
            if timeout <= 0:
                break
            done, _ = await workflow.wait(
                [*checks.running, *stays], timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break
            for stay_task in [task for task in stays if task in done]:
                settle(stay_task)
                stays.remove(stay_task)
        checks.collect()
        await checks.cancel()
        return checks.actions(reason), checks.checked_for(reason)

    def wider_brief(self) -> Brief | None:
        brief = self.data.brief
        if brief.flexibility == "two_days":
            return None
        try:
            wider = validate_brief(
                brief.model_copy(update={"flexibility": "two_days", "windows": ()}),
                self.data.fixtures,
                workflow.now(),
            )
        except ValueError:
            return None
        windows = {window.fixture_id: window for window in brief.windows}
        widens = any(
            wide.fixture_id in windows
            and (
                wide.earliest_departure < windows[wide.fixture_id].earliest_departure
                or wide.latest_return > windows[wide.fixture_id].latest_return
            )
            for wide in wider.windows
        )
        return wider if widens else None

    def wider_spec(self, wider: Brief, original: list[SearchSpec]) -> SearchSpec | None:
        original_dates = {(spec.outbound_date, spec.return_date) for spec in original}
        return next(
            (
                spec
                for spec in enumerate_specs(
                    wider, self.data.fixtures, self.data.routes, screen_impossible_dates=True
                )
                if (spec.outbound_date, spec.return_date) not in original_dates
            ),
            None,
        )

    @workflow.run
    async def run(self, data: SessionInput) -> Snapshot:
        try:
            itinerary = await asyncio.wait_for(
                self.plan(workflow.now() + timedelta(seconds=data.limits.planning_seconds)),
                timeout=data.limits.planning_seconds,
            )
        except Exception as exc:
            # Deliberately omit raw provider errors; partial results stay visible.
            known = isinstance(exc, NoCompleteTrip)
            if not known:
                workflow.logger.exception("Planning ended unexpectedly")
            explained = not known and self.explanation and self.state.failure_reason
            self.change(
                phase="failed",
                alternate_origin_city=self.state.alternate_origin_city
                if self.state.preview_trip and self.state.preview_trip.journey
                else None,
                failure_reason=exc.reason
                if isinstance(exc, NoCompleteTrip)
                else self.state.failure_reason
                if explained
                else "search_limits",
                recovery_actions=exc.actions
                if isinstance(exc, NoCompleteTrip)
                else self.checks.actions(self.state.failure_reason)
                if explained and self.checks and self.state.failure_reason
                else (),
                searched_dates=exc.searched
                if isinstance(exc, NoCompleteTrip)
                else self.state.searched_dates,
                recovery_links=self.recovery_links(),
                progress_message=str(exc)
                if known
                else self.explanation
                if explained
                else "We couldn't find a complete trip matching this request within the search "
                "limits. Try again or adjust your preferences.",
            )
            workflow.logger.info(
                "Planning finished: outcome=failed reason=%s actions=%d",
                self.state.failure_reason,
                len(self.state.recovery_actions),
            )
            return self.state
        visible_options = [(trip.fixture.id, trip.journey, trip.stay) for trip in itinerary.trips]
        visible_options.extend(
            (trip.fixture.id, option.journey, option.stay)
            for trip in itinerary.trips
            for option in trip.alternatives
        )
        earlier = tuple(
            preview
            for preview in (*self.state.preview_options, self.state.preview_trip)
            if preview
            and not any(
                fixture_id == preview.fixture.id
                and (preview.journey is None or preview.journey == journey)
                and (preview.stay is None or preview.stay == stay)
                for fixture_id, journey, stay in visible_options
            )
        )
        self.change(
            itinerary=itinerary,
            preview_trip=None,
            preview_options=earlier[-2:],
            phase="closed",
            progress_message="Your trip is ready. Check the outstanding details before booking.",
        )
        workflow.logger.info("Planning finished: outcome=closed")
        return self.state
