"""Embed-side handling of oversize markdown sections.

An embedding model reads a bounded amount of text. Past that bound a backend either
truncates silently (Ollama/bge-m3: only the head of the section is represented) or
rejects the input (llama.cpp: the whole file then fails to index). Sections are
hierarchical, so most oversize sections are *rollups* — an H1 or H2 whose children are
already indexed as their own chunks. Two cases, two treatments:

- **Rollup** (has indexed child sections): embed the heading, the intro before the first
  child, and an outline of the child headings. The children carry the detail.
- **Leaf** (no children): split into line-aware windows. Window 0 becomes the section's
  own embed text; windows 1..n become ``section_part`` rows that point at the section.

``chunks.text`` and the FTS row for the section always keep the full text, so lexical
search covers everything; only the *vector* input is bounded. Pure functions, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .markdown_sections import BREADCRUMB_SEP

# A section tuple as produced by core.section_markdown.
Section = tuple[str, int, str, int, int]  # (breadcrumb, level, text, start_line, end_line)


@dataclass(frozen=True)
class Window:
    text: str
    start_line: int
    end_line: int


@dataclass
class SectionPlan:
    """How to embed one oversize section."""

    kind: str  # "rollup" | "leaf"
    embed_text: str
    parts: list[Window] = field(default_factory=list)  # leaf windows 1..n


def children_of(sections: list[Section], i: int) -> list[Section]:
    """Sections strictly nested inside ``sections[i]`` (deeper level, inside its line span)."""
    _bc, level, _text, start, end = sections[i]
    out: list[Section] = []
    for j in range(i + 1, len(sections)):
        s = sections[j]
        if s[3] > end:
            break
        if s[1] > level and s[3] >= start and s[4] <= end:
            out.append(s)
    return out


def _cut(text: str, limit: int) -> str:
    """Trim to ``limit`` chars at a paragraph, line, or word boundary."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("\n\n", "\n", " "):
        pos = head.rfind(sep)
        if pos >= limit // 2:
            return head[:pos].rstrip()
    return head.rstrip()


def rollup_embed_text(
    section: Section,
    children: list[Section],
    budget: int,
    clean: Callable[[str], str],
) -> str:
    """Heading + intro before the first child + outline of descendant headings."""
    breadcrumb, level, text, start, _end = section
    first_child_line = min(c[3] for c in children)
    lines = text.split("\n")
    intro = clean("\n".join(lines[: max(first_child_line - start, 0)]))
    intro = _cut(intro, int(budget * 0.6))
    if not intro.strip():
        intro = breadcrumb.rsplit(BREADCRUMB_SEP, 1)[-1]
    outline: list[str] = ["", "Sections:"]
    used = len(intro) + len("\n\nSections:")
    for c in children:
        title = c[0].rsplit(BREADCRUMB_SEP, 1)[-1]
        line = f"{'  ' * (c[1] - level - 1)}- {title}"
        if used + len(line) + 1 > budget:
            outline.append("…")
            break
        outline.append(line)
        used += len(line) + 1
    return intro + "\n" + "\n".join(outline)


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Inclusive (first, last) line-index spans split at blank lines, never inside a fence."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    fence: str | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            token = stripped[:3]
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
        blank = not line.strip() and fence is None
        if blank:
            if start is not None:
                spans.append((start, i - 1))
                start = None
        elif start is None:
            start = i
    if start is not None:
        spans.append((start, len(lines) - 1))
    return spans


def _explode(lines: list[str], span: tuple[int, int], budget: int) -> list[tuple[int, int]]:
    """Split one block larger than ``budget`` into line spans that each fit."""
    out: list[tuple[int, int]] = []
    first = span[0]
    size = 0
    for i in range(span[0], span[1] + 1):
        n = len(lines[i]) + 1
        if size and size + n > budget:
            out.append((first, i - 1))
            first, size = i, 0
        size += n
    out.append((first, span[1]))
    return out


def split_windows(text: str, start_line: int, budget: int, overlap: int) -> list[Window]:
    """Pack paragraph/table blocks into windows of at most ~``budget`` chars.

    ``start_line`` is the file line of ``text``'s first line, so each window carries the
    real line span it covers. ``overlap`` chars of trailing context are repeated at the
    head of the next window. A single line longer than ``budget`` is hard-split by chars
    (all pieces keep that line number).
    """
    lines = text.split("\n")
    spans: list[tuple[int, int]] = []
    for sp in _blocks(lines):
        size = sum(len(lines[i]) + 1 for i in range(sp[0], sp[1] + 1))
        spans.extend([sp] if size <= budget else _explode(lines, sp, budget))

    def size_of(sp: tuple[int, int]) -> int:
        return sum(len(lines[i]) + 1 for i in range(sp[0], sp[1] + 1))

    groups: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    cur_size = 0
    for sp in spans:
        n = size_of(sp) + 1  # + blank separator
        if cur and cur_size + n > budget:
            groups.append(cur)
            carry: list[tuple[int, int]] = []
            carried = 0
            for prev in reversed(cur):
                pn = size_of(prev) + 1
                if carried + pn > overlap or carried + pn + n > budget:
                    break
                carry.insert(0, prev)
                carried += pn
            cur, cur_size = carry, carried
        cur.append(sp)
        cur_size += n
    if cur:
        groups.append(cur)

    windows: list[Window] = []
    for g in groups:
        first, last = g[0][0], g[-1][1]
        body = "\n".join(lines[first : last + 1]).strip("\n")
        # A lone over-long line: hard-split so nothing exceeds the budget.
        pieces = [body[k : k + budget] for k in range(0, len(body), budget)] if len(body) > budget else [body]
        for piece in pieces:
            if piece.strip():
                windows.append(Window(piece, start_line + first, start_line + last))
    return windows


def plan_section(
    sections: list[Section],
    i: int,
    budget: int,
    overlap: int,
    clean: Callable[[str], str],
) -> SectionPlan | None:
    """Return a plan when ``sections[i]`` exceeds ``budget`` once cleaned, else ``None``."""
    breadcrumb, _level, text, start, _end = sections[i]
    cleaned = clean(text)
    if len(cleaned) <= budget:
        return None
    children = children_of(sections, i)
    if children:
        return SectionPlan("rollup", rollup_embed_text(sections[i], children, budget, clean))
    # Leave room for the breadcrumb prefix the part rows carry.
    window_budget = max(budget - len(breadcrumb) - 2, budget // 2)
    wins = split_windows(text, start, window_budget, overlap)
    if len(wins) <= 1:
        return SectionPlan("leaf", _cut(cleaned, budget))
    return SectionPlan("leaf", clean(wins[0].text), wins[1:])
