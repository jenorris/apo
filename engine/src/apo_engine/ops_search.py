"""Ops search surface: multi-vault search fan-out and filter_notes."""

from __future__ import annotations

import contextvars
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from apo_engine import config, core, git_catalog, search_contract, vaults
from apo_engine.agent_args import project_frontmatter, resolve_top_k, resolve_where
from apo_engine.mcp_backend import shape_search_hits
from apo_engine.path_ref import (
    merge_vault_arg,
    PathRefError,
    peel_path_ref,
    qualified_path as _qualified_path,
)
from apo_engine.ops_common import (
    _attach_folder_tip,
    _attach_tip,
    _binding,
    _err,
    _missing_folder_warning,
    _resolve_binding_and_rel,
    _safe_resolve,
    _sqlite_index_err,
    OpsError,
)


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
    elif config.EMBED_BACKEND == "llamacpp":
        fix = (
            "results are keyword-only (BM25) until the embed backend is back — "
            f"check that llama-server is running with --embeddings (APO_LLAMACPP_URL={config.LLAMACPP_URL})"
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
    expand: bool = False,
    intent: str = "",
    explain: bool = False,
) -> tuple[list[dict[str, Any]], list[str], bool, list[str] | None]:
    """Run hybrid search in one vault. Returns (rows, warnings, reranked, default_exclude)."""
    warnings: list[str] = []
    root = b.resolved().root
    effective_exclude, applied_default, _source = search_contract.resolve_search_exclude(
        root,
        caller_exclude=exclude,
        folder_clean=folder_clean,
        query=query,
    )
    with vaults.bind(b):
        if expand:
            hits = core.search_expanded(
                query,
                intent=intent,
                k=k,
                folder=folder_clean,
                snippet_chars=snippet_chars,
                exclude=effective_exclude,
                explain=explain,
            )
        else:
            hits = core.search(
                query,
                k=k,
                folder=folder_clean,
                snippet_chars=snippet_chars,
                exclude=effective_exclude,
                hybrid=hybrid,
                explain=explain,
            )
        layout = vaults.read_usage_layout(root)
        results = shape_search_hits(hits, layout=layout, explain=explain)
        rr = core.last_search_rerank()
        degraded = core.last_search_degraded()
    if stamp_vault:
        for row in results:
            row["vault"] = b.name
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


def _search_at_ref(
    query: str,
    b: vaults.VaultBinding,
    *,
    ref: str,
    folder: str,
    folders: list[str] | None,
    snippet_chars: int,
    exclude: list[str] | None,
    top_k: int | None,
    limit: int | None,
    offset: int,
) -> dict[str, Any]:
    """FTS-only search over a git tip (no embeddings / chunk_hash)."""
    k, err = resolve_top_k(top_k, limit)
    if err:
        return _err(error="bad_request", message=err)
    if offset < 0:
        return _err(error="bad_request", message="offset must be >= 0")
    if folder.strip() and folders:
        return _err(error="bad_request", message="pass folder= or folders=[], not both")
    folder_clean = ""
    if folders:
        cleaned = [f for f in folders if isinstance(f, str) and f.strip()]
        if len(cleaned) > 1:
            return _err(
                error="bad_request",
                message="search_notes ref= accepts a single folder= (not folders=[] fan-out)",
            )
        folder_clean = cleaned[0].strip() if cleaned else ""
    elif folder.strip():
        folder_clean = folder.strip()
    root = b.resolved().root
    if folder_clean:
        try:
            _safe_resolve(root, folder_clean)
        except ValueError as e:
            return _err(error="bad_path", message=str(e))
    effective_exclude, applied_default, _source = search_contract.resolve_search_exclude(
        root,
        caller_exclude=exclude,
        folder_clean=folder_clean,
        query=query,
    )
    try:
        with vaults.bind(b):
            resolved = git_catalog.ensure_catalog(root, ref)
            page, total = core.search_at_ref(
                query,
                resolved.tree_oid,
                folder=folder_clean,
                exclude=effective_exclude,
                k=k,
                offset=offset,
                snippet_chars=snippet_chars,
            )
    except git_catalog.GitCatalogError as e:
        return _err(error=e.code, message=e.message)
    except Exception as e:
        return _err(error="git_error", message=str(e)[:400])
    results: list[dict[str, Any]] = []
    for hit in page:
        path = str(hit.get("path") or "")
        mtime = float(hit.get("mtime") or 0.0)
        row = {
            "source": path,
            "content": hit.get("content") or "",
            "score": round(float(hit.get("score") or 0.0), 4),
            "mtime": mtime,
            "modified": datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            if mtime
            else None,
            "qualified_path": _qualified_path(b.name, path),
        }
        results.append(row)
    out: dict[str, Any] = {
        "ok": True,
        "results": results,
        "vault": b.name,
        "source": "git_ref",
        "ref": resolved.ref,
        "tree_oid": resolved.tree_oid,
        "has_more": offset + len(results) < total,
    }
    if offset:
        out["offset"] = offset
    if applied_default:
        out["default_exclude"] = applied_default
    if results:
        out = _attach_tip(
            out,
            "ref= FTS is note-level — follow up with read_note(path=…, ref=…); no chunk_hash",
        )
    return _attach_folder_tip(out, folder_clean, root=root)


_SEARCH_FANOUT_WORKERS = 3
_search_fanout_pool_lock = threading.Lock()


def _new_search_fanout_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=_SEARCH_FANOUT_WORKERS,
        thread_name_prefix="apo-search",
    )


_search_fanout_pool = _new_search_fanout_pool()


def _reset_search_fanout_pool() -> None:
    """Replace the pool and close worker-local readers after registry changes."""
    global _search_fanout_pool
    with _search_fanout_pool_lock:
        old_pool = _search_fanout_pool
        _search_fanout_pool = _new_search_fanout_pool()
    old_pool.shutdown(wait=True)


def _run_search_pairs(
    pairs: list[tuple[vaults.VaultBinding, str]],
    run_pair: Callable[[vaults.VaultBinding, str], tuple[str, Any]],
) -> list[tuple[str, Any]]:
    """Run ``run_pair`` per (binding, folder) pair; outcomes keep ``pairs`` order.

    One pair stays on the caller thread. Several share a small pool; each task runs
    in its own copy of the caller's context so ``vaults.bind`` and the per-search
    rerank/degraded ContextVars never leak between workers.
    """
    if len(pairs) <= 1:
        return [run_pair(b, f) for b, f in pairs]
    with _search_fanout_pool_lock:
        pool = _search_fanout_pool
        futures = [
            pool.submit(contextvars.copy_context().run, run_pair, b, f)
            for b, f in pairs
        ]
    return [fut.result() for fut in futures]


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
    ref: str = "",
    expand: bool = False,
    intent: str = "",
    explain: bool = False,
) -> dict[str, Any]:
    ref_s = (ref or "").strip()
    if ref_s and expand:
        return _err(
            error="bad_request",
            message="search_notes ref= does not support expand= (git-tip search is FTS-only already)",
        )
    if ref_s and vaults:
        return _err(
            error="bad_request",
            message="search_notes ref= is single-vault — do not combine with vaults=[]",
        )
    try:
        targets = _resolve_search_bindings(vault, vaults)
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    if ref_s:
        return _search_at_ref(
            query,
            targets[0],
            ref=ref_s,
            folder=folder,
            folders=folders,
            snippet_chars=snippet_chars,
            exclude=exclude,
            top_k=top_k,
            limit=limit,
            offset=offset,
        )
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
    def run_pair(b: vaults.VaultBinding, folder_clean: str) -> tuple[str, Any]:
        try:
            return "ok", _search_one_vault(
                query,
                b,
                k=fetch_k,
                folder_clean=folder_clean,
                snippet_chars=snippet_chars,
                exclude=exclude,
                hybrid=hybrid,
                stamp_vault=fanout_vaults or fanout_folders,
                expand=expand,
                intent=intent,
                explain=explain,
            )
        except SystemExit as e:
            return "exit", e
        except Exception as e:
            return "error", e

    outcomes = _run_search_pairs(pairs, run_pair)
    for (b, folder_clean), (status, value) in zip(pairs, outcomes):
        if status == "ok":
            rows, w, reranked, applied_default = value
            if applied_default:
                if fanout_vaults:
                    default_exclude_by_vault[b.name] = applied_default
                else:
                    default_exclude = applied_default
            all_rows.extend(rows)
            warnings.extend(w)
            any_reranked = any_reranked or reranked
            continue
        if status == "exit":
            msg = str(value) or "index unavailable"
        else:
            if err := _sqlite_index_err(value):
                return err
            msg = str(value)
        if total_attempts > 1:
            failed += 1
            prefix = f"vault {b.name}" if fanout_vaults else ""
            if folder_clean:
                prefix = f"{prefix} folder {folder_clean}".strip()
            warnings.append(f"{prefix}: search_failed ({msg})".strip())
            continue
        return _err(error="search_failed", message=msg)

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
    except sqlite3.Error as e:
        if err := _sqlite_index_err(e):
            return err
        raise
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
