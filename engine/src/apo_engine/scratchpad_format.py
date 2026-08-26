"""Format normalize / patch helpers for JSON/YAML scratchpad buffers."""

from __future__ import annotations

import json
from typing import Any

from apo_engine import yaml_rt
from apo_engine.fm_path import delete_at_path, set_at_path
from apo_engine.scratchpad_store import Format


def normalize_buffer(fmt: Format, content: str | Any) -> tuple[str, list[dict[str, Any]]]:
    """Parse + canonicalize. Returns (text, diagnostics). Ill-formed → keep raw + ERROR."""
    diagnostics: list[dict[str, Any]] = []
    if fmt == "json":
        if isinstance(content, (dict, list)):
            try:
                return (
                    json.dumps(content, indent=2, ensure_ascii=False) + "\n",
                    diagnostics,
                )
            except (TypeError, ValueError) as e:
                diagnostics.append(_diag("ERROR", "JSON_ENCODE", "$", str(e)))
                return str(content), diagnostics
        text = "" if content is None else str(content)
        try:
            data = json.loads(text) if text.strip() else {}
            return json.dumps(data, indent=2, ensure_ascii=False) + "\n", diagnostics
        except json.JSONDecodeError as e:
            diagnostics.append(
                _diag(
                    "ERROR",
                    "JSON_PARSE",
                    f"line {e.lineno}",
                    e.msg,
                    hint="Fix JSON syntax, then patch surgically.",
                )
            )
            return text, diagnostics

    text = "" if content is None else str(content)
    data = yaml_rt.load(text)
    if data is None and text.strip():
        diagnostics.append(
            _diag("ERROR", "YAML_PARSE", "$", "Unparseable YAML mapping.", hint="Check indentation.")
        )
        return text.replace("\r\n", "\n"), diagnostics
    if data is None:
        return "{}\n", diagnostics
    return yaml_rt.dump(data), diagnostics


def buffer_as_dict(fmt: Format, content: str) -> dict[str, Any] | list[Any] | None:
    if fmt == "json":
        try:
            data = json.loads(content) if content.strip() else {}
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, (dict, list)) else None
    return yaml_rt.load(content)


def apply_ops_to_buffer(fmt: Format, content: str, ops: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    """Apply set_field / delete_field against an in-memory buffer."""
    if fmt == "json":
        try:
            data = json.loads(content) if content.strip() else {}
        except json.JSONDecodeError as e:
            return content, [{"ok": False, "error": "json_parse", "message": e.msg}], False
        if not isinstance(data, dict):
            return content, [{"ok": False, "error": "json_root", "message": "JSON root must be an object for set_field"}], False
        results: list[dict[str, Any]] = []
        for op in ops:
            kind = op.get("op")
            if kind == "set_field":
                set_at_path(data, str(op["field"]), op.get("value"))
                results.append({"ok": True, "op": "set_field", "field": op["field"]})
            elif kind == "delete_field":
                delete_at_path(data, str(op["field"]))
                results.append({"ok": True, "op": "delete_field", "field": op["field"]})
            else:
                results.append({"ok": False, "error": "unsupported_op", "message": f"unsupported op={kind!r}"})
                return content, results, False
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n", results, True

    from apo_engine.yaml_patch import apply_yaml_patch

    result = apply_yaml_patch(content, ops)
    return result.content, list(result.results), result.ok


def _diag(
    severity: str,
    code: str,
    path: str,
    message: str,
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }
    if hint:
        d["hint"] = hint
    return d
