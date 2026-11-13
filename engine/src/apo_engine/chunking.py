"""Markdown and Mermaid chunk production."""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import json
import logging
import os
import re
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cmp_to_key
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator

import sqlite_vec
import yaml

try:
    import numpy as _np
except ImportError:
    _np = None

from . import config, links, oversize, ref_catalog, rerank, table_markdown, vaults
from .index_models import Hit, IndexStats, QueueStats
from .markdown_sections import BREADCRUMB_SEP, split_sections

def slice_lines(
    text: str, start_char: int, end_char: int, base_line: int
) -> tuple[int, int]:
    if end_char <= start_char:
        line = base_line + text[:start_char].count("\n")
        return line, line
    start = base_line + text[:start_char].count("\n")
    end = base_line + text[: max(start_char, end_char - 1)].count("\n")
    return start, end

def section_markdown(text: str) -> list[tuple[str, int, str, int, int]]:
    """Return breadcrumb, level, text, and line span for each section."""
    body, body_line = body_start_line(text)
    if not body.strip():
        return []
    lines = body.split("\n")
    sections: list[tuple[str, int, str, int, int]] = []
    for span in split_sections(lines):
        start_idx = span.heading_line if span.heading_line >= 0 else span.body_start
        section_text = "\n".join(lines[start_idx : span.body_end]).strip()
        if not section_text:
            continue
        breadcrumb = BREADCRUMB_SEP.join(span.breadcrumb)
        start_line = body_line + start_idx
        end_line = (
            body_line + span.body_end - 1
            if span.body_end > start_idx
            else start_line
        )
        sections.append(
            (breadcrumb, span.level, section_text, start_line, end_line)
        )
    return sections

def chunk_markdown(
    text: str, max_chars: int = 0, overlap: int = 0
) -> list[tuple[str, int, str, int, int]]:
    """Deprecated section-indexing alias."""
    del max_chars, overlap
    return section_markdown(text)
def _note_title(text: str, rel: str) -> str:
    """Breadcrumb root for row embeds: frontmatter title, else filename stem."""
    fm = note_frontmatter(text, rel) or {}
    title = fm.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    stem = rel.rsplit("/", 1)[-1]
    for suf in (".md", ".markdown"):
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    return stem.replace("-", " ").replace("_", " ").strip()


def _markdown_chunk_rows(text: str, rel: str) -> list[tuple]:
    """Produce index rows for a markdown note: prose sections + table header/rows.

    Returns 11-field pending tuples
    ``(rel, ord, breadcrumb, text, start_line, end_line, level, chunk_id, content_hash,
    section_bytes, meta)`` where ``meta`` is ``None`` for prose sections or a dict with
    ``chunk_kind`` / table identity for ``table_header`` / ``table_row`` chunks.

    Each table stays inside its owning section chunk (prose recall unchanged) *and*
    additionally emits one ``table_header`` chunk plus one ``table_row`` chunk per data
    row so natural-language queries retrieve the specific row. Tables are never split
    mid-table.
    """
    from .markdown_sections import BREADCRUMB_SEP
    from . import table_markdown as tm

    id_prefix = f"markdown:{rel}"
    model = config.MODEL_NAME
    note_title = _note_title(text, rel)

    sections = section_markdown(text)
    rows: list[tuple] = []
    ord_counter = 0

    # Map absolute line → owning section (breadcrumb + chunk_id) for table parenting.
    section_spans: list[tuple[int, int, str, str]] = []  # (start, end, breadcrumb, chunk_id)
    for si, (breadcrumb, level, ctext, start_line, end_line) in enumerate(sections):
        chash = _content_hash(ctext)
        chunk_id = compute_chunk_id(id_prefix, start_line, end_line, chash, model)
        sec_bytes = len(ctext.encode("utf-8"))
        plan = (
            oversize.plan_section(
                sections,
                si,
                config.EMBED_MAX_CHARS,
                config.EMBED_WINDOW_OVERLAP,
                lambda t: _index_text_for_embedding(t, "section"),
            )
            if config.EMBED_MAX_CHARS > 0
            else None
        )
        meta = {"embed_text": plan.embed_text} if plan else None
        rows.append(
            (rel, ord_counter, breadcrumb, ctext, start_line, end_line, level, chunk_id, chash, sec_bytes, meta)
        )
        ord_counter += 1
        section_spans.append((start_line, end_line, breadcrumb, chunk_id))
        if plan is not None:
            logging.getLogger("apo.index").info(
                "oversize section %s:%s (%d bytes) embedded as %s%s",
                rel,
                breadcrumb or "(preamble)",
                sec_bytes,
                plan.kind,
                f" + {len(plan.parts)} part(s)" if plan.parts else "",
            )
            for win in plan.parts:
                phash = _content_hash(win.text)
                pid = compute_chunk_id(id_prefix, win.start_line, win.end_line, phash, model)
                part_embed = f"{breadcrumb}\n\n{_index_text_for_embedding(win.text, 'section')}"
                rows.append(
                    (
                        rel, ord_counter, breadcrumb, win.text, win.start_line, win.end_line, level,
                        pid, phash, len(win.text.encode("utf-8")),
                        {
                            "chunk_kind": "section_part",
                            "parent_chunk_hash": chunk_id,
                            "embed_text": part_embed,
                        },
                    )
                )
                ord_counter += 1

    body, body_line = _body_start_line(text)
    if not body.strip():
        return rows
    body_lines = body.split("\n")
    tables = tm.find_tables(body_lines)
    # Optional vault table-contract key_column (first matching path glob wins).
    key_column: str | None = None
    try:
        from .table_contract import key_column_for

        key_column = key_column_for(vaults.notes_root(), rel)
    except Exception:
        key_column = None
    for t in tables:
        abs_start = body_line + t.start_line  # 1-based file line of header row
        parent_breadcrumb = ""
        parent_chunk_hash = ""
        for s_start, s_end, bc, cid in section_spans:
            if s_start <= abs_start <= s_end:
                parent_breadcrumb = bc
                parent_chunk_hash = cid
        parent_crumbs = [c for c in parent_breadcrumb.split(BREADCRUMB_SEP) if c]
        if parent_crumbs and parent_crumbs[0] == note_title:
            # Common case: the note's H1 repeats its title (frontmatter or
            # filename stem) — skip the duplicate rather than embedding
            # "Title > Title > ..." (36% of work-vault table rows did this).
            parent_crumbs = parent_crumbs[1:]
        crumbs = [note_title] + parent_crumbs
        table_id = tm.table_id_for(rel, abs_start, t.table_index)
        schema_hash = tm.table_schema_hash(t.headers)
        level = 0
        # Header chunk — schema recall.
        htext = tm.header_flatten_text(crumbs, t.headers)
        hhash = _content_hash(htext)
        hid = compute_chunk_id(id_prefix, abs_start, abs_start + 1, hhash, model)
        rows.append(
            (
                rel, ord_counter, parent_breadcrumb, htext, abs_start, abs_start + 1, level,
                hid, hhash, len(htext.encode("utf-8")),
                {
                    "chunk_kind": "table_header",
                    "parent_chunk_hash": parent_chunk_hash,
                    "table_id": table_id,
                    "table_schema_hash": schema_hash,
                    "row_index": None,
                    "row_key": None,
                },
            )
        )
        ord_counter += 1
        # One chunk per data row.
        first_data_line = abs_start + 2  # header + delimiter
        for ri, row in enumerate(t.rows):
            rtext = tm.row_flatten_text(crumbs, t.headers, row)
            rhash = _content_hash(rtext)
            row_line = first_data_line + ri
            rid = compute_chunk_id(id_prefix, row_line, row_line, rhash, model)
            row_key = tm.row_key_for(t, ri, key_column=key_column)
            rows.append(
                (
                    rel, ord_counter, parent_breadcrumb, rtext, row_line, row_line, level,
                    rid, rhash, len(rtext.encode("utf-8")),
                    {
                        "chunk_kind": "table_row",
                        "parent_chunk_hash": parent_chunk_hash,
                        "table_id": table_id,
                        "table_schema_hash": schema_hash,
                        "row_index": ri,
                        "row_key": row_key,
                    },
                )
            )
            ord_counter += 1
    # Fenced ```mermaid blocks — parallel chunks (table analog).
    from . import mermaid_markdown as mm
    from . import mermaid_index as mi

    fences = mm.find_mermaid_fences(body_lines)
    for fence in fences:
        abs_start = body_line + fence.start_line + 1  # first content line (1-based)
        parent_breadcrumb = ""
        parent_chunk_hash = ""
        for s_start, s_end, bc, cid in section_spans:
            if s_start <= abs_start <= s_end:
                parent_breadcrumb = bc
                parent_chunk_hash = cid
        parent_crumbs = [c for c in parent_breadcrumb.split(BREADCRUMB_SEP) if c]
        if parent_crumbs and parent_crumbs[0] == note_title:
            # Common case: the note's H1 repeats its title (frontmatter or
            # filename stem) — skip the duplicate rather than embedding
            # "Title > Title > ..." (36% of work-vault table rows did this).
            parent_crumbs = parent_crumbs[1:]
        crumbs = [note_title] + parent_crumbs
        mermaid_rows, ord_counter = mi.chunk_mermaid_rows(
            fence.text,
            rel,
            breadcrumb=crumbs,
            block_index=fence.block_index,
            abs_start_line=abs_start,
            parent_chunk_hash=parent_chunk_hash,
            id_prefix=id_prefix,
            model_name=model,
            ord_start=ord_counter,
        )
        rows.extend(mermaid_rows)
    return rows


def _mmd_chunk_rows(text: str, rel: str, mtime: float | None = None) -> list[tuple]:
    """Index rows for a standalone ``.mmd`` diagram file."""
    from . import mermaid_index as mi

    id_prefix = f"mmd:{rel}"
    model = config.MODEL_NAME
    fm = mi.merge_frontmatter_for_mmd(text, rel, mtime=mtime)
    title = str(fm.get("title") or rel.split("/")[-1].replace(".mmd", "").replace("-", " ").title())
    rows, _ = mi.chunk_mermaid_rows(
        text,
        rel,
        breadcrumb=[title],
        abs_start_line=1,
        id_prefix=id_prefix,
        model_name=model,
        ord_start=0,
    )
    return rows
def compute_chunk_id(
    source: str,
    start_line: int,
    end_line: int,
    content_hash: str,
    model: str,
) -> str:
    """Composite chunk ID aligned with memsearch / OpenClaw format."""
    raw = f"markdown:{source}:{start_line}:{end_line}:{content_hash}:{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

__owned__ = ['slice_lines', 'section_markdown', 'chunk_markdown', '_note_title', '_markdown_chunk_rows', '_mmd_chunk_rows', 'compute_chunk_id']
