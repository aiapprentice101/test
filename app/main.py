"""FastAPI application exposing flight search over the `fli` library."""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.agent.runner import AgentUnavailable, describe_backend, stream_answer
from app.deals.jobs import Job, store
from app.deals.planner import PlanError, plan_search
from app.deals.regions import UnknownRegionError, known_regions
from app.deals.schemas import DealSearchRequest
from app.providers.fli_provider import FliProvider, configure_rate_limit
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

# `fli` defaults to 10 req/sec against a private Google endpoint. Slow it
# down before the first search; see app/providers/fli_provider.py.
configure_rate_limit(int(os.environ.get("FLI_CALLS_PER_SECOND", "1")))

provider = FliProvider()

app = FastAPI(
    title="Flight Search",
    version=__version__,
    description="Flight search backed by the `fli` Google Flights library.",
)


@app.exception_handler(PlanError)
async def _plan_error_handler(_request, exc: PlanError) -> JSONResponse:
    """An unsatisfiable search is the caller's to fix, so say exactly why."""
    return JSONResponse(
        status_code=422,
        content=ErrorOut(error="invalid_plan", detail=str(exc)).model_dump(),
    )


@app.exception_handler(UnknownRegionError)
async def _region_error_handler(_request, exc: UnknownRegionError) -> JSONResponse:
    """Unknown region names list the valid ones so an agent can retry."""
    return JSONResponse(
        status_code=422,
        content=ErrorOut(error="unknown_region", detail=str(exc)).model_dump(),
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


class AskRequest(BaseModel):
    """A natural-language question for the agent."""

    question: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)


@app.post("/api/ask")
def ask(request: AskRequest) -> StreamingResponse:
    """Answer a plain-English flight question, streaming the agent's work.

    Server-sent events: `thinking`, `text`, `tool_call`, `progress`, `plan`,
    `results`, `error`, `done`. Streamed because a wide search runs for
    minutes and the user should see it happening.
    """

    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, default=str)}\n\n"

    def events():
        # `stream_answer` is a generator, so credential errors surface on the
        # first iteration rather than at the call — the loop must be inside
        # the guard, not just the call.
        try:
            for event in stream_answer(request.question, request.history):
                yield sse(event)
        except AgentUnavailable as exc:
            yield sse({"type": "error", "message": str(exc)})
            yield sse({"type": "done"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/status")
def agent_status(backend: str | None = Query(default=None)) -> dict:
    """Whether the agent can run, and on which backend and model.

    `backend` overrides `FLIGHT_AGENT_BACKEND` for the check, so the UI can
    ask about one that is not currently selected.
    """
    return describe_backend(backend)


@app.get("/api/regions", response_model=list[str])
def regions() -> list[str]:
    """Region names accepted by `destination_region`."""
    return known_regions()


@app.post("/api/deals/plan", response_model=dict)
def plan_deals(request: DealSearchRequest) -> dict:
    """Cost a wide search without running it.

    Lets an agent (or a person) see the request budget and the narrowed date
    window before spending anything upstream.
    """
    plan = plan_search(request)
    return {
        "estimated_requests": plan.estimated_requests,
        "scan_requests": plan.scan_requests,
        "refine_requests": plan.refine_top_n,
        "destinations": list(plan.destinations),
        "trip_lengths": list(plan.durations),
        "departure_windows": [
            {"from": s.depart_from.isoformat(), "to": s.depart_to.isoformat()}
            for s in plan.scans[: len(plan.durations)]
        ],
        "notes": list(plan.notes),
    }


@app.post("/api/deals", response_model=Job, status_code=202)
def start_deal_search(request: DealSearchRequest) -> Job:
    """Start a wide fare search. Returns a job to poll — searches take minutes."""
    return store.submit(request, provider)


@app.get("/api/deals/{job_id}", response_model=Job)
def get_deal_search(job_id: str) -> Job:
    """Poll a wide search: progress while running, full results when complete."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return job


@app.get("/api/deals", response_model=list[Job])
def list_deal_searches(limit: int = Query(default=20, ge=1, le=100)) -> list[Job]:
    """Recent wide searches, newest first."""
    return store.list(limit=limit)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the single-page search UI."""
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
