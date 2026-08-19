"""Section-tree + frontmatter 3-way merge for scratchpad commit."""

from __future__ import annotations

import copy
import re
from typing import Any

from apo_engine import yaml_rt
from apo_engine.markdown_patch import normalize_lines
from apo_engine.markdown_sections import BREADCRUMB_SEP, split_sections
from apo_engine.scratchpad_format import content_hash
from apo_engine.yaml_rt import CommentedMap

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _split_fm(content: str) -> tuple[str | None, str]:
    m = _FM_RE.match(content)
    if not m:
        return None, content
    body = content[m.end() :]
    return m.group(1), body


def _join_fm(fm: str | None, body: str) -> str:
    if fm is None:
        return body if body.endswith("\n") or not body else body + "\n"
    body = body.lstrip("\n")
    return f"---\n{fm.rstrip()}\n---\n{body}"


def _section_map(body: str) -> dict[str, str]:
    lines = normalize_lines(body)
    out: dict[str, str] = {}
    for span in split_sections(lines):
        if span.heading_line < 0:
            key = "__preamble__"
            chunk = "\n".join(lines[span.body_start : span.body_end])
        else:
            key = BREADCRUMB_SEP.join(span.breadcrumb) if span.breadcrumb else span.title
            chunk = "\n".join(lines[span.heading_line : span.body_end])
        if key in out:
            key = f"{key}#{span.heading_line}"
        out[key] = chunk
    return out


def _trim_nested_from_ancestors(body_map: dict[str, str]) -> dict[str, str]:
    """Drop descendant chunks duplicated inside an ancestor section span.

    ``append_eof`` can add nested headings that ``split_sections`` indexes both
    on the parent span (heading through body_end) and as a child breadcrumb key.
    Emitting both during merge assembly duplicates the nested section.
    """
    if not body_map:
        return body_map
    out = dict(body_map)
    keys = list(out.keys())
    for parent in keys:
        if parent == "__preamble__":
            continue
        parent_chunk = out.get(parent)
        if not parent_chunk:
            continue
        prefix = parent + BREADCRUMB_SEP
        trimmed = parent_chunk
        for child in keys:
            if child == parent or not child.startswith(prefix):
                continue
            child_chunk = out.get(child)
            if not child_chunk:
                continue
            needle = child_chunk.rstrip("\n")
            if needle and needle in trimmed:
                trimmed = trimmed.replace(needle, "", 1)
        trimmed = trimmed.rstrip("\n")
        if trimmed:
            out[parent] = trimmed + "\n"
        else:
            out.pop(parent, None)
    return out


def _merge_maps(
    base: dict[str, str],
    ours: dict[str, str],
    theirs: dict[str, str],
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    keys = list(dict.fromkeys([*base.keys(), *ours.keys(), *theirs.keys()]))
    out: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for key in keys:
        b = base.get(key)
        o = ours.get(key)
        t = theirs.get(key)
        if o == t:
            if o is not None:
                out[key] = o
            continue
        if b == t and o is not None:
            out[key] = o
            continue
        if b == o and t is not None:
            out[key] = t
            continue
        if o is not None and t is None and b is not None:
            # deleted on theirs, changed on ours
            conflicts.append({"path": key, "code": "MERGE_CONFLICT", "message": f"section {key!r} changed in ours and deleted in trunk"})
            continue
        if t is not None and o is None and b is not None:
            conflicts.append({"path": key, "code": "MERGE_CONFLICT", "message": f"section {key!r} deleted in ours and changed in trunk"})
            continue
        if o is not None and t is not None and o != t:
            conflicts.append(
                {
                    "path": key,
                    "code": "MERGE_CONFLICT",
                    "message": f"section {key!r} changed in both scratchpad and trunk",
                    "ours_hash": content_hash(o),
                    "theirs_hash": content_hash(t),
                }
            )
            continue
        if o is not None:
            out[key] = o
        elif t is not None:
            out[key] = t
    if conflicts:
        return None, conflicts
    return out, []


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _load_fm_map(text: str | None) -> CommentedMap:
    if not text or not text.strip():
        return CommentedMap()
    data = yaml_rt.load(text)
    if isinstance(data, CommentedMap):
        return data
    if isinstance(data, dict):
        # Rare PyYAML-fallback / non-round-trip load — wrap without comments.
        cm = CommentedMap()
        for k, v in data.items():
            cm[k] = v
        return cm
    return CommentedMap()


def _clone_fm_map(m: CommentedMap) -> CommentedMap:
    try:
        cloned = copy.deepcopy(m)
        if isinstance(cloned, CommentedMap):
            return cloned
    except Exception:
        pass
    return _load_fm_map(yaml_rt.dump(m))


def _merge_frontmatter(base_fm: str | None, ours_fm: str | None, theirs_fm: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    """Field-level 3-way FM merge that preserves YAML comments via yaml_rt."""
    b = _load_fm_map(base_fm)
    o = _load_fm_map(ours_fm)
    t = _load_fm_map(theirs_fm)
    keys = set(b) | set(o) | set(t)

    # Decision per top-level key: "ours" | "theirs" | "keep" | "delete" | conflict
    decisions: dict[Any, str] = {}
    winners: dict[Any, Any] = {}
    conflicts: list[dict[str, Any]] = []
    ours_only = True
    theirs_only = True

    for key in keys:
        bv = b[key] if key in b else _MISSING
        ov = o[key] if key in o else _MISSING
        tv = t[key] if key in t else _MISSING
        o_changed = ov != bv
        t_changed = tv != bv
        if not o_changed and not t_changed:
            decisions[key] = "keep"
            continue
        if o_changed and not t_changed:
            theirs_only = False
            if ov is _MISSING:
                decisions[key] = "delete"
            else:
                decisions[key] = "ours"
                winners[key] = ov
            continue
        if t_changed and not o_changed:
            ours_only = False
            if tv is _MISSING:
                decisions[key] = "delete"
            else:
                decisions[key] = "theirs"
                winners[key] = tv
            continue
        # both changed vs base
        ours_only = False
        theirs_only = False
        if ov == tv:
            if ov is _MISSING:
                decisions[key] = "delete"
            else:
                decisions[key] = "ours"
                winners[key] = ov
            continue
        conflicts.append(
            {
                "path": f"frontmatter.{key}",
                "code": "MERGE_CONFLICT",
                "message": f"frontmatter field {key!r} changed in both sides",
            }
        )

    if conflicts:
        return None, conflicts

    if not keys and base_fm is None and ours_fm is None and theirs_fm is None:
        return None, []

    # One-sided fast path: return that side's FM text verbatim (comments/order intact).
    changed = [k for k, d in decisions.items() if d != "keep"]
    if changed and ours_only and not theirs_only:
        if ours_fm is None or not str(ours_fm).strip():
            return None if base_fm is None else "", []
        return str(ours_fm).rstrip("\n"), []
    if changed and theirs_only and not ours_only:
        if theirs_fm is None or not str(theirs_fm).strip():
            return None if base_fm is None else "", []
        return str(theirs_fm).rstrip("\n"), []
    if not changed:
        if base_fm is None and not keys:
            return None, []
        if base_fm is not None:
            return str(base_fm).rstrip("\n"), []
        return None, []

    # Mixed: clone base carrier and apply surgical set/delete.
    result = _clone_fm_map(b)
    for key, decision in decisions.items():
        if decision == "keep":
            continue
        if decision == "delete":
            if key in result:
                yaml_rt.delete_field_at_path(result, str(key))
            continue
        # ours / theirs
        yaml_rt.set_field_at_path(result, str(key), winners[key])

    if not result and base_fm is None and ours_fm is None and theirs_fm is None:
        return None, []
    return yaml_rt.dump(result).rstrip("\n"), []


def merge_markdown(base: str, ours: str, theirs: str) -> tuple[str | None, list[dict[str, Any]]]:
    if theirs == base:
        return ours, []
    if ours == theirs:
        return ours, []

    base_fm, base_body = _split_fm(base)
    ours_fm, ours_body = _split_fm(ours)
    theirs_fm, theirs_body = _split_fm(theirs)

    fm_merged, fm_conflicts = _merge_frontmatter(base_fm, ours_fm, theirs_fm)
    body_map, body_conflicts = _merge_maps(
        _section_map(base_body),
        _section_map(ours_body),
        _section_map(theirs_body),
    )
    conflicts = fm_conflicts + body_conflicts
    if conflicts or body_map is None:
        return None, conflicts

    body_map = _trim_nested_from_ancestors(body_map)

    # Preserve ours section order when possible
    order = list(_section_map(ours_body).keys())
    for k in body_map:
        if k not in order:
            order.append(k)
    body_parts = [body_map[k] for k in order if k in body_map]
    body = "\n".join(p.rstrip("\n") for p in body_parts)
    if body and not body.endswith("\n"):
        body += "\n"
    return _join_fm(fm_merged, body), []


def merge_buffers(
    *,
    fmt: str,
    base: str,
    ours: str,
    theirs: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    if theirs == base:
        return ours, []
    if fmt in ("json", "yaml", "mmd"):
        # Whole-file 3-way: if both changed vs base differently → conflict
        if ours != base and theirs != base and ours != theirs:
            return None, [
                {
                    "path": "$",
                    "code": "MERGE_CONFLICT",
                    "message": f"{fmt} buffer changed in both scratchpad and trunk",
                    "ours_hash": content_hash(ours),
                    "theirs_hash": content_hash(theirs),
                }
            ]
        return (ours if ours != base else theirs), []
    return merge_markdown(base, ours, theirs)
