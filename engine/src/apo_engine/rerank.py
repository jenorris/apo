"""Optional cross-encoder reranker over hybrid search candidates.

Opt-in via ``APO_RERANK=1`` (requires the ``rerank`` extra — fastembed ONNX
cross-encoder). The fused BM25+vector pool (``APO_RERANK_POOL`` candidates) is
rescored query-vs-chunk-text and reordered before the cut to ``k``.

First use downloads the model to the local HuggingFace cache; scoring is
CPU-only and local. Any failure (missing extra, model load, runtime) falls
back to the fused order and reports a status detail — never an exception.
"""
from __future__ import annotations

import threading
from typing import Any

from . import config

_lock = threading.Lock()
_encoder: Any = None
_encoder_model: str | None = None


def _get_encoder() -> tuple[Any, str | None]:
    """Load (once) and cache the cross-encoder. Returns (encoder, error_detail)."""
    global _encoder, _encoder_model
    with _lock:
        if _encoder is not None and _encoder_model == config.RERANK_MODEL:
            return _encoder, None
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError:
            return None, (
                "fastembed not installed — pip install -e '.[rerank]' "
                "(or unset APO_RERANK)"
            )
        try:
            enc = TextCrossEncoder(model_name=config.RERANK_MODEL)
        except Exception as e:  # model download/load — many failure shapes
            return None, f"rerank model {config.RERANK_MODEL!r} failed to load: {e}"
        _encoder = enc
        _encoder_model = config.RERANK_MODEL
        return _encoder, None


def rerank_hits(
    query: str,
    hits: list[Any],
    k: int,
    *,
    texts: list[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Reorder ``hits`` by cross-encoder score; cut to ``k``.

    ``texts`` supplies full (non-snippet) chunk bodies aligned with ``hits``.
    Returns (hits, status) where status = {"applied": bool, "detail": str}.
    On any failure the fused order is preserved (cut to ``k``).
    """
    fallback = hits[:k] if k > 0 else hits
    if len(hits) < 2:
        return fallback, {"applied": False, "detail": ""}
    enc, err = _get_encoder()
    if enc is None:
        return fallback, {"applied": False, "detail": err or "rerank unavailable"}

    docs = texts if texts is not None and len(texts) == len(hits) else [h.text for h in hits]
    try:
        scores = [float(s) for s in enc.rerank(query, docs)]
    except Exception as e:
        return fallback, {"applied": False, "detail": f"rerank scoring failed: {e}"}
    if len(scores) != len(hits):
        return fallback, {
            "applied": False,
            "detail": f"rerank returned {len(scores)} scores for {len(hits)} candidates",
        }

    order = sorted(range(len(hits)), key=lambda i: scores[i], reverse=True)
    lo, hi = min(scores), max(scores)
    span = hi - lo
    reordered = []
    for i in order:
        h = hits[i]
        # Preserve the Hit.score contract: normalized, 1.0 = top, monotonic with rank.
        h.score = round((scores[i] - lo) / span, 4) if span > 0 else 1.0
        reordered.append(h)
    out = reordered[:k] if k > 0 else reordered
    return out, {"applied": True, "detail": f"model={config.RERANK_MODEL}"}
