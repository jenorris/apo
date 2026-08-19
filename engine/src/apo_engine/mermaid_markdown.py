"""Extract fenced ```mermaid blocks from markdown bodies."""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE_OPEN_RE = re.compile(r"^\s*(```|~~~)\s*mermaid\s*$", re.I)


@dataclass
class MermaidFence:
    """A fenced mermaid block within a markdown file."""

    text: str
    start_line: int  # 0-based index into line list (opening fence)
    end_line: int  # 0-based inclusive (closing fence)
    block_index: int  # 0-based ordinal within document


def find_mermaid_fences(lines: list[str]) -> list[MermaidFence]:
    """Return every ```mermaid … ``` block, skipping nested fences."""
    blocks: list[MermaidFence] = []
    fence: str | None = None
    in_mermaid = False
    buf: list[str] = []
    start = 0
    block_index = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if not in_mermaid:
            if _FENCE_OPEN_RE.match(line):
                fence = stripped[:3]
                in_mermaid = True
                start = i
                buf = []
                i += 1
                continue
            i += 1
            continue
        # Inside mermaid fence — close on matching fence token
        if stripped.startswith("```") or stripped.startswith("~~~"):
            token = stripped[:3]
            if token == fence:
                blocks.append(
                    MermaidFence(
                        text="\n".join(buf),
                        start_line=start,
                        end_line=i,
                        block_index=block_index,
                    )
                )
                block_index += 1
                in_mermaid = False
                fence = None
                buf = []
                i += 1
                continue
        buf.append(line)
        i += 1
    return blocks
