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
        assert "Flight Agent" in res.text


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


class TestDealEndpoints:
    """The agent-facing wide-search API."""

    def deal_payload(self, **overrides) -> dict:
        body = {
            "origins": ["SIN"],
            "destination_region": "US",
            "depart_from": "2026-12-01",
            "depart_to": "2026-12-31",
            "return_from": "2027-01-01",
            "return_to": "2027-01-31",
            "min_trip_days": 30,
            "max_trip_days": 30,
            "cabin_class": "BUSINESS",
            "airlines": ["SQ"],
        }
        body.update(overrides)
        return body

    def test_plan_costs_a_search_without_running_it(self):
        res = client.post("/api/deals/plan", json=self.deal_payload())
        assert res.status_code == 200
        body = res.json()
        assert body["estimated_requests"] == 22
        assert body["scan_requests"] == 12
        assert body["departure_windows"][0]["from"] == "2026-12-02"

    def test_regions_are_discoverable(self):
        res = client.get("/api/regions")
        assert res.status_code == 200
        assert "US" in res.json()

    def test_unknown_region_lists_valid_ones(self):
        res = client.post("/api/deals/plan", json=self.deal_payload(destination_region="MARS"))
        assert res.status_code == 422
        assert res.json()["error"] == "unknown_region"

    def test_impossible_dates_are_rejected_with_a_reason(self):
        res = client.post(
            "/api/deals/plan", json=self.deal_payload(min_trip_days=90, max_trip_days=90)
        )
        assert res.status_code == 422
        assert res.json()["error"] == "invalid_plan"
        assert "no departure date" in res.json()["detail"]

    def test_missing_job_is_404(self):
        assert client.get("/api/deals/nope").status_code == 404


class TestDealJobLifecycle:
    """Submit -> poll -> result, the path an agent actually walks."""

    def test_job_runs_to_completion(self, monkeypatch):
        import time

        from test_engine import FakeProvider

        from app import main

        monkeypatch.setattr(
            main, "provider", FakeProvider({"LAX": 4200.0, "SFO": 5000.0, "JFK": 6000.0})
        )

        res = client.post(
            "/api/deals",
            json={
                "origins": ["SIN"],
                "destinations": ["LAX", "SFO", "JFK"],
                "depart_from": "2026-12-01",
                "depart_to": "2026-12-10",
                "min_trip_days": 30,
                "max_trip_days": 30,
                "cabin_class": "BUSINESS",
                "refine_top_n": 2,
            },
        )
        assert res.status_code == 202
        job = res.json()
        # A fast provider can finish before the POST returns, so "complete"
        # is a legitimate first observation.
        assert job["status"] in ("queued", "running", "complete")
        assert job["estimated_requests"] == 5

        deadline = time.time() + 10
        while time.time() < deadline:
            job = client.get(f"/api/deals/{job['id']}").json()
            if job["status"] in ("complete", "failed"):
                break
            time.sleep(0.05)

        assert job["status"] == "complete", job.get("error")
        result = job["result"]
        assert result["best"]["destination"] == "LAX"
        assert result["best"]["grid_price"] == 4200.0
        assert result["stats"]["requests_made"] == 5
        assert len(result["deals"]) == 2

    def test_job_appears_in_the_listing(self, monkeypatch):
        from test_engine import FakeProvider

        from app import main

        monkeypatch.setattr(main, "provider", FakeProvider())
        created = client.post(
            "/api/deals",
            json={
                "origins": ["SIN"],
                "destinations": ["LAX"],
                "depart_from": "2026-12-01",
                "depart_to": "2026-12-05",
            },
        ).json()
        assert created["id"] in [j["id"] for j in client.get("/api/deals").json()]

    def test_unsatisfiable_search_fails_fast_without_a_job(self):
        res = client.post(
            "/api/deals",
            json={
                "origins": ["SIN"],
                "destinations": ["LAX"],
                "depart_from": "2026-12-01",
                "depart_to": "2026-12-31",
                "return_from": "2027-01-01",
                "return_to": "2027-01-31",
                "min_trip_days": 90,
                "max_trip_days": 90,
            },
        )
        assert res.status_code == 422
        assert res.json()["error"] == "invalid_plan"


class TestAskEndpoint:
    """The natural-language endpoint the UI talks to."""

    def test_streams_sse_events(self, monkeypatch):
        from test_agent import Block, Message, StubClient

        from app.agent import runner as runner_mod

        client_stub = StubClient([
            Block and Message([Block(type="text", text="Best fare is $4,090 to SFO.")]),
        ])
        monkeypatch.setattr(runner_mod, "_client", lambda: client_stub)

        with client.stream("POST", "/api/ask", json={"question": "cheapest to SFO?"}) as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            body = "".join(res.iter_text())

        assert '"type": "text"' in body
        assert "Best fare is $4,090 to SFO." in body
        assert body.rstrip().endswith('data: {"type": "done"}')

    def test_missing_credentials_are_reported_not_raised(self, monkeypatch):
        from app.agent import runner as runner_mod
        from app.agent.runner import AgentUnavailable

        def no_creds():
            raise AgentUnavailable("no Anthropic credentials found")

        monkeypatch.setattr(runner_mod, "_client", no_creds)

        with client.stream("POST", "/api/ask", json={"question": "hi"}) as res:
            body = "".join(res.iter_text())

        assert "no Anthropic credentials found" in body
        assert '"type": "error"' in body

    def test_empty_question_rejected(self):
        assert client.post("/api/ask", json={"question": ""}).status_code == 422

    def test_agent_status_reports_unavailable(self, monkeypatch):
        from app.agent import runner as runner_mod
        from app.agent.runner import AgentUnavailable

        monkeypatch.setattr(
            runner_mod, "_client", lambda: (_ for _ in ()).throw(AgentUnavailable("no key"))
        )
        body = client.get("/api/agent/status").json()
        assert body["available"] is False
        assert "no key" in body["reason"]
