"""Turns a wide search request into a concrete, budgeted request plan.

This is where a "massive search" stops being massive. The naive reading of
"December departures, 12 US gateways" is 360 full itinerary lookups. Because
one calendar-graph request prices every date in a range, the same search is
12 requests plus a handful of refinements.

Pure functions only — no network, no provider. That keeps the expensive
decisions (how many requests, which dates are even possible) testable
offline and identical across providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.deals.regions import expand_region
from app.deals.schemas import DealSearchRequest

# Google's calendar graph caps a single request at 61 days; `fli` splits
# longer ranges into parallel chunks, so a chunk is a request.
MAX_DAYS_PER_SCAN = 61

# Guard against a request whose windows imply a huge spread of trip lengths:
# every distinct duration multiplies the scan count.
MAX_DURATIONS = 8


class PlanError(ValueError):
    """The request cannot be satisfied as asked."""


@dataclass(frozen=True)
class GridScan:
    """One coarse price scan — one upstream request."""

    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    depart_from: date
    depart_to: date
    trip_duration_days: int | None


@dataclass(frozen=True)
class SearchPlan:
    """Everything the engine will do, priced in requests before it starts."""

    scans: tuple[GridScan, ...]
    refine_top_n: int
    estimated_requests: int
    destinations: tuple[str, ...]
    durations: tuple[int | None, ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def scan_requests(self) -> int:
        """Requests spent on the coarse phase."""
        return len(self.scans)


def resolve_destinations(request: DealSearchRequest) -> list[str]:
    """Explicit destinations plus any region expansion, de-duplicated."""
    codes = list(request.destinations)
    if request.destination_region:
        codes.extend(expand_region(request.destination_region))
    seen: set[str] = set()
    ordered = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered


def resolve_durations(request: DealSearchRequest) -> list[int | None]:
    """Work out which trip lengths to scan.

    An explicit min/max wins. Otherwise the return window implies a range,
    which is derived here rather than making the caller compute it — but a
    range wider than `MAX_DURATIONS` is rejected, because each extra day is
    another full set of scans.
    """
    if not request.is_round_trip:
        return [None]

    if request.min_trip_days or request.max_trip_days:
        low = request.min_trip_days or request.max_trip_days
        high = request.max_trip_days or request.min_trip_days
    elif request.return_from and request.return_to:
        low = (request.return_from - request.depart_to).days
        high = (request.return_to - request.depart_from).days
    else:
        raise PlanError("a round trip needs min/max trip days or a return window")

    low = max(1, low)
    if high < low:
        raise PlanError(
            "the departure and return windows do not overlap for any trip length; "
            "widen one of them or set min_trip_days / max_trip_days"
        )
    span = high - low + 1
    if span > MAX_DURATIONS:
        raise PlanError(
            f"the windows allow {span} different trip lengths ({low}-{high} days), "
            f"more than the {MAX_DURATIONS} this will scan. Set min_trip_days and "
            "max_trip_days to narrow it."
        )
    return list(range(low, high + 1))


def departure_window(
    request: DealSearchRequest, duration: int | None
) -> tuple[date, date] | None:
    """The departure dates that satisfy *both* windows for this trip length.

    A 30-day trip departing 1 December returns on the 31st — which fails a
    "back in January" requirement. Intersecting the windows here removes
    those dates before they cost a request.
    """
    low, high = request.depart_from, request.depart_to
    if duration is not None and request.return_from and request.return_to:
        low = max(low, request.return_from - timedelta(days=duration))
        high = min(high, request.return_to - timedelta(days=duration))
    return (low, high) if low <= high else None


def _chunk(start: date, end: date) -> list[tuple[date, date]]:
    """Split a date range into pieces a single request can cover."""
    chunks = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=MAX_DAYS_PER_SCAN - 1), end)
        chunks.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return chunks


def plan_search(request: DealSearchRequest) -> SearchPlan:
    """Build the request plan, trimming it to fit `max_requests`."""
    destinations = resolve_destinations(request)
    if not destinations:
        raise PlanError("no destinations to search")

    durations = resolve_durations(request)
    notes: list[str] = []

    windows: list[tuple[int | None, date, date]] = []
    for duration in durations:
        window = departure_window(request, duration)
        if window is None:
            notes.append(
                f"no departure date allows a {duration}-day trip within the return window"
            )
            continue
        windows.append((duration, window[0], window[1]))

    if not windows:
        raise PlanError(
            "no departure date satisfies both the departure and return windows. "
            "Check the trip length against the dates you gave."
        )

    if any(w[1] != request.depart_from or w[2] != request.depart_to for w in windows):
        first = windows[0]
        notes.append(
            f"departure window narrowed to {first[1]} .. {first[2]} so the return "
            "falls inside the window you asked for"
        )

    # Fit the destination list to the budget before building scans: each
    # destination costs one request per duration per date-chunk.
    chunks_per_destination = sum(len(_chunk(lo, hi)) for _, lo, hi in windows)
    refine = request.refine_top_n
    kept = list(destinations)

    if request.bundle_destinations:
        groups: list[tuple[str, ...]] = [tuple(kept)]
    else:
        budget_for_scans = max(1, request.max_requests - refine)
        affordable = max(1, budget_for_scans // max(1, chunks_per_destination))
        if affordable < len(kept):
            notes.append(
                f"trimmed destinations from {len(kept)} to {affordable} to stay within "
                f"max_requests={request.max_requests}; raise it, narrow the dates, or "
                "set bundle_destinations=true"
            )
            kept = kept[:affordable]
        groups = [(code,) for code in kept]

    scans = tuple(
        GridScan(
            origins=tuple(request.origins),
            destinations=group,
            depart_from=lo,
            depart_to=hi,
            trip_duration_days=duration,
        )
        for group in groups
        for duration, window_lo, window_hi in windows
        for lo, hi in _chunk(window_lo, window_hi)
    )

    # Refinement can never cost more than there are cells to refine.
    refine = min(refine, max(0, request.max_requests - len(scans)))
    if refine < request.refine_top_n:
        notes.append(
            f"reduced refine_top_n from {request.refine_top_n} to {refine} to stay "
            f"within max_requests={request.max_requests}"
        )

    return SearchPlan(
        scans=scans,
        refine_top_n=refine,
        estimated_requests=len(scans) + refine,
        destinations=tuple(kept),
        durations=tuple(d for d, _, _ in windows),
        notes=tuple(notes),
    )
