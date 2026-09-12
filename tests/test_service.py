"""Tests for the `fli` integration layer — filter building and normalization."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import FakeSearchFlights, make_result
from fli.models import MaxStops, SeatType, SortBy, TripType
from fli.search import SearchConnectionError

from app.schemas import SearchRequest
from app.service import SearchError, build_filters, search_flights, suggest_airports


def req(**overrides) -> SearchRequest:
    payload = {
        "origin": "JFK",
        "destination": "LHR",
        "departure_date": date(2026, 10, 15),
    }
    payload.update(overrides)
    return SearchRequest(**payload)


class TestBuildFilters:
    def test_one_way_defaults(self):
        filters = build_filters(req())
        assert filters.trip_type == TripType.ONE_WAY
        assert len(filters.flight_segments) == 1
        assert filters.seat_type == SeatType.ECONOMY
        assert filters.sort_by == SortBy.CHEAPEST
        assert filters.stops == MaxStops.ANY
        assert filters.passenger_info.adults == 1

    def test_round_trip_adds_return_segment(self):
        filters = build_filters(req(return_date=date(2026, 10, 22)))
        assert filters.trip_type == TripType.ROUND_TRIP
        assert len(filters.flight_segments) == 2
        assert filters.flight_segments[1].travel_date == "2026-10-22"

    def test_passengers_and_cabin(self):
        filters = build_filters(
            req(adults=2, children=1, infants_on_lap=1, cabin_class="BUSINESS")
        )
        assert filters.passenger_info.adults == 2
        assert filters.passenger_info.children == 1
        assert filters.passenger_info.infants_on_lap == 1
        assert filters.seat_type == SeatType.BUSINESS

    def test_airline_filters(self):
        filters = build_filters(req(airlines=["ba", "aa"], exclude_airlines=["nk"]))
        assert [a.name.removeprefix("_") for a in filters.airlines] == ["BA", "AA"]
        assert [a.name.removeprefix("_") for a in filters.airlines_exclude] == ["NK"]

    def test_empty_airline_lists_stay_none(self):
        filters = build_filters(req())
        assert filters.airlines is None
        assert filters.airlines_exclude is None

    def test_departure_window(self):
        filters = build_filters(req(departure_window="6-20"))
        restrictions = filters.flight_segments[0].time_restrictions
        assert restrictions.earliest_departure == 6
        assert restrictions.latest_departure == 20

    def test_filters_encode_for_google(self):
        """The built filters must survive `fli`'s wire encoding."""
        assert len(build_filters(req(return_date=date(2026, 10, 22))).encode()) > 0


class TestSearchFlights:
    def test_one_way_normalization(self):
        fake = FakeSearchFlights(results=[make_result()])
        response = search_flights(req(), searcher=fake)

        assert response.count == 1
        itinerary = response.itineraries[0]
        assert itinerary.price == 512.0
        assert itinerary.price_display.startswith("$")
        assert itinerary.max_stops == 0
        assert len(itinerary.slices) == 1
        assert itinerary.slices[0].direction == "outbound"
        assert itinerary.slices[0].origin == "JFK"
        assert itinerary.slices[0].destination == "LHR"
        assert itinerary.airlines == ["AA"]
        assert response.cheapest_price == 512.0

    def test_round_trip_prices_from_outbound_segment(self):
        """Google returns the whole round-trip fare on the outbound leg."""
        outbound = make_result(price=880.0, duration=435)
        inbound = make_result(price=None, duration=400)
        fake = FakeSearchFlights(results=[(outbound, inbound)])

        response = search_flights(req(return_date=date(2026, 10, 22)), searcher=fake)
        itinerary = response.itineraries[0]

        assert itinerary.price == 880.0
        assert itinerary.total_duration_minutes == 835
        assert [s.direction for s in itinerary.slices] == ["outbound", "return"]

    def test_layovers_and_emissions(self, one_stop_result):
        response = search_flights(req(), searcher=FakeSearchFlights(results=[one_stop_result]))
        itinerary = response.itineraries[0]

        assert itinerary.max_stops == 1
        assert itinerary.co2_emissions_kg == 820
        assert itinerary.emissions_vs_typical_pct == -12
        layover = itinerary.slices[0].layovers[0]
        assert layover.airport == "BOS"
        assert layover.duration_minutes == 150
        assert sorted(itinerary.airlines) == ["AA", "BA"]

    def test_missing_price_is_reported_not_dropped(self):
        fake = FakeSearchFlights(results=[make_result(price=None)])
        response = search_flights(req(), searcher=fake)

        assert response.itineraries[0].price is None
        assert response.itineraries[0].price_display == "Price unavailable"
        assert response.cheapest_price is None

    def test_no_results(self):
        response = search_flights(req(), searcher=FakeSearchFlights(results=None))
        assert response.count == 0
        assert response.itineraries == []

    def test_limit_is_respected(self):
        fake = FakeSearchFlights(results=[make_result() for _ in range(10)])
        response = search_flights(req(limit=3), searcher=fake)
        assert response.count == 3

    def test_currency_passed_through(self):
        fake = FakeSearchFlights(results=[])
        search_flights(req(currency="eur"), searcher=fake)
        assert fake.calls[0]["currency"] == "EUR"

    def test_itinerary_ids_are_stable_and_distinct(self):
        fake = FakeSearchFlights(results=[make_result()])
        first = search_flights(req(), searcher=fake).itineraries[0].id
        second = search_flights(req(), searcher=fake).itineraries[0].id
        assert first == second

        other = make_result(legs=[make_result().legs[0].model_copy(update={"flight_number": "999"})])
        different = search_flights(req(), searcher=FakeSearchFlights(results=[other]))
        assert different.itineraries[0].id != first

    def test_booking_url_points_at_the_route(self):
        fake = FakeSearchFlights(results=[make_result()])
        response = search_flights(req(return_date=date(2026, 10, 22)), searcher=fake)
        url = response.itineraries[0].booking_url
        assert "JFK" in url and "LHR" in url and "2026-10-22" in url

    def test_network_failure_becomes_search_error_with_hint(self):
        fake = FakeSearchFlights(error=SearchConnectionError("Could not reach Google Flights"))
        with pytest.raises(SearchError) as exc_info:
            search_flights(req(), searcher=fake)
        assert "Could not reach Google Flights" in exc_info.value.message
        assert "www.google.com" in exc_info.value.hint

    def test_unexpected_error_becomes_search_error(self):
        fake = FakeSearchFlights(error=ValueError("shape changed"))
        with pytest.raises(SearchError) as exc_info:
            search_flights(req(), searcher=fake)
        assert "shape changed" in exc_info.value.message


class TestAirportSuggestions:
    def test_city_lookup(self):
        codes = [m.code for m in suggest_airports("new york")]
        assert "JFK" in codes and "LGA" in codes

    def test_iata_exact_ranks_first(self):
        assert suggest_airports("SFO")[0].code == "SFO"

    def test_airport_name_lookup(self):
        assert "LHR" in [m.code for m in suggest_airports("heathrow")]

    def test_unknown_query(self):
        assert suggest_airports("zzzzzz") == []
