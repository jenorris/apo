"""Section-tree + frontmatter 3-way merge for scratchpad commit."""

from __future__ import annotations

import re
from typing import Any

from apo_engine import yaml_rt
from apo_engine.markdown_patch import normalize_lines
from apo_engine.markdown_sections import BREADCRUMB_SEP, split_sections
from apo_engine.scratchpad_format import content_hash, section_hashes

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


def _merge_frontmatter(base_fm: str | None, ours_fm: str | None, theirs_fm: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    def as_map(text: str | None) -> dict[str, Any]:
        if not text or not text.strip():
            return {}
        data = yaml_rt.load(text)
        return dict(data) if isinstance(data, dict) else {}

    b, o, t = as_map(base_fm), as_map(ours_fm), as_map(theirs_fm)
    keys = set(b) | set(o) | set(t)
    out: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for key in keys:
        bv, ov, tv = b.get(key, _MISSING), o.get(key, _MISSING), t.get(key, _MISSING)
        o_changed = ov != bv
        t_changed = tv != bv
        if not o_changed and not t_changed:
            if bv is not _MISSING:
                out[key] = bv
            continue
        if o_changed and not t_changed:
            if ov is not _MISSING:
                out[key] = ov
            continue
        if t_changed and not o_changed:
            if tv is not _MISSING:
                out[key] = tv
            continue
        # both changed vs base
        if ov == tv:
            if ov is not _MISSING:
                out[key] = ov
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
    if not out and base_fm is None and ours_fm is None and theirs_fm is None:
        return None, []
    return yaml_rt.dump(out).rstrip("\n"), []


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


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
    if fmt in ("json", "yaml"):
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
