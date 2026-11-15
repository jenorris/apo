"""Ops mutate surface: write_note, append_note, patch_note, patch_entry and
patch_notes (including table ops). Each mutator holds ``note_write_lock``
around its read-check-write section."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from apo_engine import core, okf as apo_okf, vaults
from apo_engine.agent_args import resolve_body_text
from apo_engine.atomic_io import atomic_write_text, note_write_lock
from apo_engine.chunk_anchor import materialize_ops_chunk_hashes, resolve_chunk_anchor
from apo_engine.json_patch import apply_json_patch
from apo_engine.markdown_patch import (
    apply_append,
    apply_patch,
    find_section,
    minimal_note_stub,
    normalize_lines,
    PatchError,
    section_from_chunk,
)
from apo_engine.note_format import (
    is_json_catalog,
    is_markdown_note,
    is_mmd_note,
    is_yaml_note,
)
from apo_engine.patch_ops import ops_to_dicts
from apo_engine.validation_hints import flatten_patch_failure_error
from apo_engine.write_guard import (
    attach_region_hashes,
    classify_append_regions,
    classify_patch_regions,
    WriteRegions,
)
from apo_engine.yaml_patch import apply_yaml_patch
from apo_engine.ops_common import (
    _attach_flaws,
    _attach_tip,
    _binding,
    _err,
    _mtime,
    _require_note_path,
    _resolve_binding_and_rel,
    _safe_resolve,
    _top_level_dirs,
    OpsError,
)
from apo_engine.ops_guard import (
    _check_mtime,
    _enqueue_index,
    _finalize_write,
    _guard_write,
    _prepare_write_content,
    _reject_if_ref,
)
from apo_engine.ops_place import place_note


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


def _write_body_args_provided(
    *,
    content: str | None,
    text: str | None,
    body: str | None,
    sections: list[dict[str, Any]] | None,
    frontmatter: dict[str, Any] | None,
) -> bool:
    if sections is not None or frontmatter is not None:
        return True
    for val in (content, text, body):
        if val is not None and str(val).strip():
            return True
    return False


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
        b, path = _resolve_binding_and_rel(path, vault, write=True)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    try:
        _require_note_path(path, root=root, full=full)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)

    if is_mmd_note(path) or (is_markdown_note(path) and "```mermaid" in content):
        from apo_engine.mermaid_validate import should_block_write

        block, mflaws = should_block_write(content, path)
        if block:
            return _err(
                path=path,
                error="validation_failed",
                message="mermaid validation failed (hard mode)",
                flaws=mflaws,
            )

    with note_write_lock(full):
        # ``.json`` is always written exactly as supplied: whole-file CAS and no
        # OKF/frontmatter wrapper. Only top-level objects are catalog records.
        if is_json_catalog(path):
            try:
                json_value = json.loads(content)
            except json.JSONDecodeError as e:
                return _err(
                    path=path,
                    error="validation_failed",
                    message=f"invalid JSON at line {e.lineno} col {e.colno}: {e.msg}",
                )
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
            full.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(full, content)
            # Enqueue every valid JSON change. The indexer catalogs objects and
            # purges a formerly indexed object when it becomes an array or scalar.
            _enqueue_index(b, full)
            out = {
                "ok": True,
                "path": path,
                "action": "overwrote" if existed else "created",
                "bytes": full.stat().st_size,
                "mtime": _mtime(full),
                "vault": b.name,
                "catalog_format": "json",
                "tip": "raw catalog write (no OKF frontmatter wrapper)",
            }
            if not isinstance(json_value, dict):
                out["index_status"] = "unsupported_json_top_level"
                out["index_reason"] = "only top-level JSON objects are catalog records"
            attach_region_hashes(out, content)
            out = _finalize_write(
                out,
                vault=b.name,
                path=path,
                expected_mtime=expected_mtime,
                content=content,
            )
            return out

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
        atomic_write_text(full, to_write)
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
            b, path = _resolve_binding_and_rel(path, vault, write=True)
        else:
            b = _binding(vault, write=True)
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

    try:
        _require_note_path(path, root=root, full=full)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)

    if is_yaml_note(path):
        return _err(
            path=path,
            error="unsupported_format",
            message=(
                "append_note is Markdown-only; YAML catalog notes use "
                "write_note / patch_note(set_field|delete_field)"
            ),
        )
    if is_mmd_note(path):
        return _err(
            path=path,
            error="unsupported_format",
            message="append_note is Markdown-only; .mmd diagrams use write_note / patch_note(replace_text)",
        )
    if is_json_catalog(path):
        return _err(
            path=path,
            error="unsupported_format",
            message=(
                "append_note is Markdown-only; JSON catalogs use "
                "write_note / patch_note(set_field|delete_field)"
            ),
        )

    with note_write_lock(full):
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
            atomic_write_text(full, minimal_note_stub(path))
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
        atomic_write_text(full, new_content)
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
    from .table_contract import key_column_of, table_rule_for

    kind = op["op"]
    table, span = _locate_table(content, rel, op.get("table_id"), op.get("heading"))
    table_id = tm.table_id_for(
        rel, core._body_start_line(content)[1] + table.start_line, table.table_index
    )
    # One contract lookup per op: row lookups key by ``key_column``; replace_table
    # also takes merge / allow_new_columns defaults + header_synonyms from the rule.
    rule = table_rule_for(vaults.notes_root(), rel)
    key_column = key_column_of(rule)

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
        _apply_replace_table(table, op, key_column=key_column, rule=rule)
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


def _apply_replace_table(
    table,
    op: dict[str, Any],
    *,
    key_column: str | None = None,
    rule: dict[str, Any] | None = None,
) -> None:
    """``replace_table`` — whole-table swap, or append / upsert onto the existing schema.

    ``rule`` is the matching table-contract rule (may be empty): its ``merge`` and
    ``allow_new_columns`` are the defaults when the op omits them, and its
    ``header_synonyms`` are applied to incoming headers before the fuzzy map.
    ``key_column`` keys the upsert (falls back to first non-empty cell — the same
    natural key the indexer and row ops use).
    """
    from . import table_markdown as tm
    from .table_contract import header_synonyms_of, merge_default_of

    rule = rule or {}
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

    merge = op.get("merge") or merge_default_of(rule) or "replace"
    if merge == "replace":
        table.headers[:] = headers_in
        table.rows[:] = tm.dicts_to_rows(headers_in, records)
        table.alignments[:] = []
        return

    allow_new = op.get("allow_new_columns")
    if allow_new is None:
        allow_new = bool(rule.get("allow_new_columns", False))

    # Contract synonyms first (exact, normalized). A synonym whose canonical
    # header is not in the table is ignored rather than inventing a column.
    existing_by_norm = {tm.normalize_header(h): h for h in table.headers}
    mapping: dict[str, str] = {}
    for inc, canonical in header_synonyms_of(rule).items():
        canon = existing_by_norm.get(tm.normalize_header(canonical))
        if canon is None:
            continue
        for h in headers_in:
            if tm.normalize_header(h) == tm.normalize_header(inc):
                mapping[h] = canon
    unmapped = [h for h in headers_in if h not in mapping]

    # append / upsert keep existing schema; map remaining headers (fuzzy, ambiguity-reject).
    try:
        mapping.update(
            tm.fuzzy_header_map(unmapped, table.headers, allow_new_columns=bool(allow_new))
        )
    except tm.HeaderAmbiguous as e:
        raise _TableOpError("header_ambiguous", str(e), e.suggestions)

    mapped_records: list[dict[str, str]] = []
    for rec in records:
        mapped_records.append({mapping.get(k, k): v for k, v in rec.items()})

    # allow_new_columns: fuzzy_header_map keeps an unmatched header as itself;
    # the table schema has to grow to receive it, or the imported values are
    # silently dropped by dicts_to_rows.
    for target in mapping.values():
        if target not in table.headers:
            table.headers.append(target)
            for row in table.rows:
                row.append("")
            if table.alignments:
                table.alignments.append("")

    if merge == "append":
        table.rows.extend(tm.dicts_to_rows(table.headers, mapped_records))
        return
    if merge == "upsert":
        key_idx = -1
        if key_column:
            target = tm.normalize_header(key_column)
            key_idx = next(
                (i for i, h in enumerate(table.headers) if tm.normalize_header(h) == target),
                -1,
            )
        existing = {
            tm.row_key_for(table, i, key_column=key_column): i for i in range(len(table.rows))
        }
        for rec in mapped_records:
            cells = [str(rec.get(h, "")) for h in table.headers]
            key = cells[key_idx].strip() if key_idx >= 0 else ""
            if not key:
                key = next((c for c in cells if c.strip()), "")
            # last-wins within this import
            if key in existing:
                table.rows[existing[key]] = cells
            else:
                existing[key] = len(table.rows)
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

    atomic_write_text(full, new_content)
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
        b, path = _resolve_binding_and_rel(path, vault, write=True)
        root = b.resolved().root
        full = _safe_resolve(root, path)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)
    except ValueError as e:
        return _err(path=path, error="bad_path", message=str(e))

    try:
        _require_note_path(path, root=root, full=full)
    except OpsError as e:
        return _err(path=path, error=e.code, message=e.message)

    with note_write_lock(full):
        if not full.exists():
            return _err(path=path, error="not_found", message="note not found")

        content = full.read_text(encoding="utf-8")
        try:
            dict_ops = ops_to_dicts(ops)
        except (TypeError, ValueError) as e:
            return _err(path=path, error="bad_request", message=str(e))

        if any(o.get("op") == "place" for o in dict_ops):
            return _err(
                path=path,
                error="bad_request",
                message=(
                    "place op requires place-only patch_note(ops=[{op:place,src,dst}], vault=...) "
                    "without path"
                ),
                tip="omit path when using place; path+ops patch does not support place",
            )

        # Row-keyed table ops take a dedicated, strict path (no markdown section apply).
        if _table_ops_present(dict_ops):
            if is_yaml_note(path) or is_json_catalog(path):
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

        # Whole-document catalogs (.yaml / .json) CAS on the whole file: no
        # frontmatter/body/section split to scope the precondition to.
        catalog = is_yaml_note(path) or is_json_catalog(path)
        regions = classify_patch_regions(dict_ops, yaml_note=catalog)
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
            elif is_json_catalog(path):
                result = apply_json_patch(content, dict_ops, strict=strict)
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
        okf_meta: dict[str, Any] = {}
        write_flaws: list[dict[str, Any]] = []
        if is_json_catalog(path):
            # Raw catalog: no OKF stamp or lint auto-fix. JSON patching only accepts
            # objects, so every successful mutation remains an indexable record.
            atomic_write_text(full, to_write)
            _enqueue_index(b, full)
        else:
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

            atomic_write_text(full, to_write)
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
        allow_cross_vault=bool(op.get("allow_cross_vault")),
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
            "provide path+ops for one note, or place-only ops=[{op:place,src,dst}] "
            "(MCP patch_note); items=[{path?, ops, expected_mtime?}] batch mode is "
            "not exposed via MCP — use apo-local patch --items @file.json instead"
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
            bi, path_rel = _resolve_binding_and_rel(path, vault, write=True)
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
            b = _binding(next(iter(vault_ids)), write=True)
        else:
            b = _binding(vault, write=True)
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

        paths_to_gate: list[str] = []
        if place_only and ops_list:
            place_op = _op_to_dict(ops_list[0])
            val = place_op.get("dst")
            if isinstance(val, str) and val.strip():
                paths_to_gate.append(val.strip())
        elif path:
            paths_to_gate.append(path)
        gate_err: OpsError | None = None
        for gate_path in paths_to_gate:
            try:
                _require_note_path(gate_path, root=b.resolved().root)
            except OpsError as e:
                gate_err = e
                break
        if gate_err:
            entry = {
                "ok": False,
                "path": path or (paths_to_gate[0] if paths_to_gate else None),
                "error": gate_err.code,
                "message": gate_err.message,
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
