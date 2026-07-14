"""MCP backend — async adapter over core's sync sqlite-vec index.

Read paths (search, count, lookup) run in worker threads. Index writes are owned by
apo-engine watch — MCP enqueues via apo_engine.deferred instead of calling index_file here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import config, core


class ApoStore:
    def count(self) -> int:
        return core.count_chunks()

    def lookup_chunk(self, chunk_hash: str) -> dict | None:
        return core.lookup_chunk(chunk_hash)


class ApoMem:
    store = ApoStore()

    async def search(
        self,
        query: str,
        top_k: int = 5,
        source_prefix: str | None = None,
    ) -> list[dict]:
        folder = ""
        if source_prefix:
            try:
                folder = Path(source_prefix).resolve().relative_to(config.NOTES_ROOT).as_posix()
            except ValueError:
                folder = source_prefix
        hits = await asyncio.to_thread(core.search, query, k=top_k, folder=folder)
        rows: list[dict] = []
        for h in hits:
            rows.append(
                {
                    "content": h.text,
                    "score": h.score,
                    "source": h.source or str(config.NOTES_ROOT / h.path),
                    "chunk_hash": h.chunk_hash,
                    "heading": h.heading,
                    "heading_level": h.heading_level,
                    "start_line": h.start_line,
                    "end_line": h.end_line,
                    "mtime": h.mtime,
                }
            )
        return rows
