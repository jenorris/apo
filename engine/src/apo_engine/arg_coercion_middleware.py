"""FastMCP middleware: conservative argument coercion before validation."""

from __future__ import annotations

from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from apo_engine.arg_coercion import coerce_arguments, optional_non_nullable


class ArgCoercionMiddleware(Middleware):
    """Rewrite tools/call arguments with ``coerce_arguments`` before validation."""

    def __init__(self) -> None:
        super().__init__()
        self._droppable: dict[str, frozenset[str]] = {}

    async def _droppable_none(
        self, context: MiddlewareContext[Any], tool: str
    ) -> frozenset[str]:
        cached = self._droppable.get(tool)
        if cached is not None:
            return cached
        try:
            server = context.fastmcp_context.fastmcp  # type: ignore[union-attr]
            found = await server.get_tool(tool)
            out = optional_non_nullable(getattr(found, "parameters", None))
        except Exception:
            return frozenset()
        self._droppable[tool] = out
        return out

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        msg = getattr(context, "message", None)
        raw = getattr(msg, "arguments", None)
        tool = str(getattr(msg, "name", "") or "")
        if not isinstance(raw, dict) or not tool:
            return await call_next(context)
        droppable: frozenset[str] = frozenset()
        if any(v is None for v in raw.values()):
            droppable = await self._droppable_none(context, tool)
        try:
            coerced, _ = coerce_arguments(tool, raw, droppable_none=droppable)
        except Exception:
            return await call_next(context)
        if coerced is not raw:
            context = context.copy(message=msg.model_copy(update={"arguments": coerced}))
        return await call_next(context)
