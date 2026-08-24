"""Frontmatter parsing shared by the OKF contract, stamp, and version readers.

Two views of the same block:

* :func:`parse_scalars` — regex, scalars only, never raises. This is what the
  stamp path uses; it must keep working on frontmatter that is not strictly
  valid YAML (hand-edited vaults, unquoted wiki-links, stray tabs).
* :func:`parse_mapping` — structured, nested values included. OKF v0.2 puts
  ``generated``, ``sources``, ``verified`` and ``usage_window`` in nested
  shapes, so the version readers need real YAML. Falls back to the scalar
  view when the block does not parse.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from apo_engine.markdown_patch import (
    _set_field_lines,
    join_lines,
    normalize_lines,
)
from apo_engine.note_format import is_yaml_note, parse_yaml_document

_H1_RE = re.compile(r"(?m)^#\s+(.+)$")
_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_SCALAR_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def parse_scalars(text: str, rel_path: str = "") -> dict[str, str]:
    """Scalar-only frontmatter view. Nested values are skipped, never raises."""
    if is_yaml_note(rel_path):
        data = parse_yaml_document(text)
        if not data:
            return {}
        out: dict[str, str] = {}
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                continue
            out[str(key)] = "" if val is None else str(val)
        return out
    m = _FM_RE.match(text)
    if not m:
        return {}
    scalars: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line or line.startswith((" ", "\t", "-", "#")):
            continue
        sm = _SCALAR_RE.match(line)
        if not sm:
            continue
        key, val = sm.group(1), sm.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        scalars[key] = val
    return scalars


def parse_mapping(text: str, rel_path: str = "") -> dict[str, Any]:
    """Structured frontmatter view, nested values preserved.

    Required for OKF v0.2, whose trust/provenance families are nested. Falls
    back to :func:`parse_scalars` when the block is not valid YAML so a
    hand-broken note degrades to the scalar view instead of vanishing.
    """
    if is_yaml_note(rel_path):
        data = parse_yaml_document(text)
        return dict(data) if isinstance(data, dict) else {}
    m = _FM_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return dict(parse_scalars(text, rel_path))
    if not isinstance(data, dict):
        return dict(parse_scalars(text, rel_path))
    return {str(k): v for k, v in data.items()}


def body_of(text: str, rel_path: str = "") -> str:
    """Note body with the frontmatter block removed."""
    if is_yaml_note(rel_path):
        return ""
    return _FM_RE.sub("", text, count=1) if _FM_RE.match(text) else text


def first_h1(text: str, rel_path: str = "") -> str | None:
    if is_yaml_note(rel_path):
        return None
    m = _H1_RE.search(body_of(text, rel_path))
    return m.group(1).strip() if m else None


def has_frontmatter(text: str, rel_path: str = "") -> bool:
    if is_yaml_note(rel_path):
        return parse_yaml_document(text) is not None
    return bool(_FM_RE.match(text))


def set_fields(content: str, updates: dict[str, str], *, rel_path: str = "") -> str:
    if not updates:
        return content
    if is_yaml_note(rel_path):
        from apo_engine.yaml_patch import set_yaml_fields

        return set_yaml_fields(content, updates)
    had_nl = content.endswith("\n")
    lines = normalize_lines(content)
    for key, val in updates.items():
        lines = _set_field_lines(lines, key, val)
    return join_lines(lines, had_nl)


def _flow_mapping(value: dict[str, Any]) -> str:
    """Render a shallow mapping as a single-line YAML flow mapping."""
    parts = []
    for key, val in value.items():
        text = str(val).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{key}: "{text}"')
    return "{ " + ", ".join(parts) + " }"


def set_structured_fields(
    content: str,
    updates: dict[str, dict[str, Any]],
    *,
    rel_path: str = "",
) -> str:
    """Set frontmatter keys whose values are nested mappings (OKF v0.2 families).

    The scalar setter in ``markdown_patch`` quotes everything it writes, which
    would turn ``generated: { by: …, at: … }`` into a *string* rather than a
    mapping. Markdown notes therefore get the flow mapping written verbatim;
    YAML notes go through the YAML document setter, which takes real objects.
    """
    if not updates:
        return content
    if is_yaml_note(rel_path):
        from apo_engine.yaml_patch import set_yaml_fields

        return set_yaml_fields(content, updates)

    from apo_engine.markdown_patch import _frontmatter_bounds

    had_nl = content.endswith("\n")
    lines = normalize_lines(content)

    for key, value in updates.items():
        rendered = _flow_mapping(value)
        new_line = f"{key}: {rendered}"
        bounds = _frontmatter_bounds(lines)
        if bounds is None:
            lines = ["---", new_line, "---", ""] + lines
            continue
        start, end = bounds
        prefix = f"{key}:"
        for i in range(start + 1, end):
            if lines[i].split("#", 1)[0].strip().startswith(prefix):
                lines[i] = new_line
                break
        else:
            lines.insert(end, new_line)

    return join_lines(lines, had_nl)


# Legacy private aliases — the stamp module and tests reached for these names.
_parse_scalars = parse_scalars
_first_h1 = first_h1
_has_frontmatter = has_frontmatter
_set_fields = set_fields
