"""MCP / ops search hit shaping — thin adapter over core's sync sqlite-vec index.

Index writes are owned by apo-engine watch — writers enqueue via apo_engine.deferred.
"""

from __future__ import annotations

from datetime import datetime

from . import core


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
        rows.append(
            {
                "content": h.text,
                "score": round(float(h.score), 4),
                # ``Hit.path`` is already vault-relative; skip resolve/display.
                "source": h.path,
                "chunk_hash": h.chunk_hash,
                "heading": h.heading,
                "heading_level": h.heading_level,
                "start_line": h.start_line,
                "end_line": h.end_line,
                "modified": modified,
                # Float epoch for expected_mtime on follow-up writes (ISO modified stays).
                "mtime": float(mtime) if mtime else None,
            }
        )
    return rows
