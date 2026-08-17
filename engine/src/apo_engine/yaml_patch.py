"""Patch ops for standalone YAML catalog notes.

Supports ``set_field`` / ``delete_field`` with dotted paths (nested maps,
list indices, and ``[id=…]`` selectors). Heading / section / append ops raise
``unsupported_format``.
"""

from __future__ import annotations

from typing import Any

from apo_engine.fm_path import FmPathError, delete_at_path, set_at_path
from apo_engine.markdown_patch import PatchError, PatchResult
from apo_engine.note_format import (
    coerce_yaml_value,
    dump_yaml_document,
    parse_yaml_document,
)

_YAML_FIELD_OPS = frozenset({"set_field", "delete_field"})
_YAML_UNSUPPORTED = frozenset(
    {
        "append",
        "prepend",
        "append_eof",
        "replace_text",
        "replace_section",
    }
)


def _reraise_path(exc: FmPathError) -> None:
    raise PatchError(exc.code, exc.message) from exc


def set_field_path(data: dict[str, Any], field: str, value: Any) -> None:
    try:
        set_at_path(data, field, coerce_yaml_value(value))
    except FmPathError as e:
        _reraise_path(e)


def delete_field_path(data: dict[str, Any], field: str) -> None:
    try:
        delete_at_path(data, field)
    except FmPathError as e:
        _reraise_path(e)


def apply_yaml_op(data: dict[str, Any], op: dict[str, Any]) -> str:
    kind = op.get("op")
    if kind in _YAML_UNSUPPORTED:
        raise PatchError(
            "unsupported_format",
            f"op {kind!r} is Markdown-only; YAML notes support set_field/delete_field "
            "(use write_note to replace the whole document)",
        )
    if kind == "set_field":
        field = op.get("field")
        if not field:
            raise PatchError(
                "invalid_op",
                "set_field requires field (op uses field/value — not key/old/new)",
            )
        set_field_path(data, str(field), op.get("value", ""))
        return f"set yaml field {field!r}"
    if kind == "delete_field":
        field = op.get("field")
        if not field:
            raise PatchError("invalid_op", "delete_field requires field")
        delete_field_path(data, str(field))
        return f"deleted yaml field {field!r}"
    raise PatchError(
        "invalid_op",
        f"unknown op {kind!r}; YAML notes support: set_field, delete_field",
    )


def apply_yaml_patch(
    content: str,
    ops: list[dict[str, Any]],
    *,
    strict: bool = False,
) -> PatchResult:
    data = parse_yaml_document(content)
    if data is None:
        raise PatchError(
            "invalid_frontmatter",
            "YAML note must be a top-level mapping (object); fix with write_note",
        )

    original = content
    # Mutate the parsed document in place: ``data`` is a fresh per-call parse and
    # a shallow ``dict()`` copy would drop the round-trip comment/format state.
    # Rollback uses ``original`` (the untouched source text), not this object.
    working = data
    results: list[dict[str, Any]] = []
    applied = 0
    all_suggestions: list[dict[str, Any]] = []

    for i, op in enumerate(ops):
        try:
            detail = apply_yaml_op(working, op)
            results.append({"op": i, "status": "ok", "detail": detail})
            applied += 1
        except PatchError as e:
            results.append(
                {"op": i, "status": "error", "code": e.code, "message": e.message}
            )
            all_suggestions.extend(e.suggestions)
            if strict:
                return PatchResult(
                    ok=False,
                    content=original,
                    applied=0,
                    results=results,
                    error={"op_index": i, "code": e.code, "message": e.message},
                    suggestions=all_suggestions,
                )

    if applied == 0 and any(r["status"] == "error" for r in results):
        return PatchResult(
            ok=False,
            content=original,
            applied=0,
            results=results,
            error=next(
                (
                    {"op_index": r["op"], "code": r["code"], "message": r["message"]}
                    for r in results
                    if r["status"] == "error"
                ),
                None,
            ),
            suggestions=all_suggestions,
        )

    new_content = dump_yaml_document(working) if applied > 0 else original
    failed = sum(1 for r in results if r.get("status") == "error")
    return PatchResult(
        ok=failed == 0,
        content=new_content if (applied > 0 or failed == 0) else original,
        applied=applied,
        results=results,
        suggestions=all_suggestions,
        lines_added=0,
        error=(
            next(
                (
                    {
                        "op_index": r["op"],
                        "code": r["code"],
                        "message": r["message"],
                    }
                    for r in results
                    if r["status"] == "error"
                ),
                None,
            )
            if failed
            else None
        ),
    )


def set_yaml_fields(content: str, updates: dict[str, str]) -> str:
    """OKF stamp helper — set top-level (or dotted) scalar fields on a YAML note."""
    data = parse_yaml_document(content)
    if data is None:
        data = {}
    for key, val in updates.items():
        set_field_path(data, key, val)
    return dump_yaml_document(data)
