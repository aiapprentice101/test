"""Request and response schemas for the flight search API.

These are the application's own contract, deliberately decoupled from the
`fli` library's models: `fli` mirrors Google Flights' internal shapes, which
change without notice. Everything that crosses the HTTP boundary is defined
here and populated in `app.service`.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

CABIN_CLASSES = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST")
STOP_OPTIONS = ("ANY", "NON_STOP", "ONE_STOP_OR_FEWER", "TWO_OR_FEWER_STOPS")
SORT_OPTIONS = ("CHEAPEST", "BEST", "DURATION", "DEPARTURE_TIME", "ARRIVAL_TIME", "EMISSIONS")


class SearchRequest(BaseModel):
    """Everything the user can ask for in a single flight search."""

    origin: str = Field(min_length=3, max_length=3, description="Origin IATA code, e.g. JFK")
    destination: str = Field(min_length=3, max_length=3, description="Destination IATA code")
    departure_date: date
    return_date: date | None = None

    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=8)
    infants_in_seat: int = Field(default=0, ge=0, le=8)
    infants_on_lap: int = Field(default=0, ge=0, le=8)

    cabin_class: str = "ECONOMY"
    max_stops: str = "ANY"
    sort_by: str = "CHEAPEST"

    departure_window: str | None = Field(
        default=None,
        description="Departure time window as 'HH-HH', e.g. '6-20'.",
        pattern=r"^\d{1,2}-\d{1,2}$",
    )
    airlines: list[str] = Field(default_factory=list, description="Airline IATA codes to include")
    exclude_airlines: list[str] = Field(default_factory=list, description="Codes to exclude")

    currency: str = Field(default="USD", min_length=3, max_length=3)
    limit: int = Field(default=30, ge=1, le=100)

    @field_validator("origin", "destination", "currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("cabin_class", "max_stops", "sort_by")
    @classmethod
    def _upper_enum(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("airlines", "exclude_airlines")
    @classmethod
    def _upper_codes(cls, v: list[str]) -> list[str]:
        return [code.strip().upper() for code in v if code.strip()]

    @model_validator(mode="after")
    def _check_consistency(self) -> SearchRequest:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.cabin_class not in CABIN_CLASSES:
            raise ValueError(f"cabin_class must be one of {', '.join(CABIN_CLASSES)}")
        if self.max_stops not in STOP_OPTIONS:
            raise ValueError(f"max_stops must be one of {', '.join(STOP_OPTIONS)}")
        if self.sort_by not in SORT_OPTIONS:
            raise ValueError(f"sort_by must be one of {', '.join(SORT_OPTIONS)}")
        if self.return_date and self.return_date < self.departure_date:
            raise ValueError("return_date cannot be before departure_date")
        if self.departure_window:
            start, end = (int(p) for p in self.departure_window.split("-"))
            if not (0 <= start <= 23 and 1 <= end <= 24):
                raise ValueError("departure_window hours must be within 0-24")
            if start >= end:
                raise ValueError("departure_window start must be before end")
        return self

    @property
    def is_round_trip(self) -> bool:
        """Whether a return date was supplied."""
        return self.return_date is not None


class LegOut(BaseModel):
    """A single operated flight within an itinerary."""

    airline_code: str
    airline_name: str
    flight_number: str
    origin: str
    origin_name: str | None = None
    destination: str
    destination_name: str | None = None
    departure: datetime
    arrival: datetime
    duration_minutes: int
    aircraft: str | None = None
    legroom: str | None = None
    overnight: bool = False


class LayoverOut(BaseModel):
    """A connection between two legs."""

    airport: str
    airport_name: str | None = None
    city: str | None = None
    duration_minutes: int
    overnight: bool = False
    change_of_airport: bool = False


class SliceOut(BaseModel):
    """One direction of travel: outbound or return."""

    direction: str  # "outbound" | "return"
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    duration_minutes: int
    stops: int
    legs: list[LegOut]
    layovers: list[LayoverOut] = Field(default_factory=list)


class ItineraryOut(BaseModel):
    """A complete priced trip: one slice for one-way, two for a round trip."""

    id: str
    price: float | None = None
    price_display: str
    currency: str | None = None
    total_duration_minutes: int
    max_stops: int
    airlines: list[str]
    primary_airline_name: str | None = None
    co2_emissions_kg: int | None = None
    emissions_vs_typical_pct: int | None = None
    self_transfer: bool | None = None
    mixed_cabin: bool | None = None
    slices: list[SliceOut]
    booking_url: str


class SearchResponse(BaseModel):
    """The full result payload for one search."""

    query: SearchRequest
    count: int
    cheapest_price: float | None = None
    currency: str | None = None
    elapsed_seconds: float
    google_flights_url: str
    itineraries: list[ItineraryOut]


class AirportOut(BaseModel):
    """One airport suggestion for the autocomplete."""

    code: str
    name: str
    match_type: str
    score: float


class DatePriceOut(BaseModel):
    """The cheapest price found for a departure date."""

    departure_date: date
    return_date: date | None = None
    price: float
    price_display: str
    currency: str | None = None


class DateSearchResponse(BaseModel):
    """Cheapest-date results across a range."""

    origin: str
    destination: str
    count: int
    cheapest: DatePriceOut | None = None
    elapsed_seconds: float
    prices: list[DatePriceOut]


class ErrorOut(BaseModel):
    """A structured error returned to the client."""

    error: str
    detail: str
    hint: str | None = None
