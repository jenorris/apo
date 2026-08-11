#!/usr/bin/env python3
"""
Apo MCP server — FastMCP façade over apo_engine.ops (+ admin/resources).

Vault: APO_NOTES_ROOT. Deferred queue: ~/.apo/deferred-<collection>.json
"""

import asyncio
import json
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field
from apo_engine import apo_admin as apo_admin_ops
from apo_engine import config as apo_config
from apo_engine import deferred as index_deferred
from apo_engine import ops as apo_ops
from apo_engine import vaults as apo_vaults
from apo_engine.mcp_backend import ApoStore
from apo_engine.patch_ops import OPS_FIELD_DESC, PATCH_NOTES_ITEMS_DESC, PatchNotesItem, PatchOp

# Tool annotation presets
_RO = {"readOnlyHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_MUTATE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

# Default MCP search payload: anchors + short preview. Pass snippet_chars=0 for full chunk text.
_DEFAULT_SEARCH_SNIPPET = 240


class VaultError(Exception):
    pass


@dataclass
class Vault:
    name: str
    root: Path
    collection: str
    index_path: Path
    ingest_dir: str = "wiki"
    deferred: set[str] = dc_field(default_factory=set)

    def binding(self) -> apo_vaults.VaultBinding:
        return apo_vaults.VaultBinding(
            name=self.name,
            root=self.root,
            index=self.index_path,
            collection=self.collection,
        )


VAULTS: dict[str, Vault] = {}
DEFAULT_VAULT = "default"


def _runtime_config_path() -> Path:
    explicit = os.environ.get("APO_RUNTIME_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    # Per-collection override file so multiple server instances (one per vault
    # registration) never clobber each other through a shared runtime file.
    coll = (os.environ.get("APO_COLLECTION") or "").strip()
    base = Path.home() / ".apo"
    return base / (f"mcp-runtime.{coll}.json" if coll else "mcp-runtime.json")


def _read_runtime_overrides() -> dict:
    p = _runtime_config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick(overrides: dict, key: str, default: str | None = None) -> str | None:
    """Precedence: runtime JSON → process env → default."""
    raw = overrides.get(key)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    envv = os.environ.get(key)
    if envv is not None and str(envv).strip() != "":
        return str(envv).strip()
    return default


def _load_deferred(collection: str) -> set[str]:
    return index_deferred.load_index_queue(collection)


def _load_vaults() -> None:
    """(Re)build the vault registry from APO_VAULTS or legacy single-root env.

    Each vault has its own NOTES_ROOT, INDEX_PATH, and deferred COLLECTION.
    Tool calls pass ``vault=`` (name); empty uses DEFAULT_VAULT.
    """
    global VAULTS, DEFAULT_VAULT
    overrides = _read_runtime_overrides()
    ingest = (
        _pick(overrides, "APO_INGEST_DIR", apo_config.INGEST_DIR) or apo_config.INGEST_DIR
    )
    try:
        default_name, bindings = apo_vaults.load_bindings()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise VaultError(f"vault registry error: {e}") from e

    # Runtime JSON may still override collection for the *default* vault only
    # (legacy single-vault desk). Multi-vault collections come from APO_VAULTS.
    VAULTS = {}
    for name, b in bindings.items():
        coll = b.collection
        if name == default_name:
            coll = _pick(overrides, "APO_COLLECTION", coll) or coll
        VAULTS[name] = Vault(
            name=name,
            root=b.root,
            collection=coll,
            index_path=b.index,
            ingest_dir=ingest,
            deferred=_load_deferred(coll),
        )
    DEFAULT_VAULT = default_name


def _vault(name: str = "") -> Vault:
    key = (name or "").strip() or DEFAULT_VAULT
    v = VAULTS.get(key)
    if v is None:
        raise VaultError(f"unknown vault {key!r}; available: {sorted(VAULTS)}")
    return v


def _bound(v: Vault):
    """Context manager: activate this vault's root+index for core.* calls."""
    return apo_vaults.bind(v.binding())


def _safe_resolve(v: Vault, relative_path: str) -> Path:
    """Resolve a vault-relative path and assert it stays within the vault root."""
    full = (v.root / relative_path).resolve()
    full.relative_to(v.root)  # raises ValueError on traversal
    return full


def _err(**kw: Any) -> dict:
    return {"ok": False, **kw}


def _top_level_dirs(v: Vault) -> list[str]:
    if not v.root.exists():
        return []
    return sorted(p.name for p in v.root.iterdir() if p.is_dir() and not p.name.startswith("."))


###############################################################################
# Server
###############################################################################

_MCP_INSTRUCTIONS = (
    "Apo: vault-relative Markdown + YAML catalog notes; sqlite-vec hybrid search; "
    "files are source of truth. "
    "apo_admin(action=list|describe|invoke): engine ops (memory_status, reindex, "
    "delete_note, reload_config, git_sync). Destructive invoke requires "
    "confirm=true (delete_note always; reindex force=true; git_sync run/pull/rebase). "
    "vault(action=list|contracts|describe|merge|project|stats): registry + contracts + "
    "optional habit KPIs (stats). "
    "Routing: write_note=create/overwrite (content=); "
    "append_note=session log / post-search add (text=); "
    "patch_note=frontmatter/section mutate or place op (move/copy); "
    "search_notes(limit=, folder= or folders=[]); filter_notes(where=); "
    "read_note(path= or chunk_hash= from search hits); "
    "Thread mtime → expected_mtime on follow-up writes. "
    "Operator traces: otlp-mcp + Jaeger (not Apo MCP). "
    "Multi-vault: vault= or search_notes(vaults=[])."
)
mcp = FastMCP("Apo", instructions=_MCP_INSTRUCTIONS)

# FastMCP wraps middleware with reversed(list): first added = outermost.
# Metrics must sit *outside* validation rewrite so schema rejects are recorded as
# validation_error + error_shape (not raw ValidationError with empty shapes).
# Inner: rewrite opaque Pydantic ValidationError → agent-actionable ToolError
# (FastMCP validates args before tool bodies — see apo_engine.validation_hints).
from apo_engine.agent_validation import AgentValidationMiddleware  # noqa: E402

_load_vaults()


def _metrics_vault_for_args(args: dict[str, Any]) -> tuple[str, Path | None, str]:
    """Resolve (vault_id, vault_root, collection) for ToolMetricsMiddleware."""
    key = str(args.get("vault") or "").strip() or DEFAULT_VAULT
    v = VAULTS.get(key)
    if v is not None:
        return v.name, v.root, v.collection
    if VAULTS:
        dv = VAULTS.get(DEFAULT_VAULT)
        if dv is not None:
            return dv.name, dv.root, dv.collection
    root_s = os.environ.get("APO_NOTES_ROOT", "").strip()
    coll = (os.environ.get("APO_COLLECTION") or "").strip() or key or "default"
    return key, Path(root_s).expanduser() if root_s else None, coll


from apo_engine.tool_metrics_middleware import ToolMetricsMiddleware  # noqa: E402
from apo_engine.session_context_middleware import SessionContextMiddleware  # noqa: E402

mcp.add_middleware(SessionContextMiddleware())
mcp.add_middleware(
    ToolMetricsMiddleware(vault_resolver=_metrics_vault_for_args)
)
mcp.add_middleware(AgentValidationMiddleware())

# Index backend connects lazily per vault (registry loaded above).


###############################################################################
# Admin ops (invoked via apo_admin — not top-level MCP tools)
###############################################################################


def _reload_config_sync() -> dict:
    _load_vaults()
    # Nudge multi-vault watcher to hot-add any new vaults from APO_VAULTS.
    try:
        from apo_engine import deferred as index_deferred

        index_deferred.touch_registry_wake()
    except Exception:
        pass
    return {
        "ok": True,
        "default_vault": DEFAULT_VAULT,
        "vaults": {
            name: {
                "root": str(v.root),
                "index": str(v.index_path),
                "collection": v.collection,
                "ingest_dir": v.ingest_dir,
            }
            for name, v in VAULTS.items()
        },
        "runtime_file": str(_runtime_config_path()),
        "registry_wake": True,
    }



def _delete_note_sync(path: str, vault: str = "") -> dict:
    return apo_ops.delete_note(path, vault=vault)


def _memory_status_sync() -> dict:
    vaults: dict[str, Any] = {}
    store = ApoStore()
    for name, v in VAULTS.items():
        info: dict[str, Any] = {
            "root": str(v.root),
            "root_exists": v.root.exists(),
            "index_path": str(v.index_path),
            "collection": v.collection,
            "ingest_dir": v.ingest_dir,
            "default": name == DEFAULT_VAULT,
            "deferred_queue": len(v.deferred),
        }
        try:
            with _bound(v):
                info["indexed_chunks"] = store.count()
            info["index"] = "ok"
        except Exception as e:
            info["index"] = f"error: {e}"
        vaults[name] = info

    return {
        "ok": True,
        "default_vault": DEFAULT_VAULT,
        "vaults": vaults,
        "watcher": apo_ops.watcher_status(),
        "runtime_file": str(_runtime_config_path()),
    }


def _reindex_deferred_sync(vault: str = "") -> dict:
    try:
        targets = list(VAULTS.values()) if not vault.strip() else [_vault(vault)]
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))

    queued = 0
    for v in targets:
        index_deferred.touch_wake(v.collection)
        v.deferred = index_deferred.load_index_queue(v.collection)
        queued += len(v.deferred)

    watcher = apo_ops.watcher_status()
    out: dict[str, Any] = {
        "ok": True,
        "queued": queued,
        "signaled": True,
        "watcher_running": watcher["running"],
    }
    if not watcher["running"]:
        out["warning"] = (
            "no watcher detected — the deferred queue is signaled but nothing will consume it "
            "until apo-engine watch is running (just watch-status)"
        )
    return out


def _reindex_sync(force: bool = False, vault: str = "") -> dict:
    try:
        v = _vault(vault)
        index_deferred.signal_rebuild(v.collection, force=force)
        v.deferred.clear()
        index_deferred.save_index_queue(v.collection, set())
        watcher = apo_ops.watcher_status()
        out: dict[str, Any] = {
            "ok": True,
            "vault": v.name,
            "rebuild_signaled": True,
            "force": force,
            "watcher_running": watcher["running"],
        }
        if not watcher["running"]:
            out["warning"] = (
                "no watcher detected — the rebuild is signaled but will never run "
                "until apo-engine watch is running (just watch-status)"
            )
        return out
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    except Exception as e:
        return _err(error="reindex_failed", message=str(e))


def _git_sync_admin(params: dict[str, Any], *, vault: str = "") -> dict:
    action = str(params.get("action") or "status").strip()
    message = str(params.get("message") or "")
    v = vault or str(params.get("vault") or "")
    return apo_ops.git_sync_op(action, message=message, vault=v)


def _delete_note_admin(params: dict[str, Any], *, vault: str = "") -> dict:
    path = params.get("path")
    if not isinstance(path, str) or not path.strip():
        return _err(error="bad_request", message="parameters.path string required")
    v = vault or str(params.get("vault") or "")
    return _delete_note_sync(path.strip(), vault=v)


def _reindex_admin(params: dict[str, Any], *, vault: str = "") -> dict:
    mode = str(params.get("mode") or "rebuild").strip().lower()
    v = vault or str(params.get("vault") or "")
    if mode == "flush":
        return _reindex_deferred_sync(vault=v)
    if mode != "rebuild":
        return _err(error="bad_request", message="mode must be flush or rebuild")
    force = bool(params.get("force"))
    return _reindex_sync(force=force, vault=v)


def _reindex_deferred_legacy_admin(params: dict[str, Any], *, vault: str = "") -> dict:
    out = _reindex_deferred_sync(vault=vault or str(params.get("vault") or ""))
    if out.get("ok"):
        out = dict(out)
        out["tip"] = "reindex_deferred renamed — use reindex(mode=flush)"
    return out


def _memory_status_admin(_params: dict[str, Any], *, vault: str = "") -> dict:
    del vault
    return _memory_status_sync()


def _reload_config_admin(_params: dict[str, Any], *, vault: str = "") -> dict:
    del vault
    return _reload_config_sync()


_ADMIN_HANDLERS: dict[str, apo_admin_ops.AdminHandler] = {
    "reload_config": _reload_config_admin,
    "memory_status": _memory_status_admin,
    "reindex": _reindex_admin,
    "reindex_deferred": _reindex_deferred_legacy_admin,
    "delete_note": _delete_note_admin,
    "git_sync": _git_sync_admin,
}


###############################################################################
# Tools — writing (delegate to ops)
###############################################################################


_REGION_HASH_DESC = (
    "Region precondition (16-hex blake2b from read/expand/search). "
    "When expected_mtime is stale, a scoped write still proceeds if this hash matches "
    "the untouched frontmatter, body, or section/chunk."
)


@mcp.tool(annotations=_MUTATE)
async def write_note(
    path: str,
    content: Annotated[
        str | None,
        Field(description="Note body (required for create/overwrite). XOR with sections[]/frontmatter."),
    ] = None,
    sections: Annotated[
        list[dict] | None,
        Field(
            description=(
                "Structured body (XOR content=): [{heading, content, content_type}]. "
                "content_type is markdown (default), csv, or json/table_json — CSV/JSON "
                "serialize to a GFM table indexed as table rows."
            ),
        ),
    ] = None,
    frontmatter: Annotated[
        dict | None,
        Field(description="Frontmatter object (XOR content=); serialized to a YAML fence, then OKF-validated."),
    ] = None,
    expected_mtime: Annotated[
        float | None,
        Field(
            description=(
                "Optimistic concurrency: pass mtime from a prior read/write for this path. "
                "On stale_write, re-read and retry (or pass matching region hashes)."
            ),
        ),
    ] = None,
    expected_frontmatter_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC),
    ] = None,
    expected_body_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC),
    ] = None,
    expected_content_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC),
    ] = None,
    vault: str = "",
) -> dict:
    """Create or overwrite a note. Use content= (or sections[]/frontmatter). Prefer append_note / patch_note for edits."""
    return await asyncio.to_thread(
        apo_ops.write_note,
        path,
        content,
        sections=sections,
        frontmatter=frontmatter,
        expected_mtime=expected_mtime,
        expected_frontmatter_hash=expected_frontmatter_hash,
        expected_body_hash=expected_body_hash,
        expected_content_hash=expected_content_hash,
        vault=vault,
    )


@mcp.tool(annotations=_WRITE)
async def append_note(
    text: Annotated[
        str | None,
        Field(description="Body to append (required). Do not repeat the heading."),
    ] = None,
    path: str = "",
    heading: str | None = None,
    chunk_hash: str | None = None,
    position: Literal["end", "start"] = "end",
    create: bool = False,
    expected_mtime: Annotated[
        float | None,
        Field(
            description=(
                "Optimistic concurrency: pass mtime from a prior read/write for this path. "
                "On stale_write, re-read and retry (or pass matching region hashes)."
            ),
        ),
    ] = None,
    expected_frontmatter_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC),
    ] = None,
    expected_body_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC),
    ] = None,
    expected_content_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC),
    ] = None,
    vault: str = "",
) -> dict:
    """Preferred add for session log / History / post-search text. Use text=. Anchor: chunk_hash → heading → EOF."""
    return await asyncio.to_thread(
        apo_ops.append_note,
        path,
        text,
        heading=heading,
        chunk_hash=chunk_hash,
        position=position,
        create=create,
        expected_mtime=expected_mtime,
        expected_frontmatter_hash=expected_frontmatter_hash,
        expected_body_hash=expected_body_hash,
        expected_content_hash=expected_content_hash,
        vault=vault,
    )


@mcp.tool(annotations=_MUTATE)
async def patch_note(
    path: Annotated[
        str,
        Field(description="Vault-relative path for single-path mode. Omit when using items=."),
    ] = "",
    ops: Annotated[
        list[PatchOp] | None,
        Field(description=OPS_FIELD_DESC),
    ] = None,
    items: Annotated[
        list[PatchNotesItem] | None,
        Field(description=PATCH_NOTES_ITEMS_DESC),
    ] = None,
    strict: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    expected_mtime: Annotated[
        float | None,
        Field(
            description=(
                "Single-path only: optimistic concurrency mtime. "
                "Multi-path: set expected_mtime per items[] entry. "
                "Stale mtime + matching region hash still allows scoped ops."
            ),
        ),
    ] = None,
    expected_frontmatter_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC + " Single-path only; per-item for items[]."),
    ] = None,
    expected_body_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC + " Single-path only; per-item for items[]."),
    ] = None,
    expected_content_hash: Annotated[
        str | None,
        Field(description=_REGION_HASH_DESC + " Single-path only; per-item for items[]."),
    ] = None,
    vault: str = "",
) -> dict:
    """Mutate frontmatter/sections or place (ops place). Single: path+ops or place-only ops. Multi: items[]."""
    return await asyncio.to_thread(
        apo_ops.patch_entry,
        path=path,
        ops=ops,
        items=items,
        strict=strict,
        dry_run=dry_run,
        verbose=verbose,
        expected_mtime=expected_mtime,
        expected_frontmatter_hash=expected_frontmatter_hash,
        expected_body_hash=expected_body_hash,
        expected_content_hash=expected_content_hash,
        vault=vault,
    )


###############################################################################
# Tools — reading & search (delegate to ops)
###############################################################################


@mcp.tool(annotations=_RO)
async def read_note(
    path: Annotated[
        str,
        Field(description="Vault-relative path (XOR with chunk_hash)."),
    ] = "",
    chunk_hash: Annotated[
        str | None,
        Field(description="Section anchor from search_notes hits (XOR with path)."),
    ] = None,
    heading: str | None = None,
    vault: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    force: Annotated[
        bool,
        Field(description="chunk_hash mode: return full section above preview threshold."),
    ] = False,
    fields: Annotated[
        list[str] | None,
        Field(description="Optional frontmatter projection; [] omits frontmatter key."),
    ] = None,
    raw: Annotated[
        bool,
        Field(
            description=(
                "Path mode: byte-exact file text (or line slice). "
                "Default returns body only with frontmatter sidecar."
            ),
        ),
    ] = False,
    mode: Annotated[
        str,
        Field(
            description=(
                "Path mode: auto (default; body + size tip) | toc (lean outline from "
                "index: level/title/chunk_hash per section, no body) | section."
            ),
        ),
    ] = "auto",
    format: Annotated[
        str,
        Field(
            description=(
                "chunk_hash mode: markdown (default) | json (whole table as "
                "{headers, rows}) | row (single table_row as {columns, row_key}). "
                "Structured payloads are opt-in to avoid token bloat."
            ),
        ),
    ] = "markdown",
    sibling: Annotated[
        str | None,
        Field(description="chunk_hash mode: hop to same-depth 'prev' or 'next' section/row."),
    ] = None,
    siblings: Annotated[
        bool,
        Field(description="chunk_hash mode: include full same-depth siblings[] list (default off)."),
    ] = False,
) -> dict:
    """Read by path or search hit chunk_hash. Search → read_note(chunk_hash=)."""
    return await asyncio.to_thread(
        apo_ops.read_note,
        path,
        chunk_hash=chunk_hash,
        heading=heading,
        vault=vault,
        start_line=start_line,
        end_line=end_line,
        max_chars=max_chars,
        raw=raw,
        force=force,
        fields=fields,
        mode=mode,
        format=format,
        sibling=sibling,
        siblings=siblings,
    )


@mcp.tool(annotations=_RO)
async def search_notes(
    query: str,
    folder: str = "",
    folders: Annotated[
        list[str] | None,
        Field(description="Multi-folder scope (XOR with folder=). Merge by score."),
    ] = None,
    vault: str = "",
    vaults: Annotated[
        list[str] | None,
        Field(
            description=(
                "Fan-out across named vaults (separate indexes); merge by score. "
                "Do not combine with vault=."
            ),
        ),
    ] = None,
    snippet_chars: Annotated[
        int,
        Field(description="Hit preview length (default 240). 0 = full chunk text."),
    ] = _DEFAULT_SEARCH_SNIPPET,
    limit: Annotated[
        int | None,
        Field(description="Max hits (default 5)."),
    ] = None,
    offset: Annotated[
        int,
        Field(description="Skip this many top hits for pagination; response carries has_more."),
    ] = 0,
    exclude: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional path globs to drop. When omitted and folder= is empty, "
                "vault search-contract defaults apply (response carries default_exclude)."
            ),
        ),
    ] = None,
) -> dict:
    """Hybrid search. Hits include chunk_hash — read more via read_note(chunk_hash=)."""
    return await asyncio.to_thread(
        apo_ops.search,
        query,
        folder=folder,
        folders=folders,
        vault=vault,
        vaults=vaults,
        snippet_chars=snippet_chars,
        limit=limit,
        offset=offset,
        exclude=exclude,
    )


@mcp.tool(annotations=_RO)
async def filter_notes(
    where: Annotated[
        dict | None,
        Field(
            description=(
                "Frontmatter predicate (canonical). Omit or {} = all in folder; "
                "else field→scalar or {$eq,$ne,$lt,$lte,$gt,$gte,$contains,$exists,$in,$elemMatch}. "
                "Nested keys: todos.status (any list element). "
                'List-of-dicts: {"todos": {"$elemMatch": {"status": "pending"}}} '
                "(AND on one element). "
                'Example: {"status": {"$in": ["active", "waiting"]}}.'
            ),
        ),
    ] = None,
    folder: str = "",
    limit: int = 20,
    vault: str = "",
    offset: int = 0,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional frontmatter key projection. Default=full. "
                "Status sweeps: [\"status\",\"okf_type\",\"last_checked\",\"title\"]."
            ),
        ),
    ] = None,
    sort: Annotated[
        str,
        Field(
            description=(
                "Catalog sort key. Default mtime. Pass a safe frontmatter key "
                "(e.g. last_activity) for oldest/newest-by-field sweeps. "
                "Missing values sort last. Response includes has_more."
            ),
        ),
    ] = "mtime",
    order: Annotated[
        Literal["asc", "desc"],
        Field(description="Sort direction (default desc — newest/largest first)."),
    ] = "desc",
) -> dict:
    """Frontmatter catalog (no embeddings). Prefer where=; omit where or pass {} to list."""
    return await asyncio.to_thread(
        apo_ops.filter_notes,
        where,
        folder=folder,
        limit=limit,
        vault=vault,
        offset=offset,
        fields=fields,
        sort=sort,
        order=order,
    )


@mcp.tool(annotations=_RO)
async def backlinks(path: str, limit: int = 100, offset: int = 0, vault: str = "") -> dict:
    """Index-backed inbound [[wiki-links]] to this path/stem/title (target need not exist on disk)."""
    return await asyncio.to_thread(apo_ops.backlinks, path, limit=limit, offset=offset, vault=vault)


@mcp.tool(annotations=_RO)
async def history(
    limit: int = 10,
    offset: Annotated[
        int,
        Field(description="Browse only: skip this many newest notes for pagination (has_more in response)."),
    ] = 0,
    folder: str = "",
    path: Annotated[
        str,
        Field(
            description=(
                "Vault-relative note path for file-level history. "
                "When set and the vault has an active git contract + .git, returns commits. "
                "Empty → browse newest notes by mtime (optional folder=/since=/until=/preview=)."
            ),
        ),
    ] = "",
    vault: str = "",
    since: Annotated[
        str,
        Field(description="Browse only: mtime lower bound (YYYY-MM-DD or ISO datetime, ET for date-only)."),
    ] = "",
    until: Annotated[
        str,
        Field(description="Browse only: mtime upper bound (YYYY-MM-DD = end of that ET day, or ISO datetime)."),
    ] = "",
    preview: Annotated[
        Literal["first", "last"],
        Field(
            description=(
                "Browse only: which chunk feeds first_line/chunk_hash — "
                "first (default, ord 0) or last (tail; use for session logs)."
            ),
        ),
    ] = "first",
    heading: Annotated[
        str,
        Field(
            description=(
                "Browse only: scope chunk pick to this governing heading "
                "(e.g. Session log) before applying preview=first|last."
            ),
        ),
    ] = "",
    exclude: Annotated[
        list[str] | None,
        Field(description="Browse only: path globs to drop (same rules as search_notes exclude)."),
    ] = None,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Browse only: optional frontmatter key projection on each note "
                "(omit to skip frontmatter; [] = empty object)."
            ),
        ),
    ] = None,
) -> dict:
    """Browse newest notes by mtime (digest filters), or file-level git history when path= is set."""
    return await asyncio.to_thread(
        apo_ops.history,
        limit=limit,
        offset=offset,
        folder=folder,
        path=path,
        vault=vault,
        since=since,
        until=until,
        preview=preview,
        heading=heading,
        exclude=exclude,
        fields=fields,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def vault(
    action: Annotated[
        str,
        Field(description="list | contracts | describe | merge | project | stats"),
    ] = "list",
    vault: Annotated[
        str,
        Field(
            description=(
                "Vault name from APO_VAULTS. Empty: list/merge/project=all; "
                "contracts=all; describe/stats=default vault."
            ),
        ),
    ] = "",
    vaults: Annotated[
        list[str] | None,
        Field(
            description=(
                "Scope every action to a named subset of the registry. "
                "Do not combine with vault=."
            ),
        ),
    ] = None,
    full: Annotated[
        bool,
        Field(
            description=(
                "contracts / describe / merge: when false (default), return contract "
                "summaries without YAML bodies. When true, include parsed data=."
            ),
        ),
    ] = False,
    days: Annotated[
        int | None,
        Field(description="stats only: rollup window in days (default 7)."),
    ] = 7,
) -> dict:
    """Vault registry, contracts, desk projection, and optional habit KPIs (stats)."""
    return await asyncio.to_thread(
        apo_ops.vault_op,
        action,
        vault=vault,
        vaults=vaults,
        full=full,
        days=days,
    )


@mcp.tool(annotations=_RO)
async def apo_admin(
    action: Annotated[
        str,
        Field(description="list | describe | invoke"),
    ] = "list",
    name: Annotated[
        str | None,
        Field(description="Admin capability id (required for describe / invoke)."),
    ] = None,
    parameters: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "invoke only: nested args for the target capability (not alongside "
                "top-level invoke fields except vault)."
            ),
        ),
    ] = None,
    confirm: Annotated[
        bool,
        Field(
            description=(
                "invoke only: required true for delete_note, reindex(force=true), "
                "and git_sync(action=run|pull|rebase)."
            ),
        ),
    ] = False,
    vault: Annotated[
        str,
        Field(description="Optional vault name for invoke (overrides parameters.vault)."),
    ] = "",
) -> dict:
    """Engine admin ops: list/describe capabilities; invoke with confirm=true when destructive."""
    act = (action or "list").strip().lower()
    if act == "list":
        return await asyncio.to_thread(apo_admin_ops.admin_list)
    if act == "describe":
        if not (name or "").strip():
            return _err(error="bad_request", message="name required for action=describe")
        return await asyncio.to_thread(apo_admin_ops.admin_describe, name.strip())
    if act == "invoke":
        if not (name or "").strip():
            return _err(error="bad_request", message="name required for action=invoke")
        return await asyncio.to_thread(
            apo_admin_ops.admin_invoke,
            name.strip(),
            parameters=parameters,
            confirm=confirm,
            vault=vault,
            handlers=_ADMIN_HANDLERS,
        )
    return _err(
        error="bad_action",
        message="action must be list|describe|invoke",
    )


###############################################################################
# Resources
###############################################################################


@mcp.resource("note://{vault}/{path*}", mime_type="text/markdown")
def note_resource(vault: str, path: str) -> str:
    """Raw markdown via MCP resource URI note://<vault>/<relative-path>. Prefer read_note for agents."""
    v = _vault(vault)
    full = _safe_resolve(v, path)
    if not full.is_file():
        raise FileNotFoundError(f"note not found: {vault}/{path}")
    return full.read_text(encoding="utf-8")


@mcp.resource("memory://vaults", mime_type="application/json")
def vaults_resource() -> dict:
    """Registered vaults (roots, collections, top-level dirs) via memory://vaults."""
    return {
        "default_vault": DEFAULT_VAULT,
        "vaults": {
            name: {
                "root": str(v.root),
                "collection": v.collection,
                "ingest_dir": v.ingest_dir,
                "top_level_dirs": _top_level_dirs(v),
            }
            for name, v in VAULTS.items()
        },
    }


###############################################################################
# Entry point
###############################################################################

if __name__ == "__main__":
    mcp.run()
