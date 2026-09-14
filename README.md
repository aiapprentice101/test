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

export ANTHROPIC_API_KEY=sk-ant-...   # or run `ant auth login`
uvicorn app.main:app --reload
```

### Choosing a model backend

```bash
# Claude (default)
export FLIGHT_AGENT_BACKEND=anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI / Codex
export FLIGHT_AGENT_BACKEND=openai        # "codex" is accepted too
export FLIGHT_AGENT_MODEL=gpt-5-codex     # set to a model your account has
export OPENAI_API_KEY=sk-...
```

`GET /api/agent/status` reports which backend is selected, which model, and —
if it cannot run — exactly what is missing. Add `?backend=openai` to ask about
one that is not currently selected. The UI shows this on the status line, so a
missing key is visible before you press Search rather than after.

Both backends get the **same tools and the same system prompt** (`agent/prompt.py`),
which is what makes comparing them meaningful.

Open http://127.0.0.1:8000 and ask in plain English. Interactive API docs are
at `/docs`.

Without an API key everything except the agent still works — the deal search,
the CLI, and the REST API need no LLM. The UI says so up front rather than
failing when you press Search.

### As an MCP server — for Codex, Claude Code, or any MCP client

```bash
pip install 'mcp[cli]'             # optional extra, not in requirements.txt
python -m app.mcp_server           # stdio
python -m app.mcp_server --http    # streamable HTTP on :8765
```

This is the other way to use Codex here: instead of Codex being the model
*inside* this app, this app becomes a tool server that Codex's own harness
calls. Any MCP-speaking client — Codex, Claude Desktop, Claude Code — can
drive the flight search with no code change.

Claude Desktop config:

```json
{"mcpServers": {"flights": {"command": "/path/to/.venv/bin/python",
                            "args": ["-m", "app.mcp_server"],
                            "cwd": "/path/to/this/repo"}}}
```

The MCP tools and the in-app agent's tools are the same functions with the
same descriptions — `@beta_tool` keeps the original on `.func`, so there is one
implementation, not two that drift.

> **Network requirement:** the app queries `www.google.com` on every search. It
> needs unrestricted outbound HTTPS to that host — see [Known limitations](#known-limitations).

Ask it a question the way you'd ask a person:

> *Singapore Airlines, Singapore to the USA, round trip. Leave December 2026,
> back January 2027, 30-day trip, business class. Best rate and which dates?*

Claude parses that, resolves "Singapore" to SIN and "the USA" to twelve
gateways, costs the search, runs it, and reports the answer — while the UI
shows each step and a live progress bar.

Then you refine it: *"same thing but non-stop only"*, *"what about January
instead?"*. It is a conversation, not a one-shot search box — earlier turns
stay on the page with their own results, and repeated scans come back from a
local cache instead of the network.

## Three layers

1. **Agent** — natural language in, tool calls out. Five tools, streaming
   work to the browser over SSE. Runs on Claude or on OpenAI/Codex models;
   pick with one environment variable.
2. **Deal search** — a wide search across many dates and destinations at once.
   The part that makes this cheap enough to be worth doing.
3. **Point search** — one route, one date, like any flight site.

The same tools are also exposed as an **MCP server**, so Claude Desktop or
Claude Code can drive the search directly instead of through this UI.

## API

| Method | Path                  | Purpose                                                |
|--------|-----------------------|--------------------------------------------------------|
| `POST` | `/api/ask`            | **Natural language in, SSE stream of the agent's work** |
| `GET`  | `/api/agent/status`   | Whether the agent has credentials                      |
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

### Price cache

Grid scans are cached in SQLite (`.flight-cache.sqlite3`), keyed by every
field that affects the price — route, dates, trip length, cabin, airlines,
stops, currency, passengers. A repeat or refined search reuses them:

```
⚡ 10 of 17 scans served from cache
```

**Refined itineraries are never cached.** The distinction is deliberate: a
grid scan answers "which dates are worth looking at", where a few hours of
staleness changes nothing, but a refined itinerary is the fare you would
actually book and must be live.

```bash
FLIGHT_CACHE=off              # bypass entirely
FLIGHT_CACHE_TTL=21600        # seconds, default 6h
FLIGHT_CACHE_PATH=/some/file.sqlite3
```

Failed scans are not cached, so a transient network error retries next time
rather than being remembered as "no flights".

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
  mcp_server.py      the same tools over MCP
  agent/
    tools.py         the 5 tools (docstrings ARE the descriptions models read)
    registry.py      provider-neutral view of those tools
    prompt.py        the system prompt, shared by every backend
    runner.py        threading + event streaming, backend-agnostic
    context.py       lets a running tool emit progress to its caller
    backends/
      anthropic_backend.py  Claude, via the SDK's tool runner
      openai_backend.py     OpenAI / Codex, hand-written tool loop
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
tests/               104 tests, all offline
```

### How the agent is wired

`app/agent/tools.py` holds five tools: `find_airports`,
`list_destination_regions`, `estimate_search_cost`, `find_best_fares`, and
`search_one_date`. Their **docstrings are the tool descriptions Claude reads**,
so the usage rules live there — cost the search before running it, how trip
lengths map to arguments, when to use a region instead of airport codes.

The tools are declared once, with Anthropic's `@beta_tool`, which also keeps
the undecorated function on `.func`. `registry.py` turns that into a
provider-neutral form, so **one declaration feeds three consumers**: Claude's
native tool use, OpenAI's function calling, and the MCP server. There is no
second tool list to drift out of sync — a test asserts the schemas stay
identical across them.

Two further decisions worth knowing:

* **Tools return compact text; full results bypass the model.** A wide search
  produces hundreds of grid rows. Feeding those back through Claude would be
  slow and pointless, so the tool returns a short summary and the full payload
  goes straight to the browser as a `results` event.
* **Tools take comma-separated strings, not arrays.** Flat scalar arguments
  avoid JSON-schema array edge cases in tool calls.

The loop runs on a worker thread and pushes events through a queue, so the
browser sees `thinking`, `tool_call`, `progress`, `text`, and `results` as they
happen rather than waiting minutes for a single response.

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
request budgeting, the engine's two-phase flow — including what happens when
individual routes fail — and the agent layer, with the Anthropic client stubbed.

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
* **No price history yet.** Scans are cached (see below) but not retained as a
  time series, so the app cannot yet answer "is this a good fare *for this
  route*?". The cache table is the natural place to grow that.
* Jobs are in-memory and single-process; they are lost on restart. The seam to
  swap in Redis is `deals/jobs.py:JobStore`.
* Conversation history is **text only** — the assistant's summary, not the
  tool calls behind it. So a follow-up sees what was *said*, not the full grid
  from the previous turn. That keeps the payload provider-neutral, at the cost
  of the agent occasionally re-running a scan it could have reasoned from.
  The cache absorbs most of that cost.
* History lives in the browser tab; a refresh starts a new conversation.
* The OpenAI backend uses Chat Completions function calling and has been
  tested only against a stubbed client — never a live endpoint. The default
  model id (`gpt-5-codex`) is a starting guess: set `FLIGHT_AGENT_MODEL` to a
  model your account actually has. A rejected model surfaces as a clear
  error naming that variable.
* The OpenAI backend emits no `thinking` events — Chat Completions does not
  expose reasoning — so the activity log is a little quieter than on Claude.
* Agent runs cost Anthropic API tokens on top of whatever the flight provider
  costs.
* Region lists are hand-curated long-haul gateways, not exhaustive. A scan costs
  one request per destination, so the defaults trade coverage for cost — edit
  `deals/regions.py` to taste.
