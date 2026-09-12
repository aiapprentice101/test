"""Schemas for wide "find me the best fare" searches.

Flat and explicit on purpose: this is the contract an LLM agent fills in from
a sentence like "Singapore Airlines, SIN to the US, leave December, back in
January, 30 days, business". Nested objects and tuples make tool calls
error-prone, so every knob is a scalar or a list of scalars.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator

from app.schemas import CABIN_CLASSES, ItineraryOut, STOP_OPTIONS


class DealSearchRequest(BaseModel):
    """A search across many dates and destinations at once."""

    origins: list[str] = Field(min_length=1, description="Origin IATA codes, e.g. ['SIN']")
    destinations: list[str] = Field(
        default_factory=list, description="Destination IATA codes; or use destination_region"
    )
    destination_region: str | None = Field(
        default=None, description="Region name such as 'US' or 'EUROPE', expanded to gateways"
    )

    depart_from: date = Field(description="Earliest acceptable departure date")
    depart_to: date = Field(description="Latest acceptable departure date")
    return_from: date | None = Field(default=None, description="Earliest acceptable return")
    return_to: date | None = Field(default=None, description="Latest acceptable return")

    min_trip_days: int | None = Field(default=None, ge=1, le=365)
    max_trip_days: int | None = Field(default=None, ge=1, le=365)

    cabin_class: str = "ECONOMY"
    max_stops: str = "ANY"
    airlines: list[str] = Field(default_factory=list, description="Airline IATA codes, e.g. ['SQ']")
    adults: int = Field(default=1, ge=1, le=9)
    currency: str = Field(default="USD", min_length=3, max_length=3)

    refine_top_n: int = Field(
        default=10, ge=0, le=40, description="How many of the cheapest dates to price in full"
    )
    max_requests: int = Field(
        default=60, ge=1, le=500, description="Hard ceiling on upstream requests"
    )
    bundle_destinations: bool = Field(
        default=False,
        description=(
            "Scan all destinations in one request instead of one each. Much cheaper, "
            "but the grid then cannot say which destination a price belongs to."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> DealSearchRequest:
        self.origins = [c.strip().upper() for c in self.origins if c.strip()]
        self.destinations = [c.strip().upper() for c in self.destinations if c.strip()]
        self.airlines = [c.strip().upper() for c in self.airlines if c.strip()]
        self.currency = self.currency.upper()
        self.cabin_class = self.cabin_class.strip().upper()
        self.max_stops = self.max_stops.strip().upper()

        if not self.origins:
            raise ValueError("at least one origin is required")
        if not self.destinations and not self.destination_region:
            raise ValueError("provide destinations or a destination_region")
        if self.cabin_class not in CABIN_CLASSES:
            raise ValueError(f"cabin_class must be one of {', '.join(CABIN_CLASSES)}")
        if self.max_stops not in STOP_OPTIONS:
            raise ValueError(f"max_stops must be one of {', '.join(STOP_OPTIONS)}")
        if self.depart_to < self.depart_from:
            raise ValueError("depart_to cannot precede depart_from")
        if (self.return_from is None) != (self.return_to is None):
            raise ValueError("return_from and return_to must be given together")
        if self.return_to and self.return_from and self.return_to < self.return_from:
            raise ValueError("return_to cannot precede return_from")
        if self.min_trip_days and self.max_trip_days and self.max_trip_days < self.min_trip_days:
            raise ValueError("max_trip_days cannot be less than min_trip_days")
        return self

    @property
    def is_round_trip(self) -> bool:
        """Whether this is a return trip rather than one-way."""
        return bool(self.return_from or self.min_trip_days or self.max_trip_days)


class GridCell(BaseModel):
    """One (destination, departure date) price from the coarse scan."""

    destination: str
    origin: str
    departure_date: date
    return_date: date | None = None
    trip_days: int | None = None
    price: float
    currency: str


class Deal(BaseModel):
    """A ranked candidate, optionally priced in full."""

    rank: int
    origin: str
    destination: str
    departure_date: date
    return_date: date | None = None
    trip_days: int | None = None
    grid_price: float
    currency: str
    savings_vs_median: float | None = None
    itinerary: ItineraryOut | None = None
    refine_note: str | None = None


class DealSearchStats(BaseModel):
    """What the search actually cost."""

    requests_made: int
    requests_estimated: int
    grid_cells: int
    scans_succeeded: int
    scans_failed: int
    elapsed_seconds: float


class DealSearchResult(BaseModel):
    """The answer: the best fare, the runners-up, and the price landscape."""

    query: DealSearchRequest
    best: Deal | None = None
    deals: list[Deal] = Field(default_factory=list)
    calendar: list[GridCell] = Field(default_factory=list)
    cheapest_by_destination: dict[str, float] = Field(default_factory=dict)
    median_price: float | None = None
    currency: str | None = None
    stats: DealSearchStats
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
