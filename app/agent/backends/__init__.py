"""Model backends. One tool set, one prompt, several providers."""

from __future__ import annotations

import os

from app.agent.backends.base import AgentUnavailable, Backend

BACKENDS = ("anthropic", "openai")

DEFAULT_BACKEND = "anthropic"


def get_backend(name: str | None = None) -> Backend:
    """Build the named backend, defaulting to `FLIGHT_AGENT_BACKEND`."""
    chosen = (name or os.environ.get("FLIGHT_AGENT_BACKEND") or DEFAULT_BACKEND).strip().lower()

    if chosen == "anthropic":
        from app.agent.backends.anthropic_backend import AnthropicBackend

        return AnthropicBackend()
    if chosen in ("openai", "codex"):
        from app.agent.backends.openai_backend import OpenAIBackend

        return OpenAIBackend()

    raise AgentUnavailable(
        f"unknown agent backend {chosen!r}. Set FLIGHT_AGENT_BACKEND to one of: "
        + ", ".join(BACKENDS)
    )


__all__ = ["AgentUnavailable", "Backend", "BACKENDS", "get_backend"]
