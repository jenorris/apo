"""Strip wire session fields and bind per-request conversation_id for metrics."""

from __future__ import annotations

from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from apo_engine.session_context import (
    bind_request_session,
    extract_session_fields,
    strip_session_payload,
)


def _call_tool_message(context: MiddlewareContext[Any]) -> mt.CallToolRequestParams | None:
    msg = getattr(context, "message", None)
    if isinstance(msg, mt.CallToolRequestParams):
        return msg
    return None


class SessionContextMiddleware(Middleware):
    """Outermost: read _meta / _apo on tools/call; strip before validation."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        msg = _call_tool_message(context)
        if msg is None:
            return await call_next(context)

        raw_args = msg.arguments if isinstance(msg.arguments, dict) else {}
        cid, gid = extract_session_fields(meta=getattr(msg, "meta", None), arguments=raw_args)
        cleaned = strip_session_payload(raw_args)
        if cleaned is not raw_args or cid or gid:
            context = context.copy(
                message=msg.model_copy(update={"arguments": cleaned})
            )

        with bind_request_session(
            meta=getattr(msg, "meta", None),
            arguments=raw_args,
            conversation_id=cid,
            generation_id=gid,
        ):
            return await call_next(context)
