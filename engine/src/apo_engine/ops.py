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
    archival_contract,
    config,
    core,
    deferred as index_deferred,
    git_catalog,
    git_contract,
    git_sync,
    note_lint,
    search_contract,
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
from apo_engine.path_ref import (
    PathRefError,
    merge_vault_arg,
    peel_path_ref,
    qualified_path as _qualified_path,
)
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
    """Resolve vault by name — must be in this process registry (write/read gate)."""
    default, bindings = vaults.load_bindings()
    key = (vault or "").strip() or default
    if key not in bindings:
        raise OpsError("bad_vault", f"unknown vault {key!r}; available: {sorted(bindings)}")
    return bindings[key]


def _load_bindings():
    """Thin indirection so callers can name a local param ``vaults`` without
    shadowing the ``apo_engine.vaults`` module import."""
    return vaults.load_bindings()


def _resolve_binding_and_rel(path: str, vault: str = "") -> tuple[vaults.VaultBinding, str]:
    """Peel optional ``vault_id:rel`` prefix; gate vault_id to process registry.

    Mutating and path-bearing ops use this so writes cannot target vaults outside
    the MCP/RPC process bindings.
    """
    default, bindings = vaults.load_bindings()
    known = set(bindings)
    try:
        pref, rel = peel_path_ref(path, known=known)
        key = merge_vault_arg(pref, vault, default=default)
    except PathRefError as e:
        raise OpsError(e.code, e.message) from e
    if key not in bindings:
        raise OpsError("bad_vault", f"unknown vault {key!r}; available: {sorted(bindings)}")
    return bindings[key], rel


def _stamp_qualified(out: dict[str, Any], *, vault: str, path: str) -> dict[str, Any]:
    """Add ``qualified_path`` for agent copy-paste (non-breaking additive field)."""
    if out.get("ok") and vault and path:
        out["qualified_path"] = _qualified_path(vault, path)
    return out


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


def _reject_if_ref(ref: str, *, tool: str = "write") -> dict[str, Any] | None:
    """Hard-reject ``ref=`` on mutators (git catalog is read-only)."""
    r = (ref or "").strip()
    if not r:
        return None
    return _err(
        error="bad_request",
        message=(
            f"{tool} does not support ref= — git catalog views are read-only; "
            "mutate the working tree (or a jj workspace) then filter/read with ref= after export"
        ),
    )


def _build_toc_from_blob(text: str, file_bytes: int) -> dict[str, Any]:
    """Lean outline from blob ATX headings — no index / no chunk_hash."""
    from apo_engine.markdown_sections import find_headings, hierarchical_end

    lines = text.split("\n")
    headings = find_headings(lines)
    toc: list[dict[str, Any]] = []
    n = len(lines)
    for i, h in enumerate(headings):
        end = hierarchical_end(headings, i, n)
        section_text = "\n".join(lines[h.line : end])
        toc.append(
            {
                "level": h.level,
                "title": h.title,
                "chunk_hash": None,
                "chunk_kind": "section",
                "section_bytes": len(section_text.encode("utf-8")),
            }
        )
    return {"toc": toc, "file_bytes": file_bytes}


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




def _attach_search_size_tips(out: dict[str, Any]) -> dict[str, Any]:
    """Soft hints when search hits include large files/sections or fat preambles."""
    if not out.get("ok"):
        return out
    results = out.get("results") or []
    if not results:
        return out
    tips: list[str] = []
    if any(int(r.get("file_bytes") or 0) > config.FILE_TIP_BYTES for r in results):
        tips.append(
            "large note(s) in results — use read_note(chunk_hash=), not full read_note"
        )
    if any(int(r.get("section_bytes") or 0) > config.SECTION_PREVIEW_BYTES for r in results):
        tips.append(
            "large section(s) in results — check section_bytes before expand; add subheadings if recurring"
        )
    if any(
        int(r.get("heading_level") or 0) == 0
        and int(r.get("section_bytes") or 0) > config.PREAMBLE_WARN_BYTES
        for r in results
    ):
        tips.append("large preamble — consider promoting content under a # heading")
    for tip in tips:
        out = _attach_tip(out, tip)
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


def _attach_flaws(out: dict[str, Any], flaws: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge corpus findings onto a successful response. Does not touch tip/warning."""
    if not out.get("ok") or not flaws:
        return out
    existing = out.get("flaws")
    if isinstance(existing, list) and existing:
        out["flaws"] = list(existing) + list(flaws)
    else:
        out["flaws"] = list(flaws)
    return out


def _prepare_write_content(
    content: str,
    *,
    path: str,
    vault: str,
    okf_result: Any | None = None,
    format_only: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Auto-fix trailing WS before disk write; collect OKF + format flaws.

    ``format_only`` skips OKF flaw mapping (append_note path).
    """
    flaws: list[dict[str, Any]] = []
    if okf_result is not None and not format_only:
        flaws.extend(
            note_lint.flaws_from_okf(okf_result, path=path, vault=vault)
        )
    text, fmt_flaws = note_lint.apply_auto_fixes(
        content, path=path, vault=vault, enabled=True
    )
    flaws.extend(fmt_flaws)
    return text, flaws


def _attach_archival_write_flaws(
    out: dict[str, Any],
    *,
    vault: str,
    path: str,
    content: str | None = None,
) -> dict[str, Any]:
    """Post-write archival suggest check (eligible + blocked_todos only)."""
    if not out.get("ok") or not path:
        return out
    try:
        b = _binding(vault or str(out.get("vault") or ""))
        root = b.resolved().root
    except OpsError:
        return out
    flaws, tip = archival_contract.evaluate_write_path(
        root, path, content=content
    )
    if tip:
        out = _attach_tip(out, tip)
    return _attach_flaws(out, flaws)


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
    out = _attach_archival_write_flaws(
        out, vault=vault, path=path, content=content
    )
    out = _stamp_qualified(out, vault=vault, path=path)
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
    """Surface missing watcher on successful writes."""
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
) -> tuple[list[dict[str, Any]], list[str], bool, list[str] | None]:
    """Run hybrid search in one vault. Returns (rows, warnings, reranked, default_exclude)."""
    warnings: list[str] = []
    root = b.resolved().root
    effective_exclude, applied_default, _source = search_contract.resolve_search_exclude(
        root,
        caller_exclude=exclude,
        folder_clean=folder_clean,
    )
    with vaults.bind(b):
        hits = core.search(
            query,
            k=k,
            folder=folder_clean,
            snippet_chars=snippet_chars,
            exclude=effective_exclude,
            hybrid=hybrid,
        )
        results = shape_search_hits(hits)
        rr = core.last_search_rerank()
        degraded = core.last_search_degraded()
    if stamp_vault:
        for row in results:
            row["vault"] = b.name
    for row in results:
        src = str(row.get("source") or "")
        if src:
            row["qualified_path"] = _qualified_path(b.name, src)
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
    return results, warnings, reranked, applied_default


def _search_folder_pairs(
    folder_list: list[str],
    targets: list[vaults.VaultBinding],
    vault_arg: str,
) -> list[tuple[vaults.VaultBinding, str]]:
    """Expand folders with optional ``vault_id:rel`` prefixes into (binding, folder) pairs."""
    default, bindings = vaults.load_bindings()
    known = set(bindings)
    vault_s = (vault_arg or "").strip()
    pairs: list[tuple[vaults.VaultBinding, str]] = []
    for raw in folder_list:
        try:
            pref, rel = peel_path_ref(raw, known=known)
            if pref:
                key = merge_vault_arg(pref, vault_s, default=pref)
            else:
                # Unprefixed folder: apply to every search target
                for b in targets:
                    pairs.append((b, rel.replace("\\", "/").strip("/")))
                continue
        except PathRefError as e:
            raise OpsError(e.code, e.message) from e
        if key not in bindings:
            raise OpsError("bad_vault", f"unknown vault {key!r}; available: {sorted(bindings)}")
        # Prefixed folder must be allowed for this search (target list or vault=)
        target_names = {b.name for b in targets}
        if key not in target_names and vault_s and vault_s != key:
            raise OpsError(
                "bad_request",
                f"folder prefix vault {key!r} is not in search targets {sorted(target_names)}",
            )
        if key not in target_names:
            # Single prefixed folder without vault=/vaults= covering it — allow if in registry
            pairs.append((bindings[key], rel.replace("\\", "/").strip("/")))
        else:
            pairs.append((bindings[key], rel.replace("\\", "/").strip("/")))
    return pairs


def search(
    query: str,
    *,
    top_k: int | None = None,
    folder: str = "",
    folders: list[str] | None = None,
    vault: str = "",
    vaults: list[str] | None = None,
    snippet_chars: int = 240,
    exclude: list[str] | None = None,
    hybrid: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        targets = _resolve_search_bindings(vault, vaults)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    k, err = resolve_top_k(top_k, limit)
    if err:
        return _err(error="bad_request", message=err)
    if offset < 0:
        return _err(error="bad_request", message="offset must be >= 0")
    # Over-fetch one past the page so has_more is exact without an extra count query.
    fetch_k = k + offset + 1
    if folder.strip() and folders:
        return _err(
            error="bad_request",
            message="pass folder= or folders=[], not both",
        )
    folder_list: list[str] = []
    if folders:
        folder_list = [f for f in folders if isinstance(f, str) and f.strip()]
    elif folder.strip():
        folder_list = [folder.strip()]
    if not folder_list:
        folder_list = [""]
    try:
        pairs = _search_folder_pairs(folder_list, targets, vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    for b, folder_clean in pairs:
        if folder_clean:
            try:
                _safe_resolve(b.resolved().root, folder_clean)
            except ValueError as e:
                return _err(error="bad_path", message=str(e))
    fanout_vaults = len({b.name for b, _ in pairs}) > 1
    fanout_folders = len({f for _, f in pairs}) > 1
    all_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    any_reranked = False
    default_exclude: list[str] | None = None
    default_exclude_by_vault: dict[str, list[str]] = {}
    failed = 0
    total_attempts = len(pairs)
    for b, folder_clean in pairs:
        try:
            rows, w, reranked, applied_default = _search_one_vault(
                query,
                b,
                k=fetch_k,
                folder_clean=folder_clean,
                snippet_chars=snippet_chars,
                exclude=exclude,
                hybrid=hybrid,
                stamp_vault=fanout_vaults or fanout_folders,
            )
            if applied_default:
                if fanout_vaults:
                    default_exclude_by_vault[b.name] = applied_default
                else:
                    default_exclude = applied_default
            all_rows.extend(rows)
            warnings.extend(w)
            any_reranked = any_reranked or reranked
        except SystemExit as e:
            msg = str(e) or "index unavailable"
            if total_attempts > 1:
                failed += 1
                prefix = f"vault {b.name}" if fanout_vaults else ""
                if folder_clean:
                    prefix = f"{prefix} folder {folder_clean}".strip()
                warnings.append(f"{prefix}: search_failed ({msg})".strip())
                continue
            return _err(error="search_failed", message=msg)
        except Exception as e:
            if total_attempts > 1:
                failed += 1
                prefix = f"vault {b.name}" if fanout_vaults else ""
                if folder_clean:
                    prefix = f"{prefix} folder {folder_clean}".strip()
                warnings.append(f"{prefix}: search_failed ({e})".strip())
                continue
            return _err(error="search_failed", message=str(e))

    if total_attempts > 1 and failed == total_attempts:
        return _err(
            error="search_failed",
            message="all searches failed: " + "; ".join(warnings) if warnings else "all searches failed",
        )

    if total_attempts > 1:
        all_rows.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)

    # Page the fused/ranked rows: offset window + exact has_more from the over-fetch.
    has_more = len(all_rows) > offset + k
    all_rows = all_rows[offset : offset + k]

    if fanout_vaults:
        out: dict[str, Any] = {
            "ok": True,
            "results": all_rows,
            "vaults": [b.name for b in targets],
        }
        tip_root: Path | None = None
    else:
        out = {"ok": True, "results": all_rows, "vault": targets[0].name}
        tip_root = targets[0].resolved().root
    if fanout_folders:
        out["folders"] = [f for f in folder_list if f]
    folder_tip = folder_list[0] if len(folder_list) == 1 else ""
    if default_exclude:
        out["default_exclude"] = default_exclude
    if default_exclude_by_vault:
        out["default_exclude_by_vault"] = default_exclude_by_vault
    if any_reranked:
        out["reranked"] = True
    out["has_more"] = has_more
    if offset:
        out["offset"] = offset
    if warnings:
        out["warning"] = "; ".join(warnings)
    return _attach_search_size_tips(_attach_folder_tip(out, folder_tip, root=tip_root))


def _apply_read_frontmatter(
    out: dict[str, Any],
    *,
    raw_fm: dict[str, Any] | None,
    fields: list[str] | None,
    path_mode: bool,
) -> None:
    """Attach frontmatter per path/chunk read policy."""
    if fields is not None:
        if fields:
            out["frontmatter"] = project_frontmatter(raw_fm, fields)
        return
    if path_mode:
        out["frontmatter"] = raw_fm


def _short_title(heading: str) -> str:
    """Last breadcrumb segment — the section's own title, not the full trail."""
    if not heading:
        return ""
    return heading.split(" › ")[-1].strip()


def _chunk_nav(order: list[dict], idx: int) -> dict[str, Any]:
    """Lean prev/next cursors + position for a chunk at ``idx`` in ord order."""
    def _cursor(i: int) -> dict[str, Any] | None:
        if i < 0 or i >= len(order):
            return None
        c = order[i]
        cur: dict[str, Any] = {
            "title": _short_title(c["heading"]) or (c.get("row_key") or ""),
            "chunk_hash": c["chunk_hash"],
            "chunk_kind": c["chunk_kind"],
        }
        return cur

    nav: dict[str, Any] = {"position": {"index": idx, "total": len(order)}}
    prev = _cursor(idx - 1)
    nxt = _cursor(idx + 1)
    if prev:
        nav["prev"] = prev
    if nxt:
        nav["next"] = nxt
    return nav


def _resolve_sibling(order: list[dict], idx: int, direction: str) -> str | None:
    """Same-depth neighbor chunk_hash: same kind + heading_level, prev or next."""
    if idx < 0 or idx >= len(order):
        return None
    cur = order[idx]
    step = -1 if direction == "prev" else 1
    j = idx + step
    while 0 <= j < len(order):
        c = order[j]
        if c["chunk_kind"] == cur["chunk_kind"] and c["heading_level"] == cur["heading_level"]:
            return c["chunk_hash"]
        j += step
    return None


def _table_from_file(file_text: str, rel: str, table_id: str | None):
    """Return the parsed table whose id matches ``table_id`` (or None)."""
    from . import table_markdown as tm

    body, body_line = core._body_start_line(file_text)
    for t in tm.find_tables(body.split("\n")):
        abs_start = body_line + t.start_line
        if tm.table_id_for(rel, abs_start, t.table_index) == table_id:
            return t
    return None


def _build_toc(rel: str, file_bytes: int) -> dict[str, Any]:
    """Lean outline from the index (section + table_header chunks) — no file read."""
    toc: list[dict[str, Any]] = []
    for c in core.note_chunk_order(rel):
        if c["chunk_kind"] not in ("section", "table_header"):
            continue
        toc.append(
            {
                "level": c["heading_level"],
                "title": _short_title(c["heading"]) or c["heading"],
                "chunk_hash": c["chunk_hash"],
                "chunk_kind": c["chunk_kind"],
                "section_bytes": c["section_bytes"],
            }
        )
    return {"toc": toc, "file_bytes": file_bytes}


def _read_from_chunk(
    chunk_hash: str,
    *,
    vault: str = "",
    force: bool = False,
    path_guard: str | None = None,
    fields: list[str] | None = None,
    format: str = "markdown",
    sibling: str | None = None,
    siblings: bool = False,
) -> dict[str, Any]:
    """Return an indexed markdown section/row by search hit chunk_hash."""
    try:
        b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    if fields is not None and not isinstance(fields, list):
        return _err(error="bad_request", message="fields must be a list of strings")

    if sibling is not None and sibling not in ("prev", "next"):
        return _err(error="bad_request", message="sibling must be 'prev' or 'next'")
    if format not in ("markdown", "json", "row"):
        return _err(error="bad_request", message="format must be markdown|json|row")

    with vaults.bind(b):
        chunk = core.lookup_chunk(chunk_hash, include_text=True)
    if not chunk:
        return _err(
            error="anchor_not_found",
            message=f"chunk_hash {chunk_hash!r} not found in index",
        )

    rel = (chunk.get("path") or "").replace("\\", "/")
    if not rel:
        return _err(error="anchor_not_found", message="chunk has no path")
    if path_guard and path_guard.replace("\\", "/").strip("/") != rel:
        return _err(
            error="bad_request",
            message=f"path {path_guard!r} does not match chunk path {rel!r}",
        )

    # Same-depth navigation: hop to the sibling and read *that* chunk instead.
    with vaults.bind(b):
        order = core.note_chunk_order(rel)
    cur_idx = next((i for i, c in enumerate(order) if c["chunk_hash"] == chunk_hash), -1)
    if sibling is not None:
        target = _resolve_sibling(order, cur_idx, sibling) if cur_idx >= 0 else None
        if not target:
            return _err(
                error="no_sibling",
                message=f"no {sibling} sibling at this depth for chunk_hash {chunk_hash!r}",
            )
        return _read_from_chunk(
            target, vault=vault, force=force, path_guard=path_guard,
            fields=fields, format=format, siblings=siblings,
        )

    try:
        full = _safe_resolve(b.resolved().root, rel)
    except ValueError as e:
        return _err(error="anchor_not_found", message=str(e))

    body = chunk.get("content") or ""
    hlevel = int(chunk.get("heading_level") or 0)
    heading_raw = chunk.get("heading") or ""
    heading_label = f"{'#' * hlevel} {heading_raw.split(' › ')[-1]}".strip() if heading_raw else ""
    if hlevel > 0 and heading_raw and not heading_label.startswith("#"):
        title = heading_raw.split(" › ")[-1]
        heading_label = f"{'#' * hlevel} {title}".strip()

    section_bytes = len(body.encode("utf-8"))
    preview_limit = config.SECTION_PREVIEW_BYTES
    preview_chars = config.SECTION_PREVIEW_CHARS
    truncated = section_bytes > preview_limit and not force
    display = body[:preview_chars] if truncated else body

    chunk_kind = chunk.get("chunk_kind") or "section"
    breadcrumb = [c for c in (heading_raw.split(" › ") if heading_raw else []) if c]

    out_sec: dict[str, Any] = {
        "ok": True,
        "path": rel,
        "chunk_hash": chunk_hash,
        "heading": heading_label,
        "content": display,
        "section_bytes": section_bytes,
        "scope": "section",
        "chunk_kind": chunk_kind,
        "vault": b.name,
    }
    if breadcrumb:
        out_sec["breadcrumb"] = breadcrumb
    if chunk_kind == "table_row" and chunk.get("row_key"):
        out_sec["row_key"] = chunk.get("row_key")
    if chunk.get("table_id"):
        out_sec["table_id"] = chunk.get("table_id")
    if cur_idx >= 0:
        out_sec["nav"] = _chunk_nav(order, cur_idx)
        if siblings:
            out_sec["siblings"] = [
                {"title": _short_title(c["heading"]) or c.get("row_key", ""),
                 "chunk_hash": c["chunk_hash"], "chunk_kind": c["chunk_kind"]}
                for c in order
                if c["chunk_kind"] == chunk_kind and c["heading_level"] == int(chunk.get("heading_level") or 0)
            ]
    if truncated:
        out_sec["preview_truncated"] = True
        out_sec["tip"] = (
            f"section is {section_bytes} bytes — add ### subheadings or "
            "read_note(path, heading=, max_chars=) for a sub-range; pass force=true for full body"
        )

    file_text = ""
    if full.exists():
        file_text = full.read_text(encoding="utf-8")
        file_mtime = _mtime(full)
        out_sec["mtime"] = file_mtime
        lines = normalize_lines(file_text)
        section = section_from_chunk(
            lines,
            int(chunk.get("start_line", 1)),
            hlevel,
        )
        attach_region_hashes(out_sec, file_text, section=section)
        # Table chunks are indexed as flattened strings, not ATX section spans —
        # region hashing above would hash the preamble (heading_level=0). Prefer
        # the index content_hash so expected_content_hash from a row read matches
        # search hits and patch preconditions.
        if chunk_kind in ("table_row", "table_header"):
            idx_hash = chunk.get("index_content_hash") or ""
            if idx_hash:
                out_sec["content_hash"] = idx_hash
            elif body:
                out_sec["content_hash"] = region_content_hash(body)
        _record_path_touch(
            b.name, rel, float(file_mtime), file_text, heading=heading_label or None
        )
        if fields is not None:
            raw_fm = core.note_frontmatter(file_text, rel)
            _apply_read_frontmatter(out_sec, raw_fm=raw_fm, fields=fields, path_mode=False)
    else:
        out_sec["content_hash"] = region_content_hash(body) if body else ""

    # Structured (opt-in) — default stays markdown to avoid token bloat.
    if format in ("json", "row"):
        _attach_structured_table(out_sec, chunk, rel, file_text, format)

    return _stamp_qualified(out_sec, vault=b.name, path=rel)


def _attach_structured_table(
    out_sec: dict[str, Any],
    chunk: dict[str, Any],
    rel: str,
    file_text: str,
    format: str,
) -> None:
    """Attach ``format=json|row`` table payloads by re-parsing the on-disk table."""
    from . import table_markdown as tm

    chunk_kind = chunk.get("chunk_kind") or "section"
    if not file_text:
        out_sec["format_warning"] = "file unavailable; structured payload skipped"
        return

    if format == "row":
        if chunk_kind != "table_row":
            out_sec["format_warning"] = "format=row only valid on a table_row chunk_hash"
            return
        table = _table_from_file(file_text, rel, chunk.get("table_id"))
        ri = chunk.get("row_index")
        if table is None or ri is None or ri >= len(table.rows):
            out_sec["format_warning"] = "row not found on disk (re-search after reindex)"
            return
        out_sec["format"] = "row"
        out_sec["columns"] = dict(zip(table.headers, table.rows[ri]))
        out_sec["row_key"] = chunk.get("row_key") or tm.row_key_for(table, ri)
        out_sec["row_hash"] = tm.row_raw_hash(table.rows[ri])
        return

    # format == "json": header/section chunk → whole table.
    table = _table_from_file(file_text, rel, chunk.get("table_id"))
    if table is None and chunk_kind == "section":
        body, body_line = core._body_start_line(file_text)
        found = tm.find_tables(body.split("\n"))
        start = int(chunk.get("start_line", 1))
        end = int(chunk.get("end_line", start))
        for t in found:
            abs_start = body_line + t.start_line
            if start <= abs_start <= end:
                table = t
                break
    if table is None:
        out_sec["format_warning"] = "no table found in this chunk"
        return
    out_sec["format"] = "json"
    out_sec["table"] = {"headers": table.headers, "rows": table.rows}


def read_note(
    path: str = "",
    *,
    chunk_hash: str | None = None,
    heading: str | None = None,
    vault: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
    raw: bool = False,
    force: bool = False,
    fields: list[str] | None = None,
    format: str = "markdown",
    mode: str = "auto",
    sibling: str | None = None,
    siblings: bool = False,
    lint: bool = False,
    ref: str = "",
) -> dict[str, Any]:
    path_s = (path or "").strip()
    ch = (chunk_hash or "").strip()
    ref_s = (ref or "").strip()
    if ref_s and ch:
        return _err(
            error="bad_request",
            message="ref= is path-mode only — chunk_hash= uses the working-tree index",
        )
    if path_s and ch:
        return _err(
            error="bad_request",
            message="pass path= or chunk_hash= (section anchor from search_notes), not both",
        )
    if not path_s and not ch:
        return _err(
            error="bad_request",
            message="path= or chunk_hash= required",
        )
    if ch:
        if heading is not None:
            return _err(
                error="bad_request",
                message="heading= is path-mode only; use chunk_hash= from search_notes",
            )
        if start_line is not None or end_line is not None or max_chars is not None or raw:
            return _err(
                error="bad_request",
                message="line range / raw / max_chars are path-mode only; chunk reads use force=",
            )
        return _read_from_chunk(
            ch,
            vault=vault,
            force=force,
            path_guard=path_s or None,
            fields=fields,
            format=format,
            sibling=sibling,
            siblings=siblings,
        )

    if fields is not None and not isinstance(fields, list):
        return _err(error="bad_request", message="fields must be a list of strings")

    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "toc", "section"):
        return _err(path=path_s, error="bad_request", message="mode must be auto|toc|section")

    if ref_s:
        return _read_note_at_ref(
            path_s,
            ref=ref_s,
            heading=heading,
            vault=vault,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            raw=raw,
            force=force,
            fields=fields,
            mode=mode,
            lint=lint,
        )

    try:
        b, path_s = _resolve_binding_and_rel(path_s, vault)
        root = b.resolved().root
        full = _safe_resolve(root, path_s)
    except OpsError as e:
        return _err(path=path_s, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path_s, error="bad_path", message=str(e))
    if not full.exists():
        return _err(path=path_s, error="not_found", message=f"note not found: {path_s}")

    text = full.read_text(encoding="utf-8")
    file_bytes = full.stat().st_size

    # Explicit ToC: lean outline from the index, no body.
    if mode == "toc" and heading is None:
        with vaults.bind(b):
            toc_out = _build_toc(path_s, file_bytes)
        toc_out.update({"ok": True, "path": path_s, "vault": b.name, "scope": "toc"})
        return _stamp_qualified(toc_out, vault=b.name, path=path_s)
    out: dict[str, Any] = {
        "ok": True,
        "path": path_s,
        "mtime": full.stat().st_mtime,
        "size": full.stat().st_size,
        "vault": b.name,
    }
    try:
        shaped = shape_note_read(
            text,
            path=path_s,
            heading=heading,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            raw_content=raw,
        )
    except PatchError as e:
        return _err(
            path=path_s,
            error=e.code,
            message=e.message,
            suggestions=e.suggestions,
        )
    except ValueError as e:
        return _err(path=path_s, error="bad_request", message=str(e))
    _apply_read_frontmatter(
        out,
        raw_fm=shaped["frontmatter"],
        fields=fields,
        path_mode=True,
    )
    if shaped["heading"]:
        out["heading"] = shaped["heading"]
    out["content"] = shaped["content"]
    out["start_line"] = shaped["start_line"]
    out["end_line"] = shaped["end_line"]
    out["truncated"] = shaped["truncated"]

    # Unified preview gate: a large heading-scoped section truncates like chunk mode
    # unless the caller passed force / max_chars / an explicit line range.
    if (
        heading is not None
        and not raw
        and max_chars is None
        and start_line is None
        and end_line is None
        and not force
    ):
        sec_bytes = len(out["content"].encode("utf-8"))
        if sec_bytes > config.SECTION_PREVIEW_BYTES:
            out["content"] = out["content"][: config.SECTION_PREVIEW_CHARS]
            out["section_bytes"] = sec_bytes
            out["preview_truncated"] = True
            out["tip"] = (
                f"section is {sec_bytes} bytes — pass force=true for the full body, "
                "or max_chars= / start_line=/end_line= for a sub-range"
            )
    elif (
        mode == "auto"
        and heading is None
        and not raw
        and start_line is None
        and end_line is None
        and file_bytes > config.FILE_TIP_BYTES
    ):
        out.setdefault(
            "tip",
            f"note is {file_bytes} bytes — read_note(path, mode=toc) for a lean outline, "
            "then read_note(chunk_hash=) a section",
        )

    attach_region_hashes(out, text, heading=heading)
    _record_path_touch(
        b.name, path_s, float(out["mtime"]), text, heading=heading
    )
    if lint:
        _, lint_flaws = note_lint.lint_note(
            text,
            path=path_s,
            vault_root=root,
            vault=b.name,
            include_links=True,
            include_usage=True,
            include_format=True,
            auto_fix=False,
        )
        out = _attach_flaws(out, lint_flaws)
    return _stamp_qualified(out, vault=b.name, path=path_s)


def _read_note_at_ref(
    path_s: str,
    *,
    ref: str,
    heading: str | None,
    vault: str,
    start_line: int | None,
    end_line: int | None,
    max_chars: int | None,
    raw: bool,
    force: bool,
    fields: list[str] | None,
    mode: str,
    lint: bool,
) -> dict[str, Any]:
    """Path-mode read from a git blob — never touches FS / ``_recent_touches``."""
    try:
        b, path_s = _resolve_binding_and_rel(path_s, vault)
        root = b.resolved().root
        # Path traversal guard without requiring the file on the working tree.
        _safe_resolve(root, path_s)
    except OpsError as e:
        return _err(path=path_s, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path_s, error="bad_path", message=str(e))

    try:
        resolved = git_catalog.resolve_tree(root, ref)
        text = git_catalog.read_blob(root, resolved.commit_oid, path_s)
    except git_catalog.GitCatalogError as e:
        return _err(path=path_s, error=e.code, message=e.message)

    file_bytes = len(text.encode("utf-8"))
    if mode == "toc" and heading is None:
        toc_out = _build_toc_from_blob(text, file_bytes)
        toc_out.update(
            {
                "ok": True,
                "path": path_s,
                "vault": b.name,
                "scope": "toc",
                "source": "git_ref",
                "ref": resolved.ref,
                "tree_oid": resolved.tree_oid,
            }
        )
        return _stamp_qualified(toc_out, vault=b.name, path=path_s)

    out: dict[str, Any] = {
        "ok": True,
        "path": path_s,
        "size": file_bytes,
        "vault": b.name,
        "source": "git_ref",
        "ref": resolved.ref,
        "tree_oid": resolved.tree_oid,
        # Tip commit unix time for display only — not a worktree FS mtime / CAS token.
        "git_tip_mtime": resolved.tip_mtime,
    }
    try:
        shaped = shape_note_read(
            text,
            path=path_s,
            heading=heading,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            raw_content=raw,
        )
    except PatchError as e:
        return _err(
            path=path_s,
            error=e.code,
            message=e.message,
            suggestions=e.suggestions,
        )
    except ValueError as e:
        return _err(path=path_s, error="bad_request", message=str(e))
    _apply_read_frontmatter(
        out,
        raw_fm=shaped["frontmatter"],
        fields=fields,
        path_mode=True,
    )
    if shaped["heading"]:
        out["heading"] = shaped["heading"]
    out["content"] = shaped["content"]
    out["start_line"] = shaped["start_line"]
    out["end_line"] = shaped["end_line"]
    out["truncated"] = shaped["truncated"]

    if (
        heading is not None
        and not raw
        and max_chars is None
        and start_line is None
        and end_line is None
        and not force
    ):
        sec_bytes = len(out["content"].encode("utf-8"))
        if sec_bytes > config.SECTION_PREVIEW_BYTES:
            out["content"] = out["content"][: config.SECTION_PREVIEW_CHARS]
            out["section_bytes"] = sec_bytes
            out["preview_truncated"] = True
            out["tip"] = (
                f"section is {sec_bytes} bytes — pass force=true for the full body, "
                "or max_chars= / start_line=/end_line= for a sub-range"
            )
    elif (
        mode == "auto"
        and heading is None
        and not raw
        and start_line is None
        and end_line is None
        and file_bytes > config.FILE_TIP_BYTES
    ):
        out.setdefault(
            "tip",
            f"note is {file_bytes} bytes — read_note(path, mode=toc, ref=…) for a lean outline",
        )

    # No attach_region_hashes / mtime — blob hashes and tip time are not WT CAS tokens.
    # Deliberately do NOT call _record_path_touch.
    if lint:
        _, lint_flaws = note_lint.lint_note(
            text,
            path=path_s,
            vault_root=root,
            vault=b.name,
            include_links=True,
            include_usage=True,
            include_format=True,
            auto_fix=False,
        )
        out = _attach_flaws(out, lint_flaws)
    return _stamp_qualified(out, vault=b.name, path=path_s)


def expand_section(
    chunk_hash: str,
    *,
    vault: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Deprecated internal/RPC alias — prefer read_note(chunk_hash=)."""
    return _read_from_chunk(chunk_hash, vault=vault, force=force)


def filter_notes(
    where: dict | None = None,
    *,
    folder: str = "",
    limit: int = 20,
    offset: int = 0,
    vault: str = "",
    filters: dict | None = None,
    fields: list[str] | None = None,
    sort: str = "mtime",
    order: str = "desc",
    ref: str = "",
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
    ref_s = (ref or "").strip()
    try:
        if (folder or "").strip():
            b, folder_clean = _resolve_binding_and_rel(folder.strip(), vault)
            folder_clean = folder_clean.replace("\\", "/").strip("/")
        else:
            b = _binding(vault)
            folder_clean = ""
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    if folder_clean and not ref_s:
        try:
            _safe_resolve(root, folder_clean)
        except ValueError as e:
            return _err(error="bad_path", message=str(e))
    elif folder_clean and ref_s:
        try:
            _safe_resolve(root, folder_clean)
        except ValueError as e:
            return _err(error="bad_path", message=str(e))

    if ref_s:
        try:
            with vaults.bind(b):
                resolved = git_catalog.ensure_catalog(root, ref_s)
                total, matches = core.filter_notes_at_ref(
                    where_obj,
                    resolved.tree_oid,
                    folder_clean,
                    limit,
                    offset,
                    sort=sort,
                    order=order,
                )
        except git_catalog.GitCatalogError as e:
            return _err(error=e.code, message=e.message)
        except ValueError as e:
            return _err(error="bad_request", message=str(e))
        except Exception as e:
            # TimeoutExpired and unexpected git failures are mapped above; keep a soft net.
            return _err(error="git_error", message=str(e)[:400])
        notes = [
            {
                "path": path,
                "qualified_path": _qualified_path(b.name, path),
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
            "has_more": offset + len(notes) < total,
            "notes": notes,
            "vault": b.name,
            "source": "git_ref",
            "ref": resolved.ref,
            "tree_oid": resolved.tree_oid,
        }

    try:
        with vaults.bind(b):
            total, matches = core.filter_notes(
                where_obj,
                folder_clean,
                limit,
                offset,
                sort=sort,
                order=order,
            )
    except ValueError as e:
        return _err(error="bad_request", message=str(e))
    notes = [
        {
            "path": path,
            "qualified_path": _qualified_path(b.name, path),
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
        "has_more": offset + len(notes) < total,
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
    """Deprecated alias — prefer read_note(chunk_hash=)."""
    del scope
    return _read_from_chunk(chunk_hash, vault=vault)


def backlinks(path: str, *, limit: int = 100, offset: int = 0, vault: str = "") -> dict[str, Any]:
    try:
        b, path = _resolve_binding_and_rel(path, vault)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))
    if offset < 0:
        return _err(path=path, error="bad_request", message="offset must be >= 0")

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
        # Over-fetch one past the page for exact has_more.
        rows = core.list_backlinks(targets, exclude_source, limit + offset + 1)
    has_more = len(rows) > offset + limit
    rows = rows[offset : offset + limit]
    hits = [
        {
            "path": src,
            "qualified_path": _qualified_path(b.name, src),
            "line": line,
            "text": text,
        }
        for src, line, text in rows
    ]
    out = {
        "ok": True,
        "target": path,
        "total": len(hits),
        "has_more": has_more,
        "backlinks": hits,
        "vault": b.name,
    }
    if offset:
        out["offset"] = offset
    return _stamp_qualified(out, vault=b.name, path=path)


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
    offset: int = 0,
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
    folder_clean = ""
    try:
        if (path or "").strip():
            b, rel_path = _resolve_binding_and_rel(path.strip(), vault)
            rel_path = rel_path.replace("\\", "/")
        elif (folder or "").strip():
            b, folder_clean = _resolve_binding_and_rel(folder.strip(), vault)
            folder_clean = folder_clean.replace("\\", "/").strip("/")
            rel_path = ""
        else:
            b = _binding(vault)
            rel_path = ""
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

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
            return _stamp_qualified(
                {
                    "ok": True,
                    "path": rel_path,
                    "source": "git",
                    "commits": commits,
                    "vault": b.name,
                },
                vault=b.name,
                path=rel_path,
            )

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
        return _stamp_qualified(out_mtime, vault=b.name, path=rel_path)

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

    if not folder_clean:
        folder_clean = folder.replace("\\", "/").strip("/")
    effective_exclude, applied_default, _source = search_contract.resolve_search_exclude(
        root,
        caller_exclude=exclude,
        folder_clean=folder_clean,
    )

    try:
        base = _safe_resolve(root, folder_clean) if folder_clean else root
    except ValueError as e:
        return _err(error="bad_path", message=str(e))
    if not base.exists():
        return _err(error="not_found", message=f"folder not found: {folder_clean or folder}")
    heading_clean = (heading or "").strip()
    if offset < 0:
        return _err(error="bad_request", message="offset must be >= 0")
    try:
        with vaults.bind(b):
            rows = core.recent_notes_preview(
                limit + offset + 1,
                folder_clean,
                since=since_ts,
                until=until_ts,
                preview=mode,
                heading=heading_clean or None,
                exclude=effective_exclude,
            )
    except ValueError as e:
        return _err(error="bad_request", message=str(e))
    has_more = len(rows) > offset + limit
    rows = rows[offset : offset + limit]
    notes: list[dict[str, Any]] = []
    for row in rows:
        note: dict[str, Any] = {
            "path": row["path"],
            "qualified_path": _qualified_path(b.name, row["path"]),
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
    out: dict[str, Any] = {"ok": True, "notes": notes, "has_more": has_more, "vault": b.name}
    if offset:
        out["offset"] = offset
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
    elif applied_default:
        out["default_exclude"] = applied_default
    return out


###############################################################################
# Writes — enqueue index; watcher is sole index.db writer
###############################################################################


def _assemble_structured_note(
    frontmatter: dict[str, Any] | None,
    sections: list[dict[str, Any]] | None,
    body: str | None = None,
) -> str:
    """Build a full markdown note from a frontmatter object + typed sections.

    Section ``content_type`` is ``markdown`` (default), ``csv``, or ``json`` /
    ``table_json`` — CSV/JSON are serialized to a GFM table so they index as
    ``table_row`` chunks like any hand-authored table. Frontmatter is emitted as a
    YAML fence; OKF stamp/validate still runs downstream on the assembled body.
    ``body`` (when sections is None) is appended after the frontmatter fence to
    support dual-write (``frontmatter=`` + ``content=`` / ``text=``).
    """
    import yaml

    from . import table_markdown as tm

    parts: list[str] = []
    if frontmatter:
        fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        parts.append(f"---\n{fm}\n---")
    for s in sections or []:
        ctype = str(s.get("content_type") or "markdown").lower()
        raw = s.get("content", "")
        if ctype == "csv":
            block = tm.csv_to_gfm(raw if isinstance(raw, str) else "")
        elif ctype in ("json", "table_json"):
            block = tm.json_to_gfm(raw)
        else:
            block = raw if isinstance(raw, str) else str(raw)
        heading = s.get("heading")
        if heading:
            level = int(s.get("level", 2) or 2)
            parts.append(f"{'#' * level} {heading}\n\n{block.rstrip()}")
        else:
            parts.append(block.rstrip())
    if body and str(body).strip():
        parts.append(str(body).strip())
    return "\n\n".join(parts).rstrip() + "\n"


def write_note(
    path: str,
    content: str | None = None,
    *,
    text: str | None = None,
    body: str | None = None,
    sections: list[dict[str, Any]] | None = None,
    frontmatter: dict[str, Any] | None = None,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    vault: str = "",
    ref: str = "",
) -> dict[str, Any]:
    bad = _reject_if_ref(ref, tool="write_note")
    if bad:
        return bad
    structured = sections is not None or frontmatter is not None
    if structured:
        if body is not None:
            return _err(
                path=path,
                error="bad_request",
                message="pass (content= OR text=) + frontmatter= OR sections[], but not body= with frontmatter",
            )
        if sections is not None and (content is not None or text is not None):
            return _err(
                path=path,
                error="bad_request",
                message="pass content= OR sections[], not both",
            )
        body_text, alias_key, body_err = resolve_body_text(
            text, content, body=None, prefer="content"
        )
        if body_err:
            # Nothing provided is valid for a frontmatter-/sections-only note.
            if text is None and content is None:
                body_text = ""
                alias_key = None
            else:
                return _err(path=path, error="bad_request", message=body_err)
        try:
            body = _assemble_structured_note(frontmatter, sections, body=body_text)
        except (ValueError, TypeError) as e:
            return _err(path=path, error="bad_request", message=f"invalid section content: {e}")
    else:
        body, alias_key, body_err = resolve_body_text(
            text, content, body=body, prefer="content"
        )
        if body_err:
            return _err(path=path, error="bad_request", message=body_err)
    assert body is not None
    content = body

    try:
        b, path = _resolve_binding_and_rel(path, vault)
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
    to_write, write_flaws = _prepare_write_content(
        okf.content, path=path, vault=b.name, okf_result=okf
    )

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
    out = _attach_flaws(out, write_flaws)
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
    if alias_key:
        out = _attach_tip(
            out, f"write_note: used {alias_key}= alias; prefer content="
        )
    return out


def append_note(
    path: str = "",
    text: str | None = None,
    *,
    content: str | None = None,
    body: str | None = None,
    heading: str | None = None,
    chunk_hash: str | None = None,
    position: Literal["end", "start"] = "end",
    create: bool = False,
    expected_mtime: float | None = None,
    expected_frontmatter_hash: str | None = None,
    expected_body_hash: str | None = None,
    expected_content_hash: str | None = None,
    vault: str = "",
    ref: str = "",
) -> dict[str, Any]:
    bad = _reject_if_ref(ref, tool="append_note")
    if bad:
        return bad
    body, alias_key, body_err = resolve_body_text(
        text, content, body=body, prefer="text"
    )
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
        if path:
            b, path = _resolve_binding_and_rel(path, vault)
        else:
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
    new_content, write_flaws = _prepare_write_content(
        new_content, path=path, vault=b.name, format_only=True
    )
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
    out = _attach_flaws(out, write_flaws)
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
    if alias_key:
        out = _attach_tip(
            out, f"append_note: used {alias_key}= alias; prefer text="
        )
    return out


class _TableOpError(Exception):
    def __init__(self, code: str, message: str, suggestions: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []


def _table_ops_present(dict_ops: list[dict[str, Any]]) -> bool:
    from .patch_ops import TABLE_OPS

    return any(o.get("op") in TABLE_OPS for o in dict_ops)


def _locate_table(content: str, rel: str, table_id: str | None, heading: str | None):
    """Return ``(table, (start0, end0))`` — full-file 0-based inclusive line span."""
    from . import table_markdown as tm

    full_lines = content.split("\n")
    body, body_line = core._body_start_line(content)
    fm_off = body_line - 1
    tables = tm.find_tables(body.split("\n"))
    if not tables:
        raise _TableOpError("no_table", "note has no GFM table")

    def span(t):
        return (fm_off + t.start_line, fm_off + t.end_line)

    if table_id:
        for t in tables:
            if tm.table_id_for(rel, body_line + t.start_line, t.table_index) == table_id:
                return t, span(t)
        raise _TableOpError("table_not_found", f"table_id {table_id!r} not found (re-search after reindex)")
    if heading:
        sec = find_section(full_lines, heading)
        matches = [
            t for t in tables
            if sec.heading_line <= (fm_off + t.start_line) < sec.body_end
        ]
        if not matches:
            raise _TableOpError("table_not_found", f"no table under heading {heading!r}")
        if len(matches) > 1:
            raise _TableOpError(
                "table_ambiguous",
                f"heading {heading!r} contains {len(matches)} tables; pass table_id=",
            )
        return matches[0], span(matches[0])
    if len(tables) == 1:
        return tables[0], span(tables[0])
    raise _TableOpError("table_ambiguous", "multiple tables in note; pass table_id= or heading=")


def _row_content_hash_map(content: str, rel: str) -> dict[tuple[str, int], str]:
    """(table_id, row_index) → indexed flattened content_hash for the current content."""
    out: dict[tuple[str, int], str] = {}
    for row in core._markdown_chunk_rows(content, rel):
        meta = row[10]
        if isinstance(meta, dict) and meta.get("chunk_kind") == "table_row":
            out[(meta["table_id"], meta["row_index"])] = row[8]
    return out


def _fresh_row_hashes(content: str, rel: str) -> dict[tuple[str, str], tuple[str, str]]:
    """(table_id, row_key) → (content_hash, row_hash) for every data row in ``content``."""
    from . import table_markdown as tm
    from .table_contract import key_column_for

    key_column = key_column_for(vaults.notes_root(), rel)
    hmap = _row_content_hash_map(content, rel)
    out: dict[tuple[str, str], tuple[str, str]] = {}
    body, body_line = core._body_start_line(content)
    for t in tm.find_tables(body.split("\n")):
        tid = tm.table_id_for(rel, body_line + t.start_line, t.table_index)
        for i, cells in enumerate(t.rows):
            rk = tm.row_key_for(t, i, key_column=key_column)
            out[(tid, rk)] = (hmap.get((tid, i), ""), tm.row_raw_hash(cells))
    return out


def _find_row_index(table, row_key: str, *, key_column: str | None = None) -> int:
    from . import table_markdown as tm

    for i in range(len(table.rows)):
        if tm.row_key_for(table, i, key_column=key_column) == row_key:
            return i
    return -1


def _col_index(table, column: str) -> int:
    from . import table_markdown as tm

    target = tm.normalize_header(column)
    for i, h in enumerate(table.headers):
        if tm.normalize_header(h) == target:
            return i
    return -1


def _splice_table(content: str, span: tuple[int, int], new_gfm: str) -> str:
    full_lines = content.split("\n")
    merged = full_lines[: span[0]] + new_gfm.split("\n") + full_lines[span[1] + 1 :]
    return "\n".join(merged)


def _apply_one_table_op(content: str, rel: str, op: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Apply a single table op to ``content``; return (new_content, result meta)."""
    from . import table_markdown as tm
    from .table_contract import key_column_for

    kind = op["op"]
    table, span = _locate_table(content, rel, op.get("table_id"), op.get("heading"))
    table_id = tm.table_id_for(
        rel, core._body_start_line(content)[1] + table.start_line, table.table_index
    )
    key_column = key_column_for(vaults.notes_root(), rel)

    def _require_row_precondition(ri: int) -> None:
        exp_c = op.get("expected_content_hash")
        exp_r = op.get("expected_row_hash")
        if not exp_c and not exp_r:
            raise _TableOpError(
                "precondition_required",
                f"{kind} requires expected_content_hash or expected_row_hash "
                "(from a search row hit or read_note(format=row))",
            )
        cur_r = tm.row_raw_hash(table.rows[ri])
        if exp_r and exp_r != cur_r:
            raise _TableOpError("stale_write", f"row_hash stale; fresh row_hash={cur_r}")
        if exp_c:
            cur_c = _row_content_hash_map(content, rel).get((table_id, ri), "")
            if exp_c != cur_c:
                raise _TableOpError("stale_write", f"content_hash stale; fresh content_hash={cur_c}")

    if kind in ("update_cell", "update_row", "delete_row"):
        ri = _find_row_index(table, op["row_key"], key_column=key_column)
        if ri < 0:
            raise _TableOpError("row_not_found", f"row_key {op['row_key']!r} not found")
        _require_row_precondition(ri)

    if kind == "update_cell":
        ci = _col_index(table, op["column"])
        if ci < 0:
            raise _TableOpError("unknown_column", f"column {op['column']!r} not in table")
        table.rows[ri][ci] = str(op.get("value", ""))
    elif kind == "update_row":
        for col, val in (op.get("columns") or {}).items():
            ci = _col_index(table, col)
            if ci < 0:
                raise _TableOpError("unknown_column", f"column {col!r} not in table")
            table.rows[ri][ci] = str(val)
    elif kind == "delete_row":
        del table.rows[ri]
    elif kind == "append_row":
        row_map = op.get("row") or {}
        cells = [""] * len(table.headers)
        for col, val in row_map.items():
            ci = _col_index(table, col)
            if ci < 0:
                raise _TableOpError(
                    "unknown_column",
                    f"column {col!r} not in table; use alter_table_schema to add it",
                )
            cells[ci] = str(val)
        table.rows.append(cells)
        # Capture the new row_key for the response (after append, last index).
        op = {**op, "row_key": tm.row_key_for(table, len(table.rows) - 1, key_column=key_column)}
    elif kind == "replace_table":
        _apply_replace_table(table, op)
    elif kind == "alter_table_schema":
        _apply_alter_schema(table, op)
    else:  # pragma: no cover - guarded by TABLE_OPS
        raise _TableOpError("invalid_op", f"unknown table op {kind!r}")

    new_gfm = tm.records_to_gfm(table.headers, table.rows, alignments=table.alignments)
    new_content = _splice_table(content, span, new_gfm)

    meta: dict[str, Any] = {"op": kind, "status": "ok", "table_id": table_id}
    if op.get("row_key"):
        meta["row_key"] = op["row_key"]
    return new_content, meta


def _apply_replace_table(table, op: dict[str, Any]) -> None:
    from . import table_markdown as tm

    if op.get("csv") is not None:
        headers_in, records = tm.csv_to_records(op["csv"])
    elif op.get("rows") is not None:
        records = [{str(k): str(v) for k, v in r.items()} for r in op["rows"]]
        headers_in = []
        for r in records:
            for k in r:
                if k not in headers_in:
                    headers_in.append(k)
    else:
        raise _TableOpError("bad_request", "replace_table requires rows= or csv=")

    merge = op.get("merge", "replace")
    if merge == "replace":
        table.headers[:] = headers_in
        table.rows[:] = tm.dicts_to_rows(headers_in, records)
        table.alignments[:] = []
        return

    # append / upsert keep existing schema; map incoming headers (fuzzy, ambiguity-reject).
    try:
        mapping = tm.fuzzy_header_map(
            headers_in, table.headers, allow_new_columns=op.get("allow_new_columns", False)
        )
    except tm.HeaderAmbiguous as e:
        raise _TableOpError("header_ambiguous", str(e), e.suggestions)

    mapped_records: list[dict[str, str]] = []
    for rec in records:
        mapped_records.append({mapping.get(k, k): v for k, v in rec.items()})

    if merge == "append":
        table.rows.extend(tm.dicts_to_rows(table.headers, mapped_records))
        return
    if merge == "upsert":
        existing = {tm.row_key_for(table, i): i for i in range(len(table.rows))}
        seen: set[str] = set()
        for rec in mapped_records:
            cells = [str(rec.get(h, "")) for h in table.headers]
            key = next((c for c in cells if c.strip()), "")
            if key in seen:
                # last-wins within this import
                pass
            seen.add(key)
            if key in existing:
                table.rows[existing[key]] = cells
            else:
                table.rows.append(cells)
        return
    raise _TableOpError("bad_request", f"unknown merge mode {merge!r}")


def _apply_alter_schema(table, op: dict[str, Any]) -> None:
    from . import table_markdown as tm

    if not op.get("confirm"):
        raise _TableOpError(
            "confirm_required",
            "alter_table_schema changes every row's embedding; pass confirm=true",
        )
    renames = op.get("rename_columns") or {}
    if renames:
        for i, h in enumerate(table.headers):
            for old, new in renames.items():
                if tm.normalize_header(h) == tm.normalize_header(old):
                    table.headers[i] = new
    add = op.get("add_column")
    if add:
        table.headers.append(add)
        for row in table.rows:
            row.append("")
        if table.alignments:
            table.alignments.append("")
    drop = op.get("drop_column")
    if drop:
        di = _col_index(table, drop)
        if di < 0:
            raise _TableOpError("unknown_column", f"column {drop!r} not in table")
        del table.headers[di]
        for row in table.rows:
            if di < len(row):
                del row[di]
        if table.alignments and di < len(table.alignments):
            del table.alignments[di]


def _apply_table_ops(
    b,
    root: Path,
    full: Path,
    rel: str,
    content: str,
    dict_ops: list[dict[str, Any]],
    *,
    strict: bool,
    dry_run: bool,
    expected_mtime: float | None,
) -> dict[str, Any]:
    """Row-keyed table mutations. MCP never writes the index — the file is rewritten
    and the path enqueued so the watcher re-embeds (only changed rows re-embed via
    content-hash reuse). Responses return content_hash + ``reembed=pending``, not a
    live searchable chunk_hash."""
    from .patch_ops import TABLE_OPS

    if any(o.get("op") not in TABLE_OPS for o in dict_ops):
        return _err(
            path=rel,
            error="bad_request",
            message="table row ops cannot be mixed with prose/frontmatter ops in one call",
        )
    if expected_mtime is not None:
        stale = _check_mtime(full, expected_mtime, rel)
        if stale:
            return stale

    new_content = content
    results: list[dict[str, Any]] = []
    applied = 0
    for i, op in enumerate(dict_ops):
        try:
            new_content, meta = _apply_one_table_op(new_content, rel, op)
            meta["op_index"] = i
            results.append(meta)
            applied += 1
        except _TableOpError as e:
            results.append(
                {"op_index": i, "op": op.get("op"), "status": "error", "code": e.code, "message": e.message}
            )
            if strict:
                return _err(
                    path=rel, applied=0, results=results, error=e.code,
                    message=e.message, suggestions=e.suggestions or None,
                )

    if applied == 0:
        first = next((r for r in results if r["status"] == "error"), None)
        return _err(
            path=rel, applied=0, results=results,
            error=(first or {}).get("code", "table_op_failed"),
            message=(first or {}).get("message", "no table op applied"),
        )

    if dry_run:
        return {"ok": True, "path": rel, "dry_run": True, "applied": applied, "results": results, "vault": b.name}

    full.write_text(new_content, encoding="utf-8")
    _enqueue_index(b, full)

    # New content_hash per touched row — keyed by (table_id, row_key) so multi-table
    # notes never hit table_ambiguous when attaching fresh hashes.
    fresh = _fresh_row_hashes(new_content, rel)
    for r in results:
        if r["status"] != "ok" or not r.get("row_key") or not r.get("table_id"):
            continue
        pair = fresh.get((r["table_id"], r["row_key"]))
        if pair:
            r["content_hash"], r["row_hash"] = pair

    failed = sum(1 for r in results if r["status"] == "error")
    out = {
        "ok": failed == 0,
        "path": rel,
        "applied": applied,
        "failed": failed,
        "partial": bool(failed and applied),
        "results": results,
        "reembed": "pending",
        "bytes": full.stat().st_size,
        "mtime": _mtime(full),
        "vault": b.name,
    }
    out = _finalize_write(out, vault=b.name, path=rel, expected_mtime=expected_mtime, content=new_content)
    return _attach_tip(
        out,
        "table rows rewritten on disk; re-search or wait for the watcher before "
        "read_note(chunk_hash=) — address rows by row_key/table_id meanwhile",
    )


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
    ref: str = "",
) -> dict[str, Any]:
    bad = _reject_if_ref(ref, tool="patch_note")
    if bad:
        return bad
    try:
        b, path = _resolve_binding_and_rel(path, vault)
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

    # Row-keyed table ops take a dedicated, strict path (no markdown section apply).
    if _table_ops_present(dict_ops):
        if is_yaml_note(path):
            return _err(path=path, error="unsupported_format", message="table ops are markdown-only")
        return _apply_table_ops(
            b, root, full, path, content, dict_ops,
            strict=strict if strict else True,  # table ops default strict=true
            dry_run=dry_run,
            expected_mtime=expected_mtime,
        )

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
    to_write, write_flaws = _prepare_write_content(
        to_write, path=path, vault=b.name, okf_result=okf
    )

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
    out = _attach_flaws(out, write_flaws)
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


def _op_to_dict(op: Any) -> dict[str, Any]:
    if isinstance(op, dict):
        return dict(op)
    if hasattr(op, "model_dump"):
        return op.model_dump(mode="python", exclude_none=True)
    raise TypeError(f"unsupported patch op type: {type(op)!r}")


def _all_place_ops(ops: list[Any]) -> bool:
    if not ops:
        return False
    for op in ops:
        data = _op_to_dict(op)
        if data.get("op") != "place":
            return False
    return True


def _dispatch_place_op(
    op: dict[str, Any],
    *,
    vault: str,
    expected_mtime: float | None = None,
) -> dict[str, Any]:
    src = str(op.get("src") or "").strip()
    dst = str(op.get("dst") or "").strip()
    if not src or not dst:
        return _err(
            error="bad_request",
            message="place op requires src= and dst=",
        )
    fields = op.get("fields")
    if fields is not None and not isinstance(fields, dict):
        return _err(error="bad_request", message="place op fields must be an object")
    return place_note(
        src,
        dst,
        overwrite=bool(op.get("overwrite")),
        fields=fields if isinstance(fields, dict) else None,
        expected_mtime=expected_mtime,
        vault=vault,
    )


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
    has_place_single = (
        ops is not None
        and isinstance(ops, list)
        and _all_place_ops(ops)
        and not path_s
    )

    if has_items and (has_single or has_place_single):
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
    if has_place_single:
        if len(ops or []) != 1:
            return _err(
                error="bad_request",
                message="single place mode accepts exactly one place op",
            )
        return _dispatch_place_op(
            _op_to_dict(ops[0]),
            vault=vault,
            expected_mtime=expected_mtime,
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
        message=(
            "provide path+ops for one note, place-only ops=[{op:place,src,dst}], "
            "or items=[{path?, ops, expected_mtime?}] for a batch"
        ),
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

    Prefixed paths (``vault_id:rel``) must all resolve to the same vault_id in v1.
    """
    if not isinstance(items, list) or not items:
        return _err(
            error="bad_request",
            message="`items` must be a non-empty array of {path, ops, expected_mtime?}",
        )
    if len(items) > _PATCH_NOTES_MAX_ITEMS:
        return _err(
            error="bad_request",
            message=f"`items` exceeds max of {_PATCH_NOTES_MAX_ITEMS}",
        )

    # Peel path prefixes and require a single vault for the whole batch.
    peeled: list[tuple[int, dict[str, Any], str, list[Any]]] = []
    vault_ids: set[str] = set()
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            return _err(
                error="bad_item",
                message=f"items[{i}] must be an object",
            )
        path_raw = raw.get("path")
        path = path_raw.strip() if isinstance(path_raw, str) else ""
        ops_list = raw.get("ops")
        place_only = isinstance(ops_list, list) and _all_place_ops(ops_list)
        if place_only:
            # place ops carry src/dst; peel via place_note later using vault=
            peeled.append((i, raw, path, ops_list if isinstance(ops_list, list) else []))
            continue
        if not path:
            return _err(
                error="bad_request",
                message=f"items[{i}].path string required",
            )
        try:
            bi, path_rel = _resolve_binding_and_rel(path, vault)
        except OpsError as e:
            return _err(path=path, error=e.code, message=e.message)
        vault_ids.add(bi.name)
        peeled.append((i, raw, path_rel, ops_list if isinstance(ops_list, list) else []))

    if vault_ids and len(vault_ids) > 1:
        return _err(
            error="bad_request",
            message=(
                "v1 patch batches must target a single vault; "
                f"got {sorted(vault_ids)}"
            ),
        )

    try:
        if vault_ids:
            b = _binding(next(iter(vault_ids)))
        else:
            b = _binding(vault)
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    ok_n = 0
    fail_n = 0

    for i, raw, path, ops_list in peeled:
        place_only = isinstance(ops_list, list) and _all_place_ops(ops_list)
        if not place_only:
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

        def _opt_hash(key: str, _raw: dict[str, Any] = raw) -> str | None:
            val = _raw.get(key)
            if val is None:
                return None
            if not isinstance(val, str):
                return None
            s = val.strip()
            return s or None

        if place_only:
            if len(ops_list) != 1:
                item_out = {
                    "ok": False,
                    "error": "bad_request",
                    "message": f"items[{i}] place batch accepts one place op per item",
                    "index": i,
                    "vault": b.name,
                }
            else:
                item_out = _dispatch_place_op(
                    _op_to_dict(ops_list[0]),
                    vault=b.name,
                    expected_mtime=expected,
                )
        else:
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
    ref: str = "",
) -> dict[str, Any]:
    """Place a note at ``dst``: move if ``src`` is in the vault, else copy from host.

    - Vault-relative ``src``, or absolute path under the vault root → ``moved``
      (same as former ``move_note``; ``fields`` not allowed).
    - Absolute host ``.md`` outside the vault (allow-roots) → ``copied`` / promote
      (same as former ``send_note``; leaves src; optional ``fields`` merge).
    """
    bad = _reject_if_ref(ref, tool="place_note")
    if bad:
        return bad
    raw = (src or "").strip()
    if not raw:
        return _err(src=src, dst=dst, error="bad_path", message="src required")

    expanded = Path(raw).expanduser()
    try:
        # Prefer dst prefix (always vault-relative) for binding; peel vault-relative src too.
        if not expanded.is_absolute():
            b_src, raw = _resolve_binding_and_rel(raw, vault)
            b_dst, dst = _resolve_binding_and_rel(dst, vault)
            if b_src.name != b_dst.name:
                raise OpsError(
                    "bad_request",
                    f"src vault {b_src.name!r} conflicts with dst vault {b_dst.name!r}",
                )
            b = b_src
        else:
            b, dst = _resolve_binding_and_rel(dst, vault)
        root = b.resolved().root.resolve()
    except OpsError as e:
        return _err(src=src, dst=dst, error=e.code, message=e.message)
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
        b_src, src = _resolve_binding_and_rel(src, vault)
        b_dst, dst = _resolve_binding_and_rel(dst, vault)
        if b_src.name != b_dst.name:
            raise OpsError(
                "bad_request",
                f"src vault {b_src.name!r} conflicts with dst vault {b_dst.name!r}",
            )
        b = b_src
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
        b, dst = _resolve_binding_and_rel(dst, vault)
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
    to_write, write_flaws = _prepare_write_content(
        to_write, path=dst, vault=b.name, okf_result=okf
    )

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
    out = _attach_flaws(out, write_flaws)
    if new_top:
        out["warning"] = (
            f"created new top-level directory {parts[0]!r} — "
            f"existing top-level dirs: {_top_level_dirs(root)}"
        )
    return _finalize_write(
        out, vault=b.name, path=dst, expected_mtime=expected_mtime, content=to_write
    )


def delete_note(path: str, *, vault: str = "", ref: str = "") -> dict[str, Any]:
    bad = _reject_if_ref(ref, tool="delete_note")
    if bad:
        return bad
    try:
        b, path = _resolve_binding_and_rel(path, vault)
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
    return _stamp_qualified(
        _attach_watcher_tip(out), vault=b.name, path=path
    )


def git_sync_op(
    action: str = "status",
    *,
    message: str = "",
    vault: str = "",
) -> dict[str, Any]:
    """Git contract sync: status | run | pull | rebase | clear_block.

    ``run`` commits all dirty paths except ``never_commit`` / gitignore, then pushes.
    Auto path uses path-aware template message; pass ``message`` to override subject
    (tool-triggered). A Paths body trailer is always attached. ``rebase`` recovers
    from a diverged remote (fetch + rebase local commits, push if configured) when
    ``pull`` (ff-only) has blocked.
    """
    try:
        b = _binding(vault)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)

    act = (action or "status").strip().lower()
    if act not in ("status", "run", "pull", "rebase", "clear_block"):
        return _err(
            error="bad_action",
            message="action must be status|run|pull|rebase|clear_block",
            vault=b.name,
        )
    out = git_sync.run_action(
        root,
        act,  # type: ignore[arg-type]
        message=(message or "").strip() or None,
    )
    out.setdefault("vault", b.name)
    return out


def list_refs_op(*, vault: str = "", kind: str = "heads") -> dict[str, Any]:
    """Reachable git refs at the vault registry root (for ``ref=`` discovery)."""
    try:
        b = _binding(vault)
        root = b.resolved().root
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    k = (kind or "heads").strip().lower()
    try:
        refs = git_catalog.list_refs(root, kind=k)
    except git_catalog.GitCatalogError as e:
        return _err(error=e.code, message=e.message, vault=b.name, root=str(root))
    return {
        "ok": True,
        "vault": b.name,
        "root": str(root),
        "kind": k,
        "refs": refs,
        "count": len(refs),
    }


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
    vaults: list[str] | None = None,
    full: bool = False,
    days: int | None = 7,
    folder: str = "",
    limit: int | None = 50,
    offset: int = 0,
    fix: bool = False,
) -> dict[str, Any]:
    """Vault management: list | contracts | describe | merge | project | stats | lint.

    Read-only except ``stats`` (habit KPI rollups). ``project`` returns desk ``body`` + ``guidance``.
    ``lint`` emits corpus ``flaws[]`` (archival + note_lint detectors) for one vault.
    ``fix=true`` on lint applies mechanical auto remediations (trailing WS) only.
    """
    act = (action or "list").strip().lower()
    if act not in (
        "list",
        "contracts",
        "describe",
        "merge",
        "project",
        "stats",
        "lint",
    ):
        return _err(
            error="bad_action",
            message="action must be list|contracts|describe|merge|project|stats|lint",
        )

    if act == "stats":
        from apo_engine import telemetry_ops

        key = (vault or "").strip()
        try:
            b = _binding(key) if key else _binding("")
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        if days is not None and days < 0:
            return _err(error="bad_request", message="days must be >= 0 or null")
        out = telemetry_ops.vault_stats(
            vault_root=b.resolved().root,
            collection=b.collection,
            days=days,
        )
        out["vault"] = b.name
        return out

    if act == "lint":
        if vaults:
            return _err(
                error="bad_request",
                message="lint accepts vault= (single vault), not vaults=",
            )
        key = (vault or "").strip()
        try:
            b = _binding(key) if key else _binding("")
        except OpsError as e:
            return _err(error=e.code, message=e.message)
        if limit is not None and limit < 0:
            return _err(error="bad_request", message="limit must be >= 0 or null")
        if offset < 0:
            return _err(error="bad_request", message="offset must be >= 0")
        root = b.resolved().root
        folder_s = (folder or "").strip()
        lim = 50 if limit is None else int(limit)
        off = int(offset)
        # Collect full detector sets then paginate once (stable merge).
        data = archival_contract.load_archival_contract(root)
        arch = archival_contract.lint_vault(
            root,
            data,
            folder=folder_s,
            limit=100_000,
            offset=0,
            vault_name=b.name,
        )
        notes = note_lint.lint_folder(
            root,
            folder=folder_s,
            limit=100_000,
            offset=0,
            vault_name=b.name,
            include_links=True,
            fix=bool(fix),
        )
        merged_flaws: list[dict[str, Any]] = []
        for part in (arch, notes):
            for f in part.get("flaws") or []:
                if isinstance(f, dict):
                    merged_flaws.append(f)
        counts: dict[str, int] = {}
        for f in merged_flaws:
            code = str(f.get("code") or "?")
            counts[code] = counts.get(code, 0) + 1
        sliced = merged_flaws[off : off + lim]
        out: dict[str, Any] = {
            "ok": True,
            "action": "lint",
            "flaws": sliced,
            "counts_by_code": counts,
            "total_flaws": len(merged_flaws),
            "has_more": (off + len(sliced)) < len(merged_flaws),
            "offset": off,
            "limit": lim,
            "vault": b.name,
            "folder": folder_s,
            "fix": bool(fix),
        }
        warnings: list[str] = []
        tips: list[str] = []
        for part in (arch, notes):
            w = part.get("warning")
            if w:
                warnings.append(str(w))
            t = part.get("tip")
            if t:
                tips.append(str(t))
        if warnings:
            out["warning"] = "; ".join(warnings)
        if tips:
            out["tip"] = "; ".join(dict.fromkeys(tips))
        return out

    try:
        default_name, bindings = _load_bindings()
    except Exception as e:
        return _err(error="bad_vault", message=str(e))

    if vaults:
        if vault:
            return _err(error="bad_request", message="pass vault= or vaults=, not both")
        names = [v.strip() for v in vaults if isinstance(v, str) and v.strip()]
        unknown = [n for n in names if n not in bindings]
        if unknown:
            return _err(
                error="bad_vault",
                message=f"unknown vault(s) {unknown!r}; available: {sorted(bindings)}",
            )
        bindings = {k: b for k, b in bindings.items() if k in names}
        if default_name not in bindings:
            default_name = sorted(bindings)[0] if bindings else default_name

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
        pub_desk = vault_project.scope_desk_overlay(
            vault_desk.public_desk(desk), merged_vaults
        )
        return {
            "ok": True,
            "action": "merge",
            "full": bodies,
            "default_vault": default_name,
            "desk": pub_desk,
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
        projected = vault_project.project(merge)
        if not projected.get("ok"):
            return projected
        projected["default_vault"] = merge.get("default_vault")
        projected["desk_meta"] = merge.get("desk_meta")
        return projected

    # describe — single vault (default when vault empty). Resolve against the
    # (possibly vaults=-filtered) bindings loaded above, not a fresh _binding()
    # call, so a subset filter actually constrains which vault "default" means.
    key = (vault or "").strip() or default_name
    if key not in bindings:
        return _err(
            error="bad_vault",
            message=f"unknown vault {key!r}; available: {sorted(bindings)}",
        )
    b = bindings[key]
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
