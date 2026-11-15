"""Ops read surface: read_note, expand_chunk, backlinks, graph export/neighbors,
and history."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apo_engine import (
    config,
    core,
    git_catalog,
    git_contract,
    links,
    note_lint,
    search_contract,
    vaults,
)
from apo_engine.agent_args import project_frontmatter, shape_note_read
from apo_engine.markdown_patch import normalize_lines, PatchError, section_from_chunk
from apo_engine.note_format import ensure_indexed_path, is_exposed_note_rel
from apo_engine.path_ref import qualified_path as _qualified_path
from apo_engine.write_guard import (
    attach_region_hashes,
    content_hash as region_content_hash,
)
from apo_engine.ops_common import (
    _attach_flaws,
    _binding,
    _err,
    _mtime,
    _resolve_binding_and_rel,
    _safe_resolve,
    _sqlite_index_err,
    _stamp_qualified,
    OpsError,
)
from apo_engine.ops_guard import _record_path_touch


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
    if format not in ("markdown", "json", "row", "node"):
        return _err(error="bad_request", message="format must be markdown|json|row|node")

    try:
        with vaults.bind(b):
            chunk = core.lookup_chunk(chunk_hash, include_text=True)
    except sqlite3.Error as e:
        if err := _sqlite_index_err(e):
            return err
        raise
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
    try:
        with vaults.bind(b):
            order = core.note_chunk_order(rel)
    except sqlite3.Error as e:
        if err := _sqlite_index_err(e):
            return err
        raise
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
    if format in ("json", "row", "node"):
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

    if format == "node":
        if chunk_kind != "mermaid_node":
            out_sec["format_warning"] = "format=node only valid on a mermaid_node chunk_hash"
            return
        from . import mermaid_parse as mp

        body, _body_line = core._body_start_line(file_text)
        if rel.endswith(".mmd"):
            src = file_text
        else:
            from . import mermaid_markdown as mm

            diagram_id = str(chunk.get("table_id") or "")
            block_index = 0
            if "#mermaid-" in diagram_id:
                try:
                    block_index = int(diagram_id.rsplit("#mermaid-", 1)[-1])
                except ValueError:
                    block_index = 0
            fences = mm.find_mermaid_fences(body.split("\n"))
            fence = next((f for f in fences if f.block_index == block_index), None)
            if fence is None and fences:
                fence = fences[0]
            if fence is None:
                out_sec["format_warning"] = "mermaid block not found on disk"
                return
            src = fence.text
        diagram = mp.parse_mermaid(src)
        node_id = chunk.get("row_key") or ""
        node = next((n for n in diagram.nodes if n.node_id == node_id), None)
        if node is None:
            out_sec["format_warning"] = f"node {node_id!r} not found in diagram"
            return
        from . import mermaid_index as mi

        _label_by_id, in_map, out_map = mi.node_relations(diagram)
        out_sec["format"] = "node"
        out_sec["node"] = {
            "id": node.node_id,
            "label": node.label,
            "subgraph": node.subgraph,
            "in_labels": in_map.get(node.node_id, []),
            "out_labels": out_map.get(node.node_id, []),
        }
        out_sec["diagram_id"] = chunk.get("table_id")
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
    if path_s and not ch:
        rel_for_check = path_s
        head, _, tail = path_s.partition(":")
        if tail and "/" not in head:
            rel_for_check = tail
        if not is_exposed_note_rel(rel_for_check):
            return _err(path=path_s, error="bad_path", message="path is not a note")
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
        try:
            with vaults.bind(b):
                toc_out = _build_toc(path_s, file_bytes)
        except sqlite3.Error as e:
            if err := _sqlite_index_err(e):
                return err
            raise
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
        from apo_engine import vaults as _vaults

        try:
            _, _bindings = _vaults.load_bindings()
            _vault_roots = {n: bb.resolved().root for n, bb in _bindings.items()}
        except Exception:
            _vault_roots = None
        _, lint_flaws = note_lint.lint_note(
            text,
            path=path_s,
            vault_root=root,
            vault=b.name,
            include_links=True,
            include_usage=True,
            include_format=True,
            vault_roots=_vault_roots,
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
        from apo_engine import vaults as _vaults

        try:
            _, _bindings = _vaults.load_bindings()
            _vault_roots = {n: bb.resolved().root for n, bb in _bindings.items()}
        except Exception:
            _vault_roots = None
        _, lint_flaws = note_lint.lint_note(
            text,
            path=path_s,
            vault_root=root,
            vault=b.name,
            include_links=True,
            include_usage=True,
            include_format=True,
            vault_roots=_vault_roots,
            auto_fix=False,
        )
        out = _attach_flaws(out, lint_flaws)
    return _stamp_qualified(out, vault=b.name, path=path_s)


def graph_export(
    *,
    vault: str = "",
    folder: str = "",
    path: str = "",
    depth: int = 1,
    kinds: list[str] | None = None,
    include_unresolved: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Typed link graph as networkx node-link JSON (renderer hand-off; docs/links.md).

    Scope: whole vault, ``folder`` (both ends inside it), or the ego graph of
    ``path`` out to ``depth`` hops over resolved links (either direction).
    ``include_unresolved`` adds dangling / ambiguous / external / foreign targets
    as nodes. ``limit`` caps nodes (ego/folder nodes kept first) and sets
    ``truncated``. Parallel edges of different kinds share a pair, so the graph
    is a multigraph keyed by ``kind``.
    """
    if depth < 1 or depth > 3:
        return _err(error="bad_request", message="depth must be 1..3")
    if limit is not None and limit < 1:
        return _err(error="bad_request", message="limit must be >= 1 or null")
    if folder and path:
        return _err(error="bad_request", message="pass folder= or path=, not both")
    try:
        b = _binding((vault or "").strip())
    except OpsError as e:
        return _err(error=e.code, message=e.message)
    kind_filter = [k.strip() for k in (kinds or []) if k and k.strip()]
    with vaults.bind(b):
        note_rows, edge_rows = core.graph_snapshot(kind_filter)

    notes: dict[str, dict[str, Any]] = {}
    for rel, fm_json in note_rows:
        fm: dict[str, Any] = {}
        if fm_json:
            try:
                parsed = json.loads(fm_json)
                fm = parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                fm = {}
        node: dict[str, Any] = {"id": rel, "kind": "note", "title": _str_or_none(fm.get("title")) or Path(rel).stem}
        for key in ("okf_type", "status"):
            val = _str_or_none(fm.get(key))
            if val:
                node[key] = val
        notes[rel] = node

    edges: list[dict[str, Any]] = []
    for source, tpath, tkey, kind, status, tvault, count in edge_rows:
        if source not in notes:
            continue
        if tpath:
            target = tpath
        elif include_unresolved:
            target = _unresolved_node(
                {"target_key": tkey, "status": status, "target_vault": tvault}
            )
        else:
            continue
        edges.append(
            {"source": source, "target": target, "key": kind, "kind": kind, "status": status, "count": count}
        )

    if path:
        rel = path.replace("\\", "/").strip("/")
        if rel not in notes and f"{rel}.md" in notes:
            rel = f"{rel}.md"
        if rel not in notes:
            return _err(path=path, error="not_found", message="path is not an indexed note")
        adj: dict[str, set[str]] = {}
        for e in edges:
            if e["status"] == "resolved":
                adj.setdefault(e["source"], set()).add(e["target"])
                adj.setdefault(e["target"], set()).add(e["source"])
        ordered = [rel]
        seen = {rel}
        frontier = [rel]
        for _ in range(depth):
            nxt: list[str] = []
            for n in frontier:
                for m in sorted(adj.get(n, ())):
                    if m not in seen:
                        seen.add(m)
                        ordered.append(m)
                        nxt.append(m)
            frontier = nxt
        scope: list[str] | None = ordered
    elif folder:
        prefix = folder.replace("\\", "/").strip("/") + "/"
        scope = [p for p in notes if p.startswith(prefix)]
    else:
        scope = None

    if scope is not None:
        keep = set(scope)
        edges = [e for e in edges if e["source"] in keep and (e["target"] in keep or e["target"] not in notes)]
        if path:
            # Ego graph: unresolved targets only of the focus note's own links.
            edges = [e for e in edges if e["target"] in keep or e["source"] == scope[0]]
        node_ids = list(scope)
    else:
        node_ids = list(notes)
    extra = sorted({e["target"] for e in edges if e["target"] not in notes})
    node_ids.extend(extra)

    truncated = False
    if limit is not None and len(node_ids) > limit:
        node_ids = node_ids[:limit]
        truncated = True
    keep_ids = set(node_ids)
    edges = [e for e in edges if e["source"] in keep_ids and e["target"] in keep_ids]

    by_target_status = {e["target"]: e["status"] for e in edges if e["target"] not in notes}
    nodes_out: list[dict[str, Any]] = []
    for nid in node_ids:
        if nid in notes:
            nodes_out.append(notes[nid])
        else:
            st = by_target_status.get(nid, "dangling")
            nodes_out.append({"id": nid, "kind": "external" if st == "external" else st})

    graph: dict[str, Any] = {
        "vault": b.name,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if path:
        graph.update({"focus": scope[0] if scope else path, "depth": depth})
    if folder:
        graph["folder"] = folder
    if kind_filter:
        graph["kinds"] = kind_filter
    return {
        "ok": True,
        "directed": True,
        "multigraph": True,
        "graph": graph,
        "nodes": nodes_out,
        "links": edges,
        "node_count": len(nodes_out),
        "link_count": len(edges),
        "truncated": truncated,
        "vault": b.name,
    }


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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
        rows = core.list_backlinks(
            targets, exclude_source, limit + offset + 1, target_path=exclude_source or None
        )
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


def _unresolved_node(row: dict[str, Any]) -> str:
    """Display id for a link target that is not an indexed note."""
    key = row["target_key"]
    if row["status"] == "external":
        return key
    if row["status"] == "foreign" and row.get("target_vault"):
        return f"{row['target_vault']}:{key}"
    return key if key.endswith(links.NOTE_SUFFIXES) else f"{key}.md"


def graph_neighbors(
    path: str,
    *,
    depth: int = 1,
    direction: str = "both",
    limit: int = 50,
    offset: int = 0,
    vault: str = "",
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    """Index-backed link graph: inbound and/or outbound typed links (``kinds`` filters)."""
    direction = (direction or "both").strip().lower()
    if direction not in {"in", "out", "both"}:
        return _err(error="bad_request", message="direction must be in|out|both")
    if depth < 1 or depth > 3:
        return _err(error="bad_request", message="depth must be 1..3")
    try:
        b, rel_path = _resolve_binding_and_rel(path, vault)
        root = b.resolved().root
        _safe_resolve(root, rel_path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    rel = str(Path(rel_path.replace("\\", "/")))
    rel_md = rel if rel.endswith(links.NOTE_SUFFIXES) else f"{rel}.md"
    kind_filter = [k.strip() for k in (kinds or []) if k and k.strip()]

    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str, str]] = set()
    edges: list[dict[str, Any]] = []
    frontier: set[str] = {rel_md}
    visited: set[str] = set()

    def _edge(src: str, dst: str, way: str, hop: int, row: dict[str, Any]) -> None:
        edges.append(
            {
                "from": src,
                "to": dst,
                "direction": way,
                "hop": hop,
                "kind": row["kind"],
                "status": row["status"],
                "line": row["line"],
                "text": row["text"],
                "qualified_from": _qualified_path(b.name, src),
                "qualified_to": _qualified_path(b.name, dst),
            }
        )

    with vaults.bind(b):
        for hop in range(depth):
            next_frontier: set[str] = set()
            for node in sorted(frontier):
                if node in visited:
                    continue
                visited.add(node)
                seen_nodes.add(node)
                node_key = links.link_key(node)
                node_targets = {node_key, node_key.rsplit("/", 1)[-1]}

                if direction in {"in", "both"}:
                    for row in core.list_links(
                        target_path=node, target_keys=node_targets, kinds=kind_filter, limit=limit * 2
                    ):
                        src = row["source"]
                        edge_key = (src, "in", node, row["kind"])
                        if edge_key in seen_edges:
                            continue
                        seen_edges.add(edge_key)
                        _edge(src, node, "in", hop + 1, row)
                        seen_nodes.add(src)
                        if hop + 1 < depth:
                            next_frontier.add(src)

                if direction in {"out", "both"}:
                    for row in core.list_links(source=node, kinds=kind_filter, limit=limit * 2):
                        tgt = row["target_path"] or _unresolved_node(row)
                        edge_key = (node, "out", tgt, row["kind"])
                        if edge_key in seen_edges:
                            continue
                        seen_edges.add(edge_key)
                        _edge(node, tgt, "out", hop + 1, row)
                        seen_nodes.add(tgt)
                        if hop + 1 < depth and row["target_path"]:
                            next_frontier.add(tgt)
            frontier = next_frontier - visited

    has_more = len(edges) > offset + limit
    page = edges[offset : offset + limit]
    out = {
        "ok": True,
        "path": rel_path,
        "depth": depth,
        "direction": direction,
        "nodes": sorted(seen_nodes),
        "edges": page,
        "total_edges": len(edges),
        "has_more": has_more,
        "vault": b.name,
    }
    if offset:
        out["offset"] = offset
    return _stamp_qualified(out, vault=b.name, path=rel_path)


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
        query="",
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
