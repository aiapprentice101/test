# Flight Search

A flight search web app built on [`fli`](https://github.com/punitarani/fli) — a Python
library that talks to Google Flights' internal `FlightsFrontendService` API directly
(no scraping, no browser automation).

FastAPI backend + a dependency-free single-page frontend: airport autocomplete,
one-way and round-trip search, cabin/stops/time/airline filters, and per-leg
itinerary details with layovers and CO₂ figures.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Interactive API docs are at `/docs`.

> **Network requirement:** the app queries `www.google.com` on every search. It
> needs unrestricted outbound HTTPS to that host — see [Known limitations](#known-limitations).

## Two things live here

1. **Point search** — a web UI and API for one route on one date, like any
   flight site.
2. **Deal search** — a wide, agent-facing search across many dates and many
   destinations at once: *"Singapore Airlines, SIN to the US, leave December,
   back in January, 30-day trip, business — find me the best fare and the
   dates."* This is the interesting part.

## API

| Method | Path               | Purpose                                                   |
|--------|--------------------|-----------------------------------------------------------|
| `POST` | `/api/deals`       | Start a wide fare search; returns a job to poll           |
| `GET`  | `/api/deals/{id}`  | Poll progress, then results                               |
| `POST` | `/api/deals/plan`  | Cost a wide search **without running it**                 |
| `GET`  | `/api/regions`     | Region names accepted by `destination_region`             |
| `POST` | `/api/search`      | Search flights for one route and date                     |
| `GET`  | `/api/dates`       | Cheapest departure dates across a range                   |
| `GET`  | `/api/airports`    | Airport autocomplete (bundled dataset, no network call)   |
| `GET`  | `/api/health`      | Liveness probe                                             |

## Deal search

```bash
python scripts/find_deals.py \
    --from SIN --to-region US --airline SQ --cabin BUSINESS \
    --depart-between 2026-12-01 2026-12-31 \
    --return-between 2027-01-01 2027-01-31 \
    --trip-days 30
```

Add `--dry-run` to see the request budget without touching the network:

```
Plan: 12 calendar scans + 10 full lookups = ~22 requests
Destinations: JFK, EWR, LAX, SFO, SEA, ORD, IAD, BOS, IAH, ATL, DFW, MIA
Trip lengths: 30 days
Departure window: 2026-12-02 .. 2026-12-31
  note: departure window narrowed to 2026-12-02 .. 2026-12-31 so the return
        falls inside the window you asked for
```

### Why it is 22 requests and not 360

That search covers 30 departure dates across 12 US gateways — 360 date/route
combinations. Pricing each one directly would be 360 requests: slow, and
exactly the traffic pattern that gets an IP blocked.

It runs in two phases instead:

1. **Scan.** Google's calendar graph returns a price for *every date in a
   range* in a single request. Twelve requests — one per gateway — price all
   360 combinations.
2. **Refine.** Only the ten cheapest cells get a full itinerary lookup, for
   flight numbers, times and layovers.

**22 requests instead of 360, a 16x reduction.** The same arithmetic governs
your bill on a paid provider, so the design matters whichever data source you
end up on.

The planner also throws away work before it costs anything. A 30-day trip
departing 1 December returns on 31 December — which is not "back in January",
so 1 December is dropped and the real window is 2–31 December. Constraint
reconciliation like this is pure logic, runs offline, and is covered by tests.

### Agent-facing contract

`POST /api/deals` takes a flat schema — every field a scalar or a list of
scalars, because nested objects make LLM tool calls error-prone. The agent
does the natural-language parsing and fills it in:

```json
{
  "origins": ["SIN"],
  "destination_region": "US",
  "depart_from": "2026-12-01", "depart_to": "2026-12-31",
  "return_from": "2027-01-01", "return_to": "2027-01-31",
  "min_trip_days": 30, "max_trip_days": 30,
  "cabin_class": "BUSINESS",
  "airlines": ["SQ"],
  "refine_top_n": 10,
  "max_requests": 60
}
```

It returns `202` with a job id — a wide search takes minutes, far too long to
hold an HTTP request open. Poll `GET /api/deals/{id}` for live progress, then
the result: the best fare, ranked runners-up, the cheapest price per
destination, and the full price calendar. `max_requests` is a hard ceiling;
the planner trims destinations to fit and says so in `notes`.

Call `POST /api/deals/plan` first to see the cost before spending anything.

```bash
curl -X POST localhost:8000/api/search -H 'Content-Type: application/json' -d '{
  "origin": "JFK",
  "destination": "LHR",
  "departure_date": "2026-10-15",
  "return_date": "2026-10-22",
  "cabin_class": "BUSINESS",
  "max_stops": "NON_STOP",
  "sort_by": "CHEAPEST",
  "adults": 2
}'
```

Search accepts `origin`, `destination`, `departure_date`, `return_date`, passenger
counts (`adults`, `children`, `infants_in_seat`, `infants_on_lap`), `cabin_class`,
`max_stops`, `sort_by`, `departure_window` (`"6-20"`), `airlines`,
`exclude_airlines`, `currency`, and `limit`.

## Layout

```
app/
  main.py            FastAPI routes
  schemas.py         point-search contract (independent of fli's models)
  service.py         the ONLY module that imports fli
  providers/
    base.py          the provider protocol: scan_date_grid + search_itineraries
    fli_provider.py  fli behind that protocol, rate-limited
  deals/
    schemas.py       flat, agent-friendly wide-search contract
    regions.py       region -> gateway airports ("US" -> JFK, EWR, LAX, ...)
    planner.py       pure: date reconciliation, chunking, request budget
    engine.py        two-phase execution, resilient to per-route failure
    jobs.py          background job store with progress
  static/            single-page UI (no build step, no framework)
scripts/
  find_deals.py      run a wide search from the terminal
tests/               79 tests, all offline
```

Two deliberate seams. `service.py` is the only module that imports `fli`, so an
upstream shape change lands in one file. `providers/base.py` is the swap point
for the data source itself: a provider supplies a date grid and an itinerary
lookup, and the engine neither knows nor cares which one it is. Swapping `fli`
for Amadeus is a new module in `providers/`, not a rewrite.

A provider that cannot scan date ranges must set `supports_date_grid = False`;
the engine then refuses wide searches outright rather than quietly turning one
query into hundreds of requests.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite stubs `fli`'s network client, so it runs offline and deterministically.
It covers filter construction, result normalization (including the round-trip
pricing rule below), validation, error mapping, the planner's date arithmetic and
request budgeting, and the engine's two-phase flow — including what happens when
individual routes fail.

## Notes on `fli`

Findings from wiring it up, worth knowing before committing to it:

* **Round-trip pricing lives on the outbound segment.** A round-trip search returns
  a tuple of `FlightResult`s; the full fare is on element 0 and the return leg's
  `price` is typically `None`. Summing the two double-counts or under-reports.
  `service.py` prices from the outbound.
* **PyPI lags the repo.** `flights` 0.9.0 is the released version; the git HEAD
  (0.10.0) adds `fli.core.links`. This app pins the released version and builds its
  own Google Flights deep links, so it runs on a plain `pip install`.
* **Stop-filter names differ from the README.** The real enum members are
  `ONE_STOP_OR_FEWER` and `TWO_OR_FEWER_STOPS`, not the `ONE_STOP` / `TWO_PLUS_STOPS`
  the README's table shows.
* **Unknown codes raise `fli.core.parsers.ParseError`.** Unhandled, that surfaces as
  a 500; the service maps it to a 422.
* **`SearchFlights` is not thread-safe** (it caches a shopping-session id between
  calls). The app constructs a fresh instance per request.
* **Date searches over 61 days silently drop filters.** `fli` splits longer ranges
  into chunks, and the chunk builder copies `airlines` but not `airlines_exclude`,
  `alliances` or `alliances_exclude` — so those are dropped without warning on
  long ranges. Worth reporting upstream. The planner sidesteps it by keeping each
  scan inside one chunk where it can.
* **`fli` defaults to 10 requests/second**, which looks nothing like human traffic.
  `app/providers/fli_provider.py` turns that down to 1; override with
  `FLI_CALLS_PER_SECOND`.
* **Installing the CLI needs `click`.** `pip install flights` pulls `typer` but the
  `fli` CLI entry point imports `click` directly, which may not get installed. The
  library itself is unaffected — this app never imports `fli.cli`.
* **Prices are unofficial.** `fli` reverse-engineers a private endpoint. It can break
  when Google changes it, applies its own rate limiting, and returns no bookable
  fare — results link out to Google Flights to book.

## Known limitations

* **Sandboxed/restricted networks can't run live searches.** In an environment that
  blocks `www.google.com`, every search fails at the network layer. The app reports
  this as a 502 with an explanatory hint rather than a stack trace. This app's live
  search path was therefore never exercised against Google from the environment it
  was written in — everything else was verified end to end against stubbed
  responses, including the browser UI.
* No booking. Results deep-link to Google Flights.
* Multi-city search isn't exposed yet; `fli` supports it (`build_multi_city_segments`).
* **No caching, and no price history.** Every search hits the provider fresh. For
  a real product the coarse grid should be refreshed on a schedule and stored,
  so user queries read an index instead of scanning live — that collapses
  per-query cost, makes agent responses fast, and accumulates the price history
  that actually answers "is this a good fare?"
* Jobs are in-memory and single-process; they are lost on restart. The seam to
  swap in Redis is `deals/jobs.py:JobStore`.
* Region lists are hand-curated long-haul gateways, not exhaustive. A scan costs
  one request per destination, so the defaults trade coverage for cost — edit
  `deals/regions.py` to taste.
