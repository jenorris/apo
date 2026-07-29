"""Agent-facing arg aliases and note slicing — shared by MCP + local RPC ops."""

from __future__ import annotations

from typing import Any

from apo_engine import core
from apo_engine.markdown_patch import PatchError, find_section, normalize_lines


def resolve_top_k(
    top_k: int | None,
    limit: int | None,
    *,
    default: int = 5,
) -> tuple[int | None, str | None]:
    """Resolve search page size. ``limit`` is an alias for ``top_k``.

    Returns (k, error_message). On conflict or invalid values, k is None.
    """
    if top_k is not None and limit is not None and top_k != limit:
        return None, (
            f"conflicting top_k={top_k} and limit={limit}; "
            "pass only one (limit is an alias for top_k)"
        )
    k = default if top_k is None and limit is None else (top_k if top_k is not None else limit)
    assert k is not None
    if k < 0:
        return None, "top_k/limit must be >= 0"
    return k, None


def resolve_where(
    where: dict | None,
    filters: dict | None,
) -> tuple[dict | None, str | None]:
    """Resolve filter_notes query object. ``filters`` is an alias for ``where``."""
    if where is not None and filters is not None and where != filters:
        return None, (
            "conflicting where and filters; pass only one "
            "(filters is an alias for where — prefer where)"
        )
    chosen = where if where is not None else filters
    if chosen is None:
        # Default empty predicate — list notes in folder (or all) without forcing where={}.
        return {}, None
    if not isinstance(chosen, dict):
        return None, "`where` must be an object (use {} to list all indexed notes in folder)"
    return chosen, None


def project_frontmatter(
    fm: dict[str, Any] | None,
    fields: list[str] | None,
) -> dict[str, Any]:
    """Project frontmatter keys. ``None``/empty ``fields`` → full dict (default)."""
    src = fm if isinstance(fm, dict) else {}
    if not fields:
        return src
    out: dict[str, Any] = {}
    for key in fields:
        name = str(key).strip()
        if not name:
            continue
        if name in src:
            out[name] = src[name]
    return out


def slice_note_content(
    content: str,
    *,
    heading: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Slice note text by heading and/or 1-based inclusive line range.

    Line numbers are absolute within the file. When ``heading`` is set, the range
    is clamped to that section. ``max_chars`` truncates the result and sets
    ``truncated=True``.
    """
    if start_line is not None and start_line < 1:
        raise ValueError("start_line must be >= 1")
    if end_line is not None and end_line < 1:
        raise ValueError("end_line must be >= 1")
    if (
        start_line is not None
        and end_line is not None
        and start_line > end_line
    ):
        raise ValueError("start_line must be <= end_line")
    if max_chars is not None and max_chars < 0:
        raise ValueError("max_chars must be >= 0")

    lines = normalize_lines(content)
    lo, hi = 0, len(lines)
    heading_out = ""
    if heading:
        section = find_section(lines, heading)
        lo, hi = section.heading_line, section.body_end
        heading_out = f"{'#' * section.level} {section.title}"

    if start_line is not None:
        lo = max(lo, start_line - 1)
    if end_line is not None:
        hi = min(hi, end_line)

    text = "\n".join(lines[lo:hi])
    truncated = False
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {
        "content": text,
        "heading": heading_out,
        "start_line": lo + 1 if hi > lo or lo < len(lines) else lo,
        "end_line": hi,
        "truncated": truncated,
    }


def shape_note_read(
    raw: str,
    *,
    heading: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    raw_content: bool = False,
) -> dict[str, Any]:
    """Build ``read_note`` content fields from on-disk markdown.

    Always includes parsed ``frontmatter`` (``None`` when no YAML block).
    Default full-file reads return body-only ``content``; pass ``raw_content=True``
    for byte-exact file text (or an absolute-line slice of it). Heading / line
    slices always use absolute file coordinates and keep FM in-range when the
    slice covers it; note-level ``frontmatter`` is still attached as a sidecar.
    """
    frontmatter = core.note_frontmatter(raw)
    scoped = (
        heading is not None
        or start_line is not None
        or end_line is not None
    )

    if raw_content or scoped:
        sliced = slice_note_content(
            raw,
            heading=heading,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
        )
        return {
            "frontmatter": frontmatter,
            "content": sliced["content"],
            "heading": sliced["heading"],
            "start_line": sliced["start_line"],
            "end_line": sliced["end_line"],
            "truncated": sliced["truncated"],
        }

    body, body_line = core._body_start_line(raw)
    sliced = slice_note_content(body, max_chars=max_chars)
    # Remap 1-based body-relative lines to absolute file lines.
    rel_start = sliced["start_line"]
    rel_end = sliced["end_line"]
    if rel_start <= 0 and rel_end <= 0:
        abs_start, abs_end = rel_start, rel_end
    else:
        abs_start = body_line + max(rel_start, 1) - 1
        abs_end = body_line + max(rel_end, 0) - 1 if rel_end > 0 else rel_end
    return {
        "frontmatter": frontmatter,
        "content": sliced["content"],
        "heading": sliced["heading"],
        "start_line": abs_start,
        "end_line": abs_end,
        "truncated": sliced["truncated"],
    }


def patch_error_dict(path: str, e: PatchError) -> dict[str, Any]:
    return {
        "ok": False,
        "path": path,
        "error": e.code,
        "message": e.message,
        "suggestions": e.suggestions,
    }
