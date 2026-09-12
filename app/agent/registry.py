"""Provider-neutral view of the tools.

The tools are declared once with Anthropic's `@beta_tool`, which also keeps
the undecorated function on `.func`. That makes the same declarations usable
by any backend: MCP reads the raw function, and each LLM backend renders the
schema into whatever shape its API wants.

One declaration, three consumers — no parallel tool lists to drift apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.agent.tools import ALL_TOOLS


@dataclass(frozen=True)
class ToolSpec:
    """One tool, described without reference to any provider."""

    name: str
    description: str
    schema: dict[str, Any]
    invoke: Any

    def call_json(self, raw_arguments: str) -> str:
        """Run the tool from a JSON argument string, as tool-calling APIs supply it."""
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return f"Could not parse the arguments as JSON: {exc}"
        return self.call(arguments)

    def call(self, arguments: dict[str, Any]) -> str:
        """Run the tool, turning any failure into text the model can act on."""
        try:
            return str(self.invoke(arguments))
        except Exception as exc:  # noqa: BLE001 - the model should see the error
            return f"The tool failed: {type(exc).__name__}: {exc}"


def tool_specs() -> list[ToolSpec]:
    """Every tool, in a provider-neutral form."""
    return [
        ToolSpec(
            name=tool.name,
            # `.description` is the docstring the model is meant to read.
            description=(tool.description or tool.func.__doc__ or "").strip(),
            schema=tool.input_schema,
            invoke=tool.call,
        )
        for tool in ALL_TOOLS
    ]


def by_name() -> dict[str, ToolSpec]:
    """Tools keyed by name, for dispatching a tool call."""
    return {spec.name: spec for spec in tool_specs()}
