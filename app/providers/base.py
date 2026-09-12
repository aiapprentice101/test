"""Provider interface for flight data sources.

The deal engine speaks only this protocol, so swapping `fli` for a licensed
API (Amadeus, Duffel) or a paid scraper is a new module, not a rewrite.

Two capabilities matter, and they are very different in cost:

* `scan_date_grid` — one request returns a price for *every* date in a range.
  This is what makes a wide search affordable; without it a month-long,
  multi-destination search costs one request per date per route.
* `search_itineraries` — full detail (flight numbers, times, layovers) for
  one specific date. Expensive, so the engine only spends it on winners.

A provider that cannot do the first must set `supports_date_grid = False`;
the engine then refuses wide searches rather than silently melting your
rate limit or your bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class GridPrice:
    """The cheapest fare found for one departure (and return) date."""

    departure_date: date
    return_date: date | None
    price: float
    currency: str


@dataclass(frozen=True)
class GridQuery:
    """A coarse price scan over a departure-date range."""

    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    depart_from: date
    depart_to: date
    trip_duration_days: int | None
    cabin_class: str
    max_stops: str
    airlines: tuple[str, ...]
    currency: str
    adults: int


@dataclass(frozen=True)
class ItineraryQuery:
    """A full itinerary lookup for one concrete date pair."""

    origin: str
    destination: str
    departure_date: date
    return_date: date | None
    cabin_class: str
    max_stops: str
    airlines: tuple[str, ...]
    currency: str
    adults: int
    limit: int = 5


@runtime_checkable
class FlightProvider(Protocol):
    """What the deal engine needs from a data source."""

    name: str
    supports_date_grid: bool

    def scan_date_grid(self, query: GridQuery) -> list[GridPrice]:
        """Return one price per departure date across the query's range."""
        ...

    def search_itineraries(self, query: ItineraryQuery) -> list:
        """Return full `ItineraryOut` results for one concrete date pair."""
        ...
