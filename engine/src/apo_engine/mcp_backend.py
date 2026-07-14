"""MCP backend — async adapter over core's sync sqlite-vec index.

Read paths (search, count, lookup) run in worker threads. Index writes are owned by
apo-engine watch — MCP enqueues via apo_engine.deferred instead of calling index_file here.
"""

from __future__ import annotations

import asyncio
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
    """Vault-relative rows for MCP — no Path.resolve() on the event loop."""
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
            }
        )
    return rows


class ApoMem:
    store = ApoStore()

    async def search(
        self,
        query: str,
        top_k: int = 5,
        folder: str = "",
        snippet_chars: int = 0,
    ) -> list[dict]:
        def run() -> list[dict]:
            hits = core.search(
                query,
                k=top_k,
                folder=folder,
                snippet_chars=snippet_chars,
            )
            return shape_search_hits(hits)

        return await asyncio.to_thread(run)
