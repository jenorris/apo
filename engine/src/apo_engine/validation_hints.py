"""Rewrite opaque Pydantic/FastMCP validation noise into agent-actionable hints.

FastMCP validates tool args *before* tool bodies run. Failures become Cursor
``isError`` text via ``str(ValidationError)`` — e.g. ``union_tag_not_found`` —
unless middleware rewrites them. Keep this module free of FastMCP imports so
unit tests stay light.
"""

from __future__ import annotations

from typing import Any


_OPS_HINT = (
    'Each ops[] item needs "op". Examples: '
    '{"op":"replace_text","find":"…","replace":"…"}, '
    '{"op":"replace_section","heading":"Next action","text":"…"}, '
    '{"op":"append","heading":"History","text":"…"}, '
    '{"op":"set_field","field":"status","value":"active"}. '
    "Keys are field/find/replace — never key/old/new."
)

_TOOL_PARAM_HINTS: dict[str, dict[str, str]] = {
    "read_note": {
        "snippet_chars": (
            "read_note has no snippet_chars — use max_chars to truncate, "
            "or search_notes(snippet_chars=…) for search hit previews."
        ),
        "top_k": "read_note has no top_k — use search_notes(top_k=…) then read_note(path=…).",
        "limit": "read_note has no limit — use max_chars / start_line / end_line, or search_notes(limit=…).",
        "query": "read_note needs path= (vault-relative). For search, use search_notes(query=…).",
    },
    "expand_chunk": {
        "path": (
            "expand_chunk requires chunk_hash from search_notes (not path/heading). "
            "To read a section by heading: read_note(path=…, heading=…)."
        ),
        "heading": (
            "expand_chunk requires chunk_hash from search_notes (not path/heading). "
            "To read a section by heading: read_note(path=…, heading=…)."
        ),
        "snippet_chars": "expand_chunk has no snippet_chars — pass chunk_hash (+ optional scope=section|chunk).",
    },
    "append_note": {
        "content": "append_note uses text= (not content). For full overwrite use write_note(path, content).",
        "ops": "append_note has no ops — use patch_note for mutators, or append_note(path, text, heading=…).",
    },
    "write_note": {
        "text": "write_note uses content= (not text). For append under a heading use append_note(path, text, heading=…).",
        "ops": "write_note has no ops — use patch_note for surgical edits.",
    },
    "patch_note": {
        "text": "patch_note mutates via ops[] — put text on an op (append/replace_section), not top-level.",
        "content": "patch_note has no content — use write_note for full overwrite, or ops with replace_section/replace_text.",
        "find": "find belongs inside an op: {\"op\":\"replace_text\",\"find\":\"…\",\"replace\":\"…\"}.",
        "old": _OPS_HINT,
        "new": _OPS_HINT,
        "key": _OPS_HINT,
    },
    "search_notes": {
        "path": (
            "search_notes uses query= (+ optional folder=). "
            "To read a known path: read_note(path=…)."
        ),
        "chunk_hash": "search_notes returns chunk_hash; to expand one hit use expand_chunk(chunk_hash=…).",
    },
    "filter_notes": {
        "query": (
            "filter_notes is frontmatter catalog — pass where={} (or where={\"status\":\"active\"}), "
            "not query=. For semantic search use search_notes."
        ),
        "top_k": "filter_notes uses limit= (and offset=), not top_k.",
    },
}


def _pydantic_errors(exc: BaseException) -> list[dict[str, Any]]:
    """Best-effort extract of pydantic error dicts from ValidationError wrappers."""
    cause = getattr(exc, "__cause__", None)
    for candidate in (cause, exc):
        if candidate is None:
            continue
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
            if name == "expand_chunk" and loc_tail == "chunk_hash":
                hints.append(
                    "expand_chunk requires chunk_hash from search_notes. "
                    "For a section by path+heading use read_note(path=…, heading=…)."
                )
                continue
            if name == "patch_note" and loc_tail == "ops":
                hints.append(f"patch_note requires ops=[…]. {_OPS_HINT}")
                continue
            if name == "filter_notes" and loc_tail in ("where",):
                hints.append(
                    "filter_notes requires where= (use where={} to list a folder). "
                    "Alias filters= is accepted."
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
