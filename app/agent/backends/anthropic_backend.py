"""Claude backend, using the Anthropic SDK's tool runner.

The runner owns the request -> execute tool -> feed result -> repeat cycle, so
this module only translates its output into events.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.agent.backends.base import AgentUnavailable, Emit
from app.agent.prompt import system_prompt
from app.agent.tools import ALL_TOOLS

logger = logging.getLogger(__name__)


def _has_credentials(client) -> bool:
    """Whether the client has anything to authenticate with.

    The SDK constructs happily with no credentials at all — `api_key` is just
    None and the request fails later — so a successful constructor is not
    proof of anything. An unset env var is not proof of the opposite either:
    credentials may come from an `ant auth login` profile that the SDK
    resolves for itself, so a profile on disk counts as available.
    """
    if client.api_key or client.auth_token or client.auth_headers:
        return True
    profile_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    return profile_dir.is_dir() and any(profile_dir.iterdir())


class AnthropicBackend:
    """Claude with native tool use."""

    name = "anthropic"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("FLIGHT_AGENT_MODEL", "claude-opus-5")

    def client(self):
        """Build an Anthropic client, or say why we cannot."""
        try:
            import anthropic
        except ImportError as exc:
            raise AgentUnavailable("the `anthropic` package is not installed") from exc
        try:
            client = anthropic.Anthropic()
        except Exception as exc:  # noqa: BLE001
            raise AgentUnavailable(f"could not create an Anthropic client: {exc}") from exc

        if not _has_credentials(client):
            raise AgentUnavailable(
                "no Anthropic credentials found. Set ANTHROPIC_API_KEY or run `ant auth login`."
            )
        return client

    def check(self) -> None:
        """Confirm the backend can run."""
        self.client()

    def run(self, question: str, history, emit: Emit, client: Any = None) -> None:
        """Drive Claude's tool-use loop, emitting events as it goes."""
        client = client or self.client()
        messages = list(history or [])
        messages.append({"role": "user", "content": question})

        runner = client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=system_prompt(),
            # Parsing a travel request and reporting results is not
            # reasoning-heavy; the search itself dominates latency.
            output_config={"effort": "medium"},
            thinking={"type": "adaptive", "display": "summarized"},
            tools=ALL_TOOLS,
            messages=messages,
        )

        for message in runner:
            for block in message.content:
                if block.type == "thinking" and getattr(block, "thinking", ""):
                    emit("thinking", {"text": block.thinking})
                elif block.type == "text" and block.text.strip():
                    emit("text", {"text": block.text})
                elif block.type == "tool_use":
                    emit("tool_call", {"name": block.name, "input": block.input})
            if message.stop_reason == "refusal":
                emit("error", {"message": "The request was declined."})
