"""Per-request session identity for MCP/RPC (conversation_id in the wire layer)."""

from __future__ import annotations

import contextvars
import os
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

META_CONVERSATION_ID = "apo/conversation_id"
META_GENERATION_ID = "apo/generation_id"

# Last-resort session identity.
#
# Clients are supposed to supply a conversation id via MCP ``_meta`` or an
# ``_apo`` arg block, but Claude Code sends neither and the only shipped
# injector is a Cursor hook. The result was conversation_id NULL on 100% of
# recorded calls, which made every per-session analysis impossible.
#
# Under stdio transport Apo is spawned as one subprocess per client session,
# so the process *is* the session. That makes a process-scoped id a correct
# (not merely convenient) fallback there.
#
# CAVEAT: this equivalence does not hold for a long-lived HTTP/SSE server
# shared by several clients. Explicit ``_meta`` / ``_apo`` ids always win, and
# APO_SESSION_ID overrides for callers that manage their own identity.
def _initial_session_id() -> str:
    """Env-supplied id when the caller manages its own identity, else generated."""
    return os.environ.get("APO_SESSION_ID", "").strip() or f"proc-{uuid.uuid4().hex[:12]}"


_PROCESS_SESSION_ID = _initial_session_id()

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
        "read_note",
        "write_note",
        "append_note",
        "patch_note",
        "filter_notes",
        "backlinks",
        "history",
        "vault",
        "apo_admin",
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


def process_session_id() -> str:
    """Stable id for this engine process — the stdio-session fallback."""
    return _PROCESS_SESSION_ID


def request_conversation_id() -> str | None:
    """Active request conversation id (MCP/RPC layer), else legacy fallbacks.

    Resolution order: per-request contextvar, then the legacy env/file
    fallback, then this process's id. Never returns None in practice, which is
    the point — an unattributed call is an unanalysable call.
    """
    cid = (_conversation_id.get() or "").strip()
    if cid:
        return cid
    from apo_engine import telemetry_contract as tc

    legacy = tc.conversation_id_from_env()
    if legacy:
        return legacy
    return _PROCESS_SESSION_ID


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
