"""MCP backend — sqlite-vec index adapter matching memsearch MCP expectations.

Read paths (search, count, lookup) run in worker threads. Index writes are owned by
apo-engine watch — MCP enqueues via apo_engine.deferred instead of calling index_file here.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from . import config, core


class ApoStore:
    def count(self) -> int:
        return core.count_chunks()

    def query(self, filter_expr: str) -> list[dict]:
        m = re.search(r'chunk_hash\s*==\s*"([^"]+)"', filter_expr)
        if not m:
            return []
        row = core.lookup_chunk(m.group(1))
        return [row] if row else []

    def delete_by_source(self, source: str) -> None:
        core.purge_source(Path(source))


class ApoMem:
    """Drop-in for memsearch.MemSearch in the ported MCP server."""

    store = ApoStore()

    def __init__(self, root: Path):
        self.root = root.resolve()

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
                }
            )
        return rows

    async def index_file(self, path: str | Path) -> None:
        await asyncio.to_thread(core.index_file, Path(path), verbose=False)

    async def index(self, force: bool = False) -> int:
        stats = await asyncio.to_thread(core.index_vault, rebuild=force, verbose=False)
        return stats.added + stats.changed
