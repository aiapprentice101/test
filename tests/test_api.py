"""HTTP-level tests for the FastAPI routes."""

from __future__ import annotations

import pytest
from conftest import FakeSearchFlights, make_result
from fastapi.testclient import TestClient
from fli.search import SearchConnectionError

from app import service
from app.main import app

client = TestClient(app)


def payload(**overrides) -> dict:
    body = {"origin": "JFK", "destination": "LHR", "departure_date": "2026-10-15"}
    body.update(overrides)
    return body


@pytest.fixture
def stub_search(monkeypatch):
    """Swap the `fli` searcher the service constructs for a canned one."""

    def _install(results=None, error=None):
        fake = FakeSearchFlights(results=results, error=error)
        monkeypatch.setattr(service, "SearchFlights", lambda: fake)
        return fake

    return _install


class TestHealthAndStatic:
    def test_health(self):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_index_serves_ui(self):
        res = client.get("/")
        assert res.status_code == 200
        assert "Flight Search" in res.text


class TestAirports:
    def test_autocomplete(self):
        res = client.get("/api/airports", params={"q": "san fran"})
        assert res.status_code == 200
        assert "SFO" in [item["code"] for item in res.json()]

    def test_limit_applied(self):
        res = client.get("/api/airports", params={"q": "a", "limit": 3})
        assert len(res.json()) <= 3

    def test_empty_query_rejected(self):
        assert client.get("/api/airports", params={"q": ""}).status_code == 422


class TestSearchEndpoint:
    def test_successful_search(self, stub_search):
        stub_search(results=[make_result()])
        res = client.post("/api/search", json=payload())

        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 1
        assert body["itineraries"][0]["price"] == 512.0
        assert body["itineraries"][0]["slices"][0]["legs"][0]["airline_code"] == "AA"

    def test_same_origin_and_destination_rejected(self):
        res = client.post("/api/search", json=payload(destination="JFK"))
        assert res.status_code == 422

    def test_return_before_departure_rejected(self):
        res = client.post("/api/search", json=payload(return_date="2026-10-01"))
        assert res.status_code == 422

    def test_bad_cabin_class_rejected(self):
        res = client.post("/api/search", json=payload(cabin_class="LUXURY"))
        assert res.status_code == 422

    def test_bad_departure_window_rejected(self):
        res = client.post("/api/search", json=payload(departure_window="20-6"))
        assert res.status_code == 422

    def test_lowercase_codes_are_normalized(self, stub_search):
        stub_search(results=[make_result()])
        res = client.post("/api/search", json=payload(origin="jfk", destination="lhr"))
        assert res.status_code == 200
        assert res.json()["query"]["origin"] == "JFK"

    def test_upstream_failure_returns_structured_error(self, stub_search):
        stub_search(error=SearchConnectionError("Could not reach Google Flights"))
        res = client.post("/api/search", json=payload())

        assert res.status_code == 502
        body = res.json()
        assert body["error"] == "search_failed"
        assert "www.google.com" in body["hint"]

    def test_unknown_airport_code_is_a_client_error(self, stub_search):
        stub_search(results=[])
        res = client.post("/api/search", json=payload(origin="ZZZ"))
        assert res.status_code == 422
        assert "ZZZ" in res.json()["detail"]

    def test_unknown_airline_code_is_a_client_error(self, stub_search):
        stub_search(results=[])
        res = client.post("/api/search", json=payload(airlines=["ZZ9"]))
        assert res.status_code == 422


class TestDatesEndpoint:
    def test_inverted_range_rejected(self):
        res = client.get(
            "/api/dates",
            params={"origin": "JFK", "destination": "LHR", "from": "2026-11-01", "to": "2026-10-01"},
        )
        assert res.status_code == 422
