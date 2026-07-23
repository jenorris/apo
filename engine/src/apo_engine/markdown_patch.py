"""Markdown patch engine — heading anchors, frontmatter ops, batch mutations."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


class PatchError(Exception):
    def __init__(self, code: str, message: str, suggestions: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []


@dataclass
class Section:
    level: int
    title: str
    heading_line: int
    body_start: int
    body_end: int


@dataclass
class PatchResult:
    ok: bool
    content: str
    applied: int
    results: list[dict[str, Any]]
    error: dict[str, Any] | None = None
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    lines_added: int = 0


def normalize_lines(content: str) -> list[str]:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n")


def join_lines(lines: list[str], had_trailing_newline: bool) -> str:
    out = "\n".join(lines)
    if had_trailing_newline and (out == "" or not out.endswith("\n")):
        out += "\n"
    return out


def parse_sections(lines: list[str]) -> list[Section]:
    headings: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))

    if not headings:
        return []

    sections: list[Section] = []
    for idx, (line_idx, level, title) in enumerate(headings):
        body_start = line_idx + 1
        if idx + 1 < len(headings):
            body_end = headings[idx + 1][0]
        else:
            body_end = len(lines)
        sections.append(
            Section(level=level, title=title, heading_line=line_idx, body_start=body_start, body_end=body_end)
        )
    return sections


def _normalize_heading(title: str) -> str:
    t = title.strip()
    if t.startswith("#"):
        t = _HEADING_RE.match(t)
        if t:
            return t.group(2).strip().lower()
        t = title.lstrip("#").strip()
    return t.lower()


def find_section(
    lines: list[str],
    heading: str | None,
    *,
    occurrence: int = 1,
    level: int | None = None,
) -> Section:
    if not heading or heading.lower() in ("eof", "preamble"):
        raise PatchError(
            "anchor_not_found",
            f"heading anchor required (got {heading!r}); "
            "pass heading=\"Section title\" for section ops, or use append_eof / omit heading for EOF append",
        )

    if level is None:
        # A caller writing "### Notes" is specifying depth 3 explicitly — that's the whole
        # point of the #-count. _normalize_heading strips it for text comparison, so without
        # this it's silently discarded: "### Notes" and "## Notes" become the same target and
        # match whichever occurs first in the document, regardless of which level was asked for.
        m = _HEADING_RE.match(heading.strip())
        if m:
            level = len(m.group(1))

    target = _normalize_heading(heading)
    sections = parse_sections(lines)
    matches = [s for s in sections if _normalize_heading(s.title) == target and (level is None or s.level == level)]
    if not matches:
        # get_close_matches returns best-first, so suggestions[0] is the actual best guess
        # (not just document order) — and de-duped, unlike running two overlapping matchers.
        close = difflib.get_close_matches(
            target, [_normalize_heading(s.title) for s in sections], n=3, cutoff=0.6
        )
        suggestions = []
        seen: set[str] = set()
        for c in close:
            if c in seen:
                continue
            for s in sections:
                if _normalize_heading(s.title) == c:
                    seen.add(c)
                    suggestions.append({"heading": f"{'#' * s.level} {s.title}", "line": s.heading_line + 1})
                    break
        msg = f"heading {heading!r} not found"
        if suggestions:
            msg += f" (did you mean {suggestions[0]['heading']}?)"
        raise PatchError("anchor_not_found", msg, suggestions)

    if occurrence < 1 or occurrence > len(matches):
        raise PatchError(
            "anchor_ambiguous" if len(matches) > 1 else "anchor_not_found",
            f"heading {heading!r} occurrence {occurrence} not found ({len(matches)} match(es))",
        )
    return matches[occurrence - 1]


def section_from_chunk(lines: list[str], start_line: int, heading_level: int) -> Section:
    """Resolve section bounds from index chunk metadata (start_line is 1-based)."""
    if start_line < 1:
        start_line = 1
    idx = start_line - 1
    if idx >= len(lines):
        idx = max(0, len(lines) - 1)

    heading_line = idx
    level = heading_level
    title = ""

    if heading_level > 0:
        for i in range(idx, -1, -1):
            m = _HEADING_RE.match(lines[i])
            if m:
                lvl = len(m.group(1))
                if lvl <= heading_level:
                    heading_line = i
                    level = lvl
                    title = m.group(2).strip()
                    break
        body_start = heading_line + 1
        body_end = len(lines)
        for i in range(heading_line + 1, len(lines)):
            m = _HEADING_RE.match(lines[i])
            if m and len(m.group(1)) <= level:
                body_end = i
                break
    else:
        body_start = 0
        body_end = len(lines)
        for i, line in enumerate(lines):
            if _HEADING_RE.match(line):
                body_end = i
                break

    return Section(level=level, title=title, heading_line=heading_line, body_start=body_start, body_end=body_end)


def _frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return 0, i
    return None


def _quote_yaml_value(value: str) -> str:
    if not value:
        return '""'
    needs_quote = value.strip() != value or any(c in value for c in ":{}[]#&*!?|>'\"@`")
    if not needs_quote:
        # YAML 1.1 implicitly types unquoted scalars: bare `2026-07-09` becomes a date,
        # `yes`/`no`/`true`/`null`/bare integers become bool/None/int. Round-trip through
        # the real parser rather than hand-maintaining its resolver patterns — cheaper to
        # keep correct than a punctuation blocklist that will always miss cases like this.
        try:
            needs_quote = yaml.safe_load(value) != value
        except (yaml.YAMLError, ValueError, OverflowError):
            # Invalid timestamps (e.g. 2017-00-00) raise ValueError inside PyYAML's
            # datetime constructor — not YAMLError. Quote them so writes survive.
            needs_quote = True
    if needs_quote:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _set_field_lines(lines: list[str], field: str, value: str) -> list[str]:
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        val = _quote_yaml_value(value)
        stub = ["---", f"{field}: {val}", "---", ""]
        return stub + lines

    start, end = bounds
    key_prefix = f"{field}:"
    quoted = _quote_yaml_value(str(value))
    new_line = f"{field}: {quoted}"

    for i in range(start + 1, end):
        stripped = lines[i].split("#", 1)[0].strip()
        if stripped.startswith(key_prefix):
            lines[i] = new_line
            return lines

    lines.insert(end, new_line)
    return lines


def _delete_field_lines(lines: list[str], field: str) -> list[str]:
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        raise PatchError(
            "invalid_frontmatter",
            "no YAML frontmatter block found; use set_field to create --- fields, "
            "or write_note with a frontmatter stub",
        )
    start, end = bounds
    key_prefix = f"{field}:"
    for i in range(start + 1, end):
        if lines[i].split("#", 1)[0].strip().startswith(key_prefix):
            del lines[i]
            return lines
    raise PatchError("anchor_not_found", f"frontmatter field {field!r} not found")


def _insert_text_lines(existing: list[str], insert_lines: list[str], at: int) -> tuple[list[str], int]:
    if not insert_lines:
        return existing, 0
    before = existing[:at]
    after = existing[at:]
    merged = before + insert_lines + after
    return merged, len(insert_lines)


def apply_append(
    lines: list[str],
    text: str,
    *,
    heading: str | None = None,
    section: Section | None = None,
    position: str = "end",
) -> tuple[list[str], str]:
    # str.split("\n") on a "\n"-terminated string already yields a trailing "" element,
    # so insert_lines is correctly shaped for either case without further adjustment.
    insert_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if section is None and heading:
        section = find_section(lines, heading)

    if section is None:
        at = len(lines)
        if lines and lines[-1] != "":
            insert_lines = (["\n"] if lines[-1].strip() else []) + insert_lines
        merged, n = _insert_text_lines(lines, insert_lines, at)
        return merged, f"appended {n} line(s) at EOF"

    if position == "start":
        at = section.body_start
    else:
        at = section.body_end
        if at > section.body_start and at <= len(lines):
            prev_idx = at - 1
            if prev_idx >= 0 and lines[prev_idx].strip() and insert_lines and insert_lines[0] != "":
                insert_lines = [""] + insert_lines

    merged, n = _insert_text_lines(lines, insert_lines, at)
    label = f"{'#' * section.level} {section.title}" if section.title else "EOF"
    return merged, f"appended {n} line(s) under {label} ({position})"


def apply_replace_text(
    lines: list[str],
    find: str,
    replace: str,
    *,
    count: int = 1,
    scope_heading: str | None = None,
) -> tuple[list[str], str]:
    if scope_heading:
        section = find_section(lines, scope_heading)
        segment = "\n".join(lines[section.body_start : section.body_end])
        if find not in segment:
            preview = find if len(find) <= 80 else find[:77] + "..."
            raise PatchError(
                "match_not_found",
                f"text not found in section {scope_heading!r} (find={preview!r}); "
                "re-read with read_note(path, heading=…) and retry replace_text",
            )
        occurrences = segment.count(find)
        if count > occurrences:
            raise PatchError(
                "match_not_found",
                f"expected {count} replacement(s), found {occurrences} in section {scope_heading!r}",
            )
        new_segment = segment.replace(find, replace, count)
        new_lines = new_segment.split("\n")
        merged = lines[: section.body_start] + new_lines + lines[section.body_end :]
        return merged, f"replaced {count} occurrence(s) in {scope_heading!r}"

    whole = "\n".join(lines)
    if find not in whole:
        preview = find if len(find) <= 80 else find[:77] + "..."
        raise PatchError(
            "match_not_found",
            f"text not found in note (find={preview!r}); re-read with read_note and retry",
        )
    occurrences = whole.count(find)
    if count > occurrences:
        raise PatchError(
            "match_not_found",
            f"expected {count} replacement(s), found {occurrences} in note",
        )
    new_whole = whole.replace(find, replace, count)
    return new_whole.split("\n"), f"replaced {count} occurrence(s)"


def apply_replace_section(lines: list[str], heading: str, text: str) -> tuple[list[str], str]:
    section = find_section(lines, heading)
    new_body = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while new_body and new_body[-1] == "":
        new_body.pop()
    merged = lines[: section.body_start] + new_body + lines[section.body_end :]
    return merged, f"replaced body under {'#' * section.level} {section.title}"


def _scope_heading_from_op(op: dict[str, Any]) -> str | None:
    """Resolve scope heading from ``scope.heading`` or top-level ``heading`` alias."""
    scope = op.get("scope") or {}
    scope_h = scope.get("heading") if isinstance(scope, dict) else None
    top = op.get("heading")
    if top is not None and scope_h is not None and str(top) != str(scope_h):
        raise PatchError(
            "invalid_op",
            f"conflicting heading and scope.heading: {top!r} vs {scope_h!r}",
        )
    if scope_h is not None:
        return str(scope_h)
    if top is not None:
        return str(top)
    return None


def _target_heading_from_op(op: dict[str, Any]) -> str | None:
    """Resolve section target from ``heading`` or ``target`` alias."""
    heading = op.get("heading")
    target = op.get("target")
    if target is not None and heading is not None and str(target) != str(heading):
        raise PatchError(
            "invalid_op",
            f"conflicting target and heading: {target!r} vs {heading!r}",
        )
    if heading is not None:
        return str(heading)
    if target is not None:
        return str(target)
    return None


def apply_op(lines: list[str], op: dict[str, Any]) -> tuple[list[str], str]:
    kind = op.get("op")
    if kind in ("append", "prepend"):
        heading = _target_heading_from_op(op)
        position = "start" if kind == "prepend" or op.get("position") == "start" else "end"
        section = op.get("_section")
        if section and isinstance(section, dict):
            section = Section(**section)
        return apply_append(
            lines,
            op.get("text", ""),
            heading=heading,
            section=section,
            position=position,
        )
    if kind == "set_field":
        field = op.get("field")
        if not field:
            raise PatchError(
                "invalid_op",
                "set_field requires field (op uses field/value — not key/old/new)",
            )
        merged = _set_field_lines(lines, str(field), str(op.get("value", "")))
        return merged, f"set frontmatter field {field!r}"
    if kind == "delete_field":
        field = op.get("field")
        if not field:
            raise PatchError("invalid_op", "delete_field requires field")
        merged = _delete_field_lines(lines, str(field))
        return merged, f"deleted frontmatter field {field!r}"
    if kind == "replace_text":
        find = op.get("find")
        if find is None:
            raise PatchError(
                "invalid_op",
                "replace_text requires find (use find/replace — not old/new)",
            )
        return apply_replace_text(
            lines,
            str(find),
            str(op.get("replace", "")),
            count=int(op.get("count", 1)),
            scope_heading=_scope_heading_from_op(op),
        )
    if kind == "replace_section":
        heading = _target_heading_from_op(op)
        if not heading:
            raise PatchError(
                "invalid_op",
                "replace_section requires heading or target (section title)",
            )
        return apply_replace_section(lines, heading, str(op.get("text", "")))
    if kind == "append_eof":
        return apply_append(lines, op.get("text", ""), heading=None, section=None, position="end")
    raise PatchError(
        "invalid_op",
        f"unknown op {kind!r}; allowed: set_field, delete_field, replace_text, "
        "replace_section, append, prepend, append_eof",
    )


def apply_patch(content: str, ops: list[dict[str, Any]], *, strict: bool = False) -> PatchResult:
    had_nl = content.endswith("\n")
    lines = normalize_lines(content)
    original = content
    results: list[dict[str, Any]] = []
    applied = 0
    lines_added = 0
    all_suggestions: list[dict[str, Any]] = []

    for i, op in enumerate(ops):
        try:
            before_len = len(lines)
            lines, detail = apply_op(lines, op)
            added = max(0, len(lines) - before_len)
            if op.get("op") in ("append", "prepend", "append_eof"):
                lines_added += added
            results.append({"op": i, "status": "ok", "detail": detail})
            applied += 1
        except PatchError as e:
            results.append({"op": i, "status": "error", "code": e.code, "message": e.message})
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
            error=next(({"op_index": r["op"], "code": r["code"], "message": r["message"]} for r in results if r["status"] == "error"), None),
            suggestions=all_suggestions,
        )

    new_content = join_lines(lines, had_nl)
    failed = sum(1 for r in results if r.get("status") == "error")
    return PatchResult(
        ok=failed == 0,
        content=new_content if (applied > 0 or failed == 0) else original,
        applied=applied,
        results=results,
        suggestions=all_suggestions,
        lines_added=lines_added,
        error=(
            next(
                (
                    {"op_index": r["op"], "code": r["code"], "message": r["message"]}
                    for r in results
                    if r["status"] == "error"
                ),
                None,
            )
            if failed
            else None
        ),
    )


def minimal_note_stub(path_hint: str) -> str:
    stem = path_hint.rsplit("/", 1)[-1].replace(".md", "").replace("-", " ").title()
    return f"---\ntitle: \"{stem}\"\n---\n\n"
