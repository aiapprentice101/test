"""Integration layer over the `fli` library.

This module is the only place that imports `fli`. It translates an
application `SearchRequest` into `fli`'s Google Flights filter models, runs
the search, and normalizes the results into the schemas in `app.schemas`.
Keeping the dependency behind one seam means a breaking change in `fli` —
which tracks an unofficial, reverse-engineered API — is a change to this
file alone.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
from datetime import date

from fli.core import (
    build_date_search_segments,
    build_flight_segments,
    build_time_restrictions,
    format_price,
    parse_airlines,
    parse_cabin_class,
    parse_max_stops,
    parse_sort_by,
    resolve_airport,
    search_airports,
)
from fli.core.parsers import ParseError
from fli.models import (
    DateSearchFilters,
    FlightResult,
    FlightSearchFilters,
    PassengerInfo,
)
from fli.search import (
    SearchClientError,
    SearchDates,
    SearchFlights,
)

from app.schemas import (
    AirportOut,
    DatePriceOut,
    DateSearchResponse,
    ItineraryOut,
    LayoverOut,
    LegOut,
    SearchRequest,
    SearchResponse,
    SliceOut,
)

logger = logging.getLogger(__name__)

GOOGLE_FLIGHTS_URL = "https://www.google.com/travel/flights"


class SearchError(Exception):
    """A search failed in a way worth reporting to the user verbatim.

    `hint` carries an actionable next step when one exists — the most common
    failure is an environment that cannot reach google.com at all.
    """

    def __init__(self, message: str, *, hint: str | None = None, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status_code = status_code


def _booking_url(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str | None = None,
    currency: str | None = None,
) -> str:
    """Build a shareable Google Flights deep link for a route.

    `fli` grew a `fli.core.links.google_flights_url` helper after 0.9.0; this
    reproduces it so the app works on the released version.
    """
    query = f"Flights from {origin} to {destination} on {departure_date}"
    if return_date:
        query += f" through {return_date}"
    url = f"{GOOGLE_FLIGHTS_URL}?q={urllib.parse.quote(query)}"
    if currency:
        url += f"&curr={currency}"
    return url


def _itinerary_id(results: tuple[FlightResult, ...]) -> str:
    """Derive a stable id for an itinerary from its flight numbers and times."""
    parts = [
        f"{leg.airline.name}{leg.flight_number}{leg.departure_datetime.isoformat()}"
        for result in results
        for leg in result.legs
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _airline_name(airline) -> str:
    """Human-readable airline name, falling back to the code."""
    value = getattr(airline, "value", None)
    return value if isinstance(value, str) and value else str(airline.name).removeprefix("_")


def _airline_code(airline) -> str:
    """IATA code for an `fli` Airline enum member."""
    return str(airline.name).removeprefix("_")


def _to_leg(leg) -> LegOut:
    return LegOut(
        airline_code=_airline_code(leg.airline),
        airline_name=_airline_name(leg.airline),
        flight_number=leg.flight_number,
        origin=leg.departure_airport.name,
        origin_name=leg.departure_airport_name,
        destination=leg.arrival_airport.name,
        destination_name=leg.arrival_airport_name,
        departure=leg.departure_datetime,
        arrival=leg.arrival_datetime,
        duration_minutes=leg.duration,
        aircraft=leg.aircraft,
        legroom=leg.legroom_short or leg.legroom,
        overnight=bool(leg.overnight),
    )


def _to_layover(layover) -> LayoverOut:
    return LayoverOut(
        airport=layover.airport.name,
        airport_name=layover.airport_name,
        city=layover.city,
        duration_minutes=layover.duration,
        overnight=bool(layover.overnight),
        change_of_airport=bool(layover.change_of_airport),
    )


def _to_slice(result: FlightResult, direction: str) -> SliceOut:
    legs = [_to_leg(leg) for leg in result.legs]
    return SliceOut(
        direction=direction,
        origin=legs[0].origin,
        destination=legs[-1].destination,
        departure=legs[0].departure,
        arrival=legs[-1].arrival,
        duration_minutes=result.duration,
        stops=result.stops,
        legs=legs,
        layovers=[_to_layover(lo) for lo in (result.layovers or [])],
    )


def _to_itinerary(raw, request: SearchRequest) -> ItineraryOut:
    """Normalize one `fli` search result into an `ItineraryOut`.

    One-way searches yield a bare `FlightResult`; round trips yield a tuple of
    (outbound, return). Google Flights puts the whole round-trip fare on the
    outbound segment, so that is the segment we price from.
    """
    results: tuple[FlightResult, ...] = raw if isinstance(raw, tuple) else (raw,)
    priced = results[0] if len(results) <= 2 else results[-1]

    directions = ["outbound", "return"] if len(results) == 2 else ["outbound"] * len(results)
    slices = [_to_slice(r, d) for r, d in zip(results, directions)]

    airlines = sorted({leg.airline_code for s in slices for leg in s.legs})
    currency = priced.currency or request.currency

    return ItineraryOut(
        id=_itinerary_id(results),
        price=priced.price,
        price_display=format_price(priced.price, currency) if priced.price else "Price unavailable",
        currency=currency,
        total_duration_minutes=sum(r.duration for r in results),
        max_stops=max(r.stops for r in results),
        airlines=airlines,
        primary_airline_name=priced.primary_airline_name,
        co2_emissions_kg=(
            round(priced.co2_emissions_g / 1000) if priced.co2_emissions_g else None
        ),
        emissions_vs_typical_pct=priced.co2_emissions_delta_pct,
        self_transfer=priced.self_transfer,
        mixed_cabin=priced.mixed_cabin,
        slices=slices,
        booking_url=_booking_url(
            request.origin,
            request.destination,
            request.departure_date.isoformat(),
            request.return_date.isoformat() if request.return_date else None,
            request.currency,
        ),
    )


def build_filters(request: SearchRequest) -> FlightSearchFilters:
    """Translate a `SearchRequest` into `fli`'s Google Flights filter model.

    Raises:
        SearchError: with a 422 status when an airport or airline code is not
            one `fli` knows about — that is bad input, not an upstream fault.
    """
    time_restrictions = (
        build_time_restrictions(request.departure_window) if request.departure_window else None
    )
    try:
        origin = resolve_airport(request.origin)
        destination = resolve_airport(request.destination)
        airlines = parse_airlines(request.airlines) or None
        airlines_exclude = parse_airlines(request.exclude_airlines) or None
    except ParseError as exc:
        raise SearchError(str(exc), status_code=422) from exc

    segments, trip_type = build_flight_segments(
        origin=origin,
        destination=destination,
        departure_date=request.departure_date.isoformat(),
        return_date=request.return_date.isoformat() if request.return_date else None,
        time_restrictions=time_restrictions,
    )
    return FlightSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(
            adults=request.adults,
            children=request.children,
            infants_in_seat=request.infants_in_seat,
            infants_on_lap=request.infants_on_lap,
        ),
        flight_segments=segments,
        seat_type=parse_cabin_class(request.cabin_class),
        stops=parse_max_stops(request.max_stops),
        sort_by=parse_sort_by(request.sort_by),
        airlines=airlines,
        airlines_exclude=airlines_exclude,
    )


def search_flights(request: SearchRequest, searcher: SearchFlights | None = None) -> SearchResponse:
    """Run a flight search and return normalized results.

    `searcher` is injectable for tests. `SearchFlights` is documented as not
    thread-safe, so production callers get a fresh instance per request.
    """
    filters = build_filters(request)
    client = searcher or SearchFlights()

    started = time.perf_counter()
    try:
        raw_results = client.search(filters, top_n=request.limit, currency=request.currency)
    except SearchClientError as exc:
        logger.warning("fli search failed: %s", exc)
        raise SearchError(
            str(exc),
            hint=(
                "This app talks to Google Flights directly, so the machine "
                "running it needs outbound access to www.google.com."
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - fli surfaces parse errors as bare exceptions
        logger.exception("unexpected error during flight search")
        raise SearchError(
            f"Flight search failed: {exc}",
            hint="Google Flights may have changed its response format; try upgrading `flights`.",
        ) from exc
    elapsed = time.perf_counter() - started

    itineraries = [_to_itinerary(raw, request) for raw in (raw_results or [])]
    itineraries = itineraries[: request.limit]
    prices = [it.price for it in itineraries if it.price is not None]

    return SearchResponse(
        query=request,
        count=len(itineraries),
        cheapest_price=min(prices) if prices else None,
        currency=itineraries[0].currency if itineraries else request.currency,
        elapsed_seconds=round(elapsed, 2),
        google_flights_url=_booking_url(
            request.origin,
            request.destination,
            request.departure_date.isoformat(),
            request.return_date.isoformat() if request.return_date else None,
            request.currency,
        ),
        itineraries=itineraries,
    )


def search_cheapest_dates(
    origin: str | list[str],
    destination: str | list[str],
    from_date: date,
    to_date: date,
    *,
    cabin_class: str = "ECONOMY",
    max_stops: str = "ANY",
    currency: str = "USD",
    trip_duration: int | None = None,
    airlines: list[str] | None = None,
    adults: int = 1,
    searcher: SearchDates | None = None,
) -> DateSearchResponse:
    """Find the cheapest departure dates across a range.

    One request covers the whole range (`fli` splits ranges over 61 days into
    parallel chunks). `origin` and `destination` accept a list to scan several
    airports at once, which returns the cheapest across them without saying
    which one — pass a single code when you need to know.
    """
    origin_codes = [origin] if isinstance(origin, str) else list(origin)
    destination_codes = [destination] if isinstance(destination, str) else list(destination)
    origin_codes = [c.upper() for c in origin_codes]
    destination_codes = [c.upper() for c in destination_codes]
    currency = currency.upper()
    is_round_trip = trip_duration is not None

    try:
        origin_airports = [resolve_airport(c) for c in origin_codes]
        destination_airports = [resolve_airport(c) for c in destination_codes]
        parsed_airlines = parse_airlines([a.upper() for a in airlines]) if airlines else None
    except ParseError as exc:
        raise SearchError(str(exc), status_code=422) from exc

    segments, trip_type = build_date_search_segments(
        origin=origin_airports if len(origin_airports) > 1 else origin_airports[0],
        destination=(
            destination_airports if len(destination_airports) > 1 else destination_airports[0]
        ),
        start_date=from_date.isoformat(),
        trip_duration=trip_duration,
        is_round_trip=is_round_trip,
    )
    filters = DateSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=adults),
        flight_segments=segments,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        seat_type=parse_cabin_class(cabin_class),
        stops=parse_max_stops(max_stops),
        airlines=parsed_airlines,
        duration=trip_duration,
    )

    client = searcher or SearchDates()
    started = time.perf_counter()
    try:
        raw = client.search(filters, currency=currency)
    except SearchClientError as exc:
        raise SearchError(
            str(exc),
            hint="The machine running this app needs outbound access to www.google.com.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error during date search")
        raise SearchError(f"Date search failed: {exc}") from exc
    elapsed = time.perf_counter() - started

    prices = [
        DatePriceOut(
            departure_date=dp.date[0].date(),
            return_date=dp.date[1].date() if len(dp.date) > 1 else None,
            price=dp.price,
            price_display=format_price(dp.price, dp.currency or currency),
            currency=dp.currency or currency,
        )
        for dp in (raw or [])
    ]
    prices.sort(key=lambda p: p.departure_date)

    return DateSearchResponse(
        origin=",".join(origin_codes),
        destination=",".join(destination_codes),
        count=len(prices),
        cheapest=min(prices, key=lambda p: p.price) if prices else None,
        elapsed_seconds=round(elapsed, 2),
        prices=prices,
    )


def suggest_airports(query: str, limit: int = 8) -> list[AirportOut]:
    """Airport autocomplete over `fli`'s bundled IATA dataset (no network)."""
    return [
        AirportOut(code=m.code.name, name=m.name, match_type=m.match_type, score=m.score)
        for m in search_airports(query, limit=limit)
    ]
