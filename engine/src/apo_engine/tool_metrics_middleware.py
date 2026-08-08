"""FastMCP middleware: record vault-contract-governed MCP tool-use metrics."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from apo_engine import tool_metrics
from apo_engine.session_context import request_conversation_id


def _tool_name(context: MiddlewareContext[Any]) -> str:
    msg = getattr(context, "message", None)
    if msg is None:
        return ""
    name = str(getattr(msg, "name", "") or "")
    if name:
        return name
    params = getattr(context, "params", None)
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
    params = getattr(context, "params", None)
    if params is not None:
        nested = getattr(params, "arguments", None)
        if isinstance(nested, dict):
            return nested
    return {}


_SKIP_METRICS_TOOLS = frozenset({"telemetry", "apo_admin"})

# vault_id, vault_root, collection (collection may be "" → fall back to process default)
VaultResolve = tuple[str, Path | None, str]
VaultResolver = Callable[[dict[str, Any]], VaultResolve | tuple[str, Path | None]]


class ToolMetricsMiddleware(Middleware):
    """Record one tool-call event per tools/call (best-effort; never blocks the tool)."""

    def __init__(
        self,
        collection: str | None = None,
        *,
        vault_id: str = "",
        vault_root: Path | None = None,
        vault_resolver: VaultResolver | None = None,
    ) -> None:
        super().__init__()
        self.collection = (
            (collection or "").strip()
            or (os.environ.get("APO_COLLECTION") or "").strip()
            or "default"
        )
        self.default_vault_id = vault_id
        self.default_vault_root = vault_root
        self.vault_resolver = vault_resolver

    def _resolve_vault(self, args: dict[str, Any]) -> VaultResolve:
        """Return (vault_id, vault_root, collection) for this tool call.

        Prefer the registry collection from ``vault_resolver`` so
        ``vault(action=stats)`` (which filters by vault binding collection)
        sees the same bucket the middleware wrote.
        """
        if self.vault_resolver is not None:
            resolved = self.vault_resolver(args)
            if len(resolved) == 3:
                vid, root, coll = resolved  # type: ignore[misc]
                coll_s = (coll or "").strip()
                return str(vid or ""), root, coll_s or self.collection
            vid, root = resolved  # type: ignore[misc]
            return str(vid or ""), root, self.collection
        return self.default_vault_id, self.default_vault_root, self.collection

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        if not tool_metrics.metrics_enabled():
            return await call_next(context)

        tool = _tool_name(context) or "?"
        if tool in _SKIP_METRICS_TOOLS:
            return await call_next(context)

        args = _tool_arguments(context)
        flags = tool_metrics.extract_arg_flags(args, tool=tool)
        req_bytes = tool_metrics.estimate_bytes(args)
        vault_id, vault_root, collection = self._resolve_vault(args)
        conv_id = request_conversation_id()
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except ToolError as e:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            err_flags = dict(flags)
            shape = _validation_error_shape(e)
            if shape:
                err_flags["error_shape"] = shape
            tool_metrics.record_call(
                collection=collection,
                tool=tool,
                ok=False,
                error="validation_error",
                duration_ms=duration_ms,
                req_bytes=req_bytes,
                resp_bytes=tool_metrics.estimate_bytes(str(e)),
                flags=err_flags,
                vault_id=vault_id,
                vault_root=vault_root,
                arguments=args,
                conversation_id=conv_id,
            )
            raise
        except Exception as e:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tool_metrics.record_call(
                collection=collection,
                tool=tool,
                ok=False,
                error=type(e).__name__,
                duration_ms=duration_ms,
                req_bytes=req_bytes,
                resp_bytes=0,
                flags=flags,
                vault_id=vault_id,
                vault_root=vault_root,
                arguments=args,
                conversation_id=conv_id,
            )
            raise

        duration_ms = (time.perf_counter() - t0) * 1000.0
        ok, error, resp_bytes = tool_metrics.summarize_result(result)
        tool_metrics.record_call(
            collection=collection,
            tool=tool,
            ok=ok,
            error=error,
            duration_ms=duration_ms,
            req_bytes=req_bytes,
            resp_bytes=resp_bytes,
            flags=flags,
            vault_id=vault_id,
            vault_root=vault_root,
            arguments=args,
            conversation_id=conv_id,
        )
        return result
