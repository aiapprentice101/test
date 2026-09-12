"""Tests for the search planner — the logic that keeps wide searches affordable."""

from __future__ import annotations

from datetime import date

import pytest

from app.deals.planner import PlanError, departure_window, plan_search, resolve_durations
from app.deals.schemas import DealSearchRequest


def req(**overrides) -> DealSearchRequest:
    payload = {
        "origins": ["SIN"],
        "destination_region": "US",
        "depart_from": date(2026, 12, 1),
        "depart_to": date(2026, 12, 31),
        "return_from": date(2027, 1, 1),
        "return_to": date(2027, 1, 31),
        "min_trip_days": 30,
        "max_trip_days": 30,
        "cabin_class": "BUSINESS",
        "airlines": ["SQ"],
    }
    payload.update(overrides)
    return DealSearchRequest(**payload)


class TestDepartureWindow:
    def test_return_window_narrows_departures(self):
        """A 30-day trip leaving 1 Dec lands 31 Dec, which is not January."""
        window = departure_window(req(), 30)
        assert window == (date(2026, 12, 2), date(2026, 12, 31))

    def test_no_return_window_leaves_departures_alone(self):
        window = departure_window(req(return_from=None, return_to=None), 30)
        assert window == (date(2026, 12, 1), date(2026, 12, 31))

    def test_impossible_combination_yields_nothing(self):
        assert departure_window(req(), 90) is None


class TestDurations:
    def test_explicit_range(self):
        assert resolve_durations(req(min_trip_days=28, max_trip_days=31)) == [28, 29, 30, 31]

    def test_exact_duration(self):
        assert resolve_durations(req()) == [30]

    def test_one_way_has_no_duration(self):
        one_way = req(return_from=None, return_to=None, min_trip_days=None, max_trip_days=None)
        assert resolve_durations(one_way) == [None]

    def test_derived_from_windows(self):
        derived = req(min_trip_days=None, max_trip_days=None)
        # Dec 1-31 out, Jan 1-31 back => 1 to 61 days, too wide to scan blindly.
        with pytest.raises(PlanError, match="different trip lengths"):
            resolve_durations(derived)

    def test_windows_that_never_overlap(self):
        impossible = req(
            min_trip_days=None,
            max_trip_days=None,
            return_from=date(2026, 11, 1),
            return_to=date(2026, 11, 5),
        )
        with pytest.raises(PlanError, match="do not overlap"):
            resolve_durations(impossible)


class TestPlan:
    def test_example_query_budget(self):
        """12 US gateways x 30 dates is 360 combinations but only 22 requests."""
        plan = plan_search(req())
        assert plan.scan_requests == 12
        assert plan.refine_top_n == 10
        assert plan.estimated_requests == 22
        assert plan.scans[0].depart_from == date(2026, 12, 2)
        assert any("narrowed" in note for note in plan.notes)

    def test_each_destination_scanned_once_per_duration(self):
        plan = plan_search(req(min_trip_days=30, max_trip_days=32, max_requests=200))
        assert len(plan.durations) == 3
        assert plan.scan_requests == 12 * 3

    def test_bundling_collapses_to_one_scan(self):
        plan = plan_search(req(bundle_destinations=True))
        assert plan.scan_requests == 1
        assert len(plan.scans[0].destinations) == 12

    def test_budget_trims_destinations(self):
        plan = plan_search(req(max_requests=15, refine_top_n=5))
        assert plan.estimated_requests <= 15
        assert len(plan.destinations) < 12
        assert any("trimmed destinations" in note for note in plan.notes)

    def test_long_range_is_chunked_contiguously(self):
        """Ranges over 61 days split into back-to-back requests, no gaps or overlap."""
        plan = plan_search(
            req(
                destinations=["LAX"],
                destination_region=None,
                depart_from=date(2026, 12, 1),
                depart_to=date(2027, 3, 31),  # 121 days -> 61 + 60
                return_from=None,
                return_to=None,
                max_requests=100,
            )
        )
        assert plan.scan_requests == 2
        first, second = plan.scans
        assert first.depart_from == date(2026, 12, 1)
        assert second.depart_to == date(2027, 3, 31)
        assert (second.depart_from - first.depart_to).days == 1
        assert all((s.depart_to - s.depart_from).days < 61 for s in plan.scans)

    def test_explicit_destinations_and_region_merge_without_duplicates(self):
        plan = plan_search(req(destinations=["LAX", "NRT"], max_requests=200))
        assert plan.destinations.count("LAX") == 1
        assert "NRT" in plan.destinations

    def test_unsatisfiable_search_is_rejected(self):
        with pytest.raises(PlanError, match="no departure date"):
            plan_search(req(min_trip_days=90, max_trip_days=90))

    def test_one_way_search(self):
        plan = plan_search(
            req(
                return_from=None,
                return_to=None,
                min_trip_days=None,
                max_trip_days=None,
                max_requests=200,
            )
        )
        assert plan.durations == (None,)
        assert plan.scans[0].trip_duration_days is None


class TestRequestValidation:
    def test_destination_required(self):
        with pytest.raises(ValueError, match="destinations or a destination_region"):
            DealSearchRequest(
                origins=["SIN"], depart_from=date(2026, 12, 1), depart_to=date(2026, 12, 31)
            )

    def test_return_window_needs_both_ends(self):
        with pytest.raises(ValueError, match="must be given together"):
            req(return_to=None)

    def test_unknown_region_is_reported(self):
        from app.deals.regions import UnknownRegionError

        with pytest.raises(UnknownRegionError, match="Unknown region"):
            plan_search(req(destination_region="ATLANTIS"))
