"""Patch ops for standalone JSON catalog files (``.json``).

Same ``set_field`` / ``delete_field`` dotted-path semantics as ``yaml_patch``
(nested maps, list indices, ``[id=…]`` selectors), sharing its op loop. Values
stay native JSON — no YAML scalar coercion. Heading / section / append ops
raise ``unsupported_format``.
"""

from __future__ import annotations

import json
from typing import Any

from apo_engine.fm_path import FmPathError, delete_at_path, set_at_path
from apo_engine.markdown_patch import PatchError, PatchResult
from apo_engine.yaml_patch import _YAML_UNSUPPORTED, apply_mapping_patch


def parse_json_document(text: str) -> dict[str, Any] | None:
    """Parse a JSON catalog. Top-level object → dict; empty → ``{}``; else None."""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def dump_json_document(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def apply_json_op(data: dict[str, Any], op: dict[str, Any]) -> str:
    kind = op.get("op")
    if kind in _YAML_UNSUPPORTED:
        raise PatchError(
            "unsupported_format",
            f"op {kind!r} is Markdown-only; JSON catalogs support set_field/delete_field "
            "(use write_note to replace the whole document)",
        )
    field = op.get("field")
    if kind == "set_field":
        if not field:
            raise PatchError(
                "invalid_op",
                "set_field requires field (op uses field/value — not key/old/new)",
            )
        try:
            set_at_path(data, str(field), op.get("value"))
        except FmPathError as e:
            raise PatchError(e.code, e.message) from e
        return f"set json field {field!r}"
    if kind == "delete_field":
        if not field:
            raise PatchError("invalid_op", "delete_field requires field")
        try:
            delete_at_path(data, str(field))
        except FmPathError as e:
            raise PatchError(e.code, e.message) from e
        return f"deleted json field {field!r}"
    raise PatchError(
        "invalid_op",
        f"unknown op {kind!r}; JSON catalogs support: set_field, delete_field",
    )


def apply_json_patch(
    content: str,
    ops: list[dict[str, Any]],
    *,
    strict: bool = False,
) -> PatchResult:
    data = parse_json_document(content)
    if data is None:
        raise PatchError(
            "invalid_json",
            "JSON catalog must parse as a top-level object; fix with write_note",
        )
    return apply_mapping_patch(
        data,
        ops,
        original=content,
        strict=strict,
        apply_op=apply_json_op,
        dump=dump_json_document,
    )
