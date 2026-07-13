"""Configuration — all via environment, with personal-friendly defaults."""
from __future__ import annotations

import os
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[2]  # ~/Code/apo/engine


def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default)).expanduser().resolve()


# Vault to index.
NOTES_ROOT: Path = _path("APO_NOTES_ROOT", "~/Notes")

# Single-file sqlite-vec index (rebuildable, git-ignored).
INDEX_PATH: Path = _path("APO_INDEX", str(_ENGINE_ROOT / "index.db"))

# Embedding backend: "ollama" (GPU, default) or "fastembed" (CPU ONNX fallback).
EMBED_BACKEND: str = os.environ.get("APO_EMBED_BACKEND", "ollama").lower()

# Model. Defaults differ per backend (same underlying bge-m3).
_DEFAULT_MODEL = {"ollama": "bge-m3", "fastembed": "BAAI/bge-m3"}
MODEL_NAME: str = os.environ.get("APO_MODEL", _DEFAULT_MODEL.get(EMBED_BACKEND, "bge-m3"))

# Ollama endpoint (bundles its own CUDA — runs bge-m3 on the local GPU).
OLLAMA_URL: str = os.environ.get("APO_OLLAMA_URL", "http://localhost:11434").rstrip("/")

# Chunking knobs.
MAX_CHARS: int = int(os.environ.get("APO_MAX_CHARS", "1200"))
OVERLAP: int = int(os.environ.get("APO_OVERLAP", "150"))

# Ignore-file (globs relative to NOTES_ROOT).
IGNORE_FILE: Path = _path("APO_IGNORE", str(_ENGINE_ROOT / ".indexignore"))

# Deferred-queue namespace (MCP + watcher).
COLLECTION: str = os.environ.get("APO_COLLECTION", "notes_global")

# Default vault-relative dir for ingest_uri when dest_dir is omitted.
INGEST_DIR: str = os.environ.get("APO_INGEST_DIR", "resources/wiki")

# SQLite busy-handler timeout (seconds) — cross-process writer contention.
DB_TIMEOUT: float = float(os.environ.get("APO_DB_TIMEOUT", "30"))

# Watcher: prefer filesystem events over poll-only scan.
WATCH_USE_EVENTS: bool = os.environ.get("APO_WATCH_EVENTS", "1").lower() not in ("0", "false", "no")

# Fallback poll interval when events are active (seconds).
WATCH_POLL_INTERVAL: float = float(os.environ.get("WATCH_INTERVAL", "30"))

# Coalesce rapid saves / deferred enqueues before embedding (seconds of quiet).
WATCH_DEBOUNCE: float = float(os.environ.get("APO_WATCH_DEBOUNCE", "2"))

# Hybrid search candidate pool floor (per retriever). Overridden via APO_SEARCH_CANDIDATES.
SEARCH_CANDIDATES: int = int(os.environ.get("APO_SEARCH_CANDIDATES", "24"))

# Cache identical query embeddings (seconds TTL; 0 disables).
QUERY_EMBED_TTL: float = float(os.environ.get("APO_QUERY_EMBED_TTL", "120"))
QUERY_EMBED_CACHE_SIZE: int = int(os.environ.get("APO_QUERY_EMBED_CACHE", "64"))
