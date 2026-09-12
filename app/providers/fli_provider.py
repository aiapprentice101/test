"""`fli`-backed provider: Google Flights via the reverse-engineered endpoint.

Free and, uniquely among the options considered, it exposes the calendar
graph that makes wide date scans cheap. The tradeoff is that requests come
from *your* IP against an endpoint that is not a public API, so this module
deliberately runs slower than `fli`'s default.
"""

from __future__ import annotations

import logging

from app.providers.base import GridPrice, GridQuery, ItineraryQuery
from app.schemas import SearchRequest
from app.service import search_cheapest_dates, search_flights

logger = logging.getLogger(__name__)

# `fli` defaults to 10 requests/second, which is nothing like human traffic
# and is the surest way to get an IP throttled. One per second is still far
# faster than a person clicking, and a wide search is dominated by the
# number of requests, not their spacing.
DEFAULT_CALLS_PER_SECOND = 1


def configure_rate_limit(calls_per_second: int = DEFAULT_CALLS_PER_SECOND) -> None:
    """Install a slower shared `fli` HTTP client.

    `fli` builds its client as a process-wide singleton on first use, so this
    must run before any search. Reaching into the module is deliberate: the
    rate is not otherwise configurable, and the default is too aggressive to
    point at a private endpoint.
    """
    import fli.search.client as fli_client

    with fli_client._client_lock:  # noqa: SLF001 - no public setter exists
        fli_client.client = fli_client.Client(calls_per_second=calls_per_second)
    logger.info("fli rate limit set to %s req/sec", calls_per_second)


class FliProvider:
    """Adapts `fli` to the `FlightProvider` protocol."""

    name = "fli"
    supports_date_grid = True

    def scan_date_grid(self, query: GridQuery) -> list[GridPrice]:
        """One calendar-graph request per range; returns a price per date."""
        # `fli`'s date search takes a single origin/destination pair or a list
        # for a bundled multi-airport scan. The planner decides which.
        origin = list(query.origins) if len(query.origins) > 1 else query.origins[0]
        destination = (
            list(query.destinations) if len(query.destinations) > 1 else query.destinations[0]
        )
        response = search_cheapest_dates(
            origin=origin,
            destination=destination,
            from_date=query.depart_from,
            to_date=query.depart_to,
            cabin_class=query.cabin_class,
            max_stops=query.max_stops,
            currency=query.currency,
            trip_duration=query.trip_duration_days,
            airlines=list(query.airlines) or None,
            adults=query.adults,
        )
        return [
            GridPrice(
                departure_date=p.departure_date,
                return_date=p.return_date,
                price=p.price,
                currency=p.currency or query.currency,
            )
            for p in response.prices
        ]

    def search_itineraries(self, query: ItineraryQuery) -> list:
        """Full itinerary detail for one date pair."""
        request = SearchRequest(
            origin=query.origin,
            destination=query.destination,
            departure_date=query.departure_date,
            return_date=query.return_date,
            cabin_class=query.cabin_class,
            max_stops=query.max_stops,
            airlines=list(query.airlines),
            currency=query.currency,
            adults=query.adults,
            sort_by="CHEAPEST",
            limit=query.limit,
        )
        return search_flights(request).itineraries
