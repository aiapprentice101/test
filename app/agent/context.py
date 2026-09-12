"""Per-run context so tools can stream progress back to the browser.

The tool functions are called by the SDK's tool runner, which has no idea a
UI exists. A context variable lets a running tool emit progress events into
whichever session invoked it, without threading an emitter through every
signature.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any

Emitter = Callable[[str, dict[str, Any]], None]


def _discard(event: str, data: dict[str, Any]) -> None:
    """Default sink for tools called outside a session (tests, MCP)."""


_emitter: contextvars.ContextVar[Emitter] = contextvars.ContextVar("emitter", default=_discard)
_results: contextvars.ContextVar[list] = contextvars.ContextVar("results", default=[])


def set_emitter(emitter: Emitter) -> contextvars.Token:
    """Route tool events to this emitter for the current run."""
    return _emitter.set(emitter)


def emit(event: str, **data: Any) -> None:
    """Send one event to the caller's stream."""
    _emitter.get()(event, data)


def set_results_sink(sink: list) -> contextvars.Token:
    """Collect full structured results for the UI, separate from tool output."""
    return _results.set(sink)


def record_result(kind: str, payload: Any) -> None:
    """Stash a full result the model does not need to see verbatim."""
    _results.get().append({"kind": kind, "payload": payload})
