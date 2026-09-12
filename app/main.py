"""FastAPI application exposing flight search over the `fli` library."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.schemas import (
    AirportOut,
    DateSearchResponse,
    ErrorOut,
    SearchRequest,
    SearchResponse,
)
from app.service import (
    SearchError,
    search_cheapest_dates,
    search_flights,
    suggest_airports,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Flight Search",
    version=__version__,
    description="Flight search backed by the `fli` Google Flights library.",
)


@app.exception_handler(SearchError)
async def _search_error_handler(_request, exc: SearchError) -> JSONResponse:
    """Return upstream search failures as structured JSON, not a 500 traceback."""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorOut(error="search_failed", detail=exc.message, hint=exc.hint).model_dump(),
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "version": __version__}


@app.get("/api/airports", response_model=list[AirportOut])
def airports(
    q: str = Query(min_length=1, description="City, airport name, or IATA code"),
    limit: int = Query(default=8, ge=1, le=25),
) -> list[AirportOut]:
    """Autocomplete airports. Served from a bundled dataset — no network call."""
    return suggest_airports(q, limit=limit)


@app.post("/api/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    """Search flights for a route and date.

    Defined as a sync endpoint so FastAPI runs it in a worker thread: the
    underlying `fli` client is blocking, and each call constructs its own
    non-thread-safe `SearchFlights` instance.
    """
    return search_flights(request)


@app.get("/api/dates", response_model=DateSearchResponse)
def cheapest_dates(
    origin: str = Query(min_length=3, max_length=3),
    destination: str = Query(min_length=3, max_length=3),
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    cabin_class: str = Query(default="ECONOMY"),
    max_stops: str = Query(default="ANY"),
    currency: str = Query(default="USD", min_length=3, max_length=3),
    trip_duration: int | None = Query(default=None, ge=1, le=60),
) -> DateSearchResponse:
    """Find the cheapest departure dates in a range."""
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' date must not precede 'from' date")
    return search_cheapest_dates(
        origin,
        destination,
        from_date,
        to_date,
        cabin_class=cabin_class,
        max_stops=max_stops,
        currency=currency,
        trip_duration=trip_duration,
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the single-page search UI."""
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
