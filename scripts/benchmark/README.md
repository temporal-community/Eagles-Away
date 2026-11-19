# Recovery benchmark (offline, simulated inventory)

A deterministic end-to-end benchmark for trip-search completion, perceived performance and
dead-end recovery offers. It drives the real FastAPI app, a local Temporal dev server, the
real `TravelPlanningSessionWorkflow` and the real `SearchActivities` (fresh sqlite search
cache per run). **SearchApi is never called.** `inventory.py` answers every SearchApi request
from a **SIMULATED** inventory, and blocks every real HTTP transport.

## Run

```sh
# Current tree ("after")
uv run --locked python scripts/benchmark/recovery_benchmark.py --out .data/benchmark/after.jsonl --cases all

# Frozen older source ("before"): put the snapshot first on PYTHONPATH
PYTHONPATH=/tmp/eagles-baseline/src uv run --locked python scripts/benchmark/recovery_benchmark.py \
  --out .data/benchmark/before.jsonl --cases all
```

Options: `--cases baseline|all` (default `all`), `--concurrency N` (default 2 cases at a time),
`--cold-cache` (empty the search cache before each case; see [Search cache](#search-cache)),
`--no-cache` (never read or store cached searches, so recovery follow-ups search again too).
The first line printed is the `cpfc_trip` package path, so you can confirm which source ran.
It needs the `temporal` CLI on `PATH` (or at `/opt/homebrew/bin/temporal`). A full run takes about 5–10 minutes.

Outputs, next to `--out`:

- `X.jsonl`: one row per case, including follow-up runs nested under `actions[].follow_up`.
- `X.summary.md`: the summary table and a per-case table.
- `X.summary.json`: the summary, run metadata and any unsupported request shapes.

Exit status: `2` if any request tried to reach the real network, or if a SearchApi request
happened outside a Temporal Activity. Requests outside the simulated inventory get a
provider error and a warning. They do not fail the run.

### Compare two runs

```sh
uv run --locked python scripts/benchmark/compare.py \
  --before .data/benchmark/before.jsonl --after .data/benchmark/after.jsonl \
  --out .data/benchmark/compare-shared.md
```

It reads each `X.jsonl` and its `X.summary.json`, and writes the following:

- A metric table, before and after, for all cases and for the 18 baseline cases.
- A per-case table: outcome, `t_decision`, `t_to_trip`, requests (initial + follow-ups) and offers,
  each as `before → after`.
- Cache flags for the after run: a complete outcome or a completing follow-up that made no SearchApi request.

## Cases

- **baseline** (18): the 9 live origins × {Białystok, Salzburg}, 1 adult, Value, default dates.
- **private room+bath** (4): London and Manchester × both fixtures.
- **wider dates** (2): Belfast × both fixtures (`flexibility: two_days`).

For each case, the harness does the following:

1. It POSTs `/api/sessions` and polls the snapshot every 0.25 s.
2. It follows every offered action once with the follow-up brief from the recovery contract:
   - `depart_from` adds `departure_city`.
   - `wider_dates` sets `flexibility=two_days` and `windows=[]`.
   - `without_private_bathroom` sets `private_bathroom=false`.
3. For the old code, it treats `suggested_origin_city` as a single `depart_from` action.

The in-process `LocalLimiter` is disabled. It allows 90 polls per minute per session, and this
harness polls faster than that.

## Metrics

| field | meaning |
|---|---|
| `t_first_option` | seconds from POST to the first snapshot showing a journey, stay or itinerary |
| `t_pair` | seconds to an itinerary, or to a preview with both journey and stay |
| `t_decision` | seconds to phase `closed`/`failed` |
| `outcome` | `complete` (itinerary), `partial` (preview journey or stay), `empty` |
| `requests` | SearchApi requests made by the session's activities (cache hits make none) |
| `dead_ends` | offered actions whose follow-up did not complete |
| `t_to_trip` | complete: `t_pair`; recovered: `t_decision` + the fastest completing follow-up's `t_pair` |
| `next_step_s` | `t_decision` when the run completes or offers at least one action that completes |

The summary reports the following:

- Completion rate, both initially and after one recovery tap. The second counts a case if any offered action completes.
- The number of dead-end offers.
- The number of runs that ended without completing and without offering an action.
- The median and p90 of each timing.
- Total SearchApi requests.

Results are grouped by variant.

## Search cache

By default the search cache is shared within a run, as in production. Cases start in a fixed
order, but each code version searches different routes, so a later case can reuse an earlier
case's searches differently in before and after runs. For example, the new code's recovery
probe from Liverpool searches Manchester, so the Manchester case afterwards makes no requests.
Timings vary by a few hundred milliseconds between runs because of concurrency.

`--cold-cache` deletes every search cache row before each case and runs cases one at a time.
A case's follow-up runs still share that case's cache, as they would for a user who taps an
action. Use it to measure each case on its own.

## Simulated SearchApi

Fixed latency per request is as follows:

- `google_flights`: 1.2 s
- `google` (trains): 1.0 s
- `booking`: 2.0 s
- `booking_property`: 1.5 s

Payload shapes copy `tests/data/*.json` and pass the production normalizers.

The follow-up tokens are stateless. Each is base64 JSON of the chosen itinerary:

- `departure_token` encodes the outbound itinerary and the dates.
- `booking_token` also encodes the return itinerary.

The four request types behave as follows:

- **Discovery** (`google_flights`):
  - `stops=nonstop` lists only one-flight itineraries. Any other `stops` value lists both nonstop and one-stop itineraries.
  - The first item goes in `best_flights` and the rest in `other_flights`.
  - If nothing matches, the response is `{"error": "Google Flights didn't return any results."}`.
  - One-stop listings include `layovers[]`.
- **Return selection** (`departure_token`): lists the return itineraries, each with a `booking_token`.
- **Booking** (`booking_token`):
  - `selected_flights` holds two itineraries, plus `booking_options`.
  - `search_parameters` echoes the request.
  - `request_url` is a `google.com/travel/flights` URL.
- **Trains, stays and other routes:**
  - Trains (`google`): direct, unpriced PKP Intercity trains between Warsaw Central and Zielone Wzgórza, all 2 h 25 min, every day. Warsaw→Białystok trains leave at 06:30, 08:30 … 20:30, and Białystok→Warsaw trains at 05:30 … 19:30. Each train links to the operator website `https://www.intercity.pl/`. Any other train query returns no `train_results`.
  - Stays (`booking` / `booking_property`): four properties per city for every date pair. The listing has a `Private bathroom` filter option, and the filtered listing returns the same properties. Białystok room names contain "Private Bathroom", except one shared-bathroom single room. Salzburg rooms are named "Double Room" with the amenity `"En-suite bathroom"`. They never say "private bathroom", so literal private-bathroom matching fails, but an en-suite synonym would pass.
  - Any other route, engine, airport set or query is unsupported. It is counted and answered with a provider error.

## Inventory model (SIMULATED, not real flights)

The schedules are invented to reproduce the failure modes of the 17 Nov 2026 live study. Times
are local at each airport. Białystok is reached via WAW, followed by a Warsaw→Białystok train.

| origin | Białystok (WAW) | Salzburg (SZG) |
|---|---|---|
| London | works: nonstop arrives 09:30 or 14:55 | works: nonstop arrives 12:00 the day before |
| Manchester | **first-candidate trap**: no nonstops; the earliest and first-listed one-stop (via AMS) has a 45-min layover; the 14:25 arrival via FRA fits | works |
| Birmingham | **return-selection trap**: the latest-departing return (22:40) lands at BHX 00:10 the next day, after the 23:59 return window; the 15:00 return fits | works |
| Liverpool | genuinely late: every arrival is after 17:30 | provider empty |
| Edinburgh | genuinely late | works (one-stop via AMS) |
| Newcastle | genuinely late | genuinely late: lands 23:25 the day before; 17:15 on match day |
| Bristol | genuinely late | provider empty |
| Glasgow | works (one-stop via AMS, arrives 12:50) | genuinely late: lands 23:20 the day before; 16:50 on match day |
| Belfast | **wider dates help**: explicit empty for 9 and 10 Dec departures; valid nonstop on 8 Dec | **wider dates help**: on 27 and 28 Jan, candidates list but the booking token returns only the outbound (unverifiable); 26 Jan verifies |

Match-day departures never fit Białystok: every 10 Dec WAW arrival is at 09:30 or later.
Salzburg match-day arrivals are after 16:00.

### Feasibility rules the inventory relies on

These come from the old code. A screening selector should apply the same rules to discovery
and return listings to avoid the traps.

**Białystok** (kickoff 2026-12-10 18:45 Europe/Warsaw):

- The outbound flight arrives at WAW at time T. A Warsaw→Białystok train must leave between T+3 h and T+8 h on the same date (`connect_onward_rail`, `minimum_transfer_minutes=180`).
- The train must reach Białystok by 13:45 on match day (kickoff − 5 h, `MATCH_TIME_BUFFER`). Its arrival hour must fall in [6, 23).
- On the day before the match, this means T ≤ 17:30, because the last train leaves at 20:30. On match day, T ≤ 07:30.
- On the way back, a Białystok→Warsaw train must arrive between the WAW flight departure − 8 h and − 3 h, on the same date.
- That train leaves Białystok at or after 23:45 on match day (kickoff + 5 h). Its departure hour must fall in [6, 23), so the 05:30 train never qualifies.
- The return flight must land in the UK by `window.latest_return`: 23:59 Europe/London on the last window day (the 11th by default, the 12th with `two_days`). This is the Birmingham trap.

**Salzburg** (kickoff 2027-01-28 21:00 Europe/Vienna):

- The SZG arrival must be no later than 16:00 local on match day, with the arrival hour in [6, 23). A 23:20 arrival the day before fails.
- The SZG departure must be at or after 02:00 local on the 29th, with the hour in [6, 23).
- The UK arrival must be no later than 23:59 London on the window's last day.

**Both destinations:**

- A one-stop layover must be 60–480 min.
- The listing's `layovers[0].duration` equals the normalizer's computed layover.
- Booking-token responses must have exactly two `selected_flights`. Belfast→Salzburg on 27 and 28 Jan does not.
- Old discovery follows only `choices[:1]`:
  - For WAW, discovery choices are sorted by earliest final arrival, and the return list by latest first departure.
  - For SZG, both use provider order.
  - It tries nonstop first, then `one_stop_or_fewer`.
