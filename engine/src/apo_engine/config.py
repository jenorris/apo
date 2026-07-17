"""Configuration — all via environment, with sensible local defaults."""
from __future__ import annotations

import os
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[2]  # <repo>/engine


def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default)).expanduser().resolve()


# Vault to index.
NOTES_ROOT: Path = _path("APO_NOTES_ROOT", "~/Notes")

# Single-file sqlite-vec index (rebuildable, git-ignored).
INDEX_PATH: Path = _path("APO_INDEX", str(_ENGINE_ROOT / "index.db"))

# Embedding backend: "ollama" (Metal/GPU, default) or "fastembed" (ONNX).
EMBED_BACKEND: str = os.environ.get("APO_EMBED_BACKEND", "ollama").lower()

# Model. Defaults differ per backend — vectors are NOT interchangeable across models.
_DEFAULT_MODEL = {
    "ollama": "bge-m3",
    "fastembed": "BAAI/bge-large-en-v1.5",
}
MODEL_NAME: str = os.environ.get("APO_MODEL", _DEFAULT_MODEL.get(EMBED_BACKEND, "bge-m3"))

# Ollama endpoint (required when EMBED_BACKEND=ollama).
OLLAMA_URL: str = os.environ.get("APO_OLLAMA_URL", "http://localhost:11434").rstrip("/")

# Chunking knobs.
MAX_CHARS: int = int(os.environ.get("APO_MAX_CHARS", "1200"))
OVERLAP: int = int(os.environ.get("APO_OVERLAP", "150"))

# Ignore-file (globs relative to NOTES_ROOT).
IGNORE_FILE: Path = _path("APO_IGNORE", str(_ENGINE_ROOT / ".indexignore"))

# Deferred-queue namespace (MCP + watcher).
COLLECTION: str = os.environ.get("APO_COLLECTION", "notes_global")

# Default wiki path convention for agents (defuddle → write_note); not an MCP tool knob.
INGEST_DIR: str = os.environ.get("APO_INGEST_DIR", "resources/wiki")

# SQLite busy-handler timeout (seconds) — cross-process writer contention.
DB_TIMEOUT: float = float(os.environ.get("APO_DB_TIMEOUT", "30"))

# Watcher: prefer filesystem events over poll-only scan.
WATCH_USE_EVENTS: bool = os.environ.get("APO_WATCH_EVENTS", "1").lower() not in ("0", "false", "no")

# Fallback poll interval when events are inactive (seconds).
WATCH_POLL_INTERVAL: float = float(os.environ.get("WATCH_INTERVAL", "30"))

# Full-vault reconcile when fsevents are on (seconds). Defaults to 5 minutes —
# day-to-day indexing is event + deferred driven; the walk is a safety net only.
WATCH_RECONCILE_INTERVAL: float = float(
    os.environ.get("WATCH_RECONCILE_INTERVAL", "300")
)

# Coalesce rapid saves / deferred enqueues before embedding (seconds of quiet).
WATCH_DEBOUNCE: float = float(os.environ.get("APO_WATCH_DEBOUNCE", "2"))

# How often to ping a cached reader connection (seconds). 0 = every call.
READER_PING_INTERVAL: float = float(os.environ.get("APO_READER_PING", "5"))

# Hybrid search candidate pool floor (per retriever). Overridden via APO_SEARCH_CANDIDATES.
SEARCH_CANDIDATES: int = int(os.environ.get("APO_SEARCH_CANDIDATES", "24"))

# Folder-scoped vector scan: above this chunk count, hybrid search scores FTS hits only
# (avoids O(folder) Python L2 on huge folders). Override via APO_SCOPED_VECTOR_FULL_SCAN_MAX.
SCOPED_VECTOR_FULL_SCAN_MAX: int = int(os.environ.get("APO_SCOPED_VECTOR_FULL_SCAN_MAX", "500"))

# Cache identical query embeddings (seconds TTL; 0 disables).
QUERY_EMBED_TTL: float = float(os.environ.get("APO_QUERY_EMBED_TTL", "120"))
QUERY_EMBED_CACHE_SIZE: int = int(os.environ.get("APO_QUERY_EMBED_CACHE", "64"))
