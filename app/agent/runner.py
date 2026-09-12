"""The agent loop: natural language in, searches run, answer out.

Uses the Anthropic SDK's tool runner, which owns the
request -> execute tool -> feed result -> repeat cycle. Everything this module
adds is observability: each step is turned into an event the browser can
render live, because a wide search takes minutes and a silent spinner for
that long is not acceptable.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Iterator
from datetime import date
from typing import Any

from app.agent.context import set_emitter, set_results_sink
from app.agent.tools import ALL_TOOLS

logger = logging.getLogger(__name__)

MODEL = os.environ.get("FLIGHT_AGENT_MODEL", "claude-opus-5")
MAX_TOKENS = 16000

SYSTEM_PROMPT = """You are a flight search assistant with access to live \
Google Flights data. You turn a traveller's plain-English request into \
concrete searches and report what you find.

Today's date is {today}. Resolve all relative dates against it. When the user \
names a month without a year, choose the next occurrence of that month.

How to work:

1. Resolve places to IATA codes with `find_airports` when the user names a \
city rather than a code. For a country or continent, use `destination_region` \
instead of guessing a list of airports.
2. For any search spanning more than a few dates or destinations, call \
`estimate_search_cost` first. It is free, and it tells you the real departure \
window, which is often narrower than what the user said.
3. Then call `find_best_fares`. It takes minutes — that is expected.
4. For a single known date, use `search_one_date` instead.

Filling in the search:

- A phrase like "30-day trip" sets both min_trip_days and max_trip_days to 30. \
"About a month" or "3 to 4 weeks" is a range — set them differently.
- "Business class" is BUSINESS. "Singapore Airlines" is airline code SQ.
- Default to 1 adult and USD unless the user says otherwise.
- If the user gives a departure month and a return month, pass both windows. \
The planner works out which departure dates can actually satisfy both.

Reporting:

- The user sees the full results table and price calendar in the UI. Do not \
recite every date. Give them the answer: the best fare, which airport, which \
dates, and how it compares to the alternatives.
- Mention a notably cheaper destination or date if one stands out.
- Be concise and concrete. Lead with the number.
- If a search fails or returns nothing, say plainly what happened and suggest \
the most useful next step.

Never invent a fare, a flight number, or a date. Every number you report must \
come from a tool result."""


class AgentUnavailable(RuntimeError):
    """No API credentials, so the agent cannot run."""


def _client():
    """Build an Anthropic client, or explain why we cannot."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AgentUnavailable("the `anthropic` package is not installed") from exc

    # The SDK also accepts an `ant auth login` profile, so an unset env var is
    # not proof there are no credentials — let the SDK decide, and only report
    # a missing key if it actually fails.
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        raise AgentUnavailable(
            "no Anthropic credentials found. Set ANTHROPIC_API_KEY or run `ant auth login`."
        ) from exc


def stream_answer(
    question: str,
    history: list[dict[str, Any]] | None = None,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Run the agent, yielding events as it works.

    Events: `thinking`, `text`, `tool_call`, `tool_result`, `progress`,
    `plan`, `results`, `error`, `done`. The tool runner is synchronous, so it
    runs on a worker thread and pushes events through a queue that this
    generator drains.
    """
    client = client or _client()
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
            messages = list(history or [])
            messages.append({"role": "user", "content": question})

            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT.format(today=date.today().isoformat()),
                # Parsing a travel request and reporting results is not a
                # reasoning-heavy task; the search itself dominates latency.
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
