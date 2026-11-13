"""Isolated SQLite storage for git-ref note catalogs."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config, vaults

_schema_ready: set[str] = set()
_schema_lock = threading.Lock()
_writer_local = threading.local()
_reader_local = threading.local()
_build_locks_guard = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}


def path_for(main_index: Path) -> Path:
    """Return the sibling ref-catalog path for a main index path."""
    index = Path(main_index)
    return index.with_name(f"{index.stem}-refs{index.suffix}")


def active_path() -> Path:
    return path_for(Path(vaults.index_path()).resolve())


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ref_trees (
            tree_oid TEXT PRIMARY KEY,
            commit_oid TEXT NOT NULL,
            built_at REAL NOT NULL,
            catalog_version TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS ref_files (
            tree_oid TEXT NOT NULL,
            path TEXT NOT NULL,
            mtime REAL NOT NULL,
            frontmatter TEXT,
            PRIMARY KEY (tree_oid, path)
        );
        CREATE INDEX IF NOT EXISTS ref_files_tree ON ref_files(tree_oid);
        CREATE VIRTUAL TABLE IF NOT EXISTS ref_fts USING fts5(
            tree_oid UNINDEXED,
            path UNINDEXED,
            mtime UNINDEXED,
            text
        );
        """
    )


def _open(path: Path, *, read_only: bool) -> sqlite3.Connection:
    timeout = config.DB_READ_TIMEOUT if read_only else config.DB_TIMEOUT
    if read_only:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=timeout)
    db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return db


def connect(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    catalog = Path(path or active_path()).resolve()
    db = _open(catalog, read_only=read_only)
    if not read_only:
        key = str(catalog)
        with _schema_lock:
            if key not in _schema_ready:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(f"PRAGMA journal_size_limit={config.WAL_LIMIT_BYTES}")
                ensure_schema(db)
                db.commit()
                _schema_ready.add(key)
    return db


def _connections(local: threading.local) -> dict[str, sqlite3.Connection]:
    conns = getattr(local, "connections", None)
    if conns is None:
        conns = {}
        local.connections = conns
    return conns


def writer_connect(path: Path | None = None) -> sqlite3.Connection:
    catalog = Path(path or active_path()).resolve()
    key = str(catalog)
    conns = _connections(_writer_local)
    db = conns.get(key)
    if db is not None:
        try:
            db.execute("SELECT 1")
            return db
        except sqlite3.Error:
            close_writer(path=catalog)
    db = connect(catalog)
    conns[key] = db
    return db


def reader_connect(path: Path | None = None) -> sqlite3.Connection:
    catalog = Path(path or active_path()).resolve()
    key = str(catalog)
    conns = _connections(_reader_local)
    db = conns.get(key)
    if db is not None:
        try:
            db.execute("SELECT 1")
            return db
        except sqlite3.Error:
            close_reader(path=catalog)
    db = connect(catalog, read_only=True)
    conns[key] = db
    return db


def close_writer(*, path: Path | None = None) -> None:
    _close(_writer_local, path)


def close_reader(*, path: Path | None = None) -> None:
    _close(_reader_local, path)


def _close(local: threading.local, path: Path | None) -> None:
    conns = _connections(local)
    keys = list(conns) if path is None else [str(Path(path).resolve())]
    for key in keys:
        db = conns.pop(key, None)
        if db is None:
            continue
        try:
            db.close()
        except sqlite3.Error:
            pass


@contextmanager
def build_lock(path: Path | None = None) -> Iterator[None]:
    """Serialize first-build checks for one ref catalog in this process."""
    key = str(Path(path or active_path()).resolve())
    with _build_locks_guard:
        lock = _build_locks.setdefault(key, threading.Lock())
    with lock:
        yield
