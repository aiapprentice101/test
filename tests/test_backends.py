"""Tests for backend selection and the OpenAI tool-use loop.

No network: the OpenAI client is stubbed, so the loop's mechanics are tested
without credentials or a live model.
"""

from __future__ import annotations

import json

import pytest

from app.agent.backends import AgentUnavailable, get_backend
from app.agent.backends.anthropic_backend import AnthropicBackend
from app.agent.backends.base import MAX_ITERATIONS
from app.agent.backends.openai_backend import OpenAIBackend, to_openai_tools
from app.agent.registry import by_name, tool_specs
from app.agent.runner import describe_backend, stream_answer


class TestSelection:
    def test_defaults_to_anthropic(self, monkeypatch):
        monkeypatch.delenv("FLIGHT_AGENT_BACKEND", raising=False)
        assert isinstance(get_backend(), AnthropicBackend)

    def test_env_var_selects_openai(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_AGENT_BACKEND", "openai")
        assert isinstance(get_backend(), OpenAIBackend)

    def test_codex_is_an_alias_for_openai(self):
        assert isinstance(get_backend("codex"), OpenAIBackend)

    def test_unknown_backend_lists_the_valid_ones(self):
        with pytest.raises(AgentUnavailable, match="anthropic, openai"):
            get_backend("llama")

    def test_model_is_configurable(self, monkeypatch):
        monkeypatch.setenv("FLIGHT_AGENT_MODEL", "my-model")
        assert get_backend("openai").model == "my-model"

    def test_missing_openai_key_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        status = describe_backend("openai")
        assert status["available"] is False
        assert "OPENAI_API_KEY" in status["reason"]
        assert status["backend"] == "openai"


class TestToolRendering:
    def test_every_tool_is_rendered(self):
        assert len(to_openai_tools()) == len(tool_specs()) == 5

    def test_openai_function_shape(self):
        spec = next(t for t in to_openai_tools() if t["function"]["name"] == "find_best_fares")
        assert spec["type"] == "function"
        assert spec["function"]["description"].startswith("Search many dates")
        assert "origins" in spec["function"]["parameters"]["properties"]

    def test_schemas_are_shared_with_the_anthropic_declaration(self):
        from app.agent.tools import ALL_TOOLS

        rendered = {t["function"]["name"]: t["function"]["parameters"] for t in to_openai_tools()}
        for tool in ALL_TOOLS:
            assert rendered[tool.name] == tool.input_schema


class TestRegistry:
    def test_call_json_parses_arguments(self):
        spec = by_name()["find_airports"]
        assert "SIN" in spec.call_json('{"query": "singapore"}')

    def test_malformed_json_is_reported_to_the_model(self):
        spec = by_name()["find_airports"]
        assert "Could not parse" in spec.call_json("{not json")

    def test_tool_exception_becomes_text(self):
        spec = by_name()["estimate_search_cost"]
        out = spec.call_json('{"origins": "SIN", "destinations": "LAX", '
                             '"depart_from": "not-a-date", "depart_to": "2026-12-31"}')
        assert "The tool failed" in out


# --------------------------------------------------------------------------
# The OpenAI loop
# --------------------------------------------------------------------------


class Call:
    def __init__(self, name, arguments, call_id="call_1"):
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class Reply:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class StubOpenAI:
    """Returns scripted replies and records the requests it was sent."""

    def __init__(self, replies, error=None):
        self.replies = list(replies)
        self.error = error
        self.requests = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                if outer.error:
                    raise outer.error
                reply = outer.replies.pop(0)
                return type("R", (), {"choices": [type("C", (), {"message": reply})()]})()

        class Chat:
            completions = Completions()

        self.chat = Chat()


def run(client, question="cheapest to Tokyo?"):
    events = []
    OpenAIBackend(model="test-model").run(
        question, [], lambda name, data: events.append({"type": name, **data}), client
    )
    return events


class TestOpenAILoop:
    def test_plain_answer_without_tools(self):
        events = run(StubOpenAI([Reply(content="Fares start at $900.")]))
        assert events == [{"type": "text", "text": "Fares start at $900."}]

    def test_tool_call_is_executed_and_fed_back(self):
        client = StubOpenAI([
            Reply(tool_calls=[Call("find_airports", '{"query": "singapore"}')]),
            Reply(content="Singapore is SIN."),
        ])
        events = run(client)

        assert events[0]["type"] == "tool_call"
        assert events[0]["name"] == "find_airports"
        assert events[0]["input"] == {"query": "singapore"}
        assert events[-1]["text"] == "Singapore is SIN."

        # Second request carries the assistant turn plus the tool result.
        second = client.requests[1]["messages"]
        assert second[-2]["role"] == "assistant"
        assert second[-2]["tool_calls"][0]["function"]["name"] == "find_airports"
        assert second[-1]["role"] == "tool"
        assert second[-1]["tool_call_id"] == "call_1"
        assert "SIN" in second[-1]["content"]

    def test_parallel_tool_calls_all_run(self):
        client = StubOpenAI([
            Reply(tool_calls=[
                Call("find_airports", '{"query": "tokyo"}', "a"),
                Call("list_destination_regions", "{}", "b"),
            ]),
            Reply(content="done"),
        ])
        events = run(client)

        assert [e["name"] for e in events if e["type"] == "tool_call"] == [
            "find_airports", "list_destination_regions",
        ]
        tool_messages = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["a", "b"]

    def test_unknown_tool_is_reported_to_the_model(self):
        client = StubOpenAI([
            Reply(tool_calls=[Call("teleport", "{}")]),
            Reply(content="sorry"),
        ])
        run(client)
        assert "No such tool" in client.requests[1]["messages"][-1]["content"]

    def test_system_prompt_and_tools_are_sent(self):
        client = StubOpenAI([Reply(content="hi")])
        run(client)
        request = client.requests[0]

        assert request["model"] == "test-model"
        assert request["messages"][0]["role"] == "system"
        assert "flight search assistant" in request["messages"][0]["content"]
        assert len(request["tools"]) == 5
        assert request["messages"][-1] == {"role": "user", "content": "cheapest to Tokyo?"}

    def test_runaway_loop_is_capped(self):
        client = StubOpenAI([
            Reply(tool_calls=[Call("list_destination_regions", "{}")])
            for _ in range(MAX_ITERATIONS + 2)
        ])
        events = run(client)
        assert events[-1]["type"] == "error"
        assert "Stopped after" in events[-1]["message"]
        assert len(client.requests) == MAX_ITERATIONS

    def test_bad_model_error_is_actionable(self):
        error = Exception("model not found")
        error.status_code = 404
        events = run(StubOpenAI([], error=error))
        assert events[0]["type"] == "error"
        assert "FLIGHT_AGENT_MODEL" in events[0]["message"]

    def test_auth_error_is_actionable(self):
        error = Exception("unauthorized")
        error.status_code = 401
        events = run(StubOpenAI([], error=error))
        assert "OPENAI_API_KEY" in events[0]["message"]

    def test_rate_limit_error_is_actionable(self):
        error = Exception("slow down")
        error.status_code = 429
        events = run(StubOpenAI([], error=error))
        assert "rate-limited" in events[0]["message"]


class TestStreamThroughOpenAIBackend:
    def test_events_reach_the_stream(self):
        client = StubOpenAI([
            Reply(tool_calls=[Call("find_airports", '{"query": "tokyo"}')]),
            Reply(content="Tokyo is NRT or HND."),
        ])
        events = list(
            stream_answer("tokyo airports?", client=client, backend=OpenAIBackend(model="m"))
        )
        kinds = [e["type"] for e in events]

        assert kinds == ["tool_call", "text", "done"]
        assert events[1]["text"] == "Tokyo is NRT or HND."


class TestAnthropicCredentialDetection:
    """The SDK constructs with no credentials, so a client object proves nothing."""

    def test_no_credentials_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no ant profile

        status = describe_backend("anthropic")
        assert status["available"] is False
        assert "ANTHROPIC_API_KEY" in status["reason"]

    def test_api_key_makes_it_available(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert describe_backend("anthropic")["available"] is True

    def test_an_ant_profile_counts_as_credentials(self, monkeypatch, tmp_path):
        """A profile from `ant auth login` sets no env var but is still valid."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        profile = tmp_path / "anthropic"
        profile.mkdir()
        (profile / "profiles.json").write_text("{}")

        assert describe_backend("anthropic")["available"] is True
