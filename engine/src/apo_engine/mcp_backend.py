"""MCP / ops search hit shaping — thin adapter over core's sync sqlite-vec index.

Index writes are owned by apo-engine watch — writers enqueue via apo_engine.deferred.
"""

from __future__ import annotations

from datetime import datetime

from apo_engine.core import _content_hash
from . import core

# Flattened row snippets stay short — full-row bloat is what we're avoiding.
_ROW_SNIPPET_CHARS = 240


class ApoStore:
    def count(self) -> int:
        return core.count_chunks()

    def lookup_chunk(self, chunk_hash: str, *, include_text: bool = True) -> dict | None:
        return core.lookup_chunk(chunk_hash, include_text=include_text)


def shape_search_hits(
    hits: list[core.Hit],
) -> list[dict]:
    """Vault-relative rows for MCP / RPC — no Path.resolve() on the event loop."""
    rows: list[dict] = []
    for h in hits:
        mtime = h.mtime or 0.0
        modified = (
            datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None
        )
        chunk_kind = getattr(h, "chunk_kind", "section") or "section"
        # Prefer the index full-chunk content_hash (write precondition). Fall back to
        # hashing the returned text only for legacy rows indexed before the column.
        content_hash = getattr(h, "content_hash", "") or (
            _content_hash(h.text) if h.text else None
        )
        content = h.text
        if chunk_kind == "table_row" and content:
            content = core._truncate_word_boundary(content, _ROW_SNIPPET_CHARS)
        row = {
            "content": content,
            "score": round(float(h.score), 4),
            "source": h.path,
            "chunk_hash": h.chunk_hash,
            "heading": h.heading,
            "heading_level": h.heading_level,
            "modified": modified,
            "mtime": float(mtime) if mtime else None,
            "content_hash": content_hash,
            "file_bytes": int(getattr(h, "file_bytes", 0) or 0),
            "section_bytes": int(getattr(h, "section_bytes", 0) or 0),
        }
        # Table awareness — lean: kind + row_key + table_id (no full columns map).
        if chunk_kind != "section":
            row["chunk_kind"] = chunk_kind
            row_key = getattr(h, "row_key", "") or ""
            if row_key:
                row["row_key"] = row_key
            table_id = getattr(h, "table_id", "") or ""
            if table_id:
                row["table_id"] = table_id
        rows.append(row)
    return rows
