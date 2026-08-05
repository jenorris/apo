"""Per-request session identity for MCP/RPC (conversation_id in the wire layer)."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

META_CONVERSATION_ID = "apo/conversation_id"
META_GENERATION_ID = "apo/generation_id"

_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apo_conversation_id", default=None
)
_generation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "apo_generation_id", default=None
)

# Cursor MCP tool names (server-agnostic — inject via preToolUse on these names).
APO_MCP_TOOL_NAMES = frozenset(
    {
        "search_notes",
        "expand_chunk",
        "read_note",
        "write_note",
        "append_note",
        "patch_note",
        "place_note",
        "filter_notes",
        "backlinks",
        "history",
        "vault",
        "session_stats",
        "active_session",
        "reload_config",
        "memory_status",
        "reindex_deferred",
        "reindex",
        "delete_note",
        "tool_stats",
        "git_sync",
    }
)


def _meta_dict(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return meta
    model_dump = getattr(meta, "model_dump", None)
    if callable(model_dump):
        try:
            raw = model_dump(by_alias=True)
            return raw if isinstance(raw, dict) else {}
        except TypeError:
            raw = model_dump()
            return raw if isinstance(raw, dict) else {}
    return {}


def _apo_block(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def extract_session_fields(
    *,
    meta: Any = None,
    arguments: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Read conversation/generation ids from MCP _meta, _apo args, or RPC body."""
    m = _meta_dict(meta)
    cid = str(m.get(META_CONVERSATION_ID) or m.get("conversation_id") or "").strip()
    gid = str(m.get(META_GENERATION_ID) or m.get("generation_id") or "").strip()

    args = arguments if isinstance(arguments, dict) else {}
    apo = _apo_block(args.get("_apo"))
    if not cid:
        cid = str(apo.get("conversation_id") or apo.get("session_id") or "").strip()
    if not gid:
        gid = str(apo.get("generation_id") or "").strip()

    if body and isinstance(body, dict):
        top_apo = _apo_block(body.get("_apo"))
        if not cid:
            cid = str(
                top_apo.get("conversation_id")
                or body.get("conversation_id")
                or ""
            ).strip()
        if not gid:
            gid = str(top_apo.get("generation_id") or body.get("generation_id") or "").strip()
        top_meta = _meta_dict(body.get("_meta"))
        if not cid:
            cid = str(
                top_meta.get(META_CONVERSATION_ID)
                or top_meta.get("conversation_id")
                or ""
            ).strip()
        if not gid:
            gid = str(
                top_meta.get(META_GENERATION_ID)
                or top_meta.get("generation_id")
                or ""
            ).strip()

    return cid or None, gid or None


def strip_session_payload(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Remove _apo transport keys before tool validation."""
    if not isinstance(arguments, dict):
        return {}
    if "_apo" not in arguments:
        return arguments
    out = dict(arguments)
    out.pop("_apo", None)
    return out


def strip_session_body(body: dict[str, Any]) -> dict[str, Any]:
    """Remove _apo / wire-only keys from RPC JSON bodies."""
    if "_apo" not in body and "_meta" not in body:
        return body
    out = dict(body)
    out.pop("_apo", None)
    out.pop("_meta", None)
    return out


def request_conversation_id() -> str | None:
    """Active request conversation id (MCP/RPC layer), else legacy fallbacks."""
    cid = (_conversation_id.get() or "").strip()
    if cid:
        return cid
    from apo_engine import telemetry_contract as tc

    return tc.conversation_id_from_env()


def request_generation_id() -> str | None:
    gid = (_generation_id.get() or "").strip()
    return gid or None


@contextmanager
def bind_request_session(
    *,
    meta: Any = None,
    arguments: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    generation_id: str | None = None,
) -> Iterator[None]:
    """Set per-request session ids for metrics (safe under concurrent MCP calls)."""
    cid, gid = extract_session_fields(meta=meta, arguments=arguments, body=body)
    cid = (conversation_id or cid or "").strip() or None
    gid = (generation_id or gid or "").strip() or None
    tok_c = _conversation_id.set(cid)
    tok_g = _generation_id.set(gid)
    try:
        yield
    finally:
        _conversation_id.reset(tok_c)
        _generation_id.reset(tok_g)
