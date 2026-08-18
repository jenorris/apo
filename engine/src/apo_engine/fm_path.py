"""Frontmatter field-path grammar for filter_notes and set_field.

Segments:
- map key: ``owner``
- list index: ``0`` (when parent is a list)
- id selector: ``[id=skypad-resolver]`` (first matching list element)

Examples: ``todos``, ``todos.0.status``, ``todos[id=skypad-resolver].status``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class FmPathError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MapKey:
    key: str


@dataclass(frozen=True)
class ListIndex:
    index: int


@dataclass(frozen=True)
class IdSelector:
    field: str
    value: str


Segment = MapKey | ListIndex | IdSelector

# One path segment: key, key[id=val], or bare digit index after a dot.
_SEG_RE = re.compile(
    r"""
    (?:
        (?P<key>[A-Za-z_][A-Za-z0-9_]*)
        (?:\[(?P<sel_key>[A-Za-z_][A-Za-z0-9_]*)=(?P<sel_val>[^\]]+)\])?
      |
        (?P<idx>\d+)
    )
    """,
    re.VERBOSE,
)


def loose_eq(a: Any, b: Any) -> bool:
    if isinstance(a, type(b)) or isinstance(b, type(a)):
        return a == b
    return str(a).strip().lower() == str(b).strip().lower()


def split_path(field: str) -> list[Segment]:
    raw = str(field).strip()
    if not raw:
        raise FmPathError("invalid_op", "field path must be non-empty")

    parts: list[Segment] = []
    pos = 0
    while pos < len(raw):
        if parts and raw[pos] == ".":
            pos += 1
            if pos >= len(raw):
                raise FmPathError("invalid_op", f"trailing '.' in field path {field!r}")
        elif parts and raw[pos] == "[":
            # Allow ``todos[id=x]`` without a preceding dot (already consumed key).
            pass
        elif parts:
            raise FmPathError(
                "invalid_op",
                f"expected '.' or '[' in field path {field!r} at index {pos}",
            )

        m = _SEG_RE.match(raw, pos)
        if not m:
            raise FmPathError("invalid_op", f"invalid field path segment in {field!r} at index {pos}")

        if m.group("idx") is not None:
            parts.append(ListIndex(int(m.group("idx"))))
        elif m.group("sel_key") is not None:
            parts.append(MapKey(m.group("key")))
            parts.append(IdSelector(m.group("sel_key"), m.group("sel_val")))
        else:
            parts.append(MapKey(m.group("key")))
        pos = m.end()

    if pos != len(raw):
        raise FmPathError("invalid_op", f"invalid field path {field!r}")
    if not parts:
        raise FmPathError("invalid_op", "field path must be non-empty")
    return parts


def _find_by_id(lst: list[Any], sel: IdSelector) -> tuple[int, dict[str, Any]]:
    matches: list[tuple[int, dict[str, Any]]] = []
    for i, el in enumerate(lst):
        if isinstance(el, dict) and loose_eq(el.get(sel.field), sel.value):
            matches.append((i, el))
    if not matches:
        raise FmPathError(
            "anchor_not_found",
            f"no list element with {sel.field}={sel.value!r}",
        )
    if len(matches) > 1:
        raise FmPathError(
            "anchor_ambiguous",
            f"multiple list elements with {sel.field}={sel.value!r} ({len(matches)} matches)",
        )
    return matches[0]


def _ensure_list_slot(lst: list[Any], index: int) -> dict[str, Any]:
    while len(lst) <= index:
        lst.append({})
    el = lst[index]
    if not isinstance(el, dict):
        raise FmPathError(
            "invalid_op",
            f"list index {index} is not a mapping (cannot nest under it)",
        )
    return el


def _traverse(
    cur: Any,
    seg: Segment,
    *,
    create: bool,
) -> Any:
    if isinstance(seg, MapKey):
        if not isinstance(cur, dict):
            raise FmPathError(
                "anchor_not_found",
                f"cannot traverse field path through non-mapping at {seg.key!r}",
            )
        if seg.key not in cur:
            if not create:
                raise FmPathError(
                    "anchor_not_found",
                    f"frontmatter field path not found ({seg.key})",
                )
            cur[seg.key] = {}
        return cur[seg.key]

    if isinstance(seg, ListIndex):
        if create and cur is None:
            raise FmPathError("invalid_op", "cannot create list index under missing parent")
        if not isinstance(cur, list):
            raise FmPathError(
                "anchor_not_found",
                f"cannot traverse field path through non-list at index {seg.index}",
            )
        if seg.index < 0:
            raise FmPathError("invalid_op", f"list index must be >= 0 (got {seg.index})")
        if create:
            return _ensure_list_slot(cur, seg.index)
        if seg.index >= len(cur):
            raise FmPathError(
                "anchor_not_found",
                f"list index {seg.index} out of range (len={len(cur)})",
            )
        return cur[seg.index]

    # IdSelector
    if not isinstance(cur, list):
        raise FmPathError(
            "anchor_not_found",
            f"cannot apply id selector through non-list at {seg.field}={seg.value!r}",
        )
    if create and not cur:
        raise FmPathError(
            "anchor_not_found",
            f"no list element with {seg.field}={seg.value!r}",
        )
    _, el = _find_by_id(cur, seg)
    return el


def get_parent(
    data: dict[str, Any],
    parts: list[Segment],
    *,
    create: bool,
) -> tuple[Any, Segment]:
    """Return (parent container, leaf segment) for set/delete."""
    if not parts:
        raise FmPathError("invalid_op", "field path must be non-empty")
    cur: Any = data
    for i, seg in enumerate(parts[:-1]):
        # When creating and next segment needs a list parent under a missing map key,
        # pre-create list if the next segment is ListIndex or IdSelector.
        if create and isinstance(seg, MapKey) and isinstance(cur, dict) and seg.key not in cur:
            nxt = parts[i + 1]
            if isinstance(nxt, (ListIndex, IdSelector)):
                cur[seg.key] = []
                cur = cur[seg.key]
                continue
        cur = _traverse(cur, seg, create=create)
        if create and isinstance(seg, MapKey) and not isinstance(cur, (dict, list)):
            raise FmPathError(
                "invalid_op",
                f"cannot nest under non-mapping field {seg.key!r}",
            )
    return cur, parts[-1]


def set_at_path(data: dict[str, Any], field: str, value: Any) -> None:
    parts = split_path(field)
    parent, leaf = get_parent(data, parts, create=True)

    if isinstance(leaf, MapKey):
        if not isinstance(parent, dict):
            raise FmPathError(
                "invalid_op",
                f"cannot set field on non-mapping parent for {field!r}",
            )
        parent[leaf.key] = value
        return

    if isinstance(leaf, ListIndex):
        if not isinstance(parent, list):
            raise FmPathError(
                "invalid_op",
                f"cannot set list index on non-list parent for {field!r}",
            )
        if leaf.index < 0:
            raise FmPathError("invalid_op", f"list index must be >= 0 (got {leaf.index})")
        while len(parent) <= leaf.index:
            parent.append(None)
        parent[leaf.index] = value
        return

    # IdSelector as leaf — replace the matched element
    if not isinstance(parent, list):
        raise FmPathError(
            "invalid_op",
            f"cannot set id selector on non-list parent for {field!r}",
        )
    idx, _ = _find_by_id(parent, leaf)
    parent[idx] = value


def get_at_path(data: dict[str, Any], field: str) -> Any:
    """Read ``field`` path under ``data``. Missing path → ``None``."""
    try:
        parts = split_path(field)
        parent, leaf = get_parent(data, parts, create=False)
    except FmPathError:
        return None
    try:
        return _traverse(parent, leaf, create=False)
    except FmPathError:
        return None


def delete_at_path(data: dict[str, Any], field: str) -> None:
    parts = split_path(field)
    parent, leaf = get_parent(data, parts, create=False)

    if isinstance(leaf, MapKey):
        if not isinstance(parent, dict) or leaf.key not in parent:
            raise FmPathError("anchor_not_found", f"frontmatter field {field!r} not found")
        del parent[leaf.key]
        return

    if isinstance(leaf, ListIndex):
        if not isinstance(parent, list) or leaf.index < 0 or leaf.index >= len(parent):
            raise FmPathError("anchor_not_found", f"frontmatter field {field!r} not found")
        del parent[leaf.index]
        return

    if not isinstance(parent, list):
        raise FmPathError("anchor_not_found", f"frontmatter field {field!r} not found")
    idx, _ = _find_by_id(parent, leaf)
    del parent[idx]


def resolve_values(data: Any, field: str) -> list[Any]:
    """Collect all values at ``field`` for filter matching (lists expand).

    ``todos.status`` yields each element's ``status``. Missing paths yield ``[]``.
    """
    try:
        parts = split_path(field)
    except FmPathError:
        return []

    currents: list[Any] = [data]
    for seg in parts:
        nxt: list[Any] = []
        for cur in currents:
            nxt.extend(_step_filter(cur, seg))
        currents = nxt
        if not currents:
            return []
    return currents


def _step_filter(cur: Any, seg: Segment) -> list[Any]:
    if isinstance(seg, MapKey):
        if isinstance(cur, dict):
            if seg.key not in cur:
                return []
            return [cur[seg.key]]
        if isinstance(cur, list):
            out: list[Any] = []
            for el in cur:
                if isinstance(el, dict) and seg.key in el:
                    out.append(el[seg.key])
            return out
        return []

    if isinstance(seg, ListIndex):
        if not isinstance(cur, list) or seg.index < 0 or seg.index >= len(cur):
            return []
        return [cur[seg.index]]

    # IdSelector
    if not isinstance(cur, list):
        return []
    out = []
    for el in cur:
        if isinstance(el, dict) and loose_eq(el.get(seg.field), seg.value):
            out.append(el)
    return out


def path_needs_python_match(key: str) -> bool:
    """True when ``where`` key is nested (not a flat SQL-safe identifier)."""
    return "." in key or "[" in key
