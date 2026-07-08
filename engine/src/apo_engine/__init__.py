"""apo engine — local semantic search over a markdown vault.

The "memsearch" module of the Apo KB gateway: embedding + sqlite-vec index + search.
Clean-room personal reimplementation from the author's own vault spec.
Embedded (sqlite-vec), no Docker; embeddings via Ollama (GPU) or fastembed (CPU).
"""

__version__ = "0.1.0"
