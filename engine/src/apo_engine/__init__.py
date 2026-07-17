"""apo engine — local semantic search over a markdown vault.

Embedding + sqlite-vec index + hybrid (vector + FTS5 BM25) search over your
Markdown files. Embedded (sqlite-vec), no Docker; embeddings via Ollama (GPU)
or fastembed (CPU).
"""

__version__ = "0.1.0"
