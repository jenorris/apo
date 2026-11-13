"""Link extraction, resolution, storage, and graph queries."""

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

def _note_links(
    rel: str,
    text: str,
    fm_json: str | None,
    *,
    known_vaults: frozenset[str] | None,
) -> list[links.Link]:
    """Outbound typed links for one note (markdown: body + link fields; YAML/mmd: link fields)."""
    from apo_engine.note_format import is_mmd_note, is_yaml_note

    fm: dict | None = None
    if fm_json:
        try:
            parsed = json.loads(fm_json)
            fm = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            fm = None
    body = not (is_yaml_note(rel) or is_mmd_note(rel))
    if not body and not fm:
        return []
    return links.extract_links(
        rel,
        text,
        fm,
        link_fields=_link_fields_for(rel),
        body=body,
        known_vaults=known_vaults,
    )


def _links_contract():
    """The bound vault's OKF contract (mtime-cached), or None."""
    from apo_engine.okf import contract as okf_contract

    try:
        return okf_contract.get_contract(vaults.notes_root())
    except Exception:
        return None


def _link_fields_for(rel: str) -> tuple[tuple[str, str], ...]:
    from apo_engine.okf import contract as okf_contract

    return okf_contract.link_fields_for(_links_contract(), rel)


def links_state() -> str:
    """Extractor version + the contract's link-field fingerprint (stored in meta)."""
    from apo_engine.okf import contract as okf_contract

    return f"{links.LINKS_SCHEMA_VERSION}:{okf_contract.link_fields_fingerprint(_links_contract())}"


def _insert_links(db: sqlite3.Connection, rel: str, rows: list[links.Link]) -> None:
    if not rows:
        return
    db.executemany(
        "INSERT INTO backlinks(source, target_key, target_stem, line, text, kind, mode, status, "
        "target_vault, alias, heading) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (rel, r.target_key, r.target_stem, r.line, r.text, r.kind, r.mode, r.status,
             r.target_vault, r.alias, r.heading)
            for r in rows
        ],
    )


def _link_keys_for_paths(db: sqlite3.Connection, paths: Iterable[str]) -> set[str]:
    """Every key a link could use to reach these paths: full key, stem, current title."""
    keys: set[str] = set()
    plist = [p for p in paths if p]
    for p in plist:
        k = links.link_key(p)
        keys.add(k)
        keys.add(k.rsplit("/", 1)[-1])
    for i in range(0, len(plist), 400):
        chunk = plist[i : i + 400]
        qs = ",".join("?" * len(chunk))
        for (title,) in db.execute(
            f"SELECT link_title FROM files WHERE path IN ({qs}) AND link_title IS NOT NULL", chunk
        ):
            keys.add(title)
    return keys


_RESOLVE_SCOPED_MAX_PARAMS = 2000


def resolve_links(
    db: sqlite3.Connection,
    *,
    sources: Iterable[str] | None = None,
    paths: Iterable[str] | None = None,
    keys: Iterable[str] | None = None,
) -> None:
    """Set-based link resolution against ``files`` (no args → every row).

    Scoped form re-resolves rows emitted by ``sources`` plus rows that could point at
    ``paths`` (added, changed or removed notes) — by their keys or current target.
    An exact vault-path match wins; a name (stem or title) resolves only when unique,
    else ``ambiguous``; no match is ``dangling``. ``external``/``foreign`` rows are final.
    """
    where = "status NOT IN ('external','foreign')"
    params: list[Any] = []
    if sources is not None or paths is not None or keys is not None:
        src = sorted(set(sources or ()))
        pth = sorted(set(paths or ()))
        key_set = set(keys or ()) | _link_keys_for_paths(db, pth)
        clauses: list[str] = []
        if src:
            clauses.append(f"source IN ({','.join('?' * len(src))})")
            params.extend(src)
        if pth:
            clauses.append(f"target_path IN ({','.join('?' * len(pth))})")
            params.extend(pth)
        if key_set:
            ks = sorted(key_set)
            clauses.append(f"target_key IN ({','.join('?' * len(ks))})")
            params.extend(ks)
        if not clauses:
            return
        if len(params) <= _RESOLVE_SCOPED_MAX_PARAMS:
            where += " AND (" + " OR ".join(clauses) + ")"
        else:
            params = []  # big batch (first scan) — one full pass beats huge IN lists
    name_match = (
        "FROM files f WHERE b.mode = 'name' AND instr(b.target_key, '/') = 0 "
        "AND (f.link_stem = b.target_key OR f.link_title = b.target_key)"
    )
    target = (
        "COALESCE((SELECT MIN(f.path) FROM files f WHERE f.link_key = b.target_key), "
        f"(SELECT CASE WHEN COUNT(DISTINCT f.path) = 1 THEN MIN(f.path) END {name_match}))"
    )
    # One statement: SET expressions see the pre-update row, so status recomputes target.
    db.execute(
        f"""UPDATE backlinks AS b SET
                target_path = {target},
                status = CASE
                    WHEN {target} IS NOT NULL THEN 'resolved'
                    WHEN (SELECT COUNT(DISTINCT f.path) {name_match}) > 1 THEN 'ambiguous'
                    ELSE 'dangling' END
            WHERE {where}""",
        params,
    )


def links_backfill_needed(db: sqlite3.Connection) -> bool:
    """True when stored links predate this extractor or the contract's link fields."""
    row = db.execute("SELECT value FROM meta WHERE key='links_schema_version'").fetchone()
    return not row or row[0] != links_state()


def _mark_links_current(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('links_schema_version', ?)",
        (links_state(),),
    )


def backfill_links(*, verbose: bool = False) -> int:
    """Re-extract every indexed note's links from disk — no chunk or vector writes.

    Runs on the single writer (watcher scan / CLI). Covers embed-backoff and
    quarantined notes too, since it walks ``files`` rather than the change scan.
    Returns the number of notes re-extracted.
    """
    root = vaults.notes_root()
    db = writer_connect()
    known_vaults = _known_vault_ids()
    rels = [r[0] for r in db.execute("SELECT path FROM files")]
    done = 0
    for rel in rels:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm_json = note_catalog_json(text, rel)
        db.execute("DELETE FROM backlinks WHERE source=?", (rel,))
        _insert_links(db, rel, _note_links(rel, text, fm_json, known_vaults=known_vaults))
        done += 1
    resolve_links(db)
    _mark_links_current(db)
    db.commit()
    if verbose:
        print(f"  links: re-extracted {done} note(s)", flush=True)
    return done
# files.link_* — generated columns the link resolver joins on. SQLite lower() is
# ASCII-only; links.link_key() lowercases the same way on the Python side.
_FILES_LINK_KEY_SQL = (
    "lower(CASE WHEN substr(path, -3) = '.md' THEN substr(path, 1, length(path) - 3) ELSE path END)"
)
_FILES_LINK_COLUMNS = (
    ("link_key", _FILES_LINK_KEY_SQL),
    ("link_stem", "substr(link_key, length(rtrim(link_key, replace(link_key, '/', ''))) + 1)"),
    (
        "link_title",
        "CASE WHEN json_valid(frontmatter) AND json_type(frontmatter, '$.title') = 'text' "
        "THEN lower(trim(json_extract(frontmatter, '$.title'))) END",
    ),
)
_BACKLINKS_LINK_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'wikilink'"),
    ("mode", "TEXT NOT NULL DEFAULT 'name'"),
    ("target_path", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'unresolved'"),
    ("target_vault", "TEXT"),
    ("alias", "TEXT"),
    ("heading", "TEXT"),
)


def _ensure_link_columns(db: sqlite3.Connection) -> None:
    """Typed-link columns on ``backlinks`` + resolver keys on ``files`` (additive ALTERs)."""
    fcols = {row[1] for row in db.execute("PRAGMA table_xinfo(files)").fetchall()}
    for name, expr in _FILES_LINK_COLUMNS:
        if name not in fcols:
            db.execute(f"ALTER TABLE files ADD COLUMN {name} TEXT GENERATED ALWAYS AS ({expr}) VIRTUAL")
    bcols = {row[1] for row in db.execute("PRAGMA table_info(backlinks)").fetchall()}
    for name, ddl in _BACKLINKS_LINK_COLUMNS:
        if name not in bcols:
            db.execute(f"ALTER TABLE backlinks ADD COLUMN {name} {ddl}")
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS files_link_key ON files(link_key);
        CREATE INDEX IF NOT EXISTS files_link_stem ON files(link_stem);
        CREATE INDEX IF NOT EXISTS files_link_title ON files(link_title);
        CREATE INDEX IF NOT EXISTS backlinks_target_path ON backlinks(target_path);
        """
    )


def links_ready(db: sqlite3.Connection) -> bool:
    """True when this index has the typed-link schema (readers on an un-migrated index fall back)."""
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(backlinks)").fetchall()}
    except sqlite3.Error:
        return False
    return "status" in cols
_LINK_COLS = "source, target_key, target_path, kind, status, line, text, alias, heading, target_vault"


def _link_row(r: tuple) -> dict[str, Any]:
    source, key, tpath, kind, status, line, text, alias, heading, tvault = r
    return {
        "source": source,
        "target": tpath or key,
        "target_key": key,
        "target_path": tpath,
        "kind": kind,
        "status": status,
        "line": line,
        "text": text,
        "alias": alias,
        "heading": heading,
        "target_vault": tvault,
    }


def _kinds_sql(kinds: Iterable[str] | None, params: list[Any]) -> str:
    ks = sorted({k for k in (kinds or ()) if k})
    if not ks:
        return ""
    params.extend(ks)
    return f" AND kind IN ({','.join('?' * len(ks))})"


def _links_sql(
    typed: bool,
    *,
    source: str | None = None,
    target_path: str | None = None,
    target_keys: set[str] | None = None,
    kinds: Iterable[str] | None = None,
    exclude_source: str = "",
    limit: int = 100,
) -> tuple[str, list[Any]] | None:
    """``(sql, params)`` for one :func:`list_links` query, or None when it can match nothing."""
    params: list[Any] = []
    if source is not None:
        cols = _LINK_COLS if typed else "source, target_key, NULL, 'wikilink', 'unresolved', line, text, NULL, NULL, NULL"
        sql = f"SELECT {cols} FROM backlinks WHERE source = ?"
        params.append(source)
        if typed:
            sql += _kinds_sql(kinds, params)
        sql += " ORDER BY line"
    else:
        keys = sorted(target_keys or ())
        kq = ",".join("?" * len(keys))
        if not typed:
            if not keys:
                return None
            sql = (
                "SELECT source, target_key, NULL, 'wikilink', 'unresolved', line, text, NULL, NULL, NULL "
                f"FROM backlinks WHERE (target_key IN ({kq}) OR target_stem IN ({kq}))"
            )
            params.extend([*keys, *keys])
        elif target_path:
            # 'unresolved' = legacy rows not yet backfilled; 'dangling' + exact key = the
            # target exists on disk but its index row has not landed yet.
            sql = f"SELECT {_LINK_COLS} FROM backlinks WHERE (target_path = ?"
            params.append(target_path)
            if keys:
                sql += (
                    f" OR (status = 'unresolved' AND (target_key IN ({kq}) OR target_stem IN ({kq})))"
                    f" OR (status = 'dangling' AND target_key IN ({kq}))"
                )
                params.extend([*keys, *keys, *keys])
            sql += ")"
            sql += _kinds_sql(kinds, params)
        else:
            if not keys:
                return None
            sql = (
                f"SELECT {_LINK_COLS} FROM backlinks WHERE status IN ('unresolved','dangling') "
                f"AND (target_key IN ({kq}) OR target_stem IN ({kq}))"
            )
            params.extend([*keys, *keys])
            sql += _kinds_sql(kinds, params)
        if exclude_source:
            sql += " AND source != ?"
            params.append(exclude_source)
        sql += " ORDER BY source, line"
    sql += " LIMIT ?"
    params.append(limit)
    return sql, params


def list_links(
    *,
    source: str | None = None,
    target_path: str | None = None,
    target_keys: set[str] | None = None,
    kinds: Iterable[str] | None = None,
    exclude_source: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Typed link rows — outbound (``source``) or inbound (``target_path`` / ``target_keys``).

    Inbound: rows resolved to ``target_path``; when the target is not an indexed note,
    rows whose key or stem matches ``target_keys`` (dangling links to it). Ambiguous
    rows never match — a ``[[stem]]`` shared by two notes belongs to neither.
    On an index without the typed-link schema, falls back to wiki-link key matching.
    """
    db = reader_connect()
    built = _links_sql(
        links_ready(db),
        source=source,
        target_path=target_path,
        target_keys=target_keys,
        kinds=kinds,
        exclude_source=exclude_source,
        limit=limit,
    )
    if built is None:
        return []
    sql, params = built
    return [_link_row(r) for r in db.execute(sql, params).fetchall()]


def list_links_many(queries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Run several :func:`list_links` queries (same keyword args each) in one SQL statement.

    Returns one row list per query, in order, each identical to what ``list_links`` would
    return for it (every query keeps its own ORDER BY / LIMIT inside a subselect).
    """
    db = reader_connect()
    typed = links_ready(db)
    parts: list[str] = []
    params: list[Any] = []
    for idx, query in enumerate(queries):
        built = _links_sql(typed, **query)
        if built is None:
            continue
        sql, p = built
        parts.append(f"SELECT {idx} AS q, * FROM ({sql})")
        params.extend(p)
    out: list[list[dict[str, Any]]] = [[] for _ in queries]
    if not parts:
        return out
    for row in db.execute(" UNION ALL ".join(parts), params).fetchall():
        out[row[0]].append(_link_row(row[1:]))
    return out


def graph_snapshot(kinds: Iterable[str] | None = None) -> tuple[list[tuple], list[tuple]]:
    """Whole-vault link graph from the index: (note rows, edge rows).

    Notes: (path, frontmatter JSON). Edges, aggregated per (source, target, kind):
    (source, target_path, target_key, kind, status, target_vault, count).
    """
    db = reader_connect()
    notes = db.execute("SELECT path, frontmatter FROM files ORDER BY path").fetchall()
    if not links_ready(db):
        return notes, []
    params: list[Any] = []
    kind_sql = _kinds_sql(kinds, params).replace(" AND ", " WHERE ", 1)
    edges = db.execute(
        f"""SELECT source, target_path, target_key, kind, status, target_vault, COUNT(*)
            FROM backlinks{kind_sql}
            GROUP BY source, COALESCE(target_path, target_key), kind
            ORDER BY source, kind, COALESCE(target_path, target_key)""",
        params,
    ).fetchall()
    return notes, edges


def list_outlinks(source: str, *, limit: int = 100) -> list[tuple[str, int, str]]:
    """(target, line, line text) for links emitted from source (resolved path when known)."""
    rel = source.replace("\\", "/").strip("/")
    if not rel.endswith(links.NOTE_SUFFIXES):
        rel = f"{rel}.md"
    return [(r["target"], r["line"], r["text"]) for r in list_links(source=rel, limit=limit)]


def list_backlinks(
    target_keys: set[str],
    exclude_source: str = "",
    limit: int = 100,
    *,
    target_path: str | None = None,
) -> list[tuple[str, int, str]]:
    """(source path, line, line text) for notes linking to a note.

    Pass ``target_path`` (the note's vault-relative path) when it is indexed;
    ``target_keys`` (stem / path / title keys) cover legacy rows and missing targets.
    """
    if not target_keys and not target_path:
        return []
    rows = list_links(
        target_path=target_path,
        target_keys=target_keys,
        exclude_source=exclude_source,
        limit=limit,
    )
    return [(r["source"], r["line"], r["text"]) for r in rows]

__owned__ = ['links_ready', 'list_links', 'list_backlinks', '_note_links', '_links_contract', '_link_fields_for', 'links_state', '_insert_links', '_link_keys_for_paths', '_RESOLVE_SCOPED_MAX_PARAMS', 'resolve_links', 'links_backfill_needed', '_mark_links_current', 'backfill_links', '_FILES_LINK_KEY_SQL', '_FILES_LINK_COLUMNS', '_BACKLINKS_LINK_COLUMNS', '_ensure_link_columns', '_LINK_COLS', '_link_row', '_kinds_sql', '_links_sql', 'list_links_many', 'graph_snapshot', 'list_outlinks']
