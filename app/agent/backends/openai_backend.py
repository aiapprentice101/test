"""OpenAI backend: a hand-written tool-use loop against the OpenAI SDK.

There is no SDK-supplied tool runner here, so the loop is explicit: ask, run
whatever tools come back, feed the results in, repeat until the model answers
without calling a tool.

Model choice is deliberately left to configuration. Set `FLIGHT_AGENT_MODEL`
to a model your account can actually use — the default below is a starting
guess, not a verified identifier, and a wrong one surfaces as a clear error
rather than a mystery.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.agent.backends.base import MAX_ITERATIONS, AgentUnavailable, Emit
from app.agent.prompt import system_prompt
from app.agent.registry import by_name, tool_specs

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-codex"


def to_openai_tools() -> list[dict[str, Any]]:
    """Render the shared tool specs in OpenAI's function-calling shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.schema,
            },
        }
        for spec in tool_specs()
    ]


class OpenAIBackend:
    """OpenAI models (including the Codex family) with function calling."""

    name = "openai"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("FLIGHT_AGENT_MODEL") or DEFAULT_MODEL

    def client(self):
        """Build an OpenAI client, or say why we cannot."""
        try:
            import openai
        except ImportError as exc:
            raise AgentUnavailable(
                "the `openai` package is not installed: pip install openai"
            ) from exc
        if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL")):
            raise AgentUnavailable(
                "no OpenAI credentials found. Set OPENAI_API_KEY "
                "(or OPENAI_BASE_URL for a local or proxied endpoint)."
            )
        try:
            return openai.OpenAI()
        except Exception as exc:  # noqa: BLE001
            raise AgentUnavailable(f"could not create an OpenAI client: {exc}") from exc

    def check(self) -> None:
        """Confirm the backend can run."""
        self.client()

    def run(self, question: str, history, emit: Emit, client: Any = None) -> None:
        """Drive the tool-use loop by hand, emitting events as it goes."""
        client = client or self.client()
        tools = to_openai_tools()
        registry = by_name()

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt()}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": question})

        for _ in range(MAX_ITERATIONS):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the browser
                emit("error", {"message": _explain(exc, self.model)})
                return

            message = response.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])

            if getattr(message, "content", None):
                emit("text", {"text": message.content})

            if not tool_calls:
                return

            # Echo the assistant turn back verbatim, including the tool calls,
            # or the follow-up tool results have nothing to attach to.
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                name = call.function.name
                raw_arguments = call.function.arguments or "{}"
                try:
                    parsed = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    parsed = {"_raw": raw_arguments}
                emit("tool_call", {"name": name, "input": parsed})

                spec = registry.get(name)
                if spec is None:
                    result = f"No such tool: {name}"
                else:
                    result = spec.call_json(raw_arguments)

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": name, "content": result}
                )

        emit(
            "error",
            {"message": f"Stopped after {MAX_ITERATIONS} tool rounds without a final answer."},
        )


def _explain(exc: Exception, model: str) -> str:
    """Turn an SDK exception into something actionable."""
    text = str(exc)
    status = getattr(exc, "status_code", None)
    if status == 404 or "model" in text.lower() and "not" in text.lower():
        return (
            f"The model {model!r} was rejected by the API. Set FLIGHT_AGENT_MODEL "
            "to a model your account can use."
        )
    if status == 401:
        return "OpenAI rejected the credentials. Check OPENAI_API_KEY."
    if status == 429:
        return "OpenAI rate-limited the request. Try again shortly."
    return f"The model call failed: {type(exc).__name__}: {text}"
