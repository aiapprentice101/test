"""MCP server exposing the same tools the in-app agent uses.

Lets Claude Desktop, Claude Code, or any other MCP client drive this search
directly. The tool bodies are shared with `app/agent/tools.py` — the
`@beta_tool` decorator keeps the original function on `.func`, so there is
one implementation and one set of descriptions, not two that drift.

    python -m app.mcp_server          # stdio
    python -m app.mcp_server --http   # streamable HTTP on :8765
"""

from __future__ import annotations

import sys

from app.agent.tools import ALL_TOOLS
from app.providers.fli_provider import configure_rate_limit


def build_server():
    """Register every agent tool with an MCP server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - optional extra
        raise SystemExit(
            "MCP support needs the `mcp` package: pip install 'mcp[cli]'"
        ) from exc

    server = FastMCP("flight-search")
    for tool in ALL_TOOLS:
        # `.func` is the undecorated function, so MCP derives its own schema
        # from the same signature and docstring the Claude tool uses.
        server.add_tool(tool.func, name=tool.name, description=tool.func.__doc__)
    return server


def main() -> None:
    """Run the MCP server over stdio, or HTTP with --http."""
    configure_rate_limit()
    server = build_server()
    if "--http" in sys.argv:
        server.settings.port = 8765
        server.run(transport="streamable-http")
    else:
        server.run()


if __name__ == "__main__":
    main()
