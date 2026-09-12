"""Runs a backend and turns its progress into a stream of events.

Everything provider-specific lives in `app/agent/backends/`. This module owns
only the plumbing that is the same either way: run the loop off the request
thread, and surface thinking, tool calls, progress and results as they happen
— because a wide search takes minutes and a silent spinner for that long is
not acceptable.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from typing import Any

from app.agent.backends import AgentUnavailable, get_backend
from app.agent.context import set_emitter, set_results_sink

logger = logging.getLogger(__name__)

__all__ = ["AgentUnavailable", "stream_answer", "describe_backend"]


def describe_backend(name: str | None = None) -> dict[str, Any]:
    """Report whether the selected backend can run, for the UI's status line."""
    try:
        backend = get_backend(name)
    except AgentUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    try:
        backend.check()
    except AgentUnavailable as exc:
        return {
            "available": False,
            "backend": backend.name,
            "model": backend.model,
            "reason": str(exc),
        }
    return {"available": True, "backend": backend.name, "model": backend.model}


def stream_answer(
    question: str,
    history: list[dict[str, Any]] | None = None,
    client: Any = None,
    backend: Any = None,
) -> Iterator[dict[str, Any]]:
    """Run the agent, yielding events as it works.

    Events: `thinking`, `text`, `tool_call`, `progress`, `plan`, `results`,
    `error`, `done`. Backends are synchronous, so the loop runs on a worker
    thread and pushes events through a queue that this generator drains.

    Raises:
        AgentUnavailable: if the backend cannot run at all. Because this is a
            generator, that surfaces on the first iteration, not at the call —
            callers must guard the loop, not just the call.
    """
    impl = backend if backend is not None else get_backend()
    if client is None:
        impl.check()

    events: queue.Queue = queue.Queue()
    collected: list[dict[str, Any]] = []

    def emit(event: str, data: dict[str, Any]) -> None:
        events.put({"type": event, **data})

    def work() -> None:
        # Context variables set here belong to this thread's context and die
        # with it, so there is nothing to unwind.
        set_emitter(emit)
        set_results_sink(collected)
        try:
            impl.run(question, list(history or []), emit, client)
        except AgentUnavailable as exc:
            emit("error", {"message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
            logger.exception("agent run failed")
            emit("error", {"message": str(exc)})
        finally:
            for item in collected:
                emit("results", {"kind": item["kind"], "data": item["payload"]})
            emit("done", {})

    thread = threading.Thread(target=work, name="flight-agent", daemon=True)
    thread.start()

    while True:
        event = events.get()
        yield event
        if event["type"] == "done":
            break
