"""The tools Claude can call.

Docstrings here are the tool descriptions the model actually reads, so they
carry the usage rules: when to plan before searching, how dates work, and
what "cheap" means in requests.

Every tool returns a *compact* string. Full results — hundreds of grid cells,
complete itineraries — go to `record_result` for the UI to render, because
feeding them back through the model would be slow, expensive, and pointless.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from anthropic import beta_tool

from app.agent.context import emit, record_result
from app.deals.engine import run_deal_search
from app.deals.planner import PlanError, plan_search
from app.deals.regions import known_regions
from app.deals.schemas import DealSearchRequest
from app.providers.fli_provider import FliProvider
from app.schemas import SearchRequest
from app.service import SearchError, search_flights, suggest_airports


def _codes(value: str) -> list[str]:
    """Split a comma-separated code list. Tools take strings, not arrays, so
    the model cannot trip over JSON-schema array edge cases."""
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}") from exc


def _build_request(
    origins: str,
    destinations: str,
    destination_region: str,
    depart_from: str,
    depart_to: str,
    return_from: str,
    return_to: str,
    min_trip_days: int,
    max_trip_days: int,
    cabin_class: str,
    airlines: str,
    max_stops: str,
    adults: int,
    currency: str,
    refine_top_n: int,
    max_requests: int,
) -> DealSearchRequest:
    """Assemble a validated DealSearchRequest from flat tool arguments."""
    return DealSearchRequest(
        origins=_codes(origins),
        destinations=_codes(destinations),
        destination_region=destination_region.strip() or None,
        depart_from=_date(depart_from, "depart_from"),
        depart_to=_date(depart_to, "depart_to"),
        return_from=_date(return_from, "return_from") if return_from.strip() else None,
        return_to=_date(return_to, "return_to") if return_to.strip() else None,
        min_trip_days=min_trip_days or None,
        max_trip_days=max_trip_days or None,
        cabin_class=cabin_class,
        airlines=_codes(airlines),
        max_stops=max_stops,
        adults=adults,
        currency=currency,
        refine_top_n=refine_top_n,
        max_requests=max_requests,
    )


@beta_tool
def find_airports(query: str) -> str:
    """Look up airport IATA codes by city name, airport name, or code.

    Use this whenever the user names a place rather than a code — "Singapore",
    "the Bay Area", "Heathrow". Runs against a local dataset, so it is instant
    and free. Returns up to 8 matches as "CODE - Name" lines.

    Args:
        query: A city, airport name, or IATA code.
    """
    matches = suggest_airports(query, limit=8)
    if not matches:
        return f"No airports matched {query!r}."
    return "\n".join(f"{m.code} - {m.name}" for m in matches)


@beta_tool
def list_destination_regions() -> str:
    """List the region names accepted by `destination_region`.

    A region expands to a curated set of long-haul gateway airports, e.g. US
    expands to 12 gateways. Use a region when the user says "the USA" or
    "Europe" rather than naming airports.
    """
    return ", ".join(known_regions())


@beta_tool
def estimate_search_cost(
    origins: str,
    destinations: str = "",
    destination_region: str = "",
    depart_from: str = "",
    depart_to: str = "",
    return_from: str = "",
    return_to: str = "",
    min_trip_days: int = 0,
    max_trip_days: int = 0,
    cabin_class: str = "ECONOMY",
    airlines: str = "",
    max_stops: str = "ANY",
    adults: int = 1,
    currency: str = "USD",
    refine_top_n: int = 10,
    max_requests: int = 60,
) -> str:
    """Cost a wide search in upstream requests WITHOUT running it.

    Always call this before `find_best_fares` when the search covers a region
    or a long date range. It is free and instant, and it reports the narrowed
    departure window — which often differs from what the user said, because a
    30-day trip departing 1 December returns on 31 December and so cannot
    satisfy "back in January".

    If the estimate looks too large, narrow the dates, name fewer
    destinations, or lower refine_top_n before searching.

    Args:
        origins: Origin IATA codes, comma-separated, e.g. "SIN".
        destinations: Destination IATA codes, comma-separated. Leave empty when using a region.
        destination_region: A region name such as "US" instead of listing airports.
        depart_from: Earliest departure date, YYYY-MM-DD.
        depart_to: Latest departure date, YYYY-MM-DD.
        return_from: Earliest return date, YYYY-MM-DD. Empty for one-way.
        return_to: Latest return date, YYYY-MM-DD. Empty for one-way.
        min_trip_days: Shortest acceptable trip length in days. 0 if unspecified.
        max_trip_days: Longest acceptable trip length in days. 0 if unspecified.
        cabin_class: ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST.
        airlines: Airline IATA codes to restrict to, comma-separated, e.g. "SQ".
        max_stops: ANY, NON_STOP, ONE_STOP_OR_FEWER, or TWO_OR_FEWER_STOPS.
        adults: Number of adult passengers.
        currency: ISO 4217 currency code.
        refine_top_n: How many of the cheapest dates to price in full.
        max_requests: Hard ceiling on upstream requests.
    """
    request = _build_request(
        origins, destinations, destination_region, depart_from, depart_to,
        return_from, return_to, min_trip_days, max_trip_days, cabin_class,
        airlines, max_stops, adults, currency, refine_top_n, max_requests,
    )
    try:
        plan = plan_search(request)
    except PlanError as exc:
        return f"This search cannot run as specified: {exc}"

    emit("plan", estimated_requests=plan.estimated_requests,
         destinations=list(plan.destinations))
    lines = [
        f"~{plan.estimated_requests} upstream requests "
        f"({plan.scan_requests} calendar scans + {plan.refine_top_n} full lookups)",
        f"Destinations: {', '.join(plan.destinations)}",
        f"Trip lengths: {', '.join(str(d) for d in plan.durations)} days",
    ]
    if plan.scans:
        lines.append(f"Departure window: {plan.scans[0].depart_from} to {plan.scans[0].depart_to}")
    lines.extend(f"Note: {note}" for note in plan.notes)
    return "\n".join(lines)


@beta_tool
def find_best_fares(
    origins: str,
    destinations: str = "",
    destination_region: str = "",
    depart_from: str = "",
    depart_to: str = "",
    return_from: str = "",
    return_to: str = "",
    min_trip_days: int = 0,
    max_trip_days: int = 0,
    cabin_class: str = "ECONOMY",
    airlines: str = "",
    max_stops: str = "ANY",
    adults: int = 1,
    currency: str = "USD",
    refine_top_n: int = 10,
    max_requests: int = 60,
) -> str:
    """Search many dates and destinations at once for the cheapest fare.

    This is the main tool. It scans the whole departure window in one request
    per destination, then prices only the cheapest candidates in full, so a
    month-wide search across a dozen airports costs about 22 requests rather
    than hundreds. It takes minutes, not seconds.

    Call `estimate_search_cost` first for anything wide. The arguments are
    identical.

    Returns the best fare, the runners-up, and the cheapest price per
    destination. The user sees the full price calendar in the UI, so do not
    try to repeat every date back to them — summarise, and say which dates and
    which airport win.

    Args:
        origins: Origin IATA codes, comma-separated, e.g. "SIN".
        destinations: Destination IATA codes, comma-separated. Leave empty when using a region.
        destination_region: A region name such as "US" instead of listing airports.
        depart_from: Earliest departure date, YYYY-MM-DD.
        depart_to: Latest departure date, YYYY-MM-DD.
        return_from: Earliest return date, YYYY-MM-DD. Empty for one-way.
        return_to: Latest return date, YYYY-MM-DD. Empty for one-way.
        min_trip_days: Shortest acceptable trip length in days. 0 if unspecified.
        max_trip_days: Longest acceptable trip length in days. 0 if unspecified.
        cabin_class: ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST.
        airlines: Airline IATA codes to restrict to, comma-separated, e.g. "SQ".
        max_stops: ANY, NON_STOP, ONE_STOP_OR_FEWER, or TWO_OR_FEWER_STOPS.
        adults: Number of adult passengers.
        currency: ISO 4217 currency code.
        refine_top_n: How many of the cheapest dates to price in full.
        max_requests: Hard ceiling on upstream requests.
    """
    request = _build_request(
        origins, destinations, destination_region, depart_from, depart_to,
        return_from, return_to, min_trip_days, max_trip_days, cabin_class,
        airlines, max_stops, adults, currency, refine_top_n, max_requests,
    )
    try:
        result = run_deal_search(
            request,
            FliProvider(),
            progress=lambda stage, done, total: emit(
                "progress", stage=stage, done=done, total=total
            ),
        )
    except PlanError as exc:
        return f"This search cannot run as specified: {exc}"
    except SearchError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        return f"The search failed: {exc.message}{hint}"

    record_result("deal_search", result.model_dump(mode="json"))

    if not result.deals:
        detail = "; ".join(result.warnings[:3]) or "no prices returned"
        return f"No fares found. {detail}"

    lines = [result.summary, ""]
    for deal in result.deals[:10]:
        flights = ""
        if deal.itinerary:
            flights = " | " + " / ".join(
                " ".join(f"{leg.airline_code}{leg.flight_number}" for leg in s.legs)
                for s in deal.itinerary.slices
            )
        back = f" back {deal.return_date}" if deal.return_date else ""
        lines.append(
            f"{deal.rank}. {deal.origin}-{deal.destination} "
            f"depart {deal.departure_date}{back}: "
            f"{deal.currency} {deal.grid_price:,.0f}{flights}"
        )
    if result.cheapest_by_destination:
        cheapest = sorted(result.cheapest_by_destination.items(), key=lambda kv: kv[1])
        lines.append("")
        lines.append("Cheapest per destination: " + ", ".join(
            f"{dest} {result.currency} {price:,.0f}" for dest, price in cheapest
        ))
    stats = result.stats
    lines.append(
        f"\n({stats.requests_made} requests, {stats.grid_cells} date/route "
        f"combinations priced, {stats.elapsed_seconds}s)"
    )
    for warning in result.warnings[:3]:
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)


@beta_tool
def search_one_date(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str = "",
    cabin_class: str = "ECONOMY",
    airlines: str = "",
    max_stops: str = "ANY",
    adults: int = 1,
    currency: str = "USD",
) -> str:
    """Get full itineraries for ONE specific route and date.

    Use this when the user already knows the date, or to drill into a result
    from `find_best_fares`. Costs a single request. For "which date is
    cheapest" questions, use `find_best_fares` instead.

    Args:
        origin: Origin IATA code.
        destination: Destination IATA code.
        departure_date: Departure date, YYYY-MM-DD.
        return_date: Return date, YYYY-MM-DD. Empty for one-way.
        cabin_class: ECONOMY, PREMIUM_ECONOMY, BUSINESS, or FIRST.
        airlines: Airline IATA codes to restrict to, comma-separated.
        max_stops: ANY, NON_STOP, ONE_STOP_OR_FEWER, or TWO_OR_FEWER_STOPS.
        adults: Number of adult passengers.
        currency: ISO 4217 currency code.
    """
    try:
        request = SearchRequest(
            origin=origin,
            destination=destination,
            departure_date=_date(departure_date, "departure_date"),
            return_date=_date(return_date, "return_date") if return_date.strip() else None,
            cabin_class=cabin_class,
            airlines=_codes(airlines),
            max_stops=max_stops,
            adults=adults,
            currency=currency,
            sort_by="CHEAPEST",
            limit=8,
        )
        response = search_flights(request)
    except SearchError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        return f"The search failed: {exc.message}{hint}"
    except ValueError as exc:
        return f"Invalid search: {exc}"

    record_result("point_search", response.model_dump(mode="json"))
    if not response.itineraries:
        return "No flights found for that route and date."

    lines = [f"{response.count} itineraries, cheapest {response.itineraries[0].price_display}:"]
    for itinerary in response.itineraries[:6]:
        flights = " / ".join(
            " ".join(f"{leg.airline_code}{leg.flight_number}" for leg in s.legs)
            for s in itinerary.slices
        )
        hours, minutes = divmod(itinerary.total_duration_minutes, 60)
        lines.append(
            f"- {itinerary.price_display}, {hours}h{minutes:02d}m, "
            f"{itinerary.max_stops} stop(s): {flights}"
        )
    return "\n".join(lines)


ALL_TOOLS = [
    find_airports,
    list_destination_regions,
    estimate_search_cost,
    find_best_fares,
    search_one_date,
]


def tool_schemas() -> str:
    """Tool names and one-line descriptions, for debugging and the MCP server."""
    return json.dumps([t.name for t in ALL_TOOLS])
