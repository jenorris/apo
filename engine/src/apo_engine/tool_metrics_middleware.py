"""FastMCP middleware: record privacy-safe MCP tool-use metrics."""

from __future__ import annotations

import os
import time
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from apo_engine import tool_metrics


def _tool_name(context: MiddlewareContext[Any]) -> str:
    msg = getattr(context, "message", None)
    if msg is None:
        return ""
    name = str(getattr(msg, "name", "") or "")
    if name:
        return name
    params = getattr(msg, "params", None)
    return str(getattr(params, "name", "") or "")


def _validation_error_shape(exc: BaseException) -> list[str]:
    """Privacy-safe pydantic fingerprints — 'type:loc.path' only, never input values."""
    from apo_engine.validation_hints import _pydantic_errors

    out: list[str] = []
    for err in _pydantic_errors(exc)[:5]:
        loc = ".".join(str(x) for x in (err.get("loc") or ()))
        out.append(f"{err.get('type') or '?'}:{loc}")
    return out


def _tool_arguments(context: MiddlewareContext[Any]) -> dict[str, Any]:
    msg = getattr(context, "message", None)
    if msg is None:
        return {}
    args = getattr(msg, "arguments", None)
    if isinstance(args, dict):
        return args
    params = getattr(msg, "params", None)
    if params is not None:
        nested = getattr(params, "arguments", None)
        if isinstance(nested, dict):
            return nested
    return {}


class ToolMetricsMiddleware(Middleware):
    """Append one JSONL event per tools/call (best-effort; never blocks the tool)."""

    def __init__(self, collection: str | None = None) -> None:
        super().__init__()
        self.collection = (
            (collection or "").strip()
            or (os.environ.get("APO_COLLECTION") or "").strip()
            or "default"
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        if not tool_metrics.metrics_enabled():
            return await call_next(context)

        tool = _tool_name(context) or "?"
        # Don't recurse metrics into the metrics tool itself.
        if tool == "tool_stats":
            return await call_next(context)

        args = _tool_arguments(context)
        flags = tool_metrics.extract_arg_flags(args)
        req_bytes = tool_metrics.estimate_bytes(args)
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except ToolError as e:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            err_flags = dict(flags)
            shape = _validation_error_shape(e)
            if shape:
                # Which fields/op-shapes agents actually fumble — burn-down signal.
                err_flags["error_shape"] = shape
            tool_metrics.record_call(
                collection=self.collection,
                tool=tool,
                ok=False,
                error="validation_error",
                duration_ms=duration_ms,
                req_bytes=req_bytes,
                resp_bytes=tool_metrics.estimate_bytes(str(e)),
                flags=err_flags,
            )
            raise
        except Exception as e:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tool_metrics.record_call(
                collection=self.collection,
                tool=tool,
                ok=False,
                error=type(e).__name__,
                duration_ms=duration_ms,
                req_bytes=req_bytes,
                resp_bytes=0,
                flags=flags,
            )
            raise

        duration_ms = (time.perf_counter() - t0) * 1000.0
        ok, error, resp_bytes = tool_metrics.summarize_result(result)
        tool_metrics.record_call(
            collection=self.collection,
            tool=tool,
            ok=ok,
            error=error,
            duration_ms=duration_ms,
            req_bytes=req_bytes,
            resp_bytes=resp_bytes,
            flags=flags,
        )
        return result
