"""Tests for the agent layer: tools, event streaming, and the SSE endpoint.

No network and no model calls — the Anthropic client and the flight provider
are both stubbed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import make_result
from test_engine import FakeProvider

from app.agent import tools as agent_tools
from app.agent.context import set_emitter, set_results_sink
from app.agent.runner import stream_answer
from app.providers.base import GridPrice
from app.service import SearchError


@pytest.fixture
def stub_provider(monkeypatch):
    """Point the tools at a fake flight provider."""
    provider = FakeProvider({"LAX": 4200.0, "SFO": 5000.0})
    monkeypatch.setattr(agent_tools, "FliProvider", lambda: provider)
    return provider


@pytest.fixture
def captured():
    """Collect emitted events and recorded results for one tool call."""
    events: list[tuple[str, dict]] = []
    results: list[dict] = []
    set_emitter(lambda name, data: events.append((name, data)))
    set_results_sink(results)
    return {"events": events, "results": results}


class TestSimpleTools:
    def test_find_airports(self):
        out = agent_tools.find_airports.call({"query": "singapore"})
        assert "SIN" in out

    def test_find_airports_no_match(self):
        assert "No airports matched" in agent_tools.find_airports.call({"query": "zzzz"})

    def test_list_regions(self):
        assert "US" in agent_tools.list_destination_regions.call({})


class TestEstimateSearchCost:
    def test_reports_budget_and_narrowed_window(self, captured):
        out = agent_tools.estimate_search_cost.call({
            "origins": "SIN", "destination_region": "US",
            "depart_from": "2026-12-01", "depart_to": "2026-12-31",
            "return_from": "2027-01-01", "return_to": "2027-01-31",
            "min_trip_days": 30, "max_trip_days": 30,
            "cabin_class": "BUSINESS", "airlines": "SQ",
        })
        assert "~22 upstream requests" in out
        assert "2026-12-02" in out  # Dec 1 cannot return in January
        assert ("plan", {"estimated_requests": 22, "destinations": [
            "JFK", "EWR", "LAX", "SFO", "SEA", "ORD", "IAD", "BOS", "IAH", "ATL", "DFW", "MIA",
        ]}) in captured["events"]

    def test_impossible_search_explains_itself(self):
        out = agent_tools.estimate_search_cost.call({
            "origins": "SIN", "destinations": "LAX",
            "depart_from": "2026-12-01", "depart_to": "2026-12-31",
            "return_from": "2027-01-01", "return_to": "2027-01-31",
            "min_trip_days": 90, "max_trip_days": 90,
        })
        assert "cannot run as specified" in out
        assert "no departure date" in out

    def test_bad_date_is_reported_not_raised(self):
        with pytest.raises(ValueError, match="must be YYYY-MM-DD"):
            agent_tools.estimate_search_cost.call({
                "origins": "SIN", "destinations": "LAX", "depart_from": "next tuesday",
                "depart_to": "2026-12-31",
            })


class TestFindBestFares:
    def args(self, **overrides) -> dict:
        base = {
            "origins": "SIN", "destinations": "LAX,SFO",
            "depart_from": "2026-12-01", "depart_to": "2026-12-10",
            "min_trip_days": 30, "max_trip_days": 30,
            "cabin_class": "BUSINESS", "airlines": "SQ", "refine_top_n": 2,
        }
        base.update(overrides)
        return base

    def test_returns_compact_summary(self, stub_provider, captured):
        out = agent_tools.find_best_fares.call(self.args())
        assert "Best fare" in out
        assert "LAX" in out
        assert "1. SIN-LAX" in out
        assert "Cheapest per destination" in out

    def test_full_result_goes_to_the_ui_not_the_model(self, stub_provider, captured):
        out = agent_tools.find_best_fares.call(self.args())
        assert len(captured["results"]) == 1
        payload = captured["results"][0]["payload"]
        assert captured["results"][0]["kind"] == "deal_search"
        # The grid has far more rows than the model is shown.
        assert len(payload["calendar"]) > out.count("\n")

    def test_progress_is_emitted(self, stub_provider, captured):
        agent_tools.find_best_fares.call(self.args())
        stages = {data["stage"] for name, data in captured["events"] if name == "progress"}
        assert stages == {"scan", "refine"}

    def test_search_failure_is_returned_as_text(self, monkeypatch, captured):
        class Broken(FakeProvider):
            def scan_date_grid(self, query):
                raise SearchError("Could not reach Google Flights", hint="check the network")

        monkeypatch.setattr(agent_tools, "FliProvider", lambda: Broken())
        out = agent_tools.find_best_fares.call(self.args())
        # A per-route failure degrades to "no fares", carrying the reason.
        assert "No fares found" in out
        assert "Could not reach Google Flights" in out

    def test_impossible_plan_is_returned_as_text(self, stub_provider):
        out = agent_tools.find_best_fares.call(
            self.args(return_from="2027-01-01", return_to="2027-01-31",
                      min_trip_days=200, max_trip_days=200)
        )
        assert "cannot run as specified" in out


class TestSearchOneDate:
    def test_returns_itineraries(self, monkeypatch, captured):
        from app.schemas import SearchResponse

        def fake_search(request):
            from app.service import _to_itinerary

            return SearchResponse(
                query=request, count=1, cheapest_price=512.0, currency="USD",
                elapsed_seconds=0.1, google_flights_url="https://example.com",
                itineraries=[_to_itinerary(make_result(), request)],
            )

        monkeypatch.setattr(agent_tools, "search_flights", fake_search)
        out = agent_tools.search_one_date.call({
            "origin": "JFK", "destination": "LHR", "departure_date": "2026-10-15",
        })
        assert "cheapest" in out
        assert "AA100" in out
        assert captured["results"][0]["kind"] == "point_search"

    def test_invalid_route_is_returned_as_text(self):
        out = agent_tools.search_one_date.call({
            "origin": "JFK", "destination": "JFK", "departure_date": "2026-10-15",
        })
        assert "Invalid search" in out


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class StubClient:
    """Mimics the SDK tool runner closely enough to drive the event stream."""

    def __init__(self, messages, on_run=None):
        self._messages = messages
        self._on_run = on_run
        self.kwargs = None
        outer = self

        class Runner:
            def __init__(self, **kwargs):
                outer.kwargs = kwargs

            def __iter__(self):
                for message in outer._messages:
                    if outer._on_run:
                        outer._on_run()
                    yield message

        class Messages:
            def tool_runner(self, **kwargs):
                return Runner(**kwargs)

        class Beta:
            messages = Messages()

        self.beta = Beta()


class TestStreamAnswer:
    def test_emits_thinking_text_and_done(self):
        client = StubClient([
            Message([Block(type="thinking", thinking="Singapore is SIN.")]),
            Message([Block(type="text", text="Best fare is $4,090 to SFO.")]),
        ])
        events = list(stream_answer("cheapest to the US?", client=client))
        kinds = [e["type"] for e in events]

        assert kinds == ["thinking", "text", "done"]
        assert events[0]["text"] == "Singapore is SIN."
        assert events[1]["text"].startswith("Best fare")

    def test_tool_calls_are_reported(self):
        client = StubClient([
            Message([Block(type="tool_use", name="find_best_fares", input={"origins": "SIN"})]),
            Message([Block(type="text", text="done")]),
        ])
        events = list(stream_answer("q", client=client))
        tool_calls = [e for e in events if e["type"] == "tool_call"]

        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "find_best_fares"
        assert tool_calls[0]["input"] == {"origins": "SIN"}

    def test_model_and_tools_are_configured(self):
        client = StubClient([Message([Block(type="text", text="hi")])])
        list(stream_answer("q", client=client))

        assert client.kwargs["model"] == "claude-opus-5"
        assert "today" not in client.kwargs["system"]  # the date was substituted
        assert len(client.kwargs["tools"]) == 5
        assert client.kwargs["messages"][-1] == {"role": "user", "content": "q"}

    def test_history_is_passed_through(self):
        client = StubClient([Message([Block(type="text", text="hi")])])
        history = [{"role": "user", "content": "earlier"},
                   {"role": "assistant", "content": "reply"}]
        list(stream_answer("follow up", history=history, client=client))

        assert client.kwargs["messages"][:2] == history

    def test_model_failure_becomes_an_error_event(self):
        def explode():
            raise RuntimeError("rate limited")

        client = StubClient([Message([Block(type="text", text="hi")])], on_run=explode)
        events = list(stream_answer("q", client=client))

        assert events[0]["type"] == "error"
        assert "rate limited" in events[0]["message"]
        assert events[-1]["type"] == "done"

    def test_refusal_is_surfaced(self):
        client = StubClient([Message([], stop_reason="refusal")])
        events = list(stream_answer("q", client=client))
        assert any(e["type"] == "error" for e in events)

    def test_stream_always_terminates_with_done(self):
        client = StubClient([])
        assert [e["type"] for e in stream_answer("q", client=client)] == ["done"]

    def test_tool_results_reach_the_ui_as_a_results_event(self, stub_provider):
        """A tool's full payload is emitted after the run, for the browser."""
        def run_tool():
            agent_tools.find_best_fares.call({
                "origins": "SIN", "destinations": "LAX",
                "depart_from": "2026-12-01", "depart_to": "2026-12-05",
                "min_trip_days": 30, "max_trip_days": 30, "refine_top_n": 1,
            })

        client = StubClient([Message([Block(type="text", text="done")])], on_run=run_tool)
        events = list(stream_answer("q", client=client))
        results = [e for e in events if e["type"] == "results"]

        assert len(results) == 1
        assert results[0]["kind"] == "deal_search"
        assert results[0]["data"]["best"]["destination"] == "LAX"
