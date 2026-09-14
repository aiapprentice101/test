"""Tests for the price cache and its provider wrapper."""

from __future__ import annotations

import time
from datetime import date

import pytest
from test_engine import FakeProvider

from app.cache import CachedProvider, PriceCache, grid_key
from app.deals.engine import run_deal_search
from app.deals.schemas import DealSearchRequest
from app.providers.base import GridPrice, GridQuery, ItineraryQuery


def query(**overrides) -> GridQuery:
    base = {
        "origins": ("SIN",), "destinations": ("LAX",),
        "depart_from": date(2026, 12, 1), "depart_to": date(2026, 12, 10),
        "trip_duration_days": 30, "cabin_class": "BUSINESS", "max_stops": "ANY",
        "airlines": ("SQ",), "currency": "USD", "adults": 1,
    }
    base.update(overrides)
    return GridQuery(**base)


@pytest.fixture
def cache(tmp_path) -> PriceCache:
    return PriceCache(tmp_path / "cache.sqlite3", ttl_seconds=3600)


PRICES = [GridPrice(date(2026, 12, 1), date(2026, 12, 31), 4200.0, "USD")]


class TestKeying:
    def test_same_query_same_key(self):
        assert grid_key(query()) == grid_key(query())

    @pytest.mark.parametrize("field,value", [
        ("cabin_class", "ECONOMY"),
        ("airlines", ("NH",)),
        ("destinations", ("SFO",)),
        ("currency", "EUR"),
        ("adults", 2),
        ("max_stops", "NON_STOP"),
        ("trip_duration_days", 14),
        ("depart_to", date(2026, 12, 20)),
    ])
    def test_every_price_affecting_field_changes_the_key(self, field, value):
        """A field left out of the key would serve one search's prices to another."""
        assert grid_key(query()) != grid_key(query(**{field: value}))


class TestStorage:
    def test_round_trip(self, cache):
        assert cache.get(query()) is None
        cache.put(query(), PRICES)
        assert cache.get(query()) == PRICES

    def test_expiry(self, tmp_path):
        cache = PriceCache(tmp_path / "c.sqlite3", ttl_seconds=0)
        cache.put(query(), PRICES)
        time.sleep(0.01)
        assert cache.get(query()) is None

    def test_survives_reopening(self, tmp_path):
        path = tmp_path / "c.sqlite3"
        first = PriceCache(path, ttl_seconds=3600)
        first.put(query(), PRICES)
        first.close()
        assert PriceCache(path, ttl_seconds=3600).get(query()) == PRICES

    def test_empty_result_is_cached(self, cache):
        """"This airline does not fly here" is an answer worth remembering."""
        cache.put(query(), [])
        assert cache.get(query()) == []

    def test_purge_removes_stale_rows(self, tmp_path):
        cache = PriceCache(tmp_path / "c.sqlite3", ttl_seconds=0)
        cache.put(query(), PRICES)
        assert cache.purge_expired() == 1

    def test_stats(self, cache):
        cache.put(query(), PRICES)
        stats = cache.stats()
        assert stats["rows"] == 1
        assert stats["fresh_rows"] == 1


class TestCachedProvider:
    def test_second_scan_is_served_from_disk(self, cache):
        inner = FakeProvider({"LAX": 4200.0})
        provider = CachedProvider(inner, cache)

        first = provider.scan_date_grid(query())
        second = provider.scan_date_grid(query())

        assert first == second
        assert len(inner.grid_calls) == 1  # only one upstream request
        assert (provider.hits, provider.misses) == (1, 1)

    def test_a_different_search_is_not_served_stale(self, cache):
        inner = FakeProvider({"LAX": 4200.0})
        provider = CachedProvider(inner, cache)
        provider.scan_date_grid(query())
        provider.scan_date_grid(query(cabin_class="ECONOMY"))
        assert len(inner.grid_calls) == 2

    def test_itineraries_are_never_cached(self, cache):
        """The bookable fare must be live, however fresh the grid is."""
        inner = FakeProvider({"LAX": 4200.0})
        provider = CachedProvider(inner, cache)
        itinerary_query = ItineraryQuery(
            origin="SIN", destination="LAX", departure_date=date(2026, 12, 1),
            return_date=None, cabin_class="BUSINESS", max_stops="ANY",
            airlines=(), currency="USD", adults=1,
        )
        provider.search_itineraries(itinerary_query)
        provider.search_itineraries(itinerary_query)
        assert len(inner.itinerary_calls) == 2

    def test_capability_is_not_invented(self, cache):
        class NoGrid(FakeProvider):
            supports_date_grid = False

        assert CachedProvider(NoGrid(), cache).supports_date_grid is False


class TestCacheInASearch:
    def request(self) -> DealSearchRequest:
        return DealSearchRequest(
            origins=["SIN"], destinations=["LAX", "SFO"],
            depart_from=date(2026, 12, 1), depart_to=date(2026, 12, 10),
            min_trip_days=30, max_trip_days=30, cabin_class="BUSINESS",
            refine_top_n=1,
        )

    def test_repeat_search_makes_no_grid_requests(self, cache):
        inner = FakeProvider({"LAX": 4200.0, "SFO": 5000.0})
        provider = CachedProvider(inner, cache)

        first = run_deal_search(self.request(), provider)
        second = run_deal_search(self.request(), provider)

        assert first.best.grid_price == second.best.grid_price
        assert len(inner.grid_calls) == 2  # 2 destinations, fetched once each
        assert first.stats.cache_hits == 0
        assert second.stats.cache_hits == 2

    def test_cached_scans_are_not_counted_as_upstream_requests(self, cache):
        inner = FakeProvider({"LAX": 4200.0, "SFO": 5000.0})
        provider = CachedProvider(inner, cache)

        run_deal_search(self.request(), provider)
        second = run_deal_search(self.request(), provider)

        # Only the refinement is a real request the second time around.
        assert second.stats.requests_made == 1
        assert second.stats.cache_hits == 2
