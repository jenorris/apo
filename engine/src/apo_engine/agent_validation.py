"""FastMCP middleware: rewrite ValidationError into agent-actionable ToolError text."""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from apo_engine.validation_hints import format_tool_validation_error


class AgentValidationMiddleware(Middleware):
    """Catch FastMCP arg ValidationError on tools/call and re-raise ToolError with hints."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except ValidationError as e:
            tool_name = ""
            msg = getattr(context, "message", None)
            if msg is not None:
                # CallToolRequestParams exposes name on the message itself
                # (not message.params).
                tool_name = str(getattr(msg, "name", "") or "")
                if not tool_name:
                    params = getattr(msg, "params", None)
                    tool_name = str(getattr(params, "name", "") or "")
            raise ToolError(format_tool_validation_error(tool_name, e)) from e
