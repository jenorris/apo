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


def _lean_enabled() -> bool:
    """Lean desk is the default. Set APO_MCP_LEAN=0/false/no/off for admin tools."""
    raw = os.environ.get("APO_MCP_LEAN")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _top_level_dirs(v: Vault) -> list[str]:
    if not v.root.exists():
        return []
    return sorted(p.name for p in v.root.iterdir() if p.is_dir() and not p.name.startswith("."))


###############################################################################
# Server
###############################################################################

_LEAN_BOOT = _lean_enabled()
_MCP_INSTRUCTIONS = (
    "Apo: vault-relative Markdown + YAML catalog notes; sqlite-vec hybrid search; "
    "files are source of truth. "
    "Lean desk is default (APO_MCP_LEAN=0 exposes admin + delete_note + git_sync). "
    "Routing: write_note=create/overwrite only (no append); "
    "append_note=Markdown session log / History / post-search add "
    "(prefer over patch_note append; unsupported on .yaml); "
    "patch_note=frontmatter/YAML fields + MD section mutate — one path (path+ops) or multi-path (items[]); "
    "YAML notes: set_field/delete_field (dotted nested paths); whole file is the catalog row; "
    "dual-write (domain + daily session log)=parallel append_note+patch_note in one turn; "
    "place_note=move if src in vault else copy host .md into vault (delete_note is admin-only). "
    "Thread mtime → expected_mtime on follow-up writes. "
    "search_notes=content (prefer limit=; top_k alias); "
    "filter_notes=frontmatter / YAML-note catalog (prefer where=; filters alias; "
    "omit where or pass where={} to list; "
    "status sweeps pass fields=[status,okf_type,last_checked,title]); "
    "history=browse by mtime (first_line) or file git log when path= + git contract; "
    "status sweeps → filter_notes. "
    "Hits expose chunk_hash/heading for append/expand (skip read when possible; "
    "append_note may take chunk_hash alone). "
    "backlinks=[[wiki-links]]. Resources: note://<vault>/<path>, memory://vaults. "
    "MCP enqueues index work (~/.apo/deferred-*.json); apo-engine watch is the sole "
    "index.db writer and wakes on enqueue. Multi-vault: pass vault= "
    "(APO_VAULTS registry); each vault has its own index + deferred collection."
) + (
    ""
    if _LEAN_BOOT
    else (
        " Admin (APO_MCP_LEAN=0): reload_config, memory_status, reindex_deferred, "
        "reindex, delete_note, tool_stats, git_sync."
    )
)
mcp = FastMCP("Apo", instructions=_MCP_INSTRUCTIONS)

# Rewrite opaque Pydantic ValidationError text into agent-actionable ToolError hints
# (FastMCP validates args before tool bodies — see apo_engine.validation_hints).
from apo_engine.agent_validation import AgentValidationMiddleware  # noqa: E402
from apo_engine.tool_metrics_middleware import ToolMetricsMiddleware  # noqa: E402

mcp.add_middleware(AgentValidationMiddleware())
mcp.add_middleware(ToolMetricsMiddleware())

# Load vault registry at import (fast); the index backend connects lazily per vault.
_load_vaults()


###############################################################################
# Tools — config & status (admin)
###############################################################################


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    tags={"admin"},
)
async def reload_config() -> dict:
    """Reload runtime JSON overrides (APO_RUNTIME_CONFIG) without restarting the host. Vault root / APO_INDEX still need a process restart."""
    return await asyncio.to_thread(_reload_config_sync)


def _reload_config_sync() -> dict:
    _load_vaults()
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
    }


@mcp.tool(annotations=_RO, tags={"admin"})
async def memory_status() -> dict:
    """Vault roots, index health, deferred queues, watcher state — diagnose before retrying failures."""
    return await asyncio.to_thread(_memory_status_sync)


@mcp.tool(annotations=_RO, tags={"admin"})
async def tool_stats(
    days: Annotated[
        int | None,
        Field(description="Rollup window in days (default 7). Pass null for all events."),
    ] = 7,
    tool: Annotated[
        str | None,
        Field(description="Optional tool name filter (e.g. filter_notes)."),
    ] = None,
    vault: str = "",
) -> dict:
    """MCP tool-use rollups from ~/.apo/tool-metrics-*.jsonl (admin). No note bodies/paths stored."""
    return await asyncio.to_thread(_tool_stats_sync, days, tool, vault)


def _tool_stats_sync(
    days: int | None = 7,
    tool: str | None = None,
    vault: str = "",
) -> dict:
    from apo_engine import tool_metrics as apo_metrics

    try:
        v = _vault(vault)
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    if days is not None and days < 0:
        return _err(error="bad_request", message="days must be >= 0 or null")
    return apo_metrics.tool_stats(v.collection, days=days, tool=tool)


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


###############################################################################
# Tools — writing (delegate to ops)
###############################################################################


@mcp.tool(annotations=_MUTATE)
async def write_note(
    path: str,
    content: str,
    expected_mtime: Annotated[
        float | None,
        Field(
            description=(
                "Optimistic concurrency: pass mtime from a prior read/write for this path. "
                "On stale_write, re-read and retry."
            ),
        ),
    ] = None,
    vault: str = "",
) -> dict:
    """Create or overwrite a note. Prefer append_note / patch_note for edits."""
    return await asyncio.to_thread(
        apo_ops.write_note,
        path,
        content,
        expected_mtime=expected_mtime,
        vault=vault,
    )


@mcp.tool(annotations=_WRITE)
async def append_note(
    text: str,
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
                "On stale_write, re-read and retry."
            ),
        ),
    ] = None,
    vault: str = "",
) -> dict:
    """Preferred add for session log / History / post-search text. Anchor: chunk_hash (path optional) → heading → EOF. Stale hash + path+heading falls back with tip. ``text`` is body only (do not repeat the heading — a leading duplicate of the anchor is stripped). Batch with other mutators → patch_note."""
    return await asyncio.to_thread(
        apo_ops.append_note,
        path,
        text,
        heading=heading,
        chunk_hash=chunk_hash,
        position=position,
        create=create,
        expected_mtime=expected_mtime,
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
                "Multi-path: set expected_mtime per items[] entry."
            ),
        ),
    ] = None,
    vault: str = "",
) -> dict:
    """Mutate frontmatter/sections. Single path: path+ops. Multi-path: items=[{path,ops,…}] (max 20). XOR — not both. Standalone text add → append_note."""
    return await asyncio.to_thread(
        apo_ops.patch_entry,
        path=path,
        ops=ops,
        items=items,
        strict=strict,
        dry_run=dry_run,
        verbose=verbose,
        expected_mtime=expected_mtime,
        vault=vault,
    )


@mcp.tool(annotations=_MUTATE)
async def place_note(
    src: Annotated[
        str,
        Field(
            description=(
                "Source: vault-relative path to move, or absolute ~/…|/… host .md to copy. "
                "In-vault absolute paths move (not copy)."
            ),
        ),
    ],
    dst: Annotated[
        str,
        Field(description="Vault-relative destination path."),
    ],
    overwrite: bool = False,
    fields: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Copy mode only: optional frontmatter merge before write "
                '(e.g. {"source":"report"}). Forbidden when moving inside the vault.'
            ),
        ),
    ] = None,
    expected_mtime: Annotated[
        float | None,
        Field(
            description=(
                "Optimistic concurrency: src mtime when moving; dst mtime when copying "
                "over an existing note."
            ),
        ),
    ] = None,
    vault: str = "",
) -> dict:
    """Move if src is in the vault; otherwise copy host .md into the vault (leaves host src). Prefer over delete+rewrite."""
    return await asyncio.to_thread(
        apo_ops.place_note,
        src,
        dst,
        overwrite=overwrite,
        fields=fields,
        expected_mtime=expected_mtime,
        vault=vault,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    tags={"admin"},
)
async def delete_note(path: str, vault: str = "") -> dict:
    """Irreversible delete + index purge (admin / APO_MCP_LEAN=0). Prefer place_note to archives/."""
    return await asyncio.to_thread(apo_ops.delete_note, path, vault=vault)


###############################################################################
# Tools — reading & search (delegate to ops)
###############################################################################


@mcp.tool(annotations=_RO)
async def read_note(
    path: str,
    heading: str | None = None,
    vault: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    raw: Annotated[
        bool,
        Field(
            description=(
                "If true, content is byte-exact file text (or an absolute-line slice). "
                "Default full-file reads return body only; frontmatter is always a sidecar."
            ),
        ),
    ] = False,
) -> dict:
    """Read a known path. Returns frontmatter (parsed) + content (body by default). Optional heading=/line range/max_chars; raw=true for byte-exact. Unknown path → search_notes first."""
    return await asyncio.to_thread(
        apo_ops.read_note,
        path,
        heading=heading,
        vault=vault,
        start_line=start_line,
        end_line=end_line,
        max_chars=max_chars,
        raw=raw,
    )


@mcp.tool(annotations=_RO)
async def search_notes(
    query: str,
    top_k: Annotated[
        int | None,
        Field(description="Alias for limit. Prefer limit=; conflicting values → bad_request."),
    ] = None,
    folder: str = "",
    vault: str = "",
    snippet_chars: Annotated[
        int,
        Field(description="Hit preview length (default 240). 0 = full chunk text."),
    ] = _DEFAULT_SEARCH_SNIPPET,
    limit: Annotated[
        int | None,
        Field(description="Max hits (canonical; default 5). Prefer over top_k."),
    ] = None,
) -> dict:
    """Hybrid BM25+vector content search (not frontmatter — use filter_notes). Prefer limit= over top_k. folder= scopes. Hits include chunk_hash/heading for append/expand."""
    return await asyncio.to_thread(
        apo_ops.search,
        query,
        top_k=top_k,
        folder=folder,
        vault=vault,
        snippet_chars=snippet_chars,
        limit=limit,
    )


@mcp.tool(annotations=_RO)
async def expand_chunk(
    chunk_hash: str,
    vault: str = "",
    scope: Literal["section", "chunk"] = "section",
) -> dict:
    """Grow a search_notes hit by chunk_hash only (no path/heading). scope=section (default, disk) or chunk (index body). Returns mtime when the source file exists (chain into expected_mtime)."""
    return await asyncio.to_thread(
        apo_ops.expand_chunk, chunk_hash, vault=vault, scope=scope
    )


@mcp.tool(annotations=_RO)
async def filter_notes(
    where: Annotated[
        dict | None,
        Field(
            description=(
                "Frontmatter predicate (canonical). Omit or {} = all in folder; "
                "else field→scalar or {$eq,$ne,$lt,$lte,$gt,$gte,$contains,$exists,$in}. "
                'Example: {"status": {"$in": ["active", "waiting"]}}.'
            ),
        ),
    ] = None,
    folder: str = "",
    limit: int = 20,
    vault: str = "",
    offset: int = 0,
    filters: Annotated[
        dict | None,
        Field(description="Alias for where. Prefer where=; do not pass both with different values."),
    ] = None,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional frontmatter key projection. Default=full. "
                "Status sweeps: [\"status\",\"okf_type\",\"last_checked\",\"title\"]."
            ),
        ),
    ] = None,
) -> dict:
    """Frontmatter catalog (no embeddings). Prefer where=; omit where or pass {} to list. Pass fields= on status sweeps. Newest first."""
    return await asyncio.to_thread(
        apo_ops.filter_notes,
        where,
        folder=folder,
        limit=limit,
        vault=vault,
        offset=offset,
        filters=filters,
        fields=fields,
    )


@mcp.tool(annotations=_RO)
async def backlinks(path: str, limit: int = 100, vault: str = "") -> dict:
    """Index-backed inbound [[wiki-links]] to this path/stem/title (target need not exist on disk)."""
    return await asyncio.to_thread(apo_ops.backlinks, path, limit=limit, vault=vault)


@mcp.tool(annotations=_RO)
async def history(
    limit: int = 10,
    folder: str = "",
    path: Annotated[
        str,
        Field(
            description=(
                "Vault-relative note path for file-level history. "
                "When set and the vault has an active git contract + .git, returns commits. "
                "Empty → browse newest notes by mtime (optional folder=)."
            ),
        ),
    ] = "",
    vault: str = "",
) -> dict:
    """Browse newest notes by mtime, or file-level git history when path= is set (git contract)."""
    return await asyncio.to_thread(
        apo_ops.history, limit=limit, folder=folder, path=path, vault=vault
    )


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    tags={"admin"},
)
async def git_sync(
    action: Annotated[
        str,
        Field(description="status | run | pull | clear_block"),
    ] = "status",
    message: Annotated[
        str,
        Field(
            description=(
                "Commit message for action=run. Empty → template from git contract "
                "(auto path uses template; agents should pass a message)."
            ),
        ),
    ] = "",
    vault: str = "",
) -> dict:
    """Git contract sync: status, commit+push (run), ff-only pull, or clear_block. Opt-in via sync.enabled."""
    return await asyncio.to_thread(
        apo_ops.git_sync_op, action, message=message, vault=vault
    )


###############################################################################
# Tools — indexing (admin)
###############################################################################


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


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    tags={"admin"},
)
async def reindex_deferred(vault: str = "") -> dict:
    """Wake watcher to flush deferred queue. Check watcher_running; enqueue already wakes on write."""
    return await asyncio.to_thread(_reindex_deferred_sync, vault)


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


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    tags={"admin"},
)
async def reindex(force: bool = False, vault: str = "") -> dict:
    """Signal full index rebuild (prunes deleted). force=True re-embeds all. Check watcher_running."""
    return await asyncio.to_thread(_reindex_sync, force, vault)


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
# Lean mode — hide admin tools from list_tools / schema (default on)
###############################################################################

_ADMIN_TOOLS = frozenset({
    "reload_config",
    "memory_status",
    "reindex_deferred",
    "reindex",
    "delete_note",
    "tool_stats",
})


def _apply_lean_mode() -> bool:
    """Lean (default) disables admin-tagged tools. Returns whether lean applied."""
    if not _lean_enabled():
        return False
    mcp.disable(tags={"admin"})
    return True


_LEAN_ACTIVE = _apply_lean_mode()


###############################################################################
# Entry point
###############################################################################

if __name__ == "__main__":
    mcp.run()
