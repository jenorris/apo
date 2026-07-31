"""Shared vault operations — MCP and local RPC return the same {ok,…} shapes.

Read + write paths for gateways. Index writes stay watcher-owned (deferred enqueue).
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from apo_engine import __version__, config, core, deferred as index_deferred, git_contract, okf as apo_okf, vaults
from apo_engine.agent_args import resolve_top_k, resolve_where, shape_note_read, project_frontmatter
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


WATCH_PID_FILE = Path.home() / ".apo" / "watch.pid"

_WATCHER_TIP = (
    "watcher not running — write is on disk and enqueued, but search won't "
    "update until apo-engine watch is up (just watch-status)"
)

_FOLDER_TIP = (
    "pass folder= when the PARA bucket is known — unscoped search is slower and noisier"
)

_MTIME_TIP = (
    "pass expected_mtime from the prior read/write for this path to avoid stale_write"
)

# Process-local: vault:path → last successful write monotonic time (soft mtime tip).
_recent_writes: dict[str, float] = {}
_RECENT_WRITE_TTL_S = 300.0


def _write_key(vault: str, path: str) -> str:
    return f"{(vault or '').strip()}:{path.replace(chr(92), '/')}"


def _attach_tip(out: dict[str, Any], tip: str) -> dict[str, Any]:
    """Soft agent habit hint (not a failure). Does not overwrite ``warning``."""
    if not out.get("ok") or not tip:
        return out
    existing = out.get("tip")
    out["tip"] = f"{existing}; {tip}" if existing else tip
    return out


def _attach_folder_tip(out: dict[str, Any], folder_clean: str) -> dict[str, Any]:
    if folder_clean:
        return out
    return _attach_tip(out, _FOLDER_TIP)


def _attach_mtime_tip(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    expected_mtime: float | None,
) -> dict[str, Any]:
    """Tip when a second in-process write to the same path omits ``expected_mtime``."""
    now = time.monotonic()
    stale = [k for k, t in _recent_writes.items() if now - t > _RECENT_WRITE_TTL_S]
    for k in stale:
        del _recent_writes[k]

    key = _write_key(vault or str(out.get("vault") or ""), path)
    prior = _recent_writes.get(key)
    if (
        expected_mtime is None
        and prior is not None
        and (now - prior) <= _RECENT_WRITE_TTL_S
    ):
        out = _attach_tip(out, _MTIME_TIP)
    if out.get("ok"):
        _recent_writes[key] = now
    return out


def _finalize_write(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    expected_mtime: float | None,
) -> dict[str, Any]:
    out = _attach_mtime_tip(
        out, vault=vault, path=path, expected_mtime=expected_mtime
    )
    return _attach_watcher_tip(out)


def watcher_status() -> dict[str, Any]:
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
            status["warning"] = (
                f"pid {pid} is alive but doesn't look like apo-engine watch "
                "(stale/recycled pid?)"
            )
            return status
    status["running"] = True
    return status


def _attach_watcher_tip(out: dict[str, Any]) -> dict[str, Any]:
    """Surface missing watcher on successful writes (lean has no memory_status)."""
    if not out.get("ok"):
        return out
    if watcher_status().get("running"):
        return out
    existing = out.get("warning")
    out["warning"] = f"{existing}; {_WATCHER_TIP}" if existing else _WATCHER_TIP
    return out


def _send_allow_roots() -> list[Path]:
    raw = (config.SEND_ALLOW_ROOTS or "").strip()
    if not raw:
        return [Path.home().expanduser().resolve()]
    roots: list[Path] = []
    for part in raw.split(":"):
        p = part.strip()
        if not p:
            continue
        roots.append(Path(p).expanduser().resolve())
    return roots or [Path.home().expanduser().resolve()]


def _resolve_send_src(src: str, vault_root: Path) -> Path:
    """Resolve a host-path markdown source for send_note.

    Raises OpsError with a stable code on failure.
    """
    raw = (src or "").strip()
    if not raw:
        raise OpsError("bad_path", "src host path required")
    # Absolute or ~/… only — relative paths are ambiguous across MCP cwd.
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise OpsError(
            "bad_path",
            f"src must be an absolute host path (got {src!r}); "
            "use ~/… or /… — relative paths are rejected",
        )
    try:
        full = expanded.resolve(strict=True)
    except FileNotFoundError as e:
        raise OpsError("not_found", f"source file not found: {src}") from e
    except OSError as e:
        raise OpsError("bad_path", f"cannot resolve src: {e}") from e

    if not full.is_file():
        raise OpsError("bad_path", f"src is not a file: {src}")
    if full.suffix.lower() != ".md":
        raise OpsError("bad_path", f"src must be a .md file (got suffix {full.suffix!r})")

    vault_res = vault_root.expanduser().resolve()
    try:
        full.relative_to(vault_res)
    except ValueError:
        pass
    else:
        raise OpsError(
            "use_move_note",
            "src is already inside the vault; use move_note for rename/archive "
            "(or write_note / patch_note to edit in place)",
        )

    allowed = False
    for root in _send_allow_roots():
        try:
            full.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        roots = ", ".join(str(r) for r in _send_allow_roots())
        raise OpsError(
            "forbidden_src",
            f"src {str(full)!r} is outside allowed roots ({roots}); "
            "set APO_SEND_ALLOW_ROOTS to extend",
        )

    try:
        size = full.stat().st_size
    except OSError as e:
        raise OpsError("bad_path", f"cannot stat src: {e}") from e
    if size > int(config.SEND_MAX_BYTES):
        raise OpsError(
            "too_large",
            f"src is {size} bytes; max APO_SEND_MAX_BYTES={config.SEND_MAX_BYTES}",
        )
    return full


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
    return _attach_folder_tip(
        {"ok": True, "results": results, "vault": b.name},
        folder_clean,
    )


def read_note(
    path: str,
    *,
    heading: str | None = None,
    vault: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    raw: bool = False,
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

    text = full.read_text(encoding="utf-8")
    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "mtime": full.stat().st_mtime,
        "size": full.stat().st_size,
        "vault": b.name,
    }
    try:
        shaped = shape_note_read(
            text,
            heading=heading,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            raw_content=raw,
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
    out["frontmatter"] = shaped["frontmatter"]
    if shaped["heading"]:
        out["heading"] = shaped["heading"]
    out["content"] = shaped["content"]
    out["start_line"] = shaped["start_line"]
    out["end_line"] = shaped["end_line"]
    out["truncated"] = shaped["truncated"]
    return out


def filter_notes(
    where: dict | None = None,
    *,
    folder: str = "",
    limit: int = 20,
    offset: int = 0,
    vault: str = "",
    filters: dict | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    where_obj, where_err = resolve_where(where, filters)
    if where_err:
        return _err(error="bad_query", message=where_err)
    assert where_obj is not None
    if offset < 0:
        return _err(error="bad_request", message="offset must be >= 0")
    if limit < 0:
        return _err(error="bad_request", message="limit must be >= 0")
    if fields is not None:
        if not isinstance(fields, list) or any(not isinstance(x, str) for x in fields):
            return _err(error="bad_request", message="fields must be a list of strings")
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
            "frontmatter": project_frontmatter(fm, fields),
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

    try:
        full = _safe_resolve(b.resolved().root, rel)
    except ValueError as e:
        return _err(error="anchor_not_found", message=str(e))

    if scope == "chunk":
        heading = chunk.get("heading") or ""
        hlevel = int(chunk.get("heading_level") or 0)
        out_chunk: dict[str, Any] = {
            "ok": True,
            "path": rel,
            "heading": f"{'#' * hlevel} {heading}".strip() if heading else "",
            "start_line": int(chunk.get("start_line") or 1),
            "end_line": int(chunk.get("end_line") or 1),
            "content": chunk.get("content") or "",
            "scope": "chunk",
            "vault": b.name,
        }
        if full.exists():
            out_chunk["mtime"] = _mtime(full)
        return out_chunk

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
        "mtime": _mtime(full),
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


def history(
    *,
    limit: int = 10,
    folder: str = "",
    path: str = "",
    vault: str = "",
) -> dict[str, Any]:
    """Browse by mtime, or file-level git history when ``path`` is set.

    - No ``path``: index-backed recent notes (same shape as legacy ``recent_activity``).
    - With ``path`` + active git contract: ``source=git`` + commit list.
    - With ``path`` but no git contract / no ``.git``: ``source=mtime`` metadata only.
    """
    try:
        b = _binding(vault)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    rel_path = (path or "").strip().replace("\\", "/")
    if rel_path:
        try:
            full = _safe_resolve(root, rel_path)
        except ValueError as e:
            return _err(path=rel_path, error="bad_path", message=str(e))
        if not full.exists():
            return _err(path=rel_path, error="not_found", message=f"note not found: {rel_path}")

        if git_contract.git_contract_active(root):
            try:
                commits = git_contract.git_file_log(root, rel_path, limit=limit)
            except Exception as e:
                return _err(
                    path=rel_path,
                    error="git_log_failed",
                    message=str(e),
                    vault=b.name,
                )
            return {
                "ok": True,
                "path": rel_path,
                "source": "git",
                "commits": commits,
                "vault": b.name,
            }

        # No git contract: mtime + optional index first-line preview.
        modified = datetime.fromtimestamp(full.stat().st_mtime).isoformat(timespec="seconds")
        preview = ""
        index_path = rel_path if rel_path.endswith(".md") else f"{rel_path}.md"
        with vaults.bind(b):
            try:
                db = core.reader_connect()
                db_row = db.execute(
                    "SELECT f.mtime, COALESCE(substr(c.text, 1, 120), '') "
                    "FROM files f LEFT JOIN chunks c ON c.path = f.path AND c.ord = 0 "
                    "WHERE f.path = ?",
                    (index_path,),
                ).fetchone()
                if db_row is None and index_path != rel_path:
                    db_row = db.execute(
                        "SELECT f.mtime, COALESCE(substr(c.text, 1, 120), '') "
                        "FROM files f LEFT JOIN chunks c ON c.path = f.path AND c.ord = 0 "
                        "WHERE f.path = ?",
                        (rel_path,),
                    ).fetchone()
                if db_row:
                    modified = datetime.fromtimestamp(db_row[0]).isoformat(timespec="seconds")
                    preview = (db_row[1] or "").replace("\n", " ").strip()
            except Exception:
                pass
        return {
            "ok": True,
            "path": rel_path,
            "source": "mtime",
            "modified": modified,
            "first_line": preview,
            "vault": b.name,
        }

    # Browse mode
    try:
        base = _safe_resolve(root, folder) if folder else root
    except ValueError as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder}")
    with vaults.bind(b):
        rows = core.recent_notes_preview(limit, folder)
    notes = [
        {
            "path": p,
            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "first_line": first_line.replace("\n", " ").strip(),
        }
        for p, mtime, first_line in rows
    ]
    return {"ok": True, "notes": notes, "vault": b.name}


def recent_activity(
    *,
    limit: int = 10,
    folder: str = "",
    vault: str = "",
    path: str = "",
) -> dict[str, Any]:
    """Frozen alias of :func:`history` through the v0.1.x line — prefer ``history``."""
    return history(limit=limit, folder=folder, path=path, vault=vault)


###############################################################################
# Writes — enqueue index; watcher is sole index.db writer
###############################################################################


def write_note(
    path: str,
    content: str,
    *,
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
    full.write_text(to_write, encoding="utf-8")
    _enqueue_index(b, full)

    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "action": "overwrote" if existed else "created",
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
    return _finalize_write(
        out, vault=b.name, path=path, expected_mtime=expected_mtime
    )


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

    return _finalize_write(
        {
            "ok": True,
            "path": path,
            "anchor": anchor_label,
            "detail": detail,
            "lines_added": max(0, len(merged) - len(lines)),
            "bytes": full.stat().st_size,
            "mtime": _mtime(full),
            "vault": b.name,
        },
        vault=b.name,
        path=path,
        expected_mtime=expected_mtime,
    )


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
    return _finalize_write(
        out, vault=b.name, path=path, expected_mtime=expected_mtime
    )


def move_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    expected_mtime: float | None = None,
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
    if (guard := _check_mtime(src_full, expected_mtime, src)):
        return {**guard, "src": src, "dst": dst}
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
    return _finalize_write(
        out, vault=b.name, path=dst, expected_mtime=expected_mtime
    )


def send_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    fields: dict[str, Any] | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    """Copy a host .md file into the vault (optional frontmatter merge). Leaves src in place."""
    try:
        b = _binding(vault)
        root = b.resolved().root
        src_full = _resolve_send_src(src, root)
        dst_full = _safe_resolve(root, dst)
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)
    except ValueError as e:
        return _err(src=src, dst=dst, error="bad_path", message=str(e))

    if (guard := _check_mtime(dst_full, expected_mtime, dst)):
        return {**guard, "src": src, "dst": dst}
    if dst_full.exists() and not overwrite:
        return _err(
            src=src,
            dst=dst,
            error="destination_exists",
            message="pass overwrite=true to replace",
        )

    try:
        content = src_full.read_text(encoding="utf-8")
    except OSError as e:
        return _err(src=src, dst=dst, error="bad_path", message=f"cannot read src: {e}")
    except UnicodeDecodeError as e:
        return _err(src=src, dst=dst, error="bad_path", message=f"src is not utf-8 text: {e}")

    fields_applied: list[str] = []
    if fields:
        if not isinstance(fields, dict):
            return _err(
                src=src,
                dst=dst,
                error="bad_request",
                message="fields must be an object of frontmatter key→value",
            )
        patch_ops = [
            {"op": "set_field", "field": str(k), "value": v}
            for k, v in fields.items()
        ]
        result = apply_patch(content, patch_ops, strict=False)
        if not result.ok and result.applied == 0:
            return _err(
                src=src,
                dst=dst,
                applied=result.applied,
                results=result.results,
                **flatten_patch_failure_error(
                    result.error, suggestions=result.suggestions or None
                ),
            )
        content = result.content
        fields_applied = [str(k) for k in fields]

    existed = dst_full.exists()
    parts = Path(dst.replace("\\", "/")).parts
    new_top = len(parts) > 1 and not (root / parts[0]).exists()

    okf = apo_okf.process_concept(vault_root=root, rel_path=dst, content=content)
    okf_meta = okf.as_response_fields()
    if not okf.ok:
        return _err(
            src=src,
            dst=dst,
            error=okf.error or "okf_validation",
            message=okf.message or "OKF validation failed",
            **{k: val for k, val in okf_meta.items() if k != "enforcement"},
            enforcement=okf.enforcement,
        )
    to_write = okf.content

    dst_full.parent.mkdir(parents=True, exist_ok=True)
    dst_full.write_text(to_write, encoding="utf-8")
    _enqueue_index(b, dst_full)

    out: dict[str, Any] = {
        "ok": True,
        "src": str(src_full),
        "dst": dst,
        "action": "overwrote" if existed else "created",
        "bytes": dst_full.stat().st_size,
        "mtime": _mtime(dst_full),
        "fields_applied": fields_applied,
        "vault": b.name,
    }
    out.update(okf_meta)
    if new_top:
        out["warning"] = (
            f"created new top-level directory {parts[0]!r} — "
            f"existing top-level dirs: {_top_level_dirs(root)}"
        )
    return _finalize_write(
        out, vault=b.name, path=dst, expected_mtime=expected_mtime
    )


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
    return _attach_watcher_tip(out)
