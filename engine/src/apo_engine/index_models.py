"""Shared index and retrieval value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IndexStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    chunks: int = 0
    seconds: float = 0.0


@dataclass
class Hit:
    path: str
    heading: str
    text: str
    score: float
    chunk_hash: str = ""
    heading_level: int = 0
    start_line: int = 0
    end_line: int = 0
    source: str = ""
    mtime: float = 0.0
    file_bytes: int = 0
    section_bytes: int = 0
    content_hash: str = ""
    chunk_kind: str = "section"
    row_key: str = ""
    table_id: str = ""
    explain: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueueStats:
    purged: int = 0
    indexed: int = 0
    vault_stats: IndexStats | None = None
