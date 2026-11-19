"""Offline end-to-end recovery benchmark (no paid SearchApi calls).

Runs the real FastAPI app, a local Temporal dev server, the real Workflow and the real
SearchActivities (with a fresh sqlite search cache) against the SIMULATED inventory in
scripts/benchmark/inventory.py. Works against the current tree and against a frozen copy of
older source (put it first on PYTHONPATH); fields are read from raw snapshot JSON.

    uv run --locked python scripts/benchmark/recovery_benchmark.py --out .data/benchmark/after.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import os
import secrets
import shutil
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

os.environ.update(APP_ENV="test", SEARCHAPI_ENABLED="true", SEARCHAPI_API_KEY="benchmark")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import inventory  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from temporalio.contrib.pydantic import pydantic_data_converter  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import cpfc_trip  # noqa: E402
import cpfc_trip.api as api_module  # noqa: E402
from cpfc_trip.config import Settings  # noqa: E402
from cpfc_trip.persistence.database import engine  # noqa: E402
from cpfc_trip.persistence.models import Base, SearchCacheRow  # noqa: E402
from cpfc_trip.persistence.search_cache import SearchCache  # noqa: E402
from cpfc_trip.temporal.activities import SearchActivities  # noqa: E402
from cpfc_trip.temporal.workflow import TravelPlanningSessionWorkflow  # noqa: E402

HARNESS_ORIGIN = "http://eagles-benchmark.test"
POLL_SECONDS = 0.25
SESSION_TIMEOUT_SECONDS = 240
FIXTURES = {"Białystok": "uel-2026-jagiellonia-away", "Salzburg": "uel-2026-salzburg-away"}
ORIGINS = (
    "London",
    "Liverpool",
    "Manchester",
    "Belfast",
    "Birmingham",
    "Bristol",
    "Edinburgh",
    "Glasgow",
    "Newcastle",
)
VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "private room+bath": {"private_room": True, "private_bathroom": True},
    "wider dates": {"flexibility": "two_days"},
}


@dataclass(frozen=True)
class Case:
    order: int
    variant: str
    origin: str
    match: str

    @property
    def id(self) -> str:
        return f"{self.variant} | {self.origin} | {self.match}"

    def brief(self) -> dict[str, Any]:
        return {
            "origin_city": self.origin,
            "fixture_ids": [FIXTURES[self.match]],
            "transport_mode": "flight",
            "travellers": {"adults": 1, "rooms": 1, "child_ages": []},
            "budget_tier": "value",
            **VARIANTS[self.variant],
        }


def build_cases(selection: str, repeat: int = 1) -> list[Case]:
    specs = [("baseline", origin, match) for origin in ORIGINS for match in FIXTURES]
    if selection == "all":
        specs += [("private room+bath", o, m) for o in ("London", "Manchester") for m in FIXTURES]
        specs += [("wider dates", "Belfast", m) for m in FIXTURES]
    return [Case(i + 1, *spec) for i, spec in enumerate(specs * repeat)]


def offered_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if "recovery_actions" in snapshot:
        return [
            {
                "kind": action.get("kind"),
                "label": action.get("label"),
                "detail": action.get("detail"),
                "departure_city": action.get("departure_city"),
            }
            for action in snapshot.get("recovery_actions") or []
        ]
    city = snapshot.get("suggested_origin_city")
    if not city:
        return []
    return [
        {
            "kind": "depart_from",
            "label": f"Search from {city}",
            "detail": "legacy suggested_origin_city",
            "departure_city": city,
        }
    ]


def follow_up_brief(brief: dict[str, Any], action: dict[str, Any]) -> dict[str, Any] | None:
    kind = action["kind"]
    if kind == "depart_from" and action.get("departure_city"):
        return {**brief, "departure_city": action["departure_city"]}
    if kind == "wider_dates":
        return {**brief, "flexibility": "two_days", "windows": []}
    if kind == "without_private_bathroom":
        return {**brief, "private_bathroom": False}
    return None


def legs_summary(journey: dict[str, Any] | None) -> list[str] | None:
    if not journey:
        return None
    return [
        f"{leg['origin']}→{leg['destination']} {leg['departs_at'][:16]}→{leg['arrives_at'][:16]}"
        for direction in ("outbound", "inbound")
        for leg in journey.get(direction) or []
    ]


@dataclass
class Context:
    app: Any
    ledger: inventory.Ledger
    database: Any
    cold_cache: bool = False
    counter: itertools.count = field(default_factory=lambda: itertools.count(1))


async def clear_search_cache(database: Any) -> None:
    async with database.begin() as conn:
        await conn.execute(delete(SearchCacheRow))


async def run_session(ctx: Context, brief: dict[str, Any]) -> dict[str, Any]:
    number = next(ctx.counter)
    row: dict[str, Any] = {
        "brief": brief,
        "outcome": None,
        "t_first_option": None,
        "t_pair": None,
        "t_decision": None,
    }
    transport = httpx.ASGITransport(
        app=ctx.app, client=(f"198.18.{number // 250}.{number % 250 + 1}", 40000)
    )
    began = monotonic()
    snapshot: dict[str, Any] | None = None
    async with httpx.AsyncClient(transport=transport, base_url=HARNESS_ORIGIN) as client:
        response = await client.post(
            "/api/sessions",
            json={"submission_id": str(uuid4()), "brief": brief},
            headers={"Origin": HARNESS_ORIGIN, "X-Submission-Token": secrets.token_urlsafe(40)},
        )
        if response.status_code != 202:
            row.update(
                outcome="rejected", submit_status=response.status_code, detail=response.text[:500]
            )
            return row
        session_id = str(response.json()["public_session_id"])
        row["session"] = session_id
        try:
            async with asyncio.timeout(SESSION_TIMEOUT_SECONDS):
                while True:
                    response = await client.get(f"/api/sessions/{session_id}/snapshot")
                    if response.status_code == 200:
                        snapshot = response.json()
                        elapsed = round(monotonic() - began, 2)
                        preview = snapshot.get("preview_trip") or {}
                        itinerary = snapshot.get("itinerary")
                        journey, stay = bool(preview.get("journey")), bool(preview.get("stay"))
                        if row["t_first_option"] is None and (itinerary or journey or stay):
                            row["t_first_option"] = elapsed
                        if row["t_pair"] is None and (itinerary or (journey and stay)):
                            row["t_pair"] = elapsed
                        if snapshot.get("phase") in ("closed", "failed"):
                            row["t_decision"] = elapsed
                            break
                    await asyncio.sleep(POLL_SECONDS)
        except TimeoutError:
            row["timed_out"] = True
    provider = [r for r in ctx.ledger.rows if session_id in r["workflow"]]
    row["requests"] = len(provider)
    stages: dict[str, int] = {}
    for r in provider:
        stages[r["stage"]] = stages.get(r["stage"], 0) + 1
    row["requests_by_stage"] = stages
    snapshot = snapshot or {}
    preview = snapshot.get("preview_trip") or {}
    itinerary = snapshot.get("itinerary")
    shown = (
        "both"
        if preview.get("journey") and preview.get("stay")
        else ("journey" if preview.get("journey") else "stay" if preview.get("stay") else None)
    )
    row.update(
        outcome="complete" if itinerary else "partial" if shown else "empty",
        phase=snapshot.get("phase"),
        preview=shown,
        failure_reason=snapshot.get("failure_reason"),
        progress_message=snapshot.get("progress_message"),
        searched_dates=snapshot.get("searched_dates"),
        alternate_origin_city=snapshot.get("alternate_origin_city"),
        actions=offered_actions(snapshot),
        journey=legs_summary(
            itinerary["trips"][0].get("journey") if itinerary else preview.get("journey")
        ),
    )
    return row


async def run_case(ctx: Context, case: Case, gate: asyncio.Semaphore) -> dict[str, Any]:
    async with gate:
        if ctx.cold_cache:
            await clear_search_cache(ctx.database)
        brief = case.brief()
        result = await run_session(ctx, brief)
        row: dict[str, Any] = {
            "id": case.id,
            "order": case.order,
            "variant": case.variant,
            "origin": case.origin,
            "match": case.match,
            **result,
        }
        follow_ups = []
        for action in row["actions"]:
            next_brief = follow_up_brief(brief, action)
            if next_brief is None:
                follow_ups.append({**action, "follow_up": {"outcome": "unsupported action"}})
                continue
            outcome = await run_session(ctx, next_brief)
            outcome.pop("brief")
            outcome["action_kinds"] = [a["kind"] for a in outcome.pop("actions")]
            follow_ups.append({**action, "follow_up": outcome})
        row["actions"] = follow_ups
    complete = row["outcome"] == "complete"
    completing = [a for a in follow_ups if a["follow_up"].get("outcome") == "complete"]
    row["dead_ends"] = len(follow_ups) - len(completing)
    row["recovered"] = not complete and bool(completing)
    if complete:
        row["t_to_trip"] = row["t_pair"]
    elif completing and row["t_decision"] is not None:
        row["t_to_trip"] = round(
            row["t_decision"] + min(a["follow_up"]["t_pair"] for a in completing), 2
        )
    else:
        row["t_to_trip"] = None
    row["next_step_s"] = row["t_decision"] if complete or completing else None
    row["total_requests"] = row["requests"] + sum(
        a["follow_up"].get("requests", 0) for a in follow_ups
    )
    status = " ".join(
        f"[{a['kind']}{':' + a['departure_city'] if a.get('departure_city') else ''} → "
        f"{a['follow_up'].get('outcome')}]"
        for a in follow_ups
    )
    print(
        f"{case.order:02d} {case.id:<42} {row['outcome']:<8} {row.get('failure_reason') or '':<18} "
        f"decision {row['t_decision']}s, {row['requests']} req {status}",
        flush=True,
    )
    return row


def stat(values: list[float]) -> dict[str, Any]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0, "median": None, "p90": None}
    p90 = values[max(0, math.ceil(0.9 * len(values)) - 1)]
    return {"n": len(values), "median": round(statistics.median(values), 2), "p90": round(p90, 2)}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    complete = sum(r["outcome"] == "complete" for r in rows)
    after_tap = sum(r["outcome"] == "complete" or r["recovered"] for r in rows)
    return {
        "cases": n,
        "complete_initial": complete,
        "completion_rate_initial": round(complete / n, 3) if n else None,
        "complete_after_one_tap": after_tap,
        "completion_rate_after_one_tap": round(after_tap / n, 3) if n else None,
        "offers": sum(len(r["actions"]) for r in rows),
        "dead_end_offers": sum(r["dead_ends"] for r in rows),
        "terminal_no_action": sum(r["outcome"] != "complete" and not r["actions"] for r in rows),
        "t_first_option": stat([r["t_first_option"] for r in rows]),
        "t_pair": stat([r["t_pair"] for r in rows]),
        "t_decision": stat([r["t_decision"] for r in rows]),
        "t_to_trip": stat([r["t_to_trip"] for r in rows]),
        "next_step_s": stat([r["next_step_s"] for r in rows]),
        "requests_initial": sum(r["requests"] for r in rows),
        "requests_total": sum(r["total_requests"] for r in rows),
    }


def markdown(
    groups: dict[str, dict[str, Any]], rows: list[dict[str, Any]], meta: dict[str, Any]
) -> str:
    lines = [
        "# Recovery benchmark (SIMULATED inventory, offline)",
        "",
        f"- source: `{meta['source']}`",
        f"- workflow: `{meta['workflow']}`",
        f"- search cache: {meta['cache']}",
        f"- wall time: {meta['wall_seconds']} s; SearchApi requests intercepted: {meta['intercepted']}; "
        f"unsupported request shapes: {meta['unsupported']}",
        "",
        "| metric | " + " | ".join(groups) + " |",
        "|---|" + "---|" * len(groups),
    ]

    def cell(summary: dict[str, Any], key: str) -> str:
        value = summary[key]
        if isinstance(value, dict):
            return f"{value['median']} / {value['p90']} (n={value['n']})"
        return str(value)

    labels = {
        "cases": "cases",
        "completion_rate_initial": "completion rate (initial)",
        "completion_rate_after_one_tap": "completion after one recovery tap",
        "offers": "recovery offers",
        "dead_end_offers": "dead-end offers",
        "terminal_no_action": "terminal with no action",
        "t_first_option": "t_first_option s (median / p90)",
        "t_pair": "t_pair s",
        "t_decision": "t_decision s",
        "t_to_trip": "t_to_trip s",
        "next_step_s": "next_step s",
        "requests_initial": "SearchApi requests (initial runs)",
        "requests_total": "SearchApi requests (incl. follow-ups)",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | " + " | ".join(cell(s, key) for s in groups.values()) + " |")
    lines += [
        "",
        "| case | outcome | failure_reason | t_pair | t_decision | req | offers → follow-up |",
        "|---|---|---|---|---|---|---|",
    ]

    def seconds(value: float | None) -> str:
        return "—" if value is None else f"{value}"

    for r in rows:
        offers = "; ".join(
            f"{a['kind']}{' ' + a['departure_city'] if a.get('departure_city') else ''} → "
            f"{a['follow_up'].get('outcome')} (pair {seconds(a['follow_up'].get('t_pair'))} s, "
            f"{a['follow_up'].get('requests')} req)"
            for a in r["actions"]
        )
        lines.append(
            f"| {r['variant']}: {r['origin']} → {r['match']} | "
            f"{r['outcome']}{' (' + r['preview'] + ')' if r.get('preview') else ''} | "
            f"{r.get('failure_reason') or ''} | {seconds(r['t_pair'])} | {seconds(r['t_decision'])} | "
            f"{r['requests']} | {offers} |"
        )
    return "\n".join(lines) + "\n"


def temporal_binary() -> str | None:
    return shutil.which("temporal") or (
        "/opt/homebrew/bin/temporal" if Path("/opt/homebrew/bin/temporal").exists() else None
    )


class NoCache:
    async def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def put(self, *args: Any, **kwargs: Any) -> None:
        return None


async def run_cases(
    cases: list[Case],
    config: Settings,
    ledger: inventory.Ledger,
    concurrency: int,
    cold_cache: bool,
    no_cache: bool = False,
) -> list[dict[str, Any]]:
    database = engine(config)
    async with database.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_binary(),
        data_converter=pydantic_data_converter,
        dev_server_log_level="error",
    ) as env:
        search = SearchActivities(NoCache() if no_cache else SearchCache(database))
        activities = [
            getattr(search, name)
            for name in dir(type(search))
            if hasattr(getattr(type(search), name), "__temporal_activity_definition")
        ]
        app = api_module.create_app(config, temporal=env.client)
        ctx = Context(app=app, ledger=ledger, database=database, cold_cache=cold_cache)
        gate = asyncio.Semaphore(1 if cold_cache else max(1, concurrency))
        async with (
            app.router.lifespan_context(app),
            Worker(
                env.client,
                task_queue=config.temporal_task_queue,
                workflows=[TravelPlanningSessionWorkflow],
                activities=activities,
                max_concurrent_activities=config.worker_max_activities,
            ),
        ):
            rows = list(await asyncio.gather(*(run_case(ctx, case, gate) for case in cases)))
    await database.dispose()
    return sorted(rows, key=lambda r: r["order"])


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="JSONL output path")
    parser.add_argument("--cases", choices=("baseline", "all"), default="all")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--cold-cache",
        action="store_true",
        help="empty the search cache before each case (its follow-ups share it); runs cases sequentially",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="never read or store cached searches, including for follow-ups",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run every case this many times, sharing the cache (a demand spike)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)

    source = str(Path(cpfc_trip.__file__).resolve().parent)
    print(f"cpfc_trip loaded from {source}", flush=True)
    ledger = inventory.install()
    if hasattr(api_module, "LocalLimiter"):
        api_module.LocalLimiter.check = lambda self, *a, **k: None

    args.out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="eagles-benchmark-"))
    config = Settings().model_copy(
        update={
            "app_env": "test",
            "planner_mode": "live",
            "database_url": f"sqlite+aiosqlite:///{work}/benchmark.db",
            "frontend_origin": HARNESS_ORIGIN,
            "additional_frontend_origins": "",
            "static_export_dir": "",
            "temporal_task_queue": f"benchmark-{uuid4()}",
            "searchapi_enabled": True,
            "searchapi_api_key": SecretStr("benchmark"),
        }
    )
    started = monotonic()
    started_at = datetime.now(UTC).isoformat()
    try:
        rows = asyncio.run(
            run_cases(
                build_cases(args.cases, args.repeat),
                config,
                ledger,
                args.concurrency,
                args.cold_cache,
                args.no_cache,
            )
        )
    finally:
        inventory.uninstall()
        shutil.rmtree(work, ignore_errors=True)
    wall = round(monotonic() - started, 1)

    groups = {"all cases": summarize(rows)}
    if args.cases == "all":
        groups = {
            "baseline (18)": summarize([r for r in rows if r["variant"] == "baseline"]),
            **{
                variant: summarize([r for r in rows if r["variant"] == variant])
                for variant in VARIANTS
                if variant != "baseline"
            },
            "all cases": groups["all cases"],
        }
    meta = {
        "source": source,
        "workflow": getattr(TravelPlanningSessionWorkflow, "__temporal_workflow_definition").name,
        "started_at": started_at,
        "wall_seconds": wall,
        "intercepted": len(ledger.rows),
        "unsupported": len(ledger.unsupported),
        "cases": args.cases,
        "cache": "off" if args.no_cache else "cold per case" if args.cold_cache else "shared",
        "concurrency": 1 if args.cold_cache else args.concurrency,
        "latency_seconds": inventory.LATENCY,
        "inventory": inventory.archetypes(),
    }
    with args.out.open("w") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    stem = args.out.with_suffix("")
    report = markdown(groups, rows, meta)
    Path(f"{stem}.summary.md").write_text(report)
    Path(f"{stem}.summary.json").write_text(
        json.dumps(
            {"meta": meta, "summary": groups, "unsupported": ledger.unsupported},
            ensure_ascii=False,
            indent=2,
        )
    )
    print()
    print(report)
    print(f"Wrote {args.out}, {stem}.summary.md, {stem}.summary.json")

    if ledger.violations:
        print(
            "FAIL: network isolation violated:",
            *sorted(set(ledger.violations)),
            sep="\n  ",
            file=sys.stderr,
        )
        return 2
    outside = [r for r in ledger.rows if r["workflow"] == "(outside activity)"]
    if outside:
        print(
            f"FAIL: {len(outside)} SearchApi requests were made outside an Activity",
            file=sys.stderr,
        )
        return 2
    if ledger.unsupported:
        print(
            f"WARNING: {len(ledger.unsupported)} SearchApi requests fell outside the simulated "
            "inventory and were answered with a provider error:",
            *sorted({f"{u['stage']}: {u['reason']}" for u in ledger.unsupported}),
            sep="\n  ",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
