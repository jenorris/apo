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
from typing import Any, Literal

import yaml
from fastmcp import FastMCP
from apo_engine import config as apo_config
from apo_engine import core as apo_core
from apo_engine import deferred as index_deferred
from apo_engine.mcp_backend import ApoMem
from apo_engine.markdown_patch import (
    PatchError,
    _frontmatter_bounds,
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    normalize_lines,
    section_from_chunk,
)

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
    ingest_dir: str = "wiki"
    mem: ApoMem | None = None
    deferred: set[str] = dc_field(default_factory=set)


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
    """(Re)build the single default vault from env + runtime JSON.

    The engine binds NOTES_ROOT/INDEX once at import, so the registry holds exactly
    one vault rooted there. Runtime JSON may still override the collection
    (deferred-queue namespace) and ingest_dir; changing the vault root requires
    restarting the server with new APO_NOTES_ROOT / APO_INDEX env.
    """
    global VAULTS, DEFAULT_VAULT
    overrides = _read_runtime_overrides()
    coll = (
        _pick(overrides, "APO_COLLECTION", apo_config.COLLECTION) or apo_config.COLLECTION
    )
    ingest = (
        _pick(overrides, "APO_INGEST_DIR", apo_config.INGEST_DIR) or apo_config.INGEST_DIR
    )
    VAULTS = {
        "default": Vault(
            name="default",
            root=apo_config.NOTES_ROOT,
            collection=coll,
            ingest_dir=ingest,
            deferred=_load_deferred(coll),
        )
    }
    DEFAULT_VAULT = "default"


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


def _default_index_on_write() -> bool:
    return os.environ.get("APO_INDEX_ON_WRITE", "").lower() in ("1", "true", "yes")


async def _maybe_index(v: Vault, full: Path, index: bool | None) -> None:
    """Queue path for the watcher — MCP never writes index.db (single-writer policy)."""
    del index  # API compat; watcher owns all SQLite writes
    # enqueue_index returns the updated set — avoid a second flock/re-read.
    v.deferred = index_deferred.enqueue_index(v.collection, str(full.resolve()))


def _purge_index(v: Vault, full: Path) -> bool:
    """Queue index purge for the watcher. Best-effort."""
    try:
        index_deferred.enqueue_purge(v.collection, str(full.resolve()))
        return True
    except Exception:
        return False


def _lookup_chunk(v: Vault, chunk_hash: str) -> dict[str, Any] | None:
    try:
        return _ensure_mem(v).store.lookup_chunk(chunk_hash)
    except Exception:
        return None


def _jsonable(obj: Any) -> Any:
    """Coerce YAML-parsed values (dates, etc.) into JSON-safe structures."""
    return json.loads(json.dumps(obj, default=str))


def _parse_frontmatter(text: str) -> dict:
    lines = normalize_lines(text)
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return {}
    try:
        data = yaml.safe_load("\n".join(lines[bounds[0] + 1 : bounds[1]]))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _top_level_dirs(v: Vault) -> list[str]:
    if not v.root.exists():
        return []
    return sorted(p.name for p in v.root.iterdir() if p.is_dir() and not p.name.startswith("."))


###############################################################################
# Server
###############################################################################

mcp = FastMCP(
    "Apo",
    instructions=(
        "Apo — persistent semantic memory for AI agents: plain Markdown files under a single "
        "vault root, indexed into sqlite-vec (hybrid FTS5 BM25 + dense vector search). Files are the "
        "source of truth; the index is rebuildable. All paths are vault-relative; omit `vault` "
        "to use the default vault. "
        "Write routing: new note → write_note; add to a log/section → append_note; frontmatter "
        "or targeted replace → patch_note; relocate → move_note (never rewrite+delete by hand). "
        "search_notes results carry anchors (chunk_hash, heading, start_line) that feed directly "
        "into append_note / patch_note / expand_chunk — no read_note round trip needed. "
        "Query structured frontmatter with filter_notes; trace [[wiki-links]] with backlinks. "
        "MCP never writes index.db — writes enqueue paths in ~/.apo/deferred-*.json; "
        "apo-engine watch (launchd) is the sole index writer. Call reindex_deferred() after "
        "batch sweeps to wake the watcher. memory_status() reports vault health and queue depth."
    ),
)

# Load vault registry at import (fast); the index backend connects lazily per vault.
_load_vaults()


###############################################################################
# Tools — config & status
###############################################################################


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def reload_config() -> dict:
    """Rebuild the vault registry from env + optional runtime JSON, dropping the cached index backend.

    Use after editing the runtime JSON file (``APO_RUNTIME_CONFIG``; default
    ``~/.apo/mcp-runtime.<collection>.json``) to apply changes without restarting the
    MCP host. Supported JSON keys: ``APO_COLLECTION``, ``APO_INGEST_DIR``.
    The vault root is fixed at process start (APO_NOTES_ROOT / APO_INDEX env) — changing
    it requires a server restart.
    """
    _load_vaults()
    return {
        "ok": True,
        "default_vault": DEFAULT_VAULT,
        "vaults": {
            name: {"root": str(v.root), "collection": v.collection, "ingest_dir": v.ingest_dir}
            for name, v in VAULTS.items()
        },
        "runtime_file": str(_runtime_config_path()),
    }


@mcp.tool(annotations=_RO)
async def memory_status() -> dict:
    """Report vault roots, index health, deferred-index queues, and watcher state.

    Use to self-diagnose before retrying failed search/index calls.
    """
    vaults: dict[str, Any] = {}
    for name, v in VAULTS.items():
        info: dict[str, Any] = {
            "root": str(v.root),
            "root_exists": v.root.exists(),
            "collection": v.collection,
            "ingest_dir": v.ingest_dir,
            "default": name == DEFAULT_VAULT,
            "deferred_queue": len(v.deferred),
        }
        try:
            info["indexed_chunks"] = _ensure_mem(v).store.count()
            info["index"] = "ok"
        except Exception as e:
            info["index"] = f"error: {e}"
        vaults[name] = info

    watcher = {"pid_file": str(WATCH_PID_FILE), "running": False}
    try:
        pid = int(WATCH_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        watcher.update(pid=pid, running=True)
    except (OSError, ValueError):
        pass

    return {
        "ok": True,
        "default_vault": DEFAULT_VAULT,
        "vaults": vaults,
        "watcher": watcher,
        "runtime_file": str(_runtime_config_path()),
        "index_on_write_default": _default_index_on_write(),
    }


###############################################################################
# Tools — writing
###############################################################################


@mcp.tool(annotations=_MUTATE)
async def write_note(
    path: str,
    content: str,
    append: bool = False,
    index: bool | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    """Create a note, or fully overwrite an existing one.

    For edits to existing notes prefer append_note (additive) or patch_note (targeted
    mutation) — they avoid clobbering concurrent changes and need no prior read.

    Args:
        path: Vault-relative path, e.g. 'notes/topic.md'.
        content: Markdown content to write.
        append: If True, append to the raw file tail instead of overwriting.
        index: Deprecated — always queues for the watcher (single-writer policy).
        expected_mtime: If set, fail with stale_write when the file changed since this mtime.
        vault: Vault name; empty = default vault.
    """
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

    full.parent.mkdir(parents=True, exist_ok=True)
    if append and existed:
        with full.open("a", encoding="utf-8") as f:
            f.write("\n" + content)
    else:
        full.write_text(content, encoding="utf-8")
    await _maybe_index(v, full, index)

    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "action": "appended" if (append and existed) else ("overwrote" if existed else "created"),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
    }
    if new_top:
        out["warning"] = (
            f"created new top-level directory {parts[0]!r} — "
            f"existing top-level dirs: {_top_level_dirs(v)}"
        )
    return out


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
    """Add content to a note (under a heading, at an indexed chunk, or at the file tail).

    Examples:
        append_note("logs/2026-07-09.md", "**15:30** — …\\n\\n",
                    heading="## Session log", position="start")
        append_note("threads/foo.md", "- update\\n", heading="## History")
        append_note("threads/foo.md", "- follow-up\\n", chunk_hash="<from search_notes>")

    Anchor resolution: chunk_hash → heading → file tail (EOF).
    On anchor_not_found the error includes fuzzy heading suggestions.
    """
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
            chunk = _lookup_chunk(v, chunk_hash)
            if not chunk:
                return _err(path=path, error="anchor_not_found", message=f"chunk_hash {chunk_hash!r} not found")
            chunk_source = _display_source(v, chunk.get("source", ""))
            if chunk_source and chunk_source != path.replace("\\", "/"):
                return _err(
                    path=path,
                    error="path_mismatch",
                    message=f"chunk_hash belongs to {chunk_source!r}, not {path!r}",
                )
            try:
                Path(chunk.get("source", "")).resolve().relative_to(v.root)
            except ValueError:
                return _err(path=path, error="anchor_not_found", message="chunk source outside vault root")
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
    await _maybe_index(v, full, index)

    return {
        "ok": True,
        "path": path,
        "anchor": anchor_label,
        "detail": detail,
        "lines_added": max(0, len(merged) - len(lines)),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
    }


@mcp.tool(annotations=_MUTATE)
async def patch_note(
    path: str,
    ops: list[dict],
    strict: bool = False,
    dry_run: bool = False,
    index: bool | None = None,
    verbose: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict:
    """Mutate a note in place — frontmatter fields, targeted replace, batch upsert.

    Ops: set_field, delete_field, replace_text (optional scope.heading + count),
    replace_section, append / prepend (batch only), append_eof.

    Example batch upsert (history bullet + frontmatter in one call):
        patch_note("threads/foo.md", [
            {"op": "append", "heading": "## History", "text": "- 2026-07-09 …"},
            {"op": "set_field", "field": "last_checked", "value": "2026-07-09 15:30"},
            {"op": "set_field", "field": "status", "value": "resolved"},
        ])
    """
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
    result = apply_patch(content, ops, strict=strict)

    if dry_run:
        return {
            "ok": result.ok,
            "path": path,
            "dry_run": True,
            "applied": result.applied,
            "results": result.results,
            "error": result.error,
            "suggestions": result.suggestions,
        }

    if not result.ok:
        return _err(
            path=path,
            applied=result.applied,
            results=result.results,
            error=result.error,
            suggestions=result.suggestions,
        )

    full.write_text(result.content, encoding="utf-8")
    await _maybe_index(v, full, index)

    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "applied": result.applied,
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
    }
    if verbose:
        out["results"] = result.results
        out["lines_added"] = result.lines_added
    return out


@mcp.tool(annotations=_MUTATE)
async def move_note(
    src: str,
    dst: str,
    overwrite: bool = False,
    index: bool | None = None,
    vault: str = "",
) -> dict:
    """Move or rename a note, keeping the search index consistent.

    Removes the old path's indexed chunks and queues (or performs) indexing of the
    new path. Always prefer this over read + write + delete: it is atomic on disk
    and never leaves stale chunks pointing at the old location.

    Args:
        src: Current vault-relative path.
        dst: New vault-relative path (parent dirs are created).
        overwrite: Allow replacing an existing destination note.
        index: Deprecated — always queues for the watcher (single-writer policy).
        vault: Vault name; empty = default vault.
    """
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
    v.deferred.discard(src_abs)
    _save_deferred(v)
    await _maybe_index(v, dst_full, index)

    out: dict[str, Any] = {"ok": True, "src": src, "dst": dst, "index_purged": purged, "mtime": _mtime(dst_full)}
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return out


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
async def delete_note(path: str, vault: str = "") -> dict:
    """Delete a note and purge its chunks from the search index. Cannot be undone."""
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
    v.deferred.discard(abs_path)
    _save_deferred(v)
    out: dict[str, Any] = {"ok": True, "path": path, "index_purged": purged}
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return out


###############################################################################
# Tools — reading & search
###############################################################################


@mcp.tool(annotations=_RO)
async def read_note(path: str, heading: str | None = None, vault: str = "") -> dict:
    """Read a note, optionally scoped to one section.

    Args:
        path: Vault-relative path.
        heading: If set (e.g. '## Next action'), return only that section —
            avoids loading large notes when one section is needed.
        vault: Vault name; empty = default vault.
    """
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
async def search_notes(query: str, top_k: int = 5, folder: str = "", vault: str = "") -> dict:
    """Hybrid semantic + BM25 **search** across indexed note content.

    Contrast with ``filter_notes`` (frontmatter catalog filter). Use for meaning /
    recall: “what text is about this?”

    Each result carries write-ready anchors: pass chunk_hash straight to
    append_note / expand_chunk, or use heading with append_note / patch_note —
    no read_note round trip needed.

    Args:
        query: Free-text search query.
        top_k: Number of results to return.
        folder: Scope search to this vault-relative subfolder (e.g. 'projects/').
        vault: Vault name; empty = default vault.
    """
    try:
        v = _vault(vault)
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    source_prefix = str(v.root / folder) if folder else None
    try:
        results = await _ensure_mem(v).search(query, top_k=top_k, source_prefix=source_prefix)
    except Exception as e:
        return _err(error="search_failed", message=str(e))

    rows = []
    for r in results:
        src_abs = r.get("source", "")
        modified = None
        try:
            modified = datetime.fromtimestamp(Path(src_abs).stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            pass
        rows.append({
            "content": r.get("content", ""),
            "score": round(float(r.get("score", 0)), 4),
            "source": _display_source(v, src_abs),
            "chunk_hash": r.get("chunk_hash", ""),
            "heading": r.get("heading", ""),
            "heading_level": r.get("heading_level", 0),
            "start_line": r.get("start_line", 0),
            "end_line": r.get("end_line", 0),
            "modified": modified,
        })
    return {"ok": True, "results": rows}


@mcp.tool(annotations=_RO)
async def expand_chunk(chunk_hash: str, vault: str = "") -> dict:
    """Expand a search-result chunk to its full surrounding markdown section.

    Use chunk_hash values returned by search_notes for progressive recall.
    """
    try:
        v = _vault(vault)
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    chunk = _lookup_chunk(v, chunk_hash)
    if not chunk:
        return _err(error="anchor_not_found", message=f"chunk_hash {chunk_hash!r} not found in index")

    source = Path(chunk.get("source", "")).expanduser().resolve()
    try:
        source.relative_to(v.root)
    except ValueError:
        return _err(error="anchor_not_found", message=f"chunk source outside vault root: {source}")
    if not source.exists():
        return _err(error="stale_index", message=f"source file missing: {_display_source(v, str(source))}")

    lines = normalize_lines(source.read_text(encoding="utf-8"))
    section = section_from_chunk(
        lines,
        int(chunk.get("start_line", 1)),
        int(chunk.get("heading_level", 0)),
    )
    start = section.heading_line if section.title else section.body_start
    return {
        "ok": True,
        "path": _display_source(v, str(source)),
        "heading": f"{'#' * section.level} {section.title}" if section.title else "",
        "start_line": start + 1,
        "end_line": section.body_end,
        "content": "\n".join(lines[start : section.body_end]),
    }


@mcp.tool(annotations=_RO)
async def filter_notes(
    where: dict,
    folder: str = "",
    limit: int = 20,
    vault: str = "",
) -> dict:
    """Filter notes by YAML frontmatter (deterministic catalog query — no embeddings).

    Contrast with ``search_notes`` (ranked content recall). Use for status / tag /
    date sweeps: “which notes match these fields?”

    `where` maps field names to a scalar (loose equality; list fields match by
    membership) or an operator object:
        {"$eq": x} {"$ne": x} {"$lt": x} {"$lte": x} {"$gt": x} {"$gte": x}
        {"$contains": x}   substring (strings) or membership (lists)
        {"$exists": bool}
    ISO dates compare correctly as strings.

    Examples:
        filter_notes({"status": "active"}, folder="threads/")
        filter_notes({"last_checked": {"$lt": "2026-07-01"}, "status": {"$ne": "resolved"}})
        filter_notes({"tags": {"$contains": "compliance"}})

    Returns matches sorted by modification time (newest first) with full frontmatter.
    """
    try:
        v = _vault(vault)
        base = _safe_resolve(v, folder) if folder else v.root
    except (VaultError, ValueError) as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder}")
    if not isinstance(where, dict) or not where:
        return _err(error="bad_query", message="`where` must be a non-empty object of field conditions")

    total, matches = await asyncio.to_thread(apo_core.filter_notes, where, folder, limit)
    notes = [
        {
            "path": path,
            "modified": datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
            "frontmatter": _jsonable(fm),
        }
        for mt, path, fm in matches
    ]
    return {"ok": True, "total": total, "notes": notes}


@mcp.tool(annotations=_RO)
async def backlinks(path: str, limit: int = 100, vault: str = "") -> dict:
    """Find notes that reference this note via [[wiki-links]].

    Matches links against the note's file stem, its vault-relative path (with or
    without .md), and its frontmatter title. The target itself need not exist yet.
    """
    try:
        v = _vault(vault)
        full = _safe_resolve(v, path)
    except (VaultError, ValueError) as e:
        return _err(path=path, error="bad_path", message=str(e))

    rel = str(Path(path.replace("\\", "/"))).removesuffix(".md")
    targets = {Path(rel).name.lower(), rel.lower()}
    if full.exists():
        title = _parse_frontmatter(full.read_text(encoding="utf-8")).get("title")
        if isinstance(title, str) and title.strip():
            targets.add(title.strip().lower())

    exclude_source = str(full.relative_to(v.root)) if full.exists() else ""
    rows = await asyncio.to_thread(apo_core.list_backlinks, targets, exclude_source, limit)
    hits = [{"path": src, "line": line, "text": text} for src, line, text in rows]
    return {"ok": True, "target": path, "total": len(hits), "backlinks": hits}


###############################################################################
# Tools — navigation
###############################################################################


@mcp.tool(annotations=_RO)
async def list_directory(directory: str = "", vault: str = "") -> dict:
    """List notes and subdirectories within the vault.

    Args:
        directory: Vault-relative path (empty = vault root).
        vault: Vault name; empty = default vault.
    """
    try:
        v = _vault(vault)
        target = _safe_resolve(v, directory) if directory else v.root
    except (VaultError, ValueError) as e:
        return _err(error="bad_path", message=str(e))
    if not target.exists():
        return _err(error="not_found", message=f"directory not found: {directory}")
    entries = []
    for p in sorted(target.iterdir()):
        if p.name.startswith("."):
            continue
        entries.append({
            "name": p.name,
            "type": "directory" if p.is_dir() else "note",
            "path": str(p.relative_to(v.root)),
            "size": p.stat().st_size if p.is_file() else None,
        })
    return {"ok": True, "path": directory, "entries": entries}


@mcp.tool(annotations=_RO)
async def recent_activity(limit: int = 10, folder: str = "", vault: str = "") -> dict:
    """Return the most recently modified markdown notes (optionally scoped to a folder)."""
    try:
        v = _vault(vault)
        base = _safe_resolve(v, folder) if folder else v.root
    except (VaultError, ValueError) as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder}")
    rows = await asyncio.to_thread(apo_core.recent_notes, limit, folder)
    notes = []
    for path, mtime in rows:
        first_line = ""
        try:
            first_line = (v.root / path).read_text(encoding="utf-8").splitlines()[0][:120]
        except (OSError, IndexError):
            pass
        notes.append({
            "path": path,
            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "first_line": first_line,
        })
    return {"ok": True, "notes": notes}


###############################################################################
# Tools — indexing
###############################################################################


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def reindex_deferred(vault: str = "") -> dict:
    """Signal the watcher to flush the deferred index queue.

    MCP does not write index.db — the watcher consumes ~/.apo/deferred-*.json.
    Call at the end of batch sweeps for faster pickup (otherwise watcher poll/events).
    """
    try:
        targets = list(VAULTS.values()) if not vault.strip() else [_vault(vault)]
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))

    queued = 0
    for v in targets:
        index_deferred.touch_wake(v.collection)
        v.deferred = index_deferred.load_index_queue(v.collection)
        queued += len(v.deferred)
    return {"ok": True, "queued": queued, "signaled": True}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def reindex(force: bool = False, vault: str = "") -> dict:
    """Signal the watcher to rebuild the index (also prunes chunks of deleted files).

    MCP does not run index_vault directly — the watcher is the sole SQLite writer.

    Args:
        force: Re-embed all content even if unchanged (slow).
        vault: Vault name; empty = default vault.
    """
    try:
        v = _vault(vault)
        index_deferred.signal_rebuild(v.collection, force=force)
        v.deferred.clear()
        index_deferred.save_index_queue(v.collection, set())
        return {"ok": True, "vault": v.name, "rebuild_signaled": True, "force": force}
    except VaultError as e:
        return _err(error="bad_vault", message=str(e))
    except Exception as e:
        return _err(error="reindex_failed", message=str(e))


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
# Entry point
###############################################################################

if __name__ == "__main__":
    mcp.run()
