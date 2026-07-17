#!/usr/bin/env python3
"""
Apo MCP server — hybrid search + surgical writes over sqlite-vec + Ollama.

Vault: APO_NOTES_ROOT. Deferred queue: ~/.apo/deferred-<collection>.json
"""

import asyncio
import json
import os
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field
from apo_engine import config as apo_config
from apo_engine import core as apo_core
from apo_engine import deferred as index_deferred
from apo_engine import okf as apo_okf
from apo_engine import vaults as apo_vaults
from apo_engine.mcp_backend import ApoMem
from apo_engine.markdown_patch import (
    PatchError,
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    normalize_lines,
    section_from_chunk,
)
from apo_engine.patch_ops import OPS_FIELD_DESC, PatchOp, ops_to_dicts

WATCH_PID_FILE = Path.home() / ".apo" / "watch.pid"
DEFERRED_DIR = Path.home() / ".apo"

# Tool annotation presets
_RO = {"readOnlyHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_MUTATE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


class VaultError(Exception):
    pass


@dataclass
class Vault:
    name: str
    root: Path
    collection: str
    index_path: Path
    ingest_dir: str = "wiki"
    mem: ApoMem | None = None
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


def _save_deferred(v: Vault) -> None:
    index_deferred.save_index_queue(v.collection, v.deferred)


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
            coll = (
                _pick(overrides, "APO_COLLECTION", coll) or coll
            )
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


def _ensure_mem(v: Vault) -> ApoMem:
    """Lazy-init index backend per vault."""
    if v.mem is None:
        v.mem = ApoMem()
    return v.mem


def _bound(v: Vault):
    """Context manager: activate this vault's root+index for core.* calls."""
    return apo_vaults.bind(v.binding())


def _safe_resolve(v: Vault, relative_path: str) -> Path:
    """Resolve a vault-relative path and assert it stays within the vault root."""
    full = (v.root / relative_path).resolve()
    full.relative_to(v.root)  # raises ValueError on traversal
    return full


def _display_source(v: Vault, source: str) -> str:
    """Vault-relative form of an absolute source path."""
    if not source:
        return ""
    src = Path(source).expanduser().resolve()
    try:
        return str(src.relative_to(v.root))
    except ValueError:
        return str(src)


def _err(**kw: Any) -> dict:
    return {"ok": False, **kw}


def _mtime(full: Path) -> float:
    return full.stat().st_mtime


def _check_mtime(full: Path, expected: float | None, path: str) -> dict | None:
    """Optimistic-concurrency guard: fail if the file changed since `expected`."""
    if expected is None or not full.exists():
        return None
    actual = full.stat().st_mtime
    if abs(actual - float(expected)) > 1e-6:
        return _err(
            path=path,
            error="stale_write",
            message="file modified since expected_mtime; re-read before writing",
            expected_mtime=float(expected),
            actual_mtime=actual,
        )
    return None


def _env_truthy(key: str) -> bool:
    return os.environ.get(key, "").lower() in ("1", "true", "yes")


def _default_index_on_write() -> bool:
    return _env_truthy("APO_INDEX_ON_WRITE")


def _maybe_index(v: Vault, full: Path, index: bool | None) -> None:
    """Queue path for the watcher — MCP never writes index.db (single-writer policy).

    Sync on purpose: write/read tools run via ``asyncio.to_thread``, so flock/queue I/O
    must not sit in an ``async def`` body (that would still block the event loop).
    ``index`` is API-compat only; the watcher owns all SQLite writes.
    Best-effort: enqueue failure must not fail an already-successful note write.
    """
    del index
    try:
        # enqueue_index returns the updated set — avoid a second flock/re-read.
        v.deferred = index_deferred.enqueue_index(v.collection, str(full.resolve()))
    except Exception:
        pass


def _purge_index(v: Vault, full: Path) -> bool:
    """Queue index purge for the watcher. Best-effort."""
    try:
        index_deferred.enqueue_purge(v.collection, str(full.resolve()))
        return True
    except Exception:
        return False


def _lookup_chunk(
    v: Vault, chunk_hash: str, *, include_text: bool = True
) -> dict[str, Any] | None:
    try:
        with _bound(v):
            return _ensure_mem(v).store.lookup_chunk(chunk_hash, include_text=include_text)
    except Exception:
        return None


def _watcher_status() -> dict[str, Any]:
    """Best-effort liveness check for the watcher PID file.

    PID existence alone (os.kill(pid, 0)) isn't process *identity* — if the watcher died
    and the PID was later recycled by an unrelated process, that check false-positives.
    Cross-check /proc/<pid>/cmdline where available (Linux); degrade to existence-only
    elsewhere rather than fail the check outright.
    """
    status: dict[str, Any] = {"pid_file": str(WATCH_PID_FILE), "running": False}
    try:
        pid = int(WATCH_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return status
    try:
        os.kill(pid, 0)
    except OSError:
        return status
    status["pid"] = pid
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        try:
            cmdline = cmdline_path.read_bytes().decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        if cmdline and not ("apo-engine" in cmdline and "watch" in cmdline):
            status["warning"] = f"pid {pid} is alive but doesn't look like apo-engine watch (stale/recycled pid?)"
            return status
    status["running"] = True
    return status


def _top_level_dirs(v: Vault) -> list[str]:
    if not v.root.exists():
        return []
    return sorted(p.name for p in v.root.iterdir() if p.is_dir() and not p.name.startswith("."))


###############################################################################
# Server
###############################################################################

_LEAN_BOOT = _env_truthy("APO_MCP_LEAN")
_MCP_INSTRUCTIONS = (
    "Apo: vault-relative Markdown; sqlite-vec hybrid search; files are source of truth. "
    "Writes: write_note (create/overwrite), append_note (add), "
    "patch_note (mutate — ops use field/value, find/replace, heading/text; not key/old/new), "
    "move_note (rename — not read+write+delete). "
    "search_notes hits expose chunk_hash/heading for append/expand (skip read when possible). "
    "filter_notes = frontmatter catalog; backlinks = [[wiki-links]]. "
    "MCP enqueues index work (~/.apo/deferred-*.json); apo-engine watch is the sole index.db "
    "writer and wakes on enqueue. Multi-vault: pass vault= (APO_VAULTS registry); each vault "
    "has its own index + deferred collection."
) + (
    ""
    if _LEAN_BOOT
    else " Admin (APO_MCP_LEAN off): reload_config, memory_status, reindex_deferred, reindex."
)
mcp = FastMCP("Apo", instructions=_MCP_INSTRUCTIONS)

# Load vault registry at import (fast); the index backend connects lazily per vault.
_load_vaults()


###############################################################################
# Tools — config & status
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


def _memory_status_sync() -> dict:
    vaults: dict[str, Any] = {}
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
                info["indexed_chunks"] = _ensure_mem(v).store.count()
            info["index"] = "ok"
        except Exception as e:
            info["index"] = f"error: {e}"
        vaults[name] = info

    watcher = _watcher_status()

    return {
        "ok": True,
        "default_vault": DEFAULT_VAULT,
        "vaults": vaults,
        "watcher": watcher,
        "runtime_file": str(_runtime_config_path()),
        "index_on_write_default": _default_index_on_write(),
    }


###############################################################################
# Tools — writing (sync bodies; async wrappers offload via to_thread)
###############################################################################


# Default MCP search payload: anchors + short preview. Pass snippet_chars=0 for full chunk text.
_DEFAULT_SEARCH_SNIPPET = 240


def _write_note_sync(
    path: str,
    content: str,
    append: bool = False,
    index: bool | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))

    if (guard := _check_mtime(full, expected_mtime, path)):
        return guard

    existed = full.exists()
    parts = Path(path.replace("\\", "/")).parts
    new_top = len(parts) > 1 and not (v.root / parts[0]).exists()

    okf_meta: dict[str, Any] = {}
    to_write = content
    # append=True is raw tail — skip OKF stamp (same spirit as append_note).
    if not (append and existed):
        okf = apo_okf.process_concept(vault_root=v.root, rel_path=path, content=content)
        okf_meta = okf.as_response_fields()
        if not okf.ok:
            return _err(
                path=path,
                error=okf.error or "okf_validation",
                message=okf.message or "OKF validation failed",
                **{k: v for k, v in okf_meta.items() if k != "enforcement"},
                enforcement=okf.enforcement,
            )
        to_write = okf.content

    full.parent.mkdir(parents=True, exist_ok=True)
    if append and existed:
        with full.open("a", encoding="utf-8") as f:
            f.write("\n" + content)
    else:
        full.write_text(to_write, encoding="utf-8")
    _maybe_index(v, full, index)

    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "action": "appended" if (append and existed) else ("overwrote" if existed else "created"),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
    }
    out.update(okf_meta)
    if new_top:
        out["warning"] = (
            f"created new top-level directory {parts[0]!r} — "
            f"existing top-level dirs: {_top_level_dirs(v)}"
        )
    return out


@mcp.tool(annotations=_MUTATE)
async def write_note(
    path: str,
    content: str,
    append: bool = False,
    index: bool | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    """Create or overwrite a note. Prefer append_note / patch_note for edits. append=True = raw file tail."""
    return await asyncio.to_thread(
        _write_note_sync, path, content, append, index, expected_mtime, vault
    )


def _append_note_sync(
    path: str,
    text: str,
    heading: str | None = None,
    chunk_hash: str | None = None,
    position: Literal["end", "start"] = "end",
    create: bool = False,
    index: bool | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))

    if (guard := _check_mtime(full, expected_mtime, path)):
        return guard

    if not full.exists():
        if not create:
            return _err(path=path, error="not_found", message="note not found (pass create=true to create)")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(minimal_note_stub(path), encoding="utf-8")

    content = full.read_text(encoding="utf-8")
    lines = normalize_lines(content)

    try:
        section = None
        anchor_label = "EOF"
        if chunk_hash:
            # Anchor metadata only — skip loading chunk body from SQLite.
            chunk = _lookup_chunk(v, chunk_hash, include_text=False)
            if not chunk:
                return _err(path=path, error="anchor_not_found", message=f"chunk_hash {chunk_hash!r} not found")
            chunk_path = (chunk.get("path") or "").replace("\\", "/")
            want = path.replace("\\", "/")
            if chunk_path and chunk_path != want:
                return _err(
                    path=path,
                    error="path_mismatch",
                    message=f"chunk_hash belongs to {chunk_path!r}, not {path!r}",
                )
            section = section_from_chunk(
                lines,
                int(chunk.get("start_line", 1)),
                int(chunk.get("heading_level", 0)),
            )
            anchor_label = section.title or chunk_hash
            merged, detail = apply_append(lines, text, section=section, position=position)
        elif heading:
            merged, detail = apply_append(lines, text, heading=heading, position=position)
            anchor_label = heading
        else:
            merged, detail = apply_append(lines, text, heading=None, position="end")
    except PatchError as e:
        return _err(path=path, error=e.code, message=e.message, suggestions=e.suggestions)

    new_content = "\n".join(merged)
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"
    full.write_text(new_content, encoding="utf-8")
    _maybe_index(v, full, index)

    return {
        "ok": True,
        "path": path,
        "anchor": anchor_label,
        "detail": detail,
        "lines_added": max(0, len(merged) - len(lines)),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
    }


@mcp.tool(annotations=_WRITE)
async def append_note(
    path: str,
    text: str,
    heading: str | None = None,
    chunk_hash: str | None = None,
    position: Literal["end", "start"] = "end",
    create: bool = False,
    index: bool | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    """Add text under heading, at search chunk_hash, or EOF (chunk_hash → heading → EOF)."""
    return await asyncio.to_thread(
        _append_note_sync,
        path,
        text,
        heading,
        chunk_hash,
        position,
        create,
        index,
        expected_mtime,
        vault,
    )


def _patch_note_sync(
    path: str,
    ops: list[Any],
    strict: bool = False,
    dry_run: bool = False,
    index: bool | None = None,
    verbose: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))

    if not full.exists():
        return _err(path=path, error="not_found", message="note not found")

    if (guard := _check_mtime(full, expected_mtime, path)):
        return guard

    content = full.read_text(encoding="utf-8")
    result = apply_patch(content, ops_to_dicts(ops), strict=strict)

    if dry_run:
        failed = sum(1 for r in result.results if r.get("status") == "error")
        return {
            "ok": result.ok,
            "path": path,
            "dry_run": True,
            "applied": result.applied,
            "failed": failed,
            "partial": bool(failed and result.applied),
            "results": result.results,
            "error": result.error,
            "suggestions": result.suggestions,
        }

    # Non-strict: persist partial applies; surface failures via ok=false + results.
    if not result.ok and (strict or result.applied == 0):
        return _err(
            path=path,
            applied=result.applied,
            results=result.results,
            error=result.error,
            suggestions=result.suggestions,
        )

    to_write = result.content
    okf = apo_okf.process_concept(
        vault_root=v.root,
        rel_path=path,
        content=result.content,
        bump_timestamp=True,
    )
    okf_meta = okf.as_response_fields()
    if not okf.ok:
        return _err(
            path=path,
            error=okf.error or "okf_validation",
            message=okf.message or "OKF validation failed",
            applied=result.applied,
            results=result.results,
            **{k: val for k, val in okf_meta.items() if k != "enforcement"},
            enforcement=okf.enforcement,
        )
    to_write = okf.content

    full.write_text(to_write, encoding="utf-8")
    _maybe_index(v, full, index)

    failed = sum(1 for r in result.results if r.get("status") == "error")
    out: dict[str, Any] = {
        "ok": result.ok,
        "path": path,
        "applied": result.applied,
        "failed": failed,
        "partial": bool(failed and result.applied),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
        "results": result.results,
    }
    out.update(okf_meta)
    if verbose:
        out["lines_added"] = result.lines_added
    return out


@mcp.tool(annotations=_MUTATE)
async def patch_note(
    path: str,
    ops: Annotated[list[PatchOp], Field(description=OPS_FIELD_DESC)],
    strict: bool = False,
    dry_run: bool = False,
    index: bool | None = None,
    verbose: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    """Batch mutate: set_field, delete_field, replace_text, replace_section, append/prepend, append_eof. Ops are typed by op."""
    return await asyncio.to_thread(
        _patch_note_sync, path, ops, strict, dry_run, index, verbose, expected_mtime, vault
    )


def _move_note_sync(
    src: str,
    dst: str,
    overwrite: bool = False,
    index: bool | None = None,
    vault: str = "",
) -> dict:
    try:
        v = _vault(vault)
        src_full = _safe_resolve(v, src)
        dst_full = _safe_resolve(v, dst)
    except (VaultError, ValueError) as e:
        return _err(src=src, dst=dst, error="bad_path", message=str(e))

    if not src_full.exists():
        return _err(src=src, dst=dst, error="not_found", message=f"source note not found: {src}")
    if dst_full.exists() and not overwrite:
        return _err(src=src, dst=dst, error="destination_exists", message="pass overwrite=true to replace")

    dst_full.parent.mkdir(parents=True, exist_ok=True)
    src_abs = str(src_full.resolve())
    os.replace(src_full, dst_full)

    purged = _purge_index(v, Path(src_abs))
    v.deferred = index_deferred.requeue_move(v.collection, src_abs, str(dst_full.resolve()))
    # requeue_move already wakes + enqueues dst; skip a second flock/_maybe_index.
    del index  # API compat

    out: dict[str, Any] = {"ok": True, "src": src, "dst": dst, "index_purged": purged, "mtime": _mtime(dst_full)}
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return out


@mcp.tool(annotations=_MUTATE)
async def move_note(
    src: str,
    dst: str,
    overwrite: bool = False,
    index: bool | None = None,
    vault: str = "",
) -> dict:
    """Atomic rename/move (updates index). Prefer over read+write+delete. overwrite=True replaces dst."""
    return await asyncio.to_thread(_move_note_sync, src, dst, overwrite, index, vault)


def _delete_note_sync(path: str, vault: str = "") -> dict:
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))
    if not full.exists():
        return _err(path=path, error="not_found", message="note not found")
    abs_path = str(full.resolve())
    purged = _purge_index(v, full)
    full.unlink()
    # Flocked dequeue — avoid unlocked discard + full-queue rewrite.
    v.deferred = index_deferred.dequeue_paths(v.collection, [abs_path])
    out: dict[str, Any] = {"ok": True, "path": path, "index_purged": purged}
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return out


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
async def delete_note(path: str, vault: str = "") -> dict:
    """Delete a note and purge its chunks from the search index. Cannot be undone."""
    return await asyncio.to_thread(_delete_note_sync, path, vault)


###############################################################################
# Tools — reading & search
###############################################################################


def _read_note_sync(path: str, heading: str | None = None, vault: str = "") -> dict:
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))
    if not full.exists():
        return _err(path=path, error="not_found", message=f"note not found: {path}")

    content = full.read_text(encoding="utf-8")
    out: dict[str, Any] = {"ok": True, "path": path, "mtime": _mtime(full), "size": full.stat().st_size}
    if heading:
        lines = normalize_lines(content)
        try:
            section = find_section(lines, heading)
        except PatchError as e:
            return _err(path=path, error=e.code, message=e.message, suggestions=e.suggestions)
        out["heading"] = f"{'#' * section.level} {section.title}"
        out["content"] = "\n".join(lines[section.heading_line : section.body_end])
    else:
        out["content"] = content
    return out


@mcp.tool(annotations=_RO)
async def read_note(path: str, heading: str | None = None, vault: str = "") -> dict:
    """Read a note; optional heading= returns that section only."""
    return await asyncio.to_thread(_read_note_sync, path, heading, vault)

@mcp.tool(annotations=_RO)
async def search_notes(
    query: str,
    top_k: int = 5,
    folder: str = "",
    vault: str = "",
    snippet_chars: int = _DEFAULT_SEARCH_SNIPPET,
) -> dict:
    """Hybrid BM25+vector content search (not frontmatter — use filter_notes). folder= scopes. Hits include chunk_hash/heading for append/expand. content is a snippet (snippet_chars; 0=full). Pass vault= for multi-index."""
    return await asyncio.to_thread(
        _search_notes_sync, query, top_k, folder, vault, snippet_chars
    )


def _search_notes_sync(
    query: str,
    top_k: int = 5,
    folder: str = "",
    vault: str = "",
    snippet_chars: int = _DEFAULT_SEARCH_SNIPPET,
) -> dict:
    try:
        v = _vault(vault)
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    folder_clean = folder.replace("\\", "/").strip("/")
    try:
        with _bound(v):
            from apo_engine.mcp_backend import shape_search_hits

            hits = apo_core.search(
                query,
                k=top_k,
                folder=folder_clean,
                snippet_chars=snippet_chars,
            )
            results = shape_search_hits(hits)
    except Exception as e:
        return _err(error="search_failed", message=str(e))
    return {"ok": True, "results": results, "vault": v.name}


def _expand_chunk_sync(
    chunk_hash: str,
    vault: str = "",
    scope: Literal["section", "chunk"] = "section",
) -> dict:
    try:
        v = _vault(vault)
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))

    need_text = scope == "chunk"
    chunk = _lookup_chunk(v, chunk_hash, include_text=need_text)
    if not chunk:
        return _err(error="anchor_not_found", message=f"chunk_hash {chunk_hash!r} not found in index")

    rel = (chunk.get("path") or "").replace("\\", "/")
    if not rel:
        return _err(error="anchor_not_found", message="chunk has no path")

    if scope == "chunk":
        heading = chunk.get("heading") or ""
        hlevel = int(chunk.get("heading_level") or 0)
        return {
            "ok": True,
            "path": rel,
            "heading": f"{'#' * hlevel} {heading}".strip() if heading else "",
            "start_line": int(chunk.get("start_line") or 1),
            "end_line": int(chunk.get("end_line") or 1),
            "content": chunk.get("content") or "",
            "scope": "chunk",
        }

    try:
        source = _safe_resolve(v, rel)
    except ValueError as e:
        return _err(error="anchor_not_found", message=str(e))
    if not source.exists():
        return _err(error="stale_index", message=f"source file missing: {rel}")

    lines = normalize_lines(source.read_text(encoding="utf-8"))
    section = section_from_chunk(
        lines,
        int(chunk.get("start_line", 1)),
        int(chunk.get("heading_level", 0)),
    )
    start = section.heading_line if section.title else section.body_start
    return {
        "ok": True,
        "path": rel,
        "heading": f"{'#' * section.level} {section.title}" if section.title else "",
        "start_line": start + 1,
        "end_line": section.body_end,
        "content": "\n".join(lines[start : section.body_end]),
        "scope": "section",
    }


@mcp.tool(annotations=_RO)
async def expand_chunk(
    chunk_hash: str,
    vault: str = "",
    scope: Literal["section", "chunk"] = "section",
) -> dict:
    """Expand search chunk_hash: scope=section (default, surrounding markdown) or chunk (indexed body, no disk read)."""
    return await asyncio.to_thread(_expand_chunk_sync, chunk_hash, vault, scope)


def _filter_notes_sync(
    where: dict,
    folder: str = "",
    limit: int = 20,
    vault: str = "",
) -> dict:
    try:
        v = _vault(vault)
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    if not isinstance(where, dict):
        return _err(error="bad_query", message="`where` must be an object (use {} to list all indexed notes in folder)")

    folder_clean = folder.replace("\\", "/").strip("/")
    # Traversal check only — filter is index-backed and must not require the dir on disk.
    if folder_clean:
        try:
            _safe_resolve(v, folder_clean)
        except ValueError as e:
            return _err(error="bad_path", message=str(e))

    with _bound(v):
        total, matches = apo_core.filter_notes(where, folder_clean, limit)
    notes = [
        {
            "path": path,
            "modified": datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
            "frontmatter": fm,
        }
        for mt, path, fm in matches
    ]
    return {"ok": True, "total": total, "notes": notes, "vault": v.name}


@mcp.tool(annotations=_RO)
async def filter_notes(
    where: dict,
    folder: str = "",
    limit: int = 20,
    vault: str = "",
) -> dict:
    """Frontmatter catalog (no embeddings). where: {} = all in folder; else field→scalar or {$eq,$ne,$lt,$lte,$gt,$gte,$contains,$exists,$in}. Newest first. Example: filter_notes({\"status\": {\"$in\": [\"active\", \"waiting\"]}}, folder=\"areas/threads\")."""
    return await asyncio.to_thread(_filter_notes_sync, where, folder, limit, vault)


def _backlinks_sync(path: str, limit: int = 100, vault: str = "") -> dict:
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))

    rel = str(Path(path.replace("\\", "/"))).removesuffix(".md")
    targets = {Path(rel).name.lower(), rel.lower()}
    # Title from cached frontmatter — no vault file read.
    with _bound(v):
        title = apo_core.frontmatter_field(path, "title")
        if isinstance(title, str) and title.strip():
            targets.add(title.strip().lower())

        exclude_source = ""
        try:
            if full.exists():
                exclude_source = str(full.relative_to(v.root))
        except ValueError:
            pass
        rows = apo_core.list_backlinks(targets, exclude_source, limit)
    hits = [{"path": src, "line": line, "text": text} for src, line, text in rows]
    return {"ok": True, "target": path, "total": len(hits), "backlinks": hits, "vault": v.name}


@mcp.tool(annotations=_RO)
async def backlinks(path: str, limit: int = 100, vault: str = "") -> dict:
    """Notes that [[wiki-link]] this path/stem/title (target need not exist)."""
    return await asyncio.to_thread(_backlinks_sync, path, limit, vault)


def _recent_activity_sync(limit: int = 10, folder: str = "", vault: str = "") -> dict:
    try:
        v = _vault(vault)
        base = _safe_resolve(v, folder) if folder else v.root
    except (VaultError, ValueError) as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder}")
    with _bound(v):
        rows = apo_core.recent_notes_preview(limit, folder)
    notes = [
        {
            "path": path,
            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "first_line": first_line.replace("\n", " ").strip(),
        }
        for path, mtime, first_line in rows
    ]
    return {"ok": True, "notes": notes, "vault": v.name}


@mcp.tool(annotations=_RO)
async def recent_activity(limit: int = 10, folder: str = "", vault: str = "") -> dict:
    """Most recently modified notes; optional folder= scope."""
    return await asyncio.to_thread(_recent_activity_sync, limit, folder, vault)


###############################################################################
# Tools — indexing
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

    watcher = _watcher_status()
    out: dict[str, Any] = {"ok": True, "queued": queued, "signaled": True, "watcher_running": watcher["running"]}
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
        watcher = _watcher_status()
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
    """Raw markdown content of a note, addressed as note://<vault>/<relative-path>."""
    v = _vault(vault)
    full = _safe_resolve(v, path)
    if not full.is_file():
        raise FileNotFoundError(f"note not found: {vault}/{path}")
    return full.read_text(encoding="utf-8")


@mcp.resource("memory://vaults", mime_type="application/json")
def vaults_resource() -> dict:
    """Registered vaults with their roots, collections, and top-level directories."""
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
# Lean mode — hide admin tools from list_tools / schema (opt-in)
###############################################################################

_ADMIN_TOOLS = frozenset({"reload_config", "memory_status", "reindex_deferred", "reindex"})


def _apply_lean_mode() -> bool:
    """If APO_MCP_LEAN is truthy, disable admin-tagged tools. Returns whether lean applied."""
    if not _env_truthy("APO_MCP_LEAN"):
        return False
    mcp.disable(tags={"admin"})
    return True


_LEAN_ACTIVE = _apply_lean_mode()


###############################################################################
# Entry point
###############################################################################

if __name__ == "__main__":
    mcp.run()
