"""Configuration — all via environment, with personal-friendly defaults."""
from __future__ import annotations

import os
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[2]  # ~/Code/apo/engine


def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default)).expanduser().resolve()


# Vault to index. Defaults to the Meta Obsidian vault.
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
