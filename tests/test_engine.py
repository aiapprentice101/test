"""Tests for the two-phase deal engine, driven by a fake provider."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import make_result

from app.deals.engine import run_deal_search
from app.deals.schemas import DealSearchRequest
from app.providers.base import GridPrice
from app.service import SearchError, _to_itinerary
from app.schemas import SearchRequest


class FakeProvider:
    """Records every request and returns deterministic prices."""

    name = "fake"
    supports_date_grid = True

    def __init__(self, prices_by_destination=None, fail_on=(), grid_error=None):
        self.prices_by_destination = prices_by_destination or {}
        self.fail_on = set(fail_on)
        self.grid_error = grid_error
        self.grid_calls = []
        self.itinerary_calls = []

    def scan_date_grid(self, query):
        self.grid_calls.append(query)
        destination = query.destinations[0]
        if destination in self.fail_on:
            raise SearchError("upstream said no")
        if self.grid_error:
            raise self.grid_error
        base = self.prices_by_destination.get(destination, 5000.0)
        out = []
        day = query.depart_from
        offset = 0
        while day <= query.depart_to:
            return_date = (
                day + timedelta(days=query.trip_duration_days)
                if query.trip_duration_days
                else None
            )
            out.append(
                GridPrice(
                    departure_date=day,
                    return_date=return_date,
                    price=base + offset * 10,
                    currency=query.currency,
                )
            )
            day += timedelta(days=1)
            offset += 1
        return out

    def search_itineraries(self, query):
        self.itinerary_calls.append(query)
        request = SearchRequest(
            origin=query.origin,
            destination=query.destination,
            departure_date=query.departure_date,
        )
        return [_to_itinerary(make_result(), request)]


def req(**overrides) -> DealSearchRequest:
    payload = {
        "origins": ["SIN"],
        "destinations": ["LAX", "SFO", "JFK"],
        "depart_from": date(2026, 12, 1),
        "depart_to": date(2026, 12, 10),
        "return_from": date(2026, 12, 31),
        "return_to": date(2027, 1, 10),
        "min_trip_days": 30,
        "max_trip_days": 30,
        "cabin_class": "BUSINESS",
        "airlines": ["SQ"],
        "refine_top_n": 3,
    }
    payload.update(overrides)
    return DealSearchRequest(**payload)


class TestTwoPhaseSearch:
    def test_finds_the_cheapest_across_destinations_and_dates(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0, "JFK": 7000.0})
        result = run_deal_search(req(), provider)

        assert result.best is not None
        assert result.best.destination == "SFO"
        assert result.best.departure_date == date(2026, 12, 1)
        assert result.best.grid_price == 4200.0
        assert result.best.rank == 1

    def test_one_scan_per_destination_not_per_date(self):
        """The whole point: 10 dates x 3 routes costs 3 scans, not 30."""
        provider = FakeProvider()
        run_deal_search(req(), provider)
        assert len(provider.grid_calls) == 3

    def test_only_the_top_n_are_priced_in_full(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0, "JFK": 7000.0})
        result = run_deal_search(req(refine_top_n=3), provider)

        assert len(provider.itinerary_calls) == 3
        assert all(deal.itinerary is not None for deal in result.deals)
        assert len(result.calendar) == 30  # still the full grid

    def test_deals_are_ranked_cheapest_first(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0, "JFK": 7000.0})
        result = run_deal_search(req(refine_top_n=5), provider)
        prices = [deal.grid_price for deal in result.deals]
        assert prices == sorted(prices)
        assert [d.rank for d in result.deals] == [1, 2, 3, 4, 5]

    def test_return_dates_outside_the_window_are_discarded(self):
        """A 30-day trip from 1 Dec returns 31 Dec; later departures overshoot."""
        result = run_deal_search(req(), FakeProvider())
        assert all(
            date(2026, 12, 31) <= cell.return_date <= date(2027, 1, 10)
            for cell in result.calendar
        )

    def test_cheapest_by_destination_summary(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0, "JFK": 7000.0})
        result = run_deal_search(req(), provider)
        assert result.cheapest_by_destination == {"SFO": 4200.0, "LAX": 6000.0, "JFK": 7000.0}

    def test_savings_against_median(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0, "JFK": 7000.0})
        result = run_deal_search(req(), provider)
        assert result.median_price is not None
        assert result.best.savings_vs_median > 0

    def test_summary_is_human_readable(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0, "JFK": 7000.0})
        result = run_deal_search(req(), provider)
        assert "SFO" in result.summary
        assert "2026-12-01" in result.summary


class TestResilience:
    def test_one_failed_route_does_not_sink_the_search(self):
        provider = FakeProvider({"LAX": 6000.0, "SFO": 4200.0}, fail_on={"JFK"})
        result = run_deal_search(req(), provider)

        assert result.best.destination == "SFO"
        assert result.stats.scans_failed == 1
        assert result.stats.scans_succeeded == 2
        assert any("JFK" in w for w in result.warnings)

    def test_every_route_failing_yields_no_deals_not_a_crash(self):
        provider = FakeProvider(fail_on={"LAX", "SFO", "JFK"})
        result = run_deal_search(req(), provider)

        assert result.best is None
        assert result.deals == []
        assert result.stats.scans_failed == 3
        assert "No fares found" in result.summary

    def test_refine_failure_keeps_the_grid_price(self):
        provider = FakeProvider({"LAX": 4000.0})

        def boom(query):
            raise RuntimeError("itinerary lookup exploded")

        provider.search_itineraries = boom
        result = run_deal_search(req(destinations=["LAX"]), provider)

        assert result.best.grid_price == 4000.0
        assert result.best.itinerary is None
        assert "could not price in full" in result.best.refine_note

    def test_provider_without_date_grid_is_refused(self):
        class NoGrid(FakeProvider):
            supports_date_grid = False

        with pytest.raises(SearchError, match="cannot scan date ranges"):
            run_deal_search(req(), NoGrid())

    def test_stats_count_actual_requests(self):
        provider = FakeProvider()
        result = run_deal_search(req(refine_top_n=2), provider)
        assert result.stats.requests_made == 3 + 2


class TestBundling:
    def test_bundled_scan_costs_one_request(self):
        provider = FakeProvider()
        result = run_deal_search(req(bundle_destinations=True), provider)

        assert len(provider.grid_calls) == 1
        assert len(provider.grid_calls[0].destinations) == 3
        # Bundled prices cannot be attributed, so nothing is priced in full.
        assert provider.itinerary_calls == []
        assert all("bundled scan" in d.refine_note for d in result.deals)


class TestProgress:
    def test_progress_is_reported_for_both_phases(self):
        seen = []
        run_deal_search(req(), FakeProvider(), lambda *args: seen.append(args))
        stages = {stage for stage, _, _ in seen}
        assert stages == {"scan", "refine"}
