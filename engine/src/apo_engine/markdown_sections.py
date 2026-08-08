"""Single source of truth for markdown section boundaries.

Both the indexer ([`core.section_markdown`]) and the patch engine
([`markdown_patch.parse_sections`] / `section_from_chunk`) build on the spans
produced here so that an index hit, a read, and a patch resolve to the *same*
byte range. The previous split had the indexer treat a section hierarchically
(a `##` owns its nested `###` bodies) while the patch engine treated every
heading as a flat sibling — so a `chunk_hash` from search resolved to a smaller
span on write than it did on read.

Boundaries are hierarchical: a heading's section runs until the next heading of
the *same or higher* level (lower or equal ``#`` count). Level 0 is the preamble
before the first heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

BREADCRUMB_SEP = " › "


@dataclass(frozen=True)
class Heading:
    line: int  # 0-based index into the supplied lines
    level: int
    title: str


@dataclass
class SectionSpan:
    """One hierarchical section.

    Line indices are 0-based into the ``lines`` passed to :func:`split_sections`.
    ``heading_line`` is -1 for the preamble. ``body_start`` is the first content
    line after the heading; ``body_end`` is exclusive.
    """

    level: int
    title: str
    breadcrumb: list[str]
    heading_line: int
    body_start: int
    body_end: int
    ancestors: list[Heading] = field(default_factory=list)


def find_headings(lines: list[str]) -> list[Heading]:
    """Return every ATX heading, skipping fenced code blocks."""
    headings: list[Heading] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            token = stripped[:3]
            if fence is None:
                fence = token
            elif fence == token:
                fence = None
            continue
        if fence is not None:
            continue
        m = HEADING_RE.match(line)
        if m:
            headings.append(Heading(line=i, level=len(m.group(1)), title=m.group(2).strip()))
    return headings


def hierarchical_end(headings: list[Heading], i: int, n_lines: int) -> int:
    """Exclusive line index where ``headings[i]``'s hierarchical section ends."""
    level = headings[i].level
    for j in range(i + 1, len(headings)):
        if headings[j].level <= level:
            return headings[j].line
    return n_lines


def breadcrumb_for(headings: list[Heading], i: int) -> list[str]:
    """Ancestor titles + this heading's title, nearest-root first."""
    trail: list[str] = []
    level = headings[i].level
    for j in range(i, -1, -1):
        if headings[j].level < level:
            trail.append(headings[j].title)
            level = headings[j].level
        if level == 1:
            break
    trail.reverse()
    trail.append(headings[i].title)
    return trail


def _ancestors(headings: list[Heading], i: int) -> list[Heading]:
    out: list[Heading] = []
    level = headings[i].level
    for j in range(i - 1, -1, -1):
        if headings[j].level < level:
            out.append(headings[j])
            level = headings[j].level
        if level == 1:
            break
    out.reverse()
    return out


def split_sections(lines: list[str]) -> list[SectionSpan]:
    """Return hierarchical section spans, including a level-0 preamble if present."""
    headings = find_headings(lines)
    spans: list[SectionSpan] = []
    n = len(lines)

    if not headings:
        if any(l.strip() for l in lines):
            spans.append(
                SectionSpan(
                    level=0,
                    title="",
                    breadcrumb=[],
                    heading_line=-1,
                    body_start=0,
                    body_end=n,
                )
            )
        return spans

    if headings[0].line > 0 and any(l.strip() for l in lines[: headings[0].line]):
        spans.append(
            SectionSpan(
                level=0,
                title="",
                breadcrumb=[],
                heading_line=-1,
                body_start=0,
                body_end=headings[0].line,
            )
        )

    for i, h in enumerate(headings):
        spans.append(
            SectionSpan(
                level=h.level,
                title=h.title,
                breadcrumb=breadcrumb_for(headings, i),
                heading_line=h.line,
                body_start=h.line + 1,
                body_end=hierarchical_end(headings, i, n),
                ancestors=_ancestors(headings, i),
            )
        )
    return spans
