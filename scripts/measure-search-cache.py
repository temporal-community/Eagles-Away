"""Time one live trip search cold, then again from the shared search cache.

Paid: the cold run makes real SearchApi calls (bounded by the workflow's search budget); warm
runs should make none. Uses a local Temporal dev server and a throwaway SQLite database under
.data/, so neither the local PostgreSQL cache nor production is touched.

    uv run python scripts/measure-search-cache.py --city Białystok --origin London --warm-runs 2
"""

import argparse
import asyncio
import shutil
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from cpfc_trip.catalog import load_catalog
from cpfc_trip.config import Settings
from cpfc_trip.domain import Brief, CreateSession, SearchBatch, SearchSpec, SessionInput
from cpfc_trip.persistence.database import engine
from cpfc_trip.persistence.models import Base
from cpfc_trip.persistence.repository import Repository
from cpfc_trip.persistence.search_cache import SearchCache, SearchMode
from cpfc_trip.temporal.activities import SearchActivities
from cpfc_trip.temporal.workflow import TravelPlanningSessionWorkflow as Workflow

DATABASE = Path(".data/cache-benchmark.db")


class MeasuredSearch(SearchActivities):
    """Record each search's source (cache or provider), latency and provider calls."""

    def __init__(self, cache: SearchCache) -> None:
        super().__init__(cache)
        self.events: list[tuple[str, SearchMode, float, int]] = []

    async def _cached_or_live(self, spec: SearchSpec, mode: SearchMode) -> SearchBatch:
        started = time.monotonic()
        result = await super()._cached_or_live(spec, mode)
        source = (
            "cache"
            if result.calls == 0 and not result.stale and result.failure != "provider_error"
            else "provider"
        )
        self.events.append((source, mode, time.monotonic() - started, result.calls))
        return result


async def run_once(env: WorkflowEnvironment, search: MeasuredSearch, data: SessionInput) -> dict:
    search.events.clear()
    queue = str(uuid4())
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[Workflow],
        activities=[search.search_flights, search.search_trains, search.search_stays],
    ):
        started = time.monotonic()
        handle = await env.client.start_workflow(
            Workflow.run, data, id=f"cache-benchmark/{uuid4()}", task_queue=queue
        )
        first_preview = None
        while True:
            state = await handle.query(Workflow.get_snapshot)
            if first_preview is None and state.preview_trip is not None:
                first_preview = time.monotonic() - started
            if state.phase in {"closed", "failed"}:
                break
            await asyncio.sleep(0.25)
        final = await handle.result()
    total = time.monotonic() - started
    sources = Counter(source for source, *_ in search.events)
    return {
        "phase": final.phase,
        "first_result_s": round(first_preview if first_preview is not None else total, 1),
        "total_s": round(total, 1),
        "provider_searches": sources["provider"],
        "cache_hits": sources["cache"],
        "searchapi_calls": sum(calls for *_, calls in search.events),
        "slowest_provider_s": round(
            max((s for src, _, s, _ in search.events if src == "provider"), default=0), 1
        ),
        "known_total": (
            f"{money.minor_units / 100:.2f} {money.currency}"
            if final.itinerary and (money := final.itinerary.trips[0].known_total)
            else "-"
        ),
    }


async def main(city: str, origin: str, warm_runs: int, ttl_minutes: int) -> None:
    database_file = DATABASE
    settings = Settings(planner_mode="live", database_url=f"sqlite+aiosqlite:///{database_file}")
    if not settings.searchapi_api_key.get_secret_value():
        raise SystemExit("SEARCHAPI_API_KEY is required for a live benchmark")
    db = engine(settings)
    try:
        async with db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        fixture = next(f for f in load_catalog()[0] if f.city == city)
        data = await Repository(db, settings).create(
            CreateSession(
                submission_id=uuid4(), brief=Brief(fixture_ids=(fixture.id,), origin_city=origin)
            ),
            str(uuid4()),
        )
        search = MeasuredSearch(SearchCache(db, timedelta(minutes=ttl_minutes)))
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=shutil.which("temporal"),
            data_converter=pydantic_data_converter,
        ) as env:
            rows = [("cold", await run_once(env, search, data))]
            for index in range(warm_runs):
                rows.append((f"warm {index + 1}", await run_once(env, search, data)))
    finally:
        await db.dispose()
        await asyncio.to_thread(database_file.unlink, missing_ok=True)
    columns = list(rows[0][1])
    print(f"\n{origin} → {city}, cache TTL {ttl_minutes} min\n")
    print("| run | " + " | ".join(columns) + " |")
    print("|---|" + "---|" * len(columns))
    for label, row in rows:
        print(f"| {label} | " + " | ".join(str(row[c]) for c in columns) + " |")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--city", default="Białystok", help="Fixture city from the catalog")
    parser.add_argument("--origin", default="London", help="Supported UK departure city")
    parser.add_argument("--warm-runs", type=int, default=1)
    parser.add_argument("--ttl-minutes", type=int, default=1440)
    args = parser.parse_args()
    DATABASE.parent.mkdir(exist_ok=True)
    DATABASE.unlink(missing_ok=True)
    asyncio.run(main(args.city, args.origin, args.warm_runs, args.ttl_minutes))
