"""Format normalize / fragment helpers for scratchpad buffers."""

from __future__ import annotations

import json
import re
from typing import Any

from apo_engine import yaml_rt
from apo_engine.core import _content_hash
from apo_engine.fm_path import get_at_path
from apo_engine.markdown_patch import apply_patch, normalize_lines
from apo_engine.markdown_sections import BREADCRUMB_SEP, split_sections
from apo_engine.scratchpad_store import Format

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def content_hash(text: str) -> str:
    return _content_hash(text)


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

    if fmt == "yaml":
        text = "" if content is None else str(content)
        data = yaml_rt.load(text)
        if data is None and text.strip():
            diagnostics.append(
                _diag("ERROR", "YAML_PARSE", "$", "Unparseable YAML mapping.", hint="Check indentation and fences.")
            )
            return text.replace("\r\n", "\n"), diagnostics
        if data is None:
            return "{}\n", diagnostics
        return yaml_rt.dump(data), diagnostics

    # markdown
    text = "" if content is None else str(content)
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n") and text:
        text += "\n"
    return text, diagnostics


def buffer_as_dict(fmt: Format, content: str) -> dict[str, Any] | list[Any] | None:
    if fmt == "json":
        try:
            data = json.loads(content) if content.strip() else {}
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, (dict, list)) else None
    if fmt == "yaml":
        return yaml_rt.load(content)
    m = _FM_RE.match(content)
    if not m:
        return {}
    return yaml_rt.load(m.group(1)) or {}


def apply_ops_to_buffer(fmt: Format, content: str, ops: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    """Apply patch_ops against an in-memory buffer. Returns content, results, ok."""
    if fmt == "json":
        try:
            data = json.loads(content) if content.strip() else {}
        except json.JSONDecodeError as e:
            return content, [{"ok": False, "error": "json_parse", "message": e.msg}], False
        if not isinstance(data, dict):
            return content, [{"ok": False, "error": "json_root", "message": "JSON root must be an object for set_field"}], False
        from apo_engine.fm_path import delete_at_path, set_at_path

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
                results.append({"ok": False, "error": "unsupported_op", "message": f"JSON scratchpad does not support op={kind!r}"})
                return content, results, False
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n", results, True

    if fmt == "yaml":
        from apo_engine.yaml_patch import apply_yaml_patch

        result = apply_yaml_patch(content, ops)
        return result.content, list(result.results), result.ok

    result = apply_patch(content, ops)
    return result.content, list(result.results), result.ok


def fragment_view(
    fmt: Format,
    content: str,
    *,
    json_path: str | None = None,
    heading: str | None = None,
    fields: list[str] | None = None,
    region: str | None = None,
) -> Any:
    if fmt == "json":
        data = buffer_as_dict(fmt, content)
        if data is None:
            return None
        if json_path:
            path = json_path[2:] if json_path.startswith("$.") else json_path.lstrip("$").lstrip(".")
            if not path:
                return data
            if isinstance(data, dict):
                return get_at_path(data, path)
            return None
        if fields and isinstance(data, dict):
            return {k: data.get(k) for k in fields}
        return data

    if fmt == "yaml" or region == "frontmatter":
        data = buffer_as_dict("yaml" if fmt == "yaml" else "markdown", content)
        if data is None:
            return None
        if fields and isinstance(data, dict):
            return {k: get_at_path(data, k) if "." in k or "[" in k else data.get(k) for k in fields}
        if json_path and isinstance(data, dict):
            path = json_path[2:] if json_path.startswith("$.") else json_path.lstrip("$").lstrip(".")
            return get_at_path(data, path) if path else data
        return data

    if heading:
        lines = normalize_lines(content)
        for span in split_sections(lines):
            if span.title == heading or BREADCRUMB_SEP.join(span.breadcrumb) == heading:
                return "\n".join(lines[span.heading_line : span.body_end])
        return None
    return content


def handoff_view(fmt: Format, content: str, handoff_paths: list[str] | None = None) -> dict[str, Any]:
    data = buffer_as_dict(fmt if fmt != "markdown" else "markdown", content)
    if not isinstance(data, dict):
        data = {}
    paths = handoff_paths or ["okf_type", "status", "progress", "todos", "title", "description"]
    out: dict[str, Any] = {}
    for p in paths:
        if p in data:
            out[p] = data[p]
        elif "." in p or "[" in p:
            try:
                out[p] = get_at_path(data, p)
            except Exception:
                pass
    return out


def toc_view(content: str) -> list[dict[str, Any]]:
    lines = normalize_lines(content)
    return [
        {
            "heading": span.title,
            "level": span.level,
            "breadcrumb": BREADCRUMB_SEP.join(span.breadcrumb),
        }
        for span in split_sections(lines)
        if span.heading_line >= 0
    ]


def section_hashes(content: str) -> dict[str, str]:
    lines = normalize_lines(content)
    out: dict[str, str] = {}
    for span in split_sections(lines):
        key = BREADCRUMB_SEP.join(span.breadcrumb) if span.breadcrumb else (span.title or "__preamble__")
        if span.heading_line < 0:
            key = "__preamble__"
            chunk = "\n".join(lines[span.body_start : span.body_end])
        else:
            chunk = "\n".join(lines[span.heading_line : span.body_end])
        out[key] = content_hash(chunk)
    # frontmatter as its own unit
    m = _FM_RE.match(content)
    if m:
        out["__frontmatter__"] = content_hash(m.group(1))
    return out


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
