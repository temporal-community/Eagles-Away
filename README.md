# Eagles Away

Plan a Crystal Palace away day from a UK city. Eagles Away looks for travel and a place to stay around selected Europa League matches, then puts the options in one easy-to-read plan. Built with Temporal.

**[Plan an away day at eaglesaway.com](https://eaglesaway.com/)**

## What to expect

1. Choose your starting city, match, dates and travel preferences.
2. See the journeys and stays we could verify, with prices where available.
3. Review the results in your browser, check what is still missing, then open the provider links to confirm details before booking. No account or contact details are needed.

Eagles Away does not make bookings or sell match tickets. Prices and availability can change, and some costs, such as local transfers or match tickets, may be missing. If we cannot verify a complete trip, any verified journey or stay remains visible, marked as an incomplete trip. The page explains what was missing.

## Run it locally

You will need [Python 3.13](.python-version), [uv](https://docs.astral.sh/uv/), [Node.js 22](.node-version), [Docker](https://www.docker.com/) and the [Temporal CLI](https://docs.temporal.io/cli).

From the repository root, set up the project and start PostgreSQL:

```bash
scripts/dev/setup.sh
scripts/dev/postgres-up.sh
```

Then start these four processes in separate terminals:

```bash
scripts/dev/temporal-server.sh
scripts/dev/api.sh
scripts/dev/worker.sh
scripts/dev/frontend.sh
```

Open [localhost:3000](http://localhost:3000). A fresh checkout uses recorded sample trips, so you can explore the app without paid travel searches.

For live searches, set `PLANNER_MODE=live` and `SEARCHAPI_API_KEY` in `.env`. Results are reused for `SEARCH_CACHE_TTL_MINUTES` (one day by default, so repeating a search returns in under a second); set it to `0` to make every search call SearchApi. If SearchApi fails, a cached result up to `SEARCH_CACHE_STALE_IF_ERROR_MINUTES` past that is shown instead, marked as from an earlier check. `SEARCH_CACHE_EARLY_BETA` refreshes popular or slow searches shortly before they expire (probabilistic early expiration); `0` turns that off. `uv run python scripts/measure-search-cache.py` times one paid cold search against cached reruns in a throwaway database.

To check a change, stop the frontend development server and run `scripts/check.sh`. For the PostgreSQL checks too, set `TEST_DATABASE_URL=postgresql+asyncpg://cpfc:cpfc@localhost:<port>/cpfc` to your local database (the port `scripts/dev/postgres-up.sh` reports). Never point tests at a shared or production database.

Port already in use, a pull request from a fork, or a deploy to watch? See [CONTRIBUTING.md](CONTRIBUTING.md).

## How it works

The website uses Next.js. A FastAPI service handles trip requests, Temporal runs the planning workflow, PostgreSQL stores session data, and SearchApi supplies dated travel and hotel results. Deterministic Python code filters, ranks and summarises the options; no language model is involved. Google Maps links open fresh local directions. The app shows where a price or route could not be confirmed.

For architecture, deployment and project history, see the [engineering handoff](planning/2026-09-30-engineering-handoff.md), [deployment record](planning/2026-10-06-deployment-readiness.md) and [development notes](blog.md).
