"""Rewrite opaque Pydantic/FastMCP validation noise into agent-actionable hints.

FastMCP validates tool args *before* tool bodies run. Failures become Cursor
``isError`` text via ``str(ValidationError)`` — e.g. ``union_tag_not_found`` —
unless middleware rewrites them. Keep this module free of FastMCP imports so
unit tests stay light.
"""

from __future__ import annotations

from typing import Any


_OPS_HINT = (
    'Each ops[] item needs "op". '
    "Ops: set_field(field,value); replace_text(find,replace,scope.heading|heading); "
    "replace_section(heading,text); append(heading,text) "
    "(standalone add → append_note). "
    "Keys: field/find/replace — never path/key/old/new/old_text/new_text."
)

_TOOL_PARAM_HINTS: dict[str, dict[str, str]] = {
    "read_note": {
        "snippet_chars": (
            "read_note has no snippet_chars — use max_chars to truncate, "
            "or search_notes(snippet_chars=…) for search hit previews."
        ),
        "top_k": "read_note has no top_k — use search_notes(limit=…) then read_note(path=… or chunk_hash=…).",
        "limit": "read_note has no limit — use max_chars / start_line / end_line, or search_notes(limit=…).",
        "query": (
            "read_note needs path= or chunk_hash= (from search_notes). "
            "For search, use search_notes(query=…)."
        ),
        "scope": (
            "read_note has no scope — pass chunk_hash= from search_notes "
            "(optional force= for full section above preview threshold)."
        ),
    },
    "write_note": {
        "body": "write_note uses content= only on MCP. For append under a heading use append_note(path, text, heading=…).",
        "text": "write_note uses content= (not text). For append under a heading use append_note(path, text, heading=…).",
        "ops": "write_note has no ops — use patch_note for surgical edits.",
        "append": (
            "write_note append is removed — use append_note(path, text, heading=… "
            "or chunk_hash=…)."
        ),
        "index": "write_note has no index= — writes always enqueue for apo-engine watch.",
        "heading": (
            "write_note has no heading= — it overwrites the whole note. "
            "To add under a section use append_note(path, text, heading=…)."
        ),
        "create": (
            "write_note has no create= — it always creates or overwrites. "
            "create= belongs on append_note."
        ),
    },
    "append_note": {
        "body": "append_note uses text= only on MCP. For full overwrite use write_note(path, content).",
        "content": "append_note uses text= (not content). For full overwrite use write_note(path, content).",
        "ops": "append_note has no ops — use patch_note for mutators, or append_note(path, text, heading=…).",
        "index": "append_note has no index= — writes always enqueue for apo-engine watch.",
        "path": (
            "append_note path= is optional when chunk_hash= is set (path derived from index). "
            "Pass both as a guard, or path+heading to fall back if the hash is stale. "
            "YAML (.yaml/.yml) notes reject append_note — use write_note / patch_note set_field."
        ),
    },
    "patch_note": {
        "text": "patch_note mutates via ops[] — put text on an op (append/replace_section), not top-level.",
        "content": "patch_note has no content — use write_note for full overwrite, or ops with replace_section/replace_text.",
        "find": "find belongs inside an op: {\"op\":\"replace_text\",\"find\":\"…\",\"replace\":\"…\"}.",
        "path": (
            "patch_note single-path mode uses top-level path= + ops=[…]. "
            "Multi-path: items=[{path, ops, …}] — each item requires path unless all ops are place."
        ),
        "old": _OPS_HINT,
        "new": _OPS_HINT,
        "old_text": "replace_text uses find= and replace= (aliases old_text/new_text accepted).",
        "new_text": "replace_text uses find= and replace= (aliases old_text/new_text accepted).",
        "key": _OPS_HINT,
        "index": "patch_note has no index= — writes always enqueue for apo-engine watch.",
        "src": (
            "move/copy uses place op: patch_note(ops=[{op:place, src, dst, overwrite?, fields?}]). "
            "Not top-level src=."
        ),
        "dst": (
            "move/copy uses place op: patch_note(ops=[{op:place, src, dst, overwrite?, fields?}])."
        ),
    },
    "search_notes": {
        "path": (
            "search_notes uses query= (+ optional folder= or folders=[]). "
            "To read a known path: read_note(path=…)."
        ),
        "chunk_hash": (
            "search_notes returns chunk_hash; to read a hit use read_note(chunk_hash=…)."
        ),
        "folder": (
            "pass folder= or folders=[], not both. Multi-folder: folders=[\"areas/threads\", \"projects/foo\"]."
        ),
        "folders": (
            "pass folder= or folders=[], not both. Single folder: folder=… instead."
        ),
        "top_k": "search_notes uses limit= (not top_k).",
    },
    "filter_notes": {
        "query": (
            "filter_notes is frontmatter catalog — pass where={} (or where={\"status\":\"active\"}), "
            "not query=. For semantic search use search_notes."
        ),
        "top_k": "filter_notes uses limit= (and offset=), not top_k.",
        "filters": "filter_notes uses where= (not filters=).",
        "order_by": "filter_notes uses sort= (mtime or FM key) and order=asc|desc, not order_by.",
    },
}


def _pydantic_errors(exc: BaseException) -> list[dict[str, Any]]:
    """Best-effort extract of pydantic error dicts from ValidationError wrappers.

    Walks ``__cause__`` / ``__context__`` so ToolError → FastMCP ValidationError →
    pydantic.ValidationError still yields shapes (metrics sits outside the rewrite).
    """
    seen: set[int] = set()
    candidate: BaseException | None = exc
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        errors_fn = getattr(candidate, "errors", None)
        if callable(errors_fn):
            try:
                return list(errors_fn(include_url=False))  # type: ignore[call-arg]
            except TypeError:
                try:
                    return list(errors_fn())
                except Exception:
                    pass
            except Exception:
                pass
        nxt = getattr(candidate, "__cause__", None) or getattr(
            candidate, "__context__", None
        )
        candidate = nxt if isinstance(nxt, BaseException) else None
    return []


def _loc_tail(err: dict[str, Any]) -> str | None:
    loc = err.get("loc") or ()
    if not loc:
        return None
    tail = loc[-1]
    return str(tail) if tail is not None else None


def _input_keys(err: dict[str, Any]) -> list[str]:
    raw = err.get("input")
    if isinstance(raw, dict):
        return sorted(str(k) for k in raw.keys())
    return []


def format_tool_validation_error(tool_name: str, exc: BaseException) -> str:
    """Turn a FastMCP/Pydantic ValidationError into a one-shot agent hint."""
    name = (tool_name or "").strip() or "tool"
    errors = _pydantic_errors(exc)
    hints: list[str] = []

    for err in errors:
        etype = str(err.get("type") or "")
        loc_tail = _loc_tail(err)
        msg = str(err.get("msg") or "")

        loc_str = str(err.get("loc") or ())
        is_ops_tag = etype == "union_tag_not_found" or (
            "discriminator" in msg.lower() and "ops" in loc_str
        )
        if is_ops_tag and (name == "patch_note" or "ops" in loc_str):
            keys = _input_keys(err)
            extra = f" Got keys {keys}." if keys else ""
            hints.append(f'patch_note ops missing required "op". {_OPS_HINT}{extra}')
            continue

        if etype in ("unexpected_keyword_argument", "extra_forbidden") and loc_tail:
            tool_map = _TOOL_PARAM_HINTS.get(name, {})
            if loc_tail in tool_map:
                hints.append(tool_map[loc_tail])
                continue
            if name == "patch_note" and loc_tail in ("old", "new", "key", "value") and "ops" in str(
                err.get("loc") or ()
            ):
                hints.append(_OPS_HINT)
                continue
            hints.append(
                f"{name} does not accept argument {loc_tail!r}. "
                f"Call GetMcpTools for {name}'s schema, or drop the unknown kw."
            )
            continue

        if etype == "missing_argument" and loc_tail:
            if name == "read_note" and loc_tail == "chunk_hash":
                hints.append(
                    "read_note requires path= or chunk_hash= from search_notes. "
                    "For a section by path+heading use read_note(path=…, heading=…)."
                )
                continue
            if name == "read_note" and loc_tail == "path":
                for peer in errors:
                    keys = _input_keys(peer)
                    if "chunk_hash" in keys or (
                        peer.get("type") == "unexpected_keyword_argument"
                        and _loc_tail(peer) == "chunk_hash"
                    ):
                        hints.append(
                            "read_note accepts path= or chunk_hash= (from search_notes), not both."
                        )
                        break
                else:
                    hints.append(
                        "read_note missing path= — pass vault-relative path or chunk_hash= from search_notes."
                    )
                continue
            if name == "append_note" and loc_tail == "text":
                # Dual-error case: content=/body= present → prefer alias hint over generic missing.
                for peer in errors:
                    keys = _input_keys(peer)
                    if "content" in keys or (
                        peer.get("type") == "unexpected_keyword_argument"
                        and _loc_tail(peer) == "content"
                    ):
                        hints.append(_TOOL_PARAM_HINTS["append_note"]["content"])
                        break
                    if "body" in keys or (
                        peer.get("type") == "unexpected_keyword_argument"
                        and _loc_tail(peer) == "body"
                    ):
                        hints.append(_TOOL_PARAM_HINTS["append_note"]["body"])
                        break
                else:
                    hints.append(
                        "append_note missing required argument 'text' "
                        "(alias content= also accepted)."
                    )
                continue
            if name == "write_note" and loc_tail == "content":
                for peer in errors:
                    keys = _input_keys(peer)
                    if "text" in keys or (
                        peer.get("type") == "unexpected_keyword_argument"
                        and _loc_tail(peer) == "text"
                    ):
                        hints.append(_TOOL_PARAM_HINTS["write_note"]["text"])
                        break
                else:
                    hints.append(
                        "write_note missing required argument 'content' "
                        "(alias text= also accepted)."
                    )
                continue
            if name == "patch_note" and loc_tail == "path" and "items" in loc_str:
                hints.append(
                    "patch_note items[] entries each need path= and ops=[…]. "
                    "Single-path mode: top-level path= + ops= (not items=)."
                )
                continue
            if name == "patch_note" and loc_tail == "ops":
                hints.append(f"patch_note requires ops=[…]. {_OPS_HINT}")
                continue
            if name == "filter_notes" and loc_tail in ("where",):
                hints.append(
                    "filter_notes requires where= (use where={} to list a folder)."
                )
                continue
            hints.append(f"{name} missing required argument {loc_tail!r}.")
            continue

        if etype == "missing" and loc_tail:
            # Pydantic v2 field-required on nested models
            if name == "patch_note" and loc_tail == "op":
                hints.append(f"patch_note ops missing required \"op\". {_OPS_HINT}")
                continue
            if name == "patch_note" and loc_tail in ("field", "find", "text"):
                hints.append(
                    f"patch_note op missing {loc_tail!r}. "
                    "set_field→field; replace_text→find; append/replace_section→text/heading."
                )
                continue

    # Dedup while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            unique.append(h)

    if unique:
        return " ".join(unique)

    # Fallback: strip pydantic URL footers; keep first line of the raw message
    raw = str(exc).strip()
    lines = [ln for ln in raw.splitlines() if ln.strip() and "errors.pydantic.dev" not in ln]
    body = " ".join(ln.strip() for ln in lines[:6]) if lines else raw
    return (
        f"Invalid arguments for {name}: {body}. "
        f"Fix args to match the tool schema (GetMcpTools), then retry."
    )


def flatten_patch_failure_error(
    error: Any,
    *,
    suggestions: list[Any] | None = None,
) -> dict[str, Any]:
    """Normalize apply_patch's nested ``error`` dict into top-level string fields.

    Agents often check ``error`` / ``message`` as strings; nested
    ``{op_index, code, message}`` is easy to miss.
    """
    out: dict[str, Any] = {}
    if isinstance(error, dict):
        out["error"] = str(error.get("code") or "patch_failed")
        out["message"] = str(error.get("message") or "patch failed")
        if "op_index" in error and error["op_index"] is not None:
            out["op_index"] = error["op_index"]
        out["error_detail"] = error
    elif error is None:
        out["error"] = "patch_failed"
        out["message"] = "patch failed"
    else:
        out["error"] = str(error)
        out["message"] = str(error)
    if suggestions:
        out["suggestions"] = suggestions
    return out
