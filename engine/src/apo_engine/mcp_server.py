"""Optional Python/FastMCP surface for the engine (stdio).

The primary agent surface is the Laravel MCP gateway (../gateway). This exists as a
lightweight local alternative — register with Claude Code when you don't need the
Laravel auth plane:  claude mcp add apo -- <venv>/bin/python -m apo_engine.mcp_server
"""
from __future__ import annotations

from fastmcp import FastMCP

from . import core

mcp = FastMCP("apo")


@mcp.tool
def search(query: str, k: int = 8, exclude: list[str] | None = None) -> list[dict]:
    """Semantic search over the markdown vault.

    Args:
        query: natural-language query.
        k: number of results (default 8).
        exclude: optional path globs to drop, e.g. ["private/*"].
    """
    return [h.__dict__ for h in core.search(query, k=k, exclude=exclude)]


@mcp.tool
def index_stats() -> dict:
    """Index stats: note/chunk counts, model, backend, dimensions, path."""
    return core.stats()


if __name__ == "__main__":
    mcp.run()
