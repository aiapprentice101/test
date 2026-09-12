"""Shared fixtures: fake `fli` results so tests never touch the network."""

from __future__ import annotations

from datetime import datetime

import pytest
from fli.models import Airline, Airport, FlightLeg, FlightResult, Layover


def make_leg(
    airline: Airline = Airline.AA,
    flight_number: str = "100",
    origin: Airport = Airport.JFK,
    destination: Airport = Airport.LHR,
    departure: str = "2026-10-15T18:30:00",
    arrival: str = "2026-10-16T06:45:00",
    duration: int = 435,
) -> FlightLeg:
    """Build a FlightLeg with sensible defaults."""
    return FlightLeg(
        airline=airline,
        flight_number=flight_number,
        departure_airport=origin,
        arrival_airport=destination,
        departure_datetime=datetime.fromisoformat(departure),
        arrival_datetime=datetime.fromisoformat(arrival),
        duration=duration,
        departure_airport_name="John F Kennedy International Airport",
        arrival_airport_name="London Heathrow Airport",
        aircraft="Boeing 777",
        legroom_short="31 in",
    )


def make_result(
    price: float | None = 512.0,
    legs: list[FlightLeg] | None = None,
    stops: int = 0,
    duration: int = 435,
    **kwargs,
) -> FlightResult:
    """Build a FlightResult with sensible defaults."""
    return FlightResult(
        legs=legs if legs is not None else [make_leg()],
        price=price,
        currency=kwargs.pop("currency", "USD"),
        duration=duration,
        stops=stops,
        **kwargs,
    )


@pytest.fixture
def one_stop_result() -> FlightResult:
    """A one-stop JFK→LHR itinerary with a layover in Boston."""
    return make_result(
        price=389.0,
        stops=1,
        duration=620,
        legs=[
            make_leg(flight_number="200", destination=Airport.BOS, arrival="2026-10-15T20:00:00", duration=90),
            make_leg(
                airline=Airline.BA,
                flight_number="212",
                origin=Airport.BOS,
                departure="2026-10-15T22:30:00",
                arrival="2026-10-16T10:10:00",
                duration=400,
            ),
        ],
        layovers=[Layover(airport=Airport.BOS, duration=150, city="Boston")],
        co2_emissions_g=820000,
        co2_emissions_delta_pct=-12,
    )


class FakeSearchFlights:
    """Stand-in for `fli.search.SearchFlights` that returns canned results."""

    def __init__(self, results=None, error: Exception | None = None):
        self.results = results
        self.error = error
        self.calls: list[dict] = []

    def search(self, filters, top_n=5, currency=None, **kwargs):
        self.calls.append({"filters": filters, "top_n": top_n, "currency": currency})
        if self.error:
            raise self.error
        return self.results
