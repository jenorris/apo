"""FastMCP middleware: keep alias tools callable but out of ``tools/list``."""

from __future__ import annotations

import os
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from apo_engine.tool_aliases import ALIAS_TAG, LIST_ALIASES_ENV


def _list_aliases() -> bool:
    return os.environ.get(LIST_ALIASES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class AliasListingMiddleware(Middleware):
    """Keep aliases callable but out of ``tools/list`` unless opted in."""

    async def on_list_tools(
        self, context: MiddlewareContext, call_next: CallNext[Any, Any]
    ) -> Any:
        tools = await call_next(context)
        if _list_aliases():
            return tools
        return [t for t in tools if ALIAS_TAG not in (t.tags or ())]
