"""Executes a search plan in two phases: scan wide, then price the winners.

Phase 1 sweeps the calendar for every destination and trip length, building
a price grid. Phase 2 spends a full itinerary lookup only on the cheapest
cells. A failure in one scan degrades that route, not the search.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from datetime import timedelta

from app.deals.planner import SearchPlan, plan_search
from app.deals.schemas import (
    Deal,
    DealSearchRequest,
    DealSearchResult,
    DealSearchStats,
    GridCell,
)
from app.providers.base import FlightProvider, GridQuery, ItineraryQuery
from app.service import SearchError

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]


def _noop_progress(stage: str, done: int, total: int) -> None:
    """Default progress sink."""


def _grid_cells(plan: SearchPlan, request: DealSearchRequest, provider, progress):
    """Phase 1: sweep the calendar, one request per scan."""
    cells: list[GridCell] = []
    warnings: list[str] = []
    made = succeeded = failed = 0

    for index, scan in enumerate(plan.scans, start=1):
        query = GridQuery(
            origins=scan.origins,
            destinations=scan.destinations,
            depart_from=scan.depart_from,
            depart_to=scan.depart_to,
            trip_duration_days=scan.trip_duration_days,
            cabin_class=request.cabin_class,
            max_stops=request.max_stops,
            airlines=tuple(request.airlines),
            currency=request.currency,
            adults=request.adults,
        )
        label = "+".join(scan.destinations)
        made += 1
        try:
            prices = provider.scan_date_grid(query)
            succeeded += 1
        except SearchError as exc:
            failed += 1
            warnings.append(f"scan {label} ({scan.depart_from}..{scan.depart_to}) failed: {exc}")
            progress("scan", index, len(plan.scans))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad route must not end the search
            failed += 1
            logger.exception("grid scan failed for %s", label)
            warnings.append(f"scan {label} failed: {exc}")
            progress("scan", index, len(plan.scans))
            continue

        for price in prices:
            # A bundled scan cannot attribute a price to one airport.
            destination = scan.destinations[0] if len(scan.destinations) == 1 else label
            return_date = price.return_date
            if return_date is None and scan.trip_duration_days is not None:
                return_date = price.departure_date + timedelta(days=scan.trip_duration_days)
            # Belt and braces: the planner already narrowed the window, but a
            # provider may return dates outside what was asked for.
            if request.return_from and return_date and not (
                request.return_from <= return_date <= request.return_to
            ):
                continue
            cells.append(
                GridCell(
                    destination=destination,
                    origin=",".join(scan.origins),
                    departure_date=price.departure_date,
                    return_date=return_date,
                    trip_days=scan.trip_duration_days,
                    price=price.price,
                    currency=price.currency,
                )
            )
        progress("scan", index, len(plan.scans))

    return cells, warnings, made, succeeded, failed


def _best_per_cell(cells: list[GridCell]) -> list[GridCell]:
    """Keep the cheapest price per (destination, departure, return)."""
    best: dict[tuple, GridCell] = {}
    for cell in cells:
        key = (cell.destination, cell.departure_date, cell.return_date)
        if key not in best or cell.price < best[key].price:
            best[key] = cell
    return sorted(best.values(), key=lambda c: c.price)


def run_deal_search(
    request: DealSearchRequest,
    provider: FlightProvider,
    progress: ProgressCallback = _noop_progress,
) -> DealSearchResult:
    """Run a wide fare search and return ranked deals plus the price grid."""
    if not provider.supports_date_grid:
        raise SearchError(
            f"provider {provider.name!r} cannot scan date ranges, so a wide search "
            "would cost one request per date. Use a provider with a date grid, or "
            "search a single date with /api/search.",
            status_code=400,
        )

    started = time.perf_counter()
    plan = plan_search(request)
    logger.info(
        "deal search: %d scans + %d refinements (~%d requests)",
        plan.scan_requests,
        plan.refine_top_n,
        plan.estimated_requests,
    )

    cells, warnings, made, succeeded, failed = _grid_cells(plan, request, provider, progress)
    warnings = list(plan.notes) + warnings
    ranked = _best_per_cell(cells)

    prices = [c.price for c in ranked]
    median = statistics.median(prices) if prices else None
    currency = ranked[0].currency if ranked else request.currency

    cheapest_by_destination: dict[str, float] = {}
    for cell in ranked:
        if cell.destination not in cheapest_by_destination:
            cheapest_by_destination[cell.destination] = cell.price

    # Phase 2: full itineraries for the cheapest cells only.
    deals: list[Deal] = []
    to_refine = ranked[: plan.refine_top_n]
    for index, cell in enumerate(to_refine, start=1):
        deal = Deal(
            rank=index,
            origin=cell.origin,
            destination=cell.destination,
            departure_date=cell.departure_date,
            return_date=cell.return_date,
            trip_days=cell.trip_days,
            grid_price=cell.price,
            currency=cell.currency,
            savings_vs_median=round(median - cell.price, 2) if median else None,
        )
        if "," in cell.destination or "+" in cell.destination:
            deal.refine_note = "bundled scan: rerun without bundle_destinations to price this"
        else:
            made += 1
            try:
                itineraries = provider.search_itineraries(
                    ItineraryQuery(
                        origin=cell.origin.split(",")[0],
                        destination=cell.destination,
                        departure_date=cell.departure_date,
                        return_date=cell.return_date,
                        cabin_class=request.cabin_class,
                        max_stops=request.max_stops,
                        airlines=tuple(request.airlines),
                        currency=request.currency,
                        adults=request.adults,
                        limit=3,
                    )
                )
                deal.itinerary = itineraries[0] if itineraries else None
                if not itineraries:
                    deal.refine_note = "no itinerary returned for this date"
            except Exception as exc:  # noqa: BLE001 - keep the grid price we already have
                logger.warning("refine failed for %s: %s", cell.destination, exc)
                deal.refine_note = f"could not price in full: {exc}"
        deals.append(deal)
        progress("refine", index, len(to_refine))

    best = deals[0] if deals else None
    elapsed = time.perf_counter() - started

    return DealSearchResult(
        query=request,
        best=best,
        deals=deals,
        calendar=ranked,
        cheapest_by_destination=cheapest_by_destination,
        median_price=median,
        currency=currency,
        stats=DealSearchStats(
            requests_made=made,
            requests_estimated=plan.estimated_requests,
            grid_cells=len(ranked),
            scans_succeeded=succeeded,
            scans_failed=failed,
            elapsed_seconds=round(elapsed, 2),
        ),
        warnings=warnings,
        summary=_summarize(best, ranked, currency),
    )


def _summarize(best: Deal | None, cells: list[GridCell], currency: str | None) -> str:
    """A one-line answer an agent can read aloud."""
    if best is None:
        return "No fares found for those dates and filters."
    trip = f", {best.trip_days}-day trip" if best.trip_days else ""
    back = f" returning {best.return_date}" if best.return_date else ""
    spread = ""
    if len(cells) > 1:
        spread = f" Cheapest of {len(cells)} date/route combinations priced."
    return (
        f"Best fare {best.currency} {best.grid_price:,.0f} to {best.destination}, "
        f"departing {best.departure_date}{back}{trip}.{spread}"
    )
