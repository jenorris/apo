"""Shared vault operations — MCP and local RPC return the same {ok,…} shapes.

Read + write paths for gateways. Index writes stay watcher-owned (deferred enqueue).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from apo_engine import __version__, core, deferred as index_deferred, okf as apo_okf, vaults
from apo_engine.agent_args import resolve_top_k, resolve_where, slice_note_content
from apo_engine.markdown_patch import (
    PatchError,
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    normalize_lines,
    section_from_chunk,
)
from apo_engine.mcp_backend import shape_search_hits
from apo_engine.patch_ops import ops_to_dicts
from apo_engine.validation_hints import flatten_patch_failure_error


class OpsError(Exception):
    """Vault / path resolution failure with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(**kw: Any) -> dict[str, Any]:
    return {"ok": False, **kw}


def _binding(vault: str = "") -> vaults.VaultBinding:
    default, bindings = vaults.load_bindings()
    key = (vault or "").strip() or default
    if key not in bindings:
        raise OpsError("bad_vault", f"unknown vault {key!r}; available: {sorted(bindings)}")
    return bindings[key]


def _safe_resolve(root: Path, relative_path: str) -> Path:
    full = (root / relative_path).resolve()
    full.relative_to(root)  # raises ValueError on traversal
    return full


def _mtime(full: Path) -> float:
    return full.stat().st_mtime


def _check_mtime(full: Path, expected: float | None, path: str) -> dict[str, Any] | None:
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


def _enqueue_index(b: vaults.VaultBinding, full: Path) -> None:
    try:
        index_deferred.enqueue_index(b.collection, str(full.resolve()))
    except Exception:
        pass


def _enqueue_purge(b: vaults.VaultBinding, full: Path) -> bool:
    try:
        index_deferred.enqueue_purge(b.collection, str(full.resolve()))
        return True
    except Exception:
        return False


def _top_level_dirs(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def health() -> dict[str, Any]:
    default, bindings = vaults.load_bindings()
    return {
        "ok": True,
        "service": "apo-engine-rpc",
        "version": __version__,
        "default_vault": default,
        "vaults": sorted(bindings),
    }


def stats(*, vault: str = "") -> dict[str, Any]:
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    with vaults.bind(b):
        data = core.stats()
    data["ok"] = True
    data["vault"] = b.name
    return data


def search(
    query: str,
    *,
    top_k: int | None = None,
    folder: str = "",
    vault: str = "",
    snippet_chars: int = 240,
    exclude: list[str] | None = None,
    hybrid: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    k, err = resolve_top_k(top_k, limit)
    if err:
        return _err(error="bad_request", message=err)
    folder_clean = folder.replace("\\", "/").strip("/")
    try:
        with vaults.bind(b):
            hits = core.search(
                query,
                k=k,
                folder=folder_clean,
                snippet_chars=snippet_chars,
                exclude=exclude,
                hybrid=hybrid,
            )
            results = shape_search_hits(hits)
    except SystemExit as e:
        return _err(error="search_failed", message=str(e) or "index unavailable")
    except Exception as e:
        return _err(error="search_failed", message=str(e))
    return {"ok": True, "results": results, "vault": b.name}


def read_note(
    path: str,
    *,
    heading: str | None = None,
    vault: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))
    if not full.exists():
        return _err(path=path, error="not_found", message=f"note not found: {path}")

    raw = full.read_text(encoding="utf-8")
    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "mtime": full.stat().st_mtime,
        "size": full.stat().st_size,
        "vault": b.name,
    }
    try:
        sliced = slice_note_content(
            raw,
            heading=heading,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
        )
    except PatchError as e:
        return _err(
            path=path,
            error=e.code,
            message=e.message,
            suggestions=e.suggestions,
        )
    except ValueError as e:
        return _err(path=path, error="bad_request", message=str(e))
    if sliced["heading"]:
        out["heading"] = sliced["heading"]
    out["content"] = sliced["content"]
    out["start_line"] = sliced["start_line"]
    out["end_line"] = sliced["end_line"]
    out["truncated"] = sliced["truncated"]
    return out


def filter_notes(
    where: dict | None = None,
    *,
    folder: str = "",
    limit: int = 20,
    offset: int = 0,
    vault: str = "",
    filters: dict | None = None,
) -> dict[str, Any]:
    where_obj, where_err = resolve_where(where, filters)
    if where_err:
        return _err(error="bad_query", message=where_err)
    assert where_obj is not None
    if offset < 0:
        return _err(error="bad_request", message="offset must be >= 0")
    if limit < 0:
        return _err(error="bad_request", message="limit must be >= 0")
    try:
        b = _binding(vault)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    folder_clean = folder.replace("\\", "/").strip("/")
    if folder_clean:
        try:
            _safe_resolve(root, folder_clean)
        except ValueError as e:
            return _err(error="bad_path", message=str(e))

    with vaults.bind(b):
        total, matches = core.filter_notes(where_obj, folder_clean, limit, offset)
    notes = [
        {
            "path": path,
            "modified": datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
            "frontmatter": fm,
        }
        for mt, path, fm in matches
    ]
    return {
        "ok": True,
        "total": total,
        "offset": offset,
        "limit": limit,
        "notes": notes,
        "vault": b.name,
    }


def expand_chunk(
    chunk_hash: str,
    *,
    vault: str = "",
    scope: Literal["section", "chunk"] = "section",
) -> dict[str, Any]:
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    need_text = scope == "chunk"
    with vaults.bind(b):
        chunk = core.lookup_chunk(chunk_hash, include_text=need_text)
    if not chunk:
        return _err(
            error="anchor_not_found",
            message=f"chunk_hash {chunk_hash!r} not found in index",
        )

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
            "vault": b.name,
        }

    try:
        full = _safe_resolve(b.resolved().root, rel)
    except ValueError as e:
        return _err(error="anchor_not_found", message=str(e))
    if not full.exists():
        return _err(error="stale_index", message=f"source file missing: {rel}")

    lines = normalize_lines(full.read_text(encoding="utf-8"))
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
        "vault": b.name,
    }


def backlinks(path: str, *, limit: int = 100, vault: str = "") -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    rel = str(Path(path.replace("\\", "/"))).removesuffix(".md")
    targets = {Path(rel).name.lower(), rel.lower()}
    with vaults.bind(b):
        title = core.frontmatter_field(path, "title")
        if isinstance(title, str) and title.strip():
            targets.add(title.strip().lower())
        exclude_source = ""
        try:
            if full.exists():
                exclude_source = str(full.relative_to(root))
        except ValueError:
            pass
        rows = core.list_backlinks(targets, exclude_source, limit)
    hits = [{"path": src, "line": line, "text": text} for src, line, text in rows]
    return {
        "ok": True,
        "target": path,
        "total": len(hits),
        "backlinks": hits,
        "vault": b.name,
    }


def recent_activity(
    *,
    limit: int = 10,
    folder: str = "",
    vault: str = "",
) -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        base = _safe_resolve(root, folder) if folder else root
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    except ValueError as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder}")
    with vaults.bind(b):
        rows = core.recent_notes_preview(limit, folder)
    notes = [
        {
            "path": path,
            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "first_line": first_line.replace("\n", " ").strip(),
        }
        for path, mtime, first_line in rows
    ]
    return {"ok": True, "notes": notes, "vault": b.name}


###############################################################################
# Writes — enqueue index; watcher is sole index.db writer
###############################################################################


def write_note(
    path: str,
    content: str,
    *,
    append: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    if (guard := _check_mtime(full, expected_mtime, path)):
        return guard

    existed = full.exists()
    parts = Path(path.replace("\\", "/")).parts
    new_top = len(parts) > 1 and not (root / parts[0]).exists()

    okf_meta: dict[str, Any] = {}
    to_write = content
    if not (append and existed):
        okf = apo_okf.process_concept(vault_root=root, rel_path=path, content=content)
        okf_meta = okf.as_response_fields()
        if not okf.ok:
            return _err(
                path=path,
                error=okf.error or "okf_validation",
                message=okf.message or "OKF validation failed",
                **{k: val for k, val in okf_meta.items() if k != "enforcement"},
                enforcement=okf.enforcement,
            )
        to_write = okf.content

    full.parent.mkdir(parents=True, exist_ok=True)
    if append and existed:
        with full.open("a", encoding="utf-8") as f:
            f.write("\n" + content)
    else:
        full.write_text(to_write, encoding="utf-8")
    _enqueue_index(b, full)

    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "action": "appended" if (append and existed) else ("overwrote" if existed else "created"),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
        "vault": b.name,
    }
    out.update(okf_meta)
    if new_top:
        out["warning"] = (
            f"created new top-level directory {parts[0]!r} — "
            f"existing top-level dirs: {_top_level_dirs(root)}"
        )
    return out


def append_note(
    path: str,
    text: str,
    *,
    heading: str | None = None,
    chunk_hash: str | None = None,
    position: Literal["end", "start"] = "end",
    create: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    if (guard := _check_mtime(full, expected_mtime, path)):
        return guard

    if not full.exists():
        if not create:
            return _err(
                path=path,
                error="not_found",
                message="note not found (pass create=true to create)",
            )
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(minimal_note_stub(path), encoding="utf-8")

    content = full.read_text(encoding="utf-8")
    lines = normalize_lines(content)

    try:
        section = None
        anchor_label = "EOF"
        if chunk_hash:
            with vaults.bind(b):
                chunk = core.lookup_chunk(chunk_hash, include_text=False)
            if not chunk:
                return _err(
                    path=path,
                    error="anchor_not_found",
                    message=f"chunk_hash {chunk_hash!r} not found",
                )
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
    _enqueue_index(b, full)

    return {
        "ok": True,
        "path": path,
        "anchor": anchor_label,
        "detail": detail,
        "lines_added": max(0, len(merged) - len(lines)),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
        "vault": b.name,
    }


def patch_note(
    path: str,
    ops: list[Any],
    *,
    strict: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    if not full.exists():
        return _err(path=path, error="not_found", message="note not found")

    if (guard := _check_mtime(full, expected_mtime, path)):
        return guard

    content = full.read_text(encoding="utf-8")
    try:
        result = apply_patch(content, ops_to_dicts(ops), strict=strict)
    except TypeError as e:
        return _err(path=path, error="bad_request", message=str(e))

    if dry_run:
        failed = sum(1 for r in result.results if r.get("status") == "error")
        out_dry: dict[str, Any] = {
            "ok": result.ok,
            "path": path,
            "dry_run": True,
            "applied": result.applied,
            "failed": failed,
            "partial": bool(failed and result.applied),
            "results": result.results,
            "vault": b.name,
        }
        if result.error is not None:
            out_dry.update(
                flatten_patch_failure_error(
                    result.error, suggestions=result.suggestions or None
                )
            )
        elif result.suggestions:
            out_dry["suggestions"] = result.suggestions
        return out_dry

    if not result.ok and (strict or result.applied == 0):
        return _err(
            path=path,
            applied=result.applied,
            results=result.results,
            **flatten_patch_failure_error(
                result.error, suggestions=result.suggestions or None
            ),
        )

    to_write = result.content
    okf = apo_okf.process_concept(
        vault_root=root,
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
    _enqueue_index(b, full)

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
        "vault": b.name,
    }
    out.update(okf_meta)
    if verbose:
        out["lines_added"] = result.lines_added
    return out


def move_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    vault: str = "",
) -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        src_full = _safe_resolve(root, src)
        dst_full = _safe_resolve(root, dst)
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)
    except ValueError as e:
        return _err(src=src, dst=dst, error="bad_path", message=str(e))

    if not src_full.exists():
        return _err(src=src, dst=dst, error="not_found", message=f"source note not found: {src}")
    if dst_full.exists() and not overwrite:
        return _err(
            src=src,
            dst=dst,
            error="destination_exists",
            message="pass overwrite=true to replace",
        )

    dst_full.parent.mkdir(parents=True, exist_ok=True)
    src_abs = str(src_full.resolve())
    os.replace(src_full, dst_full)

    purged = _enqueue_purge(b, Path(src_abs))
    index_deferred.requeue_move(b.collection, src_abs, str(dst_full.resolve()))

    out: dict[str, Any] = {
        "ok": True,
        "src": src,
        "dst": dst,
        "index_purged": purged,
        "mtime": _mtime(dst_full),
        "vault": b.name,
    }
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return out


def delete_note(path: str, *, vault: str = "") -> dict[str, Any]:
    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))
    if not full.exists():
        return _err(path=path, error="not_found", message="note not found")
    abs_path = str(full.resolve())
    purged = _enqueue_purge(b, full)
    full.unlink()
    index_deferred.dequeue_paths(b.collection, [abs_path])
    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "index_purged": purged,
        "vault": b.name,
    }
    if not purged:
        out["warning"] = "purge not queued — watcher may retain stale chunks"
    return out
