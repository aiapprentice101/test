"""What a model backend must provide.

A backend owns one thing: driving a tool-use loop for its provider's API and
reporting what happened through `emit`. Everything else — the tools, the
system prompt, threading, the SSE plumbing — is shared.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

Emit = Callable[[str, dict[str, Any]], None]

# A loop should never run away on a model that keeps calling tools.
MAX_ITERATIONS = 12


class AgentUnavailable(RuntimeError):
    """The backend cannot run — missing package, credentials, or configuration."""


class Backend(Protocol):
    """One model provider's tool-use loop."""

    name: str
    model: str

    def check(self) -> None:
        """Raise `AgentUnavailable` if this backend cannot run right now."""
        ...

    def run(
        self,
        question: str,
        history: list[dict[str, Any]],
        emit: Emit,
        client: Any = None,
    ) -> None:
        """Answer the question, emitting events as the loop proceeds."""
        ...
