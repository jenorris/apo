"""Shared vault operations — MCP and local RPC return the same {ok,…} shapes.

Read + write paths for gateways. Index writes stay watcher-owned (deferred enqueue).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apo_engine import (
    __version__,
    config,
    core,
    deferred as index_deferred,
    git_contract,
    git_sync,
    okf as apo_okf,
    vault_contracts,
    vault_desk,
    vault_project,
    vaults,
)
from apo_engine.agent_args import (
    project_frontmatter,
    resolve_body_text,
    resolve_top_k,
    resolve_where,
    shape_note_read,
)
from apo_engine.markdown_patch import (
    PatchError,
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    normalize_lines,
    section_from_chunk,
)
from apo_engine.note_format import ensure_indexed_path, is_yaml_note
from apo_engine.yaml_patch import apply_yaml_patch
from apo_engine.chunk_anchor import materialize_ops_chunk_hashes, resolve_chunk_anchor
from apo_engine.mcp_backend import shape_search_hits
from apo_engine.patch_ops import ops_to_dicts
from apo_engine.validation_hints import flatten_patch_failure_error
from apo_engine.write_guard import (
    PathTouch,
    WriteRegions,
    attach_region_hashes,
    check_write_precondition,
    classify_append_regions,
    classify_patch_regions,
    content_hash as region_content_hash,
    snapshot_from_content,
)


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
    """Whole-file mtime CAS (place_note / simple paths). Prefer ``_guard_write``."""
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
            stale_region="whole_file",
        )
    return None


def _guard_write(
    full: Path,
    path: str,
    *,
    vault: str,
    regions: WriteRegions,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    content: str | None = None,
    chunk_section: Any = None,
) -> dict[str, Any] | None:
    """mtime + FM/body/section precondition. ``None`` means allow."""
    if (
        expected_mtime is None
        and not (expected_frontmatter_hash or "").strip()
        and not (expected_body_hash or "").strip()
        and not (expected_content_hash or "").strip()
    ):
        return None
    if not full.exists():
        return None
    actual = full.stat().st_mtime
    touch = _recent_touches.get(_write_key(vault, path))
    err = check_write_precondition(
        path=path,
        actual_mtime=actual,
        expected_mtime=expected_mtime,
        content=content,
        regions=regions,
        expected_frontmatter_hash=expected_frontmatter_hash,
        expected_body_hash=expected_body_hash,
        expected_content_hash=expected_content_hash,
        touch=touch if isinstance(touch, PathTouch) else None,
        chunk_section=chunk_section,
    )
    return err


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

# Process-local: vault:path → PathTouch (mtime + region hashes).
# Touches from read_note / expand_chunk / successful writes drive soft mtime tips
# and FM/body/section precondition fallbacks when expected_mtime is stale.
_recent_touches: dict[str, PathTouch] = {}
_RECENT_TOUCH_TTL_S = 300.0
# Back-compat alias for tests that clear the habit map.
_recent_writes = _recent_touches


def _write_key(vault: str, path: str) -> str:
    return f"{(vault or '').strip()}:{path.replace(chr(92), '/')}"


def _prune_recent_touches(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    stale = [
        k for k, touch in _recent_touches.items() if now - touch.mono > _RECENT_TOUCH_TTL_S
    ]
    for k in stale:
        del _recent_touches[k]


def _record_path_touch(
    vault: str,
    path: str,
    file_mtime: float | None = None,
    content: str | None = None,
    *,
    heading: str | None = None,
) -> None:
    """Record a successful read/expand/write so a follow-up write can tip expected_mtime."""
    now = time.monotonic()
    _prune_recent_touches(now)
    key = _write_key(vault, path)
    if content is not None:
        _recent_touches[key] = snapshot_from_content(
            now, file_mtime, content, heading=heading
        )
        return
    prior = _recent_touches.get(key)
    if prior is not None:
        prior.mono = now
        if file_mtime is not None:
            prior.mtime = file_mtime
        return
    _recent_touches[key] = PathTouch(mono=now, mtime=file_mtime)


def _attach_tip(out: dict[str, Any], tip: str) -> dict[str, Any]:
    """Soft agent habit hint (not a failure). Does not overwrite ``warning``."""
    if not out.get("ok") or not tip:
        return out
    existing = out.get("tip")
    out["tip"] = f"{existing}; {tip}" if existing else tip
    return out


def _attach_folder_tip(
    out: dict[str, Any],
    folder_clean: str,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    if folder_clean:
        return out
    tip = _FOLDER_TIP
    if root is not None:
        dirs = _top_level_dirs(root)
        if dirs:
            shown = "|".join(dirs[:8])
            tip = (
                f"pass folder= ({shown}) when the PARA bucket is known — "
                "unscoped search is slower and noisier"
            )
    return _attach_tip(out, tip)


def _attach_mtime_tip(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    expected_mtime: float | None,
    content: str | None = None,
) -> dict[str, Any]:
    """Tip when a follow-up write omits ``expected_mtime`` after a recent read/write."""
    now = time.monotonic()
    _prune_recent_touches(now)

    key = _write_key(vault or str(out.get("vault") or ""), path)
    prior = _recent_touches.get(key)
    if (
        expected_mtime is None
        and prior is not None
        and (now - prior.mono) <= _RECENT_TOUCH_TTL_S
    ):
        stored_mtime = prior.mtime
        if stored_mtime is not None:
            tip = f"pass expected_mtime={stored_mtime} to avoid stale_write"
        else:
            tip = _MTIME_TIP
        out = _attach_tip(out, tip)
    if out.get("ok"):
        new_mtime = out.get("mtime")
        file_mtime = float(new_mtime) if isinstance(new_mtime, (int, float)) else (
            prior.mtime if prior else None
        )
        if content is not None:
            _recent_touches[key] = snapshot_from_content(now, file_mtime, content)
        elif prior is not None:
            prior.mono = now
            prior.mtime = file_mtime
            fm = out.get("frontmatter_hash")
            body = out.get("body_hash")
            if isinstance(fm, str):
                prior.frontmatter_hash = fm
            if isinstance(body, str):
                prior.body_hash = body
        else:
            _recent_touches[key] = PathTouch(mono=now, mtime=file_mtime)
    return out


def _finalize_write(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    expected_mtime: float | None,
    content: str | None = None,
) -> dict[str, Any]:
    out = _attach_mtime_tip(
        out,
        vault=vault,
        path=path,
        expected_mtime=expected_mtime,
        content=content,
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


def _missing_folder_warning(root: Path, folder_clean: str, vault_name: str) -> str | None:
    """Non-fatal warning when a folder= scope does not exist on disk.

    A typo'd folder (or the right folder in the wrong vault) otherwise returns
    a silent empty result set that agents read as \"no knowledge here\".
    """
    if not folder_clean:
        return None
    try:
        full = _safe_resolve(root, folder_clean)
    except ValueError:
        return None  # traversal is rejected by callers before this point
    if full.is_dir():
        return None
    return (
        f"folder {folder_clean!r} does not exist in vault {vault_name!r} — "
        "results are empty by construction; check spelling or pass vault=. "
        f"Top-level dirs: {_top_level_dirs(root)}"
    )


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


def _dedupe_vault_names(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        n = (raw or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _resolve_search_bindings(
    vault: str = "",
    vaults_arg: list[str] | None = None,
) -> list[vaults.VaultBinding]:
    """Resolve single ``vault=`` or multi ``vaults=`` (not both)."""
    vault_s = (vault or "").strip()
    if vaults_arg is not None:
        multi = _dedupe_vault_names(list(vaults_arg))
        if not multi:
            raise OpsError(
                "bad_request",
                "vaults= must list at least one non-empty vault name",
            )
        if vault_s:
            raise OpsError(
                "bad_request",
                "pass vault= or vaults=, not both",
            )
        _default, bindings = vaults.load_bindings()
        resolved: list[vaults.VaultBinding] = []
        for name in multi:
            if name not in bindings:
                raise OpsError(
                    "bad_request",
                    f"unknown vault {name!r}; available: {sorted(bindings)}",
                )
            resolved.append(bindings[name])
        return resolved
    return [_binding(vault_s)]


def _search_degraded_warning(degraded: str) -> str:
    if config.EMBED_BACKEND == "ollama":
        fix = (
            "results are keyword-only (BM25) until the embed backend is back — "
            f"check the Ollama daemon (`just ollama`, APO_OLLAMA_URL={config.OLLAMA_URL}) "
            f"and that the model is pulled (`ollama pull {config.MODEL_NAME}`)"
        )
    else:
        fix = (
            "results are keyword-only (BM25) until the embed backend is back — "
            "check the fastembed install (pip install -e '.[cpu]') and APO_MODEL"
        )
    return f"{degraded}; {fix}"


def _search_one_vault(
    query: str,
    b: vaults.VaultBinding,
    *,
    k: int,
    folder_clean: str,
    snippet_chars: int,
    exclude: list[str] | None,
    hybrid: bool,
    stamp_vault: bool,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Run hybrid search in one vault. Returns (rows, warnings, reranked)."""
    warnings: list[str] = []
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
        rr = core.last_search_rerank()
        degraded = core.last_search_degraded()
    if stamp_vault:
        for row in results:
            row["vault"] = b.name
    reranked = False
    if rr is not None:
        if rr.get("applied"):
            reranked = True
        elif rr.get("detail"):
            detail = f"rerank unavailable ({rr['detail']}) — fused order returned"
            warnings.append(f"vault {b.name}: {detail}" if stamp_vault else detail)
    if degraded:
        detail = _search_degraded_warning(degraded)
        warnings.append(f"vault {b.name}: {detail}" if stamp_vault else detail)
    missing = _missing_folder_warning(b.resolved().root, folder_clean, b.name)
    if missing:
        warnings.append(missing)
    return results, warnings, reranked


def search(
    query: str,
    *,
    top_k: int | None = None,
    folder: str = "",
    vault: str = "",
    vaults: list[str] | None = None,
    snippet_chars: int = 240,
    exclude: list[str] | None = None,
    hybrid: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    try:
        targets = _resolve_search_bindings(vault, vaults)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    k, err = resolve_top_k(top_k, limit)
    if err:
        return _err(error="bad_request", message=err)
    folder_clean = folder.replace("\\", "/").strip("/")
    for b in targets:
        if folder_clean:
            try:
                _safe_resolve(b.resolved().root, folder_clean)
            except ValueError as e:
                return _err(error="bad_path", message=str(e))
    # Unscoped searches inherit APO_SEARCH_EXCLUDE (noise folders like session
    # logs); folder-scoped or caller-provided exclude= always wins.
    default_exclude: list[str] | None = None
    effective_exclude = exclude
    if not effective_exclude and not folder_clean and config.SEARCH_EXCLUDE_DEFAULT:
        default_exclude = list(config.SEARCH_EXCLUDE_DEFAULT)
        effective_exclude = default_exclude

    fanout = len(targets) > 1
    all_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    any_reranked = False
    failed = 0
    for b in targets:
        try:
            rows, w, reranked = _search_one_vault(
                query,
                b,
                k=k,
                folder_clean=folder_clean,
                snippet_chars=snippet_chars,
                exclude=effective_exclude,
                hybrid=hybrid,
                stamp_vault=fanout,
            )
            all_rows.extend(rows)
            warnings.extend(w)
            any_reranked = any_reranked or reranked
        except SystemExit as e:
            msg = str(e) or "index unavailable"
            if fanout:
                failed += 1
                warnings.append(f"vault {b.name}: search_failed ({msg})")
                continue
            return _err(error="search_failed", message=msg)
        except Exception as e:
            if fanout:
                failed += 1
                warnings.append(f"vault {b.name}: search_failed ({e})")
                continue
            return _err(error="search_failed", message=str(e))

    if fanout and failed == len(targets):
        return _err(
            error="search_failed",
            message="all vaults failed: " + "; ".join(warnings) if warnings else "all vaults failed",
        )

    if fanout:
        all_rows.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
        all_rows = all_rows[:k]
        out: dict[str, Any] = {
            "ok": True,
            "results": all_rows,
            "vaults": [b.name for b in targets],
        }
        tip_root: Path | None = None
    else:
        out = {"ok": True, "results": all_rows, "vault": targets[0].name}
        tip_root = targets[0].resolved().root
    if default_exclude:
        out["default_exclude"] = default_exclude
    if any_reranked:
        out["reranked"] = True
    if warnings:
        out["warning"] = "; ".join(warnings)
    return _attach_folder_tip(out, folder_clean, root=tip_root)


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
            path=path,
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
    attach_region_hashes(out, text, heading=heading)
    _record_path_touch(
        b.name, path, float(out["mtime"]), text, heading=heading
    )
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
    out: dict[str, Any] = {
        "ok": True,
        "total": total,
        "offset": offset,
        "limit": limit,
        "notes": notes,
        "vault": b.name,
    }
    missing = _missing_folder_warning(root, folder_clean, b.name)
    if missing:
        out["warning"] = missing
    return out


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
        body = chunk.get("content") or ""
        out_chunk: dict[str, Any] = {
            "ok": True,
            "path": rel,
            "heading": f"{'#' * hlevel} {heading}".strip() if heading else "",
            "start_line": int(chunk.get("start_line") or 1),
            "end_line": int(chunk.get("end_line") or 1),
            "content": body,
            "content_hash": region_content_hash(body) if body else "",
            "scope": "chunk",
            "vault": b.name,
        }
        if full.exists():
            file_text = full.read_text(encoding="utf-8")
            out_chunk["mtime"] = _mtime(full)
            attach_region_hashes(out_chunk, file_text)
            if body:
                out_chunk["content_hash"] = region_content_hash(body)
            _record_path_touch(
                b.name,
                rel,
                float(out_chunk["mtime"]),
                file_text,
                heading=out_chunk["heading"] or None,
            )
        return out_chunk

    if not full.exists():
        return _err(error="stale_index", message=f"source file missing: {rel}")

    file_text = full.read_text(encoding="utf-8")
    lines = normalize_lines(file_text)
    section = section_from_chunk(
        lines,
        int(chunk.get("start_line", 1)),
        int(chunk.get("heading_level", 0)),
    )
    start = section.heading_line if section.title else section.body_start
    file_mtime = _mtime(full)
    heading_label = f"{'#' * section.level} {section.title}" if section.title else ""
    out_sec: dict[str, Any] = {
        "ok": True,
        "path": rel,
        "heading": heading_label,
        "start_line": start + 1,
        "end_line": section.body_end,
        "content": "\n".join(lines[start : section.body_end]),
        "scope": "section",
        "mtime": file_mtime,
        "vault": b.name,
    }
    attach_region_hashes(out_sec, file_text, section=section)
    _record_path_touch(
        b.name, rel, float(file_mtime), file_text, heading=heading_label or None
    )
    return out_sec


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


_HISTORY_TZ = ZoneInfo("America/New_York")


def _parse_history_bound(
    raw: str | None,
    *,
    end_of_day: bool,
) -> tuple[float | None, str | None]:
    """Parse ``since`` / ``until`` to unix epoch. Date-only uses America/New_York."""
    s = (raw or "").strip()
    if not s:
        return None, None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            d = datetime.strptime(s, "%Y-%m-%d").date()
            if end_of_day:
                dt = datetime.combine(d, dt_time(23, 59, 59, 999999), tzinfo=_HISTORY_TZ)
            else:
                dt = datetime.combine(d, dt_time(0, 0, 0), tzinfo=_HISTORY_TZ)
            return dt.timestamp(), None
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_HISTORY_TZ)
        return dt.timestamp(), None
    except ValueError:
        return None, (
            f"invalid {'until' if end_of_day else 'since'}={raw!r}; "
            "use YYYY-MM-DD or ISO datetime"
        )


def history(
    *,
    limit: int = 10,
    folder: str = "",
    path: str = "",
    vault: str = "",
    since: str = "",
    until: str = "",
    preview: str = "first",
    heading: str = "",
    exclude: list[str] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Browse by mtime, or file-level git history when ``path`` is set.

    - No ``path``: index-backed recent notes by mtime (optional since/until,
      preview=first|last, heading=, exclude=, fields=).
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
        preview_text = ""
        chunk_hash = ""
        chunk_heading = ""
        index_path = ensure_indexed_path(rel_path)
        with vaults.bind(b):
            try:
                db = core.reader_connect()
                db_row = db.execute(
                    "SELECT f.mtime, COALESCE(substr(c.text, 1, 120), ''), "
                    "COALESCE(c.chunk_hash, ''), COALESCE(c.heading, '') "
                    "FROM files f LEFT JOIN chunks c ON c.path = f.path AND c.ord = 0 "
                    "WHERE f.path = ?",
                    (index_path,),
                ).fetchone()
                if db_row is None and index_path != rel_path:
                    db_row = db.execute(
                        "SELECT f.mtime, COALESCE(substr(c.text, 1, 120), ''), "
                        "COALESCE(c.chunk_hash, ''), COALESCE(c.heading, '') "
                        "FROM files f LEFT JOIN chunks c ON c.path = f.path AND c.ord = 0 "
                        "WHERE f.path = ?",
                        (rel_path,),
                    ).fetchone()
                if db_row:
                    modified = datetime.fromtimestamp(db_row[0]).isoformat(timespec="seconds")
                    preview_text = (db_row[1] or "").replace("\n", " ").strip()
                    chunk_hash = db_row[2] or ""
                    chunk_heading = db_row[3] or ""
            except Exception:
                pass
        out_mtime: dict[str, Any] = {
            "ok": True,
            "path": rel_path,
            "source": "mtime",
            "modified": modified,
            "first_line": preview_text,
            "vault": b.name,
        }
        if chunk_hash:
            out_mtime["chunk_hash"] = chunk_hash
        if chunk_heading:
            out_mtime["heading"] = chunk_heading
        return out_mtime

    # Browse mode
    since_ts, since_err = _parse_history_bound(since, end_of_day=False)
    if since_err:
        return _err(error="bad_request", message=since_err)
    until_ts, until_err = _parse_history_bound(until, end_of_day=True)
    if until_err:
        return _err(error="bad_request", message=until_err)
    if since_ts is not None and until_ts is not None and since_ts > until_ts:
        return _err(error="bad_request", message="since must be <= until")

    mode = (preview or "first").strip().lower() or "first"
    if mode not in ("first", "last"):
        return _err(error="bad_request", message="preview must be 'first' or 'last'")
    if fields is not None:
        if not isinstance(fields, list) or any(not isinstance(x, str) for x in fields):
            return _err(error="bad_request", message="fields must be a list of strings")
    if exclude is not None:
        if not isinstance(exclude, list) or any(not isinstance(x, str) for x in exclude):
            return _err(error="bad_request", message="exclude must be a list of strings")

    try:
        base = _safe_resolve(root, folder) if folder else root
    except ValueError as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder}")
    heading_clean = (heading or "").strip()
    try:
        with vaults.bind(b):
            rows = core.recent_notes_preview(
                limit,
                folder,
                since=since_ts,
                until=until_ts,
                preview=mode,
                heading=heading_clean or None,
                exclude=exclude,
            )
    except ValueError as e:
        return _err(error="bad_request", message=str(e))
    notes: list[dict[str, Any]] = []
    for row in rows:
        note: dict[str, Any] = {
            "path": row["path"],
            "modified": datetime.fromtimestamp(row["mtime"]).isoformat(timespec="seconds"),
            "first_line": row.get("first_line") or "",
            "chunk_hash": row.get("chunk_hash") or "",
        }
        ch_heading = row.get("heading") or ""
        if ch_heading:
            note["heading"] = ch_heading
        if fields is not None:
            note["frontmatter"] = project_frontmatter(row.get("frontmatter"), fields)
        notes.append(note)
    out: dict[str, Any] = {"ok": True, "notes": notes, "vault": b.name}
    if since_ts is not None:
        out["since"] = datetime.fromtimestamp(since_ts, tz=_HISTORY_TZ).isoformat(timespec="seconds")
    if until_ts is not None:
        out["until"] = datetime.fromtimestamp(until_ts, tz=_HISTORY_TZ).isoformat(timespec="seconds")
    if mode != "first":
        out["preview"] = mode
    if heading_clean:
        out["heading"] = heading_clean
    if exclude:
        out["exclude"] = list(exclude)
    return out


###############################################################################
# Writes — enqueue index; watcher is sole index.db writer
###############################################################################


def write_note(
    path: str,
    content: str | None = None,
    *,
    text: str | None = None,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    vault: str = "",
) -> dict[str, Any]:
    body, used_alias, body_err = resolve_body_text(text, content, prefer="content")
    if body_err:
        return _err(path=path, error="bad_request", message=body_err)
    assert body is not None
    content = body

    try:
        b = _binding(vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    prior_text = full.read_text(encoding="utf-8") if full.exists() else None
    if (
        guard := _guard_write(
            full,
            path,
            vault=b.name,
            regions=WriteRegions(whole_file=True),
            expected_mtime=expected_mtime,
            expected_frontmatter_hash=expected_frontmatter_hash,
            expected_body_hash=expected_body_hash,
            expected_content_hash=expected_content_hash,
            content=prior_text,
        )
    ):
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
    attach_region_hashes(out, to_write)
    if new_top:
        out["warning"] = (
            f"created new top-level directory {parts[0]!r} — "
            f"existing top-level dirs: {_top_level_dirs(root)}"
        )
    out = _finalize_write(
        out,
        vault=b.name,
        path=path,
        expected_mtime=expected_mtime,
        content=to_write,
    )
    if used_alias:
        out = _attach_tip(
            out, "write_note: used text= alias; prefer content="
        )
    return out


def append_note(
    path: str = "",
    text: str | None = None,
    *,
    content: str | None = None,
    heading: str | None = None,
    chunk_hash: str | None = None,
    position: Literal["end", "start"] = "end",
    create: bool = False,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    vault: str = "",
) -> dict[str, Any]:
    body, used_alias, body_err = resolve_body_text(text, content, prefer="text")
    if body_err:
        return _err(
            path=(path or "").replace("\\", "/").strip() or None,
            error="bad_request",
            message=body_err,
        )
    assert body is not None
    text = body

    path = (path or "").replace("\\", "/").strip()
    ch = (chunk_hash or "").strip() or None
    heading = (heading or "").strip() or None
    tip: str | None = None
    from_hash = False
    hash_start: int | None = None
    hash_level: int | None = None
    resolved_heading: str | None = heading

    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(path=path or None, error=e.code, message=e.message)

    if ch:
        resolved = resolve_chunk_anchor(
            b, ch, path=path or None, heading_fallback=heading
        )
        if not resolved.get("ok"):
            return _err(**{k: v for k, v in resolved.items() if k != "ok"})
        path = str(resolved["path"])
        tip = resolved.get("tip")
        from_hash = bool(resolved.get("from_hash"))
        if resolved.get("start_line") is not None:
            hash_start = int(resolved["start_line"])
        if resolved.get("heading_level") is not None:
            hash_level = int(resolved["heading_level"])
        resolved_heading = (resolved.get("heading") or heading or "").strip() or None
    elif not path:
        return _err(
            error="bad_request",
            message="append_note requires path= or chunk_hash=",
        )

    try:
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    if is_yaml_note(path):
        return _err(
            path=path,
            error="unsupported_format",
            message=(
                "append_note is Markdown-only; YAML catalog notes use "
                "write_note / patch_note(set_field|delete_field)"
            ),
        )

    regions = classify_append_regions(
        heading=resolved_heading, from_chunk=from_hash
    )
    chunk_section = None
    prior_text: str | None = None
    if full.exists():
        prior_text = full.read_text(encoding="utf-8")
        if from_hash and hash_start is not None:
            chunk_section = section_from_chunk(
                normalize_lines(prior_text), hash_start, int(hash_level or 0)
            )
        if (
            guard := _guard_write(
                full,
                path,
                vault=b.name,
                regions=regions,
                expected_mtime=expected_mtime,
                expected_frontmatter_hash=expected_frontmatter_hash,
                expected_body_hash=expected_body_hash,
                expected_content_hash=expected_content_hash,
                content=prior_text,
                chunk_section=chunk_section,
            )
        ):
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
        prior_text = full.read_text(encoding="utf-8")

    content = prior_text if prior_text is not None else full.read_text(encoding="utf-8")
    lines = normalize_lines(content)

    try:
        anchor_label = "EOF"
        if from_hash and hash_start is not None:
            section = chunk_section or section_from_chunk(
                lines, hash_start, int(hash_level or 0)
            )
            anchor_label = section.title or ch or "section"
            merged, detail = apply_append(lines, text, section=section, position=position)
        elif resolved_heading:
            merged, detail = apply_append(
                lines, text, heading=resolved_heading, position=position
            )
            anchor_label = resolved_heading
        else:
            merged, detail = apply_append(lines, text, heading=None, position="end")
    except PatchError as e:
        return _err(path=path, error=e.code, message=e.message, suggestions=e.suggestions)

    new_content = "\n".join(merged)
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"
    full.write_text(new_content, encoding="utf-8")
    _enqueue_index(b, full)

    out: dict[str, Any] = {
        "ok": True,
        "path": path,
        "anchor": anchor_label,
        "detail": detail,
        "lines_added": max(0, len(merged) - len(lines)),
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
        "vault": b.name,
    }
    attach_region_hashes(
        out,
        new_content,
        heading=resolved_heading if not from_hash else None,
        section=chunk_section if from_hash else None,
    )
    out = _finalize_write(
        out,
        vault=b.name,
        path=path,
        expected_mtime=expected_mtime,
        content=new_content,
    )
    if tip:
        out = _attach_tip(out, tip)
    if used_alias:
        out = _attach_tip(
            out, "append_note: used content= alias; prefer text="
        )
    return out


def patch_note(
    path: str,
    ops: list[Any],
    *,
    strict: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
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

    content = full.read_text(encoding="utf-8")
    try:
        dict_ops = ops_to_dicts(ops)
    except (TypeError, ValueError) as e:
        return _err(path=path, error="bad_request", message=str(e))

    materialized = materialize_ops_chunk_hashes(b, path, dict_ops)
    if not materialized.get("ok"):
        return _err(**{k: v for k, v in materialized.items() if k != "ok"})
    dict_ops = materialized["ops"]
    hash_tips: list[str] = list(materialized.get("tips") or [])

    regions = classify_patch_regions(dict_ops, yaml_note=is_yaml_note(path))
    if (
        guard := _guard_write(
            full,
            path,
            vault=b.name,
            regions=regions,
            expected_mtime=expected_mtime,
            expected_frontmatter_hash=expected_frontmatter_hash,
            expected_body_hash=expected_body_hash,
            expected_content_hash=expected_content_hash,
            content=content,
        )
    ):
        return guard

    try:
        if is_yaml_note(path):
            result = apply_yaml_patch(content, dict_ops, strict=strict)
        else:
            result = apply_patch(content, dict_ops, strict=strict)
    except (TypeError, PatchError) as e:
        if isinstance(e, PatchError):
            return _err(
                path=path,
                error=e.code,
                message=e.message,
                suggestions=e.suggestions,
            )
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
        for t in hash_tips:
            out_dry = _attach_tip(out_dry, t)
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
        "ok": True,
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
    attach_region_hashes(out, to_write)
    if verbose:
        out["lines_added"] = result.lines_added
    out = _finalize_write(
        out,
        vault=b.name,
        path=path,
        expected_mtime=expected_mtime,
        content=to_write,
    )
    for t in hash_tips:
        out = _attach_tip(out, t)
    return out


_PATCH_NOTES_MAX_ITEMS = 20


def patch_entry(
    *,
    path: str = "",
    ops: list[Any] | None = None,
    items: list[Any] | None = None,
    strict: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    vault: str = "",
) -> dict[str, Any]:
    """Dispatch single-path ``patch_note`` or multi-path ``patch_notes`` (XOR)."""
    raw_items = items
    if raw_items is not None and hasattr(raw_items, "__iter__") and not isinstance(raw_items, (str, dict)):
        # Normalize Pydantic models from MCP
        normalized: list[Any] = []
        for it in raw_items:
            if hasattr(it, "model_dump"):
                normalized.append(it.model_dump(mode="python", exclude_none=True))
            else:
                normalized.append(it)
        raw_items = normalized

    has_items = isinstance(raw_items, list) and len(raw_items) > 0
    path_s = (path or "").strip()
    has_single = bool(path_s) and ops is not None

    if has_items and has_single:
        return _err(
            error="bad_request",
            message="pass either path+ops (single) or items[] (multi-path), not both",
        )
    if has_items:
        if expected_mtime is not None or any(
            x is not None
            for x in (
                expected_frontmatter_hash,
                expected_body_hash,
                expected_content_hash,
            )
        ):
            return _err(
                error="bad_request",
                message=(
                    "expected_mtime / region hashes are per-item when using items[]; "
                    "omit top-level concurrency fields"
                ),
            )
        return patch_notes(
            raw_items,  # type: ignore[arg-type]
            strict=strict,
            dry_run=dry_run,
            verbose=verbose,
            vault=vault,
        )
    if has_single:
        return patch_note(
            path_s,
            ops if isinstance(ops, list) else [],
            strict=strict,
            dry_run=dry_run,
            verbose=verbose,
            expected_mtime=expected_mtime,
            expected_frontmatter_hash=expected_frontmatter_hash,
            expected_body_hash=expected_body_hash,
            expected_content_hash=expected_content_hash,
            vault=vault,
        )
    return _err(
        error="bad_request",
        message="provide path+ops for one note, or items=[{path, ops, expected_mtime?}] for a batch",
    )


def patch_notes(
    items: list[Any],
    *,
    strict: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    vault: str = "",
) -> dict[str, Any]:
    """Same-vault multi-path ``patch_note`` batch (patch ops only).

    Each item: ``{path, ops, expected_mtime?}``. Cap ``_PATCH_NOTES_MAX_ITEMS``.
    Continues on per-item failure; top-level ``ok`` is true only if every item ok.
    Parallel multi-tool writes (different paths / roles) stay as separate MCP calls —
    do not fold them into this batch.
    """
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    if not isinstance(items, list) or not items:
        return _err(
            error="bad_request",
            message="`items` must be a non-empty array of {path, ops, expected_mtime?}",
            vault=b.name,
        )
    if len(items) > _PATCH_NOTES_MAX_ITEMS:
        return _err(
            error="bad_request",
            message=f"`items` exceeds max of {_PATCH_NOTES_MAX_ITEMS}",
            vault=b.name,
        )

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    ok_n = 0
    fail_n = 0

    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            entry = {
                "ok": False,
                "error": "bad_item",
                "message": f"items[{i}] must be an object",
                "index": i,
                "vault": b.name,
            }
            results.append(entry)
            fail_n += 1
            continue
        path = raw.get("path")
        ops_list = raw.get("ops")
        if not isinstance(path, str) or not path.strip():
            entry = {
                "ok": False,
                "error": "bad_request",
                "message": f"items[{i}].path string required",
                "index": i,
                "vault": b.name,
            }
            results.append(entry)
            fail_n += 1
            continue
        path = path.strip()
        if path in seen:
            entry = {
                "ok": False,
                "path": path,
                "error": "duplicate_path",
                "message": f"duplicate path in batch: {path!r}",
                "index": i,
                "vault": b.name,
            }
            results.append(entry)
            fail_n += 1
            continue
        seen.add(path)
        if not isinstance(ops_list, list):
            entry = {
                "ok": False,
                "path": path,
                "error": "bad_request",
                "message": f"items[{i}].ops must be an array",
                "index": i,
                "vault": b.name,
            }
            results.append(entry)
            fail_n += 1
            continue

        em = raw.get("expected_mtime")
        expected: float | None
        if em is None:
            expected = None
        else:
            try:
                expected = float(em)
            except (TypeError, ValueError):
                entry = {
                    "ok": False,
                    "path": path,
                    "error": "bad_request",
                    "message": f"items[{i}].expected_mtime must be a number",
                    "index": i,
                    "vault": b.name,
                }
                results.append(entry)
                fail_n += 1
                continue

        def _opt_hash(key: str) -> str | None:
            val = raw.get(key)
            if val is None:
                return None
            if not isinstance(val, str):
                return None
            s = val.strip()
            return s or None

        item_out = patch_note(
            path,
            ops_list,
            strict=strict,
            dry_run=dry_run,
            verbose=verbose,
            expected_mtime=expected,
            expected_frontmatter_hash=_opt_hash("expected_frontmatter_hash"),
            expected_body_hash=_opt_hash("expected_body_hash"),
            expected_content_hash=_opt_hash("expected_content_hash"),
            vault=b.name,
        )
        item_out = dict(item_out)
        item_out["index"] = i
        results.append(item_out)
        if item_out.get("ok"):
            ok_n += 1
        else:
            fail_n += 1

    all_ok = fail_n == 0
    out: dict[str, Any] = {
        "ok": all_ok,
        "partial": bool(ok_n and fail_n),
        "applied_paths": ok_n,
        "failed_paths": fail_n,
        "results": results,
        "vault": b.name,
    }
    if not all_ok:
        out["error"] = "batch_partial" if ok_n else "batch_failed"
        out["message"] = (
            f"{fail_n} of {len(items)} path(s) failed"
            if fail_n
            else "batch failed"
        )
    return out


def place_note(
    src: str,
    dst: str,
    *,
    overwrite: bool = False,
    fields: dict[str, Any] | None = None,
    expected_mtime: float | None = None,
    vault: str = "",
) -> dict[str, Any]:
    """Place a note at ``dst``: move if ``src`` is in the vault, else copy from host.

    - Vault-relative ``src``, or absolute path under the vault root → ``moved``
      (same as former ``move_note``; ``fields`` not allowed).
    - Absolute host ``.md`` outside the vault (allow-roots) → ``copied`` / promote
      (same as former ``send_note``; leaves src; optional ``fields`` merge).
    """
    try:
        b = _binding(vault)
        root = b.resolved().root.resolve()
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)

    raw = (src or "").strip()
    if not raw:
        return _err(src=src, dst=dst, error="bad_path", message="src required")

    expanded = Path(raw).expanduser()
    in_vault_rel: str | None = None

    if expanded.is_absolute():
        try:
            full = expanded.resolve(strict=True)
        except FileNotFoundError:
            return _err(src=src, dst=dst, error="not_found", message=f"source not found: {src}")
        except OSError as e:
            return _err(src=src, dst=dst, error="bad_path", message=f"cannot resolve src: {e}")
        try:
            in_vault_rel = full.relative_to(root).as_posix()
        except ValueError:
            in_vault_rel = None
        if in_vault_rel is not None:
            if fields:
                return _err(
                    src=src,
                    dst=dst,
                    error="bad_request",
                    message="fields= is only for host→vault copy; omit when moving inside the vault",
                )
            out = move_note(
                in_vault_rel,
                dst,
                overwrite=overwrite,
                expected_mtime=expected_mtime,
                vault=b.name,
            )
            if out.get("ok"):
                out = dict(out)
                out["action"] = "moved"
                out["mode"] = "move"
            return out
        # Host copy path
        out = send_note(
            str(full),
            dst,
            overwrite=overwrite,
            fields=fields,
            expected_mtime=expected_mtime,
            vault=b.name,
        )
        if out.get("ok"):
            out = dict(out)
            # send_note already sets action created|overwrote
            out["mode"] = "copy"
        return out

    # Vault-relative move
    if fields:
        return _err(
            src=src,
            dst=dst,
            error="bad_request",
            message="fields= is only for host→vault copy; omit when moving inside the vault",
        )
    out = move_note(
        raw.replace("\\", "/"),
        dst,
        overwrite=overwrite,
        expected_mtime=expected_mtime,
        vault=b.name,
    )
    if out.get("ok"):
        out = dict(out)
        out["action"] = "moved"
        out["mode"] = "move"
    return out


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


def git_sync_op(
    action: str = "status",
    *,
    message: str = "",
    vault: str = "",
) -> dict[str, Any]:
    """Git contract sync: status | run | pull | clear_block.

    ``run`` commits all dirty paths except ``never_commit`` / gitignore, then pushes.
    Auto path uses path-aware template message; pass ``message`` to override subject
    (tool-triggered). A Paths body trailer is always attached.
    """
    try:
        b = _binding(vault)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    act = (action or "status").strip().lower()
    if act not in ("status", "run", "pull", "clear_block"):
        return _err(
            error="bad_action",
            message="action must be status|run|pull|clear_block",
            vault=b.name,
        )
    out = git_sync.run_action(
        root,
        act,  # type: ignore[arg-type]
        message=(message or "").strip() or None,
    )
    out.setdefault("vault", b.name)
    return out


def _vault_row(b: vaults.VaultBinding, *, default_name: str) -> dict[str, Any]:
    root = b.resolved().root
    return {
        "root": str(root),
        "collection": b.collection,
        "ingest_dir": config.INGEST_DIR,
        "default": b.name == default_name,
        "top_level_dirs": _top_level_dirs(root),
        "contracts": vault_contracts.contracts_summary(root),
    }


def vault_op(
    action: str = "list",
    *,
    vault: str = "",
    host: str = "both",
    write: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """Vault management: list | contracts | describe | merge | project.

    Read-only except ``project`` with ``write=True`` (writes host skill/rule files).
    Live IR preferred under ``system/contracts/``; legacy
    ``system/config/*-contract.schema.yaml`` still discovered.
    ``merge`` / ``contracts`` / ``describe`` default to contract summaries
    (no YAML bodies); pass ``full=True`` for parsed ``data``.
    ``merge`` unions the registry with per-vault contracts and ``~/.apo/desk.yaml``.
    ``project`` renders desk Cursor/Claude artifacts from merge IR.
    """
    act = (action or "list").strip().lower()
    if act not in ("list", "contracts", "describe", "merge", "project"):
        return _err(
            error="bad_action",
            message="action must be list|contracts|describe|merge|project",
        )

    try:
        default_name, bindings = vaults.load_bindings()
    except Exception as e:
        return _err(error="bad_vault", message=str(e))

    want_full = bool(full)

    if act == "list":
        return {
            "ok": True,
            "action": "list",
            "default_vault": default_name,
            "vaults": {
                name: _vault_row(b, default_name=default_name)
                for name, b in sorted(bindings.items())
            },
        }

    if act == "contracts":
        key = (vault or "").strip()
        if not key:
            out_vaults: dict[str, Any] = {}
            for name, b in sorted(bindings.items()):
                root = b.resolved().root
                found = vault_contracts.discover_contracts(root)
                out_vaults[name] = {
                    "root": str(root),
                    "contracts": vault_contracts.present_contracts(
                        found, full=want_full
                    ),
                }
            return {
                "ok": True,
                "action": "contracts",
                "full": want_full,
                "default_vault": default_name,
                "vaults": out_vaults,
            }
        try:
            b = _binding(key)
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        root = b.resolved().root
        found = vault_contracts.discover_contracts(root)
        return {
            "ok": True,
            "action": "contracts",
            "full": want_full,
            "vault": b.name,
            "root": str(root),
            "contracts": vault_contracts.present_contracts(found, full=want_full),
        }

    def _merge_payload(*, bodies: bool) -> dict[str, Any]:
        desk = vault_desk.load_desk()
        roles = desk.get("vault_roles") if isinstance(desk.get("vault_roles"), dict) else {}
        merged_vaults: dict[str, Any] = {}
        for name, b in sorted(bindings.items()):
            root = b.resolved().root
            found = vault_contracts.discover_contracts(root)
            row: dict[str, Any] = {
                "root": str(root),
                "collection": b.collection,
                "ingest_dir": config.INGEST_DIR,
                "default": name == default_name,
                "top_level_dirs": _top_level_dirs(root),
                "contract_ids": vault_contracts.contract_ids(root),
                "contracts": vault_contracts.present_contracts(found, full=bodies),
            }
            if name in roles:
                row["role"] = roles[name]
            merged_vaults[name] = row
        desk_meta = {
            "source": desk.get("_source"),
            "path": desk.get("_path"),
        }
        if desk.get("_error"):
            desk_meta["error"] = desk["_error"]
        return {
            "ok": True,
            "action": "merge",
            "full": bodies,
            "default_vault": default_name,
            "desk": vault_desk.public_desk(desk),
            "desk_meta": desk_meta,
            "merge_rules": {
                "cross_pollinate_contracts": bool(
                    desk.get("cross_pollinate_contracts", False)
                ),
                "desk_overlay_keys": [
                    "dual_write",
                    "citations",
                    "vault_roles",
                    "habits",
                    "pointers",
                    "role_notes",
                    "workspace",
                ],
            },
            "vaults": merged_vaults,
        }

    if act == "merge":
        return _merge_payload(bodies=want_full)

    if act == "project":
        # Summaries for inventory; attach usage-contract bodies for contribution.
        merge = _merge_payload(bodies=False)
        if not merge.get("ok"):
            return merge
        vault_project.attach_usage_contribution_bodies(merge)
        projected = vault_project.project(
            merge, host=host or "both", write=bool(write)
        )
        if not projected.get("ok"):
            return projected
        projected["default_vault"] = merge.get("default_vault")
        projected["desk_meta"] = merge.get("desk_meta")
        return projected

    # describe — single vault (default when vault empty)
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    root = b.resolved().root
    found = vault_contracts.discover_contracts(root)
    return {
        "ok": True,
        "action": "describe",
        "full": want_full,
        "vault": b.name,
        "root": str(root),
        "collection": b.collection,
        "ingest_dir": config.INGEST_DIR,
        "default": b.name == default_name,
        "top_level_dirs": _top_level_dirs(root),
        "contract_ids": list(found),
        "contracts": vault_contracts.present_contracts(found, full=want_full),
    }
