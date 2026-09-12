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

## API

| Method | Path            | Purpose                                                  |
|--------|-----------------|----------------------------------------------------------|
| `POST` | `/api/search`   | Search flights for a route and date                      |
| `GET`  | `/api/dates`    | Cheapest departure dates across a range                  |
| `GET`  | `/api/airports` | Airport autocomplete (bundled dataset, no network call)  |
| `GET`  | `/api/health`   | Liveness probe                                            |

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
  main.py      FastAPI routes
  schemas.py   request/response contract (independent of fli's models)
  service.py   the ONLY module that imports fli
  static/      single-page UI (no build step, no framework)
tests/         37 tests, all offline
```

`service.py` is a deliberate seam. `fli` mirrors an unofficial API whose shapes can
change without notice, so every translation between `fli`'s models and this app's
own schemas lives in one file. Nothing else in the app imports `fli`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite stubs `fli`'s network client, so it runs offline and deterministically. It
covers filter construction, result normalization (including the round-trip pricing
rule below), validation, and error mapping.

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
* No caching or rate limiting of its own — `fli` rate-limits internally, but a
  public deployment should add its own.
